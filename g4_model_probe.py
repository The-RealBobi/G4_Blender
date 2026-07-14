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

    candidates.extend(sorted(
        (RAW_DATA_ROOT / "common" / "gamedata" / "character").glob("chara_model*.xml")
    ))

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


def default_chara_parts_json() -> Path | None:
    env_path = os.environ.get("LEVEL5_G4_CHARA_PARTS")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())

    script_dir = Path(__file__).resolve().parent
    candidates.extend(sorted(script_dir.glob("chara_parts*.json")))
    candidates.extend(sorted((script_dir / "data").glob("chara_parts*.json")))
    candidates.extend(
        sorted((RAW_DATA_ROOT / "common" / "gamedata" / "character").glob("chara_parts*.json"))
    )

    if RAW_DATA_ROOT.parts[-2:] == ("raw", "data"):
        readable_root = RAW_DATA_ROOT.parents[1] / "readable" / "data"
        candidates.extend(
            sorted((readable_root / "common" / "gamedata" / "character").glob("chara_parts*.json"))
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
CHARA_MODEL_MAPS: tuple[dict[str, dict], dict[int, dict]] | None = None
CHARA_MODEL_LOOKUP = default_chara_model_lookup()
CHARA_MODEL_LOOKUP_DATA: dict | None = None
CHARA_PARTS_JSON = default_chara_parts_json()
CHARA_PARTS_DATA: dict | None = None
UNIFORM_FAMILY_LOOKUP: dict[str, dict] | None = None
UNIFORM_TEXTURE_LOOKUP: dict[str, list[tuple[int, str]]] | None = None
UNIFORM_FAMILY_SKELETON_CACHE: dict[str, tuple[bytes | None, str | None]] = {}
UNIFORM_CRC_SKELETON_CACHE: dict[str, tuple[bytes | None, str | None]] = {}
UNIFORM_CRC_SKELETON_CANDIDATES: list[tuple[bytes, str, set[int], int]] | None = None
G4SK_NAME_HASH_CACHE: dict[bytes, set[int]] = {}
SKELETON_CACHE = (
    Path(os.environ["LEVEL5_G4_SKELETON_CACHE"]).expanduser()
    if os.environ.get("LEVEL5_G4_SKELETON_CACHE")
    else None
)
TARGET_SKELETON_OVERRIDE = (
    Path(os.environ["LEVEL5_G4_TARGET_SKELETON"]).expanduser()
    if os.environ.get("LEVEL5_G4_TARGET_SKELETON")
    else None
)
TARGET_SKELETON_DATA: dict | None = None
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

# Uniform G4MD palettes omit optional targets from the full character target
# table.  These discontinuities are visible in the original fbx2g4 logs; the
# stored values therefore cannot always index ASSIGNED_SKELETON_JOINT_NAMES
# linearly across sleeve/accessory and lower-body boundaries.

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
    global RAW_DATA_ROOT, CHARA_MODEL_XML, CHARA_MODEL_MAPS, CHARA_PARTS_JSON, CHARA_PARTS_DATA, UNIFORM_FAMILY_LOOKUP, UNIFORM_TEXTURE_LOOKUP, UNIFORM_FAMILY_SKELETON_CACHE, UNIFORM_CRC_SKELETON_CACHE, UNIFORM_CRC_SKELETON_CANDIDATES
    if os.environ.get("LEVEL5_G4_RAW_ROOT"):
        return
    inferred = infer_raw_data_root(path)
    if inferred is not None:
        RAW_DATA_ROOT = inferred
        CHARA_MODEL_XML = default_chara_model_xml()
        CHARA_MODEL_MAPS = None
        CHARA_PARTS_JSON = None
        CHARA_PARTS_DATA = None
        UNIFORM_FAMILY_LOOKUP = None
        UNIFORM_TEXTURE_LOOKUP = None
        UNIFORM_FAMILY_SKELETON_CACHE.clear()
        UNIFORM_CRC_SKELETON_CACHE.clear()
        UNIFORM_CRC_SKELETON_CANDIDATES = None


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
    joint_hash_table = name_base_bias + u16(data, 0x74) * 4
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
        "joint_hash_table": joint_hash_table,
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


def remap_joint_palette_by_g4md_hashes(
    data: bytes,
    joint_palette: list[int],
    skeleton_info: dict | None,
    joint_hash_table_offset: int,
    allow_partial: bool = False,
) -> tuple[list[int] | None, int]:
    """Resolve G4MD palette slots through its embedded CRC32 joint table.

    G4MD stores the start of its CRC32 joint-name table at header offset
    ``0x74``. Local blend indices address that table from zero. Matching the
    hashes to the selected skeleton makes shared clothes and dynamic helpers
    independent of physical G4SK order.
    """
    if not joint_palette or skeleton_info is None:
        return None, 0
    names = skeleton_info.get("names") or []
    hash_to_index = {
        crc32b(name.encode("ascii")): index
        for index, name in enumerate(names)
        if name
    }
    if not hash_to_index:
        return None, 0

    if min(joint_palette) < 0 or joint_hash_table_offset <= 0:
        return None, 0
    last_offset = joint_hash_table_offset + max(joint_palette) * 4
    if last_offset + 4 > len(data):
        return None, 0
    hashes = [u32(data, joint_hash_table_offset + index * 4) for index in joint_palette]
    unresolved = [value for value in hashes if value not in hash_to_index]
    if unresolved and not allow_partial:
        return None, 0
    # A few character-exclusive clothes reference dynamic helper joints that
    # are absent from the actor's exported G4SK.  Do not reinterpret those
    # slots as physical indices: that attaches their weights to unrelated
    # limbs.  ``-1`` is intentionally ignored by the Blender mesh writer.
    remapped = [hash_to_index.get(value, -1) for value in hashes]
    if not any(index >= 0 for index in remapped):
        return None, 0
    return remapped, sum(before != after for before, after in zip(joint_palette, remapped))


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


def map_asset_name(node_name: str, model_stems: set[str]) -> str | None:
    """Resolve a map hierarchy node to the model asset it instances."""
    lowered = node_name.lower()
    if lowered in model_stems:
        return lowered
    match = re.fullmatch(r"(.+)_\d{2}_\d{3}", lowered)
    if match and match.group(1) in model_stems:
        return match.group(1)
    return None


def map_scene_placements(directory: Path, model_stems: Iterable[str]) -> dict[str, list[dict]]:
    """Read static asset placements from the world's G4SK scene hierarchy."""
    stems = {stem.lower() for stem in model_stems}
    scene_path = directory / f"{directory.name}.g4pk"
    if not scene_path.is_file() or not stems:
        return {}

    data = scene_path.read_bytes()
    if data[:4] != b"G4PK":
        return {}
    pack = parse_g4pk(data)
    entry = next((item for item in pack["entries"] if item["magic"] == "G4SK"), None)
    if entry is None:
        return {}
    skeleton = parse_g4sk(data[entry["offset"] : entry["offset"] + entry["size"]])
    names = skeleton["names"]
    parents = skeleton["parent_indices"]
    local_matrices = skeleton.get("local_srt_matrices") or skeleton["local_matrices"]
    joint_count = min(len(names), len(local_matrices))

    world_matrices: list[list[float] | None] = [None] * joint_count

    def world_matrix(index: int, visiting: set[int] | None = None) -> list[float]:
        cached = world_matrices[index]
        if cached is not None:
            return cached
        visiting = set() if visiting is None else visiting
        if index in visiting:
            return local_matrices[index]
        visiting.add(index)
        parent = parents[index] if index < len(parents) else joint_count
        if 0 <= parent < joint_count and parent != index:
            result = matrix_mul(world_matrix(parent, visiting), local_matrices[index])
        else:
            result = local_matrices[index]
        world_matrices[index] = result
        visiting.remove(index)
        return result

    def belongs_to_render_hierarchy(index: int) -> bool:
        while 0 <= index < joint_count:
            name = names[index].lower()
            if name == "instance" or name.startswith("model_r_"):
                return True
            if name.startswith("model_a_") or name.startswith("model_i_"):
                return False
            parent = parents[index] if index < len(parents) else joint_count
            if parent == index:
                break
            index = parent
        return False

    placements: dict[str, list[dict]] = {}
    for index, node_name in enumerate(names[:joint_count]):
        asset_name = map_asset_name(node_name, stems)
        if asset_name is None or not belongs_to_render_hierarchy(index):
            continue
        placements.setdefault(asset_name, []).append(
            {"node_index": index, "node_name": node_name, "matrix": world_matrix(index)}
        )
    return placements


def normalize_skeleton_info(info: dict | None) -> dict | None:
    if not isinstance(info, dict):
        return None
    names = list(info.get("names") or [])
    joint_count = int(info.get("joint_count") or len(names))
    if joint_count <= 0 or len(names) != joint_count:
        return None
    parents = list(info.get("parent_indices") or [])
    if len(parents) < joint_count:
        parents.extend([joint_count] * (joint_count - len(parents)))
    bind_matrices = list(info.get("bind_matrices") or [])
    if len(bind_matrices) != joint_count:
        return None
    inverse_bind_matrices = list(info.get("inverse_bind_matrices") or [])
    if len(inverse_bind_matrices) != joint_count:
        inverse_bind_matrices = [rigid_matrix_inverse(matrix) for matrix in bind_matrices]
    local_matrices = list(info.get("local_matrices") or [])
    if len(local_matrices) != joint_count:
        local_matrices = []
        for joint_index, matrix in enumerate(bind_matrices):
            parent = parents[joint_index] if joint_index < len(parents) else joint_count
            if parent < len(bind_matrices) and parent != joint_index:
                local_matrices.append(matrix_mul(rigid_matrix_inverse(bind_matrices[parent]), matrix))
            else:
                local_matrices.append(matrix)
    normalized = dict(info)
    normalized["joint_count"] = joint_count
    normalized["names"] = names
    normalized["parent_indices"] = parents[:joint_count]
    normalized["bind_matrices"] = bind_matrices[:joint_count]
    normalized["inverse_bind_matrices"] = inverse_bind_matrices[:joint_count]
    normalized["local_matrices"] = local_matrices[:joint_count]
    return normalized


def load_target_skeleton_override() -> dict | None:
    global TARGET_SKELETON_DATA
    if TARGET_SKELETON_DATA is not None:
        return TARGET_SKELETON_DATA
    if TARGET_SKELETON_OVERRIDE is None or not TARGET_SKELETON_OVERRIDE.exists():
        TARGET_SKELETON_DATA = {}
        return None
    try:
        data = json.loads(TARGET_SKELETON_OVERRIDE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        TARGET_SKELETON_DATA = {}
        return None
    TARGET_SKELETON_DATA = normalize_skeleton_info(data) or {}
    return TARGET_SKELETON_DATA or None


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
    global CHARA_MODEL_MAPS
    if CHARA_MODEL_MAPS is not None:
        return CHARA_MODEL_MAPS
    if not CHARA_MODEL_XML.exists():
        CHARA_MODEL_MAPS = ({}, {})
        return CHARA_MODEL_MAPS
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
    CHARA_MODEL_MAPS = (models, bodies)
    return CHARA_MODEL_MAPS


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


def load_chara_parts_data() -> dict:
    global CHARA_PARTS_JSON, CHARA_PARTS_DATA
    resolved_path = CHARA_PARTS_JSON if CHARA_PARTS_JSON and CHARA_PARTS_JSON.exists() else default_chara_parts_json()
    if resolved_path != CHARA_PARTS_JSON:
        CHARA_PARTS_JSON = resolved_path
        CHARA_PARTS_DATA = None
    if CHARA_PARTS_DATA is not None:
        return CHARA_PARTS_DATA
    if CHARA_PARTS_JSON is None or not CHARA_PARTS_JSON.exists():
        CHARA_PARTS_DATA = {}
        return {}
    try:
        CHARA_PARTS_DATA = json.loads(CHARA_PARTS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        CHARA_PARTS_DATA = {}
    return CHARA_PARTS_DATA


def load_uniform_texture_lookup() -> dict[str, list[tuple[int, str]]]:
    """Map uniform models to the G4TX containers declared by chara_parts.

    The G4MD material names only carry the texture-set CRC32 (for example,
    ``u031001_20``).  The authoritative path for that set is kept in the
    matching CHARA_PARTS_CLOTHES_INFO row, and can belong to a different
    uniform family than the model itself.
    """
    global UNIFORM_TEXTURE_LOOKUP
    if UNIFORM_TEXTURE_LOOKUP is not None:
        return UNIFORM_TEXTURE_LOOKUP

    lookup: dict[str, list[tuple[int, str]]] = {}
    for entry in load_chara_parts_data().get("Entries") or []:
        if entry.get("Name") != "CHARA_PARTS_CLOTHES_INFO":
            continue
        values = entry.get("Values") or []
        if len(values) < 2:
            continue
        model_path = str(values[0].get("Value") or "").replace("\\", "/").lower()
        texture_path = str(values[1].get("Value") or "").replace("\\", "/")
        if not model_path.startswith("_uniform/") or not texture_path.lower().endswith(".g4tx"):
            continue
        texture_crc = 0
        if len(values) > 5:
            try:
                texture_crc = int(values[5].get("Value") or 0) & 0xFFFFFFFF
            except (TypeError, ValueError):
                pass
        lookup.setdefault(model_path, []).append((texture_crc, texture_path))

    UNIFORM_TEXTURE_LOOKUP = lookup
    return lookup


def find_uniform_texture_containers_from_chara(
    path: Path,
    material_names: Iterable[str],
) -> list[Path]:
    """Find shared uniform textures by the model/material CRC32 metadata."""
    try:
        relative = path.relative_to(RAW_DATA_ROOT).as_posix().lower()
    except ValueError:
        return []
    prefix = "common/chr/"
    if not relative.startswith(prefix):
        return []

    texture_sets = set()
    for material_name in material_names:
        name = material_name[:-1] if material_name.endswith("M") else material_name
        texture_sets.add(crc32b(name.encode("ascii", errors="ignore")))

    model_path = relative[len(prefix) :]
    declared = load_uniform_texture_lookup().get(model_path) or []
    selected = [
        texture_path
        for texture_crc, texture_path in declared
        if texture_crc and texture_crc in texture_sets
    ]
    if not texture_sets:
        selected.extend(texture_path for _, texture_path in declared)

    candidates = [RAW_DATA_ROOT / "dx11" / "chr" / texture_path for texture_path in selected]
    return list(dict.fromkeys(candidate for candidate in candidates if candidate.is_file()))


def load_uniform_family_lookup() -> dict[str, dict]:
    global UNIFORM_FAMILY_LOOKUP
    if UNIFORM_FAMILY_LOOKUP is not None:
        return UNIFORM_FAMILY_LOOKUP

    data = load_chara_parts_data()
    entries = data.get("Entries") or []
    lookup: dict[str, dict] = {}
    for index, entry in enumerate(entries):
        if entry.get("Name") != "CHARA_PARTS_CLOTHES_MODEL":
            continue
        values = entry.get("Values") or []
        if len(values) < 2:
            continue
        stem = str(values[1].get("Value") or "").strip()
        if not stem:
            continue
        ref_entry = entries[index + 1] if index + 1 < len(entries) else None
        if ref_entry is None or ref_entry.get("Name") != "CHARA_PARTS_CLOTHES_MODEL_REF_INFO":
            continue
        ref_values = ref_entry.get("Values") or []
        if not ref_values:
            continue
        info_index = int(ref_values[0].get("Value") or 0)
        if info_index <= 0 or info_index >= len(entries):
            continue
        info_entry = entries[info_index]
        if info_entry.get("Name") != "CHARA_PARTS_CLOTHES_INFO":
            continue
        info_values = info_entry.get("Values") or []
        family_model = str(info_values[0].get("Value") or "").replace("\\", "/") if len(info_values) > 0 else ""
        family_type = int(info_values[2].get("Value") or 0) if len(info_values) > 2 else 0
        family_skeleton_model = str(info_values[6].get("Value") or "").replace("\\", "/") if len(info_values) > 6 else ""
        lookup[stem] = {
            "family_info_index": info_index,
            "family_model": family_model,
            "family_type": family_type,
            "family_skeleton_model": family_skeleton_model,
        }

    UNIFORM_FAMILY_LOOKUP = lookup
    return lookup


def uniform_family_probe_model(path: Path) -> Path | None:
    family = load_uniform_family_lookup().get(path.stem)
    if not family:
        return None
    rel = family.get("family_skeleton_model") or family.get("family_model") or ""
    if not rel:
        return None
    candidate = RAW_DATA_ROOT / "common" / "chr" / rel
    return candidate if candidate.is_file() else None


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
                "body_profile": body_info.get(4, ""),
                "body_mesh_profile": body_info.get(6, ""),
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
    models, bodies = load_chara_model_maps()
    if models:
        rel_candidates = model_relpath_candidates(path)
        model_info = None
        model_key = None
        for candidate in rel_candidates:
            if candidate in models:
                model_info = models[candidate]
                model_key = candidate
                break
        if model_info is not None:
            try:
                body_info = bodies.get(int(model_info.get(4, "0")))
            except ValueError:
                body_info = None
            if body_info is not None:
                candidates = skeleton_candidates_from_body_info(body_info, include_crc=False)
                if not any(candidate.exists() for candidate in candidates):
                    candidates = skeleton_candidates_from_body_info(body_info, include_crc=True)
                seen = set()
                for candidate in candidates:
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    if candidate.exists():
                        return candidate.read_bytes(), f"{candidate} via {CHARA_MODEL_XML} ({model_key})"

    # The shipped lookup remains useful for old extractions where no readable
    # config is present, but must not override the active dump's CFG.
    model_row, model_key = lookup_model_row(path)
    if model_row is not None:
        body_row = lookup_body_row(model_row)
        candidates = skeleton_candidates_from_lookup_row(model_row)
        candidates.extend(skeleton_candidates_from_lookup_row(body_row))
        for candidate in candidates:
            if candidate.exists():
                return candidate.read_bytes(), f"{candidate} via {CHARA_MODEL_LOOKUP} ({model_key})"
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


def g4sk_entries_from_candidate(path: Path) -> Iterable[tuple[bytes, str]]:
    try:
        data = path.read_bytes()
    except OSError:
        return
    if data[:4] == b"G4SK":
        yield data, str(path)
        return
    if data[:4] != b"G4PK":
        return
    try:
        pack = parse_g4pk(data)
    except (ValueError, struct.error):
        return
    for entry in pack["entries"]:
        if entry["magic"] == "G4SK":
            start = entry["offset"]
            yield data[start : start + entry["size"]], f"{path}::{entry['name']}"


def uniform_used_joint_hashes(path: Path) -> set[int]:
    try:
        md_info = parse_g4md(path.read_bytes())
    except (OSError, ValueError, struct.error):
        return set()
    joint_table = md_info.get("joint_hash_table", 0)
    if joint_table <= 0:
        return set()
    indices = {
        index
        for record in md_info.get("records", [])
        for index in joint_palette_for_record(md_info, record)
    }
    if not indices or joint_table + max(indices) * 4 + 4 > path.stat().st_size:
        return set()
    data = path.read_bytes()
    return {u32(data, joint_table + index * 4) for index in indices}


def uniform_crc_skeleton_candidates() -> list[tuple[bytes, str, set[int], int]]:
    global UNIFORM_CRC_SKELETON_CANDIDATES
    if UNIFORM_CRC_SKELETON_CANDIDATES is not None:
        return UNIFORM_CRC_SKELETON_CANDIDATES

    candidates: list[tuple[bytes, str, set[int], int]] = []
    face_root = RAW_DATA_ROOT / "common" / "chr" / "_face"
    if face_root.is_dir():
        paths = list(face_root.rglob("*.g4sk")) + list(face_root.rglob("*.g4pkm"))
        for path in paths:
            for skeleton_data, source in g4sk_entries_from_candidate(path):
                try:
                    names = parse_g4sk(skeleton_data).get("names", [])
                except (ValueError, struct.error):
                    continue
                hashes = {crc32b(name.encode("ascii")) for name in names if name}
                if hashes:
                    candidates.append((skeleton_data, source, hashes, len(names)))
    UNIFORM_CRC_SKELETON_CANDIDATES = candidates
    return candidates


def resolve_uniform_generic_g4sk(
    path: Path,
    allow_common_fallback: bool = True,
) -> tuple[bytes | None, str | None]:
    try:
        rel = path.relative_to(RAW_DATA_ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()
    if "/_uniform/" not in f"/{rel}":
        return None, None

    match = re.fullmatch(r"[us](\d{6,8})", path.stem, re.IGNORECASE)
    if match is None and not allow_common_fallback:
        return None, None
    character_root = RAW_DATA_ROOT / "common" / "chr"
    used_hashes = uniform_used_joint_hashes(path) if match is not None else set()
    if used_hashes:
        character_stem = f"c{match.group(1)}"
        direct = character_root / character_stem
        candidates = [direct / f"{character_stem}.g4sk", direct / f"{character_stem}.g4pkm"]
        for candidate in dict.fromkeys(candidates):
            for skeleton_data, source in g4sk_entries_from_candidate(candidate):
                names = parse_g4sk(skeleton_data).get("names", [])
                hashes = {crc32b(name.encode("ascii")) for name in names if name}
                if used_hashes <= hashes:
                    return skeleton_data, f"{source} via matching uniform ID"
        marker = f"/{character_stem}/{character_stem}."
        for skeleton_data, source, hashes, _joint_count in uniform_crc_skeleton_candidates():
            if marker in source and used_hashes <= hashes:
                return skeleton_data, f"{source} via matching uniform ID"

    # Some story uniforms share the body of a neighbouring character model,
    # so their numeric ID has no matching C-model.  Their G4MD palette still
    # declares every required joint by CRC32.  Search only face-character
    # packs and require complete coverage; this avoids spatial guesses and
    # finds their embedded, dynamic G4SK when one is available.
    cache_key = str(path)
    if used_hashes and cache_key in UNIFORM_CRC_SKELETON_CACHE:
        resolved = UNIFORM_CRC_SKELETON_CACHE[cache_key]
        if resolved[0] is not None:
            return resolved
    if used_hashes:
        matches: list[tuple[int, str, bytes, str]] = []
        for skeleton_data, source, hashes, joint_count in uniform_crc_skeleton_candidates():
            if used_hashes <= hashes:
                matches.append((joint_count, source, skeleton_data, source))
        if matches:
            _, _, skeleton_data, source = min(matches, key=lambda item: (item[0], item[1]))
            resolved = (skeleton_data, f"{source} via complete G4MD CRC32 palette")
            UNIFORM_CRC_SKELETON_CACHE[cache_key] = resolved
            return resolved
        UNIFORM_CRC_SKELETON_CACHE[cache_key] = (None, None)

    if not allow_common_fallback:
        return None, None
    # Uniforms without an exact character skeleton use the common body rig.
    # Its raw palette indices follow c000101; similarly sized edit skeletons
    # have different helper-bone insertion points and silently misbind limbs.
    for stem in ("c000101", "c000201", "c000301", "c000401"):
        candidate = RAW_DATA_ROOT / "common" / "chr" / stem / f"{stem}.g4sk"
        if candidate.exists():
            return candidate.read_bytes(), f"{candidate} via _uniform generic fallback"
    return None, None


def g4sk_covers_g4md_palette(skeleton_data: bytes, path: Path) -> bool:
    """Return whether a skeleton names every joint referenced by a G4MD mesh."""
    used_hashes = uniform_used_joint_hashes(path)
    if not used_hashes:
        return False
    skeleton_hashes = G4SK_NAME_HASH_CACHE.get(skeleton_data)
    if skeleton_hashes is None:
        try:
            names = parse_g4sk(skeleton_data).get("names", [])
        except (ValueError, struct.error):
            return False
        skeleton_hashes = {crc32b(name.encode("ascii")) for name in names if name}
        G4SK_NAME_HASH_CACHE[skeleton_data] = skeleton_hashes
    return used_hashes <= skeleton_hashes


def g4sk_matches_or_unskinned_g4md(skeleton_data: bytes, path: Path) -> bool:
    """Allow a configured rig for static meshes, otherwise require coverage."""
    return not uniform_used_joint_hashes(path) or g4sk_covers_g4md_palette(skeleton_data, path)


def resolve_unique_face_crc_g4sk(path: Path) -> tuple[bytes | None, str | None]:
    """Resolve an unlisted face mesh only when its CRC palette has one rig."""
    used_hashes = uniform_used_joint_hashes(path)
    if not used_hashes:
        return None, None
    matches = [
        (skeleton_data, source)
        for skeleton_data, source, hashes, _joint_count in uniform_crc_skeleton_candidates()
        if used_hashes <= hashes
    ]
    if len(matches) != 1:
        return None, None
    skeleton_data, source = matches[0]
    return skeleton_data, f"{source} via unique complete face CRC32 palette"


def find_skeleton_for_model(path: Path, pack_data: bytes | None = None) -> tuple[bytes | None, str | None]:
    palette_path = companion(path, ".g4md") if pack_data is not None else path
    if palette_path is None:
        palette_path = path

    if pack_data is not None and pack_data[:4] == b"G4PK":
        pack = parse_g4pk(pack_data)
        for entry in pack["entries"]:
            if entry["magic"] == "G4SK":
                start = entry["offset"]
                end = start + entry["size"]
                skeleton_data = pack_data[start:end]
                if g4sk_matches_or_unskinned_g4md(skeleton_data, palette_path):
                    return skeleton_data, f"{path}::{entry['name']}"

    own = companion(path, ".g4sk")
    if own is not None:
        skeleton_data = own.read_bytes()
        if g4sk_matches_or_unskinned_g4md(skeleton_data, palette_path):
            return skeleton_data, str(own)

    # Character G4MD files are sometimes unpacked beside their original
    # G4PKM.  The pack keeps the character-specific (and often dynamic) G4SK;
    # prefer it to a generic config rig, but only after exact palette coverage
    # proves that it is the sibling mesh's skeleton.
    own_pack = companion(path, ".g4pkm")
    if own_pack is not None:
        for skeleton_data, source in g4sk_entries_from_candidate(own_pack):
            if g4sk_covers_g4md_palette(skeleton_data, palette_path):
                return skeleton_data, f"{source} via sibling character pack"

    # Older face assets may store a secondary G4MD (hair, brows, etc.) beside
    # the character's primary G4MD without a companion G4SK of their own.
    # The primary mesh is the authoritative rig anchor; reuse it only when its
    # skeleton covers every CRC32 palette entry used by this secondary mesh.
    primary = path.parent / f"{path.parent.name}.g4md"
    if primary != path and primary.is_file():
        primary_data, primary_source = find_skeleton_for_model(primary)
        if primary_data is not None and g4sk_covers_g4md_palette(primary_data, path):
            return primary_data, f"{primary_source} via sibling primary mesh"

    try:
        is_face_asset = "_face" in path.relative_to(RAW_DATA_ROOT).parts
    except ValueError:
        is_face_asset = False
    # Some face variants differ only in their final model digit and share the
    # preceding variant's rig.  Keep the lookup local to that character group
    # and still require the complete CRC32 palette to match.
    if is_face_asset and path.stem.endswith("1"):
        base_stem = f"{path.stem[:-1]}0"
        base_dir = path.parent.parent / base_stem
        for candidate in (base_dir / f"{base_stem}.g4sk", base_dir / f"{base_stem}.g4pkm"):
            for skeleton_data, source in g4sk_entries_from_candidate(candidate):
                if g4sk_covers_g4md_palette(skeleton_data, palette_path):
                    return skeleton_data, f"{source} via local face variant rig"

    skeleton_data, skeleton_source = resolve_uniform_generic_g4sk(palette_path, allow_common_fallback=False)
    if skeleton_data is not None:
        return skeleton_data, skeleton_source
    skeleton_data, skeleton_source = resolve_g4sk_from_chara_model(palette_path)
    if skeleton_data is not None and (
        not is_face_asset or g4sk_matches_or_unskinned_g4md(skeleton_data, palette_path)
    ):
        return skeleton_data, skeleton_source
    skeleton_data, skeleton_source = resolve_uniform_family_g4sk(palette_path)
    if skeleton_data is not None and (
        not is_face_asset or g4sk_matches_or_unskinned_g4md(skeleton_data, palette_path)
    ):
        return skeleton_data, skeleton_source

    # A small number of legacy face-only assets have no character row or
    # companion rig.  They are hair meshes authored against the shared hair
    # skeleton, not against an arbitrary character body skeleton.
    hair_skeleton = (
        RAW_DATA_ROOT / "common" / "chr" / "_face" / "20_EDIT" / "_hairSK" / "_hairSK.g4sk"
    )
    if is_face_asset and hair_skeleton.is_file():
        hair_data = hair_skeleton.read_bytes()
        if g4sk_covers_g4md_palette(hair_data, palette_path):
            return hair_data, f"{hair_skeleton} via shared face hair rig"
    if is_face_asset:
        skeleton_data, skeleton_source = resolve_unique_face_crc_g4sk(palette_path)
        if skeleton_data is not None:
            return skeleton_data, skeleton_source
    return resolve_uniform_generic_g4sk(palette_path)



def find_texture_for_model(path: Path) -> Path | None:
    stem = path.stem
    candidates = []
    try:
        rel = path.relative_to(RAW_DATA_ROOT)
    except ValueError:
        rel = None

    if rel is not None:
        parts = list(rel.parts)
        if parts and parts[0] in {"common", "dx11", "nx"}:
            for root in ("dx11", "nx"):
                alt = RAW_DATA_ROOT.joinpath(root, *parts[1:]).with_suffix(".g4tx")
                candidates.append(alt)

    xml_texture = resolve_texture_from_chara_model(path)
    if xml_texture is not None:
        candidates.append(xml_texture)
    candidates.append(path.with_suffix(".g4tx"))

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
    for root in (RAW_DATA_ROOT / "dx11" / "chr", RAW_DATA_ROOT / "nx" / "chr"):
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
            world = parts[3]
            candidates.append(RAW_DATA_ROOT / "dx11" / "map" / "cubemap" / f"{world}_cubemap.g4tx")

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
            for platform in ("dx11", "nx"):
                candidates.append(RAW_DATA_ROOT / platform / "map" / map_kind / folder / f"{folder}.g4tx")
                candidates.append(RAW_DATA_ROOT / platform / "map" / map_kind / folder / f"{stem}.g4tx")

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
        for platform in ("dx11", "nx"):
            candidates.append(RAW_DATA_ROOT / platform / "chr" / stem / f"{stem}.g4tx")
            mirror_dir = RAW_DATA_ROOT / platform / Path(*rel.parts[1:-1])
            if mirror_dir.is_dir():
                candidates.extend(sorted(mirror_dir.glob(f"{stem}*.g4tx")))

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
            cubemap = dds_half_float_cubemap(payload)
            if cubemap is not None:
                ext = ".hdr"
                out_data = cubemap_to_radiance_hdr(payload, *cubemap)
            else:
                ext = ".dds"
                out_data = payload
        elif payload.startswith(b"NXTCH000"):
            try:
                ext = ".dds"
                out_data = nxtch_to_dds(payload)
            except ValueError:
                ext = ".nxtch"
                out_data = payload
        else:
            ext = ".bin"
            out_data = payload
        out_path = out_dir / f"{name}{ext}"
        out_path.write_bytes(out_data)
        written.append(out_path)
    return written


NXTCH_FORMATS = {
    0x25: (32, 4, None),
    0x42: (4, 8, b"DXT1"),
    0x44: (8, 16, b"DXT5"),
    0x4D: (8, 16, b"DX10"),
}


def nx_bitfield(bit_depth: int, block_compressed: bool, height: int, extension_count: int) -> list[tuple[int, int]]:
    if block_compressed:
        fields = {
            4: [(1, 0), (2, 0), (0, 1), (0, 2), (4, 0), (0, 4), (8, 0), (0, 8), (0, 16), (16, 0)],
            8: [(1, 0), (2, 0), (0, 1), (0, 2), (0, 4), (4, 0), (0, 8), (0, 16), (8, 0)],
        }.get(bit_depth)
        start_y, max_size = 32, 512
    else:
        fields = {
            8: [(1, 0), (2, 0), (4, 0), (8, 0), (0, 1), (16, 0), (0, 2), (0, 4), (32, 0)],
            16: [(1, 0), (2, 0), (4, 0), (0, 1), (8, 0), (0, 2), (0, 4), (16, 0)],
            32: [(1, 0), (2, 0), (0, 1), (4, 0), (0, 2), (0, 4), (8, 0)],
        }.get(bit_depth)
        start_y, max_size = 8, 128
    if fields is None:
        raise ValueError(f"unsupported NXTCH bit depth: {bit_depth}")
    fields = list(fields)
    if extension_count < 0:
        while start_y < min(height, max_size):
            fields.append((0, start_y))
            start_y *= 2
    else:
        for _ in range(extension_count):
            if start_y >= min(height, max_size):
                break
            fields.append((0, start_y))
            start_y *= 2
    return fields


def nx_swizzle_dimensions(width: int, height: int, fields: list[tuple[int, int]]) -> tuple[int, int, int, int]:
    macro_width = 0
    macro_height = 0
    for x_value, y_value in fields:
        macro_width |= x_value
        macro_height |= y_value
    macro_width += 1
    macro_height += 1
    padded_width = ((width + macro_width - 1) // macro_width) * macro_width
    padded_height = ((height + macro_height - 1) // macro_height) * macro_height
    return padded_width, padded_height, macro_width, macro_height


def nx_swizzle_point(index: int, width: int, fields: list[tuple[int, int]]) -> tuple[int, int]:
    macro_width = 0
    macro_height = 0
    for x_value, y_value in fields:
        macro_width |= x_value
        macro_height |= y_value
    macro_width += 1
    macro_height += 1
    points_per_macro = macro_width * macro_height
    width_in_tiles = (width + macro_width - 1) // macro_width
    macro_index = index // points_per_macro
    x = (macro_index % width_in_tiles) * macro_width
    y = (macro_index // width_in_tiles) * macro_height
    for bit, (x_value, y_value) in enumerate(fields):
        if index >> bit & 1:
            x ^= x_value
            y ^= y_value
    return x, y


def deswizzle_nxtch_level(data: bytes, width: int, height: int, bit_depth: int, unit_size: int, extension_count: int, block_compressed: bool) -> bytes:
    fields = nx_bitfield(bit_depth, block_compressed, height, extension_count)
    padded_width, padded_height, _, _ = nx_swizzle_dimensions(width, height, fields)
    if block_compressed:
        block_width = (width + 3) // 4
        block_height = (height + 3) // 4
        padded_blocks = ((padded_width + 3) // 4) * ((padded_height + 3) // 4)
        output = bytearray(block_width * block_height * unit_size)
        for source_block in range(min(len(data) // unit_size, padded_blocks)):
            x, y = nx_swizzle_point(source_block * 16, padded_width, fields)
            target_x, target_y = x // 4, y // 4
            if target_x >= block_width or target_y >= block_height:
                continue
            target = (target_y * block_width + target_x) * unit_size
            source = source_block * unit_size
            output[target : target + unit_size] = data[source : source + unit_size]
        return bytes(output)
    output = bytearray(width * height * unit_size)
    for source_index in range(min(len(data) // unit_size, padded_width * padded_height)):
        x, y = nx_swizzle_point(source_index, padded_width, fields)
        if x >= width or y >= height:
            continue
        target = (y * width + x) * unit_size
        source = source_index * unit_size
        output[target : target + unit_size] = data[source : source + unit_size]
    return bytes(output)


def swizzle_nxtch_level(data: bytes, width: int, height: int, bit_depth: int, unit_size: int, extension_count: int, block_compressed: bool) -> bytes:
    fields = nx_bitfield(bit_depth, block_compressed, height, extension_count)
    padded_width, padded_height, _, _ = nx_swizzle_dimensions(width, height, fields)
    if block_compressed:
        block_width = (width + 3) // 4
        block_height = (height + 3) // 4
        padded_block_width = (padded_width + 3) // 4
        padded_block_height = (padded_height + 3) // 4
        output = bytearray(padded_block_width * padded_block_height * unit_size)
        for source_block in range(padded_block_width * padded_block_height):
            x, y = nx_swizzle_point(source_block * 16, padded_width, fields)
            target_x, target_y = x // 4, y // 4
            if target_x >= block_width or target_y >= block_height:
                continue
            source = (target_y * block_width + target_x) * unit_size
            target = source_block * unit_size
            output[target : target + unit_size] = data[source : source + unit_size]
        return bytes(output)
    output = bytearray(padded_width * padded_height * unit_size)
    for source_index in range(padded_width * padded_height):
        x, y = nx_swizzle_point(source_index, padded_width, fields)
        if x >= width or y >= height:
            continue
        source = (y * width + x) * unit_size
        target = source_index * unit_size
        output[target : target + unit_size] = data[source : source + unit_size]
    return bytes(output)


def dds_header(width: int, height: int, mip_count: int, format_code: int, data_size: int) -> bytes:
    bit_depth, _, fourcc = NXTCH_FORMATS[format_code]
    flags = 0x0002100F if mip_count > 1 else 0x0000100F
    pitch = data_size if format_code == 0x25 else max(1, (width + 3) // 4) * NXTCH_FORMATS[format_code][1]
    header = bytearray(128 + (20 if format_code == 0x4D else 0))
    struct.pack_into("<4s7I", header, 0, b"DDS ", 124, flags, height, width, pitch, 0, mip_count)
    struct.pack_into("<I", header, 76, 32)
    if fourcc is not None:
        struct.pack_into("<I4s", header, 80, 0x4, fourcc)
    else:
        struct.pack_into("<I4sI4I", header, 80, 0x41, b"\0\0\0\0", 32, 0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
    caps = 0x401008 if mip_count > 1 else 0x1000
    struct.pack_into("<I", header, 108, caps)
    if format_code == 0x4D:
        struct.pack_into("<5I", header, 128, 98, 3, 0, 1, 0)
    return bytes(header)


def nxtch_to_dds(payload: bytes) -> bytes:
    if len(payload) < 0x100 or payload[:8] != b"NXTCH000":
        raise ValueError("not an NXTCH texture")
    width, height = struct.unpack_from("<II", payload, 0x14)
    format_code, mip_count = struct.unpack_from("<II", payload, 0x24)
    if format_code not in NXTCH_FORMATS or width <= 0 or height <= 0 or mip_count <= 0:
        raise ValueError(f"unsupported NXTCH format: 0x{format_code:x}")
    offsets = list(struct.unpack_from(f"<{mip_count}I", payload, 0x30))
    extension_count = struct.unpack_from("<i", payload, 0x74)[0]
    bit_depth, unit_size, _ = NXTCH_FORMATS[format_code]
    block_compressed = format_code != 0x25
    levels = []
    for level, offset in enumerate(offsets):
        start = 0x100 + offset
        end = 0x100 + (offsets[level + 1] if level + 1 < len(offsets) else len(payload) - 0x100)
        level_width = max(1, width >> level)
        level_height = max(1, height >> level)
        levels.append(deswizzle_nxtch_level(
            payload[start:end], level_width, level_height, bit_depth, unit_size,
            extension_count, block_compressed,
        ))
    return dds_header(width, height, mip_count, format_code, len(levels[0])) + b"".join(levels)


def dds_format(data: bytes) -> tuple[int, int]:
    if len(data) < 128 or data[:4] != b"DDS ":
        raise ValueError("not a DDS texture")
    flags = struct.unpack_from("<I", data, 80)[0]
    fourcc = data[84:88] if flags & 0x4 else b""
    if fourcc == b"DXT1":
        return 0x42, 128
    if fourcc == b"DXT5":
        return 0x44, 128
    if fourcc == b"DX10" and len(data) >= 148 and struct.unpack_from("<I", data, 128)[0] == 98:
        return 0x4D, 148
    rgb_bits = struct.unpack_from("<I", data, 88)[0]
    if flags & 0x40 and rgb_bits == 32:
        return 0x25, 128
    raise ValueError(f"unsupported DDS format: {fourcc!r}/{rgb_bits}")


def dds_to_nxtch(data: bytes, template: bytes | None = None) -> bytes:
    format_code, data_offset = dds_format(data)
    height, width = struct.unpack_from("<II", data, 12)
    mip_count = max(1, struct.unpack_from("<I", data, 28)[0])
    bit_depth, unit_size, _ = NXTCH_FORMATS[format_code]
    block_compressed = format_code != 0x25
    extension_count = -1
    if template is not None and len(template) >= 0x100 and template[:8] == b"NXTCH000":
        extension_count = struct.unpack_from("<i", template, 0x74)[0]
        header = bytearray(template[:0x100])
    else:
        header = bytearray(0x100)
        header[:8] = b"NXTCH000"
        struct.pack_into("<i", header, 0x74, extension_count)

    cursor = data_offset
    levels = []
    offsets = []
    for level in range(mip_count):
        level_width = max(1, width >> level)
        level_height = max(1, height >> level)
        if block_compressed:
            linear_size = ((level_width + 3) // 4) * ((level_height + 3) // 4) * unit_size
        else:
            linear_size = level_width * level_height * unit_size
        linear = data[cursor : cursor + linear_size]
        if len(linear) != linear_size:
            raise ValueError(f"truncated DDS mip {level}")
        offsets.append(sum(len(item) for item in levels))
        levels.append(swizzle_nxtch_level(
            linear, level_width, level_height, bit_depth, unit_size,
            extension_count, block_compressed,
        ))
        cursor += linear_size

    texture_data = b"".join(levels)
    struct.pack_into("<III", header, 0x08, len(texture_data), 0, 0)
    struct.pack_into("<II", header, 0x14, width, height)
    struct.pack_into("<IIIII", header, 0x1C, 0, 0, format_code, mip_count, len(texture_data))
    struct.pack_into(f"<{mip_count}I", header, 0x30, *offsets)
    return bytes(header) + texture_data


def dds_half_float_cubemap(data: bytes) -> tuple[int, int, int] | None:
    if len(data) < 128 or data[:4] != b"DDS ":
        return None
    height, width = struct.unpack_from("<II", data, 12)
    mip_count = max(1, u32(data, 28))
    pixel_format = u32(data, 84)
    caps2 = u32(data, 112)
    if pixel_format != 113 or not caps2 & 0x200 or not width or width != height:
        return None
    face_size = sum(max(1, width >> level) * max(1, height >> level) * 8 for level in range(mip_count))
    if 128 + face_size * 6 > len(data):
        return None
    return width, mip_count, face_size


def cubemap_to_radiance_hdr(data: bytes, face_width: int, mip_count: int, face_size: int) -> bytes:
    del mip_count
    width = face_width * 4
    height = face_width * 2

    def sample(direction: tuple[float, float, float]) -> tuple[float, float, float]:
        x, y, z = direction
        ax, ay, az = abs(x), abs(y), abs(z)
        if ax >= ay and ax >= az:
            face, u, v = (0, -z / ax, -y / ax) if x >= 0 else (1, z / ax, -y / ax)
        elif ay >= ax and ay >= az:
            face, u, v = (2, x / ay, z / ay) if y >= 0 else (3, x / ay, -z / ay)
        else:
            face, u, v = (4, x / az, -y / az) if z >= 0 else (5, -x / az, -y / az)
        px = min(face_width - 1, max(0, int((u * 0.5 + 0.5) * face_width)))
        py = min(face_width - 1, max(0, int((v * 0.5 + 0.5) * face_width)))
        offset = 128 + face * face_size + (py * face_width + px) * 8
        r, g, b, _ = struct.unpack_from("<4e", data, offset)
        return max(0.0, r), max(0.0, g), max(0.0, b)

    def rgbe(rgb: tuple[float, float, float]) -> bytes:
        maximum = max(rgb)
        if maximum < 1e-32:
            return b"\0\0\0\0"
        mantissa, exponent = math.frexp(maximum)
        scale = mantissa * 256.0 / maximum
        return bytes((min(255, int(rgb[0] * scale)), min(255, int(rgb[1] * scale)), min(255, int(rgb[2] * scale)), exponent + 128))

    pixels = bytearray()
    for row in range(height):
        latitude = (0.5 - (row + 0.5) / height) * math.pi
        cos_latitude = math.cos(latitude)
        y = math.sin(latitude)
        for column in range(width):
            longitude = ((column + 0.5) / width * 2.0 - 1.0) * math.pi
            direction = (cos_latitude * math.cos(longitude), y, cos_latitude * math.sin(longitude))
            pixels.extend(rgbe(sample(direction)))
    header = f"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y {height} +X {width}\n".encode("ascii")
    return header + pixels


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
    if "cubemap" in lower or re.search(r"(?:^|_)cm\d+_tex$", lower):
        return "environment"
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
        "environment": 3,
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


def joint_palette_uses_assigned_indices(
    joint_palette: list[int], skeleton_info: dict | None = None
) -> bool:
    """Return whether an assigned-model palette uses the shared compact order.

    Some body and uniform G4MDs already store physical indices for their
    selected G4SK.  Those rigs interleave face, hair and helper joints, so
    interpreting the values as the compact shared order moves valid body
    weights onto unrelated joints such as the head.
    """
    if not joint_palette:
        return False
    if max(joint_palette) > len(ASSIGNED_SKELETON_JOINT_NAMES):
        return False
    if skeleton_info is None:
        return True

    names = skeleton_info.get("names", [])
    compact_hits = 0
    for joint_index in joint_palette:
        source_index = joint_index - 1
        if (
            0 <= source_index < len(ASSIGNED_SKELETON_JOINT_NAMES)
            and source_index < len(names)
            and names[source_index] == ASSIGNED_SKELETON_JOINT_NAMES[source_index]
        ):
            compact_hits += 1
    # Index 1 is usually ``output`` in both layouts.  A single match is not
    # evidence that the whole palette uses the compact table.
    distinct_count = len(set(joint_palette))
    if compact_hits >= 2 and compact_hits * 2 >= distinct_count:
        return True

    reserved_prefixes = ("output", "eye_", "mouth_", "hair", "mant", "boundingBox")
    inspected = 0
    reserved_hits = 0
    for joint_index in joint_palette:
        if not 0 <= joint_index < len(names):
            continue
        inspected += 1
        if names[joint_index].startswith(reserved_prefixes):
            reserved_hits += 1
    return inspected == 0 or reserved_hits >= 3


def remap_compact_assigned_joint_palette(
    joint_palette: list[int], skeleton_info: dict | None
) -> tuple[list[int], int]:
    if not joint_palette or skeleton_info is None:
        return joint_palette, 0
    target_indices = {name: index for index, name in enumerate(skeleton_info.get("names", []))}
    remapped = []
    changed = 0
    for joint_index in joint_palette:
        source_index = joint_index - 1
        source_name = (
            ASSIGNED_SKELETON_JOINT_NAMES[source_index]
            if 0 <= source_index < len(ASSIGNED_SKELETON_JOINT_NAMES)
            else None
        )
        if source_name is None:
            remapped.append(0)
            changed += joint_index != 0
            continue
        target_index = target_indices.get(source_name)
        if target_index is None:
            remapped.append(0)
            changed += joint_index != 0
            continue
        remapped.append(target_index)
        changed += target_index != joint_index
    return remapped, changed


def remap_assigned_joint_palette(joint_palette: list[int], skeleton_info: dict | None) -> tuple[list[int], int]:
    if not joint_palette_uses_assigned_indices(joint_palette, skeleton_info):
        return joint_palette, 0
    return remap_compact_assigned_joint_palette(joint_palette, skeleton_info)


def shared_face_uses_compact_joint_palette(path: Path, joint_palette: list[int]) -> bool:
    """Whether an assigned face uses the shared character joint table.

    Face G4MDs do not carry their own G4SK.  Their short palettes index the
    stable shared character table (one-based), even when the resolved body
    G4SK happens to have unrelated joints at those physical indices.
    """
    normalized = path.as_posix().replace("\\", "/").lower()
    return bool(
        joint_palette
        and "/_face/" in normalized
        and path.stem.lower().startswith("c")
        and all(0 <= index <= len(ASSIGNED_SKELETON_JOINT_NAMES) for index in joint_palette)
    )


def uniform_source_skeleton_candidates() -> list[tuple[dict, str]]:
    character_root = RAW_DATA_ROOT / "common" / "chr"
    candidates = []
    for stem in ("c000101", "c000201", "c000301", "c000401"):
        paths = [
            character_root / stem / f"{stem}.g4sk",
            character_root / "_face" / "20_EDIT" / "_bodySK" / f"{stem}_edit" / f"{stem}_edit.g4sk",
        ]
        if stem == "c000101":
            paths.append(character_root / "_test" / "c000101_test" / "c000101_test.g4sk")
        for path in paths:
            if path.is_file():
                candidates.append((parse_g4sk(path.read_bytes()), str(path)))
    rig_root = character_root / "_rig"
    if rig_root.is_dir():
        for path in sorted(rig_root.glob("tw*/tw*.g4sk")):
            candidates.append((parse_g4sk(path.read_bytes()), str(path)))
    return candidates


def resolve_uniform_family_g4sk(path: Path) -> tuple[bytes | None, str | None]:
    probe_model = uniform_family_probe_model(path)
    if probe_model is None:
        return None, None

    cache_key = str(probe_model)
    cached = UNIFORM_FAMILY_SKELETON_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        md_data, g4mg, _ = read_model_buffers(probe_model)
        md_info = parse_g4md(md_data, g4mg)
    except (FileNotFoundError, ValueError, struct.error):
        UNIFORM_FAMILY_SKELETON_CACHE[cache_key] = (None, None)
        return None, None

    best: tuple[float, Path] | None = None
    for skeleton_info, source in uniform_source_skeleton_candidates():
        source_path = Path(source)
        total_score = 0.0
        total_vertices = 0
        for record in md_info.get("records", []):
            joint_palette = joint_palette_for_record(md_info, record)
            if len(joint_palette) <= 1:
                continue
            influences = read_skin_influences(g4mg, md_info, record)
            positions = [
                struct.unpack_from(
                    "<fff",
                    g4mg,
                    record["vertex_offset"] + vertex_index * record["vertex_stride"],
                )
                for vertex_index in range(record["vertex_count"])
            ]
            remapped, _ = remap_joint_palette_by_name(
                joint_palette,
                skeleton_info,
                skeleton_info,
            )
            total_score += palette_spatial_error(
                positions,
                influences,
                remapped,
                skeleton_info,
            ) * max(1, record["vertex_count"])
            total_vertices += max(1, record["vertex_count"])
        if total_vertices == 0:
            continue
        average_score = total_score / total_vertices
        candidate = (average_score, source_path)
        if best is None or candidate < best:
            best = candidate

    if best is None or not best[1].is_file():
        UNIFORM_FAMILY_SKELETON_CACHE[cache_key] = (None, None)
        return None, None

    resolved = (
        best[1].read_bytes(),
        f"{best[1]} via {probe_model} ({CHARA_PARTS_JSON})",
    )
    UNIFORM_FAMILY_SKELETON_CACHE[cache_key] = resolved
    return resolved


def uniform_palette_source_skeleton_candidates_for_model(path: Path) -> list[tuple[dict, str]]:
    candidates: list[tuple[dict, str]] = []
    skeleton_data, skeleton_source = resolve_uniform_family_g4sk(path)
    if skeleton_data is not None and skeleton_source is not None:
        candidates.append((parse_g4sk(skeleton_data), skeleton_source))

    seen_sources = {source for _, source in candidates}
    for skeleton_info, source in uniform_source_skeleton_candidates():
        if source in seen_sources:
            continue
        seen_sources.add(source)
        candidates.append((skeleton_info, source))
    return candidates


def resolve_uniform_palette_source(
    path: Path,
    target_skeleton: dict | None,
    palette_indices: set[int],
    skeleton_source: str | None = None,
) -> tuple[dict | None, str | None]:
    if target_skeleton is None or "/_uniform/" not in path.as_posix().replace("\\", "/"):
        return None, None
    target_names = set(target_skeleton.get("names", []))
    candidates = uniform_source_skeleton_candidates()
    if target_skeleton.get("joint_count", 0) <= 200:
        candidates.insert(0, (target_skeleton, "target skeleton"))

    def score(candidate: tuple[dict, str]) -> tuple[int, int, int]:
        skeleton, source = candidate
        names = skeleton.get("names", [])
        palette_names = [names[index] for index in palette_indices if index < len(names)]
        palette_overlap = sum(bool(name) and name in target_names for name in palette_names)
        body_overlap = sum(bool(name) and name in target_names for name in names)
        target_bonus = int(source == "target skeleton")
        return palette_overlap, body_overlap, target_bonus

    return max(candidates, key=score, default=(None, None))


def remap_joint_palette_by_name(
    joint_palette: list[int],
    source_skeleton: dict | None,
    target_skeleton: dict | None,
) -> tuple[list[int], int]:
    if not joint_palette or source_skeleton is None or target_skeleton is None:
        return joint_palette, 0
    source_names = source_skeleton.get("names", [])
    source_parents = source_skeleton.get("parent_indices", [])
    target_indices = {name: index for index, name in enumerate(target_skeleton.get("names", [])) if name}
    remapped = []
    changed = 0
    for joint_index in joint_palette:
        source_index = joint_index
        target_index = None
        visited = set()
        while source_index < len(source_names) and source_index not in visited:
            visited.add(source_index)
            source_name = source_names[source_index]
            target_index = target_indices.get(source_name) if source_name else None
            if target_index is None and source_name and source_name.endswith("_wgt_1_0"):
                target_index = target_indices.get(source_name.removesuffix("_wgt_1_0"))
            if target_index is not None:
                break
            parent = source_parents[source_index] if source_index < len(source_parents) else len(source_names)
            if parent >= len(source_names) or parent == source_index:
                break
            source_index = parent
        if target_index is None:
            remapped.append(0)
            changed += joint_index != 0
            continue
        remapped.append(target_index)
        changed += target_index != joint_index
    return remapped, changed


def palette_spatial_error(
    positions: list[tuple[float, float, float]],
    influences: list[list[tuple[int, float]]],
    joint_palette: list[int],
    target_skeleton: dict,
) -> float:
    bind_matrices = target_skeleton.get("bind_matrices", [])
    if not positions or not influences or not bind_matrices:
        return math.inf
    total = 0.0
    total_weight = 0.0
    step = max(1, len(positions) // 2000)
    for vertex_index in range(0, min(len(positions), len(influences)), step):
        position = positions[vertex_index]
        for local_index, weight in influences[vertex_index]:
            if weight <= 0.0:
                continue
            if (
                local_index >= len(joint_palette)
                or not 0 <= joint_palette[local_index] < len(bind_matrices)
            ):
                total += 100.0 * weight
                total_weight += weight
                continue
            matrix = bind_matrices[joint_palette[local_index]]
            total += sum((position[axis] - matrix[axis * 4 + 3]) ** 2 for axis in range(3)) * weight
            total_weight += weight
    return math.sqrt(total / total_weight) if total_weight else math.inf


def palette_weighted_centroids(
    positions: list[tuple[float, float, float]],
    influences: list[list[tuple[int, float]]],
    palette_length: int,
) -> list[tuple[float, float, float] | None]:
    totals = [[0.0, 0.0, 0.0, 0.0] for _ in range(palette_length)]
    for position, vertex_influences in zip(positions, influences):
        for local_index, weight in vertex_influences:
            if weight <= 0.0 or local_index >= palette_length:
                continue
            totals[local_index][0] += weight
            for axis in range(3):
                totals[local_index][axis + 1] += position[axis] * weight
    return [
        tuple(row[axis + 1] / row[0] for axis in range(3)) if row[0] else None
        for row in totals
    ]


def minimum_cost_assignment(costs: list[list[float]]) -> list[int]:
    """Rectangular Hungarian assignment; rows must not outnumber columns."""
    if not costs:
        return []
    row_count = len(costs)
    column_count = len(costs[0])
    if row_count > column_count:
        return []
    u = [0.0] * (row_count + 1)
    v = [0.0] * (column_count + 1)
    matched_row = [0] * (column_count + 1)
    previous = [0] * (column_count + 1)
    for row in range(1, row_count + 1):
        matched_row[0] = row
        column = 0
        minimum = [math.inf] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[column] = True
            active_row = matched_row[column]
            delta = math.inf
            next_column = 0
            for candidate in range(1, column_count + 1):
                if used[candidate]:
                    continue
                current = costs[active_row - 1][candidate - 1] - u[active_row] - v[candidate]
                if current < minimum[candidate]:
                    minimum[candidate] = current
                    previous[candidate] = column
                if minimum[candidate] < delta:
                    delta = minimum[candidate]
                    next_column = candidate
            for candidate in range(column_count + 1):
                if used[candidate]:
                    u[matched_row[candidate]] += delta
                    v[candidate] -= delta
                else:
                    minimum[candidate] -= delta
            column = next_column
            if matched_row[column] == 0:
                break
        while True:
            next_column = previous[column]
            matched_row[column] = matched_row[next_column]
            column = next_column
            if column == 0:
                break
    result = [-1] * row_count
    for column in range(1, column_count + 1):
        if matched_row[column]:
            result[matched_row[column] - 1] = column - 1
    return result


def refine_palette_by_bind_names(
    palette: list[int],
    positions: list[tuple[float, float, float]],
    influences: list[list[tuple[int, float]]],
    target_skeleton: dict,
    compatibility_distance: float = 0.15,
) -> tuple[list[int], int]:
    """Repair compact-palette shifts while preserving spatially valid names."""
    bind_matrices = target_skeleton.get("bind_matrices", [])
    names = target_skeleton.get("names", [])
    if not palette or not bind_matrices or len(names) != len(bind_matrices):
        return palette, 0
    centroids = palette_weighted_centroids(positions, influences, len(palette))

    def distance(local_index: int, joint_index: int) -> float:
        centroid = centroids[local_index]
        if centroid is None or joint_index >= len(bind_matrices):
            return math.inf
        matrix = bind_matrices[joint_index]
        return math.sqrt(sum(
            (centroid[axis] - matrix[axis * 4 + 3]) ** 2
            for axis in range(3)
        ))

    def semantic_match(local_index: int, joint_index: int) -> bool:
        centroid = centroids[local_index]
        name = names[joint_index] if joint_index < len(names) else ""
        if centroid is None:
            return True
        side_matches = (
            (name.startswith("l_") and centroid[0] >= -0.01)
            or (name.startswith("r_") and centroid[0] <= 0.01)
        )
        limb_tokens = (
            "_s_", "_a_", "_w_", "_slv", "_idx_", "_mid_", "_rng_",
            "_thb_", "_pky_", "_soh_", "_fa_", "_l_", "_foot_", "_pnt_",
        )
        if side_matches and any(token in name for token in limb_tokens):
            return True
        if name.startswith("c_") and abs(centroid[0]) < 0.08:
            return True
        return distance(local_index, joint_index) <= compatibility_distance

    bad_rows = [
        local_index
        for local_index, joint_index in enumerate(palette)
        if not semantic_match(local_index, joint_index)
    ]
    if not bad_rows:
        return palette, 0
    shoe_palette = len(palette) <= 8 and all(
        centroid is None or centroid[1] < 0.45
        for centroid in centroids
    )
    if shoe_palette:
        bad_rows = [index for index, centroid in enumerate(centroids) if centroid is not None]
    locked = {joint_index for index, joint_index in enumerate(palette) if index not in bad_rows}
    target_by_name = {name: index for index, name in enumerate(names) if name}
    candidates = list(dict.fromkeys(
        target_by_name[name]
        for name in ASSIGNED_SKELETON_JOINT_NAMES
        if name in target_by_name
        and target_by_name[name] not in locked
        and name not in {"output", "c_global_0_0", "c_ball_1_0"}
        and (shoe_palette or "_pnt_" not in name)
        and (not shoe_palette or re.fullmatch(r"[lr]_(?:l|foot|pnt)_\d+_\d+", name))
    ))
    if len(candidates) < len(bad_rows):
        return palette, 0
    costs = []
    for local_index in bad_rows:
        row = []
        for joint_index in candidates:
            value = distance(local_index, joint_index)
            if joint_index == palette[local_index]:
                value -= 0.06
            row.append(value)
        costs.append(row)
    assignment = minimum_cost_assignment(costs)
    if len(assignment) != len(bad_rows) or any(index < 0 for index in assignment):
        return palette, 0
    refined = palette[:]
    for local_index, candidate_index in zip(bad_rows, assignment):
        refined[local_index] = candidates[candidate_index]
    return refined, sum(left != right for left, right in zip(palette, refined))


BODY_JOINT_RE = re.compile(
    r"(?:[lr]_(?:s|a|w|wph|idx|mid|rng|thb|soh|pky|fa|bnd|slv\d*|l|foot|pnt|mnt|pkt)_"
    r"|c_(?:c|n|head|col|sht|til|ball|bst|mnt|ncl|hood))"
)


def remap_palette_by_target_segments(
    joint_palette: list[int],
    positions: list[tuple[float, float, float]],
    influences: list[list[tuple[int, float]]],
    target_skeleton: dict,
) -> tuple[list[int], int]:
    """Resolve model-specific palette offsets independently for each body side."""
    names = target_skeleton.get("names", [])
    matrices = target_skeleton.get("bind_matrices", [])
    if not joint_palette or len(names) != len(matrices):
        return joint_palette, 0
    centroids = palette_weighted_centroids(positions, influences, len(joint_palette))

    def side(centroid: tuple[float, float, float] | None) -> str:
        if centroid is None or abs(centroid[0]) <= 0.05:
            return "c"
        return "l" if centroid[0] > 0.0 else "r"

    segments = []
    start = 0
    for index in range(1, len(joint_palette)):
        numeric_gap = joint_palette[index] - joint_palette[index - 1] > 8
        side_change = side(centroids[index]) != side(centroids[index - 1])
        if numeric_gap or side_change:
            segments.append(range(start, index))
            start = index
    segments.append(range(start, len(joint_palette)))

    remapped = joint_palette[:]
    resolved = 0
    for segment in segments:
        rows = list(segment)
        segment_side = side(centroids[rows[0]])
        best = None
        minimum_offset = -min(joint_palette[index] for index in rows)
        maximum_offset = len(names) - 1 - max(joint_palette[index] for index in rows)
        for offset in range(minimum_offset, maximum_offset + 1):
            score = 0.0
            valid = True
            candidate = []
            for local_index in rows:
                joint_index = joint_palette[local_index] + offset
                name = names[joint_index]
                centroid = centroids[local_index]
                if (
                    not name
                    or "_wgt_" in name
                    or BODY_JOINT_RE.match(name) is None
                    or (segment_side == "l" and not name.startswith("l_"))
                    or (segment_side == "r" and not name.startswith("r_"))
                    or (segment_side == "c" and not name.startswith("c_"))
                    or centroid is None
                ):
                    valid = False
                    break
                matrix = matrices[joint_index]
                score += math.sqrt(sum(
                    (centroid[axis] - matrix[axis * 4 + 3]) ** 2
                    for axis in range(3)
                ))
                candidate.append(joint_index)
            if not valid:
                continue
            result = (score / len(rows), abs(offset), candidate)
            if best is None or result[:2] < best[:2]:
                best = result
        if best is None:
            continue
        for local_index, joint_index in zip(rows, best[2]):
            remapped[local_index] = joint_index
            resolved += 1
    return remapped, resolved


def remap_palette_by_body_assignment(
    joint_palette: list[int],
    positions: list[tuple[float, float, float]],
    influences: list[list[tuple[int, float]]],
    target_skeleton: dict,
) -> tuple[list[int], int]:
    """Last-resort one-to-one body-name assignment for irregular palettes."""
    names = target_skeleton.get("names", [])
    matrices = target_skeleton.get("bind_matrices", [])
    if not joint_palette or len(names) != len(matrices):
        return joint_palette, 0
    centroids = palette_weighted_centroids(positions, influences, len(joint_palette))
    remapped = joint_palette[:]
    assigned = 0
    for side in ("l", "r", "c"):
        rows = [
            index for index, centroid in enumerate(centroids)
            if centroid is not None
            and (
                (side == "l" and centroid[0] > 0.05)
                or (side == "r" and centroid[0] < -0.05)
                or (side == "c" and abs(centroid[0]) <= 0.05)
            )
        ]
        candidates = [
            index for index, name in enumerate(names)
            if name
            and not re.match(r"[lrc]_(?:mnt|pkt)_", name)
            and BODY_JOINT_RE.match(name)
            and (
                (side == "l" and name.startswith("l_"))
                or (side == "r" and name.startswith("r_"))
                or (side == "c" and name.startswith("c_"))
            )
        ]
        if not rows or len(candidates) < len(rows):
            continue
        costs = []
        for local_index in rows:
            centroid = centroids[local_index]
            costs.append([
                math.sqrt(sum(
                    (centroid[axis] - matrices[joint_index][axis * 4 + 3]) ** 2
                    for axis in range(3)
                ))
                for joint_index in candidates
            ])
        assignment = minimum_cost_assignment(costs)
        if any(index < 0 for index in assignment):
            continue
        for local_index, candidate_index in zip(rows, assignment):
            remapped[local_index] = candidates[candidate_index]
            assigned += 1
    return remapped, assigned


def refine_arm_palette_by_centroids(
    palette: list[int],
    positions: list[tuple[float, float, float]],
    influences: list[list[tuple[int, float]]],
    target_skeleton: dict,
) -> tuple[list[int], int]:
    """Resolve compact arm palettes that interleave terminal weight helpers."""
    names = target_skeleton.get("names", [])
    matrices = target_skeleton.get("bind_matrices", [])
    if not palette or len(names) != len(matrices):
        return palette, 0
    centroids = palette_weighted_centroids(positions, influences, len(palette))
    result = palette[:]
    changed = 0
    for side, direction in (("l", 1.0), ("r", -1.0)):
        def preserve_assignment(index: int) -> bool:
            if not 0 <= palette[index] < len(names):
                return False
            name = names[palette[index]]
            return bool(
                re.match(rf"^{side}_(?:s|a|wph)_", name)
                or re.match(rf"^{side}_slv1_", name)
            )

        side_rows = [
            index for index, centroid in enumerate(centroids)
            if centroid is not None
            and centroid[1] > 1.05
            and centroid[0] * direction > 0.05
        ]
        locked = {
            palette[index]
            for index in side_rows
            if preserve_assignment(index)
        }
        rows = [
            index for index in side_rows
            if not preserve_assignment(index)
        ]
        pattern = re.compile(
            rf"^{side}_(?:s|a|w|wph|idx|mid|rng|thb|soh|pky|fa|bnd|slv\d*)_"
        )
        candidates = [
            index for index, name in enumerate(names)
            if pattern.match(name) and "_mnt_" not in name and index not in locked
        ]
        if not rows or len(candidates) < len(rows):
            continue
        costs = []
        for local_index in rows:
            centroid = centroids[local_index]
            costs.append([
                math.sqrt(sum(
                    (centroid[axis] - matrices[joint_index][axis * 4 + 3]) ** 2
                    for axis in range(3)
                ))
                for joint_index in candidates
            ])
        assignment = minimum_cost_assignment(costs)
        if any(index < 0 for index in assignment):
            continue
        for local_index, candidate_index in zip(rows, assignment):
            joint_index = candidates[candidate_index]
            changed += result[local_index] != joint_index
            result[local_index] = joint_index
    return result, changed


def sanitize_palette_body_names(
    palette: list[int],
    positions: list[tuple[float, float, float]],
    influences: list[list[tuple[int, float]]],
    target_skeleton: dict,
) -> tuple[list[int], int]:
    """Remove residual facial/non-body and opposite-side bindings."""
    names = target_skeleton.get("names", [])
    matrices = target_skeleton.get("bind_matrices", [])
    body_candidates = [
        index for index, name in enumerate(names)
        if name and "_wgt_" not in name and BODY_JOINT_RE.match(name)
    ]
    if len(body_candidates) < 20 or len(names) != len(matrices):
        return palette, 0
    centroids = palette_weighted_centroids(positions, influences, len(palette))
    result = palette[:]
    changed = 0
    for local_index, joint_index in enumerate(palette):
        centroid = centroids[local_index]
        name = names[joint_index] if 0 <= joint_index < len(names) else ""
        wrong_side = bool(
            centroid
            and (
                (centroid[0] > 0.08 and name.startswith("r_"))
                or (centroid[0] < -0.08 and name.startswith("l_"))
            )
        )
        invalid = bool(
            re.search(r"(?:eye|eld|ebw|lip|tng|jaw|chk|hir)", name, re.IGNORECASE)
            or (name and BODY_JOINT_RE.match(name) is None and name not in {"output", "c_global_0_0", "boundingBox"})
        )
        if centroid is None or not (wrong_side or invalid):
            continue
        side = "l" if centroid[0] > 0.05 else "r" if centroid[0] < -0.05 else "c"
        candidates = [
            candidate for candidate in body_candidates
            if (
                (side == "l" and names[candidate].startswith("l_"))
                or (side == "r" and names[candidate].startswith("r_"))
                or (side == "c" and names[candidate].startswith("c_"))
            )
        ]
        if not candidates:
            continue
        result[local_index] = min(candidates, key=lambda candidate: sum(
            (centroid[axis] - matrices[candidate][axis * 4 + 3]) ** 2
            for axis in range(3)
        ))
        changed += result[local_index] != joint_index
    return result, changed


def choose_uniform_joint_palette(
    joint_palette: list[int],
    positions: list[tuple[float, float, float]],
    influences: list[list[tuple[int, float]]],
    target_skeleton: dict,
    source_skeletons: list[tuple[dict, str]],
) -> tuple[list[int], int, str, float]:
    candidates: list[tuple[list[int], int, str]] = [(joint_palette, 0, "target skeleton indices")]
    # Compact shared palettes cannot reference an index beyond their fixed
    # table.  Such entries are already physical G4SK indices; applying body
    # offset heuristics would turn valid targets such as c_bst1_1_0 into
    # unrelated head or foot helpers.
    if any(index > len(ASSIGNED_SKELETON_JOINT_NAMES) for index in joint_palette):
        palette, remap_count = refine_arm_palette_by_centroids(
            joint_palette, positions, influences, target_skeleton
        )
        refined_score = palette_spatial_error(positions, influences, palette, target_skeleton)
        direct_score = palette_spatial_error(positions, influences, joint_palette, target_skeleton)
        if remap_count and refined_score < direct_score:
            candidates.append((palette, remap_count, "physical target G4SK indices + arm centroid refinement"))
    if joint_palette and min(joint_palette) > 0 and max(joint_palette) <= len(ASSIGNED_SKELETON_JOINT_NAMES):
        remapped, changed = remap_compact_assigned_joint_palette(joint_palette, target_skeleton)
        candidates.append((remapped, changed, "shared uniform skeleton indices"))
    for source_skeleton, source in source_skeletons:
        remapped, changed = remap_joint_palette_by_name(joint_palette, source_skeleton, target_skeleton)
        candidates.append((remapped, changed, source))
    segmented, segmented_count = remap_palette_by_target_segments(
        joint_palette, positions, influences, target_skeleton
    )
    if segmented_count:
        candidates.append((segmented, segmented_count, "target G4SK segmented body-name offsets"))

    target_names = target_skeleton.get("names", [])
    centroids = palette_weighted_centroids(positions, influences, len(joint_palette))

    def semantic_quality(palette: list[int]) -> tuple[int, int, int]:
        facial = 0
        crossed = 0
        non_body = 0
        for local_index, joint_index in enumerate(palette):
            name = target_names[joint_index] if 0 <= joint_index < len(target_names) else ""
            centroid = centroids[local_index] if local_index < len(centroids) else None
            facial += bool(re.search(r"(?:eye|eld|ebw|lip|tng|jaw|chk|hir)", name, re.IGNORECASE))
            crossed += bool(
                centroid
                and (
                    (centroid[0] > 0.08 and name.startswith("r_"))
                    or (centroid[0] < -0.08 and name.startswith("l_"))
                )
            )
            non_body += bool(
                name
                and BODY_JOINT_RE.match(name) is None
                and name not in {"output", "c_global_0_0", "boundingBox"}
            )
        return facial, crossed, non_body

    if not any(semantic_quality(palette) == (0, 0, 0) for palette, _, _ in candidates):
        assigned, assigned_count = remap_palette_by_body_assignment(
            joint_palette, positions, influences, target_skeleton
        )
        if assigned_count:
            candidates.append((assigned, assigned_count, "target G4SK one-to-one body-name assignment"))

    unique = {}
    for palette, changed, source in candidates:
        unique.setdefault(tuple(palette), (palette, changed, source))

    scored = []
    for palette, changed, source in unique.values():
        score = palette_spatial_error(positions, influences, palette, target_skeleton)
        scored.append((semantic_quality(palette), score, palette, changed, source))
    _, score, palette, changed, source = min(scored, key=lambda item: (item[0], item[1]))
    palette, sanitized = sanitize_palette_body_names(
        palette, positions, influences, target_skeleton
    )
    if sanitized:
        changed += sanitized
        source += " + body-name sanitization"
        score = palette_spatial_error(positions, influences, palette, target_skeleton)
    return palette, changed, source, score


def remap_shoe_point_helpers(joint_palette: list[int], target_skeleton: dict) -> tuple[list[int], int]:
    names = target_skeleton.get("names", [])
    indices = {name: index for index, name in enumerate(names) if name}
    remapped = joint_palette[:]
    changed = 0
    for palette_index, joint_index in enumerate(joint_palette):
        name = names[joint_index] if 0 <= joint_index < len(names) else ""
        match = re.fullmatch(r"([lr])_pnt_1_0", name)
        if match is None:
            continue
        replacement = indices.get(f"{match.group(1)}_foot_1_1_wgt_1_0")
        if replacement is None:
            replacement = indices.get(f"{match.group(1)}_foot_1_1")
        if replacement is not None and replacement != joint_index:
            remapped[palette_index] = replacement
            changed += 1
    return remapped, changed


def remap_separated_shoe_palette(
    data: bytes,
    joint_palette: list[int],
    target_skeleton: dict,
    palette_table_offset: int,
    source_skeletons: list[tuple[dict, str]],
) -> tuple[list[int] | None, int]:
    """Resolve the compact lower-body palette used by separated shoes.

    Cross exports this family as six named SkinnedMeshRenderer bones in the
    order left foot helper, left ankle helper, left shin helper, then the
    matching right-side trio.  In Victory Road the G4MD palette addresses
    that model's CRC32 name table with a three-entry prefix included.  It is
    neither a physical target-G4SK index nor the normal one-based CRC slot.
    """
    if len(joint_palette) != 6:
        return None, 0

    target_indices = {name: index for index, name in enumerate(target_skeleton.get("names", [])) if name}
    if not target_indices:
        return None, 0

    leg_helper = re.compile(r"[lr]_(?:foot|l)_1_[01]_wgt_1_0")
    target_hashes = {crc32b(name.encode("ascii")): name for name in target_indices}

    # The G4MD has a contiguous hash table immediately before its local
    # palette data.  Locate it by its complete overlap with the assigned
    # skeleton, then apply the shoe-specific three-entry displacement.  This
    # is portable across c000*/c050*/c060* bodies because final resolution is
    # performed by bone name rather than by an ordinal from c000201.
    if min(joint_palette) > 3:
        required_length = max(joint_palette)
        search_end = min(len(data), palette_table_offset)
        best_offset = -1
        best_length = 0
        for offset in range(0, max(0, search_end - required_length * 4 + 1), 4):
            length = 0
            while offset + (length + 1) * 4 <= search_end:
                if u32(data, offset + length * 4) not in target_hashes:
                    break
                length += 1
            if length > best_length:
                best_offset = offset
                best_length = length
        if best_length >= required_length:
            names = [
                target_hashes.get(u32(data, best_offset + (index - 4) * 4), "")
                for index in joint_palette
            ]
            if (
                len(set(names)) == 6
                and all(leg_helper.fullmatch(name) is not None for name in names)
                and sum(name.startswith("l_") for name in names) == 3
                and sum(name.startswith("r_") for name in names) == 3
            ):
                remapped = [target_indices[name] for name in names]
                return remapped, sum(before != after for before, after in zip(joint_palette, remapped))

    # Older extracted model variants can lack the full table. Keep the
    # verified c000201 fallback for those files only.
    # The ``141, 142, 143`` prefix identifies the common c000201-derived
    # shoe template.  Its right-side palette is discontinuous (the exact
    # stored values vary by shoe), whereas the semantic slot order does not.
    # Cross's exported prefabs make that order explicit.
    c000201_template = joint_palette[:3] == [141, 142, 143]
    for source_skeleton, source in source_skeletons:
        if "/c000201/" not in source.replace("\\", "/"):
            continue
        source_names = source_skeleton.get("names", [])
        source_palette = (
            [144, 145, 146, 153, 154, 155]
            if c000201_template
            else [index + 3 for index in joint_palette]
        )
        if any(index >= len(source_names) for index in source_palette):
            continue
        names = [source_names[index] for index in source_palette]
        # Only claim the special encoding if it resolves to Cross's complete
        # bilateral lower-body helper set. Other shoe families use ordinary
        # physical G4SK indices and continue through the general path.
        if len(set(names)) != 6 or any(leg_helper.fullmatch(name or "") is None for name in names):
            continue
        if sum(name.startswith("l_") for name in names) != 3 or sum(name.startswith("r_") for name in names) != 3:
            continue
        if any(name not in target_indices for name in names):
            continue
        remapped = [target_indices[name] for name in names]
        return remapped, sum(before != after for before, after in zip(joint_palette, remapped))
    return None, 0


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
    normalized_path = path.as_posix().replace("\\", "/")
    if "/_uniform/" in normalized_path:
        target_override = load_target_skeleton_override()
        if target_override is not None:
            skeleton_info = target_override
            skeleton_source = f"{TARGET_SKELETON_OVERRIDE} via actor skeleton override"
    assigned_skeleton = skeleton_is_assigned(path, skeleton_source)
    uniform_model = "/_uniform/" in normalized_path and skeleton_info is not None
    uniform_palette_sources = (
        uniform_palette_source_skeleton_candidates_for_model(path)
        if uniform_model
        else []
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    dae_path = out_dir / f"{path.stem}.dae"

    texture_containers: list[Path] = []
    texture_paths: list[Path] = []
    if extract_textures:
        texture_containers = find_texture_containers_for_model(path)
        texture_containers.extend(
            candidate
            for candidate in find_uniform_texture_containers_from_chara(path, md_info.get("material_names", []))
            if candidate not in texture_containers
        )
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
    normalized_path = path.as_posix().lower()
    prefer_material_refs = "/map/" in normalized_path or "/effect/" in normalized_path
    for record in records:
        mesh_name = mesh_name_for_export(md_info, record, skeleton_info)
        material_name = material_name_for_mesh(md_info, mesh_name, record["index"], record, path.stem)
        if material_name not in used_materials:
            texture = (
                choose_texture_from_material_record(md_info, material_name, texture_paths, path.stem)
                if prefer_material_refs else None
            )
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
        vertex_colors: list[float] = []
        color_element = layout_element(layout_for_record(md_info, record), 8)
        color_offset = color_element.get("value_offset") if color_element is not None else None
        for vertex_index in range(vertex_count):
            off = vertex_offset + vertex_index * vertex_stride
            position = struct.unpack_from("<fff", g4mg, off)
            position_tuples.append(position)
            positions.extend(position)
            texcoords.extend(read_uv0(g4mg, md_info, record, vertex_index))
            if color_offset is not None and off + color_offset + 4 <= len(g4mg):
                vertex_colors.extend(value / 255.0 for value in g4mg[off + color_offset : off + color_offset + 4])
            else:
                vertex_colors.extend((1.0, 1.0, 1.0, 1.0))

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
        if len(joint_palette) == 1:
            skin_influences = rigid_skin_influences(vertex_count)
            skin_mode = "rigid_single_palette"
        else:
            skin_influences = read_skin_influences(g4mg, md_info, record)
            skin_mode = "vertex_weights"
            if joint_palette:
                valid_influences, invalid_influences = skin_palette_fit(skin_influences, len(joint_palette))
                if invalid_influences > valid_influences:
                    skin_influences = rigid_skin_influences(vertex_count)
                    skin_mode = "rigid_invalid_palette_bytes"
        palette_remap_count = 0
        palette_remap_source = None
        palette_spatial_score = None
        # The separated neck/arm meshes have a shuffled local palette.  Their
        # embedded CRC table is the only stable relationship to the actor
        # skeleton; selecting a source G4SK by proximity swaps finger groups
        # on otherwise compatible body types.
        crc_palette_is_authoritative = (
            uniform_model
            and path.stem.lower().startswith(("sk", "s", "g", "m", "n"))
        )
        hash_palette, hash_remap_count = remap_joint_palette_by_g4md_hashes(
            md_data,
            joint_palette,
            skeleton_info,
            md_info.get("joint_hash_table", 0),
            allow_partial=crc_palette_is_authoritative,
        )
        direct_palette_score = palette_spatial_error(
            position_tuples,
            skin_influences,
            joint_palette,
            skeleton_info,
        )
        hash_palette_score = (
            palette_spatial_error(position_tuples, skin_influences, hash_palette, skeleton_info)
            if hash_palette is not None
            else None
        )
        shoe_palette, shoe_palette_remap_count = (
            remap_separated_shoe_palette(
                joint_palette,
                skeleton_info,
                uniform_palette_sources,
            )
            if hash_palette is None and uniform_model and path.stem.lower().startswith("s")
            else (None, 0)
        )
        if crc_palette_is_authoritative and hash_palette is not None:
            joint_palette = hash_palette
            palette_remap_count = hash_remap_count
            palette_remap_source = "G4MD CRC32 joint table (named character part)"
            palette_spatial_score = hash_palette_score
        elif shoe_palette is not None:
            joint_palette = shoe_palette
            palette_remap_count = shoe_palette_remap_count
            palette_remap_source = "c000201 compact lower-body helper palette (legacy fallback)"
            palette_spatial_score = palette_spatial_error(
                position_tuples, skin_influences, joint_palette, skeleton_info
            )
        # A G4MD can contain a full CRC32 skeleton-name table while some of
        # its submeshes still use physical G4SK indices.  This is common in
        # bodies and shoes, so keep a geometrically better physical palette.
        # Conversely, gloves can be compact CRC palettes even when their
        # physical indices happen to be near the mesh in bind pose.
        elif direct_palette_score <= 0.35:
            if hash_palette is not None and hash_palette_score is not None and hash_palette_score < direct_palette_score:
                joint_palette = hash_palette
                palette_remap_count = hash_remap_count
                palette_remap_source = "G4MD CRC32 joint table (spatially verified)"
                palette_spatial_score = hash_palette_score
            else:
                palette_remap_source = "physical target G4SK indices (spatially verified)"
                palette_spatial_score = direct_palette_score
        elif uniform_model and len(joint_palette) > 1:
            joint_palette, palette_remap_count, palette_remap_source, palette_spatial_score = choose_uniform_joint_palette(
                joint_palette,
                position_tuples,
                skin_influences,
                skeleton_info,
                uniform_palette_sources,
            )
            if hash_palette is not None and hash_palette_score is not None and hash_palette_score < palette_spatial_score:
                joint_palette = hash_palette
                palette_remap_count = hash_remap_count
                palette_remap_source = "G4MD CRC32 joint table (spatially verified)"
                palette_spatial_score = hash_palette_score
            if path.stem.lower().startswith("s"):
                joint_palette, shoe_remaps = remap_shoe_point_helpers(joint_palette, skeleton_info)
                if shoe_remaps:
                    palette_remap_count += shoe_remaps
                    palette_remap_source += " + shoe point helpers"
                    palette_spatial_score = palette_spatial_error(
                        position_tuples, skin_influences, joint_palette, skeleton_info
                    )
        elif hash_palette is not None and hash_palette_score is not None and hash_palette_score < direct_palette_score:
            joint_palette = hash_palette
            palette_remap_count = hash_remap_count
            palette_remap_source = "G4MD CRC32 joint table (spatially verified)"
            palette_spatial_score = hash_palette_score
        elif assigned_skeleton:
            if shared_face_uses_compact_joint_palette(path, joint_palette):
                joint_palette, palette_remap_count = remap_compact_assigned_joint_palette(
                    joint_palette, skeleton_info
                )
                palette_remap_source = "shared face skeleton order"
            else:
                joint_palette, palette_remap_count = remap_assigned_joint_palette(joint_palette, skeleton_info)
                palette_remap_source = "compact assigned skeleton order"
        joint_palette_override = face_rigid_joint_override(path, skeleton_info, joint_palette)
        if joint_palette_override is not None:
            joint_palette = joint_palette_override
            skin_mode = "rigid_face_head_override"
        p: list[int] = []
        for index in indices:
            p.extend((index, index, index, index))

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
                "vertex_colors": vertex_colors,
                "color_offset": color_offset,
                "indices": p,
                "triangle_indices": indices,
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
                "palette_remap_source": palette_remap_source,
                "palette_spatial_score": palette_spatial_score,
                "skin_mode": skin_mode,
                "skin_influences": skin_influences,
                "skin_summary": summarize_skin_influences(skin_influences),
            }
        )

    native_path = dae_path.with_suffix(".g4mesh.json")
    native_payload = {
        "format": "level5-g4-native-mesh",
        "version": 1,
        "source": str(path),
        "materials": {name: str(texture) if texture else None for name, texture in used_materials.items()},
        "meshes": [
            {
                "name": payload["name"],
                "material": payload["material"],
                "positions": payload["positions"],
                "normals": payload["normals"],
                "texcoords": payload["texcoords"],
                "vertex_colors": payload["vertex_colors"],
                "indices": payload["triangle_indices"],
                "joint_palette": payload["joint_palette"],
                "palette_base": payload["palette_base"],
                "skin_influences": payload["skin_influences"],
            }
            for payload in mesh_payloads
        ],
        "skeleton": None
        if skeleton_info is None
        else {
            "names": skeleton_info.get("names", []),
            "parent_indices": skeleton_info.get("parent_indices", []),
            "bind_matrices": skeleton_info.get("bind_matrices", []),
            "inverse_bind_matrices": skeleton_info.get("inverse_bind_matrices", []),
            "local_matrices": skeleton_info.get("local_matrices", []),
            "local_rotations_xyzw": skeleton_info.get("local_rotations_xyzw", []),
        },
    }
    native_path.write_text(json.dumps(native_payload, separators=(",", ":")), encoding="utf-8")

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
        vertex_colors = payload["vertex_colors"]
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
            f'<source id="{gid}_colors"><float_array id="{gid}_colors_array" count="{len(vertex_colors)}">'
            f"{dae_float_array(vertex_colors)}</float_array><technique_common>"
            f'<accessor source="#{gid}_colors_array" count="{vcount}" stride="4">'
            f'<param name="R" type="float"/><param name="G" type="float"/>'
            f'<param name="B" type="float"/><param name="A" type="float"/>'
            f"</accessor></technique_common></source>"
            f'<vertices id="{gid}_vertices"><input semantic="POSITION" source="#{gid}_positions"/></vertices>'
            f'<triangles material="{mat_id}" count="{triangles}">'
            f'<input semantic="VERTEX" source="#{gid}_vertices" offset="0"/>'
            f'<input semantic="NORMAL" source="#{gid}_normals" offset="1"/>'
            f'<input semantic="TEXCOORD" source="#{gid}_texcoords" offset="2" set="0"/>'
            f'<input semantic="COLOR" source="#{gid}_colors" offset="3" set="0"/>'
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
        "native_mesh": str(native_path),
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
            "outline_parameters": "normalized uint8x4 attribute type 8; preserved as vertex COLOR",
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
                "color_offset": None if payload["color_offset"] is None else f"0x{payload['color_offset']:x}",
                "normal_source": payload["normal_source"],
                "palette_offset": payload["palette_base"],
                "palette_length": payload["palette_length"],
                "joint_palette": payload["joint_palette"],
                "assigned_skeleton_palette_remaps": payload["assigned_skeleton_palette_remaps"],
                "palette_remap_source": payload["palette_remap_source"],
                "palette_spatial_score": payload["palette_spatial_score"],
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
        "native_mesh": report.get("native_mesh"),
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
