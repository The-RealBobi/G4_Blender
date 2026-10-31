"""Blender entry point for standalone authored model packages."""

from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable
import math
import os
import re
import tempfile

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator
from mathutils import Matrix

from .g4_model_writer import AuthoredMaterial, AuthoredTexture, bc7_dds_header, build_authored_model, build_authored_texture_archive
from .g4_port import (Mesh, Vertex, RecordRule, STABLE_CUSTOM_VERTEX_COLOR,
                      apply_record_outline_rules, png_to_dds)
try:
    from ..formats.g4_axes import g4_from_blender_matrix
    from ..formats.g4sk_writer import G4SKNode, NATIVE_HELPER_NAMES
except ImportError:
    from formats.g4_axes import g4_from_blender_matrix
    from formats.g4sk_writer import G4SKNode, NATIVE_HELPER_NAMES


@dataclass(frozen=True)
class ImageSource:
    image: bpy.types.Image
    uv_map: str


@dataclass(frozen=True)
class MaterialSource:
    base_image: ImageSource | None
    base_color: tuple[float, ...]
    alpha_blend: bool
    normal_image: ImageSource | None
    tangent_uv_map: str
    line_image: bpy.types.Image | None
    occlusion_image: bpy.types.Image | None


def _image_source(link: bpy.types.NodeLink, material_name: str) -> ImageSource:
    node = link.from_node
    if node.type != 'TEX_IMAGE' or link.from_socket.name != 'Color' or node.image is None:
        raise ValueError(f"Material {material_name!r} requires a directly connected image Color output")
    vector = node.inputs['Vector']
    if node.projection != 'FLAT' or (vector.is_linked and vector.links[0].from_node.type != 'UVMAP'):
        raise ValueError(f"Material {material_name!r} requires unmodified UV image mapping")
    if node.extension != 'REPEAT' or node.interpolation != 'Linear':
        raise ValueError(f"Material {material_name!r} requires Repeat extension and Linear image interpolation")
    if node.image.source not in {'FILE', 'GENERATED'} or node.image.type not in {'IMAGE', 'UV_TEST'}:
        raise ValueError(f"Material {material_name!r} requires a single still image")
    uv_map = vector.links[0].from_node.uv_map if vector.is_linked else ''
    return ImageSource(node.image, uv_map)


