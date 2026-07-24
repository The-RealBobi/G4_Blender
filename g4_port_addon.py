import json
import hashlib
import math
import os
import re
import shutil
import shlex
import struct
import subprocess
import sys
import tempfile
import time
from array import array
from pathlib import Path

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup, UIList
from bpy_extras.io_utils import ExportHelper, ImportHelper

try:
    from .g4_roundtrip import native_mesh_signature
except ImportError:
    from g4_roundtrip import native_mesh_signature


ADDON_ID = __name__.split(".", 1)[0] if "." in __name__ else __name__
IS_STANDALONE_ADDON = False
MODEL_EXTENSIONS = {".g4md", ".g4pkm"}
MAX_GENERATED_TEXTURE_SIZE = 2048
FACE_ATLAS_COLUMNS = 4
FACE_ATLAS_ROWS = 2
FACE_ATLAS_SLOTS = FACE_ATLAS_COLUMNS * FACE_ATLAS_ROWS


def default_python() -> str:
    for candidate in ("/usr/bin/python3", "/opt/homebrew/bin/python3", sys.executable, "python3"):
        if candidate and Path(candidate).exists():
            return candidate
    return "python3"


def addon_root() -> Path:
    return Path(__file__).resolve().parent


def default_port_script() -> str:
    env_path = os.environ.get("LEVEL5_G4_PORT")
    candidates = [Path(env_path)] if env_path else []
    root = addon_root()
    candidates.extend(
        [
            root / "g4_port.py",
        ]
    )
    return next((str(path) for path in candidates if path.is_file()), "")


def default_config_dir() -> str:
    env_path = os.environ.get("LEVEL5_G4_PORT_CONFIGS")
    candidates = [Path(env_path)] if env_path else []
    root = addon_root()
    candidates.extend(
        [
            root,
            root / "configs",
        ]
    )
    return next((str(path) for path in candidates if path.is_dir()), "")


def default_probe_script() -> str:
    env_path = os.environ.get("LEVEL5_G4_PROBE")
    candidates = [Path(env_path)] if env_path else []
    root = addon_root()
    candidates.extend(
        [
            root / "g4_model_probe.py",
        ]
    )
    return next((str(path) for path in candidates if path.is_file()), "")


def default_cache_dir() -> str:
    return str(Path(tempfile.gettempdir()) / "level5_g4_port_blender")


def default_output_root() -> str:
    return str(Path.home() / "level5_g4_port_package")


def addon_preferences():
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is not None:
        return addon.preferences

    class Defaults:
        python_path = default_python()
        port_script = default_port_script()
        config_dir = default_config_dir()
        raw_data_root = os.environ.get("LEVEL5_G4_RAW_ROOT", "")
        output_root = os.environ.get("LEVEL5_G4_OUT_ROOT", default_output_root())
        chara_model_xml = os.environ.get("LEVEL5_G4_CHARA_MODEL", "")
        cache_dir = default_cache_dir()
        keep_temporary_files = False

    return Defaults()


def port_log(log_path: Path | None, message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    print(f"[G4 Port] {line}", flush=True)
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except Exception:
        pass


def resolve_file(path: str, fallback: str = "") -> Path:
    configured = bpy.path.abspath(path or fallback or "")
    return Path(configured) if configured else Path()


def resolve_port_script(prefs) -> Path:
    script = resolve_file(getattr(prefs, "port_script", ""), default_port_script())
    if script.is_file():
        return script
    raise RuntimeError("g4_port.py was not found. Configure it in the addon preferences.")


def resolve_probe_script(prefs) -> Path:
    script = resolve_file(getattr(prefs, "probe_script", ""), default_probe_script())
    if script.is_file():
        return script
    raise RuntimeError("g4_model_probe.py was not found. Configure it in the addon preferences.")


def config_path(name: str, prefs=None) -> Path:
    prefs = prefs or addon_preferences()
    directory = resolve_file(getattr(prefs, "config_dir", ""), default_config_dir())
    return directory / name


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def join_csv(values) -> str:
    return ", ".join(str(item) for item in values)


def mesh_objects(selected_only: bool) -> list[bpy.types.Object]:
    objects = bpy.context.selected_objects if selected_only else bpy.data.objects
    return [obj for obj in objects if obj.type == "MESH"]


def blender_base_name(name: str) -> str:
    return name.rsplit(".", 1)[0] if name.rsplit(".", 1)[-1].isdigit() else name


def material_image_path(material) -> str:
    if material is None or material.node_tree is None:
        return ""
    for node in material.node_tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None and node.image.filepath:
            return bpy.path.abspath(node.image.filepath)
    return ""


def first_used_material_image(obj: bpy.types.Object) -> str:
    """Return the first usable diffuse image from a material actually used by the mesh."""
    if obj is None or obj.type != "MESH":
        return ""
    used_indices = []
    for polygon in obj.data.polygons:
        if polygon.material_index not in used_indices:
            used_indices.append(polygon.material_index)
    for index in used_indices:
        if 0 <= index < len(obj.data.materials):
            path = material_image_path(obj.data.materials[index])
            if path:
                return path
    return material_image_path(obj.active_material)


def active_material_image_path(context) -> str:
    obj = context.active_object
    if obj is None or obj.type != "MESH":
        return ""
    return material_image_path(obj.active_material)


def mesh_weights(obj: bpy.types.Object) -> dict:
    group_names = {group.index: group.name for group in obj.vertex_groups}
    influences = []
    for vertex in obj.data.vertices:
        weights = [
            [group_names[item.group], item.weight]
            for item in vertex.groups
            if item.group in group_names and item.weight > 0.0
        ]
        weights.sort(key=lambda item: item[1], reverse=True)
        influences.append(weights[:8])
    return {
        "name": obj.name,
        "vertex_count": len(obj.data.vertices),
        "influences": influences,
    }


def write_weights_json(path: Path, selected_only: bool) -> int:
    meshes = [mesh_weights(obj) for obj in mesh_objects(selected_only)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "meshes": meshes}, indent=2), encoding="utf-8")
    return len(meshes)


