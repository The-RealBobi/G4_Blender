"""Build standalone character model resources from authored data, without donors."""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
import zlib

from .g4_model_probe import dds_format, dds_to_nxtch
from .g4_normal_materials import NORMAL_SHADER, serialize_expanded_g4tx
from .g4_port import (
    Mesh, RecordRule, STABLE_CUSTOM_VERTEX_COLOR, build_g4mg,
    build_offset_string_table, matrix_multiply, mesh_tangent_space, split_mesh_by_palette_limit,
    validate_generated_model, validate_model_texture_bindings, vector_cross, vector_dot,
)
try:
    from ..formats.g4sk_writer import G4SKNode, write_g4sk_v104
except ImportError:
    from formats.g4sk_writer import G4SKNode, write_g4sk_v104


@dataclass(frozen=True)
class AuthoredTexture:
    name: str
    dds: bytes


@dataclass(frozen=True)
class AuthoredMaterial:
    name: str
    base_texture: str
    line_texture: str
    occlusion_texture: str
    alpha_blend: bool
    normal_texture: str | None


@dataclass(frozen=True)
class ModelResources:
    g4md: bytes
    g4mg: bytes
    g4sk: bytes
    g4tx: bytes


def validate_authored_bind_pose(nodes: list[G4SKNode]) -> None:
    """Require local SRT, world bind and inverse-bind transforms to agree."""
    identity = (1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.)
    for index, node in enumerate(nodes):
        s = node.local_srt
        if (len(s) != 12 or len(node.global_matrix) != 12 or len(node.inverse_bind_matrix) != 12
                or not all(math.isfinite(v) for v in (*s, *node.global_matrix, *node.inverse_bind_matrix))):
            raise ValueError(f"Joint {node.name!r} has invalid bind transforms")
        if s[3] != 0 or s[11] != 1:
            raise ValueError(f"Joint {node.name!r} requires homogeneous SRT components 0 and 1")
        if node.parent_index is not None and not 0 <= node.parent_index < index:
            raise ValueError(f"Joint {node.name!r} requires its parent before the joint")
        x, y, z, w = s[4:8]
        if abs(x*x + y*y + z*z + w*w - 1.) > 2e-5:
            raise ValueError(f"Joint {node.name!r} has a non-unit bind quaternion")
        sx, sy, sz = s[:3]
        local = ((1-2*(y*y+z*z))*sx, 2*(x*y-z*w)*sy, 2*(x*z+y*w)*sz, s[8],
                 2*(x*y+z*w)*sx, (1-2*(x*x+z*z))*sy, 2*(y*z-x*w)*sz, s[9],
                 2*(x*z-y*w)*sx, 2*(y*z+x*w)*sy, (1-2*(x*x+y*y))*sz, s[10],
                 0., 0., 0., 1.)
        global_matrix = (*node.global_matrix, 0., 0., 0., 1.)
        parent = nodes[node.parent_index] if node.parent_index is not None else None
        expected = matrix_multiply((*parent.global_matrix, 0., 0., 0., 1.), local) if parent else local
        inverse_product = matrix_multiply(global_matrix, (*node.inverse_bind_matrix, 0., 0., 0., 1.))
        for actual, reference, label in ((global_matrix, expected, 'local/global bind'),
                                         (inverse_product, identity, 'inverse bind')):
            if any(not math.isfinite(a) or abs(a-b) > 2e-5 * max(1., abs(a), abs(b))
                   for a, b in zip(actual, reference)):
                raise ValueError(f"Joint {node.name!r} has inconsistent {label} transforms")


def _names(names: list[str], label: str) -> list[int]:
    hashes = []
    for name in names:
        if not name or '\0' in name:
            raise ValueError(f"{label} names must be nonempty and contain no NUL")
        try:
            hashes.append(zlib.crc32(name.encode('ascii')) & 0xFFFFFFFF)
        except UnicodeEncodeError as error:
            raise ValueError(f"{label} name must be ASCII: {name!r}") from error
    if len(hashes) != len(set(hashes)):
        raise ValueError(f"{label} names must have unique CRC32 hashes")
    if len(names) * 2 + sum(len(name) + 1 for name in names) > 65535:
        raise ValueError(f"{label} names exceed the U16 string-table capacity")
    return hashes


