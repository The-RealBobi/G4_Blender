"""Native Character normal-map material variants and texture table expansion."""
from __future__ import annotations

import struct
import zlib

try:
    from .g4_model_probe import parse_g4md
except ImportError:
    from g4_model_probe import parse_g4md


# Native Character variants: ordinary/toon+specular and tangent-normal equivalents.
NORMAL_SHADER = 249771619
NORMAL_SPECULAR_SHADER = 1062613594
SUPPORTED_SOURCE_SHADERS = {1531193697, 1640516477, 4224621613, NORMAL_SHADER, NORMAL_SPECULAR_SHADER}


def _replace_sections(data: bytes, sections: dict[tuple[int, int], bytes]) -> bytes:
    ordered = sorted(sections.items())
    output = bytearray()
    cursor = 0
    for (start, end), replacement in ordered:
        if not cursor <= start <= end <= len(data) or (len(replacement) - (end - start)) % 4:
            raise ValueError("Invalid normal-material section boundary or alignment")
        output.extend(data[cursor:start])
        output.extend(replacement)
        cursor = end
    output.extend(data[cursor:])
    base = struct.unpack_from('<H', data, 0x0A)[0] * 4
    for field in range(0x62, 0x8C, 2):
        value = struct.unpack_from('<H', data, field)[0]
        if not value:
            continue
        absolute = base + value * 4
        delta = sum(len(new) - (end - start) for (start, end), new in ordered if end <= absolute)
        shifted = value + delta // 4
        if not 0 <= shifted <= 0xFFFF:
            raise ValueError("Normal-material tables exceed G4MD offset capacity")
        struct.pack_into('<H', output, field, shifted)
    struct.pack_into('<I', output, 0x0C, len(output) - struct.unpack_from('<H', data, 4)[0])
    return bytes(output)