def export_collada(path: Path, selected_only: bool, align_forward_to_y: bool, apply_modifiers: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "filepath": str(path),
        "selected": selected_only,
        "apply_modifiers": apply_modifiers,
        "triangulate": True,
    }
    if align_forward_to_y:
        kwargs.update(
            {
                "apply_global_orientation": True,
                "export_global_forward_selection": "Y",
                "export_global_up_selection": "Z",
                "export_object_transformation_type_selection": "matrix",
            }
        )
    bpy.ops.wm.collada_export(**kwargs)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Could not read {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON preset: {path}") from exc


TEXTURE_MODE_ITEMS = [
    ("custom", "Rebuild Custom G4TX", "Copy the original G4TX and replace selected texture entries"),
    ("native", "Use Native G4TX", "Copy the original G4TX files from the raw data root"),
    ("keep", "Keep Existing G4TX", "Preserve the G4TX already present in the output folder"),
]

TEXTURE_PLATFORM_ITEMS = [
    ("auto", "Automatic (DX11, then NX)", "Use DX11 when available, otherwise Nintendo Switch NX"),
    ("dx11", "Windows / DX11", "Read and write DDS payloads under data/dx11"),
    ("nx", "Nintendo Switch / NX", "Read and write NXTCH payloads under data/nx"),
]


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def parse_g4tx_entries(path: Path) -> list[dict]:
    data = path.read_bytes()
    if data[:4] != b"G4TX":
        raise ValueError(f"{path} is not a G4TX")
    texture_count = struct.unpack_from("<H", data, 0x20)[0]
    total_count = struct.unpack_from("<H", data, 0x22)[0]
    sub_count = data[0x25]
    pos = 0x60
    entries = []
    for index in range(texture_count):
        raw = data[pos : pos + 0x30]
        values = struct.unpack_from("<IIIIIIHHI", raw, 0)
        entries.append({"index": index, "width": values[6] or 1024, "height": values[7] or 1024})
        pos += 0x30
    pos += sub_count * 0x18
    pos = align(pos, 0x10)
    pos += total_count * 4
    pos = align(pos + total_count, 4)
    string_base = pos
    offsets = list(struct.unpack_from("<" + "H" * total_count, data, pos))
    for entry, offset in zip(entries, offsets[:texture_count]):
        start = string_base + offset
        end = data.find(b"\0", start)
        if end > start:
            entry["name"] = data[start:end].decode("ascii", errors="replace")
    return [entry for entry in entries if entry.get("name")]


def parse_g4tx_names(path: Path) -> list[str]:
    return [entry["name"] for entry in parse_g4tx_entries(path)]


def infer_data_root(path: Path) -> Path | None:
    resolved = path.resolve()
    for parent in resolved.parents:
        if parent.name == "data" and (parent / "common").is_dir():
            return parent
    parts = resolved.parts
    if "common" in parts:
        index = parts.index("common")
        return Path(*parts[:index]) if index > 0 else None
    for platform in ("dx11", "nx"):
        if platform in parts:
            index = parts.index(platform)
            return Path(*parts[:index]) if index > 0 else None
    return None


def relative_model_from_data(path: Path, data_root: Path) -> str:
    rel = path.with_suffix("").resolve().relative_to(data_root.resolve())
    parts = rel.parts
    if parts and parts[0] in {"common", "dx11", "nx"}:
        parts = parts[1:]
    return Path(*parts).as_posix()


def dx11_g4tx_for_model(data_root: Path, model_rel: str) -> Path:
    return data_root / "dx11" / Path(model_rel).with_suffix(".g4tx")


def nx_g4tx_for_model(data_root: Path, model_rel: str) -> Path:
    return data_root / "nx" / Path(model_rel).with_suffix(".g4tx")


def common_g4tx_for_model(data_root: Path, model_rel: str) -> Path:
    return data_root / "common" / Path(model_rel).with_suffix(".g4tx")


def texture_key_for_record(record_name: str, texture_names: list[str]) -> str:
    if record_name in texture_names:
        return record_name
    prefix_matches = [name for name in texture_names if name == record_name or name.startswith(f"{record_name}_")]
    if prefix_matches:
        return prefix_matches[0]
    stem = record_name.rsplit("_", 1)[0]
    stem_matches = [name for name in texture_names if name == stem or name.startswith(f"{stem}_")]
    return stem_matches[0] if stem_matches else ""


def shared_face_texture_key(texture_names: list[str]) -> str:
    """Return the native base map shared by the eye and mouth expression meshes."""
    return next((name for name in texture_names if re.search(r"_10$", name) and not is_special_texture(name)), "")


def is_face_atlas_record(record) -> bool:
    name = record.output_name.lower()
    material = record.material_name.lower().removesuffix("m")
    return name in {"eye_10", "mouth_10"} or material in {"eye_10", "mouth_10"}


def assign_shared_face_texture_key(records, texture_names: list[str]) -> str:
    key = shared_face_texture_key(texture_names)
    if not key:
        return ""
    for record in records:
        if is_face_atlas_record(record):
            record.texture_key = key
    return key


def target_mesh_items(self, context):
    props = settings(context)
    items = [("__none__", "Unassigned", "Do not export this object into a native mesh")]
    for record in props.records:
        label = record.output_name or f"mesh_{record.original_index}"
        items.append((label, label, record.material_name or "Original mesh"))
    return items


COMMON_JOINT_ALIASES = {
    "head": "c_head_1_0",
    "neck": "c_n_1_0",
    "hair": "c_hir1_1_0",
    "l_hair": "l_hir1_1_0",
    "r_hair": "r_hir1_1_0",
    "l_hair_a01": "l_hir1_1_0",
    "l_hair_a02": "l_hir1_1_1",
    "l_hair_a03": "l_hir2_1_0",
    "l_hair_b01": "l_hir2_1_1",
    "l_hair_b02": "l_hir2_1_2",
    "l_hair_b03": "l_hir3_1_0",
    "r_hair_a01": "r_hir1_1_0",
    "r_hair_a02": "r_hir1_1_1",
    "r_hair_a03": "r_hir2_1_0",
    "r_hair_b01": "r_hir2_1_1",
    "r_hair_b02": "r_hir2_1_2",
    "r_hair_b03": "r_hir3_1_0",
    "l_ribbon_a01": "l_hir3_1_1",
    "l_ribbon_b01": "l_hir4_1_0",
    "l_ribbon_b02": "l_hir4_1_0",
    "r_ribbon_a01": "r_hir3_1_1",
    "r_ribbon_b01": "r_hir4_1_0",
    "r_ribbon_b02": "r_hir4_1_0",
}


def normalize_joint_key(name: str) -> str:
    return blender_base_name(name).strip().lower().replace("-", "_").replace(" ", "_")


def guess_joint_alias(group_name: str) -> str:
    key = normalize_joint_key(group_name)
    if key in COMMON_JOINT_ALIASES:
        return COMMON_JOINT_ALIASES[key]
    if key.startswith("l_") and "hair" in key:
        return "l_hir1_1_0"
    if key.startswith("r_") and "hair" in key:
        return "r_hir1_1_0"
    if "hair" in key:
        return "c_hir1_1_0"
    if "head" in key:
        return "c_head_1_0"
    if "neck" in key:
        return "c_n_1_0"
    return ""


def vertex_group_names(selected_only: bool = False) -> list[str]:
    names = []
    for obj in mesh_objects(selected_only):
        names.extend(group.name for group in obj.vertex_groups)
    return sorted(dict.fromkeys(names))


class G4PortPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    python_path: StringProperty(
        name="Python",
        subtype="FILE_PATH",
        default=default_python(),
        description="Python executable used to run g4_port.py",
    )
    port_script: StringProperty(
        name="G4 Port Script",
        subtype="FILE_PATH",
        default=default_port_script(),
        description="Path to bundled or external g4_port.py; bundled installations detect it automatically",
    )
    config_dir: StringProperty(
        name="Preset Folder",
        subtype="DIR_PATH",
        default=default_config_dir(),
        description="Folder containing G4 port presets",
    )
    probe_script: StringProperty(
        name="Model Probe Script",
        subtype="FILE_PATH",
        default=default_probe_script(),
        description="Path to bundled or external g4_model_probe.py; used to build records from an original model",
    )
    raw_data_root: StringProperty(
        name="Raw Data Root",
        subtype="DIR_PATH",
        default=os.environ.get("LEVEL5_G4_RAW_ROOT", ""),
        description="Fallback data root containing common/ and dx11/. The selected original model normally defines this automatically",
    )
    output_root: StringProperty(
        name="Package Folder",
        subtype="DIR_PATH",
        default=os.environ.get("LEVEL5_G4_OUT_ROOT", default_output_root()),
        description="Destination folder. The addon writes a data/common and data/dx11 filesystem inside it",
    )
    chara_model_xml: StringProperty(
        name="Chara Model XML",
        subtype="FILE_PATH",
        default=os.environ.get("LEVEL5_G4_CHARA_MODEL", ""),
        description="Optional chara_model XML used by g4_port.py to resolve skeletons",
    )
    cache_dir: StringProperty(
        name="Export Cache",
        subtype="DIR_PATH",
        default=default_cache_dir(),
        description="Temporary folder for DAE, weights, generated presets and reports",
    )
    keep_temporary_files: BoolProperty(
        name="Keep Temporary Files",
        default=False,
        description="Keep generated DAE/config/weights files after export",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "python_path")
        layout.prop(self, "port_script")
        layout.prop(self, "config_dir")
        layout.prop(self, "probe_script")
        layout.prop(self, "raw_data_root")
        layout.prop(self, "output_root")
        layout.prop(self, "chara_model_xml")
        layout.prop(self, "cache_dir")
        layout.prop(self, "keep_temporary_files")


class G4PortObjectSettings(PropertyGroup):
    target_record: EnumProperty(name="Target Mesh", items=target_mesh_items)
    source_texture: StringProperty(
        name="Atlas Source",
        subtype="FILE_PATH",
        default="",
        description="Optional image override. Empty uses the first diffuse image from a material used by this mesh",
    )
    uv_scale_u: FloatProperty(name="U Scale", default=1.0, min=0.0001, soft_max=1.0)
    uv_scale_v: FloatProperty(name="V Scale", default=1.0, min=0.0001, soft_max=1.0)
    uv_offset_u: FloatProperty(name="U Offset", default=0.0, soft_min=0.0, soft_max=1.0)
    uv_offset_v: FloatProperty(name="V Offset", default=0.0, soft_min=0.0, soft_max=1.0)


class G4PortJointAlias(PropertyGroup):
    source_group: StringProperty(name="Blender Group", default="")
    target_joint: StringProperty(name="Level-5 Joint", default="")


class G4PortTextureReplacement(PropertyGroup):
    texture_name: StringProperty(name="Texture", default="")
    replacement_path: StringProperty(
        name="Replacement",
        subtype="FILE_PATH",
        default="",
        description="Leave empty to preserve this G4TX texture",
    )
    atlas_signature: StringProperty(default="", options={"HIDDEN"})
    atlas_summary: StringProperty(default="", options={"HIDDEN"})
    expression_atlas: BoolProperty(default=False, options={"HIDDEN"})
    expression_atlas_mode: EnumProperty(
        items=[("pool", "Pool", "Built from the eight expression-pool cells"), ("existing", "Existing", "Use an already prepared 4x2 facial atlas")],
        default="pool",
        options={"HIDDEN"},
    )


class G4PortExpressionImage(PropertyGroup):
    image_path: StringProperty(name="Expression", subtype="FILE_PATH", default="")


class G4PortRecord(PropertyGroup):
    output_name: StringProperty(name="Output", default="c01000010_20")
    material_name: StringProperty(name="Material", default="c01000010_20M")
    match_names: StringProperty(name="Objects", default="*", description="Comma-separated object or material names")
    original_index: IntProperty(name="Original Mesh", default=-1, min=-1)
    texture_key: StringProperty(name="Texture Key", default="", description="Native texture name to replace in custom mode")
    texture_file: StringProperty(name="Texture File", subtype="FILE_PATH", default="")
    uv_flip_x: BoolProperty(name="Flip U", default=False)
    uv_flip_y: BoolProperty(name="Flip V", default=False)
    uv_scale_u: FloatProperty(name="U Scale", default=1.0, min=0.0001, soft_max=1.0)
    uv_scale_v: FloatProperty(name="V Scale", default=1.0, min=0.0001, soft_max=1.0)
    uv_offset_u: FloatProperty(name="U Offset", default=0.0, soft_min=0.0, soft_max=1.0)
    uv_offset_v: FloatProperty(name="V Offset", default=0.0, soft_min=0.0, soft_max=1.0)
    fallback_degenerate: BoolProperty(name="Fallback Triangle", default=True)
    rigid_joint: StringProperty(name="Default Joint", default="c_head_1_0")
    auto_palette: BoolProperty(name="Auto Palette", default=True)
    force_layout_material: BoolProperty(name="Force Layout/Material", default=False)
    layout_index: IntProperty(name="Layout", default=1, min=0)
    material_index: IntProperty(name="Material Slot", default=1, min=0)
    secondary_weight_scale: FloatProperty(
        name="Dynamic Weight",
        default=1.0,
        min=0.0,
        max=1.0,
        description="Scale applied to non-anchor weights; 1 keeps the source rig unchanged",
    )
    weight_anchor_joint: StringProperty(name="Anchor Joint", default="")

    def to_config(self, include_source_uv_transforms: bool = False) -> dict:
        item = {
            "output_name": self.output_name,
            "material_name": self.material_name,
            "match_names": split_csv(self.match_names) or ["*"],
            "fallback_degenerate": self.fallback_degenerate,
        }
        if self.uv_flip_x or self.uv_flip_y:
            item["uv_flip"] = [self.uv_flip_x, self.uv_flip_y]
        if self.uv_scale_u != 1.0 or self.uv_scale_v != 1.0:
            item["uv_scale"] = [self.uv_scale_u, self.uv_scale_v]
        if self.uv_offset_u or self.uv_offset_v:
            item["uv_offset"] = [self.uv_offset_u, self.uv_offset_v]
        if include_source_uv_transforms:
            source_uv_transforms = {}
            for obj in objects_for_record(self):
                uv = obj.level5_g4_port
                if uv.uv_scale_u != 1.0 or uv.uv_scale_v != 1.0 or uv.uv_offset_u or uv.uv_offset_v:
                    source_uv_transforms[obj.name] = {
                        "scale": [uv.uv_scale_u, uv.uv_scale_v],
                        "offset": [uv.uv_offset_u, uv.uv_offset_v],
                    }
            if source_uv_transforms:
                item["source_uv_transforms"] = source_uv_transforms
        if self.force_layout_material:
            item["force_layout_material"] = [self.layout_index, self.material_index]
        if self.rigid_joint:
            item["rigid_joint"] = self.rigid_joint
        if self.auto_palette:
            item["auto_palette"] = True
        if self.secondary_weight_scale != 1.0:
            item["secondary_weight_scale"] = self.secondary_weight_scale
            if self.weight_anchor_joint:
                item["weight_anchor_joint"] = self.weight_anchor_joint
        return item


class G4PortSceneSettings(PropertyGroup):
    model_rel: StringProperty(
        name="Model Path",
        default="chr/_face/01_IE1/c01000010/c01000010",
        description="Path inside common/chr without extension",
    )
    native_material_names: StringProperty(
        name="Native Materials",
        default="c01000010_20M, mouth_10M, eye_10M",
    )
    texture_replacements: StringProperty(
        name="Texture Replacements",
        default="c01000010_20=hairTexture.png, c01000010_10=faceTexture.png",
        description="Comma-separated texture=filename pairs",
    )
    original_model: StringProperty(
        name="Original Model",
        subtype="FILE_PATH",
        default="",
        description="Original G4MD/G4PKM used as a record/material template",
    )
    use_preset_file: BoolProperty(
        name="Use Preset File Directly",
        default=True,
        description="Use the selected JSON preset without rewriting it from the UI records",
    )
    preset_file: StringProperty(
        name="Preset",
        subtype="FILE_PATH",
        default="",
        description="Optional JSON preset. Empty uses the settings inferred from the selected original model",
    )
    texture_mode: EnumProperty(name="Textures", items=TEXTURE_MODE_ITEMS, default="custom")
    texture_platform: EnumProperty(name="Platform", items=TEXTURE_PLATFORM_ITEMS, default="auto")
    texture_source_dir: StringProperty(name="Texture Source Folder", subtype="DIR_PATH", default="")
    texture_entries: CollectionProperty(type=G4PortTextureReplacement)
    expression_pool: CollectionProperty(type=G4PortExpressionImage)
    generate_png_set_on_export: BoolProperty(
        name="Regenerate Atlas On Export",
        default=False,
        description="Regenerate only missing or outdated prepared atlases before exporting a custom G4TX",
    )
    use_source_uv_transforms: BoolProperty(
        name="Use Object UV Tiles",
        default=False,
        description="Apply per-object UV scale/offset values when exporting merged records",
    )
    auto_pack_source_uvs: BoolProperty(
        name="Auto Pack Object UVs",
        default=False,
        description="Assign automatic per-object atlas tiles when generating texture PNGs",
    )
    replace_special_textures: BoolProperty(
        name="Replace Special Maps",
        default=False,
        description="Allow custom replacements for line/oc/sp/spm maps instead of keeping bundled G4TX payloads",
    )
    preserve_native_roundtrip: BoolProperty(
        name="Preserve Untouched Native Import",
        default=True,
        description="Copy the original G4MD/G4MG/G4TX byte-for-byte when all assigned imported meshes are unchanged",
    )
    selected_only: BoolProperty(name="Selected Meshes Only", default=False)
    apply_modifiers: BoolProperty(
        name="Apply Modifiers",
        default=False,
        description="Apply Blender modifiers in the temporary DAE. Keep disabled unless weights were authored for the evaluated mesh",
    )
    align_forward_to_y: BoolProperty(
        name="Align Forward to Y Axis",
        default=False,
        description="Rotate the exported DAE so Blender forward points along the game Y axis",
    )
    analyze_only: BoolProperty(name="Analyze Only", default=False)
    generate_tangents: BoolProperty(name="Generate Tangents", default=True)
    strict_skinning: BoolProperty(
        name="Strict Skinning",
        default=False,
        description="Fail when source weights cannot be represented; disable for foreign rigs that should fall back to Default Joint",
    )
    global_uv_flip_x: BoolProperty(name="Global Flip U", default=False)
    global_uv_flip_y: BoolProperty(name="Global Flip V", default=True)
    records: CollectionProperty(type=G4PortRecord)
    active_record: IntProperty(default=0)
    joint_aliases: CollectionProperty(type=G4PortJointAlias)
    active_joint_alias: IntProperty(default=0)
    template_signature: StringProperty(default="")
    texture_names: StringProperty(name="Original G4TX Textures", default="")
    show_original: BoolProperty(name="Original Model", default=True)
    show_mapping: BoolProperty(name="Mesh Correspondence", default=True)
    show_rigging: BoolProperty(name="Rigging", default=False)
    show_record_settings: BoolProperty(name="Advanced Mesh Settings", default=False)
    show_textures: BoolProperty(name="Textures", default=False)
    show_export: BoolProperty(name="Export", default=True)

    def preset_path(self, prefs) -> Path:
        configured = bpy.path.abspath(self.preset_file or "")
        if configured:
            return Path(configured)
        return Path()

    def texture_map(self) -> dict:
        result = {}
        atlas_states = {row["name"]: row["state"] for row in atlas_status_rows(self)}
        face_texture = shared_face_texture_key([entry.texture_name for entry in self.texture_entries])
        for item in self.texture_entries:
            if not item.texture_name or not item.replacement_path:
                continue
            # The eye and mouth records sample authored windows of one native
            # facial atlas.  A generic image replacement would flatten those
            # windows into a different layout, so only the explicit 4x2 pool
            # is allowed to replace this entry.
            if item.texture_name == face_texture and not item.expression_atlas:
                continue
            if item.atlas_signature and atlas_states.get(item.texture_name) != "ready":
                continue
            if self.replace_special_textures or not is_special_texture(item.texture_name):
                result[item.texture_name] = bpy.path.basename(item.replacement_path)
        for item in split_csv(self.texture_replacements):
            if "=" in item:
                key, value = item.split("=", 1)
                key = key.strip()
                if key == face_texture:
                    continue
                entry = texture_entry(self, key)
                if entry is not None and entry.atlas_signature and atlas_states.get(key) == "warning":
                    continue
                if self.replace_special_textures or not is_special_texture(key):
                    result[key] = value.strip()
        return result

    def to_config(self) -> dict:
        active_texture_keys = set(self.texture_map())
        return {
            "model_rel": self.model_rel,
            "native_material_names": split_csv(self.native_material_names),
            "records": [
                record.to_config(
                    self.use_source_uv_transforms
                    and record.texture_key in active_texture_keys
                    and not is_face_atlas_record(record)
                )
                for record in self.records
            ],
            "texture_replacements": self.texture_map(),
            "texture_platform": self.texture_platform,
            "material_overrides": [],
            "joint_aliases": {
                alias.source_group: alias.target_joint
                for alias in self.joint_aliases
                if alias.source_group and alias.target_joint
            },
            "generate_tangents": self.generate_tangents,
            "strict_skinning": self.strict_skinning,
            "uv_flip": [self.global_uv_flip_x, self.global_uv_flip_y],
        }


def settings(context) -> G4PortSceneSettings:
    return context.scene.level5_g4_port


def apply_config_to_settings(target: G4PortSceneSettings, config: dict) -> None:
    target.model_rel = config.get("model_rel", target.model_rel)
    target.native_material_names = join_csv(config.get("native_material_names", []))
    replacements = config.get("texture_replacements", {})
    target.texture_replacements = join_csv(f"{key}={value}" for key, value in replacements.items())
    target.texture_platform = str(config.get("texture_platform", "auto"))
    for entry in target.texture_entries:
        entry.replacement_path = str(replacements.get(entry.texture_name, ""))
    target.generate_tangents = bool(config.get("generate_tangents", False))
    target.strict_skinning = bool(config.get("strict_skinning", False))
    uv_flip = config.get("uv_flip") or [False, True]
    target.global_uv_flip_x = bool(uv_flip[0]) if len(uv_flip) > 0 else False
    target.global_uv_flip_y = bool(uv_flip[1]) if len(uv_flip) > 1 else False
    target.records.clear()
    for source in config.get("records", []):
        record = target.records.add()
        record.output_name = source.get("output_name", "")
        record.material_name = source.get("material_name", "")
        record.texture_key = record.output_name
        record.match_names = join_csv(source.get("match_names", []))
        record.fallback_degenerate = bool(source.get("fallback_degenerate", False))
        record.rigid_joint = str(source.get("rigid_joint", ""))
        record.auto_palette = bool(source.get("auto_palette", True))
        uv = source.get("uv_flip") or [False, False]
        record.uv_flip_x = bool(uv[0]) if len(uv) > 0 else False
        record.uv_flip_y = bool(uv[1]) if len(uv) > 1 else False
        uv_scale = source.get("uv_scale") or [1.0, 1.0]
        record.uv_scale_u = float(uv_scale[0]) if len(uv_scale) > 0 else 1.0
        record.uv_scale_v = float(uv_scale[1]) if len(uv_scale) > 1 else 1.0
        uv_offset = source.get("uv_offset") or [0.0, 0.0]
        record.uv_offset_u = float(uv_offset[0]) if len(uv_offset) > 0 else 0.0
        record.uv_offset_v = float(uv_offset[1]) if len(uv_offset) > 1 else 0.0
        forced = source.get("force_layout_material")
        record.force_layout_material = isinstance(forced, list) and len(forced) == 2
        if record.force_layout_material:
            record.layout_index = int(forced[0])
            record.material_index = int(forced[1])
        record.secondary_weight_scale = float(source.get("secondary_weight_scale", 1.0))
        record.weight_anchor_joint = str(source.get("weight_anchor_joint", ""))
    target.joint_aliases.clear()
    for source_group, target_joint in (config.get("joint_aliases") or {}).items():
        alias = target.joint_aliases.add()
        alias.source_group = str(source_group)
        alias.target_joint = str(target_joint)
    target.active_record = min(target.active_record, max(0, len(target.records) - 1))


def run_model_probe(path: Path, prefs) -> dict:
    command = [
        bpy.path.abspath(getattr(prefs, "python_path", "") or default_python()),
        str(resolve_probe_script(prefs)),
        "--json",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            "G4 model probe failed\n"
            f"Command: {shlex.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    try:
        results = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model probe did not return JSON:\n{completed.stdout}") from exc
    if not results:
        raise RuntimeError("Model probe returned no results")
    return results[0]


def model_rel_from_path(path: Path) -> str:
    data_root = infer_data_root(path)
    if data_root is not None:
        try:
            return relative_model_from_data(path, data_root)
        except ValueError:
            pass
    parts = list(path.with_suffix("").parts)
    for root_name in ("common", "dx11", "nx"):
        if root_name in parts:
            index = parts.index(root_name)
            return "/".join(parts[index + 1 :])
    return path.with_suffix("").name


def original_template_signature(md: dict) -> str:
    material_names = md.get("material_names", [])
    mesh_names = md.get("mesh_names", [])
    rows = []
    for source in md.get("records", []):
        index = int(source.get("index", len(rows)))
        material_index = int(source.get("material_index", source.get("material_or_lod", 0)))
        rows.append(
            [
                mesh_names[index] if index < len(mesh_names) else f"mesh_{index}",
                material_names[material_index] if material_index < len(material_names) else "",
                int(source.get("layout_index", 0)),
                material_index,
            ]
        )
    return json.dumps(rows, separators=(",", ":"))


def record_default_joint(md: dict, source: dict) -> str:
    flags = int(source.get("flags0", 0))
    palette_length = flags & 0xFF if flags & 0x100 else 0
    palette_offset = int(source.get("palette_or_list", 0))
    palette = md.get("joint_palette_indices") or []
    if palette_length > 0 and 0 <= palette_offset < len(palette):
        return f"joint_{int(palette[palette_offset])}"
    return "c_head_1_0"


def original_g4tx_path(data_root: Path, model_rel: str) -> Path | None:
    for path in (
        dx11_g4tx_for_model(data_root, model_rel),
        nx_g4tx_for_model(data_root, model_rel),
        common_g4tx_for_model(data_root, model_rel),
    ):
        if path.is_file():
            return path
    return None


def apply_original_model_to_settings(target: G4PortSceneSettings, path: Path, summary: dict) -> None:
    md = summary.get("g4md") or {}
    data_root = infer_data_root(path)
    if data_root is None:
        raise RuntimeError("The original model must be inside a data/common or data/dx11 filesystem tree.")
    model_rel = model_rel_from_path(path)
    g4tx_path = original_g4tx_path(data_root, model_rel)
    texture_names = parse_g4tx_names(g4tx_path) if g4tx_path is not None else []
    signature = original_template_signature(md)
    target.original_model = str(path)
    target.model_rel = model_rel
    target.native_material_names = join_csv(md.get("material_names", []))
    target.texture_replacements = ""
    target.texture_names = join_csv(texture_names)
    target.texture_entries.clear()
    for texture_name in texture_names:
        entry = target.texture_entries.add()
        entry.texture_name = texture_name
    material_names = md.get("material_names", [])
    mesh_names = md.get("mesh_names", [])
    if target.template_signature == signature and len(target.records) == len(md.get("records", [])):
        for record in target.records:
            if not record.texture_key:
                record.texture_key = texture_key_for_record(record.output_name, texture_names)
        assign_shared_face_texture_key(target.records, texture_names)
        return
    target.records.clear()
    for source in md.get("records", []):
        record = target.records.add()
        index = int(source.get("index", len(target.records) - 1))
        material_index = int(source.get("material_index", source.get("material_or_lod", 0)))
        layout_index = int(source.get("layout_index", 0))
        record.original_index = index
        record.output_name = mesh_names[index] if index < len(mesh_names) else f"mesh_{index}"
        record.material_name = material_names[material_index] if material_index < len(material_names) else ""
        record.match_names = record.output_name
        record.texture_key = texture_key_for_record(record.output_name, texture_names)
        record.fallback_degenerate = True
        record.force_layout_material = True
        record.layout_index = layout_index
        record.material_index = material_index
        record.rigid_joint = record_default_joint(md, source)
        record.auto_palette = True
    assign_shared_face_texture_key(target.records, texture_names)
    target.active_record = 0
    target.template_signature = signature
    target.use_preset_file = False


def assign_selected_to_record(context, record: G4PortRecord) -> int:
    selected = mesh_objects(True)
    names = [obj.name for obj in selected]
    if not selected:
        return 0
    for obj in selected:
        obj.level5_g4_port.target_record = record.output_name
    if not record.texture_file:
        for obj in selected:
            image_path = material_image_path(obj.active_material)
            if image_path:
                record.texture_file = image_path
                break
    existing = [item for item in split_csv(record.match_names) if item != "*"]
    merged = list(dict.fromkeys(existing + names))
    record.match_names = join_csv(merged)
    settings(context).use_preset_file = False
    return len(names)


def guess_object_assignments(props: G4PortSceneSettings) -> int:
    records = list(props.records)
    if not records:
        return 0
    assigned = 0
    for obj in mesh_objects(False):
        object_key = blender_base_name(obj.name).lower()
        material_key = blender_base_name(obj.active_material.name).lower() if obj.active_material else ""
        best = None
        for record in records:
            candidates = [
                record.output_name.lower(),
                record.material_name.removesuffix("M").lower(),
                record.material_name.lower(),
            ]
            if object_key in candidates or material_key in candidates:
                best = record
                break
        if best is None:
            continue
        obj.level5_g4_port.target_record = best.output_name
        existing = [item for item in split_csv(best.match_names) if item != "*"]
        if obj.name not in existing:
            best.match_names = join_csv(existing + [obj.name])
        assigned += 1
    if assigned:
        props.use_preset_file = False
    return assigned


def sync_assignment_table(context) -> None:
    props = settings(context)
    assignments: dict[str, list[str]] = {record.output_name: [] for record in props.records}
    for obj in mesh_objects(False):
        target = getattr(obj.level5_g4_port, "target_record", "__none__")
        if target in assignments:
            assignments[target].append(obj.name)
    for record in props.records:
        names = assignments.get(record.output_name) or split_csv(record.match_names)
        record.match_names = join_csv(list(dict.fromkeys(name for name in names if name and name != "*")))
    props.use_preset_file = False


def objects_for_record(record: G4PortRecord) -> list[bpy.types.Object]:
    explicit = set(split_csv(record.match_names))
    assigned = [
        obj for obj in mesh_objects(False)
        if getattr(obj.level5_g4_port, "target_record", "__none__") == record.output_name
    ]
    if assigned:
        return assigned
    return [obj for obj in mesh_objects(False) if obj.name in explicit]


def detect_joint_aliases(props: G4PortSceneSettings, selected_only: bool = False) -> int:
    existing = {alias.source_group: alias for alias in props.joint_aliases}
    added = 0
    for group_name in vertex_group_names(selected_only):
        if group_name in existing:
            continue
        alias = props.joint_aliases.add()
        alias.source_group = group_name
        alias.target_joint = guess_joint_alias(group_name)
        existing[group_name] = alias
        added += 1
    props.active_joint_alias = min(props.active_joint_alias, max(0, len(props.joint_aliases) - 1))
    return added


def auto_map_joint_aliases(props: G4PortSceneSettings) -> int:
    changed = 0
    for alias in props.joint_aliases:
        if alias.target_joint:
            continue
        guess = guess_joint_alias(alias.source_group)
        if guess:
            alias.target_joint = guess
            changed += 1
    return changed


def generated_config_path(cache: Path) -> Path:
    return cache / "generated_port_config.json"


def prepare_custom_textures(props: G4PortSceneSettings, dae_path: Path) -> Path:
    custom_dir = dae_path.parent / "customTextures"
    custom_dir.mkdir(parents=True, exist_ok=True)
    for record in props.records:
        if not record.texture_file:
            continue
        source = Path(bpy.path.abspath(record.texture_file))
        if source.is_file():
            shutil.copy2(source, custom_dir / source.name)
    texture_source_dir = resolve_file(props.texture_source_dir)
    for entry in props.texture_entries:
        if not entry.replacement_path:
            continue
        source = Path(bpy.path.abspath(entry.replacement_path))
        if source.is_file():
            shutil.copy2(source, custom_dir / source.name)
    for rel_path in props.texture_map().values():
        source = texture_source_dir / rel_path
        if source.is_file():
            shutil.copy2(source, custom_dir / source.name)
    return custom_dir


def export_python(prefs, needs_pillow: bool) -> str:
    configured = bpy.path.abspath(getattr(prefs, "python_path", "") or default_python())
    if not needs_pillow:
        return configured
    candidates = [configured, "/usr/bin/python3", "/opt/homebrew/bin/python3", "python3"]
    for candidate in dict.fromkeys(candidates):
        try:
            completed = subprocess.run(
                [candidate, "-c", "import PIL"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        if completed.returncode == 0:
            return candidate
    raise RuntimeError(
        "PNG texture replacement needs a Python installation with Pillow (PIL). "
        "DDS and NXTCH replacements do not require Pillow."
    )


def auto_assign_record_texture_files(props: G4PortSceneSettings) -> int:
    assigned = 0
    for record in props.records:
        if record.texture_file:
            continue
        for obj in objects_for_record(record):
            image_path = material_image_path(obj.active_material)
            if image_path:
                record.texture_file = image_path
                assigned += 1
                break
    if assigned:
        props.use_preset_file = False
    return assigned


def is_special_texture(name: str) -> bool:
    return name.endswith(("line", "oc", "sp", "spm"))


def special_texture_suffix(name: str) -> str:
    for suffix in ("line", "spm", "sp", "oc"):
        if name.endswith(suffix):
            return suffix
    return ""


def base_texture_name(name: str) -> str:
    suffix = special_texture_suffix(name)
    return name[: -len(suffix)] if suffix else name


def special_texture_default_color(name: str) -> tuple[float, float, float, float]:
    return {
        "line": (0.0, 0.0, 1.0, 1.0),
        "oc": (1.0, 1.0, 0.0, 1.0),
        "sp": (0.0, 0.0, 0.0, 0.0),
        "spm": (0.0, 0.0, 0.0, 1.0),
    }.get(special_texture_suffix(name), (0.0, 0.0, 0.0, 0.0))


def image_pixels(width: int, height: int, color: tuple[float, float, float, float]) -> array:
    return array("f", color) * (width * height)


def set_pixel(pixels: array, width: int, height: int, x: int, y: int, color: tuple[float, float, float, float]) -> None:
    if 0 <= x < width and 0 <= y < height:
        offset = (y * width + x) * 4
        pixels[offset] = color[0]
        pixels[offset + 1] = color[1]
        pixels[offset + 2] = color[2]
        pixels[offset + 3] = color[3]


def draw_line(
    pixels: array,
    width: int,
    height: int,
    start: tuple[int, int],
    end: tuple[int, int],
    color: tuple[float, float, float, float],
) -> None:
    x0, y0 = start
    x1, y1 = end
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        set_pixel(pixels, width, height, x0, y0, color)
        if x0 == x1 and y0 == y1:
            break
        step = 2 * err
        if step >= dy:
            err += dy
            x0 += sx
        if step <= dx:
            err += dx
            y0 += sy


def uv_to_pixel(
    uv,
    record: G4PortRecord,
    props: G4PortSceneSettings,
    width: int,
    height: int,
    obj_uv: G4PortObjectSettings | None = None,
) -> tuple[int, int]:
    u, v = uv
    if obj_uv is not None:
        u -= math.floor(u)
        v -= math.floor(v)
        u = u * obj_uv.uv_scale_u + obj_uv.uv_offset_u
        v = v * obj_uv.uv_scale_v + obj_uv.uv_offset_v
    flip_x = record.uv_flip_x or props.global_uv_flip_x
    flip_y = record.uv_flip_y or props.global_uv_flip_y
    if flip_x:
        u = 1.0 - u
    if flip_y:
        v = 1.0 - v
    u = u * record.uv_scale_u + record.uv_offset_u
    v = v * record.uv_scale_v + record.uv_offset_v
    return (
        int(round(max(0.0, min(1.0, u)) * (width - 1))),
        int(round(max(0.0, min(1.0, v)) * (height - 1))),
    )


def draw_object_uvs(
    pixels: array,
    width: int,
    height: int,
    obj: bpy.types.Object,
    record: G4PortRecord,
    props: G4PortSceneSettings,
) -> None:
    mesh = obj.data
    if not mesh.uv_layers.active:
        return
    uv_data = mesh.uv_layers.active.data
    if not uv_data:
        return
    color = (0.02, 0.02, 0.02, 1.0)
    for polygon in mesh.polygons:
        loop_indices = list(polygon.loop_indices)
        if len(loop_indices) < 2:
            continue
        valid_indices = [index for index in loop_indices if index < len(uv_data)]
        if len(valid_indices) != len(loop_indices):
            continue
        points = [
            uv_to_pixel(uv_data[index].uv, record, props, width, height, obj.level5_g4_port)
            for index in valid_indices
        ]
        for index, point in enumerate(points):
            draw_line(pixels, width, height, point, points[(index + 1) % len(points)], color)


def save_png(path: Path, width: int, height: int, pixels: array) -> None:
    image = bpy.data.images.new(path.stem, width=width, height=height, alpha=True)
    try:
        image.pixels.foreach_set(pixels)
        image.filepath_raw = str(path)
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)


def load_image_pixels(path: str) -> tuple[int, int, array] | None:
    if not path:
        return None
    source = Path(bpy.path.abspath(path))
    if not source.is_file():
        return None
    image = bpy.data.images.load(str(source), check_existing=True)
    width, height = image.size
    if width <= 0 or height <= 0 or len(image.pixels) < int(width) * int(height) * 4:
        return None
    pixels = array("f", [0.0]) * (int(width) * int(height) * 4)
    image.pixels.foreach_get(pixels)
    return int(width), int(height), pixels


def blit_image_fit(
    target: array,
    target_width: int,
    target_height: int,
    source: tuple[int, int, array],
    cell_x: int,
    cell_y: int,
    cell_width: int,
    cell_height: int,
) -> tuple[int, int, int, int]:
    source_width, source_height, source_pixels = source
    if source_width <= 0 or source_height <= 0 or cell_width <= 0 or cell_height <= 0:
        return cell_x, cell_y, cell_width, cell_height
    scale = min(cell_width / source_width, cell_height / source_height)
    draw_width = max(1, int(round(source_width * scale)))
    draw_height = max(1, int(round(source_height * scale)))
    draw_x = cell_x + max(0, (cell_width - draw_width) // 2)
    draw_y = cell_y + max(0, (cell_height - draw_height) // 2)
    for y in range(draw_height):
        src_y = min(source_height - 1, int(y / max(scale, 0.0001)))
        dst_y = draw_y + y
        if not 0 <= dst_y < target_height:
            continue
        for x in range(draw_width):
            src_x = min(source_width - 1, int(x / max(scale, 0.0001)))
            dst_x = draw_x + x
            if not 0 <= dst_x < target_width:
                continue
            src = (src_y * source_width + src_x) * 4
            dst = (dst_y * target_width + dst_x) * 4
            target[dst : dst + 4] = source_pixels[src : src + 4]
    return draw_x, draw_y, draw_width, draw_height


def atlas_grid(count: int) -> tuple[int, int]:
    columns = 1
    while columns * columns < count:
        columns += 1
    rows = (count + columns - 1) // columns
    return columns, rows


def capped_atlas_dimensions(
    entry_width: int, entry_height: int, source_width: int, source_height: int, columns: int, rows: int
) -> tuple[int, int, int, int]:
    cell_width = max(1, max(entry_width, source_width * columns) // columns)
    cell_height = max(1, max(entry_height, source_height * rows) // rows)
    limit = min(MAX_GENERATED_TEXTURE_SIZE, 0xFFFF)
    if cell_width * columns > limit:
        cell_width = max(1, limit // columns)
    if cell_height * rows > limit:
        cell_height = max(1, limit // rows)
    width = min(limit, max(1, cell_width * columns))
    height = min(limit, max(1, cell_height * rows))
    return width, height, cell_width, cell_height


def object_uv_bounds(obj: bpy.types.Object) -> tuple[float, float, float, float] | None:
    mesh = obj.data
    if not mesh.uv_layers.active or not mesh.uv_layers.active.data:
        return None
    values = [loop.uv for loop in mesh.uv_layers.active.data]
    min_u = min(uv.x for uv in values)
    max_u = max(uv.x for uv in values)
    min_v = min(uv.y for uv in values)
    max_v = max(uv.y for uv in values)
    return min_u, max_u, min_v, max_v


def uv_requires_projection(obj: bpy.types.Object) -> bool:
    bounds = object_uv_bounds(obj)
    return bool(bounds and (bounds[0] < 0.0 or bounds[1] > 1.0 or bounds[2] < 0.0 or bounds[3] > 1.0))


def object_image_extension(obj: bpy.types.Object) -> str:
    for polygon in obj.data.polygons:
        if not 0 <= polygon.material_index < len(obj.data.materials):
            continue
        material = obj.data.materials[polygon.material_index]
        if material is None or material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None and node.image.filepath:
                return node.extension
    return "REPEAT"


def projected_source_image(
    source: tuple[int, int, array], bounds: tuple[float, float, float, float], extension: str
) -> tuple[int, int, array]:
    """Bake the source image over a mesh's UV domain so repeated UVs survive atlas fitting."""
    width, height, pixels = source
    min_u, max_u, min_v, max_v = bounds
    out = array("f", [0.0]) * (width * height * 4)
    for y in range(height):
        v = min_v + (max_v - min_v) * ((y + 0.5) / height)
        for x in range(width):
            u = min_u + (max_u - min_u) * ((x + 0.5) / width)
            if extension == "CLIP" and not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                continue
            if extension == "REPEAT":
                u -= math.floor(u)
                v -= math.floor(v)
            else:
                u = max(0.0, min(1.0, u))
                v = max(0.0, min(1.0, v))
            src_x = min(width - 1, int(u * (width - 1)))
            src_y = min(height - 1, int(v * (height - 1)))
            src = (src_y * width + src_x) * 4
            dst = (y * width + x) * 4
            out[dst : dst + 4] = pixels[src : src + 4]
    return width, height, out


def set_object_uv_fit(obj: bpy.types.Object, origin_u: float, origin_v: float, width: float, height: float) -> None:
    bounds = object_uv_bounds(obj)
    uv = obj.level5_g4_port
    if bounds is None:
        uv.uv_scale_u = width
        uv.uv_scale_v = height
        uv.uv_offset_u = origin_u
        uv.uv_offset_v = origin_v
        return
    min_u, max_u, min_v, max_v = bounds
    bounds_u = max(max_u - min_u, 0.0001)
    bounds_v = max(max_v - min_v, 0.0001)
    scale = min(width / bounds_u, height / bounds_v)
    used_u = bounds_u * scale
    used_v = bounds_v * scale
    uv.uv_scale_u = scale
    uv.uv_scale_v = scale
    uv.uv_offset_u = origin_u + (width - used_u) * 0.5 - min_u * scale
    uv.uv_offset_v = origin_v + (height - used_v) * 0.5 - min_v * scale


def set_object_uv_tile(obj: bpy.types.Object, origin_u: float, origin_v: float, width: float, height: float) -> None:
    uv = obj.level5_g4_port
    uv.uv_scale_u = width
    uv.uv_scale_v = height
    uv.uv_offset_u = origin_u
    uv.uv_offset_v = origin_v


def assign_texture_uv_tiles(records_by_texture: dict[str, list[G4PortRecord]]) -> None:
    for records in records_by_texture.values():
        items = [(record, obj) for record in records for obj in objects_for_record(record)]
        if not items:
            continue
        columns, rows = atlas_grid(len(items))
        scale_u = 1.0 / columns
        scale_v = 1.0 / rows
        for record in records:
            record.uv_scale_u = 1.0
            record.uv_scale_v = 1.0
            record.uv_offset_u = 0.0
            record.uv_offset_v = 0.0
        for index, (_, obj) in enumerate(items):
            column = index % columns
            row = index // columns
            set_object_uv_fit(obj, column * scale_u, row * scale_v, scale_u, scale_v)


def reset_uv_tiles(props: G4PortSceneSettings) -> None:
    for record in props.records:
        record.uv_scale_u = 1.0
        record.uv_scale_v = 1.0
        record.uv_offset_u = 0.0
        record.uv_offset_v = 0.0
    for obj in mesh_objects(False):
        uv = obj.level5_g4_port
        uv.uv_scale_u = 1.0
        uv.uv_scale_v = 1.0
        uv.uv_offset_u = 0.0
        uv.uv_offset_v = 0.0


def records_grouped_by_texture(props: G4PortSceneSettings) -> dict[str, list[G4PortRecord]]:
    records_by_texture: dict[str, list[G4PortRecord]] = {}
    for record in props.records:
        if record.texture_key:
            records_by_texture.setdefault(record.texture_key, []).append(record)
    return records_by_texture


def source_path_for_object(obj: bpy.types.Object) -> str:
    override = bpy.path.abspath(obj.level5_g4_port.source_texture)
    return override if override and Path(override).is_file() else first_used_material_image(obj)


def texture_entry(props: G4PortSceneSettings, texture_name: str) -> G4PortTextureReplacement | None:
    return next((entry for entry in props.texture_entries if entry.texture_name == texture_name), None)


def atlas_signature(texture_name: str, records: list[G4PortRecord]) -> str:
    items = []
    for record, obj in texture_items_for_records(records):
        bounds = object_uv_bounds(obj)
        source = source_path_for_object(obj)
        source_path = Path(source) if source else Path()
        items.append({
            "record": record.output_name,
            "object": obj.name,
            "source": str(source_path),
            "source_mtime": source_path.stat().st_mtime_ns if source_path.is_file() else 0,
            "uv_bounds": [round(value, 6) for value in bounds] if bounds else [],
        })
    payload = json.dumps({"texture": texture_name, "items": items}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expression_pool_paths(props: G4PortSceneSettings) -> list[Path]:
    return [Path(bpy.path.abspath(item.image_path)) for item in props.expression_pool if item.image_path]


def expression_pool_signature(props: G4PortSceneSettings, texture_name: str) -> str:
    items = []
    for path in expression_pool_paths(props):
        items.append({
            "path": str(path),
            "mtime": path.stat().st_mtime_ns if path.is_file() else 0,
        })
    payload = json.dumps({"texture": texture_name, "layout": [FACE_ATLAS_COLUMNS, FACE_ATLAS_ROWS], "items": items}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def face_atlas_entry_ready(props: G4PortSceneSettings, entry: G4PortTextureReplacement) -> bool:
    if not entry.expression_atlas or not entry.replacement_path:
        return False
    if not Path(bpy.path.abspath(entry.replacement_path)).is_file():
        return False
    return entry.expression_atlas_mode == "existing" or entry.atlas_signature == expression_pool_signature(props, entry.texture_name)


def is_shared_face_atlas(texture_name: str, records) -> bool:
    return any(is_face_atlas_record(record) for record in records) and texture_name == shared_face_texture_key(
        [texture_name]
    )


def atlas_status_rows(props: G4PortSceneSettings) -> list[dict]:
    rows = []
    grouped = records_grouped_by_texture(props)
    names = list(grouped)
    names.extend(entry.texture_name for entry in props.texture_entries if entry.texture_name not in grouped)
    for texture_name in names:
        if is_special_texture(texture_name):
            continue
        records = grouped.get(texture_name, [])
        entry = texture_entry(props, texture_name)
        shared_face = is_shared_face_atlas(texture_name, records)
        signature = atlas_signature(texture_name, records)
        objects = [obj for _, obj in texture_items_for_records(records)]
        missing = [obj.name for obj in objects if not source_path_for_object(obj)]
        unreadable = [
            obj.name for obj in objects
            if source_path_for_object(obj) and load_image_pixels(source_path_for_object(obj)) is None
        ] if not shared_face else []
        repeated = [
            obj.name for obj in objects
            if (bounds := object_uv_bounds(obj)) and (bounds[0] < 0.0 or bounds[1] > 1.0 or bounds[2] < 0.0 or bounds[3] > 1.0)
        ]
        prepared = entry and entry.replacement_path and Path(bpy.path.abspath(entry.replacement_path)).is_file()
        fresh = prepared and entry.atlas_signature == signature
        if entry is not None and entry.expression_atlas:
            pool_paths = expression_pool_paths(props)
            signature = expression_pool_signature(props, texture_name)
            prepared = entry.replacement_path and Path(bpy.path.abspath(entry.replacement_path)).is_file()
            if entry.expression_atlas_mode == "existing":
                state, message = (
                    ("ready", entry.atlas_summary or "Existing 4x2 expression atlas")
                    if prepared else ("warning", "Existing 4x2 expression atlas is missing")
                )
            elif len(pool_paths) != FACE_ATLAS_SLOTS or not all(path.is_file() for path in pool_paths):
                state, message = "warning", "Expression pool needs 8 valid images (4x2)"
            elif prepared and entry.atlas_signature == signature:
                state, message = "ready", entry.atlas_summary or "Prepared 4x2 expression atlas"
            else:
                state, message = "stale", "Expression atlas needs rebuilding"
        elif shared_face:
            state, message = "native", "Shared eye/mouth 4x2 atlas preserved"
        elif missing:
            state, message = "warning", f"Missing source: {', '.join(missing)}"
        elif unreadable:
            state, message = "warning", f"Unreadable source: {', '.join(unreadable)}; native G4TX entry will be preserved"
        elif not objects:
            state, message = "native", "No assigned meshes; native G4TX entry will be preserved"
        elif fresh:
            state, message = "ready", entry.atlas_summary or "Prepared atlas"
        elif prepared and not entry.atlas_signature:
            state, message = "manual", "Manual replacement"
        elif prepared:
            state, message = "stale", "Atlas needs regeneration"
        else:
            state, message = "native", "Native G4TX entry will be preserved"
        rows.append({"name": texture_name, "records": records, "signature": signature, "state": state, "message": message, "repeated": repeated, "shared_face": shared_face})
    return rows


def object_texture_path(record: G4PortRecord, obj: bpy.types.Object) -> str:
    return source_path_for_object(obj)


def sibling_texture_path(path: str, suffix: str) -> str:
    if not path:
        return ""
    source = Path(bpy.path.abspath(path))
    names = [
        source.with_name(f"{source.stem}{suffix}{source.suffix}"),
        source.with_name(f"{source.stem}_{suffix}{source.suffix}"),
    ]
    for candidate in names:
        if candidate.is_file():
            return str(candidate)
    return ""


def object_special_texture_path(
    record: G4PortRecord,
    obj: bpy.types.Object,
    texture_name: str,
    explicit_map: dict[str, str],
    texture_source_dir: Path,
) -> str:
    explicit = explicit_map.get(texture_name)
    if explicit:
        source = texture_source_dir / explicit
        if source.is_file():
            return str(source)
        absolute = Path(bpy.path.abspath(explicit))
        if absolute.is_file():
            return str(absolute)
    suffix = special_texture_suffix(texture_name)
    for base in (source_path_for_object(obj), bpy.path.abspath(record.texture_file or "")):
        if not base:
            continue
        source = Path(bpy.path.abspath(base))
        if source.parent == texture_source_dir and source.stem.startswith(record.texture_key):
            continue
        sibling = sibling_texture_path(base, suffix)
        if sibling:
            return sibling
    return ""


def texture_items_for_records(records: list[G4PortRecord]) -> list[tuple[G4PortRecord, bpy.types.Object]]:
    return [(record, obj) for record in records for obj in objects_for_record(record)]


def build_texture_spritesheet(
    path: Path,
    entry: dict,
    records: list[G4PortRecord],
    props: G4PortSceneSettings,
    source_path_for_item=None,
    empty_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0),
    draw_missing_guides: bool = True,
    assign_record_texture_file: bool = False,
    log_path: Path | None = None,
    update_uv_transforms: bool = True,
) -> bool:
    items = texture_items_for_records(records)
    if not items:
        port_log(log_path, f"{entry['name']}: skipped spritesheet; no assigned objects")
        return False
    if source_path_for_item is None:
        source_path_for_item = object_texture_path
    port_log(log_path, f"{entry['name']}: loading {len(items)} source texture item(s)")
    sources = []
    source_cache = {}
    for index, (record, obj) in enumerate(items, 1):
        source_path = source_path_for_item(record, obj)
        port_log(
            log_path,
            f"{entry['name']}: [{index}/{len(items)}] {obj.name} record={record.output_name} source={source_path or '<none>'}",
        )
        started = time.perf_counter()
        cache_key = str(Path(bpy.path.abspath(source_path)).resolve()) if source_path else ""
        if cache_key and cache_key in source_cache:
            source = source_cache[cache_key]
            if source is not None and uv_requires_projection(obj):
                source = projected_source_image(source, object_uv_bounds(obj), object_image_extension(obj))
                cache_key = f"{cache_key}|projection:{obj.name}"
            elapsed = time.perf_counter() - started
            port_log(log_path, f"{entry['name']}: [{index}/{len(items)}] reused cached image ({elapsed:.2f}s)")
            sources.append((record, obj, source, cache_key))
            continue
        source = load_image_pixels(source_path)
        if cache_key and source is not None:
            source_cache[cache_key] = source
        if source is not None and uv_requires_projection(obj):
            source = projected_source_image(source, object_uv_bounds(obj), object_image_extension(obj))
            cache_key = f"{cache_key}|projection:{obj.name}"
        elapsed = time.perf_counter() - started
        if source is None:
            port_log(log_path, f"{entry['name']}: [{index}/{len(items)}] no source image ({elapsed:.2f}s)")
        else:
            port_log(log_path, f"{entry['name']}: [{index}/{len(items)}] loaded {source[0]}x{source[1]} ({elapsed:.2f}s)")
        sources.append((record, obj, source, cache_key))

    unreadable = [obj.name for _, obj, source, _ in sources if source is None]
    if unreadable:
        port_log(
            log_path,
            f"{entry['name']}: skipped spritesheet; unreadable source image for {', '.join(unreadable)}",
        )
        return False

    groups = []
    grouped_sources = {}
    for record, obj, source, cache_key in sources:
        if source is None or not cache_key:
            groups.append({"records": [record], "objects": [obj], "source": source, "key": obj.name})
            continue
        group = grouped_sources.get(cache_key)
        if group is None:
            group = {"records": [], "objects": [], "source": source, "key": cache_key}
            grouped_sources[cache_key] = group
            groups.append(group)
        group["records"].append(record)
        group["objects"].append(obj)

    columns, rows = atlas_grid(len(groups))
    max_source_width = max((group["source"][0] for group in groups if group["source"] is not None), default=entry["width"])
    max_source_height = max((group["source"][1] for group in groups if group["source"] is not None), default=entry["height"])
    width, height, cell_width, cell_height = capped_atlas_dimensions(
        entry["width"], entry["height"], max_source_width, max_source_height, columns, rows
    )
    port_log(
        log_path,
        f"{entry['name']}: atlas {width}x{height}, grid={columns}x{rows}, cell={cell_width}x{cell_height}, groups={len(groups)}",
    )
    pixels = image_pixels(width, height, empty_color)
    for record in records:
        record.uv_scale_u = 1.0
        record.uv_scale_v = 1.0
        record.uv_offset_u = 0.0
        record.uv_offset_v = 0.0
    for index, group in enumerate(groups):
        column = index % columns
        row = index // columns
        cell_x = column * cell_width
        cell_y = row * cell_height
        source = group["source"]
        objects = group["objects"]
        records_in_group = group["records"]
        if source is None:
            origin_u = cell_x / width
            origin_v = cell_y / height
            rect_u = cell_width / width
            rect_v = cell_height / height
            for record, obj in zip(records_in_group, objects):
                if update_uv_transforms:
                    set_object_uv_fit(obj, origin_u, origin_v, rect_u, rect_v)
                if draw_missing_guides:
                    port_log(log_path, f"{entry['name']}: drawing UV guide for {obj.name} ({len(obj.data.polygons)} polygon(s))")
                    started = time.perf_counter()
                    draw_object_uvs(pixels, width, height, obj, record, props)
                    port_log(log_path, f"{entry['name']}: UV guide for {obj.name} done ({time.perf_counter() - started:.2f}s)")
            continue
        port_log(log_path, f"{entry['name']}: blitting group {index + 1}/{len(groups)} ({len(objects)} object(s)) into cell ({column}, {row})")
        started = time.perf_counter()
        draw_x, draw_y, draw_width, draw_height = blit_image_fit(
            pixels, width, height, source, cell_x, cell_y, cell_width, cell_height
        )
        port_log(
            log_path,
            f"{entry['name']}: blit group done as {draw_width}x{draw_height} ({time.perf_counter() - started:.2f}s)",
        )
        if update_uv_transforms:
            for obj in objects:
                set_object_uv_fit(obj, draw_x / width, draw_y / height, draw_width / width, draw_height / height)
    port_log(log_path, f"{entry['name']}: saving {path}")
    started = time.perf_counter()
    save_png(path, width, height, pixels)
    port_log(log_path, f"{entry['name']}: saved ({time.perf_counter() - started:.2f}s)")
    props.use_source_uv_transforms = True
    return True


def discard_generated_atlas(entry: G4PortTextureReplacement | None, generated_path: Path) -> None:
    """Forget only a failed atlas created by this exporter; manual paths stay intact."""
    if entry is None:
        return
    source = Path(bpy.path.abspath(entry.replacement_path)) if entry.replacement_path else None
    is_generated_path = source is not None and source == generated_path
    if not is_generated_path and not entry.atlas_signature:
        return
    if is_generated_path and source.is_file():
        source.unlink()
    entry.replacement_path = ""
    entry.atlas_signature = ""
    entry.atlas_summary = ""


def generate_texture_png_set(context, output_dir: Path, log_path: Path | None = None) -> int:
    props = settings(context)
    port_log(log_path, "Generate PNG set started")
    port_log(log_path, f"Output folder: {output_dir}")
    port_log(log_path, f"Max generated texture size: {MAX_GENERATED_TEXTURE_SIZE}")
    original_model = resolve_file(props.original_model)
    raw_root = infer_data_root(original_model) if original_model.is_file() else None
    if raw_root is None:
        raise RuntimeError("Choose an original G4MD/G4PKM before generating texture PNGs.")
    port_log(log_path, f"Original model: {original_model}")
    port_log(log_path, f"Raw data root: {raw_root}")
    g4tx_path = original_g4tx_path(raw_root, props.model_rel)
    if g4tx_path is None:
        raise RuntimeError(f"Original G4TX not found for {props.model_rel}")
    entries = parse_g4tx_entries(g4tx_path)
    port_log(log_path, f"Original G4TX entries: {len(entries)}")
    output_dir.mkdir(parents=True, exist_ok=True)
    replacements = [
        f"{entry.texture_name}={Path(entry.replacement_path).name}"
        for entry in props.texture_entries
        if face_atlas_entry_ready(props, entry)
    ]
    records_by_texture = records_grouped_by_texture(props)
    explicit_map = props.texture_map()
    texture_source_dir = resolve_file(props.texture_source_dir)
    port_log(log_path, f"Texture source folder: {texture_source_dir if texture_source_dir else '<none>'}")
    port_log(log_path, f"Texture groups: {', '.join(f'{key}={len(value)}' for key, value in records_by_texture.items()) or '<none>'}")
    port_log(log_path, f"Auto pack source UVs: {props.auto_pack_source_uvs}; use source UV transforms: {props.use_source_uv_transforms}")
    if props.auto_pack_source_uvs:
        port_log(log_path, "Assigning automatic object UV tiles")
        assign_texture_uv_tiles(records_by_texture)
        props.use_source_uv_transforms = True
    elif not props.use_source_uv_transforms:
        port_log(log_path, "Resetting object UV tiles")
        reset_uv_tiles(props)
    for index, entry in enumerate(entries, 1):
        name = entry["name"]
        path = output_dir / f"{name}.png"
        port_log(log_path, f"[{index}/{len(entries)}] Processing {name} ({entry['width']}x{entry['height']})")
        if is_special_texture(name):
            if not props.replace_special_textures:
                port_log(log_path, f"{name}: preserving native special map")
                continue
            default_color = special_texture_default_color(name)
            records = records_by_texture.get(base_texture_name(name), [])
            port_log(log_path, f"{name}: special map, base records={len(records)}, default={default_color}")
            special_map = dict(explicit_map)
            if Path(special_map.get(name, "")).name == path.name:
                special_map.pop(name, None)
            if build_texture_spritesheet(
                path,
                entry,
                records,
                props,
                source_path_for_item=lambda record, obj, texture=name, mapping=special_map: object_special_texture_path(
                    record, obj, texture, mapping, texture_source_dir
                ),
                empty_color=default_color,
                draw_missing_guides=False,
                assign_record_texture_file=False,
                log_path=log_path,
                update_uv_transforms=False,
            ):
                replacements.append(f"{name}={path.name}")
            else:
                port_log(log_path, f"{name}: writing default special texture {entry['width']}x{entry['height']}")
                pixels = image_pixels(entry["width"], entry["height"], default_color)
                save_png(path, entry["width"], entry["height"], pixels)
        else:
            records = records_by_texture.get(name, [])
            port_log(log_path, f"{name}: base map, records={len(records)}")
            if is_shared_face_atlas(name, records):
                entry = texture_entry(props, name)
                if entry is not None and entry.expression_atlas:
                    port_log(log_path, f"{name}: retaining prepared 4x2 expression atlas")
                else:
                    port_log(log_path, f"{name}: retaining native shared eye/mouth atlas")
                continue
            missing_sources = [
                obj.name for record, obj in texture_items_for_records(records) if not object_texture_path(record, obj)
            ]
            if missing_sources:
                port_log(log_path, f"{name}: missing source image for {', '.join(missing_sources)}; preserving native G4TX entry")
                discard_generated_atlas(texture_entry(props, name), path)
                continue
            if build_texture_spritesheet(path, entry, records, props, log_path=log_path):
                replacements.append(f"{name}={path.name}")
                atlas_entry = texture_entry(props, name)
                if atlas_entry is not None:
                    atlas_entry.atlas_signature = atlas_signature(name, records)
                    atlas_size = load_image_pixels(str(path))
                    dimensions = f"{atlas_size[0]}x{atlas_size[1]}" if atlas_size is not None else "unknown size"
                    atlas_entry.atlas_summary = (
                        f"{len(texture_items_for_records(records))} object(s), {dimensions}, {path.name}"
                    )
            else:
                port_log(log_path, f"{name}: unreadable source; preserving native G4TX entry")
                discard_generated_atlas(texture_entry(props, name), path)
    props.texture_source_dir = str(output_dir)
    props.texture_replacements = join_csv(replacements)
    generated = dict(item.split("=", 1) for item in replacements)
    for entry in props.texture_entries:
        if entry.texture_name in generated:
            entry.replacement_path = str(output_dir / generated[entry.texture_name])
    port_log(log_path, f"Generate PNG set finished; replacements={len(replacements)}")
    return len(replacements)


def build_expression_pool_atlas(props: G4PortSceneSettings, output_dir: Path) -> Path:
    texture_name = shared_face_texture_key([entry.texture_name for entry in props.texture_entries])
    entry = texture_entry(props, texture_name)
    if entry is None:
        raise RuntimeError("The original model has no shared eye/mouth texture entry")
    sources = expression_pool_paths(props)
    if len(sources) != FACE_ATLAS_SLOTS or not all(path.is_file() for path in sources):
        raise RuntimeError("Expression pool requires exactly 8 valid images for its 4x2 layout")
    original_model = resolve_file(props.original_model)
    raw_root = infer_data_root(original_model) if original_model.is_file() else None
    g4tx_path = original_g4tx_path(raw_root, props.model_rel) if raw_root is not None else None
    source_entry = next((item for item in parse_g4tx_entries(g4tx_path) if item["name"] == texture_name), None) if g4tx_path else None
    if source_entry is None:
        raise RuntimeError(f"Native G4TX entry not found: {texture_name}")
    width, height = source_entry["width"], source_entry["height"]
    cell_width = width // FACE_ATLAS_COLUMNS
    cell_height = height // FACE_ATLAS_ROWS
    pixels = image_pixels(width, height, (0.0, 0.0, 0.0, 0.0))
    for index, source_path in enumerate(sources):
        source = load_image_pixels(str(source_path))
        if source is None:
            raise RuntimeError(f"Could not read expression image: {source_path}")
        column = index % FACE_ATLAS_COLUMNS
        row = index // FACE_ATLAS_COLUMNS
        blit_image_fit(pixels, width, height, source, column * cell_width, row * cell_height, cell_width, cell_height)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{texture_name}.png"
    save_png(output_path, width, height, pixels)
    entry.replacement_path = str(output_path)
    entry.atlas_signature = expression_pool_signature(props, texture_name)
    entry.atlas_summary = f"{FACE_ATLAS_COLUMNS}x{FACE_ATLAS_ROWS} expression atlas, {width}x{height}, {output_path.name}"
    entry.expression_atlas = True
    entry.expression_atlas_mode = "pool"
    props.texture_source_dir = str(output_dir)
    return output_path


def has_unchanged_native_roundtrip(props: G4PortSceneSettings, original_model: Path) -> bool:
    """True only when the current assignment is an untouched native import."""
    if not props.preserve_native_roundtrip or original_model.suffix.lower() != ".g4md":
        return False
    if props.texture_mode == "keep" or (props.texture_mode == "custom" and props.texture_map()):
        return False
    source = str(original_model.resolve())
    records = list(props.records)
    if not records:
        return False
    for record in records:
        objects = objects_for_record(record)
        if not objects:
            return False
        for obj in objects:
            if obj.get("g4_native_model_source") != source:
                return False
            if obj.get("g4_native_roundtrip_signature") != native_mesh_signature(obj):
                return False
            target = getattr(obj.level5_g4_port, "target_record", "__none__")
            if target not in {"__none__", record.output_name}:
                return False
    return True


def copy_unchanged_native_roundtrip(
    props: G4PortSceneSettings,
    original_model: Path,
    raw_root: Path,
    package_root: Path,
    source_g4tx: Path,
) -> dict:
    """Preserve native bytes for an identity import/export instead of rebuilding records."""
    output_root = package_root / "data"
    common_rel = Path(props.model_rel).with_suffix(".g4md")
    common_out = output_root / "common" / common_rel
    g4mg_source = original_model.with_suffix(".g4mg")
    if not g4mg_source.is_file():
        raise RuntimeError(f"Original G4MG not found next to {original_model.name}")
    common_out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original_model, common_out)
    shutil.copy2(g4mg_source, common_out.with_suffix(".g4mg"))

    texture_out_dir = output_root / source_g4tx.parent.resolve().relative_to(raw_root.resolve())
    texture_out_dir.mkdir(parents=True, exist_ok=True)
    for source in source_g4tx.parent.glob("*.g4tx"):
        shutil.copy2(source, texture_out_dir / source.name)
    entries = parse_g4tx_entries(source_g4tx)
    return {
        "meshes": len(props.records),
        "textures": len(entries),
        "roundtrip_preserved": True,
        "g4md": str(common_out),
        "g4mg": str(common_out.with_suffix(".g4mg")),
        "g4tx": str(texture_out_dir / source_g4tx.name),
        "texture_platform": source_g4tx.parent.parent.name,
        "package_root": str(package_root),
        "data_root": str(output_root),
    }


def run_port(context, filepath: str = "") -> tuple[dict, Path]:
    prefs = addon_preferences()
    props = settings(context)
    original_model = resolve_file(props.original_model)
    if not original_model.is_file():
        raise RuntimeError(
            "Choose the original model first: in the Level-5 > G4 Port panel, "
            "press 'Choose Original G4MD/G4PKM' and select the base .g4md or .g4pkm "
            "from data/common or data/dx11."
        )
    raw_root = infer_data_root(original_model)
    if raw_root is None:
        raise RuntimeError("The original model must be inside a data/common or data/dx11 filesystem tree.")
    source_g4tx = original_g4tx_path(raw_root, props.model_rel)
    if source_g4tx is None:
        raise RuntimeError(f"Original G4TX not found in DX11 or NX for {props.model_rel}")

    package_root = Path(bpy.path.abspath(filepath)) if filepath else resolve_file(getattr(prefs, "output_root", ""))
    if has_unchanged_native_roundtrip(props, original_model):
        report = copy_unchanged_native_roundtrip(props, original_model, raw_root, package_root, source_g4tx)
        cache = resolve_file(getattr(prefs, "cache_dir", ""), default_cache_dir())
        cache.mkdir(parents=True, exist_ok=True)
        report_path = cache / "export_report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        port_log(None, "Untouched native import detected; copied original G4MD/G4MG/G4TX bytes")
        return report, report_path

    cache = resolve_file(getattr(prefs, "cache_dir", ""), default_cache_dir())
    cache.mkdir(parents=True, exist_ok=True)

    dae_path = cache / "scene_export.dae"
    weights_path = cache / "scene_weights.json"
    report_path = cache / ("analyze_report.json" if props.analyze_only else "export_report.json")
    output_root = package_root / "data"

    export_collada(dae_path, props.selected_only, props.align_forward_to_y, props.apply_modifiers)
    mesh_count = write_weights_json(weights_path, props.selected_only)
    if mesh_count == 0:
        raise RuntimeError("No mesh objects were found to export.")
    if not props.use_source_uv_transforms and not props.auto_pack_source_uvs:
        reset_uv_tiles(props)

    if props.texture_mode == "custom":
        atlas_rows = atlas_status_rows(props)
        refresh_needed = [
            row for row in atlas_rows
            if row["state"] in {"native", "stale"} and not row.get("shared_face", False)
        ]
        warnings = [row for row in atlas_rows if row["state"] == "warning"]
        for row in warnings:
            port_log(None, f"Texture {row['name']}: {row['message']}; preserving native G4TX entry")
        if props.generate_png_set_on_export and refresh_needed:
            model_name = Path(props.model_rel).name or "model"
            generate_texture_png_set(context, package_root / "texture_sources" / model_name)
        elif refresh_needed:
            names = ", ".join(row["name"] for row in refresh_needed)
            port_log(None, f"Prepared atlas missing or stale for {names}; preserving native G4TX entries")

    prepare_custom_textures(props, dae_path)

    config = generated_config_path(cache)
    config.write_text(json.dumps(props.to_config(), indent=2), encoding="utf-8")

    needs_pillow = props.texture_mode == "custom" and any(
        Path(path).suffix.lower() not in {".dds", ".nxtch"}
        for path in props.texture_map().values()
    )
    command = [
        export_python(prefs, needs_pillow),
        str(resolve_port_script(prefs)),
        str(dae_path),
        "--raw-root",
        str(raw_root),
        "--config",
        str(config),
        "--weights-json",
        str(weights_path),
        "--report-json",
        str(report_path),
    ]
    chara_model = resolve_file(getattr(prefs, "chara_model_xml", ""))
    if chara_model.is_file():
        command.extend(["--chara-model", str(chara_model)])
    if props.analyze_only:
        command.append("--analyze")
    else:
        command.extend(["--texture-mode", props.texture_mode, "--out-root", str(output_root)])

    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            "G4 port export failed\n"
            f"Command: {shlex.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read export report: {report_path}") from exc

    if not getattr(prefs, "keep_temporary_files", False):
        for path in (dae_path, weights_path, generated_config_path(cache)):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        shutil.rmtree(dae_path.parent / "customTextures", ignore_errors=True)
    report["package_root"] = str(package_root)
    report["data_root"] = str(output_root)
    return report, report_path


class LEVEL5_G4PORT_UL_records(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.label(text=item.output_name or "Record", icon="MESH_DATA")
            row.label(text=item.material_name)
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text=item.output_name[:2])


class LEVEL5_G4PORT_UL_joint_aliases(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.label(text=item.source_group or "Vertex Group", icon="GROUP_VERTEX")
            row.label(text=item.target_joint or "Unmapped")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text=item.source_group[:2])


class LEVEL5_G4PORT_OT_load_original_model(Operator, ImportHelper):
    bl_idname = "level5_g4_port.load_original_model"
    bl_label = "Choose Original G4MD/G4PKM"
    bl_description = "Read an original G4MD/G4PKM and create editable records from its native meshes/materials"

    filename_ext = ".g4md"
    filter_glob: StringProperty(default="*.g4md;*.g4pkm", options={"HIDDEN"})

    def execute(self, context):
        path = Path(self.filepath)
        if path.suffix.lower() not in MODEL_EXTENSIONS:
            self.report({"ERROR"}, "Select a G4MD or G4PKM model")
            return {"CANCELLED"}
        try:
            apply_original_model_to_settings(settings(context), path, run_model_probe(path, addon_preferences()))
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Loaded original template: {path.name}")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_add_record(Operator):
    bl_idname = "level5_g4_port.add_record"
    bl_label = "Add Record"

    def execute(self, context):
        props = settings(context)
        record = props.records.add()
        selected = mesh_objects(True)
        record.output_name = selected[0].name if selected else "new_record"
        record.material_name = selected[0].active_material.name if selected and selected[0].active_material else ""
        record.match_names = join_csv(obj.name for obj in selected) if selected else "*"
        props.active_record = len(props.records) - 1
        props.use_preset_file = False
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_assign_selected(Operator):
    bl_idname = "level5_g4_port.assign_selected"
    bl_label = "Assign Selected"
    bl_description = "Assign selected Blender meshes to the active original mesh record"

    def execute(self, context):
        props = settings(context)
        if not props.records:
            self.report({"ERROR"}, "No target record exists")
            return {"CANCELLED"}
        count = assign_selected_to_record(context, props.records[props.active_record])
        if count == 0:
            self.report({"ERROR"}, "Select at least one mesh object")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Assigned {count} mesh object(s)")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_guess_assignments(Operator):
    bl_idname = "level5_g4_port.guess_assignments"
    bl_label = "Guess Assignments"
    bl_description = "Assign scene meshes to original records by matching object and material names"

    def execute(self, context):
        count = guess_object_assignments(settings(context))
        self.report({"INFO"}, f"Assigned {count} mesh object(s) by name/material")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_use_active_texture(Operator):
    bl_idname = "level5_g4_port.use_active_texture"
    bl_label = "Use Active Texture"
    bl_description = "Use the active object's image texture as the custom replacement for the active record"

    def execute(self, context):
        props = settings(context)
        if not props.records:
            self.report({"ERROR"}, "No target record exists")
            return {"CANCELLED"}
        image_path = active_material_image_path(context)
        if not image_path:
            self.report({"ERROR"}, "The active mesh material has no image texture")
            return {"CANCELLED"}
        record = props.records[props.active_record]
        active = context.active_object
        if active is not None and active.type == "MESH":
            active.level5_g4_port.source_texture = image_path
        props.use_preset_file = False
        self.report({"INFO"}, f"Atlas source set for {active.name if active else record.output_name}: {Path(image_path).name}")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_generate_texture_pngs(Operator):
    bl_idname = "level5_g4_port.prepare_atlas"
    bl_label = "Prepare Atlas"
    bl_description = "Build only the assigned base-texture atlases and preserve every other native G4TX entry"

    def execute(self, context):
        prefs = addon_preferences()
        props = settings(context)
        sync_assignment_table(context)
        package_root = resolve_file(getattr(prefs, "output_root", ""), default_output_root())
        model_name = Path(props.model_rel).name or "model"
        output_dir = package_root / "texture_sources" / model_name
        log_path = resolve_file(getattr(prefs, "cache_dir", ""), default_cache_dir()) / "generate_png_set.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        try:
            count = generate_texture_png_set(context, output_dir, log_path)
        except Exception as exc:
            self.report({"ERROR"}, f"{exc} (log: {log_path})")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Prepared {count} atlas texture(s) in {output_dir}; log: {log_path}")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_initialize_expression_pool(Operator):
    bl_idname = "level5_g4_port.initialize_expression_pool"
    bl_label = "Initialize 4x2 Expression Pool"

    def execute(self, context):
        pool = settings(context).expression_pool
        pool.clear()
        for _ in range(FACE_ATLAS_SLOTS):
            pool.add()
        self.report({"INFO"}, "Expression pool initialized with 8 slots (4x2)")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_build_expression_atlas(Operator):
    bl_idname = "level5_g4_port.build_expression_atlas"
    bl_label = "Build 4x2 Expression Atlas"
    bl_description = "Build the shared eye/mouth texture from 8 expression images"

    def execute(self, context):
        prefs = addon_preferences()
        props = settings(context)
        package_root = resolve_file(getattr(prefs, "output_root", ""), default_output_root())
        model_name = Path(props.model_rel).name or "model"
        try:
            output_path = build_expression_pool_atlas(props, package_root / "texture_sources" / model_name)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Built 4x2 expression atlas: {output_path.name}")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_use_existing_expression_atlas(Operator):
    bl_idname = "level5_g4_port.use_existing_expression_atlas"
    bl_label = "Use Existing 4x2 Atlas"
    bl_description = "Use the selected prepared facial atlas without rebuilding it"

    def execute(self, context):
        props = settings(context)
        texture_name = shared_face_texture_key([entry.texture_name for entry in props.texture_entries])
        entry = texture_entry(props, texture_name)
        source = Path(bpy.path.abspath(entry.replacement_path)) if entry is not None and entry.replacement_path else None
        if entry is None or source is None or load_image_pixels(str(source)) is None:
            self.report({"ERROR"}, "Choose a valid prepared 4x2 facial atlas first")
            return {"CANCELLED"}
        entry.expression_atlas = True
        entry.expression_atlas_mode = "existing"
        entry.atlas_signature = ""
        entry.atlas_summary = f"Existing 4x2 expression atlas, {source.name}"
        self.report({"INFO"}, f"Using existing 4x2 expression atlas: {source.name}")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_reset_object_uv_tiles(Operator):
    bl_idname = "level5_g4_port.reset_object_uv_tiles"
    bl_label = "Reset Object UV Tiles"
    bl_description = "Clear per-object UV scale and offset values"

    def execute(self, context):
        props = settings(context)
        reset_uv_tiles(props)
        props.use_source_uv_transforms = False
        props.auto_pack_source_uvs = False
        props.use_preset_file = False
        self.report({"INFO"}, "Object UV tiles reset")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_detect_vertex_groups(Operator):
    bl_idname = "level5_g4_port.detect_vertex_groups"
    bl_label = "Detect Vertex Groups"
    bl_description = "Add Blender vertex groups to the rigging alias table"

    selected_only: BoolProperty(name="Selected Only", default=False)

    def execute(self, context):
        count = detect_joint_aliases(settings(context), self.selected_only)
        self.report({"INFO"}, f"Detected {count} new vertex group(s)")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_auto_map_joints(Operator):
    bl_idname = "level5_g4_port.auto_map_joints"
    bl_label = "Auto-map Common Joints"
    bl_description = "Fill empty aliases using common head, neck, hair and ribbon naming patterns"

    def execute(self, context):
        count = auto_map_joint_aliases(settings(context))
        self.report({"INFO"}, f"Mapped {count} joint alias(es)")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_add_joint_alias(Operator):
    bl_idname = "level5_g4_port.add_joint_alias"
    bl_label = "Add Joint Alias"

    def execute(self, context):
        props = settings(context)
        alias = props.joint_aliases.add()
        alias.source_group = "vertex_group"
        alias.target_joint = ""
        props.active_joint_alias = len(props.joint_aliases) - 1
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_remove_joint_alias(Operator):
    bl_idname = "level5_g4_port.remove_joint_alias"
    bl_label = "Remove Joint Alias"

    def execute(self, context):
        props = settings(context)
        if props.joint_aliases:
            props.joint_aliases.remove(props.active_joint_alias)
            props.active_joint_alias = min(props.active_joint_alias, max(0, len(props.joint_aliases) - 1))
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_remove_record(Operator):
    bl_idname = "level5_g4_port.remove_record"
    bl_label = "Remove Record"

    def execute(self, context):
        props = settings(context)
        if props.records:
            props.records.remove(props.active_record)
            props.active_record = min(props.active_record, max(0, len(props.records) - 1))
            props.use_preset_file = False
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_analyze(Operator):
    bl_idname = "level5_g4_port.analyze"
    bl_label = "Analyze Port"
    bl_description = "Validate the scene and generated weights without writing the final G4 files"

    def execute(self, context):
        props = settings(context)
        previous = props.analyze_only
        props.analyze_only = True
        try:
            sync_assignment_table(context)
            report, report_path = run_port(context)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            props.analyze_only = previous
        unresolved = report.get("unresolved_influences", 0)
        records = len(report.get("records", [])) if isinstance(report.get("records"), list) else 0
        self.report({"INFO"}, f"Analysis OK: {records} records, unresolved influences {unresolved}. {report_path}")
        return {"FINISHED"}


class EXPORT_OT_level5_g4_port(Operator, ExportHelper):
    bl_idname = "export_scene.level5_g4_port"
    bl_label = "Export Level-5 G4 Port"
    bl_options = {"REGISTER"}

    filename_ext = ""
    filepath: StringProperty(subtype="DIR_PATH")
    waiting_for_output_path: BoolProperty(options={"HIDDEN", "SKIP_SAVE"}, default=False)

    def execute(self, context):
        if not self.waiting_for_output_path:
            prefs = addon_preferences()
            if not self.filepath:
                self.filepath = getattr(prefs, "output_root", "") or default_output_root()
            self.waiting_for_output_path = True
            context.window_manager.fileselect_add(self)
            return {"RUNNING_MODAL"}
        try:
            sync_assignment_table(context)
            report, report_path = run_port(context, self.filepath)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        validation = report.get("validation") or {}
        vertices = validation.get("vertices_checked", report.get("vertices", "?"))
        indices = validation.get("indices_checked", report.get("indices", "?"))
        self.report({"INFO"}, f"G4 port exported: {vertices} vertices, {indices} indices. {report_path}")
        return {"FINISHED"}

    def invoke(self, context, event):
        self.waiting_for_output_path = False
        self.filepath = ""
        return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        props = settings(context)
        layout = self.layout
        draw_original_and_mapping(layout, context, include_actions=True)
        layout.separator()
        draw_export_settings(layout, props, self)


def draw_original_and_mapping(layout, context, include_actions: bool) -> None:
    props = settings(context)
    row = layout.row(align=True)
    row.prop(
        props,
        "show_original",
        icon="TRIA_DOWN" if props.show_original else "TRIA_RIGHT",
        emboss=False,
    )
    if props.show_original:
        box = layout.box()
    else:
        box = None
    if box is not None:
        box.label(text="1. Original model template", icon="FILE_FOLDER")
        box.operator(LEVEL5_G4PORT_OT_load_original_model.bl_idname, icon="FILE_FOLDER")
        if props.original_model:
            box.prop(props, "original_model", text="Loaded")
            box.label(text=props.model_rel, icon="FILE")
        else:
            box.label(text="Required before Analyze or Export", icon="ERROR")
        if props.texture_names:
            box.label(text=f"{len(split_csv(props.texture_names))} G4TX texture(s)", icon="TEXTURE")

    row = layout.row(align=True)
    row.prop(
        props,
        "show_mapping",
        icon="TRIA_DOWN" if props.show_mapping else "TRIA_RIGHT",
        emboss=False,
    )
    if props.show_mapping:
        box = layout.box()
        box.label(text="Select target mesh:", icon="OUTLINER_OB_MESH")
        for obj in mesh_objects(False):
            row = box.row(align=True)
            row.label(text=obj.name, icon="MESH_DATA")
            row.prop(obj.level5_g4_port, "target_record", text="")
            row.prop(obj.level5_g4_port, "source_texture", text="")
        row = box.row(align=True)
        row.operator(LEVEL5_G4PORT_OT_assign_selected.bl_idname, icon="RESTRICT_SELECT_OFF")
        row.operator(LEVEL5_G4PORT_OT_guess_assignments.bl_idname, icon="VIEWZOOM")

    row = layout.row(align=True)
    row.prop(
        props,
        "show_rigging",
        icon="TRIA_DOWN" if props.show_rigging else "TRIA_RIGHT",
        emboss=False,
    )
    if props.show_rigging:
        box = layout.box()
        row = box.row(align=True)
        row.operator(LEVEL5_G4PORT_OT_detect_vertex_groups.bl_idname, icon="GROUP_VERTEX")
        row.operator(LEVEL5_G4PORT_OT_auto_map_joints.bl_idname, icon="BONE_DATA")
        row = box.row()
        row.template_list(
            "LEVEL5_G4PORT_UL_joint_aliases",
            "",
            props,
            "joint_aliases",
            props,
            "active_joint_alias",
            rows=5,
        )
        col = row.column(align=True)
        col.operator(LEVEL5_G4PORT_OT_add_joint_alias.bl_idname, text="", icon="ADD")
        col.operator(LEVEL5_G4PORT_OT_remove_joint_alias.bl_idname, text="", icon="REMOVE")
        if props.joint_aliases and 0 <= props.active_joint_alias < len(props.joint_aliases):
            alias = props.joint_aliases[props.active_joint_alias]
            edit = box.box()
            edit.prop(alias, "source_group")
            edit.prop(alias, "target_joint")
        unresolved = sum(1 for alias in props.joint_aliases if alias.source_group and not alias.target_joint)
        if unresolved:
            box.label(text=f"{unresolved} unmapped group(s)", icon="ERROR")
        box.prop(props, "strict_skinning")

    row = layout.row(align=True)
    row.prop(
        props,
        "show_record_settings",
        icon="TRIA_DOWN" if props.show_record_settings else "TRIA_RIGHT",
        emboss=False,
    )
    if props.show_record_settings:
        row = layout.row()
        row.template_list(
            "LEVEL5_G4PORT_UL_records",
            "",
            props,
            "records",
            props,
            "active_record",
            rows=5,
        )
        col = row.column(align=True)
        col.operator(LEVEL5_G4PORT_OT_add_record.bl_idname, text="", icon="ADD")
        col.operator(LEVEL5_G4PORT_OT_remove_record.bl_idname, text="", icon="REMOVE")
        if props.records and 0 <= props.active_record < len(props.records):
            record = props.records[props.active_record]
            box = layout.box()
            if record.original_index >= 0:
                box.label(text=f"Original mesh #{record.original_index}", icon="MESH_DATA")
            box.prop(record, "output_name")
            box.prop(record, "material_name")
            box.prop(record, "match_names")
            row = box.row(align=True)
            row.prop(record, "uv_flip_x")
            row.prop(record, "uv_flip_y")
            row = box.row(align=True)
            row.prop(record, "uv_scale_u")
            row.prop(record, "uv_scale_v")
            row = box.row(align=True)
            row.prop(record, "uv_offset_u")
            row.prop(record, "uv_offset_v")
            box.prop(record, "rigid_joint")
            box.prop(record, "auto_palette")
            box.prop(record, "secondary_weight_scale")
            box.prop(record, "weight_anchor_joint")
            box.prop(record, "texture_key")
            box.operator(LEVEL5_G4PORT_OT_use_active_texture.bl_idname, text="Use Active Image as Atlas Source", icon="TEXTURE")
            box.prop(record, "fallback_degenerate")
            box.prop(record, "force_layout_material")
            if record.force_layout_material:
                row = box.row(align=True)
                row.prop(record, "layout_index")
                row.prop(record, "material_index")

    row = layout.row(align=True)
    row.prop(
        props,
        "show_textures",
        icon="TRIA_DOWN" if props.show_textures else "TRIA_RIGHT",
        emboss=False,
    )
    if props.show_textures:
        box = layout.box()
        box.label(text="3. Prepare and review atlas", icon="TEXTURE")
        box.prop(props, "texture_platform")
        for status in atlas_status_rows(props):
            row = box.row(align=True)
            icon = {"ready": "CHECKMARK", "manual": "CHECKMARK", "stale": "FILE_REFRESH", "warning": "ERROR"}.get(status["state"], "INFO")
            row.label(text=status["name"], icon=icon)
            row.label(text=status["message"])
            if status["repeated"]:
                box.label(text=f"UVs adjusted for: {', '.join(status['repeated'])}", icon="UV")
        box.prop(props, "texture_source_dir")
        if props.texture_entries:
            for entry in props.texture_entries:
                row = box.row(align=True)
                row.label(text=entry.texture_name, icon="TEXTURE")
                row.prop(entry, "replacement_path", text="")
        else:
            box.label(text="Load an original model to list its G4TX textures", icon="INFO")
        face_key = shared_face_texture_key([entry.texture_name for entry in props.texture_entries])
        if face_key:
            expression_box = box.box()
            expression_box.label(text=f"Expression pool for {face_key} (4x2)", icon="SEQ_CHROMA_SCOPE")
            expression_box.label(text="eye_10 and mouth_10 keep the native atlas unless a 4x2 source is explicitly accepted", icon="INFO")
            face_entry = texture_entry(props, face_key)
            if face_entry is not None:
                expression_box.prop(face_entry, "replacement_path", text="Existing 4x2 Atlas")
            row = expression_box.row(align=True)
            row.operator(LEVEL5_G4PORT_OT_use_existing_expression_atlas.bl_idname, icon="CHECKMARK")
            row.operator(LEVEL5_G4PORT_OT_initialize_expression_pool.bl_idname, icon="ADD")
            row.operator(LEVEL5_G4PORT_OT_build_expression_atlas.bl_idname, icon="IMAGE_DATA")
            for index, item in enumerate(props.expression_pool):
                expression_box.prop(item, "image_path", text=f"Cell {index % FACE_ATLAS_COLUMNS + 1}, {index // FACE_ATLAS_COLUMNS + 1}")
        box.prop(props, "generate_png_set_on_export")
        box.prop(props, "use_source_uv_transforms")
        box.prop(props, "auto_pack_source_uvs")
        box.prop(props, "replace_special_textures")
        row = box.row(align=True)
        row.operator(LEVEL5_G4PORT_OT_generate_texture_pngs.bl_idname, icon="TEXTURE")
        row.operator(LEVEL5_G4PORT_OT_reset_object_uv_tiles.bl_idname, icon="FILE_REFRESH")

    if include_actions:
        row = layout.row(align=True)
        row.operator(LEVEL5_G4PORT_OT_analyze.bl_idname, icon="VIEWZOOM")


def draw_texture_replacements(layout, props: G4PortSceneSettings) -> None:
    box = layout.box()
    box.label(text="G4TX Texture Replacements", icon="TEXTURE")
    box.prop(props, "texture_platform")
    if props.texture_entries:
        for entry in props.texture_entries:
            row = box.row(align=True)
            row.label(text=entry.texture_name)
            row.prop(entry, "replacement_path", text="")
    else:
        box.label(text="Load an original model to list its G4TX textures", icon="INFO")
    box.label(text="Empty paths preserve the original texture", icon="CHECKMARK")
    box.prop(props, "replace_special_textures")


def draw_export_settings(layout, props: G4PortSceneSettings, operator=None) -> None:
    row = layout.row(align=True)
    row.prop(
        props,
        "show_export",
        icon="TRIA_DOWN" if props.show_export else "TRIA_RIGHT",
        emboss=False,
    )
    if not props.show_export:
        return
    box = layout.box()
    box.prop(props, "texture_mode")
    if props.texture_mode == "custom":
        statuses = atlas_status_rows(props)
        unresolved = [row for row in statuses if row["state"] in {"native", "stale", "warning"}]
        if unresolved:
            box.label(text=f"{len(unresolved)} texture(s) will keep their native G4TX payload", icon="INFO")
        draw_texture_replacements(layout, props)
    box.prop(props, "selected_only")
    box.prop(props, "apply_modifiers")
    box.prop(props, "align_forward_to_y")
    box.prop(props, "preserve_native_roundtrip")


class LEVEL5_G4PORT_PT_panel(Panel):
    bl_label = "G4 Port"
    bl_idname = "LEVEL5_G4PORT_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Level-5"

    def draw(self, context):
        layout = self.layout
        draw_original_and_mapping(layout, context, include_actions=False)
        props = settings(context)
        draw_export_settings(layout, props)

        row = layout.row(align=True)
        row.operator(LEVEL5_G4PORT_OT_analyze.bl_idname, icon="VIEWZOOM")
        row.operator(EXPORT_OT_level5_g4_port.bl_idname, icon="EXPORT")


def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_level5_g4_port.bl_idname, text="Level-5 G4 Port")


classes = []

if IS_STANDALONE_ADDON:
    classes.append(G4PortPreferences)

classes.extend([
    G4PortObjectSettings,
    G4PortJointAlias,
    G4PortTextureReplacement,
    G4PortExpressionImage,
    G4PortRecord,
    G4PortSceneSettings,
    LEVEL5_G4PORT_UL_records,
    LEVEL5_G4PORT_UL_joint_aliases,
    LEVEL5_G4PORT_OT_load_original_model,
    LEVEL5_G4PORT_OT_add_record,
    LEVEL5_G4PORT_OT_remove_record,
    LEVEL5_G4PORT_OT_assign_selected,
    LEVEL5_G4PORT_OT_guess_assignments,
    LEVEL5_G4PORT_OT_use_active_texture,
    LEVEL5_G4PORT_OT_generate_texture_pngs,
    LEVEL5_G4PORT_OT_initialize_expression_pool,
    LEVEL5_G4PORT_OT_build_expression_atlas,
    LEVEL5_G4PORT_OT_use_existing_expression_atlas,
    LEVEL5_G4PORT_OT_reset_object_uv_tiles,
    LEVEL5_G4PORT_OT_detect_vertex_groups,
    LEVEL5_G4PORT_OT_auto_map_joints,
    LEVEL5_G4PORT_OT_add_joint_alias,
    LEVEL5_G4PORT_OT_remove_joint_alias,
    LEVEL5_G4PORT_OT_analyze,
    EXPORT_OT_level5_g4_port,
    LEVEL5_G4PORT_PT_panel,
])


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.level5_g4_port = bpy.props.PointerProperty(type=G4PortObjectSettings)
    bpy.types.Scene.level5_g4_port = bpy.props.PointerProperty(type=G4PortSceneSettings)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    del bpy.types.Scene.level5_g4_port
    del bpy.types.Object.level5_g4_port
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
