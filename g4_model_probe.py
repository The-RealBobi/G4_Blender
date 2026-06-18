#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape


ASCII_RE = re.compile(rb"[A-Za-z0-9_]{3,}(?:_LOD[0-9])?M?")
RAW_DATA_ROOT = Path(os.environ.get("LEVEL5_G4_RAW_ROOT", ".")).expanduser()


def default_chara_model_xml() -> Path:
    env_path = os.environ.get("LEVEL5_G4_CHARA_MODEL")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())

    script_dir = Path(__file__).resolve().parent
    search_dirs = [script_dir, script_dir / "TOOLS", script_dir.parent / "TOOLS", Path("TOOLS")]
    for directory in search_dirs:
        candidates.append(directory / "chara_model_1.03.49.00.cfg.bin.xml")
        candidates.extend(sorted(directory.glob("chara_model*.xml")))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate
    return Path("TOOLS/chara_model_1.03.49.00.cfg.bin.xml")


def default_chara_model_lookup() -> Path | None:
    env_path = os.environ.get("LEVEL5_G4_CHARA_LOOKUP")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())

    script_dir = Path(__file__).resolve().parent
    candidates.extend(
        [
            script_dir / "chara_model_lookup.json",
            Path("data/chara_model_lookup.json"),
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate
    return None


CHARA_MODEL_XML = default_chara_model_xml()
CHARA_MODEL_LOOKUP = default_chara_model_lookup()
CHARA_MODEL_LOOKUP_DATA: dict | None = None
SKELETON_CACHE = (
    Path(os.environ["LEVEL5_G4_SKELETON_CACHE"]).expanduser()
    if os.environ.get("LEVEL5_G4_SKELETON_CACHE")
    else None
)
MODEL_EXTENSIONS = {".g4md", ".g4pkm", ".objbin"}

# Shared character models store palette indices in this compact, stable joint
# order. Assigned G4SK files may contain the same joints at different indices,
# so palettes must be translated by name before writing the skin controller.
ASSIGNED_SKELETON_JOINT_NAMES = (
    "output", "c_global_0_0", "c_c_1_0", "c_c_1_1",
    "l_s_1_0", "l_a_1_0", "l_a_1_1", "l_w_1_0", "l_wph_1_0",
    "l_idx_1_0", "l_idx_1_1", "l_idx_1_2",
    "l_mid_1_0", "l_mid_1_1", "l_mid_1_2",
    "l_rng_1_0", "l_rng_1_1", "l_rng_1_2",
    "l_thb_1_0", "l_thb_1_1", "l_thb_1_2", "l_soh_1_0",
    "l_pky_1_0", "l_pky_1_1", "l_pky_1_2", "l_fa_1_0",
    "l_slv2_1_0", "l_slv1_1_0", "c_n_1_0", "c_head_1_0",
    "c_hir1_1_0", "c_hir2_1_0", "c_hir2_1_1", "c_hir2_1_2",
    "c_hir3_1_0", "c_hir3_1_1", "c_hir4_1_0",
    "l_hir1_1_0", "l_hir1_1_1", "l_hir2_1_0", "l_hir2_1_1",
    "l_hir2_1_2", "l_hir3_1_0", "l_hir3_1_1", "l_hir4_1_0",
    "r_hir1_1_0", "r_hir1_1_1", "r_hir2_1_0", "r_hir2_1_1",
    "r_hir2_1_2", "r_hir3_1_0", "r_hir3_1_1", "r_hir4_1_0",
    "r_s_1_0", "r_a_1_0", "r_a_1_1", "r_w_1_0", "r_wph_1_0",
    "r_idx_1_0", "r_idx_1_1", "r_idx_1_2",
    "r_mid_1_0", "r_mid_1_1", "r_mid_1_2",
    "r_rng_1_0", "r_rng_1_1", "r_rng_1_2",
    "r_thb_1_0", "r_thb_1_1", "r_thb_1_2", "r_soh_1_0",
    "r_pky_1_0", "r_pky_1_1", "r_pky_1_2", "r_fa_1_0",
    "r_slv2_1_0", "r_slv1_1_0",
    "r_mnt_1_0", "r_mnt_1_1", "l_mnt_1_0", "l_mnt_1_1",
    "c_mnt_1_0", "c_mnt_1_1",
    "l_l_1_0", "l_l_1_1", "l_foot_1_0", "l_foot_1_1", "l_pnt_1_0",
    "r_l_1_0", "r_l_1_1", "r_foot_1_0", "r_foot_1_1", "r_pnt_1_0",
    "c_sht1_1_0", "c_ball_1_0",
)


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u8(data: bytes, offset: int) -> int:
    return data[offset]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def f32(data: bytes, offset: int) -> float:
    return struct.unpack_from("<f", data, offset)[0]


def align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def cstr(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("ascii", errors="replace")


def infer_raw_data_root(path: Path) -> Path | None:
    parts = path.resolve().parts
    for index, part in enumerate(parts):
        if part == "data" and index + 1 < len(parts) and parts[index + 1] in {"common", "dx11", "nx"}:
            return Path(*parts[: index + 1])
    return None


def configure_raw_data_root_from_path(path: Path) -> None:
    global RAW_DATA_ROOT
    if os.environ.get("LEVEL5_G4_RAW_ROOT"):
        return
    inferred = infer_raw_data_root(path)
    if inferred is not None:
        RAW_DATA_ROOT = inferred


@dataclass
class PackEntry:
    index: int
    name: str
    offset: int
    size: int
    magic: str
    crc32b: str


@dataclass
class MeshRecord:
    index: int
    vertex_offset: int
    index_offset: int
    vertex_count: int
    index_count: int
    triangle_count: int
    material_or_lod: int
    name_index: int
    palette_or_list: int
    flags0: int
    flags1: int
    layout_index: int
    material_index: int
    vertex_stride: int
    vertex_range_ok: bool
    index_range_ok: bool
    first_position: tuple[float, float, float] | None
    first_indices: tuple[int, ...]


@dataclass
class TextureEntry:
    index: int
    name: str
    crc32b: str
    offset: int
    size: int
    width: int
    height: int
    magic: str


@dataclass
class SkeletonInfo:
    magic: str
    size: int
    joint_count: int
    table_count: int
    section_offsets: list[int]
    parent_indices: list[int]
    names: list[str]
    bind_matrices: list[list[float]]
    inverse_bind_matrices: list[list[float]]
    local_matrices: list[list[float]]
    local_scales: list[list[float]]
    local_rotations_xyzw: list[list[float]]
    local_translations: list[list[float]]
    local_srt_matrices: list[list[float]]


def crc32b(data: bytes) -> int:
    # Python's zlib.crc32 matches the CRC32B used by Kuriimu for these names.
    import zlib

    return zlib.crc32(data) & 0xFFFFFFFF


def signed_crc32(text: str) -> int:
    value = crc32b(text.encode("ascii"))
    return value if value < 0x80000000 else value - 0x100000000


def matrix_from_3x4(data: bytes, offset: int) -> list[float]:
    values = struct.unpack_from("<12f", data, offset)
    return [
        values[0],
        values[1],
        values[2],
        values[3],
        values[4],
        values[5],
        values[6],
        values[7],
        values[8],
        values[9],
        values[10],
        values[11],
        0.0,
        0.0,
        0.0,
        1.0,
    ]




def matrix_from_srt(scale: list[float], rotation_xyzw: list[float], translation: list[float]) -> list[float]:
    sx, sy, sz = scale
    x, y, z, w = rotation_xyzw
    tx, ty, tz = translation
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    r00 = 1.0 - 2.0 * (yy + zz)
    r01 = 2.0 * (xy - wz)
    r02 = 2.0 * (xz + wy)
    r10 = 2.0 * (xy + wz)
    r11 = 1.0 - 2.0 * (xx + zz)
    r12 = 2.0 * (yz - wx)
    r20 = 2.0 * (xz - wy)
    r21 = 2.0 * (yz + wx)
    r22 = 1.0 - 2.0 * (xx + yy)
    return [
        r00 * sx,
        r01 * sy,
        r02 * sz,
        tx,
        r10 * sx,
        r11 * sy,
        r12 * sz,
        ty,
        r20 * sx,
        r21 * sy,
        r22 * sz,
        tz,
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def read_local_srt(data: bytes, section_offset: int, joint_count: int) -> tuple[list[list[float]], list[list[float]], list[list[float]], list[list[float]]]:
    scales: list[list[float]] = []
    rotations: list[list[float]] = []
    translations: list[list[float]] = []
    matrices: list[list[float]] = []
    for joint_index in range(joint_count):
        off = section_offset + joint_index * 0x30
        if off + 0x30 > len(data):
            break
        values = struct.unpack_from("<12f", data, off)
        scale = [values[0], values[1], values[2]]
        rotation = [values[4], values[5], values[6], values[7]]
        translation = [values[8], values[9], values[10]]
        scales.append(scale)
        rotations.append(rotation)
        translations.append(translation)
        matrices.append(matrix_from_srt(scale, rotation, translation))
    return scales, rotations, translations, matrices

def matrix_mul(a: list[float], b: list[float]) -> list[float]:
    out = [0.0] * 16
    for row in range(4):
        for col in range(4):
            out[row * 4 + col] = sum(a[row * 4 + k] * b[k * 4 + col] for k in range(4))
    return out


def rigid_matrix_inverse(matrix: list[float]) -> list[float]:
    r00, r01, r02, tx = matrix[0], matrix[1], matrix[2], matrix[3]
    r10, r11, r12, ty = matrix[4], matrix[5], matrix[6], matrix[7]
    r20, r21, r22, tz = matrix[8], matrix[9], matrix[10], matrix[11]
    return [
        r00,
        r10,
        r20,
        -(r00 * tx + r10 * ty + r20 * tz),
        r01,
        r11,
        r21,
        -(r01 * tx + r11 * ty + r21 * tz),
        r02,
        r12,
        r22,
        -(r02 * tx + r12 * ty + r22 * tz),
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def parse_g4pk(data: bytes) -> dict:
    if data[:4] != b"G4PK":
        raise ValueError("not a G4PK/G4PKM file")

    header_size = u16(data, 0x04)
    file_type = u16(data, 0x06)
    version = u32(data, 0x08)
    content_size = u32(data, 0x0C)
    file_count = u32(data, 0x20)
    hash_count = u16(data, 0x24)
    table3_count = u16(data, 0x26)
    unk2 = u16(data, 0x28)
    unk3 = u16(data, 0x2A)

    pos = header_size
    file_offsets = list(struct.unpack_from("<" + "I" * file_count, data, pos))
    pos += 4 * file_count
    file_sizes = list(struct.unpack_from("<" + "I" * file_count, data, pos))
    pos += 4 * file_count
    hashes = list(struct.unpack_from("<" + "I" * hash_count, data, pos))
    pos += 4 * hash_count

    id_count = table3_count // 2
    unk_ids = list(struct.unpack_from("<" + "h" * id_count, data, pos))
    pos += 2 * id_count
    pos = (pos + 3) & ~3
    string_base = pos
    string_offsets = list(struct.unpack_from("<" + "h" * id_count, data, pos))

    entries: list[PackEntry] = []
    for index in range(file_count):
        name = cstr(data, string_base + string_offsets[index])
        file_offset = header_size + (file_offsets[index] << 2)
        size = file_sizes[index]
        magic = data[file_offset : file_offset + 4].decode("ascii", errors="replace")
        entries.append(
            PackEntry(
                index=index,
                name=name,
                offset=file_offset,
                size=size,
                magic=magic,
                crc32b=f"{hashes[index]:08x}",
            )
        )

    return {
        "magic": "G4PK",
        "header_size": header_size,
        "file_type": file_type,
        "version": version,
        "content_size": content_size,
        "file_count": file_count,
        "hash_count": hash_count,
        "table3_count": table3_count,
        "unk2": unk2,
        "unk3": unk3,
        "unk_ids": unk_ids,
        "entries": [asdict(entry) for entry in entries],
    }


def find_embedded(data: bytes, magic: bytes) -> bytes | None:
    offset = data.find(magic)
    if offset < 0:
        return None
    return data[offset:]


def ascii_runs(data: bytes) -> list[dict]:
    return [
        {"offset": match.start(), "value": match.group().decode("ascii", errors="replace")}
        for match in ASCII_RE.finditer(data)
    ]


def tail_names(data: bytes) -> list[str]:
    runs = ascii_runs(data)
    if not runs:
        return []
    # The real name table is the final compact string cluster. Earlier
    # accidental ASCII runs come from binary payloads and are separated by
    # much larger gaps.
    cluster = [runs[-1]]
    for run in reversed(runs[:-1]):
        gap = cluster[0]["offset"] - run["offset"]
        if gap > 0x80:
            break
        cluster.insert(0, run)

    names = [run["value"] for run in cluster if not any(ch in run["value"] for ch in " \t\r\n")]
    # If a short accidental token survived at the start, trim until the first
    # model-like name. This preserves later names such as "hair2".
    for index, name in enumerate(names):
        if len(name) >= 5 and ("_" in name or name.endswith("M")):
            return names[index:]
    return names


def offset_table_names(data: bytes, table_offset: int, count: int) -> list[str]:
    names: list[str] = []
    if table_offset <= 0 or table_offset + count * 2 > len(data):
        return names
    for index in range(count):
        name_offset = u16(data, table_offset + index * 2)
        absolute = table_offset + name_offset
        if absolute >= len(data):
            return []
        name = cstr(data, absolute)
        if not name or any(ch in name for ch in " \t\r\n"):
            return []
        names.append(name)
    return names


def parse_material_name_index_map(
    data: bytes, name_base_bias: int, material_count: int, record_name_indices: set[int]
) -> dict[int, int]:
    if material_count <= 0:
        return {}
    material_table = name_base_bias + u16(data, 0x64) * 4
    name_index_table = name_base_bias + u16(data, 0x80) * 4
    name_index_table_end = name_base_bias + u16(data, 0x82) * 4
    if (
        material_table <= 0
        or name_index_table <= 0
        or name_index_table_end <= name_index_table
        or material_table + material_count * 0x10 > len(data)
        or name_index_table_end > len(data)
    ):
        return {}

    name_indices = [
        u16(data, name_index_table + index * 2)
        for index in range((name_index_table_end - name_index_table) // 2)
    ]
    starts = [u16(data, material_table + index * 0x10 + 0x0E) for index in range(material_count)]
    if starts != sorted(starts) or starts[-1] > len(name_indices):
        return {}
    starts.append(len(name_indices))

    mapping: dict[int, int] = {}
    for material_index in range(material_count):
        start = starts[material_index]
        end = starts[material_index + 1]
        if end < start or end > len(name_indices):
            return {}
        for name_index in name_indices[start:end]:
            if name_index in record_name_indices:
                mapping[name_index] = material_index
    return mapping


def infer_material_ref_table(data: bytes, material_table: int, material_count: int, fallback_base: int) -> int:
    starts_counts: list[tuple[int, int]] = []
    for index in range(material_count):
        off = material_table + index * 0x10
        if off + 0x10 > len(data):
            return fallback_base
        starts_counts.append((u16(data, off + 0x0E), u16(data, off + 0x0C)))

    best_base = fallback_base
    best_score: tuple[int, int, int] | None = None
    candidate = align(fallback_base, 0x10)
    search_end = min(len(data) - 6, fallback_base + 0x100)
    while candidate <= search_end:
        score = 0
        for ref_start, ref_count in starts_counts:
            for ref_index in range(ref_count):
                ref_off = candidate + (ref_start + ref_index) * 6
                if ref_off + 6 > len(data):
                    score -= 64
                    continue
                tex_index = data[ref_off]
                slot_type = data[ref_off + 1]
                if slot_type in (3, 4, 5, 6, 7):
                    score += 6
                elif slot_type == 0:
                    score -= 2
                else:
                    score -= 5
                if all(value <= 0x10 for value in data[ref_off + 2 : ref_off + 6]):
                    score += 2
                else:
                    score -= 4
                if tex_index <= 0x40:
                    score += 1
        candidate_score = (score, -abs(candidate - fallback_base), -candidate)
        if best_score is None or candidate_score > best_score:
            best_score = candidate_score
            best_base = candidate
        candidate += 0x10
    return best_base


def parse_material_records(data: bytes, material_table: int, material_count: int) -> list[dict]:
    records: list[dict] = []
    if material_count <= 0 or material_table <= 0 or material_table + material_count * 0x10 > len(data):
        return records
    fallback_ref_table = material_table + material_count * 0x10 + 0x30
    ref_table = infer_material_ref_table(data, material_table, material_count, fallback_ref_table)
    for index in range(material_count):
        off = material_table + index * 0x10
        values = struct.unpack_from("<8H", data, off)
        texture_ref_count = values[6]
        texture_ref_start = values[7]
        refs = []
        for ref_index in range(texture_ref_count):
            ref_off = ref_table + (texture_ref_start + ref_index) * 6
            if ref_off + 6 > len(data):
                break
            refs.append(
                {
                    "index": ref_index,
                    "offset": ref_off,
                    "texture_index": data[ref_off],
                    "slot_type": data[ref_off + 1],
                    "tail": list(data[ref_off + 2 : ref_off + 6]),
                }
            )
        records.append(
            {
                "index": index,
                "offset": off,
                "raw_u16": list(values),
                "texture_ref_count": texture_ref_count,
                "texture_ref_start": texture_ref_start,
                "texture_refs": refs,
                "material_ref_table": ref_table,
            }
        )
    return records


def parse_texture_hash_table(data: bytes) -> list[dict]:
    if len(data) <= 0x77:
        return []
    name_base_bias = u16(data, 0x0A) * 4
    table = name_base_bias + u16(data, 0x76) * 4
    count = u8(data, 0x27)
    if count <= 0 or table <= 0 or table + count * 4 > len(data):
        return []
    return [
        {
            "index": index,
            "hash": f"{u32(data, table + index * 4):08x}",
            "hash_u32": u32(data, table + index * 4),
        }
        for index in range(count)
    ]


def parse_vertex_layouts(data: bytes, submesh_table_end: int) -> list[dict]:
    layout_count = u8(data, 0x26) if len(data) > 0x26 else 0
    cursor = submesh_table_end
    layouts: list[dict] = []
    for layout_index in range(layout_count):
        if cursor + 8 > len(data):
            break
        entry_count = u8(data, cursor + 1)
        layout_size = 8 + entry_count * 8
        if cursor + layout_size > len(data):
            break
        elements = []
        for element_index in range(entry_count):
            element_offset = cursor + 8 + element_index * 8
            element_type, value_offset, padding, format_id = struct.unpack_from(
                "<BHBI", data, element_offset
            )
            elements.append(
                {
                    "index": element_index,
                    "type": element_type,
                    "value_offset": value_offset,
                    "padding": padding,
                    "format_id": format_id,
                }
            )
        layouts.append(
            {
                "index": layout_index,
                "offset": cursor,
                "entry_count": entry_count,
                "elements": elements,
            }
        )
        cursor += layout_size
    return layouts


def parse_g4md(data: bytes, g4mg: bytes | None = None) -> dict:
    if data[:4] != b"G4MD":
        raise ValueError("not a G4MD file")

    submesh_table = u16(data, 0x04)
    mesh_count = u16(data, 0x20)
    material_count = u16(data, 0x22)
    vertex_buffer_size = u32(data, 0x50)
    index_buffer_size = u32(data, 0x54)
    vertex_buffer_size_b = u32(data, 0x5C)

    mg_len = len(g4mg) if g4mg is not None else None
    index_base = vertex_buffer_size_b

    records: list[MeshRecord] = []
    for index in range(mesh_count):
        off = submesh_table + index * 0x50
        if off + 0x50 > len(data):
            break

        vertex_offset = u32(data, off + 0x00)
        index_offset = u32(data, off + 0x04)
        vertex_count = u32(data, off + 0x08)
        index_count = u32(data, off + 0x0C)
        triangle_count = u32(data, off + 0x30)
        material_or_lod = u16(data, off + 0x34)
        name_index = u16(data, off + 0x38)
        palette_or_list = u16(data, off + 0x3A)
        flags0 = u16(data, off + 0x3C)
        flags1 = u16(data, off + 0x3E)
        layout_index = u8(data, off + 0x42)
        material_index = u8(data, off + 0x43)

        vertex_stride = flags1 & 0xFF
        if vertex_stride == 0:
            vertex_stride = 0x44
        vertex_end = vertex_offset + vertex_count * vertex_stride
        index_end = index_offset + index_count * 2
        vertex_ok = vertex_end <= vertex_buffer_size
        index_ok = index_end <= index_buffer_size

        first_position = None
        first_indices: tuple[int, ...] = ()
        if g4mg is not None:
            if vertex_offset + 12 <= len(g4mg):
                first_position = (
                    f32(g4mg, vertex_offset),
                    f32(g4mg, vertex_offset + 4),
                    f32(g4mg, vertex_offset + 8),
                )
            absolute_index_offset = index_base + index_offset
            count = min(index_count, 12)
            if absolute_index_offset + count * 2 <= len(g4mg):
                first_indices = struct.unpack_from("<" + "H" * count, g4mg, absolute_index_offset)

        records.append(
            MeshRecord(
                index=index,
                vertex_offset=vertex_offset,
                index_offset=index_offset,
                vertex_count=vertex_count,
                index_count=index_count,
                triangle_count=triangle_count,
                material_or_lod=material_or_lod,
                name_index=name_index,
                palette_or_list=palette_or_list,
                flags0=flags0,
                flags1=flags1,
                layout_index=layout_index,
                material_index=material_index,
                vertex_stride=vertex_stride,
                vertex_range_ok=vertex_ok,
                index_range_ok=index_ok,
                first_position=first_position,
                first_indices=first_indices,
            )
        )

    vertex_layouts = parse_vertex_layouts(data, submesh_table + mesh_count * 0x50)
    material_table = align(vertex_layouts_end(vertex_layouts, submesh_table + mesh_count * 0x50), 0x10)
    material_records = parse_material_records(data, material_table, material_count)
    texture_hashes = parse_texture_hash_table(data)
    name_base_bias = u16(data, 0x0A) * 4
    mesh_name_table = name_base_bias + u16(data, 0x84) * 4
    material_name_table = name_base_bias + u16(data, 0x86) * 4
    joint_palette_table = name_base_bias + u16(data, 0x82) * 4
    joint_palette_indices: list[int] = []
    joint_palette_count = max(
        (
            record.palette_or_list + (record.flags0 & 0xFF)
            for record in records
            if record.flags0 & 0x100
        ),
        default=0,
    )
    if (
        joint_palette_count > 0
        and 0 < joint_palette_table < mesh_name_table <= len(data)
        and joint_palette_table + joint_palette_count * 2 <= mesh_name_table
    ):
        palette_size = joint_palette_count * 2
        if palette_size >= 2:
            joint_palette_indices = list(
                struct.unpack_from("<" + "H" * (palette_size // 2), data, joint_palette_table)
            )
    mesh_names = offset_table_names(data, mesh_name_table, mesh_count)
    material_names = offset_table_names(data, material_name_table, material_count)
    names = tail_names(data)
    if not material_names:
        material_names = [name for name in names if name.endswith("M")]
    if not mesh_names:
        mesh_names = [name for name in names if not name.endswith("M") and name != "G4MD"]
    material_name_index_map = parse_material_name_index_map(
        data, name_base_bias, material_count, {record.name_index for record in records}
    )

    return {
        "magic": "G4MD",
        "size": len(data),
        "mesh_count": mesh_count,
        "material_count": material_count,
        "submesh_table": submesh_table,
        "vertex_buffer_size": vertex_buffer_size,
        "index_buffer_size": index_buffer_size,
        "vertex_buffer_size_b": vertex_buffer_size_b,
        "g4mg_size": mg_len,
        "index_buffer_base": index_base,
        "g4mg_tail_size": None if mg_len is None else mg_len - index_base - index_buffer_size,
        "ascii_runs": ascii_runs(data),
        "tail_names": names,
        "mesh_name_table": mesh_name_table,
        "material_name_table": material_name_table,
        "joint_palette_table": joint_palette_table,
        "joint_palette_count": len(joint_palette_indices),
        "joint_palette_indices": joint_palette_indices,
        "material_names": material_names,
        "mesh_names": mesh_names,
        "vertex_layouts": vertex_layouts,
        "material_table": material_table,
        "material_records": material_records,
        "texture_hashes": texture_hashes,
        "material_name_index_map": material_name_index_map,
        "records": [asdict(record) for record in records],
    }


def mesh_palette_length(record: dict) -> int:
    flags0 = record.get("flags0", 0)
    if flags0 & 0x100:
        return flags0 & 0xFF
    return 0


def joint_palette_for_record(md_info: dict, record: dict) -> list[int]:
    palette_offset = record["palette_or_list"]
    palette_length = mesh_palette_length(record)
    palette_indices = md_info.get("joint_palette_indices") or []
    if palette_length <= 0 or palette_offset >= len(palette_indices):
        return []
    return palette_indices[palette_offset : palette_offset + palette_length]


def vertex_stride_for_record(record: dict) -> int:
    return record.get("vertex_stride") or (record.get("flags1", 0) & 0xFF) or 0x44


def uv0_offset_for_stride(stride: int) -> int | None:
    if stride >= 0x44:
        return 0x40
    if stride >= 0x24:
        return 0x20
    return None


def layout_for_record(md_info: dict, record: dict) -> dict | None:
    layout_index = record.get("layout_index")
    for layout in md_info.get("vertex_layouts", []):
        if layout.get("index") == layout_index:
            return layout
    return None


def layout_element(layout: dict | None, element_type: int) -> dict | None:
    if layout is None:
        return None
    for element in layout.get("elements", []):
        if element.get("type") == element_type:
            return element
    return None


def layout_slice_sizes(layout: dict | None, stride: int) -> dict[int, int]:
    if layout is None:
        return {}
    elements = sorted(layout.get("elements", []), key=lambda item: item.get("value_offset", 0))
    sizes: dict[int, int] = {}
    for index, element in enumerate(elements):
        current = element.get("value_offset", 0)
        next_offset = elements[index + 1].get("value_offset", stride) if index + 1 < len(elements) else stride
        sizes[element.get("index", index)] = max(0, next_offset - current)
    return sizes


def vertex_layouts_end(layouts: list[dict], fallback: int) -> int:
    end = fallback
    for layout in layouts:
        entry_count = layout.get("entry_count", 0)
        end = max(end, layout.get("offset", fallback) + 8 + entry_count * 8)
    return end


def uv0_offset_for_record(md_info: dict, record: dict) -> int | None:
    element = layout_element(layout_for_record(md_info, record), 10)
    if element is not None:
        return element.get("value_offset")
    return uv0_offset_for_stride(vertex_stride_for_record(record))


def decode_uv_pair_for_format(raw: bytes, format_id: int, invert_v: bool = False) -> tuple[float, float]:
    def finish(u: float, v: float) -> tuple[float, float]:
        return u, 1.0 - v if invert_v else v

    if format_id in (2, 3) and len(raw) >= 8:
        return finish(*struct.unpack_from("<2f", raw, 0))
    if format_id == 12 and len(raw) >= 2:
        u, v = struct.unpack_from("<2B", raw, 0)
        return finish(u / 255.0, v / 255.0)
    if format_id == 14 and len(raw) >= 4:
        u, v = struct.unpack_from("<2H", raw, 0)
        return finish(u / 65535.0, v / 65535.0)
    if format_id in (18, 20) and len(raw) >= 4:
        u, v = struct.unpack_from("<2h", raw, 0)
        return finish(u / 32767.0, v / 32767.0)
    if len(raw) >= 4:
        try:
            u, v = struct.unpack_from("<2e", raw, 0)
            return finish(float(u), float(v))
        except struct.error:
            pass
        if format_id == 18:
            u, v = struct.unpack_from("<2h", raw, 0)
            return finish(u / 32767.0, v / 32767.0)
        u, v = struct.unpack_from("<2H", raw, 0)
        return finish(u / 65535.0, v / 65535.0)
    return 0.0, 0.0


def uv_raw_size_for_format(format_id: int, slice_size: int) -> int:
    if format_id in (2, 3):
        return 8
    if format_id == 12:
        return 2
    return max(4, min(8, slice_size or 4))


def read_uv0(g4mg: bytes, md_info: dict, record: dict, vertex_index: int) -> tuple[float, float]:
    stride = vertex_stride_for_record(record)
    layout = layout_for_record(md_info, record)
    element = layout_element(layout, 10)
    if element is None:
        uv_offset = uv0_offset_for_stride(stride)
        if uv_offset is None:
            return 0.0, 0.0
        off = record["vertex_offset"] + vertex_index * stride + uv_offset
        u, v = struct.unpack_from("<HH", g4mg, off)
        return u / 65535.0, 1.0 - (v / 65535.0)

    sizes = layout_slice_sizes(layout, stride)
    raw_size = uv_raw_size_for_format(element.get("format_id", 0), sizes.get(element.get("index", 0), 4))
    off = record["vertex_offset"] + vertex_index * stride + element["value_offset"]
    return decode_uv_pair_for_format(g4mg[off : off + raw_size], element.get("format_id", 0), invert_v=True)


def companion(path: Path, suffix: str) -> Path | None:
    candidate = path.with_suffix(suffix)
    return candidate if candidate.exists() else None


def parse_g4tx(data: bytes) -> dict:
    if data[:4] != b"G4TX":
        raise ValueError("not a G4TX file")

    header_size = u16(data, 0x04)
    table_size = u32(data, 0x0C)
    texture_count = u16(data, 0x20)
    total_count = u16(data, 0x22)
    sub_texture_count = data[0x25]

    pos = header_size
    entries = []
    for index in range(texture_count):
        unk1, tex_offset, tex_size, unk2, unk3, unk4, width, height, const2 = struct.unpack_from(
            "<IIIIIIHHI", data, pos
        )
        entries.append(
            {
                "index": index,
                "unk1": unk1,
                "offset": tex_offset,
                "size": tex_size,
                "unk2": unk2,
                "unk3": unk3,
                "unk4": unk4,
                "width": width,
                "height": height,
                "const2": const2,
            }
        )
        pos += 0x30

    pos += sub_texture_count * 0x18
    pos = (pos + 0xF) & ~0xF
    pos += total_count * 4
    ids = list(data[pos : pos + total_count])
    pos = (pos + total_count + 3) & ~3
    string_offset_pos = pos
    string_offsets = list(struct.unpack_from("<" + "H" * total_count, data, pos))
    string_base = string_offset_pos
    texture_base = (header_size + table_size + 0xF) & ~0xF

    names = []
    for index in range(total_count):
        offset = string_offsets[index]
        names.append(cstr(data, string_base + offset))

    textures: list[TextureEntry] = []
    for entry in entries:
        absolute = texture_base + entry["offset"]
        magic = data[absolute : absolute + 8].decode("ascii", errors="replace")
        textures.append(
            TextureEntry(
                index=entry["index"],
                name=names[entry["index"]],
                crc32b=f"{crc32b(names[entry['index']].encode('ascii')):08x}",
                offset=absolute,
                size=entry["size"],
                width=entry["width"],
                height=entry["height"],
                magic=magic,
            )
        )

    return {
        "magic": "G4TX",
        "header_size": header_size,
        "table_size": table_size,
        "texture_count": texture_count,
        "total_count": total_count,
        "sub_texture_count": sub_texture_count,
        "ids": ids,
        "names": names,
        "textures": [asdict(texture) for texture in textures],
    }


def parse_g4sk(data: bytes) -> dict:
    if data[:4] != b"G4SK":
        raise ValueError("not a G4SK file")
    joint_count = u16(data, 0x20)
    section_offsets = [0x40 + u16(data, 0x24 + index * 2) * 4 for index in range(8)]

    bind_matrices = []
    for joint_index in range(joint_count):
        off = 0x40 + joint_index * 0x30
        if off + 0x30 > len(data):
            break
        bind_matrices.append(matrix_from_3x4(data, off))

    inverse_bind_matrices = []
    inverse_bind_table = section_offsets[0] if section_offsets else 0
    for joint_index in range(joint_count):
        off = inverse_bind_table + joint_index * 0x30
        if off + 0x30 > len(data):
            break
        inverse_bind_matrices.append(matrix_from_3x4(data, off))

    local_srt_table = section_offsets[1] if len(section_offsets) > 1 else 0
    local_scales, local_rotations_xyzw, local_translations, local_srt_matrices = read_local_srt(
        data, local_srt_table, joint_count
    )

    parent_indices: list[int] = []
    parent_table = section_offsets[3] if len(section_offsets) > 3 else 0
    for joint_index in range(joint_count):
        off = parent_table + joint_index * 2
        if off + 2 > len(data):
            break
        parent_indices.append(u16(data, off))

    local_matrices: list[list[float]] = []
    for joint_index, matrix in enumerate(bind_matrices):
        parent = parent_indices[joint_index] if joint_index < len(parent_indices) else joint_count
        if parent < len(bind_matrices) and parent != joint_index:
            local_matrices.append(matrix_mul(rigid_matrix_inverse(bind_matrices[parent]), matrix))
        else:
            local_matrices.append(matrix)

    names: list[str] = []
    name_table = section_offsets[7] if len(section_offsets) > 7 else 0
    if name_table > 0 and name_table + joint_count * 2 <= len(data):
        for joint_index in range(joint_count):
            name_offset = u16(data, name_table + joint_index * 2)
            absolute = name_table + name_offset
            if absolute < len(data):
                names.append(cstr(data, absolute))

    if len(names) != joint_count:
        cutoff = max(0, len(data) - 0x1800)
        names = [
            run["value"]
            for run in ascii_runs(data)
            if run["offset"] >= cutoff and not any(ch in run["value"] for ch in " \t\r\n")
        ]
        if "output" in names:
            names = names[names.index("output") :]

    info = SkeletonInfo(
        magic="G4SK",
        size=len(data),
        joint_count=joint_count,
        table_count=u16(data, 0x22),
        section_offsets=section_offsets,
        parent_indices=parent_indices,
        names=names,
        bind_matrices=bind_matrices,
        inverse_bind_matrices=inverse_bind_matrices,
        local_matrices=local_matrices,
        local_scales=local_scales,
        local_rotations_xyzw=local_rotations_xyzw,
        local_translations=local_translations,
        local_srt_matrices=local_srt_matrices,
    )
    return asdict(info)


def model_relpath_candidates(path: Path) -> set[str]:
    candidates = set()
    try:
        rel = path.relative_to(RAW_DATA_ROOT)
    except ValueError:
        rel = None
    if rel is not None:
        parts = list(rel.parts)
        if len(parts) >= 2 and parts[0] in {"common", "dx11", "nx"} and parts[1] == "chr":
            rel = Path(*parts[2:])
        elif parts and parts[0] in {"common", "dx11", "nx"}:
            rel = Path(*parts[1:])
        rel_g4md = rel.with_suffix(".g4md").as_posix()
        rel_objbin = rel.with_suffix(".objbin").as_posix()
        candidates.update({rel_g4md, rel_objbin})
    candidates.add(path.with_suffix(".g4md").name)
    candidates.add(path.with_suffix(".objbin").name)
    return candidates


def load_chara_model_maps() -> tuple[dict[str, dict], dict[int, dict]]:
    if not CHARA_MODEL_XML.exists():
        return {}, {}
    root = ET.parse(CHARA_MODEL_XML).getroot()
    models: dict[str, dict] = {}
    bodies: dict[int, dict] = {}
    for entry in root.findall("entry"):
        name = entry.get("name")
        values_node = entry.find("values")
        if values_node is None:
            continue
        values = {
            int(value.get("index")): (value.text or "")
            for value in values_node.findall("value")
            if value.get("index") is not None
        }
        if name == "CHARA_MODEL_INFO":
            paths = [values.get(index, "") for index in (1, 10) if values.get(index)]
            for model_path in paths:
                model_path = model_path.replace("\\", "/")
                models[model_path] = values
        elif name == "CHARA_BODY_INFO" and values.get(0):
            try:
                bodies[int(values[0])] = values
            except ValueError:
                pass
    return models, bodies


def load_chara_model_lookup() -> dict:
    global CHARA_MODEL_LOOKUP_DATA
    if CHARA_MODEL_LOOKUP_DATA is not None:
        return CHARA_MODEL_LOOKUP_DATA
    if CHARA_MODEL_LOOKUP is None or not CHARA_MODEL_LOOKUP.exists():
        CHARA_MODEL_LOOKUP_DATA = {}
        return {}
    try:
        CHARA_MODEL_LOOKUP_DATA = json.loads(CHARA_MODEL_LOOKUP.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        CHARA_MODEL_LOOKUP_DATA = {}
    return CHARA_MODEL_LOOKUP_DATA


def lookup_model_row(path: Path) -> tuple[dict | None, str | None]:
    lookup = load_chara_model_lookup()
    models = lookup.get("models") or {}
    if not models:
        return None, None
    for candidate in model_relpath_candidates(path):
        row = models.get(candidate)
        if row:
            return row, candidate
    return None, None


def lookup_body_row(model_row: dict) -> dict | None:
    lookup = load_chara_model_lookup()
    bodies = lookup.get("bodies") or {}
    body_id = model_row.get("body_id")
    if body_id is None:
        return None
    return bodies.get(str(body_id)) or bodies.get(body_id)


def skeleton_candidates_from_lookup_row(row: dict | None) -> list[Path]:
    if not row:
        return []

    candidates: list[Path] = []
    for field in ("g4sk_path", "body_path"):
        value = (row.get(field) or "").replace("\\", "/")
        if not value:
            continue
        rel = Path(value).with_suffix(".g4sk")
        parts = list(rel.parts)
        if parts and parts[0] == "_common" and len(parts) > 1:
            candidates.append(RAW_DATA_ROOT / "common" / "chr" / Path(*parts[1:]))
        candidates.append(RAW_DATA_ROOT / "common" / "chr" / rel)

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def skeleton_candidates_from_body_info(body_info: dict, include_crc: bool = True) -> list[Path]:
    candidates: list[Path] = []
    body_path = body_info.get(1, "").replace("\\", "/")
    if body_path:
        body_rel = Path(body_path).with_suffix(".g4sk")
        parts = list(body_rel.parts)
        if parts and parts[0] == "_common" and len(parts) > 1:
            candidates.append(RAW_DATA_ROOT / "common" / "chr" / Path(*parts[1:]))
        candidates.append(RAW_DATA_ROOT / "common" / "chr" / body_rel)

    if include_crc:
        try:
            base_crc = int(body_info.get(3, "0"))
        except ValueError:
            base_crc = 0
        if base_crc:
            candidates.extend(g4sk_paths_for_signed_crc(base_crc))
    return candidates


def g4sk_paths_for_signed_crc(value: int) -> list[Path]:
    roots = [
        RAW_DATA_ROOT / "common" / "chr",
        RAW_DATA_ROOT / "common",
    ]
    matches: list[Path] = []
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for sk_path in root.glob("**/*.g4sk"):
            if sk_path in seen:
                continue
            seen.add(sk_path)
            try:
                if signed_crc32(sk_path.stem) == value:
                    matches.append(sk_path)
            except UnicodeEncodeError:
                continue
    return sorted(matches)


def skeleton_cache_source_key(path: Path) -> str:
    try:
        return path.relative_to(RAW_DATA_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def build_skeleton_cache(cache_path: Path) -> dict:
    models, bodies = load_chara_model_maps()
    cache = {
        "version": 1,
        "raw_data_root": str(RAW_DATA_ROOT),
        "chara_model_xml": str(CHARA_MODEL_XML),
        "models": {},
        "skeletons": {},
    }
    if not models or not bodies:
        return cache

    for model_key, model_info in models.items():
        try:
            body_id = int(model_info.get(4, "0"))
            body_info = bodies.get(body_id)
        except ValueError:
            body_id = 0
            body_info = None
        if body_info is None:
            continue

        candidates = skeleton_candidates_from_body_info(body_info, include_crc=False)
        if not any(candidate.exists() for candidate in candidates):
            candidates = skeleton_candidates_from_body_info(body_info, include_crc=True)

        for candidate in candidates:
            if not candidate.exists():
                continue
            source_key = skeleton_cache_source_key(candidate)
            if source_key not in cache["skeletons"]:
                cache["skeletons"][source_key] = {
                    "source": source_key,
                    "stem": candidate.stem,
                    "crc32b": f"{crc32b(candidate.stem.encode('ascii')):08x}",
                    "signed_crc32": signed_crc32(candidate.stem),
                    "g4sk": parse_g4sk(candidate.read_bytes()),
                }
            cache["models"][model_key] = {
                "skeleton": source_key,
                "body_id": body_id,
                "body_path": body_info.get(1, "").replace("\\", "/"),
                "body_skeleton_crc": body_info.get(3, "0"),
            }
            break

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    return cache


def load_skeleton_cache() -> dict | None:
    if SKELETON_CACHE is None:
        return None
    if not SKELETON_CACHE.exists():
        if CHARA_MODEL_XML.exists():
            return build_skeleton_cache(SKELETON_CACHE)
        return None
    cache = json.loads(SKELETON_CACHE.read_text(encoding="utf-8"))
    if CHARA_MODEL_XML.exists():
        stale = (
            cache.get("chara_model_xml") != str(CHARA_MODEL_XML)
            or cache.get("raw_data_root") != str(RAW_DATA_ROOT)
            or not cache.get("models")
        )
        if stale:
            return build_skeleton_cache(SKELETON_CACHE)
    return cache


def resolve_g4sk_info_from_cache(path: Path) -> tuple[dict | None, str | None]:
    cache = load_skeleton_cache()
    if not cache:
        return None, None
    for candidate in model_relpath_candidates(path):
        model_row = cache.get("models", {}).get(candidate)
        if not model_row:
            continue
        skeleton_key = model_row.get("skeleton")
        skeleton_row = cache.get("skeletons", {}).get(skeleton_key)
        if skeleton_row and skeleton_row.get("g4sk"):
            return skeleton_row["g4sk"], f"{SKELETON_CACHE}::{skeleton_key} ({candidate})"
    return None, None


def resolve_g4sk_from_chara_model(path: Path) -> tuple[bytes | None, str | None]:
    model_row, model_key = lookup_model_row(path)
    if model_row is not None:
        body_row = lookup_body_row(model_row)
        candidates = skeleton_candidates_from_lookup_row(model_row)
        candidates.extend(skeleton_candidates_from_lookup_row(body_row))
        for candidate in candidates:
            if candidate.exists():
                return candidate.read_bytes(), f"{candidate} via {CHARA_MODEL_LOOKUP} ({model_key})"

    models, bodies = load_chara_model_maps()
    if not models:
        return None, None

    rel_candidates = model_relpath_candidates(path)
    model_info = None
    model_key = None
    for candidate in rel_candidates:
        if candidate in models:
            model_info = models[candidate]
            model_key = candidate
            break
    if model_info is None:
        return None, None

    body_info = None
    try:
        body_info = bodies.get(int(model_info.get(4, "0")))
    except ValueError:
        body_info = None
    if body_info is None:
        return None, None

    seen = set()
    for candidate in skeleton_candidates_from_body_info(body_info):
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate.read_bytes(), f"{candidate} via {CHARA_MODEL_XML} ({model_key})"
    return None, None


def resolve_texture_from_chara_model(path: Path) -> Path | None:
    model_row, _ = lookup_model_row(path)
    if model_row is not None:
        texture_path = (model_row.get("texture_path") or "").replace("\\", "/")
        if texture_path and texture_path != "0":
            for root in ("dx11", "common", "nx"):
                candidate = RAW_DATA_ROOT / root / "chr" / texture_path
                if candidate.exists():
                    return candidate

    models, _ = load_chara_model_maps()
    if not models:
        return None
    model_info = None
    for candidate in model_relpath_candidates(path):
        if candidate in models:
            model_info = models[candidate]
            break
    if model_info is None:
        return None
    texture_path = model_info.get(11, "").replace("\\", "/")
    if not texture_path:
        return None
    candidate = RAW_DATA_ROOT / "dx11" / "chr" / texture_path
    if candidate.exists():
        return candidate
    candidate = RAW_DATA_ROOT / "common" / "chr" / texture_path
    if candidate.exists():
        return candidate
    return None


def resolve_uniform_generic_g4sk(path: Path) -> tuple[bytes | None, str | None]:
    try:
        rel = path.relative_to(RAW_DATA_ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()
    if "/_uniform/" not in f"/{rel}":
        return None, None

    for stem in ("c000401", "c000301", "c000201", "c000101"):
        candidate = RAW_DATA_ROOT / "common" / "chr" / stem / f"{stem}.g4sk"
        if candidate.exists():
            return candidate.read_bytes(), f"{candidate} via _uniform generic fallback"
    return None, None


def find_skeleton_for_model(path: Path, pack_data: bytes | None = None) -> tuple[bytes | None, str | None]:
    if pack_data is not None and pack_data[:4] == b"G4PK":
        pack = parse_g4pk(pack_data)
        for entry in pack["entries"]:
            if entry["magic"] == "G4SK":
                start = entry["offset"]
                end = start + entry["size"]
                return pack_data[start:end], f"{path}::{entry['name']}"

    own = companion(path, ".g4sk")
    if own is not None:
        return own.read_bytes(), str(own)

    skeleton_data, skeleton_source = resolve_g4sk_from_chara_model(path)
    if skeleton_data is not None:
        return skeleton_data, skeleton_source
    return resolve_uniform_generic_g4sk(path)



def find_texture_for_model(path: Path) -> Path | None:
    stem = path.stem
    candidates = []
    xml_texture = resolve_texture_from_chara_model(path)
    if xml_texture is not None:
        candidates.append(xml_texture)
    candidates.append(path.with_suffix(".g4tx"))

    try:
        rel = path.relative_to(RAW_DATA_ROOT)
    except ValueError:
        rel = None

    if rel is not None:
        parts = list(rel.parts)
        if parts and parts[0] in {"common", "dx11", "nx"}:
            for root in ("dx11", "common", "nx"):
                alt = RAW_DATA_ROOT.joinpath(root, *parts[1:]).with_suffix(".g4tx")
                candidates.append(alt)

    # Some face G4TX files are one level above the model folder.
    if rel is not None and len(rel.parts) > 2:
        parent_file = RAW_DATA_ROOT / "dx11" / Path(*rel.parts[1:-1]).with_suffix(".g4tx")
        candidates.append(parent_file)

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate

    # Last resort: local sibling search by stem.
    for root in (RAW_DATA_ROOT / "dx11" / "chr", RAW_DATA_ROOT / "common" / "chr"):
        if not root.exists():
            continue
        matches = list(root.glob(f"**/{stem}.g4tx"))
        if matches:
            return matches[0]
    return None


def find_texture_containers_for_model(path: Path) -> list[Path]:
    candidates: list[Path] = []
    primary = find_texture_for_model(path)
    if primary is not None:
        candidates.append(primary)

    try:
        rel = path.relative_to(RAW_DATA_ROOT)
    except ValueError:
        rel = None

    if rel is not None and len(rel.parts) >= 4 and rel.parts[0] in {"common", "dx11", "nx"}:
        parts = list(rel.parts)
        if parts[1] == "map":
            dx11_base = RAW_DATA_ROOT / "dx11" / Path(*parts[1:-2])
            if dx11_base.exists():
                candidates.extend(sorted(dx11_base.glob("*.g4tx")))
            local_dir = RAW_DATA_ROOT / "dx11" / Path(*parts[1:-1])
            if local_dir.exists():
                candidates.extend(sorted(local_dir.glob("*.g4tx")))

    seen = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def find_shared_map_texture_containers(path: Path, material_names: Iterable[str]) -> list[Path]:
    try:
        rel = path.relative_to(RAW_DATA_ROOT)
    except ValueError:
        return []
    if len(rel.parts) < 4 or rel.parts[0] not in {"common", "dx11", "nx"} or rel.parts[1] != "map":
        return []

    candidates: list[Path] = []
    map_kind = rel.parts[2]
    for material_name in material_names:
        match = re.match(r"([a-z]\d{2}[a-z]\d{3})", material_name.lower())
        if not match:
            continue
        stem = match.group(1)
        folders = [stem]
        extended = re.match(r"([a-z]\d{2}[a-z]\d{3})[a-z]\d{2}", material_name.lower())
        if extended:
            folders.insert(0, extended.group(1))
        for folder in folders:
            candidates.append(RAW_DATA_ROOT / "dx11" / "map" / map_kind / folder / f"{folder}.g4tx")
            candidates.append(RAW_DATA_ROOT / "dx11" / "map" / map_kind / folder / f"{stem}.g4tx")

    seen = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def find_shared_texture_containers_for_materials(path: Path, material_names: Iterable[str]) -> list[Path]:
    try:
        rel = path.relative_to(RAW_DATA_ROOT)
    except ValueError:
        return []
    if len(rel.parts) < 3 or rel.parts[0] not in {"common", "dx11", "nx"} or rel.parts[1] != "chr":
        return []

    candidates: list[Path] = []
    for material_name in material_names:
        match = re.match(r"([A-Za-z]\d{6})_", material_name)
        if not match:
            continue
        stem = match.group(1)
        candidates.append(RAW_DATA_ROOT / "dx11" / "chr" / stem / f"{stem}.g4tx")

    seen = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def extract_g4tx(path: Path, out_dir: Path) -> list[Path]:
    data = path.read_bytes()
    info = parse_g4tx(data)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for texture in info["textures"]:
        name = texture["name"]
        offset = texture["offset"]
        size = texture["size"]
        payload = data[offset : offset + size]
        if payload.startswith(b"DDS "):
            ext = ".dds"
            out_data = payload
        elif payload.startswith(b"NXTCH000"):
            ext = ".nxtch"
            out_data = payload
        else:
            ext = ".bin"
            out_data = payload
        out_path = out_dir / f"{name}{ext}"
        out_path.write_bytes(out_data)
        written.append(out_path)
    return written


def extract_texture_containers(paths: list[Path], out_dir: Path) -> list[Path]:
    written: list[Path] = []
    for path in paths:
        written.extend(extract_g4tx(path, out_dir / path.stem))
    return written


def load_model(path: Path) -> dict:
    configure_raw_data_root_from_path(path)
    data = path.read_bytes()
    result: dict = {"path": str(path)}
    pack_data = data if data[:4] == b"G4PK" else None

    if data[:4] == b"G4PK":
        result["pack"] = parse_g4pk(data)
        md_data = find_embedded(data, b"G4MD")
        if md_data is not None:
            md_size = u32(md_data, 0x0C) + 0xA4 if len(md_data) >= 0x10 else len(md_data)
            result["embedded_g4md_offset"] = data.find(b"G4MD")
            result["embedded_g4md_probe_size"] = min(len(md_data), md_size)
            data = md_data
        else:
            external_md = companion(path, ".g4md")
            if external_md is None:
                return result
            result["external_g4md"] = str(external_md)
            data = external_md.read_bytes()

    if data[:4] != b"G4MD":
        result["error"] = "No G4MD data found"
        return result

    skeleton_data, skeleton_source = find_skeleton_for_model(path, pack_data)
    if skeleton_data is not None:
        result["g4sk_source"] = skeleton_source
        result["g4sk"] = parse_g4sk(skeleton_data)
    else:
        skeleton_info, skeleton_source = resolve_g4sk_info_from_cache(path)
        if skeleton_info is not None:
            result["g4sk_source"] = skeleton_source
            result["g4sk"] = skeleton_info
        else:
            skeleton_data, skeleton_source = resolve_g4sk_from_chara_model(path)
            if skeleton_data is not None:
                result["g4sk_source"] = skeleton_source
                result["g4sk"] = parse_g4sk(skeleton_data)

    mg_path = companion(path, ".g4mg")
    g4mg = mg_path.read_bytes() if mg_path is not None else None
    if mg_path is not None:
        result["g4mg_path"] = str(mg_path)

    result["g4md"] = parse_g4md(data, g4mg)
    return result


def export_obj(path: Path, out_dir: Path) -> Path:
    configure_raw_data_root_from_path(path)
    data = path.read_bytes()
    if data[:4] == b"G4PK":
        embedded_offset = data.find(b"G4MD")
        if embedded_offset < 0:
            external_md = companion(path, ".g4md")
            if external_md is None:
                raise ValueError(f"{path} has no embedded or external G4MD")
            data = external_md.read_bytes()
        else:
            data = data[embedded_offset:]

    if data[:4] != b"G4MD":
        raise ValueError(f"{path} is not G4MD/G4PKM")

    g4mg_path = companion(path, ".g4mg")
    if g4mg_path is None:
        raise ValueError(f"{path} has no companion .g4mg")
    g4mg = g4mg_path.read_bytes()

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{path.stem}.probe.obj"
    mesh_count = u16(data, 0x20)
    index_base = u32(data, 0x5C)

    vertex_base = 1
    with out_path.open("w", encoding="ascii", newline="\n") as obj:
        obj.write(f"# Probe OBJ generated from {path}\n")
        for mesh_index in range(mesh_count):
            rec = 0xA0 + mesh_index * 0x50
            vertex_offset = u32(data, rec + 0x00)
            index_offset = u32(data, rec + 0x04)
            vertex_count = u32(data, rec + 0x08)
            index_count = u32(data, rec + 0x0C)
            flags1 = u16(data, rec + 0x3E)
            vertex_stride = flags1 & 0xFF or 0x44
            uv_offset = uv0_offset_for_stride(vertex_stride)

            obj.write(f"\no mesh_{mesh_index:03d}\n")
            for vertex_index in range(vertex_count):
                off = vertex_offset + vertex_index * vertex_stride
                x, y, z = struct.unpack_from("<fff", g4mg, off)
                obj.write(f"v {x:.9g} {y:.9g} {z:.9g}\n")

            for vertex_index in range(vertex_count):
                if uv_offset is None:
                    obj.write("vt 0 0\n")
                else:
                    off = vertex_offset + vertex_index * vertex_stride + uv_offset
                    u, v = struct.unpack_from("<HH", g4mg, off)
                    obj.write(f"vt {u / 65535.0:.9g} {1.0 - (v / 65535.0):.9g}\n")

            indices = struct.unpack_from("<" + "H" * index_count, g4mg, index_base + index_offset)
            positions = [
                struct.unpack_from("<fff", g4mg, vertex_offset + vertex_index * vertex_stride)
                for vertex_index in range(vertex_count)
            ]
            normals = read_native_normals(g4mg, vertex_offset, vertex_count, vertex_stride)
            if normals is None:
                normals = [(0.0, 1.0, 0.0)] * vertex_count
            for nx, ny, nz in normals:
                obj.write(f"vn {nx:.9g} {ny:.9g} {nz:.9g}\n")

            for face in range(0, index_count - 2, 3):
                a, b, c = indices[face : face + 3]
                obj.write(
                    f"f {vertex_base + a}/{vertex_base + a}/{vertex_base + a} "
                    f"{vertex_base + b}/{vertex_base + b}/{vertex_base + b} "
                    f"{vertex_base + c}/{vertex_base + c}/{vertex_base + c}\n"
                )

            vertex_base += vertex_count

    return out_path


def read_model_buffers(path: Path) -> tuple[bytes, bytes, Path | None]:
    configure_raw_data_root_from_path(path)
    data = path.read_bytes()
    if data[:4] == b"G4PK":
        embedded_offset = data.find(b"G4MD")
        if embedded_offset >= 0:
            data = data[embedded_offset:]
        else:
            external_md = companion(path, ".g4md")
            if external_md is None:
                raise ValueError(f"{path} has no embedded or external G4MD")
            data = external_md.read_bytes()

    if data[:4] != b"G4MD":
        raise ValueError(f"{path} is not G4MD/G4PKM")

    g4mg_path = companion(path, ".g4mg")
    if g4mg_path is None:
        raise ValueError(f"{path} has no companion .g4mg")
    return data, g4mg_path.read_bytes(), g4mg_path


def resolve_model_input(value: Path) -> Path:
    if value.exists():
        if value.is_dir():
            matches = sorted(
                path
                for path in value.iterdir()
                if path.is_file() and path.suffix.lower() in {".g4pkm", ".g4md", ".objbin"}
            )
            if matches:
                return preferred_model_path(matches, value.name)
            raise ValueError(f"{value} has no G4 model file")
        return value

    text = value.as_posix()
    candidates: list[Path] = []
    if "/" in text:
        for root in ("common", "dx11", "nx"):
            candidate = RAW_DATA_ROOT / root / text
            candidates.extend(model_path_variants(candidate))
    else:
        candidates.extend(search_model_by_name(text))

    candidates = [candidate for candidate in candidates if candidate.exists()]
    if not candidates:
        raise ValueError(f"Could not resolve model input: {value}")
    return preferred_model_path(candidates, Path(text).stem)


def model_path_variants(path: Path) -> list[Path]:
    if path.suffix.lower() in MODEL_EXTENSIONS:
        variants = [path]
    else:
        variants = [path.with_suffix(ext) for ext in (".g4pkm", ".g4md", ".objbin")]
        variants.extend(path / f"{path.name}{ext}" for ext in (".g4pkm", ".g4md", ".objbin"))
    return variants


def search_model_by_name(name: str) -> list[Path]:
    stem = Path(name).stem
    matches: list[Path] = []
    for ext in (".g4pkm", ".g4md", ".objbin"):
        matches.extend(RAW_DATA_ROOT.glob(f"common/**/{stem}{ext}"))
    return matches


def preferred_model_path(paths: list[Path], stem: str) -> Path:
    def score(path: Path) -> tuple[int, int, str]:
        suffix_score = {".g4pkm": 0, ".g4md": 1, ".objbin": 2}.get(path.suffix.lower(), 3)
        exact_dir = 0 if path.parent.name == stem else 1
        return suffix_score, exact_dir, path.as_posix()

    return sorted(paths, key=score)[0]


def mesh_name_for_record(md_info: dict, record: MeshRecord | dict) -> str:
    rec = record if isinstance(record, dict) else asdict(record)
    mesh_names = md_info.get("mesh_names", [])
    if rec["index"] < len(mesh_names):
        return mesh_names[rec["index"]]
    records = md_info.get("records", [])
    sorted_name_indices = sorted({item["name_index"] for item in records})
    if rec["name_index"] in sorted_name_indices:
        ordinal = sorted_name_indices.index(rec["name_index"])
        if ordinal < len(mesh_names):
            return mesh_names[ordinal]
    return f"mesh_{rec['index']:03d}"


def mesh_name_for_export(md_info: dict, record: dict, skeleton_info: dict | None = None) -> str:
    mesh_name = mesh_name_for_record(md_info, record)
    if not mesh_name.startswith("mesh_"):
        return mesh_name
    if skeleton_info is not None:
        names = skeleton_info.get("names", [])
        name_index = record.get("name_index", -1)
        if 0 <= name_index < len(names):
            name = names[name_index]
            if name and name not in {"output"}:
                return name
    return mesh_name


def clean_material_base(material_name: str) -> str:
    base = material_name[:-1] if material_name.endswith("M") else material_name
    base = base.strip("_")
    while base.startswith("pasted_"):
        base = base[len("pasted_") :].strip("_")
    return base


def compact_name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def semantic_material_score(mesh_name: str, material_name: str, model_stem: str) -> int:
    if not model_stem or not model_stem.startswith("w"):
        return 0

    mesh = mesh_name.lower().strip("_")
    mesh = re.sub(r"^_?merge_", "", mesh)
    mesh = re.sub(r"_lv\d+$", "", mesh)
    mesh = re.sub(r"_light$", "", mesh)
    mesh_compact = compact_name_key(mesh)

    material = clean_material_base(material_name).lower()
    material = material.lstrip("_")
    if material.startswith(f"{model_stem.lower()}_"):
        material_tail = material[len(model_stem) + 1 :]
    else:
        material_tail = material
    material_tail = re.sub(r"_bf$", "", material_tail)
    material_tail = re.sub(r"_(?:p|l)?_?\d+$", "", material_tail)
    material_compact = compact_name_key(material_tail)

    if not material_compact:
        return 0
    if material_compact in mesh_compact or mesh_compact in material_compact:
        return 100 + len(material_compact)

    cb_match = re.match(r"0?(\d+)_cb", material_tail)
    window_match = re.search(r"window0?(\d+)", mesh)
    if cb_match and window_match and int(cb_match.group(1)) == int(window_match.group(1)):
        return 95

    for token in ("bl01", "bl02", "roof01", "wall01", "name_a"):
        if token in mesh_compact and token in material_compact:
            return 90 + len(token)

    if "farbush" in mesh_compact and "farbush" in material_compact:
        return 88
    if "grass" in mesh_compact and "grass" in material_compact:
        return 88
    if ("sdw" in mesh_compact or "shadow" in mesh_compact) and (
        "sdw" in material_compact or "shadow" in material_compact
    ):
        return 86
    if "light" in mesh_compact and "light" in material_compact:
        return 80
    return 0


def semantic_material_for_mesh(md_info: dict, mesh_name: str, model_stem: str) -> str | None:
    material_names = md_info.get("material_names", [])
    scored = [
        (semantic_material_score(mesh_name, material_name, model_stem), material_name)
        for material_name in material_names
    ]
    scored = [(score, material_name) for score, material_name in scored if score > 0]
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], int(item[1].startswith("pasted_")), item[1]))
    return scored[0][1]


def material_name_for_mesh(
    md_info: dict,
    mesh_name: str,
    fallback_index: int,
    record: dict | None = None,
    model_stem: str = "",
) -> str:
    material_names = md_info.get("material_names", [])
    if record is not None:
        material_index = record.get("material_index")
        if material_index is not None and material_index < len(material_names):
            return material_names[material_index]
    semantic = semantic_material_for_mesh(md_info, mesh_name, model_stem)
    if semantic is not None:
        return semantic
    material_name_index_map = md_info.get("material_name_index_map") or {}
    if record is not None:
        material_index = material_name_index_map.get(record.get("name_index"))
        if material_index is not None and material_index < len(material_names):
            return material_names[material_index]
    direct = f"{mesh_name}M"
    if direct in material_names:
        return direct
    if mesh_name.endswith("_LOD1") or mesh_name.endswith("_LOD2"):
        base = mesh_name.rsplit("_LOD", 1)[0]
        direct = f"{base}M"
        if direct in material_names:
            return direct
    if mesh_name.startswith("hair"):
        for material_name in material_names:
            if material_name.endswith("_20M"):
                return material_name
    if fallback_index < len(material_names):
        return material_names[fallback_index]
    return f"material_{fallback_index:03d}"


def texture_usage_from_name(name: str) -> str:
    lower = name.lower()
    if lower.endswith("_a.1"):
        return "transparent_base"
    if lower.endswith(".a"):
        return "alpha_red"
    if lower.endswith("msk") or lower.endswith("_mask"):
        return "mask"
    if lower.endswith("nml") or lower.endswith("_normal"):
        return "normal"
    if lower.endswith("_re.2") or lower.endswith("_n.2") or lower.endswith("_nm.2"):
        return "normal"
    if lower.endswith(".2") and not lower.endswith(("line.2", "sp.2", "oc.2")):
        return "normal_or_packed"
    if lower.endswith(".1") and (
        lower.endswith("_re.1") or lower.endswith("_n.1") or lower.endswith("_nm.1") or "_normal" in lower
    ):
        return "base"
    if lower.endswith(".2") or lower.endswith("_n") or lower.endswith("_nm") or "_normal" in lower:
        return "normal"
    if lower.endswith("spm"):
        return "specular_mask"
    if lower.endswith("sp"):
        return "specular"
    if lower.endswith("oc"):
        return "occlusion"
    if lower.endswith("line"):
        return "line"
    return "base"


def strip_texture_variant(name: str) -> str:
    lower = name.lower()
    usage = texture_usage_from_name(lower)
    if usage == "transparent_base" and lower.endswith("_a.1"):
        return lower[:-2]
    if usage in {"normal", "normal_or_packed"} and lower.endswith(".2"):
        return lower[:-2]
    if usage == "normal" and lower.endswith("nml"):
        return re.sub(r"_?nml$", "", lower)
    if usage == "alpha_red" and lower.endswith(".a"):
        return lower[:-2]
    if usage == "mask":
        return re.sub(r"(?:_?msk|_mask)$", "", lower)
    return re.sub(r"\.(?:\d+|[A-Za-z])$", "", lower)


def texture_key(name: str) -> str:
    return strip_texture_variant(name).strip("_").lower()


def texture_keys_for_name(name: str) -> list[str]:
    key = texture_key(name)
    keys = [key]
    usage = texture_usage_from_name(name)
    if usage == "alpha_red" and key and not key.endswith("_a"):
        keys.append(f"{key}_a")
    if key.endswith("_a"):
        keys.append(key[:-2])

    unique: list[str] = []
    seen = set()
    for item in keys:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def is_diffuse_texture(path: Path) -> bool:
    return texture_usage_from_name(path.stem) in {"base", "transparent_base"}


def material_texture_keys(material_name: str, model_stem: str, mesh_name: str = "") -> list[str]:
    base = clean_material_base(material_name)
    base = base.rstrip("_")
    keys = [base]

    cb_match = re.match(r"(.+?_cb)(?:_(?:p|l))?(?:_\d+)?$", base)
    if cb_match:
        keys.append(cb_match.group(1))

    for suffix in ("_bf",):
        if base.endswith(suffix):
            keys.append(base[: -len(suffix)])

    no_numeric = re.sub(r"_\d+$", "", base)
    if no_numeric != base:
        keys.append(no_numeric)

    if re.search(r"_bl01$", base) or re.search(r"_wall01$", base):
        keys.append(f"_{model_stem}_01_cb")
    if re.search(r"_bl02$", base):
        keys.append(f"_{model_stem}_02_cb")
    if re.search(r"_roof01$", base):
        keys.append(f"{model_stem}at")

    if base.startswith(f"{model_stem}_stair"):
        keys.append(base.replace(f"{model_stem}_stair", "w10g_stairs") + "_re")

    if material_name.startswith("material_"):
        mesh = mesh_name.rstrip("_")
        window_match = re.match(r"window(\d{2})[A-Za-z]?_", mesh)
        if window_match:
            keys.append(f"{model_stem}_window{window_match.group(1)}_a")
        door_match = re.match(r"door_(\d{2})", mesh)
        if door_match:
            keys.append(f"{model_stem}_door{door_match.group(1)}_a")
        if mesh.startswith("light_"):
            keys.append(f"{model_stem}_light01_a")

    if base in {"eye_10", "mouth_10", "hair1", "hair2", "hair3", "mant_10", "mant_11"}:
        keys.insert(0, f"{model_stem}_10")

    unique: list[str] = []
    seen = set()
    for key in keys:
        normalized = texture_key(key)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def texture_variant_score(path: Path, model_stem: str) -> tuple[int, int, str]:
    stem = path.stem.lower()
    usage = texture_usage_from_name(stem)
    variant = stem.rsplit(".", 1)[1] if "." in stem else ""
    usage_score = {
        "base": 0,
        "transparent_base": 1,
        "normal_or_packed": 9,
        "mask": 8,
        "specular": 4,
        "specular_mask": 5,
        "occlusion": 6,
        "line": 7,
        "alpha_red": 8,
        "normal": 9,
    }.get(usage, 10)
    variant_score = {"": 0, "a": 1, "1": 6, "2": 7, "3": 8}.get(variant, 9)
    locality = 0 if model_stem in path.parts or path.parent.name == model_stem else 1
    return usage_score, variant_score, locality, str(path)


def texture_hashes_by_name(texture_paths: list[Path]) -> dict[str, Path]:
    by_hash: dict[str, Path] = {}
    for texture in texture_paths:
        try:
            value = crc32b(texture.stem.encode("ascii"))
        except UnicodeEncodeError:
            continue
        by_hash.setdefault(f"{value:08x}", texture)
    return by_hash


def texture_slots_by_g4md_hash(md_info: dict, texture_paths: list[Path]) -> dict[int, Path]:
    by_hash = texture_hashes_by_name(texture_paths)
    slots: dict[int, Path] = {}
    for row in md_info.get("texture_hashes", []):
        texture = by_hash.get(row.get("hash"))
        if texture is not None:
            slots[row["index"]] = texture
    return slots


def material_record_by_name(md_info: dict, material_name: str) -> dict | None:
    names = md_info.get("material_names", [])
    for material in md_info.get("material_records", []):
        index = material.get("index", -1)
        if index < len(names) and names[index] == material_name:
            return material
    return None


def choose_texture_from_material_record(
    md_info: dict, material_name: str, texture_paths: list[Path], model_stem: str
) -> Path | None:
    material = material_record_by_name(md_info, material_name)
    if material is None:
        return None
    slots = texture_slots_by_g4md_hash(md_info, texture_paths)
    candidates: list[Path] = []
    for ref in material.get("texture_refs", []):
        if ref.get("slot_type") == 0:
            continue
        texture = slots.get(ref.get("texture_index"))
        if texture is None:
            continue
        usage = texture_usage_from_name(texture.stem)
        if usage in {"base", "transparent_base"}:
            candidates.append(texture)
    if candidates:
        return sorted(candidates, key=lambda item: texture_variant_score(item, model_stem))[0]
    return None


def enriched_material_records(md_info: dict, texture_paths: list[Path]) -> list[dict]:
    by_hash = texture_hashes_by_name(texture_paths)
    by_slot = texture_slots_by_g4md_hash(md_info, texture_paths)
    texture_hashes = md_info.get("texture_hashes", [])
    names = md_info.get("material_names", [])
    enriched = []
    for material in md_info.get("material_records", []):
        refs = []
        for ref in material.get("texture_refs", []):
            texture = by_slot.get(ref.get("texture_index"))
            refs.append(
                {
                    **ref,
                    "texture_name": texture.stem if texture is not None else None,
                    "texture_path": str(texture) if texture is not None else None,
                    "usage": texture_usage_from_name(texture.stem) if texture is not None else None,
                }
            )
        hash_rows = []
        for row in texture_hashes:
            texture = by_hash.get(row.get("hash"))
            hash_rows.append({**row, "texture_name": texture.stem if texture is not None else None})
        enriched.append(
            {
                **material,
                "name": names[material["index"]] if material["index"] < len(names) else None,
                "texture_refs": refs,
                "texture_hashes_resolved": hash_rows,
            }
        )
    return enriched


def choose_texture(material_name: str, extracted: list[Path], model_stem: str, mesh_name: str = "") -> Path | None:
    if not extracted:
        return None
    wanted_keys = material_texture_keys(material_name, model_stem, mesh_name)
    by_key: dict[str, list[Path]] = {}
    for texture in extracted:
        for key in texture_keys_for_name(texture.stem):
            by_key.setdefault(key, []).append(texture)

    def choose_diffuse(matches: list[Path]) -> Path | None:
        diffuse = [texture for texture in matches if is_diffuse_texture(texture)]
        if diffuse:
            return sorted(diffuse, key=lambda item: texture_variant_score(item, model_stem))[0]
        return None

    for wanted in wanted_keys:
        matches = by_key.get(wanted, [])
        if matches:
            chosen = choose_diffuse(matches)
            if chosen is not None:
                return chosen

    for wanted in wanted_keys:
        matches = [
            texture
            for key, textures in by_key.items()
            if key.startswith(wanted) or wanted.startswith(key)
            for texture in textures
        ]
        if matches:
            chosen = choose_diffuse(matches)
            if chosen is not None:
                return chosen
    return None


def summarize_texture_variants(texture_paths: list[Path]) -> dict[str, dict[str, list[str]]]:
    variants: dict[str, dict[str, list[str]]] = {}
    for texture in texture_paths:
        usage = texture_usage_from_name(texture.stem)
        for key in texture_keys_for_name(texture.stem):
            variants.setdefault(key, {}).setdefault(usage, []).append(str(texture))
    for roles in variants.values():
        for paths in roles.values():
            paths.sort()
    return dict(sorted(variants.items()))


def dae_float_array(values: Iterable[float]) -> str:
    return " ".join(f"{value:.9g}" for value in values)


def dae_int_array(values: Iterable[int]) -> str:
    return " ".join(str(value) for value in values)


def dae_name_array(values: Iterable[str]) -> str:
    return " ".join(value.replace(" ", "_") for value in values)


def identity_matrix() -> list[float]:
    return [
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def joint_node_id(index: int) -> str:
    return f"joint_{index:03d}"


def render_joint_nodes(skeleton_info: dict) -> str:
    names = skeleton_info.get("names", [])
    parents = skeleton_info.get("parent_indices", [])
    matrices = skeleton_info.get("local_matrices") or skeleton_info.get("bind_matrices", [])
    joint_count = skeleton_info.get("joint_count", len(names))
    children: dict[int, list[int]] = {index: [] for index in range(joint_count)}
    roots: list[int] = []
    for index in range(joint_count):
        parent = parents[index] if index < len(parents) else joint_count
        if parent < joint_count and parent != index:
            children.setdefault(parent, []).append(index)
        else:
            roots.append(index)

    def render(index: int) -> str:
        name = names[index] if index < len(names) and names[index] else joint_node_id(index)
        matrix = matrices[index] if index < len(matrices) else identity_matrix()
        return (
            f'<node id="{joint_node_id(index)}" sid="{escape(name)}" name="{escape(name)}" type="JOINT">'
            f"<matrix>{dae_float_array(matrix)}</matrix>"
            f"{''.join(render(child) for child in children.get(index, []))}"
            f"</node>"
        )

    return "".join(render(root) for root in roots)


def build_skin_controller(payload: dict, skeleton_info: dict) -> tuple[str, dict] | None:
    names = skeleton_info.get("names", [])
    matrices = skeleton_info.get("inverse_bind_matrices") or skeleton_info.get("bind_matrices", [])
    joint_count = skeleton_info.get("joint_count", len(names))
    palette_base = payload["palette_base"]
    joint_palette = payload.get("joint_palette") or []
    influences = payload["skin_influences"]
    bind_shape_matrix = payload.get("bind_shape_matrix") or identity_matrix()

    def resolve_joint(local_index: int) -> int | None:
        if joint_palette:
            return joint_palette[local_index] if local_index < len(joint_palette) else None
        fallback = palette_base + local_index
        if fallback < joint_count:
            return fallback
        return None

    used_global = sorted(
        {
            global_index
            for vertex in influences
            for local_index, weight in vertex
            if weight > 0.0
            for global_index in [resolve_joint(local_index)]
            if global_index is not None and global_index < joint_count
        }
    )
    if not used_global:
        return None

    local_by_global = {global_index: local_index for local_index, global_index in enumerate(used_global)}
    joint_names = [
        names[global_index] if global_index < len(names) and names[global_index] else joint_node_id(global_index)
        for global_index in used_global
    ]
    inverse_bind_matrices = [
        component
        for global_index in used_global
        for component in (matrices[global_index] if global_index < len(matrices) else identity_matrix())
    ]

    weights: list[float] = []
    vcount: list[int] = []
    v: list[int] = []
    unresolved = 0
    for vertex in influences:
        filtered = []
        for local_index, weight in vertex:
            if weight <= 0.0:
                continue
            global_index = resolve_joint(local_index)
            if global_index is None:
                unresolved += 1
                continue
            if global_index not in local_by_global:
                unresolved += 1
                continue
            filtered.append((local_by_global[global_index], weight))
        total = sum(weight for _, weight in filtered)
        if total > 0.0:
            filtered = [(joint, weight / total) for joint, weight in filtered]
        vcount.append(len(filtered))
        for joint, weight in filtered:
            weights.append(weight)
            v.extend((joint, len(weights) - 1))

    cid = f"{payload['id']}_controller"
    controller = (
        f'<controller id="{cid}"><skin source="#{payload["id"]}_geo">'
        f"<bind_shape_matrix>{dae_float_array(bind_shape_matrix)}</bind_shape_matrix>"
        f'<source id="{cid}_joints"><Name_array id="{cid}_joints_array" count="{len(joint_names)}">'
        f"{dae_name_array(joint_names)}</Name_array><technique_common>"
        f'<accessor source="#{cid}_joints_array" count="{len(joint_names)}" stride="1">'
        f'<param name="JOINT" type="name"/></accessor></technique_common></source>'
        f'<source id="{cid}_bind_poses"><float_array id="{cid}_bind_poses_array" count="{len(inverse_bind_matrices)}">'
        f"{dae_float_array(inverse_bind_matrices)}</float_array><technique_common>"
        f'<accessor source="#{cid}_bind_poses_array" count="{len(joint_names)}" stride="16">'
        f'<param name="TRANSFORM" type="float4x4"/></accessor></technique_common></source>'
        f'<source id="{cid}_weights"><float_array id="{cid}_weights_array" count="{len(weights)}">'
        f"{dae_float_array(weights)}</float_array><technique_common>"
        f'<accessor source="#{cid}_weights_array" count="{len(weights)}" stride="1">'
        f'<param name="WEIGHT" type="float"/></accessor></technique_common></source>'
        f"<joints><input semantic=\"JOINT\" source=\"#{cid}_joints\"/>"
        f"<input semantic=\"INV_BIND_MATRIX\" source=\"#{cid}_bind_poses\"/></joints>"
        f'<vertex_weights count="{len(vcount)}"><input semantic="JOINT" source="#{cid}_joints" offset="0"/>'
        f'<input semantic="WEIGHT" source="#{cid}_weights" offset="1"/>'
        f"<vcount>{dae_int_array(vcount)}</vcount><v>{dae_int_array(v)}</v></vertex_weights>"
        f"</skin></controller>"
    )
    summary = {
        "palette_base": palette_base,
        "palette_length": len(joint_palette),
        "joint_palette": joint_palette,
        "remap_rule": "global_joint = joint_palette[local_blend_index]; fallback = palette_or_list + local_blend_index",
        "used_global_joint_indices": used_global,
        "used_joint_names": joint_names,
        "unresolved_influences": unresolved,
    }
    return controller, summary


def compute_smooth_normals(positions: list[tuple[float, float, float]], indices: Iterable[int]) -> list[tuple[float, float, float]]:
    normals = [[0.0, 0.0, 0.0] for _ in positions]
    index_list = list(indices)
    for face in range(0, len(index_list) - 2, 3):
        ia, ib, ic = index_list[face : face + 3]
        if ia >= len(positions) or ib >= len(positions) or ic >= len(positions):
            continue
        ax, ay, az = positions[ia]
        bx, by, bz = positions[ib]
        cx, cy, cz = positions[ic]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 1e-12:
            continue
        nx, ny, nz = nx / length, ny / length, nz / length
        for vertex_index in (ia, ib, ic):
            normals[vertex_index][0] += nx
            normals[vertex_index][1] += ny
            normals[vertex_index][2] += nz

    result = []
    for nx, ny, nz in normals:
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 1e-12:
            result.append((0.0, 1.0, 0.0))
        else:
            result.append((nx / length, ny / length, nz / length))
    return result


def decode_snorm16x4(data: bytes, offset: int) -> tuple[float, float, float, float]:
    values = struct.unpack_from("<hhhh", data, offset)
    return tuple(max(-1.0, value / 32767.0) for value in values)


def read_native_normals(
    g4mg: bytes, vertex_offset: int, vertex_count: int, vertex_stride: int
) -> list[tuple[float, float, float]] | None:
    normals: list[tuple[float, float, float]] = []
    valid = 0
    for vertex_index in range(vertex_count):
        nx, ny, nz, _ = decode_snorm16x4(g4mg, vertex_offset + vertex_index * vertex_stride + 0x0C)
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if 0.25 <= length <= 1.25:
            valid += 1
            normals.append((nx / length, ny / length, nz / length))
        else:
            normals.append((0.0, 1.0, 0.0))
    if vertex_count == 0 or valid / vertex_count < 0.75:
        return None
    return normals


def read_native_normals_for_record(g4mg: bytes, md_info: dict, record: dict) -> list[tuple[float, float, float]] | None:
    layout = layout_for_record(md_info, record)
    element = layout_element(layout, 2)
    if element is None:
        return read_native_normals(
            g4mg, record["vertex_offset"], record["vertex_count"], vertex_stride_for_record(record)
        )

    vertex_count = record["vertex_count"]
    vertex_stride = vertex_stride_for_record(record)
    value_offset = element["value_offset"]
    normals: list[tuple[float, float, float]] = []
    valid = 0
    for vertex_index in range(vertex_count):
        off = record["vertex_offset"] + vertex_index * vertex_stride + value_offset
        nx, ny, nz, _ = decode_snorm16x4(g4mg, off)
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if 0.25 <= length <= 1.25:
            valid += 1
            normals.append((nx / length, ny / length, nz / length))
        else:
            normals.append((0.0, 1.0, 0.0))
    if vertex_count == 0 or valid / vertex_count < 0.75:
        return None
    return normals


def read_skin_influences(
    g4mg: bytes, md_info: dict, record: dict
) -> list[list[tuple[int, float]]]:
    vertex_offset = record["vertex_offset"]
    vertex_count = record["vertex_count"]
    vertex_stride = vertex_stride_for_record(record)
    layout = layout_for_record(md_info, record)
    weight_element = layout_element(layout, 5)
    joint_element = layout_element(layout, 6)
    influences: list[list[tuple[int, float]]] = []
    if weight_element is not None and joint_element is not None:
        sizes = layout_slice_sizes(layout, vertex_stride)
        weight_size = min(16, sizes.get(weight_element.get("index", 0), 0))
        joint_size = min(8, sizes.get(joint_element.get("index", 0), 0))
        influence_count = min(weight_size // 2, joint_size)
        if influence_count > 0:
            for vertex_index in range(vertex_count):
                base = vertex_offset + vertex_index * vertex_stride
                weight_raw = g4mg[
                    base + weight_element["value_offset"] : base + weight_element["value_offset"] + weight_size
                ]
                joint_raw = g4mg[
                    base + joint_element["value_offset"] : base + joint_element["value_offset"] + joint_size
                ]
                weights = struct.unpack_from(
                    "<" + "H" * influence_count, weight_raw.ljust(influence_count * 2, b"\0"), 0
                )
                bones = struct.unpack_from(
                    "<" + "B" * influence_count, joint_raw.ljust(influence_count, b"\0"), 0
                )
                total = sum(weights)
                if total > 0:
                    influences.append(
                        [
                            (bone, weight / total)
                            for bone, weight in zip(bones, weights)
                            if weight != 0 and bone != 0xFF
                        ]
                    )
                else:
                    influences.append([])
            return influences

    if vertex_stride < 0x3C:
        return [[] for _ in range(vertex_count)]
    for vertex_index in range(vertex_count):
        off = vertex_offset + vertex_index * vertex_stride
        weights = struct.unpack_from("<8H", g4mg, off + 0x24)
        bones = struct.unpack_from("<8B", g4mg, off + 0x34)
        vertex_influences = [
            (bone, weight / 65535.0)
            for bone, weight in zip(bones, weights)
            if weight != 0 and bone != 0xFF
        ]
        influences.append(vertex_influences)
    return influences


def rigid_skin_influences(vertex_count: int) -> list[list[tuple[int, float]]]:
    return [[(0, 1.0)] for _ in range(vertex_count)]


def skin_palette_fit(influences: list[list[tuple[int, float]]], palette_length: int) -> tuple[int, int]:
    valid = 0
    invalid = 0
    for vertex in influences:
        for local_index, weight in vertex:
            if weight <= 0.0:
                continue
            if local_index < palette_length:
                valid += 1
            else:
                invalid += 1
    return valid, invalid


def summarize_skin_influences(influences: list[list[tuple[int, float]]]) -> dict:
    used_bones = sorted({bone for vertex in influences for bone, weight in vertex if weight > 0.0})
    weighted_vertices = sum(1 for vertex in influences if vertex)
    max_influences = max((len(vertex) for vertex in influences), default=0)
    return {
        "weighted_vertices": weighted_vertices,
        "max_influences_per_vertex": max_influences,
        "used_local_bone_indices": used_bones,
    }


def skeleton_bone_orientation(skeleton_info: dict | None) -> dict | None:
    if skeleton_info is None:
        return None
    return {
        "space": "local",
        "rotation_order": "quaternion_xyzw",
        "source": "G4SK section 1 SRT",
        "names": skeleton_info.get("names", []),
        "parent_indices": skeleton_info.get("parent_indices", []),
        "local_rotations_xyzw": skeleton_info.get("local_rotations_xyzw", []),
    }


def face_rigid_joint_override(path: Path, skeleton_info: dict | None, joint_palette: list[int]) -> list[int] | None:
    if skeleton_info is None or len(joint_palette) != 1:
        return None
    normalized = path.as_posix().replace("\\", "/")
    if "/_face/" not in normalized:
        return None
    try:
        head_index = skeleton_info.get("names", []).index("c_head_1_0")
    except ValueError:
        return None
    if joint_palette[0] == head_index:
        return None
    return [head_index]


def skeleton_is_assigned(path: Path, skeleton_source: str | None) -> bool:
    if not skeleton_source:
        return False
    if skeleton_source.startswith(f"{path}::"):
        return False
    own = companion(path, ".g4sk")
    return own is None or skeleton_source != str(own)


def remap_assigned_joint_palette(joint_palette: list[int], skeleton_info: dict | None) -> tuple[list[int], int]:
    if not joint_palette or skeleton_info is None:
        return joint_palette, 0
    target_indices = {name: index for index, name in enumerate(skeleton_info.get("names", []))}
    remapped = []
    changed = 0
    for joint_index in joint_palette:
        if joint_index >= len(ASSIGNED_SKELETON_JOINT_NAMES):
            remapped.append(joint_index)
            continue
        target_index = target_indices.get(ASSIGNED_SKELETON_JOINT_NAMES[joint_index])
        if target_index is None:
            remapped.append(joint_index)
            continue
        remapped.append(target_index)
        changed += target_index != joint_index
    return remapped, changed


def export_dae(path: Path, out_dir: Path, extract_textures: bool = True) -> Path:
    md_data, g4mg, _ = read_model_buffers(path)
    md_info = parse_g4md(md_data, g4mg)
    source_data = path.read_bytes()
    skeleton_data, skeleton_source = find_skeleton_for_model(path, source_data if source_data[:4] == b"G4PK" else None)
    if skeleton_data is not None:
        skeleton_info = parse_g4sk(skeleton_data)
    else:
        skeleton_info, skeleton_source = resolve_g4sk_info_from_cache(path)
        if skeleton_info is None:
            skeleton_data, skeleton_source = resolve_g4sk_from_chara_model(path)
            if skeleton_data is not None:
                skeleton_info = parse_g4sk(skeleton_data)
    assigned_skeleton = skeleton_is_assigned(path, skeleton_source)
    out_dir.mkdir(parents=True, exist_ok=True)
    dae_path = out_dir / f"{path.stem}.dae"

    texture_containers: list[Path] = []
    texture_paths: list[Path] = []
    if extract_textures:
        texture_containers = find_texture_containers_for_model(path)
        texture_containers.extend(
            candidate
            for candidate in find_shared_map_texture_containers(path, md_info.get("material_names", []))
            if candidate not in texture_containers
        )
        texture_containers.extend(
            candidate
            for candidate in find_shared_texture_containers_for_materials(path, md_info.get("material_names", []))
            if candidate not in texture_containers
        )
        if texture_containers:
            texture_paths = extract_texture_containers(texture_containers, out_dir / f"{path.stem}_textures")

    records = md_info["records"]
    index_base = md_info["index_buffer_base"]

    mesh_payloads = []
    used_materials: dict[str, Path | None] = {}
    prefer_material_refs = "/map/" in path.as_posix()
    for record in records:
        mesh_name = mesh_name_for_export(md_info, record, skeleton_info)
        material_name = material_name_for_mesh(md_info, mesh_name, record["index"], record, path.stem)
        if material_name not in used_materials:
            texture = None
            if prefer_material_refs:
                texture = choose_texture_from_material_record(md_info, material_name, texture_paths, path.stem)
            if texture is None:
                texture = choose_texture(material_name, texture_paths, path.stem, mesh_name)
            used_materials[material_name] = texture

        vertex_offset = record["vertex_offset"]
        vertex_count = record["vertex_count"]
        index_offset = record["index_offset"]
        index_count = record["index_count"]
        vertex_stride = vertex_stride_for_record(record)
        uv_offset = uv0_offset_for_record(md_info, record)

        positions: list[float] = []
        position_tuples: list[tuple[float, float, float]] = []
        texcoords: list[float] = []
        for vertex_index in range(vertex_count):
            off = vertex_offset + vertex_index * vertex_stride
            position = struct.unpack_from("<fff", g4mg, off)
            position_tuples.append(position)
            positions.extend(position)
            texcoords.extend(read_uv0(g4mg, md_info, record, vertex_index))

        indices = list(struct.unpack_from("<" + "H" * index_count, g4mg, index_base + index_offset))
        native_normals = read_native_normals_for_record(g4mg, md_info, record)
        if native_normals is None:
            normals_tuples = [(0.0, 1.0, 0.0)] * vertex_count
            normal_source = "fallback_flat_up"
        else:
            normals_tuples = native_normals
            normal_source = "native_snorm16_offset_0x0c"
        normals = [component for normal in normals_tuples for component in normal]
        joint_palette = joint_palette_for_record(md_info, record)
        palette_remap_count = 0
        if assigned_skeleton:
            joint_palette, palette_remap_count = remap_assigned_joint_palette(joint_palette, skeleton_info)
        joint_palette_override = face_rigid_joint_override(path, skeleton_info, joint_palette)
        if joint_palette_override is not None:
            joint_palette = joint_palette_override
        if len(joint_palette) == 1:
            skin_influences = rigid_skin_influences(vertex_count)
            skin_mode = "rigid_face_head_override" if joint_palette_override is not None else "rigid_single_palette"
        else:
            skin_influences = read_skin_influences(g4mg, md_info, record)
            skin_mode = "vertex_weights"
            if joint_palette:
                valid_influences, invalid_influences = skin_palette_fit(skin_influences, len(joint_palette))
                if invalid_influences > valid_influences:
                    skin_influences = rigid_skin_influences(vertex_count)
                    skin_mode = "rigid_invalid_palette_bytes"
        p: list[int] = []
        for index in indices:
            p.extend((index, index, index))

        mesh_payloads.append(
            {
                "id": f"mesh_{record['index']:03d}",
                "name": mesh_name,
                "material": material_name,
                "record_index": record["index"],
                "name_index": record["name_index"],
                "layout_index": record["layout_index"],
                "material_index": record["material_index"],
                "material_or_lod": record["material_or_lod"],
                "positions": positions,
                "normals": normals,
                "texcoords": texcoords,
                "indices": p,
                "vertex_count": vertex_count,
                "triangle_count": index_count // 3,
                "vertex_stride": vertex_stride,
                "uv0_offset": uv_offset,
                "normal_source": normal_source,
                "palette_base": record["palette_or_list"],
                "palette_length": mesh_palette_length(record),
                "joint_palette": joint_palette,
                "bind_shape_matrix": None,
                "assigned_skeleton_palette_remaps": palette_remap_count,
                "skin_mode": skin_mode,
                "skin_influences": skin_influences,
                "skin_summary": summarize_skin_influences(skin_influences),
            }
        )

    images = []
    effects = []
    materials = []
    for material_index, (material_name, texture) in enumerate(used_materials.items()):
        safe_mat = f"mat_{material_index:03d}"
        if texture is not None:
            rel = texture.relative_to(out_dir).as_posix()
            images.append(
                f'<image id="{safe_mat}_image" name="{escape(texture.stem)}">'
                f"<init_from>{escape(rel)}</init_from></image>"
            )
            effects.append(
                f'<effect id="{safe_mat}_effect"><profile_COMMON>'
                f'<newparam sid="{safe_mat}_surface"><surface type="2D">'
                f'<init_from>{safe_mat}_image</init_from></surface></newparam>'
                f'<newparam sid="{safe_mat}_sampler"><sampler2D>'
                f'<source>{safe_mat}_surface</source></sampler2D></newparam>'
                f'<technique sid="common"><phong><diffuse><texture texture="{safe_mat}_sampler" texcoord="UVSET0"/>'
                f'</diffuse></phong></technique></profile_COMMON></effect>'
            )
        else:
            color = "0.75 0.75 0.75 1"
            effects.append(
                f'<effect id="{safe_mat}_effect"><profile_COMMON><technique sid="common">'
                f"<phong><diffuse><color>{color}</color></diffuse></phong>"
                f"</technique></profile_COMMON></effect>"
            )
        materials.append(
            f'<material id="{safe_mat}" name="{escape(material_name)}">'
            f'<instance_effect url="#{safe_mat}_effect"/></material>'
        )

    material_id_by_name = {name: f"mat_{idx:03d}" for idx, name in enumerate(used_materials)}

    geometries = []
    controllers = []
    scene_nodes = []
    skin_reports: dict[str, dict] = {}
    for payload in mesh_payloads:
        gid = payload["id"]
        mat_id = material_id_by_name[payload["material"]]
        positions = payload["positions"]
        normals = payload["normals"]
        texcoords = payload["texcoords"]
        indices = payload["indices"]
        vcount = payload["vertex_count"]
        triangles = payload["triangle_count"]

        geometries.append(
            f'<geometry id="{gid}_geo" name="{escape(payload["name"])}"><mesh>'
            f'<source id="{gid}_positions"><float_array id="{gid}_positions_array" count="{len(positions)}">'
            f"{dae_float_array(positions)}</float_array><technique_common>"
            f'<accessor source="#{gid}_positions_array" count="{vcount}" stride="3">'
            f'<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
            f"</accessor></technique_common></source>"
            f'<source id="{gid}_texcoords"><float_array id="{gid}_texcoords_array" count="{len(texcoords)}">'
            f"{dae_float_array(texcoords)}</float_array><technique_common>"
            f'<accessor source="#{gid}_texcoords_array" count="{vcount}" stride="2">'
            f'<param name="S" type="float"/><param name="T" type="float"/>'
            f"</accessor></technique_common></source>"
            f'<source id="{gid}_normals"><float_array id="{gid}_normals_array" count="{len(normals)}">'
            f"{dae_float_array(normals)}</float_array><technique_common>"
            f'<accessor source="#{gid}_normals_array" count="{vcount}" stride="3">'
            f'<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
            f"</accessor></technique_common></source>"
            f'<vertices id="{gid}_vertices"><input semantic="POSITION" source="#{gid}_positions"/></vertices>'
            f'<triangles material="{mat_id}" count="{triangles}">'
            f'<input semantic="VERTEX" source="#{gid}_vertices" offset="0"/>'
            f'<input semantic="NORMAL" source="#{gid}_normals" offset="1"/>'
            f'<input semantic="TEXCOORD" source="#{gid}_texcoords" offset="2" set="0"/>'
            f"<p>{dae_int_array(indices)}</p></triangles>"
            f"</mesh></geometry>"
        )
        bind_material = (
            f'<bind_material><technique_common><instance_material symbol="{mat_id}" target="#{mat_id}">'
            f'<bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>'
            f"</instance_material></technique_common></bind_material>"
        )
        controller = build_skin_controller(payload, skeleton_info) if skeleton_info is not None else None
        if controller is None:
            scene_nodes.append(
                f'<node id="{gid}" name="{escape(payload["name"])}"><instance_geometry url="#{gid}_geo">'
                f"{bind_material}</instance_geometry></node>"
            )
        else:
            controller_xml, skin_report = controller
            controllers.append(controller_xml)
            skin_reports[payload["name"]] = skin_report
            scene_nodes.append(
                f'<node id="{gid}" name="{escape(payload["name"])}"><instance_controller url="#{gid}_controller">'
                f'<skeleton>#skeleton_root</skeleton>{bind_material}</instance_controller></node>'
            )

    skeleton_nodes = ""
    if skeleton_info is not None:
        skeleton_nodes = f'<node id="skeleton_root" name="skeleton_root">{render_joint_nodes(skeleton_info)}</node>'

    dae = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">'
        "<asset><contributor><authoring_tool>g4_model_probe.py</authoring_tool></contributor>"
        "<unit name=\"meter\" meter=\"1\"/><up_axis>Y_UP</up_axis></asset>"
        f"<library_images>{''.join(images)}</library_images>"
        f"<library_effects>{''.join(effects)}</library_effects>"
        f"<library_materials>{''.join(materials)}</library_materials>"
        f"<library_geometries>{''.join(geometries)}</library_geometries>"
        f"<library_controllers>{''.join(controllers)}</library_controllers>"
        f'<library_visual_scenes><visual_scene id="Scene" name="Scene">{skeleton_nodes}{"".join(scene_nodes)}</visual_scene></library_visual_scenes>'
        '<scene><instance_visual_scene url="#Scene"/></scene>'
        "</COLLADA>\n"
    )
    with dae_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(dae)

    report = {
        "source": str(path),
        "dae": str(dae_path),
        "texture_source": str(texture_containers[0]) if texture_containers else None,
        "texture_sources": [str(texture) for texture in texture_containers],
        "textures": [str(texture) for texture in texture_paths],
        "texture_variants": summarize_texture_variants(texture_paths),
        "g4md_texture_hashes": [
            {
                **row,
                "resolved_texture": (
                    str(texture_hashes_by_name(texture_paths).get(row["hash"]))
                    if texture_hashes_by_name(texture_paths).get(row["hash"]) is not None
                    else None
                ),
            }
            for row in md_info.get("texture_hashes", [])
        ],
        "material_records": enriched_material_records(md_info, texture_paths),
        "mesh_count": len(mesh_payloads),
        "materials": {name: str(texture) if texture else None for name, texture in used_materials.items()},
        "vertex_layout": {
            "stride": "per mesh; low byte of record flags1",
            "position": "float3 @ 0x00",
            "normal": "snorm16x4 @ 0x0c",
            "tangent_or_binormal_a": "snorm16x4 @ 0x14",
            "tangent_or_binormal_b": "snorm16x4 @ 0x1c",
            "blend_weights": "uint16[8] normalized @ 0x24 when stride >= 0x3c",
            "blend_indices": "uint8[8] local palette indices @ 0x34 when stride >= 0x3c",
            "uv0": "layout-declared attribute type 10; V is inverted for DAE/DDS texture origin",
        },
        "meshes": [
            {
                "name": payload["name"],
                "record_index": payload["record_index"],
                "name_index": payload["name_index"],
                "layout_index": payload["layout_index"],
                "material_index": payload["material_index"],
                "material_or_lod": payload["material_or_lod"],
                "material": payload["material"],
                "texture": str(used_materials.get(payload["material"])) if used_materials.get(payload["material"]) else None,
                "vertex_stride": f"0x{payload['vertex_stride']:x}",
                "uv0_offset": None if payload["uv0_offset"] is None else f"0x{payload['uv0_offset']:x}",
                "normal_source": payload["normal_source"],
                "palette_offset": payload["palette_base"],
                "palette_length": payload["palette_length"],
                "joint_palette": payload["joint_palette"],
                "assigned_skeleton_palette_remaps": payload["assigned_skeleton_palette_remaps"],
                "skin_mode": payload["skin_mode"],
                "skin": payload["skin_summary"],
                "controller": skin_reports.get(payload["name"]),
            }
            for payload in mesh_payloads
        ],
        "skeleton_source": skeleton_source,
        "assigned_skeleton": assigned_skeleton,
        "bone_orientation": skeleton_bone_orientation(skeleton_info),
        "skeleton": None
        if skeleton_info is None
        else {
            "joint_count": skeleton_info["joint_count"],
            "table_count": skeleton_info["table_count"],
            "section_offsets": [f"0x{offset:x}" for offset in skeleton_info["section_offsets"]],
            "joint_node_matrices": "local matrices derived from global bind block at 0x40 and parent table",
            "inverse_bind_matrices": "G4SK section 0",
            "local_srt": "G4SK section 1: scale.xyz, pad, rotation quaternion xyzw, translation.xyz, pad",
            "first_local_rotations_xyzw": skeleton_info.get("local_rotations_xyzw", [])[:32],
            "name_count": len(skeleton_info["names"]),
            "first_names": skeleton_info["names"][:32],
        },
        "rigging": "exported when G4SK is available; local blend indices are remapped through the G4MD joint palette table",
    }
    dae_path.with_suffix(".dae.report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return dae_path


def validate_dae(path: Path) -> dict:
    ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
    root = ET.parse(path).getroot()
    nonfinite_arrays = []
    for array in root.findall(".//c:float_array", ns):
        values = [float(value) for value in (array.text or "").split()]
        if not all(math.isfinite(value) for value in values):
            nonfinite_arrays.append(array.get("id"))
    return {"xml": "ok", "nonfinite_arrays": nonfinite_arrays}


def export_model_auto(value: Path, out_dir: Path) -> dict:
    path = resolve_model_input(value)
    dae_path = export_dae(path, out_dir, extract_textures=True)
    report_path = dae_path.with_suffix(".dae.report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validation = validate_dae(dae_path)
    materials = report.get("materials", {})
    hashes = report.get("g4md_texture_hashes", [])
    return {
        "input": str(value),
        "resolved_model": str(path),
        "dae": str(dae_path),
        "report": str(report_path),
        "mesh_count": report.get("mesh_count", 0),
        "texture_sources": report.get("texture_sources", []),
        "material_textures": {
            "resolved": sum(1 for texture in materials.values() if texture),
            "total": len(materials),
            "missing": [name for name, texture in materials.items() if not texture],
        },
        "texture_hashes": {
            "resolved": sum(1 for row in hashes if row.get("resolved_texture")),
            "total": len(hashes),
        },
        "validation": validation,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe Level-5 G4 model files.")
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of compact text.")
    parser.add_argument("--obj-dir", type=Path, help="Write probe OBJ files for geometry validation.")
    parser.add_argument("--dae-dir", type=Path, help="Write preliminary Collada DAE files.")
    parser.add_argument("--extract-g4tx", type=Path, help="Extract all textures from one G4TX into this directory.")
    parser.add_argument("--build-skeleton-cache", type=Path, help="Build local parsed G4SK cache JSON from chara_model XML.")
    parser.add_argument(
        "--export-model",
        action="store_true",
        help="Resolve model names/partial paths and export DAE with automatic texture extraction.",
    )
    args = parser.parse_args(argv)

    if args.build_skeleton_cache is not None:
        cache = build_skeleton_cache(args.build_skeleton_cache)
        print(
            f"Wrote {args.build_skeleton_cache}: "
            f"{len(cache.get('models', {}))} model mappings, "
            f"{len(cache.get('skeletons', {}))} skeletons"
        )
        return 0

    if not args.paths:
        parser.error("paths are required unless --build-skeleton-cache is used")

    if args.export_model:
        out_dir = args.dae_dir or Path("exports/dae")
        summaries = [export_model_auto(path, out_dir) for path in args.paths]
        if args.json:
            print(json.dumps(summaries, indent=2))
        else:
            for summary in summaries:
                material_textures = summary["material_textures"]
                texture_hashes = summary["texture_hashes"]
                validation = summary["validation"]
                print(f"== {summary['input']} ==")
                print(f"  model: {summary['resolved_model']}")
                print(f"  dae: {summary['dae']}")
                print(f"  report: {summary['report']}")
                print(f"  meshes: {summary['mesh_count']}")
                print(f"  texture sources: {len(summary['texture_sources'])}")
                print(
                    f"  materials: {material_textures['resolved']}/{material_textures['total']} "
                    f"textures resolved"
                )
                print(f"  hashes: {texture_hashes['resolved']}/{texture_hashes['total']} resolved")
                print(
                    f"  validation: xml={validation['xml']} "
                    f"nonfinite_arrays={len(validation['nonfinite_arrays'])}"
                )
                if material_textures["missing"]:
                    print("  missing materials:", ", ".join(material_textures["missing"][:16]))
        return 0

    if args.extract_g4tx is not None:
        for path in args.paths:
            written = extract_g4tx(path, args.extract_g4tx)
            print(f"Extracted {len(written)} textures from {path}")
        return 0

    results = [load_model(path) for path in args.paths]
    if args.obj_dir is not None:
        for path in args.paths:
            out_path = export_obj(path, args.obj_dir)
            print(f"Wrote {out_path}")
    if args.dae_dir is not None:
        for path in args.paths:
            out_path = export_dae(path, args.dae_dir)
            print(f"Wrote {out_path}")

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    for result in results:
        print(f"== {result['path']} ==")
        if "pack" in result:
            pack = result["pack"]
            print(f"G4PK files={pack['file_count']} type=0x{pack['file_type']:x}")
            for entry in pack["entries"]:
                print(
                    f"  [{entry['index']}] {entry['name']} off=0x{entry['offset']:x} "
                    f"size={entry['size']} magic={entry['magic']} crc={entry['crc32b']}"
                )
        if "g4md" not in result:
            print(f"  {result.get('error', 'no G4MD')}")
            continue
        md = result["g4md"]
        print(
            f"G4MD meshes={md['mesh_count']} materials={md['material_count']} "
            f"vbuf={md['vertex_buffer_size']} ibuf={md['index_buffer_size']} "
            f"mg={md['g4mg_size']} tail={md['g4mg_tail_size']}"
        )
        print("  tail names:", ", ".join(md["tail_names"][-16:]))
        for rec in md["records"]:
            print(
                f"  mesh {rec['index']}: vo=0x{rec['vertex_offset']:x} io=0x{rec['index_offset']:x} "
                f"v={rec['vertex_count']} i={rec['index_count']} tri={rec['triangle_count']} "
                f"mat={rec['material_or_lod']} nameIdx={rec['name_index']} pal={rec['palette_or_list']} "
                f"flags=0x{rec['flags0']:x}/0x{rec['flags1']:x} "
                f"ok=v{int(rec['vertex_range_ok'])}/i{int(rec['index_range_ok'])} "
                f"p0={rec['first_position']} idx={rec['first_indices'][:6]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