def _material_source(material: bpy.types.Material | None) -> MaterialSource:
    if material is None or material.node_tree is None:
        raise ValueError('Assign a node material with a Principled BSDF before exporting')
    tree = material.node_tree
    outputs = [node for node in tree.nodes if node.type == 'OUTPUT_MATERIAL' and node.is_active_output]
    if len(outputs) != 1 or not outputs[0].inputs['Surface'].is_linked:
        raise ValueError(f"Material {material.name!r} requires one connected surface output")
    if outputs[0].inputs['Volume'].is_linked or outputs[0].inputs['Displacement'].is_linked:
        raise ValueError(f"Material {material.name!r} uses unsupported volume or displacement")
    shader = outputs[0].inputs['Surface'].links[0].from_node
    if shader.type != 'BSDF_PRINCIPLED':
        raise ValueError(f"Material {material.name!r} requires a direct Principled BSDF surface")
    for socket in shader.inputs:
        if socket.is_linked and socket.name not in {'Base Color', 'Alpha', 'Normal'}:
            raise ValueError(f"Material {material.name!r}: linked {socket.name} is not supported by independent Chr_Toon export")
    for name in ('Metallic', 'Transmission Weight', 'Coat Weight', 'Sheen Weight', 'Subsurface Weight', 'Anisotropic IOR Level'):
        socket = shader.inputs.get(name)
        if socket is not None and socket.default_value != 0:
            raise ValueError(f"Material {material.name!r}: {name} must be zero for independent Chr_Toon export")
    if (shader.inputs['Emission Strength'].default_value > 0
            and any(shader.inputs['Emission Color'].default_value[:3])):
        raise ValueError(f"Material {material.name!r}: emission is not supported by independent Chr_Toon export")
    color = shader.inputs['Base Color']
    image = _image_source(color.links[0], material.name) if color.is_linked else None
    alpha = shader.inputs['Alpha']
    if alpha.is_linked:
        link = alpha.links[0]
        if image is None or link.from_node != color.links[0].from_node or link.from_socket.name != 'Alpha':
            raise ValueError(f"Material {material.name!r} requires alpha from its base image")
    if not alpha.is_linked and alpha.default_value != 1.0:
        raise ValueError(f"Material {material.name!r}: bake constant alpha into its base image before exporting")
    normal = shader.inputs['Normal']
    normal_image = None
    tangent_uv_map = ''
    if normal.is_linked:
        link = normal.links[0]
        node = link.from_node
        if node.type != 'NORMAL_MAP' or link.from_socket.name != 'Normal' or node.space != 'TANGENT':
            raise ValueError(f"Material {material.name!r} requires a direct tangent-space Normal Map")
        if node.inputs['Strength'].is_linked or node.inputs['Strength'].default_value != 1:
            raise ValueError(f"Material {material.name!r} requires Normal Map strength 1; bake other strength into the image")
        if not node.inputs['Color'].is_linked:
            raise ValueError(f"Material {material.name!r} requires an image connected to Normal Map Color")
        normal_image = _image_source(node.inputs['Color'].links[0], material.name)
        if normal_image.image.colorspace_settings.name != 'Non-Color':
            raise ValueError(f"Material {material.name!r} requires a Non-Color normal image")
        tangent_uv_map = node.uv_map
    return MaterialSource(image, tuple(color.default_value), alpha.is_linked, normal_image, tangent_uv_map,
                          bpy.data.images.get(material.get('g4_export_line_image', '')),
                          bpy.data.images.get(material.get('g4_export_occlusion_image', '')))


def _image_texture(name: str, image: bpy.types.Image | None, color: tuple[float, ...], cache: Path) -> AuthoredTexture:
    target = image if image is not None else bpy.data.images.new(name, width=8, height=8)
    original_format = target.file_format
    try:
        if image is None:
            target.generated_color = color
        width, height = target.size
        if not 0 < width <= 8192 or not 0 < height <= 8192:
            raise ValueError(f"Texture {name!r} requires readable image data up to 8192 pixels per side")
        png = cache / f'{name}.png'
        target.file_format = 'PNG'
        # Image.copy() reloads the backing pixels and loses unsaved texture paint.
        target.save(filepath=str(png), save_copy=True)
        mips = max(1, max(width, height).bit_length() - 2)
        payload = png_to_dds(png, bc7_dds_header(width, height, mips))
        return AuthoredTexture(name, payload)
    finally:
        target.file_format = original_format
        if image is None:
            bpy.data.images.remove(target)