def bc7_dds_header(width: int, height: int, mip_count: int) -> bytes:
    """Describe a 2D BC7_UNORM DDS; pixel data is supplied by the encoder."""
    if not 0 < width <= 65535 or not 0 < height <= 65535 or not 1 <= mip_count <= max(width, height).bit_length():
        raise ValueError('Invalid BC7 texture dimensions or mip count')
    header = bytearray(148)
    header[:4] = b'DDS '
    flags = 0x81007 | (0x20000 if mip_count > 1 else 0)
    top_size = ((width + 3) // 4) * ((height + 3) // 4) * 16
    struct.pack_into('<7I', header, 4, 124, flags, height, width, top_size, 1, mip_count)
    struct.pack_into('<II4s', header, 76, 32, 4, b'DX10')
    struct.pack_into('<I', header, 108, 0x1000 | (0x400008 if mip_count > 1 else 0))
    struct.pack_into('<5I', header, 128, 98, 3, 0, 1, 0)
    return bytes(header)


def build_authored_texture_archive(textures: list[AuthoredTexture], platform: str) -> bytes:
    """Build a standalone DX11 DDS or NX NXTCH archive from authored BC7 textures."""
    if platform not in ('dx11', 'nx'):
        raise ValueError(f"Unsupported authored texture platform: {platform!r}")
    _names([texture.name for texture in textures], 'Texture')
    if not 0 < len(textures) <= 255:
        raise ValueError('Standalone G4TX requires 1 to 255 textures')
    header = bytearray(0x60)
    header[:4] = b'G4TX'
    struct.pack_into('<HH', header, 4, 0x60, 101)
    struct.pack_into('<H', header, 10, 0x18)
    entries = []
    for texture in textures:
        data = texture.dds
        if len(data) < 148 or data[:4] != b'DDS ' or data[84:88] != b'DX10':
            raise ValueError(f"Texture {texture.name!r} requires a BC7_UNORM 2D DDS")
        if dds_format(data) != (0x4D, 148) or struct.unpack_from('<5I', data, 128) != (98, 3, 0, 1, 0):
            raise ValueError(f"Texture {texture.name!r} requires a BC7_UNORM 2D DDS")
        height, width = struct.unpack_from('<II', data, 12)
        mip_count = struct.unpack_from('<I', data, 28)[0]
        generated_header = bc7_dds_header(width, height, mip_count)
        pixel_size = sum(((max(1, width >> level) + 3) // 4)
                         * ((max(1, height >> level) + 3) // 4) * 16 for level in range(mip_count))
        if len(data) != 148 + pixel_size:
            raise ValueError(f"Texture {texture.name!r} has an invalid DDS mip payload size")
        record = bytearray(0x30)
        top_size = struct.unpack_from('<I', generated_header, 20)[0]
        payload = generated_header + data[148:]
        flags = 0x30085 | mip_count << 8
        if platform == 'nx':
            payload = dds_to_nxtch(payload, None)
            top_size = 0
            # Native NX NPOT descriptors carry the BC7 row pitch and the 0x20 flag.
            if width & (width - 1) or height & (height - 1):
                flags |= 0x20
                struct.pack_into('<I', record, 0x20, ((width + 3) // 4) * 16)
        struct.pack_into('<IIIHHI', record, 0x0C, top_size, flags,
                         0xAAE4, width, height, 1)
        entries.append({'name': texture.name, 'payload': payload,
                        'template': {'raw': bytes(record)}})
    return serialize_expanded_g4tx(bytes(header), entries)


def _model_descriptor(meshes: list[Mesh], records: list[dict], materials: list[AuthoredMaterial],
                      textures: list[AuthoredTexture], nodes: list[G4SKNode], palettes: list[list[int]]) -> bytes:
    mesh_hashes = _names([mesh.name for mesh in meshes], 'Mesh')
    material_hashes = _names([material.name for material in materials], 'Material')
    texture_names = [texture.name for texture in textures]
    texture_hashes = _names(texture_names, 'Texture')
    joint_hashes = _names([node.name for node in nodes], 'Joint')
    bindings = [(("base", material.base_texture), ("line", material.line_texture), ("oc", material.occlusion_texture))
                + ((("nml", material.normal_texture),) if material.normal_texture is not None else ())
                for material in materials]
    material_shaders = [NORMAL_SHADER if material.normal_texture is not None else 3377185770 for material in materials]
    shader_hashes = list(dict.fromkeys(material_shaders))
    uv_names = [f'{material.name}_{role}' for material, references in zip(materials, bindings) for role, _ in references]
    uv_hashes = _names(uv_names, 'UV binding')
    md = bytearray(0xA0 + len(meshes) * 0x50)
    md[:4] = b'G4MD'
    struct.pack_into('<HH', md, 4, 0xA0, 104)
    struct.pack_into('<H', md, 10, 0x28)
    struct.pack_into('<HHHBBBBH', md, 0x20, len(meshes), len(materials), len(nodes),
                     1, len(textures), len(shader_hashes), 3, len(uv_hashes))
    struct.pack_into('<II', md, 0x50, records[0]['vertex_buffer_size'], records[0]['index_buffer_size'])
    struct.pack_into('<I', md, 0x5C, records[0]['index_base'])

    def bounds(offset: int, vertices) -> None:
        points = [vertex.position for vertex in vertices]
        low = [min(p[axis] for p in points) for axis in range(3)]
        high = [max(p[axis] for p in points) for axis in range(3)]
        struct.pack_into('<4f3f', md, offset, *((a + b) * .5 for a, b in zip(low, high)),
                         1., *((b - a) * .5 for a, b in zip(low, high)))

    bounds(0x30, [vertex for mesh in meshes for vertex in mesh.vertices])
    palette_start = 0
    for index, (mesh, record, palette) in enumerate(zip(meshes, records, palettes)):
        offset = 0xA0 + index * 0x50
        struct.pack_into('<4I', md, offset, record['vertex_offset'], record['index_offset'],
                         record['vertex_count'], record['index_count'])
        bounds(offset + 0x10, mesh.vertices)
        struct.pack_into('<IHHHHH', md, offset + 0x30, len(mesh.indices) // 3,
                         255, 0, index, palette_start, 0x100 | len(palette))
        struct.pack_into('<H', md, offset + 0x3E, 0x48 if record['uv_format_id'] == 2 else 0x44)
        md[offset + 0x43] = mesh.material_index
        palette_start += len(palette)

    def section(field: int, payload: bytes, alignment: int) -> None:
        md.extend(bytes(-len(md) % alignment))
        units = (len(md) - 0xA0) // 4
        if units > 65535:
            raise ValueError('Authored G4MD sections exceed the U16 offset capacity')
        struct.pack_into('<H', md, field, units)
        md.extend(payload)

    uv_format = records[0]['uv_format_id']
    elements = [(1, 0, 3), (2, 12, 20), (13, 20, 20), (3, 28, 20),
                (5, 36, 32), (6, 52, 24), (8, 60, 12),
                (10, 64, uv_format), (11, 64, uv_format), (12, 64, uv_format)]
    section(0x62, bytes((0, len(elements), 0, 0, 0, 0, 0, 0))
            + b''.join(struct.pack('<BHBI', kind, offset, 0, fmt) for kind, offset, fmt in elements), 4)
    states = bytearray()
    material_records = bytearray()
    refs = bytearray()
    reference_start = 0
    for index, (material, references) in enumerate(zip(materials, bindings)):
        render_states = {1: 0, 4: 1, 5: 1, 6: 0, 14: 1}
        if material.alpha_blend:
            render_states.update({5: 0, 6: 1, 7: 1, 9: 4, 10: 5})
        shader_index = shader_hashes.index(material_shaders[index])
        material_records.extend(struct.pack('<8H', 0, 0, len(states) // 2, reference_start,
                                            15 | shader_index << 8, len(render_states) << 8 | 3,
                                            len(references), reference_start))
        states.extend(value for pair in render_states.items() for value in pair)
        for _role, name in references:
            if name not in texture_names:
                raise ValueError(f"Material {material.name!r} requires missing texture {name!r}")
            refs.extend(bytes((texture_names.index(name), 3, 0, 0, 0, 0)))
        reference_start += len(references)
    section(0x64, bytes(material_records), 16)
    section(0x66, struct.pack('<12f', 1, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0), 4)
    section(0x68, bytes(refs), 4)
    section(0x6A, struct.pack('<12f', 0, 0, 0, 0, 0, 0, 0, 0, .9921875, .9765625, 0, 0), 4)
    section(0x88, struct.pack('<8f', 1, 0, 0, 0, 0, 1, 0, 0) * len(uv_hashes), 4)
    section(0x8A, struct.pack('<4f', 1, 1, 0, 0) * len(uv_hashes), 4)
    section(0x6C, bytes(states), 4)
    section(0x6E, bytes((1, 1, 2, 1, 3, 1)), 4)
    for field, hashes in ((0x70, mesh_hashes), (0x72, material_hashes), (0x74, joint_hashes),
                          (0x76, texture_hashes), (0x78, shader_hashes), (0x7A, uv_hashes)):
        section(field, struct.pack(f'<{len(hashes)}I', *hashes), 4)
    for field, hashes in ((0x7C, mesh_hashes), (0x7E, material_hashes), (0x80, uv_hashes)):
        order = sorted(range(len(hashes)), key=hashes.__getitem__)
        section(field, struct.pack(f'<{len(order)}H', *order), 4)
    flat_palette = [joint for palette in palettes for joint in palette]
    section(0x82, struct.pack(f'<{len(flat_palette)}H', *flat_palette), 4)
    section(0x84, build_offset_string_table([mesh.name for mesh in meshes]), 4)
    section(0x86, build_offset_string_table([material.name for material in materials]), 4)
    md.extend(bytes(-len(md) % 4))
    struct.pack_into('<I', md, 0x0C, len(md) - 0xA0)
    return bytes(md)


def build_authored_model(meshes: list[Mesh], materials: list[AuthoredMaterial],
                         textures: list[AuthoredTexture], nodes: list[G4SKNode]) -> ModelResources:
    if not meshes or not 0 < len(materials) <= 256:
        raise ValueError('Authored export requires meshes and 1 to 256 materials')
    joint_indices = {node.name: index for index, node in enumerate(nodes)}
    header = bytearray(0x40)
    header[:4] = b'G4SK'
    struct.pack_into('<HH', header, 4, 0x40, 104)
    struct.pack_into('<H', header, 10, 0x10)
    header[0x22] = 4
    skeleton = write_g4sk_v104(bytes(header), nodes)
    validate_authored_bind_pose(nodes)
    archive = build_authored_texture_archive(textures, 'dx11')
    prepared = []
    for mesh in meshes:
        if not mesh.vertices or not mesh.indices or len(mesh.indices) % 3:
            raise ValueError(f"Mesh {mesh.name!r} requires nonempty triangles")
        if not 0 <= mesh.material_index < len(materials) or materials[mesh.material_index].name != mesh.material_name:
            raise ValueError(f"Mesh {mesh.name!r} has an invalid material assignment")
        if any(not 0 <= index < len(mesh.vertices) for index in mesh.indices):
            raise ValueError(f"Mesh {mesh.name!r} has an invalid triangle index")
        for vertex in mesh.vertices:
            if (len(vertex.position) != 3 or len(vertex.normal) != 3 or len(vertex.uv) != 2
                    or not all(math.isfinite(v) for v in (*vertex.position, *vertex.normal, *vertex.uv))):
                raise ValueError(f"Mesh {mesh.name!r} has invalid vertex coordinates")
            if any(abs(v) > 3.4028234663852886e38 for v in vertex.position) or not any(vertex.normal):
                raise ValueError(f"Mesh {mesh.name!r} has unrepresentable positions or zero-length normals")
            if vertex.color is not None and (len(vertex.color) != 4
                                             or any(not isinstance(v, int) or not 0 <= v <= 255 for v in vertex.color)):
                raise ValueError(f"Mesh {mesh.name!r} requires four byte-valued vertex color channels")
            if any(abs(v) > 3.4028234663852886e38 for v in vertex.uv):
                raise ValueError(f"Mesh {mesh.name!r} has UV coordinates outside the float32 range")
            if len(vertex.influences) > 8:
                raise ValueError(f"Mesh {mesh.name!r} exceeds eight skin influences per vertex")
            if any(name not in joint_indices or not math.isfinite(weight) or weight <= 0
                   for name, weight in vertex.influences):
                raise ValueError(f"Mesh {mesh.name!r} has an unknown joint or invalid skin weight")
        rigid = 0 if any(not vertex.influences for vertex in mesh.vertices) else None
        rule = RecordRule(mesh.name, mesh.material_name, [mesh.name], rigid_joint=rigid)
        prepared.extend(split_mesh_by_palette_limit(mesh, rule, [], {}, joint_indices))
    if len(prepared) > 65535:
        raise ValueError('Authored export exceeds 65535 mesh records')
    palettes = []
    for mesh in prepared:
        palette = sorted({joint_indices[name] for vertex in mesh.vertices for name, _ in vertex.influences}
                         | ({0} if any(not vertex.influences for vertex in mesh.vertices) else set()))
        palettes.append(palette)
    if sum(map(len, palettes)) > 65535:
        raise ValueError('Authored export exceeds the U16 joint-palette offset capacity')
    rules = [RecordRule(mesh.name, mesh.material_name, [mesh.name], rigid_joint=0) for mesh in prepared]
    tangent_meshes = []
    for mesh in prepared:
        if materials[mesh.material_index].normal_texture is not None:
            for start in range(0, len(mesh.indices), 3):
                a, b, c = (mesh.vertices[i].uv for i in mesh.indices[start:start + 3])
                if abs((b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])) <= 1e-12:
                    raise ValueError(f"Normal-mapped mesh {mesh.name!r} requires nondegenerate UV triangles")
        tangents = mesh_tangent_space(mesh) if any(v.tangent is None or v.bitangent is None for v in mesh.vertices) else mesh
        for vertex in tangents.vertices:
            if (not all(math.isfinite(v) for v in (*vertex.tangent, *vertex.bitangent))
                    or (materials[mesh.material_index].normal_texture is not None
                        and abs(vector_dot(vector_cross(vertex.normal, vertex.tangent), vertex.bitangent)) < .5)):
                raise ValueError(f"Mesh {mesh.name!r} has an invalid tangent basis")
        tangent_meshes.append(tangents)
    uv_format = 2 if any(not 0 <= value <= 1 for mesh in prepared
                         for vertex in mesh.vertices for value in vertex.uv) else 14
    geometry, records = build_g4mg(tangent_meshes, (False, True), rules,
                                  palettes, {}, joint_indices,
                                  fallback_colors=[STABLE_CUSTOM_VERTEX_COLOR] * len(prepared),
                                  uv_formats=[uv_format] * len(prepared))
    if any(record['unresolved_influences'] for record in records):
        raise ValueError('Authored geometry has unresolved skin influences')
    descriptor = _model_descriptor(prepared, records, materials, textures, nodes, palettes)
    validate_generated_model(descriptor, geometry)
    validate_model_texture_bindings(descriptor, archive)
    return ModelResources(descriptor, geometry, skeleton, archive)
