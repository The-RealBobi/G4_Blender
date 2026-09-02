#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from pathlib import Path

try:
    from g4_model_probe import dds_format, dds_to_nxtch
    from g4_joint_aliases import load_joint_alias_catalog, normalize_joint_key, resolve_catalog_alias
except ImportError:
    from .g4_model_probe import dds_format, dds_to_nxtch
    from .g4_joint_aliases import load_joint_alias_catalog, normalize_joint_key, resolve_catalog_alias


DEFAULT_RAW_ROOT = Path(os.environ.get("LEVEL5_G4_RAW_ROOT", "data"))
DEFAULT_CHARA_MODEL = Path(os.environ.get("LEVEL5_G4_CHARA_MODEL", "chara_model.xml"))
MODEL_REL = Path("chr/_face/01_IE1/c01000010/c01000010")
COMMON_REL = Path("common") / MODEL_REL.with_suffix(".g4md")
DX11_REL = Path("dx11") / MODEL_REL.with_suffix(".g4tx")
NATIVE_MATERIAL_NAMES = ["c01000010_20M", "mouth_10M", "eye_10M"]
FACE_MESH_NAMES = {"face_mesh", "face_mesh_001"}
NOSE_MESH_NAMES = {"nose_mesh", "nose_mesh_001"}
NON_FACE_NAME_INDEX = 95
FACE_NAME_INDEX = 96
EYE_NAME_INDEX = 97
G4_PORT_CONVERTER_MARKER = "expression-face-v9-json-marker-3"
G4_PORT_SCRIPT_VERSION = "1.0.5"
STABLE_CUSTOM_VERTEX_COLOR = (255, 153, 191, 127)
VERTEX_COLOR_MODES = {"native", "constant"}


@dataclass
class RecordRule:
    output_name: str
    material_name: str
    match_names: list[str]
    fallback_degenerate: bool = False
    force_layout_material: tuple[int, int] | None = None
    force_palette_flags: tuple[int, int] | None = None
    force_name_index: int | None = None
    uv_scale: tuple[float, float] = (1.0, 1.0)
    uv_offset: tuple[float, float] = (0.0, 0.0)
    uv_flip: tuple[bool, bool] | None = None
    source_uv_transforms: dict[str, dict[str, list[float]]] | None = None
    uv_fit_display_rect: tuple[float, float, float, float] | None = None
    rigid_joint: str | int | None = None
    palette_joints: list[str | int] | None = None
    auto_palette: bool = False
    secondary_weight_scale: float = 1.0
    weight_anchor_joint: str | int | None = None
    outline_enabled: bool = True
    native_template_record_index: int | None = None


@dataclass
class MaterialOverride:
    material_index: int
    clone_from: int
    texture_refs: list[tuple[int, int]]
    ref_start: int | None = None


@dataclass
class PortConfig:
    model_rel: Path
    native_material_names: list[str]
    records: list[RecordRule]
    texture_replacements: dict[str, str]
    texture_platform: str = "auto"
    material_overrides: list[MaterialOverride] | None = None
    uv_flip: tuple[bool, bool] = (False, False)
    joint_aliases: dict[str, str | int] | None = None
    strict_skinning: bool = True
    generate_tangents: bool = False
    apply_bind_shape: bool = False
    source_mesh_assignments: dict[str, str] | None = None
    joint_position_offsets: dict[str, tuple[float, float, float]] | None = None
    joint_position_transforms: dict[str, tuple[float, ...]] | None = None
    source_joint_position_transforms: dict[str, tuple[float, ...]] | None = None
    producer_plugin_version: str = ""
    producer_expected_script_version: str = ""
    producer_marker: str = ""
    producer_source: str = ""
    unique_material_per_mesh: bool = False
    vertex_color_mode: str = "native"
    vertex_color: tuple[int, int, int, int] = STABLE_CUSTOM_VERTEX_COLOR

    @property
    def common_rel(self) -> Path:
        return Path("common") / self.model_rel.with_suffix(".g4md")

    @property
    def dx11_rel(self) -> Path:
        return Path("dx11") / self.model_rel.with_suffix(".g4tx")

    @property
    def nx_rel(self) -> Path:
        return Path("nx") / self.model_rel.with_suffix(".g4tx")

    def texture_rel(self, raw_root: Path) -> Path:
        candidates = {
            "dx11": self.dx11_rel,
            "nx": self.nx_rel,
        }
        if self.texture_platform in candidates:
            rel = candidates[self.texture_platform]
            if not (raw_root / rel).is_file():
                raise FileNotFoundError(f"{self.texture_platform.upper()} G4TX not found: {raw_root / rel}")
            return rel
        for platform in ("dx11", "nx"):
            rel = candidates[platform]
            if (raw_root / rel).is_file():
                return rel
        raise FileNotFoundError(
            f"G4TX not found in either {raw_root / self.dx11_rel} or {raw_root / self.nx_rel}"
        )


@dataclass
class Vertex:
    position: tuple[float, float, float]
    normal: tuple[float, float, float]
    uv: tuple[float, float]
    influences: tuple[tuple[str, float], ...] = ()
    tangent: tuple[float, float, float] | None = None
    bitangent: tuple[float, float, float] | None = None
    color: tuple[int, int, int, int] | None = None
    color_from_source: bool = False


@dataclass
class Mesh:
    name: str
    vertices: list[Vertex]
    indices: list[int]
    material_index: int
    material_name: str
    source_names: tuple[str, ...] = ()
    uv_transform_trace: tuple[dict[str, object], ...] = ()
    source_record_index: int | None = None


def _source_record_index(mesh: Mesh, fallback: int, record_count: int) -> int:
    index = mesh.source_record_index
    return index if index is not None and 0 <= index < record_count else fallback


@dataclass
class MaterialPlan:
    name: str
    base_texture: str
    texture_refs: list[str]