def _scene_geometry(context: bpy.types.Context, selected_only: bool) -> tuple[
        list[Mesh], list[bpy.types.Material], list[MaterialSource], bpy.types.Object | None]:
    objects = sorted((obj for obj in (context.selected_objects if selected_only else context.scene.objects)
                      if obj.type == 'MESH' and obj.visible_get()), key=lambda obj: obj.name)
    if not objects:
        raise ValueError('Select at least one visible mesh to export')
    if context.mode != 'OBJECT':
        raise ValueError('Switch to Object Mode before exporting')
    rigs = {modifier.object for obj in objects for modifier in obj.modifiers
            if modifier.type == 'ARMATURE' and modifier.show_viewport and modifier.object is not None}
    if len(rigs) > 1:
        raise ValueError('Independent export supports one armature per model')
    rig = next(iter(rigs), None)
    if rig and any(bone.name in NATIVE_HELPER_NAMES for bone in rig.data.bones):
        raise ValueError('The armature uses reserved motion-helper names; use an authored rig without native helpers')
    if rig and any(bone.bbone_segments > 1 for bone in rig.data.bones if bone.use_deform):
        raise ValueError('Independent export requires ordinary bones without segmented B-Bone deformation')
    nondeforming = {bone.name for bone in rig.data.bones if not bone.use_deform} if rig else set()
    conversion = g4_from_blender_matrix()
    meshes = []
    materials = []
    sources = []
    depsgraph = context.evaluated_depsgraph_get()
    for obj in objects:
        if obj.parent_type == 'BONE':
            raise ValueError(f"Mesh {obj.name!r} is bone-parented; use an armature modifier and skin weights")
        deformers = [m for m in obj.modifiers if m.type == 'ARMATURE' and m.show_viewport and m.object is not None]
        if len(deformers) > 1 or any(not m.use_vertex_groups or m.use_bone_envelopes
                                     or m.use_deform_preserve_volume or m.vertex_group for m in deformers):
            raise ValueError(f"Mesh {obj.name!r} requires one linear armature modifier using only vertex groups")
        temporary = obj.copy()
        context.scene.collection.objects.link(temporary)
        evaluated = None
        try:
            for modifier in temporary.modifiers:
                if modifier.type == 'ARMATURE':
                    modifier.show_viewport = False
            context.view_layer.update()
            evaluated = temporary.evaluated_get(depsgraph)
            data = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
            data.calc_loop_triangles()
            world = conversion @ obj.matrix_world
            world_determinant = world.determinant()
            if abs(world_determinant) < 1e-10:
                raise ValueError(f"Mesh {obj.name!r} has a singular object transform")
            direction_matrix = world.to_3x3()
            normal_matrix = direction_matrix.inverted().transposed()
            group_names = {group.index: group.name for group in obj.vertex_groups}
            has_rig = any(m.type == 'ARMATURE' and m.show_viewport and m.object is rig
                          for m in obj.modifiers) if rig else False
            for slot in sorted({triangle.material_index for triangle in data.loop_triangles}):
                material = data.materials[slot] if slot < len(data.materials) else None
                if material not in materials:
                    source = _material_source(material)
                    materials.append(material)
                    sources.append(source)
                material_index = materials.index(material)
                source = sources[material_index]
                render_uv = next((layer for layer in data.uv_layers if layer.active_render), data.uv_layers.active)
                images = [entry for entry in (source.base_image, source.normal_image) if entry is not None]
                layers = [data.uv_layers.get(entry.uv_map) if entry.uv_map else render_uv for entry in images]
                if source.normal_image is not None:
                    layers.append(data.uv_layers.get(source.tangent_uv_map) if source.tangent_uv_map else render_uv)
                if any(layer is None for layer in layers):
                    raise ValueError(f"Mesh {obj.name!r} needs the UV maps used by its material images and tangent normal")
                if len({layer.name for layer in layers}) > 1:
                    raise ValueError(f"Mesh {obj.name!r} requires the same UV map for base color, normal image and normal tangents")
                uv_layer = layers[0] if layers else render_uv
                if source.normal_image is not None:
                    uv_name = uv_layer.name
                    data.calc_tangents(uvmap=uv_name)
                    # Tangent allocation can invalidate UV layer handles in Blender 4.5.
                    uv_layer = data.uv_layers[uv_name]
                vertices = []
                indices = []
                lookup = {}
                for triangle in data.loop_triangles:
                    if triangle.material_index != slot:
                        continue
                    corners = list(triangle.loops)
                    if world_determinant < 0:
                        corners.reverse()
                    for loop_index in corners:
                        loop = data.loops[loop_index]
                        point = data.vertices[loop.vertex_index]
                        influences = tuple((group_names[g.group], float(g.weight))
                                           for g in point.groups if g.weight > 0
                                           and group_names[g.group] not in nondeforming) if has_rig else ()
                        if not influences:
                            influences = (('c_global_0_0', 1.),)
                        normal = (normal_matrix @ loop.normal).normalized()
                        tangent = bitangent = None
                        if source.normal_image is not None:
                            tangent_vector = (direction_matrix @ loop.tangent).normalized()
                            orientation = loop.bitangent_sign * (-1 if world_determinant < 0 else 1)
                            tangent = tuple(tangent_vector)
                            bitangent = tuple(normal.cross(tangent_vector).normalized() * orientation)
                        vertex = Vertex(tuple(world @ point.co), tuple(normal),
                                        tuple(uv_layer.data[loop_index].uv) if uv_layer else (0., 0.), influences,
                                        tangent=tangent, bitangent=bitangent)
                        key = (vertex.position, vertex.normal, vertex.uv, vertex.influences, tangent, bitangent)
                        if key not in lookup:
                            lookup[key] = len(vertices)
                            vertices.append(vertex)
                        indices.append(lookup[key])
                mesh = Mesh(str(obj.get('g4_export_mesh_name', f'{obj.name}_{slot}')), vertices, indices, material_index, material.name)
                if not material.get('g4_export_outline', True):
                    for vertex in mesh.vertices:
                        vertex.color = STABLE_CUSTOM_VERTEX_COLOR
                    rule = RecordRule(mesh.name, mesh.material_name, [mesh.name], outline_enabled=False)
                    apply_record_outline_rules([mesh], [rule])
                meshes.append(mesh)
        finally:
            if evaluated is not None:
                evaluated.to_mesh_clear()
            bpy.data.objects.remove(temporary, do_unlink=True)
    return meshes, materials, sources, rig