def promote_normal_materials(data: bytes, requested: dict[int, str], texture_names: dict[int, str]) -> bytes:
    if not requested:
        return data
    info = parse_g4md(data)
    base = struct.unpack_from('<H', data, 0x0A)[0] * 4
    def offset(field: int) -> int:
        return base + struct.unpack_from('<H', data, field)[0] * 4
    shader_hashes = bytearray(data[offset(0x78):offset(0x7A)])
    texture_hashes = bytearray(data[offset(0x76):offset(0x78)])
    uv_hashes = bytearray(data[offset(0x7A):offset(0x7C)])
    uv_matrices = bytearray(data[offset(0x88):offset(0x8A)])
    uv_count = struct.unpack_from('<H', data, 0x2A)[0]
    if len(uv_hashes) < uv_count * 4 or len(uv_matrices) < uv_count * 32:
        raise ValueError('Incomplete native material UV tables')
    uv_hashes = uv_hashes[:uv_count * 4]
    uv_matrices = uv_matrices[:uv_count * 32]
    references = bytearray()
    output = bytearray(data)
    specular_layouts = set()
    known_hashes = {entry['hash_u32']: entry['index'] for entry in info['texture_hashes']}
    names_by_hash = {zlib.crc32(name.encode('ascii')): name for name in texture_names.values()}
    for material in info['material_records']:
        index = material['index']
        raw = list(material['raw_u16'])
        refs = [data[ref['offset']:ref['offset'] + 6] for ref in material['texture_refs']]
        name = requested.get(index)
        if name:
            if material.get('shader_hash') not in SUPPORTED_SOURCE_SHADERS:
                raise ValueError(f"Material {index} has no verified normal-map shader variant ({material.get('shader_hash')})")
            prefix = name[:-3]
            roles = {}
            for position, ref in enumerate(material['texture_refs']):
                texture_hash = info['texture_hashes'][ref['texture_index']]['hash_u32']
                texture_name = names_by_hash.get(texture_hash, '')
                if texture_name.startswith(prefix):
                    roles[texture_name[len(prefix):]] = position
            if 'nml' not in roles:
                if not all(role in roles for role in ('', 'line', 'oc')):
                    raise ValueError(f"Material {index} does not have the native Character base/line/oc texture contract")
                normal_hash = zlib.crc32(name.encode('ascii'))
                normal_index = known_hashes.get(normal_hash)
                if normal_index is None:
                    normal_index = len(texture_hashes) // 4
                    if normal_index >= 255:
                        raise ValueError("Normal map exceeds the G4MD texture index capacity")
                    texture_hashes.extend(struct.pack('<I', normal_hash))
                    known_hashes[normal_hash] = normal_index
                normal_ref = bytearray(refs[roles['']])
                normal_ref[0] = normal_index
                specular = 'sp' in roles and 'spm' in roles
                wanted = ['', 'line', 'oc', 'nml'] + (['sp', 'spm'] if specular else [])
                new_refs = []
                raw[7] = len(uv_matrices) // 32
                matrices = material.get('uv_matrices', [])
                hashes = material.get('uv_animation_hashes', [])
                if len(matrices) != len(refs) or len(hashes) != len(refs):
                    raise ValueError("Normal-map material has incomplete UV animation tables")
                for role in wanted:
                    source = roles[''] if role == 'nml' else roles[role]
                    new_refs.append(bytes(normal_ref) if role == 'nml' else refs[source])
                    uv_hashes.extend(struct.pack('<I', hashes[source]))
                    uv_matrices.extend(struct.pack('<8f', *matrices[source]))
                refs = new_refs
                shader = NORMAL_SPECULAR_SHADER if specular else NORMAL_SHADER
                shader_values = list(struct.unpack('<' + 'I' * (len(shader_hashes) // 4), shader_hashes))
                if shader in shader_values:
                    shader_index = shader_values.index(shader)
                else:
                    shader_index = len(shader_values)
                    if shader_index >= 255:
                        raise ValueError("Normal-map shader index exceeds G4MD capacity")
                    shader_hashes.extend(struct.pack('<I', shader))
                raw[4] = (raw[4] & 255) | shader_index << 8
                if (raw[5] & 255) < 3:
                    raise ValueError("Normal-map material requires three native parameter vectors")
                raw[5] = (raw[5] & 0xFF00) | 3
                if specular:
                    specular_layouts.update(record['layout_index'] for record in info['records'] if record['material_index'] == index)
        raw[3] = len(references) // 6
        raw[6] = (raw[6] & 0xFF00) | len(refs)
        references.extend(b''.join(refs))
        struct.pack_into('<8H', output, material['offset'], *raw)
    output[0x27] = len(texture_hashes) // 4
    output[0x28] = len(shader_hashes) // 4
    if len(uv_matrices) // 32 > 65535:
        raise ValueError('Normal-map UV table exceeds G4MD capacity')
    struct.pack_into('<H', output, 0x2A, len(uv_matrices) // 32)
    references.extend(b'\0' * (-len(references) % 4))
    sections = {
        (offset(0x68), offset(0x6A)): bytes(references),
        (offset(0x76), offset(0x78)): bytes(texture_hashes),
        (offset(0x78), offset(0x7A)): bytes(shader_hashes),
        (offset(0x7A), offset(0x7C)): bytes(uv_hashes),
        (offset(0x88), offset(0x8A)): bytes(uv_matrices),
    }
    if specular_layouts:
        layouts = bytearray()
        start = info['vertex_layouts'][0]['offset']
        for layout in info['vertex_layouts']:
            header = bytearray(data[layout['offset']:layout['offset'] + 8])
            elements = list(layout['elements'])
            if layout['index'] in specular_layouts:
                uv = next(element for element in elements if element['type'] == 10)
                for kind in (14, 15):
                    if not any(element['type'] == kind for element in elements):
                        elements.append({**uv, 'type': kind})
            header[1] = len(elements)
            layouts.extend(header)
            for element in elements:
                layouts.extend(struct.pack('<BHBI', element['type'], element['value_offset'], element['padding'], element['format_id']))
        layouts.extend(b'\0' * (-(start + len(layouts)) % 16))
        sections[start, info['material_table']] = bytes(layouts)
    return _replace_sections(bytes(output), sections)


def serialize_expanded_g4tx(header: bytes, entries: list[dict]) -> bytes:
    count = len(entries)
    if count > 255 or header[0x25] or struct.unpack_from('<H', header, 0x20)[0] != struct.unpack_from('<H', header, 0x22)[0]:
        raise ValueError("Normal-map expansion requires a standalone G4TX with at most 255 textures")
    hashes_offset = (0x60 + count * 0x30 + 15) & ~15
    ids_offset = hashes_offset + count * 4
    strings_offset = (ids_offset + count + 3) & ~3
    cursor = (strings_offset + count * 2 + 3) & ~3
    data_offset = (cursor + sum(len(entry['name']) + 1 for entry in entries) + 15) & ~15
    output = bytearray(data_offset)
    output[:0x60] = header
    struct.pack_into('<HH', output, 0x20, count, count)
    struct.pack_into('<I', output, 0x0C, data_offset - 0x60)
    for field, absolute in ((0x38, hashes_offset), (0x3C, hashes_offset), (0x3E, data_offset),
                            (0x40, ids_offset), (0x42, hashes_offset), (0x44, hashes_offset), (0x46, strings_offset)):
        struct.pack_into('<H', output, field, (absolute - 0x60) // 4)
    hashes = [zlib.crc32(entry['name'].encode('ascii')) for entry in entries]
    output[ids_offset:ids_offset + count] = bytes(sorted(range(count), key=hashes.__getitem__))
    for index, entry in enumerate(entries):
        struct.pack_into('<I', output, hashes_offset + index * 4, hashes[index])
        relative = cursor - strings_offset
        if relative > 65535:
            raise ValueError("Normal-map G4TX name table exceeds offset capacity")
        struct.pack_into('<H', output, strings_offset + index * 2, relative)
        name = entry['name'].encode('ascii') + b'\0'
        output[cursor:cursor + len(name)] = name
        cursor += len(name)
        payload = entry['payload']
        record = bytearray(entry['template']['raw'])
        struct.pack_into('<II', record, 4, len(output) - data_offset, len(payload))
        if payload.startswith(b'DDS '):
            height, width = struct.unpack_from('<II', payload, 12)
        elif payload.startswith(b'NXTCH000'):
            width, height = struct.unpack_from('<II', payload, 0x14)
        else:
            raise ValueError("Unsupported normal-map G4TX payload")
        if not 0 < width <= 65535 or not 0 < height <= 65535:
            raise ValueError("Invalid normal-map G4TX dimensions")
        struct.pack_into('<HH', record, 0x18, width, height)
        output[0x60 + index * 0x30:0x60 + (index + 1) * 0x30] = record
        output.extend(payload)
        output.extend(b'\0' * (-len(output) % 16))
    struct.pack_into('<I', output, 0x2C, len(output) - data_offset)
    return bytes(output)