COMPACT_JOINT_NAMES = (
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


def default_config() -> PortConfig:
    return PortConfig(
        model_rel=MODEL_REL,
        native_material_names=NATIVE_MATERIAL_NAMES,
        records=[
            RecordRule("c01000010_20", "c01000010_20M", ["*"]),
            RecordRule("eye_10", "mouth_10M", ["nose_mesh", "nose_mesh_001"], True, (1, 1)),
            RecordRule(
                "mouth_10",
                "mouth_10M",
                ["face_mesh", "face_mesh_001"],
                False,
                None,
                (0, 0x103),
                (0.0, 1.0),
                (True, True),
            ),
        ],
        texture_replacements={
            "c01000010_20": "hairTexture.png",
            "c01000010_10": "faceTexture.png",
        },
        uv_flip=(True, True),
        vertex_color_mode="constant",
        vertex_color=STABLE_CUSTOM_VERTEX_COLOR,
    )


def normalize_vertex_color(
    value: object, default: tuple[int, int, int, int] = STABLE_CUSTOM_VERTEX_COLOR
) -> tuple[int, int, int, int]:
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("vertex_color must contain exactly four byte values")
    color = tuple(int(component) for component in value)
    if any(component < 0 or component > 255 for component in color):
        raise ValueError("vertex_color components must be in the 0..255 range")
    return color[0], color[1], color[2], color[3]


def load_config(path: Path | None) -> PortConfig:
    if path is None:
        return default_config()
    data = json.loads(path.read_text())
    vertex_color_mode = str(data.get("vertex_color_mode", "native")).lower()
    if vertex_color_mode not in VERTEX_COLOR_MODES:
        raise ValueError(
            f"unsupported vertex_color_mode {vertex_color_mode!r}; "
            f"expected one of {sorted(VERTEX_COLOR_MODES)}"
        )
    records = []
    for item in data["records"]:
        layout_material = item.get("force_layout_material")
        palette_flags = item.get("force_palette_flags")
        records.append(
            RecordRule(
                output_name=item["output_name"],
                material_name=item["material_name"],
                match_names=item.get("match_names", []),
                fallback_degenerate=bool(item.get("fallback_degenerate", False)),
                force_layout_material=tuple(layout_material) if layout_material is not None else None,
                force_palette_flags=tuple(palette_flags) if palette_flags is not None else None,
                force_name_index=item.get("force_name_index"),
                uv_scale=tuple(item.get("uv_scale", [1.0, 1.0])),
                uv_offset=tuple(item.get("uv_offset", [0.0, 0.0])),
                uv_flip=tuple(item["uv_flip"]) if "uv_flip" in item else None,
                source_uv_transforms=dict(item.get("source_uv_transforms", {})),
                uv_fit_display_rect=tuple(item["uv_fit_display_rect"]) if "uv_fit_display_rect" in item else None,
                rigid_joint=item.get("rigid_joint"),
                palette_joints=list(item["palette_joints"]) if "palette_joints" in item else None,
                auto_palette=bool(item.get("auto_palette", False)),
                secondary_weight_scale=float(item.get("secondary_weight_scale", 1.0)),
                weight_anchor_joint=item.get("weight_anchor_joint"),
                outline_enabled=bool(item.get("outline_enabled", True)),
                native_template_record_index=(
                    int(item["native_template_record_index"])
                    if item.get("native_template_record_index") is not None
                    else None
                ),
            )
        )
    return PortConfig(
        model_rel=Path(data["model_rel"]),
        native_material_names=list(data.get("native_material_names", NATIVE_MATERIAL_NAMES)),
        records=records,
        texture_replacements=dict(data.get("texture_replacements", {})),
        texture_platform=str(data.get("texture_platform", "auto")).lower(),
        material_overrides=[
            MaterialOverride(
                material_index=int(item["material_index"]),
                clone_from=int(item["clone_from"]),
                texture_refs=[(int(ref[0]), int(ref[1])) for ref in item.get("texture_refs", [])],
                ref_start=item.get("ref_start"),
            )
            for item in data.get("material_overrides", [])
        ],
        uv_flip=tuple(data.get("uv_flip", [False, False])),
        joint_aliases=dict(data.get("joint_aliases", {})),
        strict_skinning=bool(data.get("strict_skinning", True)),
        generate_tangents=bool(data.get("generate_tangents", False)),
        apply_bind_shape=bool(data.get("apply_bind_shape", False)),
        source_mesh_assignments=dict(data.get("source_mesh_assignments", {})),
        joint_position_offsets={
            str(name): tuple(float(component) for component in value)
            for name, value in dict(data.get("joint_position_offsets", {})).items()
            if isinstance(value, (list, tuple)) and len(value) == 3
        },
        joint_position_transforms={
            str(name): tuple(float(component) for component in value)
            for name, value in dict(data.get("joint_position_transforms", {})).items()
            if isinstance(value, (list, tuple)) and len(value) == 12
        },
        source_joint_position_transforms={
            str(name): tuple(float(component) for component in value)
            for name, value in dict(data.get("source_joint_position_transforms", {})).items()
            if isinstance(value, (list, tuple)) and len(value) == 12
        },
        producer_plugin_version=str(data.get("g4_blender_plugin_version", "")),
        producer_expected_script_version=str(data.get("g4_port_script_version_expected", "")),
        producer_marker=str(data.get("g4_blender_addon_marker", "")),
        producer_source=str(data.get("g4_blender_addon_source", "")),
        unique_material_per_mesh=bool(data.get("unique_material_per_mesh", False)),
        vertex_color_mode=vertex_color_mode,
        vertex_color=normalize_vertex_color(data.get("vertex_color"), STABLE_CUSTOM_VERTEX_COLOR),
    )


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def crc32b(text: str) -> int:
    return zlib.crc32(text.encode("ascii")) & 0xFFFFFFFF


def cstr(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("ascii", errors="replace")


def parse_g4sk(path: Path) -> dict:
    data = path.read_bytes()
    if data[:4] != b"G4SK" or len(data) < 0x40:
        raise ValueError(f"{path} is not a valid G4SK")
    joint_count = struct.unpack_from("<H", data, 0x20)[0]
    section_offsets = [0x40 + struct.unpack_from("<H", data, 0x24 + index * 2)[0] * 4 for index in range(8)]
    name_table = section_offsets[7]
    if name_table + joint_count * 2 > len(data):
        raise ValueError(f"{path} has an invalid G4SK name table")
    names = []
    for index in range(joint_count):
        relative = struct.unpack_from("<H", data, name_table + index * 2)[0]
        names.append(cstr(data, name_table + relative))
    bind_matrices = []
    inverse_bind_matrices = []
    inverse_bind_table = section_offsets[0]
    for index in range(joint_count):
        bind_offset = 0x40 + index * 0x30
        inverse_offset = inverse_bind_table + index * 0x30
        if bind_offset + 0x30 > len(data) or inverse_offset + 0x30 > len(data):
            break
        bind_values = struct.unpack_from("<12f", data, bind_offset)
        inverse_values = struct.unpack_from("<12f", data, inverse_offset)
        bind_matrices.append(tuple(bind_values) + (0.0, 0.0, 0.0, 1.0))
        inverse_bind_matrices.append(tuple(inverse_values) + (0.0, 0.0, 0.0, 1.0))
    max_inverse_error = 0.0
    identity = identity_matrix()
    for bind, inverse in zip(bind_matrices, inverse_bind_matrices):
        product = matrix_multiply(bind, inverse)
        max_inverse_error = max(max_inverse_error, max(abs(value - expected) for value, expected in zip(product, identity)))
    return {
        "path": str(path),
        "joint_count": joint_count,
        "names": names,
        "matrix_count": len(bind_matrices),
        "max_bind_inverse_error": max_inverse_error,
    }


def resolve_expected_skeleton(model_rel: Path, raw_root: Path, chara_model: Path) -> dict:
    result = {"resolved": False, "expected_path": None, "body_id": None, "source": str(chara_model)}
    if not chara_model.exists():
        return result
    root = ET.parse(chara_model).getroot()
    model_path = model_rel.with_suffix(".g4md")
    model_candidates = {model_path.as_posix(), "_" + model_path.as_posix()}
    if model_path.parts and model_path.parts[0] == "chr":
        without_chr = Path(*model_path.parts[1:]).as_posix()
        model_candidates.update({without_chr, "_" + without_chr})
    body_id = None
    for entry in root.findall("entry"):
        if entry.get("name") != "CHARA_MODEL_INFO":
            continue
        values = {int(node.get("index")): node.text or "" for node in entry.findall("./values/value")}
        if any(values.get(index, "").replace("\\", "/") in model_candidates for index in (1, 10)):
            body_id = values.get(4)
            break
    result["body_id"] = int(body_id) if body_id and body_id.lstrip("-").isdigit() else None
    if body_id is None:
        return result
    body_path = None
    for entry in root.findall("entry"):
        if entry.get("name") != "CHARA_BODY_INFO":
            continue
        values = {int(node.get("index")): node.text or "" for node in entry.findall("./values/value")}
        if values.get(0) == body_id:
            body_path = values.get(1, "").replace("\\", "/")
            break
    if not body_path:
        return result
    relative = Path(body_path).with_suffix(".g4sk")
    if relative.parts and relative.parts[0] == "_common":
        relative = Path(*relative.parts[1:])
    expected = raw_root / "common" / "chr" / relative
    result["expected_path"] = str(expected)
    result["resolved"] = expected.exists()
    return result


def validate_skeleton_palettes(
    palettes: list[list[int]], skeleton_path: Path | None, expected: dict
) -> dict:
    if skeleton_path is None and expected.get("resolved"):
        skeleton_path = Path(expected["expected_path"])
    used = sorted({joint for palette in palettes for joint in palette})
    if skeleton_path is None:
        invalid_compact = [joint for joint in used if joint >= len(COMPACT_JOINT_NAMES)]
        return {
            "status": "expected_missing" if expected.get("expected_path") else "unresolved",
            "expected_path": expected.get("expected_path"),
            "checked_compact_indices": len(used),
            "invalid_compact_indices": invalid_compact,
        }
    skeleton = parse_g4sk(skeleton_path)
    name_set = set(skeleton["names"])
    required_names = [COMPACT_JOINT_NAMES[index] for index in used if index < len(COMPACT_JOINT_NAMES)]
    missing_names = [name for name in required_names if name not in name_set]
    return {
        "status": "ok" if not missing_names and skeleton["max_bind_inverse_error"] <= 1e-3 else "missing_joints" if missing_names else "invalid_bind_matrices",
        "path": str(skeleton_path),
        "joint_count": skeleton["joint_count"],
        "required_names": required_names,
        "missing_names": missing_names,
        "matrix_count": skeleton["matrix_count"],
        "max_bind_inverse_error": skeleton["max_bind_inverse_error"],
        "name_to_g4sk_index": {name: skeleton["names"].index(name) for name in required_names if name in name_set},
    }


def pack_snorm16(value: float) -> int:
    value = max(-1.0, min(1.0, value))
    return int(round(value * 32767.0)) & 0xFFFF


def pack_vertex(
    vertex: Vertex,
    influences: list[tuple[int, float]] | None = None,
    uv_flip: tuple[bool, bool] = (False, False),
    uv_scale: tuple[float, float] = (1.0, 1.0),
    uv_offset: tuple[float, float] = (0.0, 0.0),
    fallback_color: tuple[int, int, int, int] = (255, 255, 255, 255),
    uv_format_id: int = 14,
) -> bytes:
    out = bytearray(0x44)
    struct.pack_into("<3f", out, 0x00, *vertex.position)
    struct.pack_into("<4H", out, 0x0C, *(pack_snorm16(v) for v in (*vertex.normal, 0.0)))
    tangent = vertex.tangent or (1.0, 0.0, 0.0)
    bitangent = vertex.bitangent or (0.0, 1.0, 0.0)
    if vertex.tangent is not None and uv_flip[0]:
        tangent = tuple(-value for value in tangent)
    if vertex.bitangent is not None and uv_flip[1]:
        bitangent = tuple(-value for value in bitangent)
    struct.pack_into("<4H", out, 0x14, *(pack_snorm16(v) for v in (*tangent, 0.0)))
    struct.pack_into("<4H", out, 0x1C, *(pack_snorm16(v) for v in (*bitangent, 0.0)))
    packed_influences = quantize_influences(influences or [(0, 1.0)])
    weights = [weight for _, weight in packed_influences]
    joints = [joint for joint, _ in packed_influences]
    weights.extend([0] * (8 - len(weights)))
    # Native G4MG records use joint 0 for unused slots.  Keep the padding
    # inside every valid palette: Metal may fetch all eight indices even when
    # the corresponding weights are zero, so 0xFF can become an out-of-range
    # palette read on macOS.
    while len(joints) < 4:
        joints.append(0)
    joints.extend([0] * (8 - len(joints)))
    struct.pack_into("<8H", out, 0x24, *weights)
    struct.pack_into("<8B", out, 0x34, *joints)
    struct.pack_into("<4B", out, 0x3C, *(vertex.color or fallback_color))
    local_uv = (1.0 - vertex.uv[0] if uv_flip[0] else vertex.uv[0], 1.0 - vertex.uv[1] if uv_flip[1] else vertex.uv[1])
    uv = (local_uv[0] * uv_scale[0] + uv_offset[0], local_uv[1] * uv_scale[1] + uv_offset[1])
    if uv_format_id in (18, 20):
        # Signed-normalized 16-bit.  eye_10 in c11010060 uses format 18.
        # Writing these values as UNORM16 makes the importer reinterpret the
        # upper half of the range as negative and stretches the UV island.
        u = int(round(max(-1.0, min(1.0, uv[0])) * 32767.0))
        v = int(round(max(-1.0, min(1.0, uv[1])) * 32767.0))
        struct.pack_into("<2h", out, 0x40, u, v)
    elif uv_format_id == 12:
        u = int(round(max(0.0, min(1.0, uv[0])) * 255.0))
        v = int(round(max(0.0, min(1.0, uv[1])) * 255.0))
        struct.pack_into("<2B", out, 0x40, u, v)
        struct.pack_into("<H", out, 0x42, 0)
    else:
        # Native format 14 and the legacy fallback are unsigned-normalized.
        u = int(round(max(0.0, min(1.0, uv[0])) * 65535.0))
        v = int(round(max(0.0, min(1.0, uv[1])) * 65535.0))
        struct.pack_into("<2H", out, 0x40, u, v)
    return bytes(out)


def quantize_influences(influences: list[tuple[int, float]]) -> list[tuple[int, int]]:
    combined: dict[int, float] = {}
    for joint, weight in influences:
        if 0 <= joint < 0xFF and weight > 0.0:
            combined[joint] = combined.get(joint, 0.0) + weight
    selected = sorted(combined.items(), key=lambda item: (-item[1], item[0]))[:8]
    total = sum(weight for _, weight in selected)
    if total <= 0.0:
        return [(0, 0xFFFF)]
    exact = [(joint, weight * 65535.0 / total) for joint, weight in selected]
    quantized = [(joint, int(value)) for joint, value in exact]
    remainder = 65535 - sum(value for _, value in quantized)
    order = sorted(range(len(exact)), key=lambda index: exact[index][1] - quantized[index][1], reverse=True)
    for index in order[:remainder]:
        joint, value = quantized[index]
        quantized[index] = (joint, value + 1)
    return quantized


def vector_sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def vector_cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def vector_dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vector_normalize(v: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if length <= 0.000001:
        return 0.0, 1.0, 0.0
    return v[0] / length, v[1] / length, v[2] / length


def triangle_tangent_space(vertices: list[tuple]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    p0, p1, p2 = (vertex[0] for vertex in vertices)
    uv0, uv1, uv2 = (vertex[2] for vertex in vertices)
    edge1 = vector_sub(p1, p0)
    edge2 = vector_sub(p2, p0)
    du1, dv1 = uv1[0] - uv0[0], uv1[1] - uv0[1]
    du2, dv2 = uv2[0] - uv0[0], uv2[1] - uv0[1]
    determinant = du1 * dv2 - du2 * dv1
    if abs(determinant) <= 1e-12:
        return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
    scale = 1.0 / determinant
    tangent = vector_normalize(tuple((edge1[i] * dv2 - edge2[i] * dv1) * scale for i in range(3)))
    bitangent = vector_normalize(tuple((edge2[i] * du1 - edge1[i] * du2) * scale for i in range(3)))
    return tangent, bitangent


def vertex_tangent_space(
    normal: tuple[float, float, float],
    tangent: tuple[float, float, float],
    bitangent: tuple[float, float, float],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    tangent = vector_normalize(tuple(tangent[i] - normal[i] * vector_dot(normal, tangent) for i in range(3)))
    handedness = -1.0 if vector_dot(vector_cross(normal, tangent), bitangent) < 0.0 else 1.0
    return tangent, tuple(value * handedness for value in vector_normalize(vector_cross(normal, tangent)))


def calibration_mesh() -> list[Mesh]:
    points = [
        (-0.35, -0.22, 0.0),
        (0.35, -0.22, 0.0),
        (0.35, 0.22, 0.0),
        (-0.35, 0.22, 0.0),
        (0.0, 0.0, 0.28),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),
        (0, 4, 1),
        (1, 4, 2),
        (2, 4, 3),
        (3, 4, 0),
    ]
    uvs = [(0.15, 0.85), (0.85, 0.85), (0.85, 0.25), (0.15, 0.25), (0.5, 0.05)]
    vertices: list[Vertex] = []
    indices: list[int] = []
    for face in faces:
        p0, p1, p2 = (points[index] for index in face)
        normal = vector_normalize(vector_cross(vector_sub(p1, p0), vector_sub(p2, p0)))
        base = len(vertices)
        for index in face:
            vertices.append(Vertex(points[index], normal, uvs[index]))
        indices.extend([base, base + 1, base + 2])
    return [Mesh("c01000010_20", vertices, indices, 0, "c01000010_20")]


def clean_symbol(value: str) -> str:
    value = source_ref(value)
    if value.endswith("-material"):
        value = value[: -len("-material")]
    return re.sub(r"[^0-9A-Za-z_]+", "_", value).strip("_") or "material"


def ns_name(element: ET.Element, local: str) -> str:
    if element.tag.startswith("{"):
        return element.tag[: element.tag.index("}") + 1] + local
    return local


def parse_float_array(root: ET.Element, source_id: str) -> list[float]:
    source = root.find(f".//*[@id='{source_id}']")
    if source is None:
        return []
    array = source.find(ns_name(source, "float_array"))
    if array is None or not array.text:
        return []
    return [float(value) for value in array.text.split()]


def parse_float_source(root: ET.Element, source_id: str, component_hint: int | None = None) -> list[tuple[float, ...]]:
    source = root.find(f".//*[@id='{source_id}']")
    if source is None:
        return []
    array = source.find(ns_name(source, "float_array"))
    if array is None or not array.text:
        return []
    values = [float(value) for value in array.text.split()]
    accessor = source.find(".//" + ns_name(source, "accessor"))
    stride = int(accessor.attrib.get("stride", "1")) if accessor is not None else (component_hint or 1)
    offset = int(accessor.attrib.get("offset", "0")) if accessor is not None else 0
    count = int(accessor.attrib.get("count", str(max(0, (len(values) - offset) // stride)))) if accessor is not None else len(values)
    params = accessor.findall(ns_name(accessor, "param")) if accessor is not None else []
    component_count = len(params) or component_hint or stride
    return [
        tuple(values[offset + index * stride : offset + index * stride + component_count])
        for index in range(count)
        if offset + index * stride + component_count <= len(values)
    ]


def parse_name_array(root: ET.Element, source_id: str) -> list[str]:
    source = root.find(f".//*[@id='{source_id}']")
    if source is None:
        return []
    for array_name in ("Name_array", "IDREF_array"):
        array = source.find(ns_name(source, array_name))
        if array is not None and array.text:
            return array.text.split()
    return []


def source_ref(value: str) -> str:
    return value[1:] if value.startswith("#") else value


def parse_dae_skin(root: ET.Element) -> dict[str, list[tuple[tuple[str, float], ...]]]:
    skins: dict[str, list[tuple[tuple[str, float], ...]]] = {}
    for controller in root.findall(".//" + ns_name(root, "controller")):
        skin = controller.find(ns_name(controller, "skin"))
        if skin is None:
            continue
        geometry_id = source_ref(skin.attrib.get("source", ""))
        vertex_weights = skin.find(ns_name(skin, "vertex_weights"))
        if not geometry_id or vertex_weights is None:
            continue
        inputs = {
            node.attrib.get("semantic", ""): (
                source_ref(node.attrib.get("source", "")),
                int(node.attrib.get("offset", "0")),
            )
            for node in vertex_weights.findall(ns_name(vertex_weights, "input"))
        }
        if "JOINT" not in inputs or "WEIGHT" not in inputs:
            continue
        joint_source, joint_offset = inputs["JOINT"]
        weight_source, weight_offset = inputs["WEIGHT"]
        joint_names = parse_name_array(root, joint_source)
        weight_values = parse_float_source(root, weight_source, 1)
        weights = [value[0] for value in weight_values if value]
        vcount_node = vertex_weights.find(ns_name(vertex_weights, "vcount"))
        values_node = vertex_weights.find(ns_name(vertex_weights, "v"))
        if vcount_node is None or values_node is None or not vcount_node.text or not values_node.text:
            continue
        counts = [int(value) for value in vcount_node.text.split()]
        values = [int(value) for value in values_node.text.split()]
        stride = max(offset for _, offset in inputs.values()) + 1
        cursor = 0
        vertex_influences: list[tuple[tuple[str, float], ...]] = []
        for count in counts:
            influences: list[tuple[str, float]] = []
            for _ in range(count):
                packed = values[cursor : cursor + stride]
                cursor += stride
                if joint_offset >= len(packed) or weight_offset >= len(packed):
                    continue
                joint_index = packed[joint_offset]
                weight_index = packed[weight_offset]
                if joint_index < len(joint_names) and weight_index < len(weights) and weights[weight_index] > 0.0:
                    influences.append((joint_names[joint_index], weights[weight_index]))
            influences.sort(key=lambda item: item[1], reverse=True)
            vertex_influences.append(tuple(influences[:8]))
        skins[geometry_id] = vertex_influences
    return skins


def parse_dae_bind_shapes(root: ET.Element) -> dict[str, tuple[float, ...]]:
    matrices: dict[str, tuple[float, ...]] = {}
    for controller in root.findall(".//" + ns_name(root, "controller")):
        skin = controller.find(ns_name(controller, "skin"))
        if skin is None:
            continue
        node = skin.find(ns_name(skin, "bind_shape_matrix"))
        if node is not None and node.text:
            matrices[source_ref(skin.attrib.get("source", ""))] = collada_matrix(
                [float(value) for value in node.text.split()]
            )
    return matrices


def canonical_mesh_name(name: str) -> str:
    """Normalize Blender/Collada duplicate and geometry suffixes deterministically.

    Baked exports commonly turn ``face_mesh.001`` into names such as
    ``face_mesh_006``.  Remove both kinds of generated suffixes repeatedly so
    the original sidecar identity can still be recovered.
    """
    name = clean_symbol(name)
    previous = None
    while name != previous:
        previous = name
        name = re.sub(r"_\d{3}$", "", name)
        name = re.sub(r"_mesh$", "", name)
    return name


def sidecar_exact_names(name: str) -> set[str]:
    clean = clean_symbol(name)
    names = {clean}
    if clean.endswith("_mesh"):
        names.add(clean[: -len("_mesh")])
    if clean.endswith("_mesh_001"):
        names.add(clean[: -len("_mesh_001")] + "_001")
    return {item for item in names if item}


def load_weight_sidecar(path: Path | None) -> list[dict]:
    if path is None:
        return []
    data = json.loads(path.read_text())
    meshes = data.get("meshes")
    if not isinstance(meshes, list):
        raise ValueError("weight sidecar must contain a meshes array")
    return [{**mesh, "_sidecar_index": index} for index, mesh in enumerate(meshes)]


def sidecar_influences(mesh: dict, vertex_count: int):
    influences = mesh.get("influences")
    if not isinstance(influences, list) or len(influences) != vertex_count:
        raise ValueError(f"weight sidecar entry {mesh.get('name')!r} has invalid influence count")
    return [
        tuple((str(name), float(weight)) for name, weight in vertex if float(weight) > 0.0)
        for vertex in influences
    ]


def sidecar_mesh_for_geometry(
    sidecar_meshes: list[dict],
    geometry: ET.Element,
    vertex_count: int,
    used_sidecar_indices: set[int] | None = None,
    instance_name: str = "",
) -> dict | None:
    """Resolve one exported DAE geometry back to its original Blender object.

    Blender/Collada may sanitize duplicate object names (``Part.001`` ->
    ``Part_001``) or reuse a geometry-style name.  The weight sidecar is the
    authoritative source identity because it is written directly from the
    Blender objects before the DAE is parsed.
    """
    if not sidecar_meshes:
        return None
    used_sidecar_indices = used_sidecar_indices if used_sidecar_indices is not None else set()
    available = [mesh for mesh in sidecar_meshes if int(mesh.get("_sidecar_index", -1)) not in used_sidecar_indices]
    if not available:
        return None

    exact_names = (
        sidecar_exact_names(geometry.attrib.get("id", ""))
        | sidecar_exact_names(geometry.attrib.get("name", ""))
        | sidecar_exact_names(instance_name)
    )
    exact = [mesh for mesh in available if clean_symbol(str(mesh.get("name", ""))) in exact_names]
    exact_with_count = [mesh for mesh in exact if int(mesh.get("vertex_count", -1)) == vertex_count]
    if exact_with_count:
        chosen = exact_with_count[0]
    elif len(exact) == 1:
        # Modifiers can change the DAE vertex count.  An unambiguous exact name
        # is still safe for source identity, even when its skin sidecar cannot
        # be reused for influences.
        chosen = exact[0]
    else:
        names = {
            canonical_mesh_name(geometry.attrib.get("id", "")),
            canonical_mesh_name(geometry.attrib.get("name", "")),
            canonical_mesh_name(instance_name),
        }
        canonical_all = [
            mesh
            for mesh in available
            if canonical_mesh_name(str(mesh.get("name", ""))) in names
        ]
        canonical_exact_count = [
            mesh for mesh in canonical_all
            if int(mesh.get("vertex_count", -1)) == vertex_count
        ]
        if len(canonical_exact_count) == 1:
            chosen = canonical_exact_count[0]
        else:
            # Applying outline/solidify modifiers before Collada export often
            # multiplies the evaluated vertex count (2x in the common outline
            # case).  Use that stable ratio to distinguish source objects that
            # share a canonical Blender base name.
            canonical_multiple_count = []
            for mesh in canonical_all:
                source_count = int(mesh.get("vertex_count", -1))
                if source_count > 0 and vertex_count >= source_count and vertex_count % source_count == 0:
                    ratio = vertex_count // source_count
                    if 1 <= ratio <= 8:
                        canonical_multiple_count.append(mesh)
            if len(canonical_multiple_count) == 1:
                chosen = canonical_multiple_count[0]
            elif len(canonical_all) == 1:
                # An unambiguous canonical identity remains safe even when an
                # arbitrary modifier changed the evaluated topology.
                chosen = canonical_all[0]
            else:
                by_count = [mesh for mesh in available if int(mesh.get("vertex_count", -1)) == vertex_count]
                chosen = by_count[0] if len(by_count) == 1 else None

    if chosen is None:
        return None
    used_sidecar_indices.add(int(chosen.get("_sidecar_index", -1)))
    return chosen


def sidecar_skin_for_geometry(
    sidecar_meshes: list[dict],
    geometry: ET.Element,
    vertex_count: int,
    used_sidecar_indices: set[int] | None = None,
):
    chosen = sidecar_mesh_for_geometry(sidecar_meshes, geometry, vertex_count, used_sidecar_indices)
    if chosen is None:
        return None
    if int(chosen.get("vertex_count", -1)) != vertex_count:
        return None
    return sidecar_influences(chosen, vertex_count)


def identity_matrix() -> tuple[float, ...]:
    return (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def matrix_multiply(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(sum(a[row * 4 + k] * b[k * 4 + column] for k in range(4)) for row in range(4) for column in range(4))


def collada_matrix(values: list[float]) -> tuple[float, ...]:
    if len(values) != 16:
        raise ValueError(f"Collada matrix requires 16 values, got {len(values)}")
    return tuple(values[column * 4 + row] for row in range(4) for column in range(4))


def transform_point(matrix: tuple[float, ...], point: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = point
    return tuple(matrix[row * 4] * x + matrix[row * 4 + 1] * y + matrix[row * 4 + 2] * z + matrix[row * 4 + 3] for row in range(3))  # type: ignore[return-value]


def transform_direction(matrix: tuple[float, ...], direction: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = direction
    return vector_normalize(tuple(matrix[row * 4] * x + matrix[row * 4 + 1] * y + matrix[row * 4 + 2] * z for row in range(3)))  # type: ignore[arg-type,return-value]


def transform_normal(matrix: tuple[float, ...], normal: tuple[float, float, float]) -> tuple[float, float, float]:
    a, b, c, d, e, f, g, h, i = (
        matrix[0], matrix[1], matrix[2], matrix[4], matrix[5], matrix[6], matrix[8], matrix[9], matrix[10]
    )
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(determinant) <= 1e-12:
        return transform_direction(matrix, normal)
    inverse_transpose = (
        (e * i - f * h) / determinant, (f * g - d * i) / determinant, (d * h - e * g) / determinant,
        (c * h - b * i) / determinant, (a * i - c * g) / determinant, (b * g - a * h) / determinant,
        (b * f - c * e) / determinant, (c * d - a * f) / determinant, (a * e - b * d) / determinant,
    )
    x, y, z = normal
    return vector_normalize(tuple(inverse_transpose[row * 3] * x + inverse_transpose[row * 3 + 1] * y + inverse_transpose[row * 3 + 2] * z for row in range(3)))  # type: ignore[arg-type,return-value]


def dae_geometry_transforms(root: ET.Element) -> dict[str, tuple[float, ...]]:
    transforms: dict[str, tuple[float, ...]] = {}
    visual_scene = root.find(".//" + ns_name(root, "visual_scene"))
    if visual_scene is None:
        return transforms

    def visit(node: ET.Element, parent: tuple[float, ...]) -> None:
        local = identity_matrix()
        for child in node:
            local_name = child.tag.rsplit("}", 1)[-1]
            values = [float(value) for value in (child.text or "").split()]
            if local_name == "matrix" and values:
                local = matrix_multiply(local, collada_matrix(values))
            elif local_name == "translate" and len(values) == 3:
                local = matrix_multiply(local, (1, 0, 0, values[0], 0, 1, 0, values[1], 0, 0, 1, values[2], 0, 0, 0, 1))
            elif local_name == "scale" and len(values) == 3:
                local = matrix_multiply(local, (values[0], 0, 0, 0, 0, values[1], 0, 0, 0, 0, values[2], 0, 0, 0, 0, 1))
            elif local_name == "rotate" and len(values) == 4:
                x, y, z = vector_normalize((values[0], values[1], values[2]))
                angle = math.radians(values[3]); c, s, t = math.cos(angle), math.sin(angle), 1.0 - math.cos(angle)
                local = matrix_multiply(local, (t*x*x+c, t*x*y-s*z, t*x*z+s*y, 0, t*x*y+s*z, t*y*y+c, t*y*z-s*x, 0, t*x*z-s*y, t*y*z+s*x, t*z*z+c, 0, 0, 0, 0, 1))
        world = matrix_multiply(parent, local)
        for instance_name in ("instance_geometry", "instance_controller"):
            for instance in node.findall(ns_name(node, instance_name)):
                target = source_ref(instance.attrib.get("url", ""))
                previous = transforms.get(target)
                if previous is not None and previous != world:
                    raise ValueError(f"geometry/controller {target!r} is instanced with multiple transforms")
                transforms[target] = world
        for child in node.findall(ns_name(node, "node")):
            visit(child, world)

    for node in visual_scene.findall(ns_name(visual_scene, "node")):
        visit(node, identity_matrix())
    for controller in root.findall(".//" + ns_name(root, "controller")):
        controller_id = controller.attrib.get("id", "")
        skin = controller.find(ns_name(controller, "skin"))
        if skin is not None and controller_id in transforms:
            transforms[source_ref(skin.attrib.get("source", ""))] = transforms[controller_id]
    return transforms


def dae_geometry_instance_names(root: ET.Element) -> dict[str, str]:
    names: dict[str, str] = {}
    visual_scene = root.find(".//" + ns_name(root, "visual_scene"))
    if visual_scene is None:
        return names

    def assign(target: str, name: str) -> None:
        if target and name and target not in names:
            names[target] = clean_symbol(name)

    def visit(node: ET.Element) -> None:
        node_name = node.attrib.get("name") or node.attrib.get("id", "")
        for instance in node.findall(ns_name(node, "instance_geometry")):
            target = source_ref(instance.attrib.get("url", ""))
            assign(target, instance.attrib.get("name") or node_name)
        for instance in node.findall(ns_name(node, "instance_controller")):
            target = source_ref(instance.attrib.get("url", ""))
            assign(target, instance.attrib.get("name") or node_name)
        for child in node.findall(ns_name(node, "node")):
            visit(child)

    for node in visual_scene.findall(ns_name(visual_scene, "node")):
        visit(node)
    for controller in root.findall(".//" + ns_name(root, "controller")):
        controller_id = controller.attrib.get("id", "")
        skin = controller.find(ns_name(controller, "skin"))
        if skin is not None and controller_id in names:
            assign(source_ref(skin.attrib.get("source", "")), names[controller_id])
    return names


def parse_dae(
    path: Path,
    model_stem: str,
    preferred_texcoord_set: int = 0,
    weight_sidecar: Path | None = None,
    generate_tangents: bool = False,
    apply_bind_shape: bool = False,
) -> list[Mesh]:
    root = ET.parse(path).getroot()
    skins = parse_dae_skin(root)
    sidecar_meshes = load_weight_sidecar(weight_sidecar)
    used_sidecar_indices: set[int] = set()
    geometry_transforms = dae_geometry_transforms(root)
    geometry_instance_names = dae_geometry_instance_names(root)
    bind_shapes = parse_dae_bind_shapes(root)
    meshes: list[Mesh] = []
    material_indices: dict[str, int] = {}
    for geometry in root.findall(".//" + ns_name(root, "geometry")):
        geometry_id = geometry.attrib.get("id", "")
        geometry_transform = geometry_transforms.get(geometry_id, identity_matrix())
        if apply_bind_shape and geometry_id in bind_shapes:
            geometry_transform = matrix_multiply(geometry_transform, bind_shapes[geometry_id])
        skin_influences = skins.get(geometry_id, [])
        mesh_node = geometry.find(ns_name(geometry, "mesh"))
        if mesh_node is None:
            continue

        vertices_map: dict[str, str] = {}
        for vertices in mesh_node.findall(ns_name(mesh_node, "vertices")):
            position_input = vertices.find(ns_name(vertices, "input") + "[@semantic='POSITION']")
            if position_input is not None:
                vertices_map[vertices.attrib["id"]] = source_ref(position_input.attrib["source"])

        position_sources = list(vertices_map.values())
        position_count = len(parse_float_source(root, position_sources[0], 3)) if position_sources else 0
        sidecar_mesh = sidecar_mesh_for_geometry(
            sidecar_meshes,
            geometry,
            position_count,
            used_sidecar_indices,
            geometry_instance_names.get(geometry_id, ""),
        )
        sidecar_source_name = str(sidecar_mesh.get("name", "")) if sidecar_mesh is not None else ""
        if not skin_influences and sidecar_mesh is not None and int(sidecar_mesh.get("vertex_count", -1)) == position_count:
            skin_influences = sidecar_influences(sidecar_mesh, position_count)

        for primitive_name in ("triangles", "polylist"):
            for primitive in mesh_node.findall(ns_name(mesh_node, primitive_name)):
                inputs = []
                for input_node in primitive.findall(ns_name(primitive, "input")):
                    semantic = input_node.attrib.get("semantic", "")
                    source = source_ref(input_node.attrib.get("source", ""))
                    if semantic == "VERTEX":
                        source = vertices_map.get(source, source)
                    texcoord_set = int(input_node.attrib.get("set", "0")) if semantic.startswith("TEXCOORD") else 0
                    inputs.append((semantic, source, int(input_node.attrib.get("offset", "0")), texcoord_set))
                stride = max((item[2] for item in inputs), default=0) + 1
                data_node = primitive.find(ns_name(primitive, "p"))
                if data_node is None or not data_node.text:
                    continue
                raw = [int(value) for value in data_node.text.split()]

                vcounts: list[int]
                if primitive_name == "polylist":
                    vcount_node = primitive.find(ns_name(primitive, "vcount"))
                    if vcount_node is None or not vcount_node.text:
                        continue
                    vcounts = [int(value) for value in vcount_node.text.split()]
                else:
                    vcounts = [3] * int(primitive.attrib.get("count", "0"))

                sources = {
                    source: parse_float_source(
                        root,
                        source,
                        2 if semantic.startswith("TEXCOORD") else 3 if semantic in {"VERTEX", "POSITION", "NORMAL"} else None,
                    )
                    for semantic, source, _, _ in inputs
                }
                vertices: list[Vertex] = []
                indices: list[int] = []
                vertex_cache: dict[tuple, int] = {}
                cursor = 0
                for vcount in vcounts:
                    face_indices = []
                    for _ in range(vcount):
                        packed = raw[cursor : cursor + stride]
                        cursor += stride
                        pos = (0.0, 0.0, 0.0)
                        uv = (0.5, 0.5)
                        normal = None
                        for semantic, source, offset, texcoord_set in inputs:
                            index = packed[offset]
                            values = sources.get(source, [])
                            if semantic in {"VERTEX", "POSITION"} and index < len(values) and len(values[index]) >= 3:
                                pos = transform_point(geometry_transform, tuple(values[index][:3]))  # type: ignore[arg-type]
                            elif semantic == "NORMAL" and index < len(values) and len(values[index]) >= 3:
                                normal = transform_normal(geometry_transform, tuple(values[index][:3]))  # type: ignore[arg-type]
                            elif (
                                semantic.startswith("TEXCOORD")
                                and texcoord_set == preferred_texcoord_set
                                and index < len(values)
                                and len(values[index]) >= 2
                            ):
                                uv = (values[index][0], values[index][1])
                        position_index = packed[next((offset for semantic, _, offset, _ in inputs if semantic in {"VERTEX", "POSITION"}), 0)]
                        influences = skin_influences[position_index] if position_index < len(skin_influences) else ()
                        face_indices.append((pos, normal, uv, influences))
                    if len(face_indices) < 3:
                        continue
                    for tri in range(1, len(face_indices) - 1):
                        tri_vertices = [face_indices[0], face_indices[tri], face_indices[tri + 1]]
                        fallback_normal = vector_normalize(
                            vector_cross(
                                vector_sub(tri_vertices[1][0], tri_vertices[0][0]),
                                vector_sub(tri_vertices[2][0], tri_vertices[0][0]),
                            )
                        )
                        tangent, bitangent = triangle_tangent_space(tri_vertices) if generate_tangents else (None, None)
                        tri_indices = []
                        for pos, normal, uv, influences in tri_vertices:
                            vertex_normal = vector_normalize(normal or fallback_normal)
                            vertex_tangent, vertex_bitangent = (
                                vertex_tangent_space(vertex_normal, tangent, bitangent)
                                if tangent is not None and bitangent is not None
                                else (None, None)
                            )
                            vertex = Vertex(pos, vertex_normal, uv, influences, vertex_tangent, vertex_bitangent)
                            key = (
                                vertex.position,
                                vertex.normal,
                                vertex.uv,
                                vertex.influences,
                                vertex.tangent,
                                vertex.bitangent,
                            )
                            vertex_index = vertex_cache.get(key)
                            if vertex_index is None:
                                vertex_index = len(vertices)
                                vertex_cache[key] = vertex_index
                                vertices.append(vertex)
                            tri_indices.append(vertex_index)
                        indices.extend(tri_indices)

                if vertices:
                    material_name = clean_symbol(primitive.attrib.get("material", "material"))
                    if material_name not in material_indices:
                        material_indices[material_name] = len(material_indices)
                    mesh_name = clean_symbol(
                        geometry_instance_names.get(geometry_id)
                        or geometry.attrib.get("name")
                        or geometry.attrib.get("id")
                        or material_name
                    )
                    source_names = (sidecar_source_name,) if sidecar_source_name else ()
                    meshes.append(Mesh(mesh_name, vertices, indices, material_indices[material_name], material_name, source_names))
    return meshes or calibration_mesh()


def build_offset_string_table(names: list[str]) -> bytes:
    table_size = len(names) * 2
    strings = bytearray()
    offsets = []
    for name in names:
        offsets.append(table_size + len(strings))
        strings.extend(name.encode("ascii") + b"\0")
    out = bytearray()
    for offset in offsets:
        out.extend(struct.pack("<H", offset))
    out.extend(strings)
    return bytes(out)


def append_aligned(buf: bytearray, boundary: int = 0x10) -> int:
    while len(buf) % boundary:
        buf.append(0)
    return len(buf)


def build_g4mg(
    meshes: list[Mesh],
    uv_flip: tuple[bool, bool] = (False, False),
    record_rules: list[RecordRule] | None = None,
    palettes: list[list[int]] | None = None,
    joint_aliases: dict[str, str | int] | None = None,
    native_joint_indices: dict[str, int] | None = None,
    fallback_colors: list[tuple[int, int, int, int]] | None = None,
    uv_formats: list[int] | None = None,
    joint_position_offsets: dict[str, tuple[float, float, float]] | None = None,
    joint_position_transforms: dict[str, tuple[float, ...]] | None = None,
    source_joint_position_transforms: dict[str, tuple[float, ...]] | None = None,
) -> tuple[bytes, list[dict]]:
    buf = bytearray()
    records = []
    for mesh_index, mesh in enumerate(meshes):
        source_index = _source_record_index(mesh, mesh_index, len(record_rules or meshes))
        source_rule = (
            record_rules[source_index]
            if record_rules is not None and source_index < len(record_rules)
            else None
        )
        vertex_offset = len(buf)
        uv_scale = source_rule.uv_scale if source_rule is not None else (1.0, 1.0)
        uv_offset = source_rule.uv_offset if source_rule is not None else (0.0, 0.0)
        record_uv_flip = (
            source_rule.uv_flip
            if source_rule is not None and source_rule.uv_flip is not None
            else uv_flip
        )
        rule = source_rule
        uv_format_id = uv_formats[source_index] if uv_formats is not None and source_index < len(uv_formats) else 14
        # Palettes are resolved for the output mesh itself because a split
        # chunk can expose joints that were absent from its sibling chunk.
        # The native source index only selects the baseline palette in
        # configured_record_palettes().
        palette = palettes[mesh_index] if palettes is not None and mesh_index < len(palettes) else []
        resolved = [
            resolve_vertex_influences(
                vertex, palette, rule, joint_aliases or {}, native_joint_indices
            )
            for vertex in mesh.vertices
        ]
        unresolved = sum(item[1] for item in resolved)
        fallback_color = fallback_colors[source_index] if fallback_colors and source_index < len(fallback_colors) else (255, 255, 255, 255)
        for vertex, (influences, _) in zip(mesh.vertices, resolved):
            adjusted_vertex = vertex
            if (joint_position_offsets or joint_position_transforms or source_joint_position_transforms) and influences:
                offset = [0.0, 0.0, 0.0]
                normal = [0.0, 0.0, 0.0]
                tangent = [0.0, 0.0, 0.0] if vertex.tangent is not None else None
                bitangent = [0.0, 0.0, 0.0] if vertex.bitangent is not None else None
                names_by_index = {index: name for name, index in (native_joint_indices or {}).items()}
                source_transforms = source_joint_position_transforms or {}
                source_weights = {
                    name: weight
                    for name, weight in vertex.influences
                    if compact_joint_index(name, joint_aliases, native_joint_indices=native_joint_indices) is not None
                }
                for local_joint, weight in influences:
                    name = names_by_index.get(palette[local_joint]) if local_joint < len(palette) else None
                    # Keep roll/helper bones distinct while rebasing rest
                    # space.  They may later collapse to one G4 joint, but
                    # their source bind matrices are not interchangeable.
                    source_entries = [
                        (source_transforms[source_name], source_weight)
                        for source_name, source_weight in source_weights.items()
                        if source_name in source_transforms
                        and palette_compatible_joint(
                            compact_joint_index(source_name, joint_aliases, native_joint_indices=native_joint_indices),
                            palette, native_joint_indices or {},
                        ) == palette[local_joint]
                    ]
                    transform = (joint_position_transforms or {}).get(name or "")
                    if source_entries:
                        total_source_weight = sum(source_weight for _, source_weight in source_entries)
                        transforms = [
                            (source_transform, source_weight / total_source_weight * weight)
                            for source_transform, source_weight in source_entries
                        ]
                    elif transform is not None:
                        transforms = [(transform, weight)]
                    else:
                        transforms = []
                    for transform, transform_weight in transforms:
                        transformed = (
                            transform[0] * vertex.position[0] + transform[1] * vertex.position[1] + transform[2] * vertex.position[2] + transform[3],
                            transform[4] * vertex.position[0] + transform[5] * vertex.position[1] + transform[6] * vertex.position[2] + transform[7],
                            transform[8] * vertex.position[0] + transform[9] * vertex.position[1] + transform[10] * vertex.position[2] + transform[11],
                        )
                        for axis in range(3):
                            offset[axis] += (transformed[axis] - vertex.position[axis]) * transform_weight
                            normal[axis] += (
                                transform[axis * 4] * vertex.normal[0]
                                + transform[axis * 4 + 1] * vertex.normal[1]
                                + transform[axis * 4 + 2] * vertex.normal[2]
                            ) * transform_weight
                            if tangent is not None and vertex.tangent is not None:
                                tangent[axis] += (
                                    transform[axis * 4] * vertex.tangent[0]
                                    + transform[axis * 4 + 1] * vertex.tangent[1]
                                    + transform[axis * 4 + 2] * vertex.tangent[2]
                                ) * transform_weight
                            if bitangent is not None and vertex.bitangent is not None:
                                bitangent[axis] += (
                                    transform[axis * 4] * vertex.bitangent[0]
                                    + transform[axis * 4 + 1] * vertex.bitangent[1]
                                    + transform[axis * 4 + 2] * vertex.bitangent[2]
                                ) * transform_weight
                    if not transforms:
                        delta = (joint_position_offsets or {}).get(name or "")
                        for axis in range(3):
                            if delta is not None:
                                offset[axis] += delta[axis] * weight
                            normal[axis] += vertex.normal[axis] * weight
                            if tangent is not None and vertex.tangent is not None:
                                tangent[axis] += vertex.tangent[axis] * weight
                            if bitangent is not None and vertex.bitangent is not None:
                                bitangent[axis] += vertex.bitangent[axis] * weight
                if any(offset):
                    adjusted_vertex = Vertex(
                        tuple(vertex.position[axis] + offset[axis] for axis in range(3)),
                        vector_normalize(tuple(normal)) if any(normal) else vertex.normal,
                        vertex.uv,
                        vertex.influences,
                        vector_normalize(tuple(tangent)) if tangent is not None and any(tangent) else vertex.tangent,
                        vector_normalize(tuple(bitangent)) if bitangent is not None and any(bitangent) else vertex.bitangent,
                        vertex.color, vertex.color_from_source,
                    )
            buf.extend(pack_vertex(
                adjusted_vertex, influences, record_uv_flip, uv_scale, uv_offset, fallback_color, uv_format_id
            ))
        records.append({
            "vertex_offset": vertex_offset,
            "vertex_count": len(mesh.vertices),
            "weighted_vertices": sum(bool(vertex.influences) for vertex in mesh.vertices),
            "unresolved_influences": unresolved,
            "palette": palette,
            "source_color_vertices": sum(vertex.color_from_source for vertex in mesh.vertices),
            "fallback_color": list(fallback_color),
            "uv_format_id": uv_format_id,
        })
    vertex_buffer_size = len(buf)
    append_aligned(buf, 0x40)
    index_base = len(buf)
    for mesh, record in zip(meshes, records):
        record["index_offset"] = len(buf) - index_base
        record["index_count"] = len(mesh.indices)
        for index in mesh.indices:
            buf.extend(struct.pack("<H", index))
    index_buffer_size = len(buf) - index_base
    append_aligned(buf, 0x40)
    for record in records:
        record["vertex_buffer_size"] = vertex_buffer_size
        record["index_base"] = index_base
        record["index_buffer_size"] = index_buffer_size
    return bytes(buf), records


def realign_g4mg_index_buffer(md: bytes, mg: bytes) -> tuple[bytes, bytes]:
    """Repair older 0x10-aligned index payloads without touching vertex data."""
    vertex_size = struct.unpack_from("<I", md, 0x50)[0]
    index_size = struct.unpack_from("<I", md, 0x54)[0]
    index_base = struct.unpack_from("<I", md, 0x5C)[0]
    if index_base % 0x40 == 0 and len(mg) % 0x40 == 0:
        return md, mg
    if vertex_size > len(mg) or index_base + index_size > len(mg):
        raise ValueError("legacy G4MG buffer sizes are inconsistent")
    repaired = bytearray(mg[:vertex_size])
    append_aligned(repaired, 0x40)
    repaired_base = len(repaired)
    repaired.extend(mg[index_base:index_base + index_size])
    append_aligned(repaired, 0x40)
    repaired_md = bytearray(md)
    struct.pack_into("<I", repaired_md, 0x5C, repaired_base)
    return bytes(repaired_md), bytes(repaired)


def native_color_kd_tree(colors: list[tuple[tuple[float, float, float], tuple[int, int, int, int]]]):
    """Build a deterministic nearest-neighbour index for native outline data."""
    def build(rows, depth: int = 0):
        if not rows:
            return None
        axis = depth % 3
        rows.sort(key=lambda row: (row[0][axis], row[2]))
        middle = len(rows) // 2
        point, color, source_index = rows[middle]
        return (
            point,
            color,
            source_index,
            axis,
            build(rows[:middle], depth + 1),
            build(rows[middle + 1 :], depth + 1),
        )

    return build([(point, color, index) for index, (point, color) in enumerate(colors)])


def nearest_native_color(tree, position: tuple[float, float, float]) -> tuple[int, int, int, int] | None:
    best_distance = float("inf")
    best_index = float("inf")
    best_color = None

    def visit(node) -> None:
        nonlocal best_distance, best_index, best_color
        if node is None:
            return
        point, color, source_index, axis, left, right = node
        distance = sum((a - b) * (a - b) for a, b in zip(position, point))
        if (distance, source_index) < (best_distance, best_index):
            best_distance, best_index, best_color = distance, source_index, color
        delta = position[axis] - point[axis]
        near, far = (left, right) if delta <= 0.0 else (right, left)
        visit(near)
        if delta * delta <= best_distance:
            visit(far)

    visit(tree)
    return best_color


def transfer_native_vertex_colors(
    meshes: list[Mesh], native_colors: list[list[tuple[tuple[float, float, float], tuple[int, int, int, int]]]],
    fallback_colors: list[tuple[int, int, int, int]],
) -> None:
    for mesh_index, mesh in enumerate(meshes):
        source_index = _source_record_index(mesh, mesh_index, len(native_colors))
        colors = native_colors[source_index] if source_index < len(native_colors) else []
        fallback = fallback_colors[mesh_index] if mesh_index < len(fallback_colors) else (255, 255, 255, 255)
        tree = native_color_kd_tree(colors)
        for vertex in mesh.vertices:
            if vertex.color_from_source:
                continue
            vertex.color = nearest_native_color(tree, vertex.position) or fallback


def apply_vertex_color_policy(
    meshes: list[Mesh],
    mode: str,
    color: tuple[int, int, int, int] = STABLE_CUSTOM_VERTEX_COLOR,
) -> int:
    """Apply the explicit COLOR policy after native color transfer.

    Metal rejects the variable edge2 values produced by the custom Toru port,
    while the same geometry is stable with the constant COLOR used by a
    working custom model.  Keep native transfer available for diagnostic and
    untouched exports, but make the safe custom value authoritative when the
    preset requests it.
    """
    mode = str(mode).lower()
    if mode not in VERTEX_COLOR_MODES:
        raise ValueError(
            f"unsupported vertex_color_mode {mode!r}; "
            f"expected one of {sorted(VERTEX_COLOR_MODES)}"
        )
    if mode == "native":
        return 0
    color = normalize_vertex_color(color)
    changed = 0
    for mesh in meshes:
        for vertex in mesh.vertices:
            if vertex.color != color or vertex.color_from_source:
                changed += 1
            vertex.color = color
            vertex.color_from_source = False
    return changed


def apply_record_outline_rules(meshes: list[Mesh], rules: list[RecordRule]) -> None:
    """Disable edge2 displacement for only the records explicitly opted out."""
    for mesh_index, mesh in enumerate(meshes):
        source_index = _source_record_index(mesh, mesh_index, len(rules))
        rule = rules[source_index] if source_index < len(rules) else None
        if rule is None:
            continue
        if rule.outline_enabled:
            continue
        for vertex in mesh.vertices:
            red, green, _blue, alpha = vertex.color or (255, 255, 255, 255)
            vertex.color = (red, green, 0, alpha)


def native_record_vertex_colors(md: bytes, mg: bytes) -> list[list[tuple[tuple[float, float, float], tuple[int, int, int, int]]]]:
    """Read the native per-vertex outline parameters from a matching G4MD/G4MG pair."""
    if len(md) < 0x60 or not mg:
        return []
    header_size = struct.unpack_from("<H", md, 0x04)[0]
    mesh_count = struct.unpack_from("<H", md, 0x20)[0]
    vertex_buffer_size = struct.unpack_from("<I", md, 0x50)[0]
    if header_size <= 0 or header_size + mesh_count * 0x50 > len(md) or vertex_buffer_size > len(mg):
        return []

    records = []
    for mesh_index in range(mesh_count):
        record_offset = header_size + mesh_index * 0x50
        vertex_offset, _index_offset, vertex_count, _index_count = struct.unpack_from("<IIII", md, record_offset)
        stride = struct.unpack_from("<H", md, record_offset + 0x3E)[0] & 0xFF
        if stride < 0x40 or vertex_offset + vertex_count * stride > vertex_buffer_size:
            records.append([])
            continue
        records.append([
            (
                struct.unpack_from("<3f", mg, vertex_offset + vertex_index * stride),
                struct.unpack_from("<4B", mg, vertex_offset + vertex_index * stride + 0x3C),
            )
            for vertex_index in range(vertex_count)
        ])
    return records


def compact_joint_index(
    name: str,
    aliases: dict[str, str | int] | None = None,
    visited: set[str] | None = None,
    native_joint_indices: dict[str, int] | None = None,
) -> int | None:
    visited = set() if visited is None else visited
    if name in visited:
        raise ValueError(f"cyclic joint alias involving {name!r}")
    visited.add(name)
    alias = (aliases or {}).get(name)
    if isinstance(alias, int):
        return alias
    if isinstance(alias, str) and alias != name:
        return compact_joint_index(alias, aliases, visited, native_joint_indices)
    catalog = load_joint_alias_catalog()
    target = resolve_catalog_alias(name, catalog)
    if not target:
        compact_aliases = {
            "spine01": "c_c_1_1", "spine02": "c_c_1_1",
            "r_index01": "r_idx_1_0", "r_index02": "r_idx_1_1", "r_index03": "r_idx_1_2",
            "l_index01": "l_idx_1_0", "l_index02": "l_idx_1_1", "l_index03": "l_idx_1_2",
            "l_arm_roll": "l_a_1_0", "l_arm_roll_02": "l_a_1_0",
            "r_arm_roll": "r_a_1_0", "r_arm_roll_02": "r_a_1_0",
            "l_hand_roll": "l_a_1_1", "l_hand_roll_02": "l_a_1_1",
            "r_hand_roll": "r_a_1_1", "r_hand_roll_02": "r_a_1_1",
        }
        target = compact_aliases.get(normalize_joint_key(name), "")
    if target and target != name:
        return compact_joint_index(target, aliases, visited, native_joint_indices)
    if name in COMPACT_JOINT_NAMES:
        if native_joint_indices and name in native_joint_indices:
            return native_joint_indices[name]
        return COMPACT_JOINT_NAMES.index(name)
    match = re.fullmatch(r"joint_(\d+)", name)
    return int(match.group(1)) if match else None


def rigid_local_joint(
    rule: RecordRule | None, palette: list[int], aliases: dict[str, str | int] | None = None,
    native_joint_indices: dict[str, int] | None = None,
) -> int:
    if rule is None or rule.rigid_joint is None:
        return 0
    global_joint = (
        rule.rigid_joint
        if isinstance(rule.rigid_joint, int)
        else compact_joint_index(rule.rigid_joint, aliases, native_joint_indices=native_joint_indices)
    )
    if global_joint is None:
        raise ValueError(f"unknown compact joint {rule.rigid_joint!r}")
    global_joint = palette_compatible_joint(global_joint, palette, native_joint_indices or {})
    if global_joint is None:
        raise ValueError(f"joint {rule.rigid_joint!r} ({global_joint}) is not present in native palette {palette}")
    return palette.index(global_joint)


def resolve_vertex_influences(
    vertex: Vertex,
    palette: list[int],
    rule: RecordRule | None,
    aliases: dict[str, str | int] | None = None,
    native_joint_indices: dict[str, int] | None = None,
) -> tuple[list[tuple[int, float]], int]:
    if not vertex.influences:
        return [(rigid_local_joint(rule, palette, aliases, native_joint_indices), 1.0)], 0

    resolved: list[tuple[int, float]] = []
    unresolved = 0
    for name, weight in vertex.influences:
        global_joint = compact_joint_index(name, aliases, native_joint_indices=native_joint_indices)
        global_joint = palette_compatible_joint(global_joint, palette, native_joint_indices or {})
        if global_joint is None:
            unresolved += 1
            continue
        resolved.append((palette.index(global_joint), weight))

    if not resolved:
        return [(rigid_local_joint(rule, palette, aliases, native_joint_indices), 1.0)], unresolved

    combined: dict[int, float] = {}
    for local_joint, weight in resolved:
        combined[local_joint] = combined.get(local_joint, 0.0) + weight
    resolved = list(combined.items())

    if rule is not None and rule.secondary_weight_scale != 1.0:
        if not 0.0 <= rule.secondary_weight_scale <= 1.0:
            raise ValueError(
                f"secondary_weight_scale for {rule.output_name} must be between 0 and 1"
            )
        anchor_joint = rule.weight_anchor_joint if rule.weight_anchor_joint is not None else rule.rigid_joint
        if anchor_joint is None:
            raise ValueError(
                f"record {rule.output_name} needs weight_anchor_joint or rigid_joint when damping weights"
            )
        anchor_global = (
            anchor_joint
            if isinstance(anchor_joint, int)
            else compact_joint_index(anchor_joint, aliases, native_joint_indices=native_joint_indices)
        )
        anchor_global = palette_compatible_joint(anchor_global, palette, native_joint_indices or {})
        if anchor_global is None:
            raise ValueError(f"weight anchor {anchor_joint!r} is not present in palette {palette}")
        anchor_local = palette.index(anchor_global)
        combined = {}
        transferred = 0.0
        for local_joint, weight in resolved:
            if local_joint == anchor_local:
                combined[local_joint] = combined.get(local_joint, 0.0) + weight
            else:
                scaled = weight * rule.secondary_weight_scale
                combined[local_joint] = combined.get(local_joint, 0.0) + scaled
                transferred += weight - scaled
        combined[anchor_local] = combined.get(anchor_local, 0.0) + transferred
        resolved = [(joint, weight) for joint, weight in combined.items() if weight > 0.0]

    return resolved, unresolved


def native_record_uv_formats(md: bytes) -> list[int]:
    """Return the native UV0 format id selected by each mesh record."""
    if len(md) < 0x28:
        return []
    header_size = struct.unpack_from("<H", md, 0x04)[0]
    mesh_count = struct.unpack_from("<H", md, 0x20)[0]
    layout_count = md[0x26]
    cursor = header_size + mesh_count * 0x50
    layout_uv_formats: list[int] = []
    for _ in range(layout_count):
        if cursor + 8 > len(md):
            break
        entry_count = md[cursor + 1]
        uv_format = 14
        element_pos = cursor + 8
        for _entry in range(entry_count):
            if element_pos + 8 > len(md):
                break
            element_type, _value_offset, _padding, format_id = struct.unpack_from("<BHBI", md, element_pos)
            if element_type == 10:
                uv_format = int(format_id)
            element_pos += 8
        layout_uv_formats.append(uv_format)
        cursor += 8 + entry_count * 8

    formats: list[int] = []
    for mesh_index in range(mesh_count):
        record_off = header_size + mesh_index * 0x50
        if record_off + 0x43 > len(md):
            formats.append(14)
            continue
        layout_index = md[record_off + 0x42]
        formats.append(layout_uv_formats[layout_index] if layout_index < len(layout_uv_formats) else 14)
    return formats


def native_record_palettes(md: bytes) -> list[list[int]]:
    header_size = struct.unpack_from("<H", md, 0x04)[0]
    mesh_count = struct.unpack_from("<H", md, 0x20)[0]
    name_base = struct.unpack_from("<H", md, 0x0A)[0] * 4
    palette_table = name_base + struct.unpack_from("<H", md, 0x82)[0] * 4
    palettes: list[list[int]] = []
    for mesh_index in range(mesh_count):
        off = header_size + mesh_index * 0x50
        palette_start = struct.unpack_from("<H", md, off + 0x3A)[0]
        flags = struct.unpack_from("<H", md, off + 0x3C)[0]
        palette_length = flags & 0xFF if flags & 0x100 else 0
        palettes.append([
            struct.unpack_from("<H", md, palette_table + (palette_start + index) * 2)[0]
            for index in range(palette_length)
        ])
    return palettes


def native_joint_name_indices(md: bytes) -> dict[str, int]:
    """Resolve joint indices from the native G4MD hash table, not a shared rig order."""
    if len(md) < 0x84:
        return {}
    header_size = struct.unpack_from("<H", md, 0x04)[0]
    start = header_size + struct.unpack_from("<H", md, 0x74)[0] * 4
    end = header_size + struct.unpack_from("<H", md, 0x82)[0] * 4
    if not 0 <= start <= end <= len(md):
        return {}
    by_hash = {crc32b(name): name for name in COMPACT_JOINT_NAMES}
    indices: dict[str, int] = {}
    for offset in range(start, end - 3, 4):
        name = by_hash.get(struct.unpack_from("<I", md, offset)[0])
        if name is not None:
            indices[name] = (offset - start) // 4
    return indices


def palette_compatible_joint(
    joint: int | None, palette: list[int], native_joint_indices: dict[str, int]
) -> int | None:
    if joint is None:
        return None
    if joint in palette:
        return joint
    names = {index: name for name, index in native_joint_indices.items()}
    name = names.get(joint, "")
    replacements: list[str] = []
    if name == "c_global_0_0":
        replacements.append("c_c_1_0")
    elif name.endswith("_wph_1_0"):
        replacements.append(name.replace("_wph_1_0", "_w_1_0"))
    elif name.endswith("_foot_1_1"):
        replacements.append(name.replace("_foot_1_1", "_foot_1_0"))
    elif re.match(r"^[lr]_(?:idx|mid|rng|thb|pky)_1_[0-2]$", name):
        replacements.append(f"{name[:2]}w_1_0")
    for replacement in replacements:
        candidate = native_joint_indices.get(replacement)
        if candidate in palette:
            return candidate
    return None


def configured_record_palettes(
    native_palettes: list[list[int]],
    records: list[RecordRule],
    meshes: list[Mesh],
    aliases: dict[str, str | int],
    native_joint_indices: dict[str, int] | None = None,
) -> list[list[int]]:
    palettes: list[list[int]] = []

    def used_palette_joints(mesh: Mesh, rule: RecordRule, native_palette: list[int]) -> set[int]:
        used_joints: set[int] = set()
        for vertex in mesh.vertices:
            for joint_name, _ in vertex.influences:
                resolved = compact_joint_index(joint_name, aliases, native_joint_indices=native_joint_indices)
                compatible = palette_compatible_joint(resolved, native_palette, native_joint_indices or {})
                if compatible is not None:
                    resolved = compatible
                if resolved is not None:
                    used_joints.add(resolved)
        return used_joints

    def expanded_native_palette(index: int, rule: RecordRule, mesh: Mesh) -> list[int]:
        palette = list(native_palettes[index])
        used_joints = used_palette_joints(mesh, rule, palette)
        palette.extend(sorted(used_joints.difference(palette)))
        if len(palette) > 0xFF:
            raise ValueError(f"automatic palette for {rule.output_name} exceeds 255 joints")
        return palette

    for index, _output_rule in enumerate(records):
        mesh = meshes[index]
        source_index = _source_record_index(mesh, index, len(records))
        rule = records[source_index]
        native_palette = native_palettes[source_index] if source_index < len(native_palettes) else []
        if rule.auto_palette:
            palettes.append(expanded_native_palette(source_index, rule, mesh))
            continue
        if rule.palette_joints is None:
            used_joints = used_palette_joints(mesh, rule, native_palette)
            if used_joints.difference(native_palette):
                palettes.append(expanded_native_palette(source_index, rule, mesh))
            else:
                palettes.append(native_palette)
            continue
        palette: list[int] = []
        for joint in rule.palette_joints:
            resolved = joint if isinstance(joint, int) else compact_joint_index(joint, aliases, native_joint_indices=native_joint_indices)
            if resolved is None:
                raise ValueError(f"unknown compact joint {joint!r} in palette for {rule.output_name}")
            if not 0 <= resolved < 0x10000:
                raise ValueError(f"joint index {resolved} is outside uint16 range")
            if resolved not in palette:
                palette.append(resolved)
        if not palette or len(palette) > 0xFF:
            raise ValueError(f"palette for {rule.output_name} must contain 1..255 unique joints")
        palettes.append(palette)
    return palettes


def compact_palettes(palettes: list[list[int]]) -> tuple[list[int], list[int]]:
    table: list[int] = []
    starts: list[int] = []
    for palette in palettes:
        start = next(
            (index for index in range(len(table) - len(palette) + 1) if table[index : index + len(palette)] == palette),
            None,
        )
        if start is None:
            start = len(table)
            table.extend(palette)
        starts.append(start)
    return table, starts


def rewrite_joint_palettes(md: bytes, palettes: list[list[int]]) -> bytes:
    native_palettes = native_record_palettes(md)
    if palettes == native_palettes:
        return md
    mesh_count = struct.unpack_from("<H", md, 0x20)[0]
    if len(palettes) != mesh_count:
        raise ValueError(f"palette count {len(palettes)} does not match mesh count {mesh_count}")

    name_base = struct.unpack_from("<H", md, 0x0A)[0] * 4
    palette_table = name_base + struct.unpack_from("<H", md, 0x82)[0] * 4
    mesh_name_table = name_base + struct.unpack_from("<H", md, 0x84)[0] * 4
    material_name_table = name_base + struct.unpack_from("<H", md, 0x86)[0] * 4
    if not palette_table <= mesh_name_table <= material_name_table <= len(md):
        raise ValueError("native trailing G4MD tables are not ordered as expected")
    for field in range(0x88, 0xA0, 2):
        value = struct.unpack_from("<H", md, field)[0]
        absolute = name_base + value * 4
        if value and absolute >= mesh_name_table:
            raise ValueError(f"cannot safely move unknown trailing table referenced by header field 0x{field:02X}")

    palette_entries, palette_starts = compact_palettes(palettes)
    palette_bytes = b"".join(struct.pack("<H", joint) for joint in palette_entries)
    palette_bytes += b"\0" * (align(len(palette_bytes), 4) - len(palette_bytes))
    old_palette_size = mesh_name_table - palette_table
    delta = len(palette_bytes) - old_palette_size
    out = bytearray(md[:palette_table] + palette_bytes + md[mesh_name_table:])

    struct.pack_into("<H", out, 0x84, struct.unpack_from("<H", md, 0x84)[0] + delta // 4)
    struct.pack_into("<H", out, 0x86, struct.unpack_from("<H", md, 0x86)[0] + delta // 4)
    struct.pack_into("<I", out, 0x0C, struct.unpack_from("<I", md, 0x0C)[0] + delta)
    header_size = struct.unpack_from("<H", out, 0x04)[0]
    for index, (palette, start) in enumerate(zip(palettes, palette_starts)):
        off = header_size + index * 0x50
        flags = struct.unpack_from("<H", out, off + 0x3C)[0]
        struct.pack_into("<H", out, off + 0x3A, start)
        struct.pack_into("<H", out, off + 0x3C, (flags & ~0x1FF) | 0x100 | len(palette))
    return bytes(out)


def validate_generated_model(md: bytes, mg: bytes) -> dict:
    if md[:4] != b"G4MD":
        raise ValueError("generated model has no G4MD magic")
    header_size = struct.unpack_from("<H", md, 0x04)[0]
    mesh_count = struct.unpack_from("<H", md, 0x20)[0]
    vertex_buffer_size = struct.unpack_from("<I", md, 0x50)[0]
    index_buffer_size = struct.unpack_from("<I", md, 0x54)[0]
    index_base = struct.unpack_from("<I", md, 0x5C)[0]
    if index_base < vertex_buffer_size or index_base + index_buffer_size > len(mg):
        raise ValueError("generated G4MG buffer sizes are inconsistent with G4MD")

    checked_vertices = 0
    checked_indices = 0
    for mesh_index in range(mesh_count):
        off = header_size + mesh_index * 0x50
        vertex_offset, index_offset, vertex_count, index_count = struct.unpack_from("<IIII", md, off)
        flags = struct.unpack_from("<H", md, off + 0x3C)[0]
        stride = struct.unpack_from("<H", md, off + 0x3E)[0] & 0xFF
        palette_length = flags & 0xFF if flags & 0x100 else 0
        if stride < 0x3C or vertex_offset + vertex_count * stride > vertex_buffer_size:
            raise ValueError(f"record {mesh_index} vertex range exceeds generated vertex buffer")
        if index_offset + index_count * 2 > index_buffer_size:
            raise ValueError(f"record {mesh_index} index range exceeds generated index buffer")
        indices = struct.unpack_from("<" + "H" * index_count, mg, index_base + index_offset) if index_count else ()
        if any(index >= vertex_count for index in indices):
            raise ValueError(f"record {mesh_index} contains an index outside its local vertex range")
        for vertex_index in range(vertex_count):
            vertex = vertex_offset + vertex_index * stride
            weights = struct.unpack_from("<8H", mg, vertex + 0x24)
            joints = struct.unpack_from("<8B", mg, vertex + 0x34)
            if sum(weights) != 0xFFFF:
                raise ValueError(f"record {mesh_index} vertex {vertex_index} weights do not sum to 65535")
            if any(joint >= palette_length for joint, weight in zip(joints, weights) if weight):
                raise ValueError(f"record {mesh_index} vertex {vertex_index} references outside its joint palette")
        checked_vertices += vertex_count
        checked_indices += index_count
    return {
        "records": mesh_count,
        "vertices": checked_vertices,
        "indices": checked_indices,
        "g4mg_size": len(mg),
    }


def mesh_match_rank(mesh: Mesh, rule: RecordRule) -> int:
    """Rank an assignment without letting Blender duplicate suffixes steal a mesh."""
    if "*" in rule.match_names:
        return 1
    mesh_names = {mesh.name, clean_symbol(mesh.name)}
    rule_names = {name for match_name in rule.match_names for name in (match_name, clean_symbol(match_name))}
    material = mesh.material_name.lower()
    if mesh_names & rule_names or material in {name.lower() for name in rule.match_names}:
        return 2
    mesh_canonical = canonical_mesh_name(mesh.name)
    if any(mesh_canonical == canonical_mesh_name(match_name) for match_name in rule.match_names):
        return 1
    return 0


def mesh_matches_rule(mesh: Mesh, rule: RecordRule) -> bool:
    return mesh_match_rank(mesh, rule) > 0


def source_uv_transform(mesh: Mesh, rule: RecordRule) -> tuple[tuple[float, float], tuple[float, float]]:
    transforms = rule.source_uv_transforms or {}

    def values(transform: dict) -> tuple[tuple[float, float], tuple[float, float]]:
        scale = transform.get("scale", [1.0, 1.0])
        offset = transform.get("offset", [0.0, 0.0])
        return (float(scale[0]), float(scale[1])), (float(offset[0]), float(offset[1]))

    # The sidecar-preserved Blender object identity is authoritative.  A DAE
    # mesh name can be a sanitized duplicate (Part.001 -> Part_001), and using
    # it first can make every part inherit the same atlas tile.
    source_names = [name for name in mesh.source_names if name]
    lookup_names = source_names + ([mesh.name] if mesh.name else [])
    for name in lookup_names:
        exact = {name, clean_symbol(name)}
        for key, transform in transforms.items():
            if exact & {key, clean_symbol(key)}:
                return values(transform)

    # Canonical matching is only a last-resort compatibility path.  Never pick
    # the first of several duplicate Blender names because that silently folds
    # different atlas cells onto one another.
    candidates: list[tuple[str, tuple[tuple[float, float], tuple[float, float]]]] = []
    canonical_names = {canonical_mesh_name(name) for name in lookup_names if name}
    for key, transform in transforms.items():
        if canonical_mesh_name(key) in canonical_names:
            candidates.append((key, values(transform)))
    if candidates:
        distinct = {value for _, value in candidates}
        if len(distinct) == 1:
            return candidates[0][1]
        names = ", ".join(key for key, _ in candidates)
        raise ValueError(
            f"ambiguous source UV transform for {mesh.name!r}; original source names={source_names!r}; "
            f"matching config objects={names}. Re-export the DAE/weights sidecar together."
        )
    return (1.0, 1.0), (0.0, 0.0)


def fit_mesh_uv_to_display_rect(
    mesh: Mesh,
    target_display_rect: tuple[float, float, float, float],
    flip_y: bool = True,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Fit the actual post-modifier DAE UV bounds into a displayed atlas rectangle.

    The exporter may apply Blender modifiers while producing the DAE, so bounds
    measured on the pre-export Blender object are not authoritative.  Compute
    from the Mesh vertices parsed by g4_port instead.  Returned offset is in the
    stored G4 convention; read_uv0/importer inverts V back for Blender display.
    """
    if not mesh.vertices:
        return (1.0, 1.0), (0.0, 0.0)
    min_u = min(vertex.uv[0] for vertex in mesh.vertices)
    max_u = max(vertex.uv[0] for vertex in mesh.vertices)
    min_v = min(vertex.uv[1] for vertex in mesh.vertices)
    max_v = max(vertex.uv[1] for vertex in mesh.vertices)
    origin_u, origin_v, width, height = target_display_rect
    bounds_u = max(max_u - min_u, 0.0001)
    bounds_v = max(max_v - min_v, 0.0001)
    scale = min(width / bounds_u, height / bounds_v)
    used_u = bounds_u * scale
    used_v = bounds_v * scale
    display_offset_u = origin_u + (width - used_u) * 0.5 - min_u * scale
    display_min_v = origin_v + (height - used_v) * 0.5
    if flip_y:
        display_offset_v = display_min_v - min_v * scale
        stored_offset_v = 1.0 - scale - display_offset_v
    else:
        # Without the pre-pack V flip, Blender's import-side inversion reverses
        # the source V direction.  Position max_v at the requested display min.
        stored_offset_v = 1.0 - max_v * scale - display_min_v
    return (scale, scale), (display_offset_u, stored_offset_v)


def transformed_source_vertex(vertex: Vertex, scale: tuple[float, float], offset: tuple[float, float]) -> Vertex:
    if scale == (1.0, 1.0) and offset == (0.0, 0.0):
        return vertex
    # Source materials use repeating UVs (notably Tsubasa's neck/ear sheet has
    # V coordinates above 1). Wrap inside the source image before moving that
    # image into an atlas cell; otherwise repeated islands spill into a
    # neighbouring, often empty, cell.
    local_uv = tuple(value - math.floor(value) for value in vertex.uv)
    uv = (local_uv[0] * scale[0] + offset[0], local_uv[1] * scale[1] + offset[1])
    return Vertex(
        vertex.position, vertex.normal, uv, vertex.influences, vertex.tangent, vertex.bitangent,
        vertex.color, vertex.color_from_source,
    )


def split_mesh_by_vertex_limit(mesh: Mesh, output_names: list[str], max_vertices: int = 0xFFFF) -> list[Mesh]:
    chunks: list[Mesh] = []
    vertices: list[Vertex] = []
    indices: list[int] = []
    remap: dict[int, int] = {}

    def flush() -> None:
        nonlocal vertices, indices, remap
        if not vertices:
            return
        name = output_names[len(chunks)] if len(chunks) < len(output_names) else f"{mesh.name}_{len(chunks)}"
        chunks.append(Mesh(
            name,
            vertices,
            indices,
            mesh.material_index,
            mesh.material_name,
            mesh.source_names,
            mesh.uv_transform_trace,
            mesh.source_record_index,
        ))
        vertices = []
        indices = []
        remap = {}

    for start in range(0, len(mesh.indices), 3):
        triangle = mesh.indices[start : start + 3]
        if len(triangle) < 3:
            continue
        missing = [index for index in triangle if index not in remap]
        if vertices and len(vertices) + len(missing) > max_vertices:
            flush()
            missing = list(triangle)
        for source_index in triangle:
            target_index = remap.get(source_index)
            if target_index is None:
                if len(vertices) >= max_vertices:
                    flush()
                target_index = len(vertices)
                remap[source_index] = target_index
                vertices.append(mesh.vertices[source_index])
            indices.append(target_index)
    flush()
    return chunks


def merged_native_meshes(meshes: list[Mesh], config: PortConfig) -> list[Mesh]:
    assigned: set[int] = set()
    output: list[Mesh | None] = [None] * len(config.records)

    def merge(
        rule: RecordRule,
        material_index: int,
        material_name: str,
        parts: list[Mesh],
        source_record_index: int,
    ) -> Mesh:
        vertices: list[Vertex] = []
        indices: list[int] = []
        uv_transform_trace = []
        for part in parts:
            base = len(vertices)
            scale, offset = source_uv_transform(part, rule)
            uv_transform_trace.append({
                "source": part.name,
                "source_names": list(part.source_names),
                "material": part.material_name,
                "scale": list(scale),
                "offset": list(offset),
                "input_uv_bounds": [
                    [min((vertex.uv[axis] for vertex in part.vertices), default=0.0), max((vertex.uv[axis] for vertex in part.vertices), default=0.0)]
                    for axis in range(2)
                ],
            })
            vertices.extend(transformed_source_vertex(vertex, scale, offset) for vertex in part.vertices)
            indices.extend(base + index for index in part.indices)
        template_index = (
            rule.native_template_record_index
            if rule.native_template_record_index is not None
            else source_record_index
        )
        return Mesh(
            rule.output_name,
            vertices,
            indices,
            material_index,
            material_name,
            tuple(
                source_name
                for part in parts
                for source_name in (part.source_names or (part.name,))
            ),
            tuple(uv_transform_trace),
            template_index,
        )

    zero = Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.5, 0.5))
    wildcard_rules = [rule for rule in config.records if "*" in rule.match_names]
    explicit_rules = [rule for rule in config.records if "*" not in rule.match_names]

    def assigned_record(mesh: Mesh) -> str:
        assignments = config.source_mesh_assignments or {}
        if not assignments:
            return ""

        # Prefer the original Blender identity preserved in the sidecar.  Do
        # not strip .001/.002-style suffixes during the exact pass: those are
        # distinct objects and may intentionally target different native
        # records / atlas cells.
        lookup_names = [name for name in mesh.source_names if name]
        if mesh.name:
            lookup_names.append(mesh.name)
        for lookup_name in lookup_names:
            key = clean_symbol(lookup_name).casefold()
            for source_name, record_name in assignments.items():
                if clean_symbol(source_name).casefold() == key:
                    return record_name

        # Legacy files may only expose a canonicalized Collada name.  Accept a
        # suffix-stripped fallback only when it resolves to one unique record;
        # otherwise leave it unassigned instead of silently choosing the first
        # duplicate object.
        fallback_records: set[str] = set()
        canonical_keys = {canonical_mesh_name(name).casefold() for name in lookup_names}
        for source_name, record_name in assignments.items():
            if canonical_mesh_name(source_name).casefold() in canonical_keys:
                fallback_records.add(record_name)
        return next(iter(fallback_records)) if len(fallback_records) == 1 else ""

    def is_best_explicit_match(mesh: Mesh, rule: RecordRule) -> bool:
        rank = mesh_match_rank(mesh, rule)
        return rank > 0 and rank == max(mesh_match_rank(mesh, explicit) for explicit in explicit_rules)

    for record_index, rule in enumerate(config.records):
        if output[record_index] is not None:
            continue
        explicit_assignment_mode = bool(config.source_mesh_assignments)
        if "*" in rule.match_names:
            selected = [
                (index, mesh)
                for index, mesh in enumerate(meshes)
                if index not in assigned
                and (
                    assigned_record(mesh) == rule.output_name
                    or (
                        not explicit_assignment_mode
                        and not assigned_record(mesh)
                        and not any(mesh_matches_rule(mesh, explicit) for explicit in explicit_rules)
                    )
                )
            ]
        else:
            selected = [
                (index, mesh)
                for index, mesh in enumerate(meshes)
                if index not in assigned
                and (
                    assigned_record(mesh) == rule.output_name
                    or (
                        not explicit_assignment_mode
                        and not assigned_record(mesh)
                        and is_best_explicit_match(mesh, rule)
                    )
                )
            ]
        parts = [mesh for _, mesh in selected]
        part_indices = {index for index, _ in selected}
        for index, _ in selected:
            assigned.add(index)
        material_index = config.native_material_names.index(rule.material_name)
        if parts:
            merged = merge(rule, material_index, rule.material_name, parts, record_index)
            if len(merged.vertices) <= 0xFFFF:
                output[record_index] = merged
                continue
            chunk_rule_indices = []
            for index in range(record_index, len(config.records)):
                if output[index] is not None:
                    continue
                chunk_rule = config.records[index]
                if all(mesh_matches_rule(part, chunk_rule) for part in parts):
                    chunk_rule_indices.append(index)
                    continue
                claimed_by_other_mesh = any(
                    mesh_index not in part_indices and mesh_matches_rule(mesh, chunk_rule)
                    for mesh_index, mesh in enumerate(meshes)
                )
                if not claimed_by_other_mesh:
                    chunk_rule_indices.append(index)
            chunks = split_mesh_by_vertex_limit(
                merged,
                [config.records[index].output_name for index in chunk_rule_indices],
            )
            if len(chunks) > len(chunk_rule_indices):
                raise ValueError(
                    f"record {rule.output_name!r} needs {len(chunks)} native records to keep chunks under 65535 "
                    f"vertices, but only {len(chunk_rule_indices)} matching records are assigned"
                )
            for chunk_index, chunk in zip(chunk_rule_indices, chunks):
                chunk_rule = config.records[chunk_index]
                chunk_rule.material_name = rule.material_name
                chunk_rule.force_layout_material = rule.force_layout_material
                output[chunk_index] = chunk
        elif rule.fallback_degenerate or wildcard_rules:
            output[record_index] = Mesh(
                rule.output_name, [zero, zero, zero], [0, 1, 2], material_index, rule.material_name,
                source_record_index=record_index,
            )
        else:
            output[record_index] = Mesh(
                rule.output_name, [zero, zero, zero], [0, 1, 2], material_index, rule.material_name,
                source_record_index=record_index,
            )
    missing_records = [config.records[index].output_name for index, mesh in enumerate(output) if mesh is None]
    if missing_records:
        raise ValueError(f"native records did not receive output meshes: {missing_records}")
    return [mesh for mesh in output if mesh is not None]


def unique_materials(meshes: list[Mesh]) -> list[str]:
    materials: list[str] = []
    for mesh in meshes:
        if mesh.material_name not in materials:
            materials.append(mesh.material_name)
    return materials


def build_g4md(
    meshes: list[Mesh],
    records: list[dict],
    material_plans: list[MaterialPlan],
    texture_names: list[str],
) -> bytes:
    material_names = [f"{material.name}M" for material in material_plans]
    mesh_count = len(meshes)
    material_count = len(material_names)
    header_size = 0xA0
    md = bytearray(header_size)
    md[0:4] = b"G4MD"
    struct.pack_into("<HHI", md, 0x04, header_size, 0x68, 0x00280000)
    struct.pack_into("<HHHBBBB", md, 0x20, mesh_count, material_count, 1, 1, len(texture_names), 2, 0x0A)
    struct.pack_into("<I", md, 0x50, records[0]["vertex_buffer_size"])
    struct.pack_into("<I", md, 0x54, records[0]["index_buffer_size"])
    struct.pack_into("<I", md, 0x5C, records[0]["index_base"])

    mins = [min(vertex.position[axis] for mesh in meshes for vertex in mesh.vertices) for axis in range(3)]
    maxs = [max(vertex.position[axis] for mesh in meshes for vertex in mesh.vertices) for axis in range(3)]
    center = [(mins[axis] + maxs[axis]) * 0.5 for axis in range(3)]
    extent = [(maxs[axis] - mins[axis]) * 0.5 for axis in range(3)]
    struct.pack_into("<3f", md, 0x30, *center)
    struct.pack_into("<f", md, 0x3C, 1.0)
    struct.pack_into("<3f", md, 0x40, *extent)

    for mesh_index, (mesh, record) in enumerate(zip(meshes, records)):
        off = header_size + mesh_index * 0x50
        if len(md) < off + 0x50:
            md.extend(b"\0" * (off + 0x50 - len(md)))
        positions = [vertex.position for vertex in mesh.vertices]
        local_mins = [min(position[axis] for position in positions) for axis in range(3)]
        local_maxs = [max(position[axis] for position in positions) for axis in range(3)]
        local_center = [(local_mins[axis] + local_maxs[axis]) * 0.5 for axis in range(3)]
        local_extent = [(local_maxs[axis] - local_mins[axis]) * 0.5 for axis in range(3)]
        struct.pack_into(
            "<IIII",
            md,
            off,
            record["vertex_offset"],
            record["index_offset"],
            record["vertex_count"],
            record["index_count"],
        )
        struct.pack_into("<3f", md, off + 0x10, *local_center)
        struct.pack_into("<f", md, off + 0x1C, 1.0)
        struct.pack_into("<3f", md, off + 0x20, *local_extent)
        struct.pack_into("<I", md, off + 0x30, record["index_count"] // 3)
        struct.pack_into("<H", md, off + 0x34, 0xFF)
        struct.pack_into("<H", md, off + 0x38, FACE_NAME_INDEX if mesh.name in FACE_MESH_NAMES else NON_FACE_NAME_INDEX)
        struct.pack_into("<H", md, off + 0x3A, 0)
        struct.pack_into("<H", md, off + 0x3C, 0x101)
        struct.pack_into("<H", md, off + 0x3E, 0x44)
        struct.pack_into("<BB", md, off + 0x42, 0, mesh.material_index)

    layout_offset = len(md)
    md.extend(struct.pack("<8B", 0, 11, 0, 0, 0, 0, 0, 0))
    for element_type, value_offset, format_id in [
        (1, 0x00, 0x03),
        (2, 0x0C, 0x14),
        (13, 0x14, 0x14),
        (3, 0x1C, 0x14),
        (5, 0x24, 0x20),
        (6, 0x34, 0x18),
        (8, 0x3C, 0x0C),
        (10, 0x40, 0x0E),
        (11, 0x40, 0x0E),
        (12, 0x40, 0x0E),
        (14, 0x40, 0x0E),
    ]:
        md.extend(struct.pack("<BHBI", element_type, value_offset, 0, format_id))

    material_table = append_aligned(md, 0x10)
    texture_ref_counts = [len(plan.texture_refs) for plan in material_plans]
    material_ref_table = material_table + material_count * 0x10 + 0x30
    ref_start = 0
    for ref_count in texture_ref_counts:
        md.extend(struct.pack("<8H", 0, 0, 0, 0, 15, 1283, ref_count, ref_start))
        ref_start += ref_count
    while len(md) < material_ref_table:
        md.append(0)
    for plan in material_plans:
        for texture_name in plan.texture_refs:
            md.extend(struct.pack("<BBBBBB", texture_names.index(texture_name), 3, 0, 0, 0, 0))

    append_aligned(md, 0x10)
    name_base_bias = 0x28
    tables: dict[int, int] = {}

    def add_table(field: int, data: bytes, boundary: int = 4) -> None:
        append_aligned(md, boundary)
        tables[field] = (len(md) - name_base_bias) // 4
        md.extend(data)

    add_table(0x76, b"".join(struct.pack("<I", crc32b(name)) for name in texture_names))
    add_table(0x7C, struct.pack("<H", 0))
    add_table(0x80, b"".join(struct.pack("<H", mesh.material_index) for mesh in meshes))
    add_table(0x82, b"")
    add_table(0x84, build_offset_string_table([mesh.name for mesh in meshes]), 2)
    add_table(0x86, build_offset_string_table(material_names), 2)

    for field, value in tables.items():
        struct.pack_into("<H", md, field, value)
    struct.pack_into("<H", md, 0x64, (material_table - name_base_bias) // 4)
    struct.pack_into("<H", md, 0x0A, name_base_bias // 4)
    struct.pack_into("<I", md, 0x0C, len(md) - header_size)

    return bytes(md)


def table_offset_units(absolute: int, name_base_bias: int = 0x28) -> int:
    return (absolute - name_base_bias) // 4


def vertex_layouts_end(data: bytes, layout_offset: int, layout_count: int) -> int:
    cursor = layout_offset
    for _ in range(layout_count):
        if cursor + 8 > len(data):
            break
        entry_count = data[cursor + 1]
        cursor += 8 + entry_count * 8
    return cursor


def material_table_offset(data: bytes, mesh_count: int) -> int:
    submesh_table = struct.unpack_from("<H", data, 0x04)[0]
    layout_count = data[0x26]
    return align(vertex_layouts_end(data, submesh_table + mesh_count * 0x50, layout_count), 0x10)


def apply_material_overrides(md: bytearray, overrides: list[MaterialOverride]) -> None:
    if not overrides:
        return

    mesh_count = struct.unpack_from("<H", md, 0x20)[0]
    material_count = struct.unpack_from("<H", md, 0x22)[0]
    material_table = material_table_offset(md, mesh_count)
    material_ref_table = material_table + material_count * 0x10 + 0x30
    existing_end = 0
    for index in range(material_count):
        off = material_table + index * 0x10
        ref_count = struct.unpack_from("<H", md, off + 0x0C)[0]
        ref_start = struct.unpack_from("<H", md, off + 0x0E)[0]
        existing_end = max(existing_end, ref_start + ref_count)

    next_ref_start = existing_end
    for override in overrides:
        if override.material_index >= material_count or override.clone_from >= material_count:
            raise ValueError(
                f"material override out of range: {override.material_index} <- {override.clone_from}, "
                f"material_count={material_count}"
            )
        dst = material_table + override.material_index * 0x10
        src = material_table + override.clone_from * 0x10
        md[dst : dst + 0x10] = md[src : src + 0x10]
        ref_start = int(override.ref_start) if override.ref_start is not None else next_ref_start
        struct.pack_into("<H", md, dst + 0x0C, len(override.texture_refs))
        struct.pack_into("<H", md, dst + 0x0E, ref_start)
        for ref_index, (texture_index, slot_type) in enumerate(override.texture_refs):
            ref_off = material_ref_table + (ref_start + ref_index) * 6
            if ref_off + 6 > len(md):
                raise ValueError("material override references exceed G4MD table padding")
            struct.pack_into("<BBBBBB", md, ref_off, texture_index, slot_type, 0, 0, 0, 0)
        next_ref_start = max(next_ref_start, ref_start + len(override.texture_refs))


def duplicate_g4md_material_records(
    md: bytes,
    source_material_indices: list[int],
    material_names: list[str],
) -> bytes:
    """Give each output record an independent G4MD material slot."""
    mesh_count = struct.unpack_from("<H", md, 0x20)[0]
    old_material_count = struct.unpack_from("<H", md, 0x22)[0]
    new_material_count = len(source_material_indices)
    if new_material_count != mesh_count:
        raise ValueError("unique material export requires one material slot per mesh record")
    if new_material_count <= old_material_count:
        return md
    if len(material_names) != new_material_count:
        raise ValueError("unique material names do not match the output material count")

    material_table = material_table_offset(md, mesh_count)
    old_ref_table = material_table + old_material_count * 0x10 + 0x30
    name_base = struct.unpack_from("<H", md, 0x0A)[0] * 4
    old_name_index_table = name_base + struct.unpack_from("<H", md, 0x80)[0] * 4
    old_name_index_end = name_base + struct.unpack_from("<H", md, 0x82)[0] * 4
    if not (
        old_name_index_table > 0
        and old_name_index_table < old_name_index_end <= len(md)
        and (old_name_index_end - old_name_index_table) % 2 == 0
    ):
        raise ValueError("native G4MD material index table is outside the file")
    old_name_index_count = (old_name_index_end - old_name_index_table) // 2
    source_refs: list[tuple[int, int]] = []
    old_ref_end = 0
    for source_index in range(old_material_count):
        record_offset = material_table + source_index * 0x10
        ref_count = struct.unpack_from("<H", md, record_offset + 0x0C)[0]
        ref_start = struct.unpack_from("<H", md, record_offset + 0x0E)[0]
        source_refs.append((ref_start, ref_count))
        old_ref_end = max(old_ref_end, ref_start + ref_count)

    new_records = bytearray()
    new_refs = bytearray()
    new_name_indices = bytearray()
    for source_index in source_material_indices:
        if not 0 <= source_index < old_material_count:
            raise ValueError(
                f"source material index {source_index} is outside native material count {old_material_count}"
            )
        record_offset = material_table + source_index * 0x10
        record = bytearray(md[record_offset : record_offset + 0x10])
        ref_start, ref_count = source_refs[source_index]
        name_index_end = ref_start + ref_count
        if name_index_end > old_name_index_count:
            raise ValueError("native G4MD material index table does not cover material references")
        struct.pack_into("<H", record, 0x0E, len(new_refs) // 6)
        new_records.extend(record)
        new_name_indices.extend(
            md[old_name_index_table + ref_start * 2 : old_name_index_table + name_index_end * 2]
        )
        ref_offset = old_ref_table + ref_start * 6
        ref_end = ref_offset + ref_count * 6
        if ref_end > len(md):
            raise ValueError("native G4MD material references exceed the file")
        new_refs.extend(md[ref_offset:ref_end])

    old_ref_end_offset = old_ref_table + old_ref_end * 6
    if old_ref_end_offset > len(md):
        raise ValueError("native G4MD material reference table exceeds the file")
    gap = md[material_table + old_material_count * 0x10 : old_ref_table]
    prefix = md[:material_table] + new_records + gap + new_refs
    # Keep every absolute header pointer word-aligned.  The native reference
    # stream may end at a non-aligned offset, so align the inserted prefix to
    # that same remainder rather than aligning it to zero unconditionally.
    prefix += b"\0" * ((old_ref_end_offset - len(prefix)) % 4)
    out = bytearray(prefix + md[old_ref_end_offset:])
    delta = len(out) - len(md)

    struct.pack_into("<H", out, 0x22, new_material_count)
    for field in (0x74, 0x76, 0x78, 0x7A, 0x7C, 0x7E, 0x80, 0x82, 0x84, 0x86):
        value = struct.unpack_from("<H", md, field)[0]
        absolute = name_base + value * 4
        if value and absolute >= old_ref_table:
            struct.pack_into("<H", out, field, value + delta // 4)

    shifted_name_index_table = old_name_index_table + delta
    shifted_name_index_end = old_name_index_end + delta
    name_index_delta = len(new_name_indices) - (old_name_index_end - old_name_index_table)
    out = bytearray(
        out[:shifted_name_index_table]
        + new_name_indices
        + out[shifted_name_index_end:]
    )
    total_delta = delta + name_index_delta
    struct.pack_into("<H", out, 0x82, table_offset_units(shifted_name_index_table + len(new_name_indices), name_base))
    for field in (0x84, 0x86):
        value = struct.unpack_from("<H", md, field)[0]
        absolute = name_base + value * 4
        if value and absolute >= old_name_index_end:
            struct.pack_into("<H", out, field, value + total_delta // 4)

    old_material_name_table = name_base + struct.unpack_from("<H", md, 0x86)[0] * 4
    shifted_material_name_table = old_material_name_table + total_delta
    if not old_material_name_table <= len(md):
        raise ValueError("native G4MD material name table is outside the file")
    out = bytearray(out[:shifted_material_name_table] + build_offset_string_table(material_names))
    out.extend(b"\0" * ((-len(out)) % 4))
    struct.pack_into("<H", out, 0x86, table_offset_units(shifted_material_name_table, name_base))
    submesh_table = struct.unpack_from("<H", out, 0x04)[0]
    for output_index in range(new_material_count):
        out[submesh_table + output_index * 0x50 + 0x43] = output_index
    struct.pack_into("<I", out, 0x0C, len(out) - struct.unpack_from("<H", out, 0x04)[0])
    return bytes(out)


def build_base_g4md(meshes: list[Mesh], records: list[dict], raw_root: Path, config: PortConfig) -> bytes:
    native = (raw_root / config.common_rel).read_bytes()
    native_mesh_count = struct.unpack_from("<H", native, 0x20)[0]
    mesh_count = len(meshes)
    header_size = 0xA0
    if mesh_count < native_mesh_count:
        raise ValueError(f"output mesh count {mesh_count} cannot shrink native count {native_mesh_count}")
    if mesh_count == native_mesh_count:
        md = bytearray(native)
    else:
        extra_bytes = (mesh_count - native_mesh_count) * 0x50
        native_records_end = header_size + native_mesh_count * 0x50
        md = bytearray(native[:header_size] + b"\0" * (mesh_count * 0x50) + native[native_records_end:])
        struct.pack_into("<H", md, 0x20, mesh_count)
        for field in (
            0x62, 0x64, 0x66, 0x68, 0x6A, 0x6C, 0x6E, 0x70, 0x72,
            0x74, 0x76, 0x78, 0x7A, 0x7C, 0x7E, 0x80, 0x82, 0x84, 0x86,
        ):
            value = struct.unpack_from("<H", native, field)[0]
            struct.pack_into("<H", md, field, value + extra_bytes // 4)

        name_base = struct.unpack_from("<H", md, 0x0A)[0] * 4
        mesh_name_table = name_base + struct.unpack_from("<H", md, 0x84)[0] * 4
        material_name_table = name_base + struct.unpack_from("<H", md, 0x86)[0] * 4
        if not mesh_name_table <= material_name_table <= len(md):
            raise ValueError("expanded G4MD name tables are not ordered as expected")
        mesh_names = build_offset_string_table([rule.output_name for rule in config.records])
        material_names = build_offset_string_table(config.native_material_names)
        material_start = mesh_name_table + len(mesh_names)
        material_start = (material_start + 3) & ~3
        md = bytearray(md[:mesh_name_table] + mesh_names)
        md.extend(b"\0" * (material_start - len(md)))
        md.extend(material_names)
        md.extend(b"\0" * ((-len(md)) % 4))
        struct.pack_into("<H", md, 0x84, table_offset_units(mesh_name_table, name_base))
        struct.pack_into("<H", md, 0x86, table_offset_units(material_start, name_base))
        struct.pack_into("<I", md, 0x0C, len(md) - header_size)

    struct.pack_into("<I", md, 0x50, records[0]["vertex_buffer_size"])
    struct.pack_into("<I", md, 0x54, records[0]["index_buffer_size"])
    struct.pack_into("<I", md, 0x5C, records[0]["index_base"])

    mins = [min(vertex.position[axis] for mesh in meshes for vertex in mesh.vertices) for axis in range(3)]
    maxs = [max(vertex.position[axis] for mesh in meshes for vertex in mesh.vertices) for axis in range(3)]
    center = [(mins[axis] + maxs[axis]) * 0.5 for axis in range(3)]
    extent = [(maxs[axis] - mins[axis]) * 0.5 for axis in range(3)]
    struct.pack_into("<3f", md, 0x30, *center)
    struct.pack_into("<f", md, 0x3C, 1.0)
    struct.pack_into("<3f", md, 0x40, *extent)

    for mesh_index, (mesh, record, rule) in enumerate(zip(meshes, records, config.records)):
        off = header_size + mesh_index * 0x50
        source_index = _source_record_index(mesh, mesh_index, native_mesh_count)
        source_off = header_size + source_index * 0x50
        if mesh_count > native_mesh_count:
            md[off : off + 0x50] = native[source_off : source_off + 0x50]
        positions = [vertex.position for vertex in mesh.vertices]
        local_mins = [min(position[axis] for position in positions) for axis in range(3)]
        local_maxs = [max(position[axis] for position in positions) for axis in range(3)]
        local_center = [(local_mins[axis] + local_maxs[axis]) * 0.5 for axis in range(3)]
        local_extent = [(local_maxs[axis] - local_mins[axis]) * 0.5 for axis in range(3)]
        struct.pack_into(
            "<IIII",
            md,
            off,
            record["vertex_offset"],
            record["index_offset"],
            record["vertex_count"],
            record["index_count"],
        )
        struct.pack_into("<3f", md, off + 0x10, *local_center)
        struct.pack_into("<f", md, off + 0x1C, 1.0)
        struct.pack_into("<3f", md, off + 0x20, *local_extent)
        struct.pack_into("<I", md, off + 0x2C, 0)
        struct.pack_into("<I", md, off + 0x30, record["index_count"] // 3)
        name_index = rule.force_name_index
        if name_index is None and source_index != mesh_index:
            source_rule = config.records[source_index]
            if source_rule.force_name_index is not None:
                name_index = source_rule.force_name_index
            else:
                name_index = struct.unpack_from("<H", native, source_off + 0x38)[0]
        if name_index is not None:
            struct.pack_into("<H", md, off + 0x38, name_index)
        if rule.force_layout_material is not None:
            struct.pack_into("<BB", md, off + 0x42, *rule.force_layout_material)
        if rule.force_palette_flags is not None:
            struct.pack_into("<H", md, off + 0x3A, rule.force_palette_flags[0])
            struct.pack_into("<H", md, off + 0x3C, rule.force_palette_flags[1])
    if config.unique_material_per_mesh:
        source_material_indices = [mesh.material_index for mesh in meshes]
        unique_names = [
            f"{config.native_material_names[source_index]}_{mesh_index:02d}"
            for mesh_index, source_index in enumerate(source_material_indices)
        ]
        md = bytearray(duplicate_g4md_material_records(md, source_material_indices, unique_names))
    apply_material_overrides(md, config.material_overrides or [])
    return bytes(md)


def material_core(material_name: str) -> str:
    name = material_name.lower()
    if name.startswith("mt_"):
        name = name[3:]
    name = re.sub(r"_uv\d+$", "", name)
    name = re.sub(r"_\d{3}$", "", name)
    return name


def texture_base_for_material(material_name: str, texture_dir: Path) -> str:
    core = material_core(material_name)
    candidates: list[str] = []
    if core == "hair_light":
        candidates.append("t_chr006a_h01_c")
    if core.startswith("c006"):
        tail = (core[6:] if core.startswith("c006a_") else core[4:]).lstrip("_")
        candidates.append("t_chr006a_" + tail + "_c")
        candidates.append("t_chr006a_" + tail + "_C")
        candidates.append("T_chr000a_" + tail + "_C")
        candidates.append("t_chr000a_" + tail + "_c")
    candidates.extend(
        [
            f"t_chr006a_{core}_c",
            f"t_chr006f_{core}_c",
            f"t_chr000a_{core}_c",
            f"T_chr000a_{core}_C",
        ]
    )
    available = {path.stem: path for path in texture_dir.glob("*.png")}
    lower_lookup = {name.lower(): name for name in available}
    for candidate in candidates:
        exact = lower_lookup.get(candidate.lower())
        if exact:
            return exact
    return "t_chr006a_h01_c" if "h" in core or "hair" in core else "t_chr006a_f01_c"


def texture_template_group(material_name: str) -> str:
    core = material_core(material_name)
    if "f01" in core or "face" in core:
        return "10"
    return "20"


def build_material_plans(meshes: list[Mesh], texture_dir: Path) -> list[MaterialPlan]:
    plans: list[MaterialPlan] = []
    for material in unique_materials(meshes):
        base = texture_base_for_material(material, texture_dir)
        refs = [base]
        for suffix in ("line", "oc", "sp", "spm"):
            refs.append(f"{base}{suffix}")
        plans.append(MaterialPlan(material, base, refs))
    return plans


def dds_dimensions(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"DDS ") or len(data) < 20:
        raise ValueError("not a DDS payload")
    height = struct.unpack_from("<I", data, 12)[0]
    width = struct.unpack_from("<I", data, 16)[0]
    return width, height


def texture_dimensions(data: bytes) -> tuple[int, int]:
    if data.startswith(b"DDS "):
        return dds_dimensions(data)
    if data.startswith(b"NXTCH000") and len(data) >= 0x1C:
        return struct.unpack_from("<II", data, 0x14)
    raise ValueError("unsupported G4TX texture payload")


def validate_g4tx_dimensions(name: str, width: int, height: int) -> None:
    if not 0 <= width <= 0xFFFF or not 0 <= height <= 0xFFFF:
        raise ValueError(
            f"texture {name!r} is {width}x{height}, but G4TX stores dimensions as uint16; "
            "reduce the replacement texture or generated spritesheet size"
        )


def native_dds_compression(payload: bytes) -> str:
    """Return the encoder name required to preserve a DX11 template format."""
    format_code, _ = dds_format(payload)
    return {0x42: "BC1", 0x44: "BC3", 0x4D: "BC7"}.get(format_code, "")


def dds_mip_data_size(width: int, height: int, format_code: int, mip_count: int) -> int:
    """Return the byte count occupied by all DDS mip levels."""
    block_bytes = {0x42: 8, 0x44: 16, 0x4D: 16}.get(format_code)
    if format_code == 0x25:
        return sum(max(1, width >> level) * max(1, height >> level) * 4 for level in range(mip_count))
    if block_bytes is None:
        raise ValueError(f"unsupported DDS format code: 0x{format_code:x}")
    return sum(
        max(1, ((width >> level) + 3) // 4)
        * max(1, ((height >> level) + 3) // 4)
        * block_bytes
        for level in range(mip_count)
    )


def normalize_replacement_dds(payload: bytes, native_payload: bytes) -> bytes:
    """Use the native DDS header contract while retaining replacement pixels.

    TextureConverter and texconv both produce valid BC payloads, but their
    header flags/pitch differ from the game's native files.  The DX11 loader
    reads those fields before G4TX metadata, so copy the template header and
    update only dimensions, mip count and the top-level linear size.
    """
    if not payload.startswith(b"DDS ") or not native_payload.startswith(b"DDS "):
        return payload
    replacement_format, replacement_offset = dds_format(payload)
    native_format, native_offset = dds_format(native_payload)
    if replacement_format != native_format or replacement_offset != native_offset:
        raise ValueError("DDS replacement format does not match the native texture header")
    if len(payload) < replacement_offset:
        raise ValueError("DDS replacement is truncated before its pixel payload")
    width, height = dds_dimensions(payload)
    mip_count = max(1, struct.unpack_from("<I", payload, 28)[0])
    expected_size = dds_mip_data_size(width, height, replacement_format, mip_count)
    actual_size = len(payload) - replacement_offset
    if actual_size < expected_size:
        raise ValueError(
            f"DDS replacement pixel payload is truncated: expected {expected_size} bytes, got {actual_size}"
        )
    header = bytearray(native_payload[:native_offset])
    flags = struct.unpack_from("<I", native_payload, 8)[0]
    if mip_count <= 1:
        flags &= ~0x20000  # DDSD_MIPMAPCOUNT
        if replacement_format == 0x25:
            flags = (flags & ~0x80000) | 0x8  # pitch, not compressed linear size
    struct.pack_into("<III", header, 8, flags, height, width)
    if replacement_format == 0x25:
        pitch = width * 4
    else:
        block_bytes = {0x42: 8, 0x44: 16, 0x4D: 16}[replacement_format]
        pitch = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block_bytes
    struct.pack_into("<I", header, 20, pitch)
    struct.pack_into("<I", header, 28, mip_count)
    # Keep caps coherent for a manually supplied one-level DDS while retaining
    # every other opaque field from the native template.
    if mip_count <= 1:
        struct.pack_into("<I", header, 108, struct.unpack_from("<I", header, 108)[0] & ~0x400008)
    return bytes(header) + payload[replacement_offset:]


def texture_converter_path() -> Path | None:
    configured = os.environ.get("LEVEL5_G4_TEXTURE_CONVERTER", "")
    candidates = [Path(configured)] if configured else []
    candidates.append(Path("/Applications/Xcode.app/Contents/Developer/usr/bin/TextureConverter"))
    texconv = shutil.which("texconv")
    if texconv:
        candidates.append(Path(texconv))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def png_to_dds(path: Path, native_payload: bytes) -> bytes:
    """Encode a PNG in the exact compressed DDS family expected by the template."""
    compression = native_dds_compression(native_payload)
    if not compression:
        raise RuntimeError(
            "The native DX11 texture format is unsupported for PNG replacement. "
            "Provide a DDS encoded like the original texture."
        )
    converter = texture_converter_path()
    if converter is None:
        raise RuntimeError(
            "A native DDS encoder is required to replace this DX11 texture. "
            "Install Xcode TextureConverter on macOS or DirectXTex texconv on Windows, "
            "or set LEVEL5_G4_TEXTURE_CONVERTER to its executable."
        )
    mip_count = max(1, struct.unpack_from("<I", native_payload, 28)[0])
    tmp = path.with_suffix(path.suffix + ".port_tmp.dds")
    try:
        if converter.name.casefold() == "texconv":
            command = [
                str(converter), "-f", f"{compression}_UNORM", "-m", str(mip_count),
                "-y", "-o", str(tmp.parent), str(path),
            ]
            output = tmp.parent / f"{path.stem}.DDS"
        else:
            command = [
                str(converter), f"--compression_format={compression}",
                # Xcode's NVTT Production/Normal BC7 path aborts on valid
                # transparent texels; ISPC Fastest remains deterministic and
                # writes the same DX10/BC7 contract as the native archive.
                "--compression_quality=Fastest", "--alpha_mode=Preserve",
                f"--max_mipmaps={mip_count}", "--file_format=DDS", f"--output={tmp}", str(path),
            ]
            output = tmp
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0 or not output.is_file():
            detail = (completed.stderr or completed.stdout or "unknown encoder failure").strip()
            raise RuntimeError(f"DDS encoding failed for {path.name}: {detail}")
        payload = output.read_bytes()
        if native_dds_compression(payload) != compression:
            raise RuntimeError(f"DDS encoder produced the wrong format for {path.name}")
        return payload
    finally:
        tmp.unlink(missing_ok=True)
        if converter.name.casefold() == "texconv":
            (tmp.parent / f"{path.stem}.DDS").unlink(missing_ok=True)


def replacement_texture(path: Path, native_payload: bytes) -> bytes:
    suffix = path.suffix.lower()
    if suffix == ".dds":
        payload = path.read_bytes()
    elif suffix == ".nxtch":
        payload = path.read_bytes()
        if not payload.startswith(b"NXTCH000"):
            raise ValueError(f"invalid NXTCH replacement: {path}")
    else:
        payload = png_to_dds(path, native_payload)

    native_is_nx = native_payload.startswith(b"NXTCH000")
    replacement_is_nx = payload.startswith(b"NXTCH000")
    if native_is_nx and not replacement_is_nx:
        return dds_to_nxtch(payload, native_payload)
    if not native_is_nx and replacement_is_nx:
        raise ValueError(f"NXTCH replacement cannot be written into a DX11 G4TX: {path}")
    if not native_is_nx and native_payload.startswith(b"DDS "):
        if native_dds_compression(payload) != native_dds_compression(native_payload):
            raise ValueError(
                f"DDS replacement format does not match the native DX11 texture: {path}. "
                "Use a DDS encoded in the same BC format as the original."
            )
        payload = normalize_replacement_dds(payload, native_payload)
    return payload


def parse_g4tx_payloads(path: Path) -> tuple[bytes, list[dict], dict[str, bytes]]:
    data = path.read_bytes()
    if data[:4] != b"G4TX":
        raise ValueError(f"{path} is not a G4TX")
    header = data[:0x60]
    texture_count = struct.unpack_from("<H", data, 0x20)[0]
    total_count = struct.unpack_from("<H", data, 0x22)[0]
    sub_count = data[0x25]
    pos = 0x60
    entries = []
    for index in range(texture_count):
        raw = bytearray(data[pos : pos + 0x30])
        values = struct.unpack_from("<IIIIIIHHI", raw, 0)
        entries.append({"index": index, "raw": raw, "offset": values[1], "size": values[2]})
        pos += 0x30
    pos += sub_count * 0x18
    pos = align(pos, 0x10)
    pos += total_count * 4
    ids = list(data[pos : pos + total_count])
    pos = align(pos + total_count, 4)
    string_offset_pos = pos
    string_offsets = list(struct.unpack_from("<" + "H" * total_count, data, pos))
    string_base = string_offset_pos
    texture_base = align(struct.unpack_from("<H", data, 0x04)[0] + struct.unpack_from("<I", data, 0x0C)[0], 0x10)
    payloads: dict[str, bytes] = {}
    for index, entry in enumerate(entries):
        absolute_name = string_base + string_offsets[index]
        end = data.find(b"\0", absolute_name)
        name = data[absolute_name:end].decode("ascii")
        entry["name"] = name
        entry["id"] = ids[index] if index < len(ids) else index
        payloads[name] = data[texture_base + entry["offset"] : texture_base + entry["offset"] + entry["size"]]
    return header, entries, payloads


def rebuild_g4tx(template_path: Path, material_plans: list[MaterialPlan], texture_dir: Path, out_path: Path) -> list[str]:
    header, template_entries, native_payloads = parse_g4tx_payloads(template_path)
    entries_by_name = {entry["name"]: entry for entry in template_entries}
    output_entries: list[dict] = []
    texture_names: list[str] = []
    for plan in material_plans:
        group = texture_template_group(plan.name)
        for texture_name in plan.texture_refs:
            if texture_name in texture_names:
                continue
            texture_names.append(texture_name)
            if texture_name == plan.base_texture:
                png_path = next(texture_dir.glob(f"{plan.base_texture}.png"), None)
                payload = (
                    replacement_texture(png_path, native_payloads[f"c01000010_{group}"])
                    if png_path else native_payloads[f"c01000010_{group}"]
                )
                template = entries_by_name[f"c01000010_{group}"]
            else:
                suffix = texture_name[len(plan.base_texture) :]
                native_name = f"c01000010_{group}{suffix}"
                fallback = f"c01000010_20{suffix}" if suffix in {"sp", "spm"} else f"c01000010_{group}"
                payload = native_payloads.get(native_name, native_payloads[fallback])
                template = entries_by_name.get(native_name, entries_by_name[fallback])
            output_entries.append({"name": texture_name, "payload": payload, "template": template})

    count = len(output_entries)
    entry_offset = 0x60
    hash_offset = align(entry_offset + count * 0x30, 0x10)
    id_offset = hash_offset + count * 4
    string_offset = align(id_offset + count, 4)
    string_content_offset = align(string_offset + count * 2 + 7, 4)
    string_size = sum(len(entry["name"]) + 1 for entry in output_entries)
    data_offset = align(string_content_offset + string_size, 0x10)

    out = bytearray(data_offset)
    out[:0x60] = header
    struct.pack_into("<H", out, 0x20, count)
    struct.pack_into("<H", out, 0x22, count)
    struct.pack_into("<B", out, 0x25, 0)

    cursor = string_content_offset
    for index, entry in enumerate(output_entries):
        struct.pack_into("<H", out, string_offset + index * 2, cursor - string_offset)
        out[cursor : cursor + len(entry["name"])] = entry["name"].encode("ascii")
        cursor += len(entry["name"]) + 1
        struct.pack_into("<I", out, hash_offset + index * 4, crc32b(entry["name"]))
        out[id_offset + index] = index & 0xFF

    data_cursor = data_offset
    for index, entry in enumerate(output_entries):
        payload = entry["payload"]
        entry_raw = bytearray(entry["template"]["raw"])
        width, height = dds_dimensions(payload)
        validate_g4tx_dimensions(entry["name"], width, height)
        struct.pack_into("<II", entry_raw, 4, data_cursor - data_offset, len(payload))
        struct.pack_into("<HH", entry_raw, 0x18, width, height)
        out[entry_offset + index * 0x30 : entry_offset + (index + 1) * 0x30] = entry_raw
        if len(out) < data_cursor:
            out.extend(b"\0" * (data_cursor - len(out)))
        out.extend(payload)
        data_cursor = align(len(out), 0x10)
        if len(out) < data_cursor:
            out.extend(b"\0" * (data_cursor - len(out)))

    table_size = align(cursor - 0x60, 4)
    struct.pack_into("<I", out, 0x0C, table_size)
    struct.pack_into("<I", out, 0x2C, len(out) - data_offset)
    out_path.write_bytes(bytes(out))
    return texture_names


def rebuild_native_g4tx_with_custom_textures(
    template_path: Path, custom_dir: Path, out_path: Path, replacements: dict[str, str]
) -> list[str]:
    template_data = template_path.read_bytes()
    header, template_entries, native_payloads = parse_g4tx_payloads(template_path)
    replacement_paths = {name: custom_dir / rel_path for name, rel_path in replacements.items()}
    output_entries: list[dict] = []
    for entry in template_entries:
        name = entry["name"]
        replacement_path = replacement_paths.get(name)
        payload = (
            replacement_texture(replacement_path, native_payloads[name])
            if replacement_path is not None and replacement_path.exists()
            else native_payloads[name]
        )
        output_entries.append({"name": name, "payload": payload, "template": entry})

    if all(entry["payload"] == native_payloads[entry["name"]] for entry in output_entries):
        out_path.write_bytes(template_data)
        return [entry["name"] for entry in output_entries]

    # The DX11 loader is sensitive to the template's opaque table layout.  Keep
    # its hashes, IDs, strings and table size intact; only records and payloads
    # are replaced.  Rebuilding those tables shifted the texture base by 16 B.
    entry_offset = 0x60
    data_offset = align(struct.unpack_from("<H", template_data, 0x04)[0] + struct.unpack_from("<I", template_data, 0x0C)[0], 0x10)
    out = bytearray(template_data[:data_offset])
    data_cursor = data_offset
    for index, entry in enumerate(output_entries):
        payload = entry["payload"]
        entry_raw = bytearray(entry["template"]["raw"])
        width, height = texture_dimensions(payload)
        validate_g4tx_dimensions(entry["name"], width, height)
        struct.pack_into("<II", entry_raw, 4, data_cursor - data_offset, len(payload))
        struct.pack_into("<HH", entry_raw, 0x18, width, height)
        out[entry_offset + index * 0x30 : entry_offset + (index + 1) * 0x30] = entry_raw
        if len(out) < data_cursor:
            out.extend(b"\0" * (data_cursor - len(out)))
        out.extend(payload)
        data_cursor = align(len(out), 0x10)
        if len(out) < data_cursor:
            out.extend(b"\0" * (data_cursor - len(out)))

    struct.pack_into("<I", out, 0x2C, len(out) - data_offset)
    out_path.write_bytes(bytes(out))
    return [entry["name"] for entry in output_entries]


def variant_texture_replacements(
    template_path: Path, model_stem: str, replacements: dict[str, str]
) -> dict[str, str]:
    """Translate main-model replacement names to a suffixed G4TX variant.

    Character folders commonly carry c*_00/c*_02/c*_08 G4TX companions.  The
    texture names in those files include the companion suffix, while the
    generated atlas is named after the main model.  Keep this mapping explicit
    so only matching texture slots are propagated; unrelated variant entries
    remain native.
    """
    variant_stem = template_path.stem
    prefix = f"{model_stem}_"
    if not model_stem or variant_stem == model_stem or not variant_stem.startswith(prefix):
        return {}
    variant_replacements: dict[str, str] = {}
    for main_name, replacement in replacements.items():
        if not main_name.startswith(prefix):
            continue
        variant_name = variant_stem + main_name[len(model_stem) :]
        variant_replacements[variant_name] = replacement
    return variant_replacements


def prepare_port_geometry(
    source_dae: Path | None,
    raw_root: Path,
    config: PortConfig,
    weight_sidecar: Path | None,
) -> tuple[list[Mesh], list[Mesh], list[list[int]], list[list[int]], list[dict]]:
    model_stem = config.model_rel.name
    source_meshes = (
        parse_dae(
            source_dae,
            model_stem,
            weight_sidecar=weight_sidecar,
            generate_tangents=config.generate_tangents,
            apply_bind_shape=config.apply_bind_shape,
        )
        if source_dae
        else calibration_mesh()
    )
    meshes = merged_native_meshes(source_meshes, config)
    # Resolve UV fitting only after Collada has been parsed and modifiers are
    # already baked into the actual exported mesh.  This prevents a mirrored or
    # modifier-expanded face from using stale pre-export Blender UV bounds.
    for mesh, rule in zip(meshes, config.records):
        if rule.uv_fit_display_rect is None:
            continue
        effective_flip = rule.uv_flip if rule.uv_flip is not None else config.uv_flip
        rule.uv_scale, rule.uv_offset = fit_mesh_uv_to_display_rect(
            mesh, rule.uv_fit_display_rect, bool(effective_flip[1])
        )
    native_g4md = (raw_root / config.common_rel).read_bytes()
    native_palettes = native_record_palettes(native_g4md)
    native_joint_indices = native_joint_name_indices(native_g4md)
    palettes = configured_record_palettes(native_palettes, config.records, meshes, config.joint_aliases or {}, native_joint_indices)
    resolved_records = []
    for mesh, palette in zip(meshes, palettes):
        source_index = _source_record_index(mesh, len(config.records), len(config.records))
        rule = config.records[source_index] if source_index < len(config.records) else config.records[0]
        resolved = [
            resolve_vertex_influences(
                vertex,
                palette,
                rule,
                config.joint_aliases or {},
                native_joint_indices,
                )
            for vertex in mesh.vertices
        ]
        resolved_records.append({
            "record": mesh.name,
            "source_names": list(mesh.source_names),
            "uv_transform_trace": list(mesh.uv_transform_trace),
            "vertices": len(mesh.vertices),
            "indices": len(mesh.indices),
            "weighted_vertices": sum(bool(vertex.influences) for vertex in mesh.vertices),
            "max_influences": max((len(vertex.influences) for vertex in mesh.vertices), default=0),
            "unresolved_influences": sum(item[1] for item in resolved),
            "palette": palette,
            "uv_scale": list(rule.uv_scale),
            "uv_offset": list(rule.uv_offset),
            "uv_fit_display_rect": list(rule.uv_fit_display_rect) if rule.uv_fit_display_rect is not None else None,
        })
    return source_meshes, meshes, native_palettes, palettes, resolved_records


def analyze_port(
    source_dae: Path | None,
    raw_root: Path,
    config: PortConfig,
    weight_sidecar: Path | None = None,
    skeleton_path: Path | None = None,
    chara_model: Path = DEFAULT_CHARA_MODEL,
) -> dict:
    source_meshes, meshes, native_palettes, palettes, records = prepare_port_geometry(
        source_dae, raw_root, config, weight_sidecar
    )
    expected_skeleton = resolve_expected_skeleton(config.model_rel, raw_root, chara_model)
    skeleton = validate_skeleton_palettes(palettes, skeleton_path, expected_skeleton)
    recommendations = []
    unresolved = sum(record["unresolved_influences"] for record in records)
    if unresolved:
        recommendations.append("Add joint_aliases or expand the affected record palettes before exporting.")
    if skeleton["status"] == "expected_missing":
        recommendations.append(f"Provide --g4sk {skeleton['expected_path']} to validate the palette against the assigned skeleton.")
    elif skeleton["status"] == "missing_joints":
        recommendations.append("Remove or remap palette joints missing from the assigned G4SK.")
    elif skeleton["status"] == "invalid_bind_matrices":
        recommendations.append("Use a different G4SK: its bind and inverse-bind matrices are not mutually consistent.")
    if any(len(mesh.vertices) > 60000 for mesh in meshes):
        recommendations.append("A record is close to the 65535 vertex limit; reserve another native slot or simplify the mesh.")
    if source_dae and not any(vertex.influences for mesh in source_meshes for vertex in mesh.vertices):
        recommendations.append("The DAE contains no skin weights; use --weights-json or a Collada export with controllers.")
    if not config.generate_tangents:
        recommendations.append("Enable generate_tangents for materials that use normal/specular direction maps.")
    return {
        "mode": "analyze",
        "model_rel": config.model_rel.as_posix(),
        "source": str(source_dae) if source_dae else None,
        "source_meshes": [
            {
                "name": mesh.name,
                "material": mesh.material_name,
                "vertices": len(mesh.vertices),
                "indices": len(mesh.indices),
                "weighted_vertices": sum(bool(vertex.influences) for vertex in mesh.vertices),
                "uv_bounds": [
                    [min((vertex.uv[axis] for vertex in mesh.vertices), default=0.0), max((vertex.uv[axis] for vertex in mesh.vertices), default=0.0)]
                    for axis in range(2)
                ],
            }
            for mesh in source_meshes
        ],
        "records": records,
        "native_palettes": native_palettes,
        "palettes": palettes,
        "palette_expanded": palettes != native_palettes,
        "unresolved_influences": unresolved,
        "expected_skeleton": expected_skeleton,
        "skeleton_validation": skeleton,
        "recommendations": recommendations,
    }


def write_port(
    source_dae: Path | None,
    raw_root: Path,
    out_root: Path,
    config: PortConfig,
    texture_mode: str = "custom",
    weight_sidecar: Path | None = None,
    skeleton_path: Path | None = None,
    chara_model: Path = DEFAULT_CHARA_MODEL,
) -> dict:
    source_meshes, meshes, native_palettes, palettes, _ = prepare_port_geometry(
        source_dae, raw_root, config, weight_sidecar
    )
    if source_dae is not None and weight_sidecar is not None:
        sidecar_meshes = load_weight_sidecar(weight_sidecar)
        sidecar_has_skin = any(
            any(vertex for vertex in mesh.get("influences", []))
            for mesh in sidecar_meshes
        )
        if sidecar_has_skin and not any(
            vertex.influences for mesh in source_meshes for vertex in mesh.vertices
        ):
            raise ValueError(
                "The exported mesh lost all skin weights while applying modifiers. "
                "Remove the topology-changing modifier or use a skin-preserving export."
            )
    native_model = raw_root / config.common_rel
    native_md = native_model.read_bytes() if native_model.is_file() else b""
    native_mg = native_model.with_suffix(".g4mg")
    if native_md and native_mg.is_file():
        transfer_native_vertex_colors(meshes, native_record_vertex_colors(native_md, native_mg.read_bytes()), [])
    vertex_color_vertices = apply_vertex_color_policy(
        meshes, config.vertex_color_mode, config.vertex_color
    )
    apply_record_outline_rules(meshes, config.records)
    native_joint_indices = native_joint_name_indices(native_md) if native_md else {}
    native_uv_formats = native_record_uv_formats(native_md) if native_md else []
    g4mg, records = build_g4mg(
        meshes,
        config.uv_flip,
        config.records,
        palettes,
        config.joint_aliases or {},
        native_joint_indices,
        uv_formats=native_uv_formats,
        joint_position_offsets=config.joint_position_offsets,
        joint_position_transforms=config.joint_position_transforms,
        source_joint_position_transforms=config.source_joint_position_transforms,
    )
    unresolved_influences = sum(record["unresolved_influences"] for record in records)
    if config.strict_skinning and unresolved_influences:
        raise ValueError(
            f"{unresolved_influences} skin influences could not be represented by their native record palettes"
        )
    expected_skeleton = resolve_expected_skeleton(config.model_rel, raw_root, chara_model)
    skeleton_validation = validate_skeleton_palettes(palettes, skeleton_path, expected_skeleton)
    if config.strict_skinning and skeleton_validation["status"] in {"missing_joints", "invalid_bind_matrices"}:
        raise ValueError(f"assigned G4SK validation failed: {skeleton_validation}")

    common_out = out_root / config.common_rel
    mg_out = common_out.with_suffix(".g4mg")
    texture_rel = config.texture_rel(raw_root)
    texture_template = raw_root / texture_rel
    texture_out = out_root / texture_rel
    common_out.parent.mkdir(parents=True, exist_ok=True)
    texture_out.parent.mkdir(parents=True, exist_ok=True)
    g4md = build_base_g4md(meshes, records, raw_root, config)
    g4md = rewrite_joint_palettes(g4md, palettes)
    validation = validate_generated_model(g4md, g4mg)
    custom_texture_dir = source_dae.parent / "customTextures" if source_dae else Path()
    if texture_mode == "custom":
        texture_names = rebuild_native_g4tx_with_custom_textures(
            texture_template, custom_texture_dir, texture_out, config.texture_replacements
        )
    elif texture_mode == "native":
        texture_out.write_bytes(texture_template.read_bytes())
        _, template_entries, _ = parse_g4tx_payloads(texture_template)
        texture_names = [entry["name"] for entry in template_entries]
    elif texture_mode == "keep":
        if not texture_out.exists():
            texture_out.write_bytes(texture_template.read_bytes())
        _, template_entries, _ = parse_g4tx_payloads(texture_out)
        texture_names = [entry["name"] for entry in template_entries]
    else:
        raise ValueError(f"unsupported texture mode {texture_mode!r}")
    common_out.write_bytes(g4md)
    mg_out.write_bytes(g4mg)

    model_stem = Path(config.model_rel).name
    for source in texture_template.parent.glob("*.g4tx"):
        if source.name == texture_out.name:
            continue
        destination = texture_out.parent / source.name
        variant_replacements = (
            variant_texture_replacements(source, model_stem, config.texture_replacements)
            if texture_mode == "custom"
            else {}
        )
        if variant_replacements:
            rebuild_native_g4tx_with_custom_textures(
                source, custom_texture_dir, destination, variant_replacements
            )
        else:
            destination.write_bytes(source.read_bytes())

    return {
        "meshes": len(meshes),
        "materials": struct.unpack_from("<H", g4md, 0x22)[0],
        "textures": len(texture_names),
        "vertices": sum(len(mesh.vertices) for mesh in meshes),
        "indices": sum(len(mesh.indices) for mesh in meshes),
        "source_meshes": len(source_meshes),
        "weighted_vertices": sum(record["weighted_vertices"] for record in records),
        "tangent_vertices": sum(vertex.tangent is not None for mesh in meshes for vertex in mesh.vertices),
        "vertex_color_mode": config.vertex_color_mode,
        "vertex_color": list(config.vertex_color),
        "vertex_color_vertices": vertex_color_vertices,
        "unresolved_influences": unresolved_influences,
        "palettes": [record["palette"] for record in records],
        "native_palettes": native_palettes,
        "native_uv_formats": native_uv_formats,
        "palette_expanded": palettes != native_palettes,
        "validation": validation,
        "expected_skeleton": expected_skeleton,
        "skeleton_validation": skeleton_validation,
        "record_assignments": {mesh.name: list(mesh.source_names) for mesh in meshes},
        "uv_record_transforms": {
            rule.output_name: {
                "scale": list(rule.uv_scale),
                "offset": list(rule.uv_offset),
                "fit_display_rect": list(rule.uv_fit_display_rect) if rule.uv_fit_display_rect is not None else None,
            }
            for rule in config.records
        },
        "g4md": str(common_out),
        "g4mg": str(mg_out),
        "g4tx": str(texture_out),
        "texture_platform": texture_rel.parts[0],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Experimental DAE to Level-5 G4MD/G4MG/G4TX port writer.")
    parser.add_argument("dae", nargs="?", type=Path, help="Optional source Collada DAE.")
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--out-root", type=Path, default=Path("data"))
    parser.add_argument("--config", type=Path, help="Optional port preset JSON.")
    parser.add_argument("--analyze", action="store_true", help="Analyze source, records, palettes and skeleton without writing model files.")
    parser.add_argument("--g4sk", type=Path, help="Assigned G4SK used to validate compact palette joints by name.")
    parser.add_argument("--chara-model", type=Path, default=DEFAULT_CHARA_MODEL, help="chara_model XML used to resolve the expected body skeleton.")
    parser.add_argument(
        "--weights-json",
        type=Path,
        help="Optional Blender weight sidecar for DAE files that lost their Collada skin controllers.",
    )
    parser.add_argument("--report-json", type=Path, help="Write the export diagnostics as JSON.")
    parser.add_argument(
        "--texture-mode",
        choices=("custom", "native", "keep"),
        default="custom",
        help="custom=rebuild G4TX from preset replacements, native=copy original G4TX, keep=preserve existing output G4TX.",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    if args.analyze:
        result = analyze_port(
            args.dae,
            args.raw_root,
            config,
            args.weights_json,
            args.g4sk,
            args.chara_model,
        )
    else:
        result = write_port(
            args.dae,
            args.raw_root,
            args.out_root,
            config,
            args.texture_mode,
            args.weights_json,
            args.g4sk,
            args.chara_model,
        )
    result["g4_port_script_version"] = G4_PORT_SCRIPT_VERSION
    result["g4_port_converter_marker"] = G4_PORT_CONVERTER_MARKER
    result["g4_port_converter_source"] = str(Path(__file__).resolve())
    if config.producer_plugin_version:
        result["g4_blender_plugin_version"] = config.producer_plugin_version
    if config.producer_expected_script_version:
        result["g4_port_script_version_expected"] = config.producer_expected_script_version
        result["g4_port_script_version_match"] = (
            config.producer_expected_script_version == G4_PORT_SCRIPT_VERSION
        )
    if config.producer_marker:
        result["g4_blender_addon_marker"] = config.producer_marker
    if config.producer_source:
        result["g4_blender_addon_source"] = config.producer_source
    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(result, indent=2))
    if args.analyze:
        print(json.dumps(result, indent=2))
        return
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