def _scene_skeleton(meshes: list[Mesh], rig: bpy.types.Object | None) -> list[G4SKNode]:
    conversion = g4_from_blender_matrix()
    globals_ = [Matrix.Identity(4) for _ in NATIVE_HELPER_NAMES]
    names = list(NATIVE_HELPER_NAMES)
    parents = [None, 0, 0, 2]
    if rig is not None:
        bones = list(rig.data.bones)
        pending = list(bones)
        while pending:
            ready = [bone for bone in pending if bone.parent is None or bone.parent.name in names]
            if not ready:
                raise ValueError('The armature hierarchy cannot be ordered parent first')
            for bone in ready:
                parents.append(names.index(bone.parent.name) if bone.parent else 2)
                names.append(bone.name)
                globals_.append(conversion @ rig.matrix_world @ bone.matrix_local @ conversion.inverted())
                pending.remove(bone)
    radii = [0.] * len(names)
    indices = {name: index for index, name in enumerate(names)}
    for mesh in meshes:
        for vertex in mesh.vertices:
            for name, _ in vertex.influences:
                if name not in indices:
                    raise ValueError(f"Mesh {mesh.name!r} references missing bone {name!r}")
                index = indices[name]
                radii[index] = max(radii[index], math.sqrt(sum(
                    (vertex.position[axis] - globals_[index].translation[axis]) ** 2 for axis in range(3))))
    nodes = []
    for index, (name, parent, global_matrix) in enumerate(zip(names, parents, globals_)):
        if abs(global_matrix.determinant()) < 1e-10:
            raise ValueError(f"Joint {name!r} has a singular bind matrix")
        local = globals_[parent].inverted() @ global_matrix if parent is not None else global_matrix
        location, rotation, scale = local.decompose()
        rebuilt = Matrix.LocRotScale(location, rotation, scale)
        if max(abs(local[row][col] - rebuilt[row][col]) for row in range(4) for col in range(4)) > 1e-4:
            raise ValueError(f"Joint {name!r} has shear that G4SK cannot represent")
        def matrix12(matrix):
            return tuple(float(matrix[row][column]) for row in range(3) for column in range(4))
        nodes.append(G4SKNode(name, parent,
                             (*scale, 0., rotation.x, rotation.y, rotation.z, rotation.w, *location, 1.),
                             matrix12(global_matrix), matrix12(global_matrix.inverted()),
                             33 if name in ('output', 'c_global_0_0') else 1, radii[index]))
    return nodes


def export_authored_model(context: bpy.types.Context, directory: Path, model_name: str, selected_only: bool) -> Path:
    return export_model_package(context, directory, model_name, selected_only, 'dx11', None)


def export_model_package(context: bpy.types.Context, directory: Path, model_name: str, selected_only: bool,
                         platform: str, prepare_profile: Callable[[Path], None] | None) -> Path:
    if platform not in {'dx11', 'nx', 'both'}:
        raise ValueError('Choose DX11, NX or Both')
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', model_name) is None:
        raise ValueError('Model name requires 1 to 64 ASCII letters, digits, underscores or hyphens')
    if model_name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                         *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        raise ValueError('Choose a model name that is not a reserved Windows filename')
    destination = directory / model_name
    if destination.exists():
        raise FileExistsError(f"The package {model_name!r} already exists; choose another name or folder")
    meshes, materials, sources, rig = _scene_geometry(context, selected_only)
    nodes = _scene_skeleton(meshes, rig)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.g4-model-', dir=directory) as temporary:
        cache = Path(temporary)
        textures = [_image_texture('neutral', None, (1., 1., 1., 1.), cache)]
        output_materials = []
        for index, source in enumerate(sources):
            name = f'{model_name}_{index:02d}'
            image = source.base_image.image if source.base_image else None
            textures.append(_image_texture(name, image, source.base_color, cache))
            normal_name = None
            if source.normal_image is not None:
                normal_name = f'{name}nml'
                textures.append(_image_texture(normal_name, source.normal_image.image, (), cache))
            slots = []
            for suffix, image in (('line', source.line_image), ('oc', source.occlusion_image)):
                slot_name = f'{name}{suffix}' if image is not None else 'neutral'
                if image is not None:
                    textures.append(_image_texture(slot_name, image, (), cache))
                slots.append(slot_name)
            output_materials.append(AuthoredMaterial(f'{name}M', name, slots[0], slots[1], source.alpha_blend, normal_name))
        for mesh in meshes:
            mesh.material_name = output_materials[mesh.material_index].name
        resources = build_authored_model(meshes, output_materials, textures, nodes)
        package = cache / 'package'
        common = package / 'data/common/chr' / model_name
        common.mkdir(parents=True)
        for suffix, data in (('.g4md', resources.g4md), ('.g4mg', resources.g4mg), ('.g4sk', resources.g4sk)):
            (common / f'{model_name}{suffix}').write_bytes(data)
        for target_platform in (('dx11', 'nx') if platform == 'both' else (platform,)):
            texture_dir = package / f'data/{target_platform}/chr' / model_name
            texture_dir.mkdir(parents=True)
            archive = resources.g4tx if target_platform == 'dx11' else build_authored_texture_archive(textures, 'nx')
            (texture_dir / f'{model_name}.g4tx').write_bytes(archive)
        if prepare_profile is not None:
            prepare_profile(common / f'{model_name}.g4md')
        if destination.exists():
            raise FileExistsError(f"The package {model_name!r} appeared during export; choose another name")
        os.rename(package, destination)
    if rig is not None and rig.is_editable:
        model_path = (destination / 'data/common/chr' / model_name / f'{model_name}.g4md').resolve()
        rig['g4_exported_model_path'] = str(model_path)
        rig['g4_exported_skeleton_path'] = str(model_path.with_suffix('.g4sk'))
    return destination


class EXPORT_OT_level5_g4_model(Operator):
    bl_idname = 'export_scene.level5_g4_model'
    bl_label = 'Export Independent G4 Model'
    bl_description = 'Create a new model package from authored meshes and diffuse materials without a native model'
    bl_options = {'REGISTER'}

    directory: StringProperty(subtype='DIR_PATH')
    model_name: StringProperty(name='Model Name', default='custom_model')
    selected_only: BoolProperty(name='Selected Meshes Only', default=True)

    def invoke(self, context, event):
        return bpy.ops.level5_g4_export.begin('INVOKE_DEFAULT')

    def execute(self, context):
        try:
            if not self.directory:
                raise ValueError('Choose an output folder')
            destination = export_authored_model(context, Path(bpy.path.abspath(self.directory)),
                                                self.model_name, self.selected_only)
        except (OSError, ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Independent G4 model exported to {destination}')
        return {'FINISHED'}
