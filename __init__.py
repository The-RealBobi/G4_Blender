bl_info = {
    "name": "Level-5 G4 Blender Tools",
    "author": "Bobi",
    "version": (0, 11, 2),
    "blender": (4, 0, 0),
    "location": "File > Import/Export > G4MD / G4PKM",
    "description": "",
    "category": "Import-Export",
}

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, StringProperty
from bpy.types import AddonPreferences, Operator, OperatorFileListElement
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Quaternion, Vector

ADDON_ID = __name__
MODEL_EXTENSIONS = {".g4md", ".g4pkm"}

try:
    from . import g4_port_addon
except ImportError:
    import g4_port_addon

try:
    from . import g4_animation_addon
except ImportError:
    import g4_animation_addon

g4_port_addon.ADDON_ID = ADDON_ID
g4_animation_addon.ADDON_ID = ADDON_ID


def default_probe_script() -> str:
    env_path = os.environ.get("LEVEL5_G4_PROBE")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    addon_path = Path(__file__).resolve()
    candidates.extend(
        [
            addon_path.parent / "g4_model_probe.py",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def default_export_dir() -> str:
    env_path = os.environ.get("LEVEL5_G4_EXPORT_DIR")
    if env_path:
        return env_path
    return str(Path.home() / "level5_g4_exports")


def default_chara_model_xml() -> str:
    env_path = os.environ.get("LEVEL5_G4_CHARA_MODEL")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))

    addon_path = Path(__file__).resolve()
    search_dirs = [addon_path.parent, addon_path.parent / "TOOLS", addon_path.parents[1] / "TOOLS"]
    for directory in search_dirs:
        candidates.append(directory / "chara_model_1.03.49.00.cfg.bin.xml")
        candidates.extend(sorted(directory.glob("chara_model*.xml")))

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return str(candidate)
    return ""


def default_chara_model_lookup() -> str:
    env_path = os.environ.get("LEVEL5_G4_CHARA_LOOKUP")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))

    addon_path = Path(__file__).resolve()
    candidates.extend(
        [
            addon_path.parent / "chara_model_lookup.json",
            addon_path.parent / "data" / "chara_model_lookup.json",
            addon_path.parents[1] / "data" / "chara_model_lookup.json",
        ]
    )

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return str(candidate)
    return ""


def default_python() -> str:
    for candidate in (sys.executable, "python3"):
        if candidate and Path(candidate).exists():
            return candidate
    return "python3"


def addon_preferences() -> "G4ImporterPreferences":
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is not None:
        return addon.preferences

    class Defaults:
        python_path = default_python()
        probe_script = default_probe_script()
        export_dir = default_export_dir()
        raw_data_root = os.environ.get("LEVEL5_G4_RAW_ROOT", "")
        chara_model_lookup = default_chara_model_lookup()
        chara_model_xml = default_chara_model_xml()
        pack_imported_textures = True
        cleanup_import_cache = True
        apply_bone_orientation = True

    return Defaults()


def resolve_probe_script(prefs: "G4ImporterPreferences") -> Path:
    configured_probe = bpy.path.abspath(getattr(prefs, "probe_script", "") or "")
    probe_script = Path(configured_probe) if configured_probe else Path()
    if probe_script.exists():
        return probe_script

    fallback_path = default_probe_script()
    if fallback_path:
        fallback = Path(fallback_path)
        if fallback.exists():
            return fallback

    raise RuntimeError(
        "Exporter script not found. Set the addon preference 'Exporter Script' "
        "to g4_model_probe.py, place g4_model_probe.py next to the addon, or define LEVEL5_G4_PROBE."
    )


def exporter_environment(prefs: "G4ImporterPreferences", export_dir: Path) -> dict:
    env = os.environ.copy()
    raw_data_root = bpy.path.abspath(getattr(prefs, "raw_data_root", "") or "")
    if raw_data_root:
        env["LEVEL5_G4_RAW_ROOT"] = raw_data_root
    chara_model_lookup = bpy.path.abspath(getattr(prefs, "chara_model_lookup", "") or default_chara_model_lookup())
    if chara_model_lookup:
        env["LEVEL5_G4_CHARA_LOOKUP"] = chara_model_lookup
    chara_model_xml = bpy.path.abspath(getattr(prefs, "chara_model_xml", "") or default_chara_model_xml())
    if chara_model_xml:
        env["LEVEL5_G4_CHARA_MODEL"] = chara_model_xml
    env["LEVEL5_G4_SKELETON_CACHE"] = str(export_dir / "g4_skeleton_cache.json")
    return env


class G4ImporterPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    python_path: StringProperty(
        name="Python",
        subtype="FILE_PATH",
        default=default_python(),
        description="Python executable used to run g4_model_probe.py",
    )
    probe_script: StringProperty(
        name="Exporter Script",
        subtype="FILE_PATH",
        default=default_probe_script(),
        description="Path to the bundled or external g4_model_probe.py",
    )
    export_dir: StringProperty(
        name="Export Cache",
        subtype="DIR_PATH",
        default=default_export_dir(),
        description="Directory where temporary DAE, textures and reports are generated",
    )
    raw_data_root: StringProperty(
        name="Raw Data Root",
        subtype="DIR_PATH",
        default="",
        description="Optional raw/data root. Required when importing by model name instead of selecting a file",
    )
    chara_model_xml: StringProperty(
        name="Chara Model XML",
        subtype="FILE_PATH",
        default=default_chara_model_xml(),
        description="Optional chara_model XML fallback used to resolve shared character skeletons",
    )
    chara_model_lookup: StringProperty(
        name="Chara Model Lookup",
        subtype="FILE_PATH",
        default=default_chara_model_lookup(),
        description="Optional preprocessed chara_model lookup JSON. It can be placed next to the addon",
    )
    pack_imported_textures: BoolProperty(
        name="Pack Imported Textures",
        default=True,
        description="Pack imported DDS images into the Blender file after loading the model",
    )
    cleanup_import_cache: BoolProperty(
        name="Clean Temporary Imports",
        default=True,
        description="Delete generated DAE/report/texture files after Blender has loaded and packed them",
    )
    apply_bone_orientation: BoolProperty(
        name="Apply Bone Orientation",
        default=False,
        description="Orient bones for display; leave disabled when the rig will receive G4MT animation",
    )
    decoder_script: StringProperty(
        name="G4MT Decoder",
        subtype="FILE_PATH",
        default=g4_animation_addon.default_decoder_script(),
        description="Path to bundled or external g4mt_motion.py",
    )
    camera_decoder_script: StringProperty(
        name="G4CM Decoder",
        subtype="FILE_PATH",
        default=g4_animation_addon.default_camera_decoder_script(),
        description="Path to bundled or external g4cm_camera.py",
    )
    keep_decode_json: BoolProperty(
        name="Keep Animation Decode JSON",
        default=False,
        description="Keep intermediate G4MT/G4CM JSON files for investigation",
    )
    event_character_parts: StringProperty(
        default="{}",
        options={"HIDDEN"},
        description="Persistent body and shoes selections keyed by event actor ID",
    )
    port_script: StringProperty(
        name="G4 Port Script",
        subtype="FILE_PATH",
        default=g4_port_addon.default_port_script(),
        description="Path to bundled or external g4_port.py; bundled installations detect it automatically",
    )
    config_dir: StringProperty(
        name="Port Preset Folder",
        subtype="DIR_PATH",
        default=g4_port_addon.default_config_dir(),
        description="Folder containing G4 port presets",
    )
    output_root: StringProperty(
        name="Port Package Folder",
        subtype="DIR_PATH",
        default=os.environ.get("LEVEL5_G4_OUT_ROOT", g4_port_addon.default_output_root()),
        description="Destination folder. The exporter writes data/common and data/dx11 inside it",
    )
    cache_dir: StringProperty(
        name="Port Export Cache",
        subtype="DIR_PATH",
        default=g4_port_addon.default_cache_dir(),
        description="Temporary folder for DAE, weights, generated presets and reports",
    )
    keep_temporary_files: BoolProperty(
        name="Keep Port Temporary Files",
        default=False,
        description="Keep generated DAE/config/weights files after export",
    )

    def draw(self, context):
        layout = self.layout
        shared_box = layout.box()
        shared_box.label(text="Shared Runtime")
        shared_box.prop(self, "python_path")
        shared_box.prop(self, "probe_script")
        shared_box.prop(self, "raw_data_root")
        shared_box.prop(self, "chara_model_xml")

        import_box = layout.box()
        import_box.label(text="Import")
        import_box.prop(self, "export_dir")
        import_box.prop(self, "chara_model_lookup")
        import_box.prop(self, "pack_imported_textures")
        import_box.prop(self, "cleanup_import_cache")
        import_box.prop(self, "apply_bone_orientation")

        animation_box = layout.box()
        animation_box.label(text="Animation Import")
        animation_box.prop(self, "decoder_script")
        animation_box.prop(self, "camera_decoder_script")
        animation_box.prop(self, "keep_decode_json")

        port_box = layout.box()
        port_box.label(text="Port Export")
        port_box.prop(self, "port_script")
        port_box.prop(self, "config_dir")
        port_box.prop(self, "output_root")
        port_box.prop(self, "cache_dir")
        port_box.prop(self, "keep_temporary_files")


def matrix_to_flat_list(matrix) -> list[float]:
    return [float(matrix[row][column]) for row in range(4) for column in range(4)]


def armature_skeleton_info(armature) -> dict | None:
    if armature is None or armature.type != "ARMATURE":
        return None
    bones = list(armature.data.bones)
    if not bones:
        return None
    index_by_name = {bone.name: index for index, bone in enumerate(bones)}
    names = [bone.name for bone in bones]
    joint_count = len(bones)
    parent_indices = [
        index_by_name.get(bone.parent.name, joint_count) if bone.parent is not None else joint_count
        for bone in bones
    ]
    bind_matrices = [matrix_to_flat_list(bone.matrix_local) for bone in bones]
    inverse_bind_matrices = [matrix_to_flat_list(bone.matrix_local.inverted_safe()) for bone in bones]
    local_matrices = []
    local_scales = []
    local_rotations_xyzw = []
    local_translations = []
    for bone in bones:
        local_matrix = (
            bone.parent.matrix_local.inverted_safe() @ bone.matrix_local
            if bone.parent is not None
            else bone.matrix_local.copy()
        )
        local_matrices.append(matrix_to_flat_list(local_matrix))
        local_scale = local_matrix.to_scale()
        local_translation = local_matrix.to_translation()
        local_rotation = local_matrix.to_quaternion()
        local_scales.append([float(local_scale.x), float(local_scale.y), float(local_scale.z)])
        local_rotations_xyzw.append(
            [
                float(local_rotation.x),
                float(local_rotation.y),
                float(local_rotation.z),
                float(local_rotation.w),
            ]
        )
        local_translations.append(
            [
                float(local_translation.x),
                float(local_translation.y),
                float(local_translation.z),
            ]
        )
    return {
        "magic": "BLENDER_ARMATURE",
        "joint_count": joint_count,
        "table_count": 0,
        "section_offsets": [],
        "parent_indices": parent_indices,
        "names": names,
        "bind_matrices": bind_matrices,
        "inverse_bind_matrices": inverse_bind_matrices,
        "local_matrices": local_matrices,
        "local_scales": local_scales,
        "local_rotations_xyzw": local_rotations_xyzw,
        "local_translations": local_translations,
    }


def run_exporter(model_path: str, prefs: G4ImporterPreferences, target_armature=None) -> dict:
    python_path = Path(bpy.path.abspath(prefs.python_path or default_python()))
    probe_script = resolve_probe_script(prefs)
    export_dir = Path(bpy.path.abspath(prefs.export_dir))
    export_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(python_path),
        str(probe_script),
        "--export-model",
        "--json",
        "--dae-dir",
        str(export_dir),
        model_path,
    ]
    env = exporter_environment(prefs, export_dir)
    skeleton_override_path: Path | None = None
    skeleton_info = armature_skeleton_info(target_armature)
    if skeleton_info is not None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="level5_g4_target_skeleton_",
            delete=False,
        ) as handle:
            json.dump(skeleton_info, handle, separators=(",", ":"))
            skeleton_override_path = Path(handle.name)
        env["LEVEL5_G4_TARGET_SKELETON"] = str(skeleton_override_path)
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    finally:
        if skeleton_override_path is not None:
            skeleton_override_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "G4 exporter failed\n"
            f"Command: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    try:
        summaries = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Exporter did not return JSON:\n{completed.stdout}") from exc
    if not summaries:
        raise RuntimeError("Exporter returned no model summary")
    summary = summaries[0]
    report_path = Path(summary.get("report", ""))
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}
        for key in (
            "textures",
            "materials",
            "texture_variants",
            "material_records",
            "g4md_texture_hashes",
            "meshes",
            "skeleton_source",
            "skeleton",
            "bone_orientation",
        ):
            if key in report:
                summary[key] = report[key]
    return summary


def import_collada(dae_path: Path) -> set[str]:
    before = set(bpy.data.objects)
    bpy.ops.wm.collada_import(filepath=str(dae_path))
    after = set(bpy.data.objects)
    return {obj.name for obj in after - before}


def blender_base_name(name: str) -> str:
    return re.sub(r"\.\d{3}$", "", name)


def material_image_paths(material) -> set[str]:
    tree = material.node_tree
    if tree is None:
        return set()
    paths = set()
    for node in tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None and node.image.filepath:
            paths.add(str(Path(bpy.path.abspath(node.image.filepath)).resolve()))
    return paths


def materials_are_compatible(left, right) -> bool:
    left_paths = material_image_paths(left)
    right_paths = material_image_paths(right)
    return not left_paths or not right_paths or bool(left_paths & right_paths)


def collapse_duplicate_materials(imported_names: set[str]) -> None:
    for object_name in imported_names:
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            base_name = blender_base_name(material.name)
            if base_name == material.name:
                continue
            target = bpy.data.materials.get(base_name)
            if target is None or target == material or not materials_are_compatible(material, target):
                continue
            slot.material = target
            if material.users == 0:
                bpy.data.materials.remove(material)


def global_orientation_matrices(names: list[str], parents: list[int], rotations: list[list[float]]) -> list[Matrix]:
    local_matrices = []
    for rotation in rotations:
        if len(rotation) != 4:
            local_matrices.append(Matrix.Identity(4))
            continue
        x, y, z, w = rotation
        local_matrices.append(Quaternion((w, x, y, z)).to_matrix().to_4x4())

    global_matrices: list[Matrix] = [Matrix.Identity(4) for _ in names]
    for index, local_matrix in enumerate(local_matrices[: len(names)]):
        parent = parents[index] if index < len(parents) else len(names)
        if 0 <= parent < index:
            global_matrices[index] = global_matrices[parent] @ local_matrix
        else:
            global_matrices[index] = local_matrix
    return global_matrices


def imported_armatures(imported_names: set[str]) -> list[bpy.types.Object]:
    armatures = []
    for item in imported_names:
        obj = item if hasattr(item, "type") else bpy.data.objects.get(item)
        if obj is not None and obj.type == "ARMATURE":
            armatures.append(obj)
    return armatures


def apply_g4_bone_orientation(imported_names: set[str], summary: dict, enabled: bool) -> dict:
    orientation = summary.get("bone_orientation")
    if not enabled or not isinstance(orientation, dict):
        return {"enabled": bool(enabled), "applied": 0, "missing": 0}

    names = orientation.get("names") or []
    parents = orientation.get("parent_indices") or []
    rotations = orientation.get("local_rotations_xyzw") or []
    if not names or not rotations:
        return {"enabled": bool(enabled), "applied": 0, "missing": len(names)}

    matrices = global_orientation_matrices(names, parents, rotations)
    result = {"enabled": True, "applied": 0, "missing": 0, "armatures": []}
    previous_active = bpy.context.view_layer.objects.active
    previous_mode = bpy.context.object.mode if bpy.context.object is not None else "OBJECT"
    if previous_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    try:
        for armature in imported_armatures(imported_names):
            result["armatures"].append(armature.name)
            bpy.context.view_layer.objects.active = armature
            armature.select_set(True)
            bpy.ops.object.mode_set(mode="EDIT")
            name_to_index = {name: index for index, name in enumerate(names)}
            child_indices: dict[int, list[int]] = {index: [] for index in range(len(names))}
            for index, parent in enumerate(parents):
                if 0 <= parent < len(names):
                    child_indices.setdefault(parent, []).append(index)

            original_heads = {
                bone_name: edit_bone.head.copy()
                for bone_name, edit_bone in armature.data.edit_bones.items()
            }
            original_tails = {
                bone_name: edit_bone.tail.copy()
                for bone_name, edit_bone in armature.data.edit_bones.items()
            }
            directions: dict[int, Vector] = {}

            for index, bone_name in enumerate(names):
                head = original_heads.get(bone_name)
                if head is None:
                    continue
                candidates = []
                for child_index in child_indices.get(index, []):
                    if child_index >= len(names):
                        continue
                    child_head = original_heads.get(names[child_index])
                    if child_head is None:
                        continue
                    delta = child_head - head
                    if delta.length > 1e-6:
                        candidates.append(delta)
                if candidates:
                    directions[index] = max(candidates, key=lambda item: item.length).normalized()

            for index, bone_name in enumerate(names):
                edit_bone = armature.data.edit_bones.get(bone_name)
                if edit_bone is None or index >= len(matrices):
                    result["missing"] += 1
                    continue
                length = max(edit_bone.length, 0.004)
                head = edit_bone.head.copy()
                rotation = matrices[index].to_3x3().normalized()
                parent = parents[index] if index < len(parents) else len(names)
                if index in directions:
                    y_axis = directions[index]
                elif 0 <= parent < len(names) and parent in directions:
                    y_axis = directions[parent]
                else:
                    original_tail = original_tails.get(bone_name)
                    fallback = (original_tail - head) if original_tail is not None else Vector()
                    y_axis = fallback.normalized() if fallback.length > 1e-6 else (rotation @ Vector((0.0, 1.0, 0.0))).normalized()
                z_axis = (rotation @ Vector((0.0, 0.0, 1.0))).normalized()
                edit_bone.tail = head + y_axis * length
                edit_bone.align_roll(z_axis)
                result["applied"] += 1
            bpy.ops.object.mode_set(mode="OBJECT")
            for index, bone_name in enumerate(names):
                bone = armature.data.bones.get(bone_name)
                if bone is not None and index < len(rotations):
                    bone["g4_rest_rotation_xyzw"] = rotations[index]
    finally:
        if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        if previous_active is not None:
            bpy.context.view_layer.objects.active = previous_active
        if previous_active is not None and previous_mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode=previous_mode)
            except RuntimeError:
                pass
    return result


def compact_skeleton_summary(summary: dict) -> None:
    skeleton = summary.get("skeleton")
    if not isinstance(skeleton, dict):
        return
    for key in ("names", "parent_indices", "bind_matrices", "local_matrices"):
        skeleton.pop(key, None)


def make_report_text(summary: dict) -> None:
    model_name = Path(summary.get("resolved_model", "g4_model")).stem
    text = bpy.data.texts.new(f"G4 Import Report - {model_name}")
    text.write(json.dumps(summary, indent=2))


def make_debug_text(summary: dict, lines: list[str]) -> None:
    model_name = Path(summary.get("resolved_model", "g4_model")).stem
    text = bpy.data.texts.new(f"G4 Import Debug - {model_name}")
    text.write("\n".join(lines) + "\n")


def write_debug_log(summary: dict, lines: list[str]) -> None:
    dae = Path(summary.get("dae", ""))
    if not dae:
        return
    log_path = dae.with_suffix(".g4_import_debug.log")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary["debug_log"] = str(log_path)


def image_paths() -> set[str]:
    return {str(Path(bpy.path.abspath(image.filepath)).resolve()) for image in bpy.data.images if image.filepath}


def strip_texture_variant(name: str) -> str:
    lower = name.lower()
    lower = lower.removesuffix(".dds")
    lower = lower.removesuffix(".nxtch")
    lower = lower.removesuffix(".bin")
    return lower.rsplit(".", 1)[0] if "." in lower else lower


def texture_role(path: Path) -> str:
    stem = path.stem.lower()
    if stem.endswith("_a.1"):
        return "transparent_base"
    if stem.endswith(".a"):
        return "alpha_red"
    if stem.endswith("msk") or stem.endswith("_mask"):
        return "mask"
    if stem.endswith("nml") or stem.endswith("_normal"):
        return "normal"
    if stem.endswith("_re.2") or stem.endswith("_n.2") or stem.endswith("_nm.2"):
        return "normal"
    if stem.endswith(".2") or stem.endswith("_n") or stem.endswith("_nm") or "_normal" in stem:
        return "normal"
    if stem.endswith("spm"):
        return "specular_mask"
    if stem.endswith("sp"):
        return "specular"
    if stem.endswith("oc"):
        return "occlusion"
    if stem.endswith("line"):
        return "line"
    return "base"


def texture_base_key(path: Path) -> str:
    stem = path.stem.lower()
    role = texture_role(path)
    if role == "transparent_base" and stem.endswith("_a.1"):
        return stem[:-2].strip("_")
    if role == "normal" and stem.endswith(".2"):
        return stem[:-2].strip("_")
    if role == "normal" and stem.endswith("nml"):
        return re.sub(r"_?nml$", "", stem).strip("_")
    if role == "alpha_red" and stem.endswith(".a"):
        return stem[:-2].strip("_")
    if role == "mask":
        return re.sub(r"(?:_?msk|_mask)$", "", stem).strip("_")
    if role in {"specular_mask", "specular", "occlusion", "line"}:
        return re.sub(r"(?:spm|sp|oc|line)$", "", stem).strip("_")
    return strip_texture_variant(stem).strip("_")


def texture_base_keys(path: Path) -> list[str]:
    key = texture_base_key(path)
    keys = [key]
    if texture_role(path) == "alpha_red" and key and not key.endswith("_a"):
        keys.append(f"{key}_a")
    if key.endswith("_a"):
        keys.append(key[:-2])

    unique = []
    seen = set()
    for item in keys:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def normalized_material_key(name: str) -> str:
    key = blender_base_name(name).lower()
    key = key[:-1] if key.endswith("m") else key
    key = key.strip("_")
    return key


def material_variant_keys(material_name: str, diffuse_path: Path | None = None) -> list[str]:
    keys = []
    if diffuse_path is not None:
        keys.extend(texture_base_keys(diffuse_path))

    key = normalized_material_key(material_name)
    if key:
        keys.append(key)
        keys.append(re.sub(r"_of$", "", key).strip("_"))
        keys.append(re.sub(r"_of_", "_", key).strip("_"))
        keys.append(re.sub(r"_of.*$", "", key).strip("_"))
        if "_a_of" in key:
            keys.append(key.replace("_a_of", "_a").strip("_"))
        if key.endswith("_a"):
            keys.append(key[:-2])

    unique = []
    seen = set()
    for item in keys:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def load_image(path: Path):
    if not path.exists():
        return None
    for image in bpy.data.images:
        if image.filepath and Path(bpy.path.abspath(image.filepath)).resolve() == path.resolve():
            return image
    try:
        return bpy.data.images.load(str(path), check_existing=True)
    except RuntimeError:
        return None


def principled_node(nodes):
    for node in nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def node_input(node, *names: str):
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            return socket
    return None


def image_texture_node(nodes, image):
    for node in nodes:
        if node.type == "TEX_IMAGE" and node.image == image:
            return node
    node = nodes.new(type="ShaderNodeTexImage")
    node.image = image
    return node


def material_uses_image(material, path: Path) -> bool:
    tree = material.node_tree
    if tree is None:
        return False
    expected = path.resolve()
    for node in tree.nodes:
        if node.type != "TEX_IMAGE" or node.image is None or not node.image.filepath:
            continue
        if Path(bpy.path.abspath(node.image.filepath)).resolve() == expected:
            return True
    return False


def find_material(name: str, diffuse_path: Path | None = None):
    material = bpy.data.materials.get(name)
    if material is not None:
        return material
    for material in bpy.data.materials:
        if material.name == name or material.name.startswith(f"{name}."):
            return material
    if diffuse_path is not None:
        for material in bpy.data.materials:
            if material_uses_image(material, diffuse_path):
                return material
    return None


def first_variant_path(variants: dict[str, list[str] | str | Path], *roles: str) -> Path | None:
    for role in roles:
        value = variants.get(role)
        if not value:
            continue
        if isinstance(value, list):
            return Path(value[0]) if value else None
        return Path(value)
    return None


def variants_from_report(
    summary: dict, material_name: str, diffuse_path: Path, fallback: dict[str, Path]
) -> dict[str, list[str] | str | Path]:
    report_variants = summary.get("texture_variants") or {}
    merged: dict[str, list[str] | str | Path] = {}
    for key in material_variant_keys(material_name, diffuse_path):
        roles = report_variants.get(key)
        if roles:
            merged.update(roles)
    for role, path in fallback.items():
        merged.setdefault(role, path)
    return merged


def clear_input_links(links, socket) -> None:
    for link in list(socket.links):
        links.remove(link)


def link_to_input(links, output, input_socket) -> None:
    if input_socket is None:
        return
    clear_input_links(links, input_socket)
    links.new(output, input_socket)


def set_transparent_material(material) -> None:
    material.blend_method = "BLEND"
    material.use_screen_refraction = False
    material.show_transparent_back = True


def base_color_texture_node(material, principled):
    base_input = node_input(principled, "Base Color")
    if base_input is None:
        return None
    for link in base_input.links:
        node = link.from_node
        if node.type == "TEX_IMAGE" and node.image is not None:
            return node

    tree = material.node_tree
    if tree is None:
        return None
    image_nodes = [node for node in tree.nodes if node.type == "TEX_IMAGE" and node.image is not None]
    if len(image_nodes) == 1:
        return image_nodes[0]
    return None


def connect_base_texture_alpha(material, principled) -> bool:
    tree = material.node_tree
    if tree is None:
        return False
    alpha_input = node_input(principled, "Alpha")
    if alpha_input is None:
        return False
    node = base_color_texture_node(material, principled)
    if node is None:
        return False
    alpha_output = node.outputs.get("Alpha")
    if alpha_output is None:
        return False
    link_to_input(tree.links, alpha_output, alpha_input)
    set_transparent_material(material)
    return True


def material_base_image_path(material) -> Path | None:
    tree = material.node_tree
    if tree is None:
        return None
    principled = principled_node(tree.nodes)
    if principled is None:
        return None
    node = base_color_texture_node(material, principled)
    if node is None or node.image is None or not node.image.filepath:
        return None
    return Path(bpy.path.abspath(node.image.filepath)).resolve()


def name_implies_alpha(material_name: str, diffuse_path: Path) -> bool:
    text = f"{material_name}_{diffuse_path.stem}".lower()
    return (
        "_a_" in text
        or text.endswith("_a")
        or "_a." in text
        or "_of_" in text
        or "_of" in text
        or "alpha" in text
    )


def build_texture_index(paths: list[Path]) -> dict[str, dict[str, Path]]:
    by_key: dict[str, dict[str, Path]] = {}
    for texture in paths:
        for key in texture_base_keys(texture):
            by_key.setdefault(key, {})[texture_role(texture)] = texture
    return by_key


def all_available_texture_paths(summary: dict) -> list[Path]:
    paths = [Path(path) for path in summary.get("textures", [])]
    for path in list(paths):
        parent = path.parent
        if parent.exists():
            paths.extend(sorted(parent.glob("*.dds")))
    unique = []
    seen = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def material_variants_from_index(material_name: str, base_path: Path, by_key: dict[str, dict[str, Path]]) -> dict[str, Path]:
    variants: dict[str, Path] = {}
    for key in material_variant_keys(material_name, base_path):
        variants.update(by_key.get(key, {}))
    return variants


def connect_alpha_mask(material, principled, mask_path: Path, debug: list[str] | None = None) -> bool:
    image = load_image(mask_path)
    alpha_input = node_input(principled, "Alpha")
    if image is None or alpha_input is None or material.node_tree is None:
        if debug is not None:
            debug.append(
                f"[connect] {material.name}: failed alpha mask {mask_path.name} "
                f"image={image is not None} input={alpha_input is not None} tree={material.node_tree is not None}"
            )
        return False
    image.colorspace_settings.name = "Non-Color"
    tex = image_texture_node(material.node_tree.nodes, image)
    sep = material.node_tree.nodes.new(type="ShaderNodeSeparateColor")
    material.node_tree.links.new(tex.outputs["Color"], sep.inputs["Color"])
    link_to_input(material.node_tree.links, sep.outputs["Red"], alpha_input)
    set_transparent_material(material)
    if debug is not None:
        debug.append(f"[connect] {material.name}: linked alpha mask {mask_path.name}")
    return True


def connect_normal_map(material, principled, normal_path: Path, debug: list[str] | None = None) -> bool:
    image = load_image(normal_path)
    normal_input = node_input(principled, "Normal")
    if image is None or normal_input is None or material.node_tree is None:
        if debug is not None:
            debug.append(
                f"[connect] {material.name}: failed normal {normal_path.name} "
                f"image={image is not None} input={normal_input is not None} tree={material.node_tree is not None}"
            )
        return False
    image.colorspace_settings.name = "Non-Color"
    tex = image_texture_node(material.node_tree.nodes, image)
    normal = material.node_tree.nodes.new(type="ShaderNodeNormalMap")
    material.node_tree.links.new(tex.outputs["Color"], normal.inputs["Color"])
    link_to_input(material.node_tree.links, normal.outputs["Normal"], normal_input)
    if debug is not None:
        debug.append(f"[connect] {material.name}: linked normal {normal_path.name}")
    return True


def apply_level5_toon_shader(
    material,
    base_path: Path,
    variants: dict[str, Path],
    debug: list[str] | None = None,
) -> bool:
    base_image = load_image(base_path)
    if base_image is None or material.node_tree is None:
        return False
    tree = material.node_tree
    nodes = tree.nodes
    links = tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (1040, 120)
    base = nodes.new("ShaderNodeTexImage")
    base.name = "G4 Base"
    base.label = base_path.name
    base.image = base_image
    base.interpolation = "Linear"
    base.location = (-980, 280)

    saturation = nodes.new("ShaderNodeHueSaturation")
    saturation.name = "G4 Saturation"
    saturation.location = (-760, 280)
    # The source albedo is already authored at the in-game saturation.  The
    # previous boost clipped reds and removed the pastel Level-5 palette.
    saturation.inputs["Saturation"].default_value = 1.0
    links.new(base.outputs["Color"], saturation.inputs["Color"])
    base_color = saturation.outputs["Color"]

    mask_path = first_variant_path(variants, "mask")
    if mask_path is not None:
        image = load_image(mask_path)
        if image is not None:
            image.colorspace_settings.name = "Non-Color"
            mask = nodes.new("ShaderNodeTexImage")
            mask.name = "G4 Recolor Mask"
            mask.image = image
            mask.location = (-980, 580)
            channels = nodes.new("ShaderNodeSeparateColor")
            channels.location = (-760, 580)
            links.new(mask.outputs["Color"], channels.inputs["Color"])
            for index, channel in enumerate(("Red", "Green", "Blue")):
                tint = nodes.new("ShaderNodeMixRGB")
                tint.name = f"G4 Mask {channel} Tint"
                tint.label = f"G4 Mask {channel} Tint"
                tint.blend_type = "MULTIPLY"
                tint.location = (-520 + index * 180, 430)
                tint.inputs[2].default_value = (1.0, 1.0, 1.0, 1.0)
                links.new(channels.outputs[channel], tint.inputs[0])
                links.new(base_color, tint.inputs[1])
                base_color = tint.outputs["Color"]

    diffuse = nodes.new("ShaderNodeBsdfDiffuse")
    diffuse.location = (-720, -100)
    diffuse.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    diffuse.inputs["Roughness"].default_value = 0.0
    diffuse_rgb = nodes.new("ShaderNodeShaderToRGB")
    diffuse_rgb.location = (-520, -100)
    diffuse_bw = nodes.new("ShaderNodeRGBToBW")
    diffuse_bw.location = (-340, -100)
    light_ramp = nodes.new("ShaderNodeValToRGB")
    light_ramp.name = "G4 Toon Light"
    light_ramp.location = (-150, -100)
    light_ramp.color_ramp.interpolation = "CONSTANT"
    light_ramp.color_ramp.elements[0].position = 0.0
    light_ramp.color_ramp.elements[0].color = (0.66, 0.61, 0.72, 1.0)
    light_ramp.color_ramp.elements[1].position = 0.56
    light_ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
    links.new(diffuse.outputs["BSDF"], diffuse_rgb.inputs["Shader"])
    links.new(diffuse_rgb.outputs["Color"], diffuse_bw.inputs["Color"])
    links.new(diffuse_bw.outputs["Val"], light_ramp.inputs["Fac"])

    lit = nodes.new("ShaderNodeMixRGB")
    lit.name = "G4 Cel Diffuse"
    lit.blend_type = "MULTIPLY"
    lit.inputs[0].default_value = 1.0
    lit.location = (60, 250)
    links.new(base_color, lit.inputs[1])
    links.new(light_ramp.outputs["Color"], lit.inputs[2])
    color_output = lit.outputs["Color"]

    occlusion_path = first_variant_path(variants, "occlusion")
    if occlusion_path is not None:
        image = load_image(occlusion_path)
        if image is not None:
            image.colorspace_settings.name = "Non-Color"
            occlusion = nodes.new("ShaderNodeTexImage")
            occlusion.name = "G4 Occlusion"
            occlusion.image = image
            occlusion.location = (-700, 520)
            occlusion_bw = nodes.new("ShaderNodeRGBToBW")
            occlusion_bw.location = (-500, 520)
            occlusion_ramp = nodes.new("ShaderNodeValToRGB")
            occlusion_ramp.location = (-310, 520)
            occlusion_ramp.color_ramp.interpolation = "CONSTANT"
            occlusion_ramp.color_ramp.elements[0].color = (0.78, 0.78, 0.78, 1.0)
            occlusion_ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
            occlusion_ramp.color_ramp.elements[1].position = 0.5
            ao_multiply = nodes.new("ShaderNodeMixRGB")
            ao_multiply.name = "G4 Shadow Map"
            ao_multiply.blend_type = "MULTIPLY"
            ao_multiply.inputs[0].default_value = 1.0
            ao_multiply.location = (250, 280)
            links.new(occlusion.outputs["Color"], occlusion_bw.inputs["Color"])
            links.new(occlusion_bw.outputs["Val"], occlusion_ramp.inputs["Fac"])
            links.new(color_output, ao_multiply.inputs[1])
            links.new(occlusion_ramp.outputs["Color"], ao_multiply.inputs[2])
            color_output = ao_multiply.outputs["Color"]

    line_path = first_variant_path(variants, "line")
    if line_path is not None:
        image = load_image(line_path)
        if image is not None:
            line_texture = nodes.new("ShaderNodeTexImage")
            line_texture.name = "G4 Line Parameter"
            line_texture.image = image
            line_texture.location = (-180, -520)

    specular_mask_path = first_variant_path(variants, "specular_mask")
    specular_path = first_variant_path(variants, "specular")
    if specular_mask_path is not None:
        image = load_image(specular_mask_path)
        if image is not None:
            image.colorspace_settings.name = "Non-Color"
            specular_mask = nodes.new("ShaderNodeTexImage")
            specular_mask.name = "G4 Specular Mask"
            specular_mask.image = image
            specular_mask.location = (-420, -760)
            glossy = nodes.new("ShaderNodeBsdfGlossy")
            glossy.location = (-200, -780)
            glossy.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
            glossy.inputs["Roughness"].default_value = 0.28
            glossy_rgb = nodes.new("ShaderNodeShaderToRGB")
            glossy_rgb.location = (0, -780)
            glossy_bw = nodes.new("ShaderNodeRGBToBW")
            glossy_bw.location = (170, -780)
            glossy_ramp = nodes.new("ShaderNodeValToRGB")
            glossy_ramp.location = (340, -780)
            glossy_ramp.color_ramp.interpolation = "CONSTANT"
            glossy_ramp.color_ramp.elements[0].position = 0.0
            glossy_ramp.color_ramp.elements[0].color = (0.0, 0.0, 0.0, 1.0)
            glossy_ramp.color_ramp.elements[1].position = 0.82
            glossy_ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
            links.new(glossy_rgb.outputs["Color"], glossy_bw.inputs["Color"])
            links.new(glossy_bw.outputs["Val"], glossy_ramp.inputs["Fac"])
            specular_output = glossy_ramp.outputs["Color"]
            if specular_path is not None:
                specular_image = load_image(specular_path)
                if specular_image is not None:
                    specular_image.colorspace_settings.name = "Non-Color"
                    coordinates = nodes.new("ShaderNodeTexCoord")
                    coordinates.location = (-620, -980)
                    specular_shape = nodes.new("ShaderNodeTexImage")
                    specular_shape.name = "G4 Specular Shape"
                    specular_shape.image = specular_image
                    specular_shape.projection = "SPHERE"
                    specular_shape.location = (-420, -980)
                    shape_multiply = nodes.new("ShaderNodeMixRGB")
                    shape_multiply.blend_type = "MULTIPLY"
                    shape_multiply.inputs[0].default_value = 1.0
                    shape_multiply.location = (20, -940)
                    links.new(coordinates.outputs["Normal"], specular_shape.inputs["Vector"])
                    links.new(specular_output, shape_multiply.inputs[1])
                    links.new(specular_shape.outputs["Color"], shape_multiply.inputs[2])
                    specular_output = shape_multiply.outputs["Color"]
            spec_multiply = nodes.new("ShaderNodeMixRGB")
            spec_multiply.blend_type = "MULTIPLY"
            spec_multiply.inputs[0].default_value = 1.0
            spec_multiply.location = (230, -700)
            spec_add = nodes.new("ShaderNodeMixRGB")
            spec_add.blend_type = "ADD"
            spec_add.inputs[0].default_value = 0.12
            spec_add.location = (650, 160)
            links.new(glossy.outputs["BSDF"], glossy_rgb.inputs["Shader"])
            links.new(specular_output, spec_multiply.inputs[1])
            links.new(specular_mask.outputs["Color"], spec_multiply.inputs[2])
            links.new(color_output, spec_add.inputs[1])
            links.new(spec_multiply.outputs["Color"], spec_add.inputs[2])
            color_output = spec_add.outputs["Color"]

    emission = nodes.new("ShaderNodeEmission")
    emission.location = (820, 180)
    emission.inputs["Strength"].default_value = 1.0
    links.new(color_output, emission.inputs["Color"])
    alpha_output = base.outputs.get("Alpha")
    alpha_path = first_variant_path(variants, "alpha_red")
    if alpha_path is not None:
        image = load_image(alpha_path)
        if image is not None:
            image.colorspace_settings.name = "Non-Color"
            alpha_texture = nodes.new("ShaderNodeTexImage")
            alpha_texture.image = image
            alpha_texture.location = (420, 600)
            alpha_separate = nodes.new("ShaderNodeSeparateColor")
            alpha_separate.location = (610, 600)
            links.new(alpha_texture.outputs["Color"], alpha_separate.inputs["Color"])
            alpha_output = alpha_separate.outputs["Red"]
    if alpha_output is not None and (alpha_path is not None or name_implies_alpha(material.name, base_path)):
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        transparent.location = (820, 20)
        mix_shader = nodes.new("ShaderNodeMixShader")
        mix_shader.location = (1020, 100)
        links.new(alpha_output, mix_shader.inputs[0])
        links.new(transparent.outputs["BSDF"], mix_shader.inputs[1])
        links.new(emission.outputs["Emission"], mix_shader.inputs[2])
        links.new(mix_shader.outputs["Shader"], output.inputs["Surface"])
        set_transparent_material(material)
    else:
        links.new(emission.outputs["Emission"], output.inputs["Surface"])
    material["g4_level5_toon"] = True
    material["g4_base_texture"] = str(base_path)
    material["g4_recolor_mask"] = str(mask_path) if mask_path is not None else ""
    if debug is not None:
        debug.append(
            f"[toon] {material.name}: oc={occlusion_path and occlusion_path.name} "
            f"line={line_path and line_path.name} sp={specular_path and specular_path.name} "
            f"spm={specular_mask_path and specular_mask_path.name}"
        )
    return True


def apply_material_texture_variants(summary: dict, debug: list[str] | None = None) -> None:
    textures = [Path(path) for path in summary.get("textures", [])]
    if not textures:
        if debug is not None:
            debug.append("[summary-pass] no textures in exporter summary")
        return

    by_key = build_texture_index(textures)
    if debug is not None:
        debug.append(f"[summary-pass] textures={len(textures)} keys={len(by_key)} materials={len(summary.get('materials', {}))}")

    for material_name, diffuse_path_text in summary.get("materials", {}).items():
        if not diffuse_path_text:
            if debug is not None:
                debug.append(f"[summary-pass] {material_name}: no diffuse path in summary")
            continue
        diffuse_path = Path(diffuse_path_text)
        material = find_material(material_name, diffuse_path)
        fallback_variants = {}
        for key in material_variant_keys(material_name, diffuse_path):
            fallback_variants.update(by_key.get(key, {}))
        variants = variants_from_report(summary, material_name, diffuse_path, fallback_variants)
        if material is None:
            if debug is not None:
                debug.append(
                    f"[summary-pass] {material_name}: material not found; diffuse={diffuse_path.name} "
                    f"keys={material_variant_keys(material_name, diffuse_path)} variants={list(variants)}"
                )
            continue
        if debug is not None:
            debug.append(
                f"[summary-pass] {material.name}: diffuse={diffuse_path.name} "
                f"keys={material_variant_keys(material_name, diffuse_path)} variants={list(variants)}"
            )

        material.use_nodes = True
        tree = material.node_tree
        if tree is None:
            continue
        nodes = tree.nodes
        links = tree.links
        principled = principled_node(nodes)
        if principled is None:
            continue

        base_image = load_image(diffuse_path)
        base_input = node_input(principled, "Base Color")
        if base_image is not None and base_input is not None:
            tex = image_texture_node(nodes, base_image)
            link_to_input(links, tex.outputs["Color"], base_input)
            if debug is not None:
                debug.append(f"[summary-pass] {material.name}: linked base {diffuse_path.name}")
        elif debug is not None:
            debug.append(f"[summary-pass] {material.name}: skipped base image={base_image is not None} input={base_input is not None}")

        alpha_path = first_variant_path(variants, "alpha_red")
        if alpha_path is None and name_implies_alpha(material_name, diffuse_path):
            alpha_path = first_variant_path(variants, "mask")
        if alpha_path is not None:
            image = load_image(alpha_path)
            alpha_input = node_input(principled, "Alpha")
            if image is not None and alpha_input is not None:
                image.colorspace_settings.name = "Non-Color"
                tex = image_texture_node(nodes, image)
                sep = nodes.new(type="ShaderNodeSeparateColor")
                links.new(tex.outputs["Color"], sep.inputs["Color"])
                link_to_input(links, sep.outputs["Red"], alpha_input)
                set_transparent_material(material)
                if debug is not None:
                    debug.append(f"[summary-pass] {material.name}: linked alpha mask {alpha_path.name}")
            elif debug is not None:
                debug.append(
                    f"[summary-pass] {material.name}: failed alpha mask {alpha_path.name} "
                    f"image={image is not None} input={alpha_input is not None}"
                )
        elif texture_role(diffuse_path) == "transparent_base":
            image = load_image(diffuse_path)
            alpha_input = node_input(principled, "Alpha")
            if image is not None and alpha_input is not None:
                tex = image_texture_node(nodes, image)
                link_to_input(links, tex.outputs["Alpha"], alpha_input)
                set_transparent_material(material)
                if debug is not None:
                    debug.append(f"[summary-pass] {material.name}: linked base alpha {diffuse_path.name}")
        elif name_implies_alpha(material_name, diffuse_path):
            if not connect_base_texture_alpha(material, principled):
                image = load_image(diffuse_path)
                alpha_input = node_input(principled, "Alpha")
                if image is not None and alpha_input is not None:
                    tex = image_texture_node(nodes, image)
                    link_to_input(links, tex.outputs["Alpha"], alpha_input)
                    set_transparent_material(material)
                    if debug is not None:
                        debug.append(f"[summary-pass] {material.name}: linked implied base alpha {diffuse_path.name}")

        normal_path = first_variant_path(variants, "normal", "normal_or_packed")
        if normal_path is not None:
            image = load_image(normal_path)
            normal_input = node_input(principled, "Normal")
            if image is not None and normal_input is not None:
                image.colorspace_settings.name = "Non-Color"
                tex = image_texture_node(nodes, image)
                normal = nodes.new(type="ShaderNodeNormalMap")
                links.new(tex.outputs["Color"], normal.inputs["Color"])
                link_to_input(links, normal.outputs["Normal"], normal_input)
                if debug is not None:
                    debug.append(f"[summary-pass] {material.name}: linked normal {normal_path.name}")
            elif debug is not None:
                debug.append(
                    f"[summary-pass] {material.name}: failed normal {normal_path.name} "
                    f"image={image is not None} input={normal_input is not None}"
                )


def apply_auxiliary_textures_to_imported_materials(imported_names: set[str], summary: dict, debug: list[str] | None = None) -> None:
    materials = []
    seen = set()
    for object_name in imported_names:
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is not None and material.name not in seen:
                seen.add(material.name)
                materials.append(material)

    available = all_available_texture_paths(summary)
    by_key = build_texture_index(available)
    if debug is not None:
        debug.append(
            f"[post-pass] imported_objects={len(imported_names)} materials={len(materials)} "
            f"available_textures={len(available)} keys={len(by_key)}"
        )
        for key in sorted(by_key)[:200]:
            roles = ", ".join(f"{role}:{path.name}" for role, path in sorted(by_key[key].items()))
            debug.append(f"[texture-key] {key} -> {roles}")
    for material in materials:
        material.use_nodes = True
        tree = material.node_tree
        if tree is None:
            if debug is not None:
                debug.append(f"[post-pass] {material.name}: no node tree")
            continue
        principled = principled_node(tree.nodes)
        if principled is None:
            if debug is not None:
                debug.append(f"[post-pass] {material.name}: no principled node; nodes={[node.type for node in tree.nodes]}")
            continue
        base_path = material_base_image_path(material)
        if base_path is None:
            if debug is not None:
                image_nodes = [
                    node.image.name if node.type == "TEX_IMAGE" and node.image is not None else node.type
                    for node in tree.nodes
                ]
                debug.append(f"[post-pass] {material.name}: no base image path; nodes={image_nodes}")
            continue
        variants = material_variants_from_index(material.name, base_path, by_key)
        keys = material_variant_keys(material.name, base_path)
        if debug is not None:
            debug.append(
                f"[post-pass] {material.name}: base={base_path.name} keys={keys} "
                f"variants={{{', '.join(f'{role}:{path.name}' for role, path in sorted(variants.items()))}}} "
                f"implies_alpha={name_implies_alpha(material.name, base_path)}"
            )
        alpha_path = first_variant_path(variants, "alpha_red")
        if alpha_path is None and name_implies_alpha(material.name, base_path):
            alpha_path = first_variant_path(variants, "mask")
        if alpha_path is not None:
            connect_alpha_mask(material, principled, alpha_path, debug)
        elif name_implies_alpha(material.name, base_path):
            linked = connect_base_texture_alpha(material, principled)
            if debug is not None:
                debug.append(f"[post-pass] {material.name}: fallback base alpha linked={linked}")
        elif debug is not None:
            debug.append(f"[post-pass] {material.name}: no alpha candidate")

        normal_path = first_variant_path(variants, "normal", "normal_or_packed")
        if normal_path is not None:
            connect_normal_map(material, principled, normal_path, debug)
        elif debug is not None:
            debug.append(f"[post-pass] {material.name}: no normal candidate")
        apply_level5_toon_shader(material, base_path, variants, debug)


def configure_level5_outlines(debug: list[str] | None = None) -> bool:
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    try:
        scene.render.use_freestyle = True
        settings = view_layer.freestyle_settings
        for stale in tuple(settings.linesets):
            if stale.linestyle is None:
                settings.linesets.remove(stale)
        line_set = settings.linesets.get("Level-5 G4 Outline")
        if line_set is None or line_set.linestyle is None:
            if line_set is not None:
                settings.linesets.remove(line_set)
            bpy.ops.scene.freestyle_lineset_add()
            line_set = settings.linesets.active
            line_set.name = "Level-5 G4 Outline"
        line_style = line_set.linestyle
        line_style.name = "Level-5 G4 Outline"
        line_set.select_silhouette = True
        line_set.select_border = True
        line_set.select_crease = True
        line_set.select_material_boundary = True
        line_set.select_contour = True
        line_set.edge_type_combination = "OR"
        settings.crease_angle = math.radians(55.0)
        line_style.color = (0.018, 0.012, 0.018)
        line_style.thickness = 1.15
        line_style.caps = "ROUND"
    except (AttributeError, RuntimeError, TypeError):
        if debug is not None:
            debug.append("[outline] Freestyle is unavailable for the active render configuration")
        return False
    if debug is not None:
        debug.append("[outline] depth/normal approximation enabled with silhouette, crease and material edges")
    return True


def configure_viewport_outlines(imported_names: set[str], debug: list[str] | None = None) -> int:
    """Add a back-face hull so silhouettes are visible outside Freestyle renders."""
    material = bpy.data.materials.get("Level-5 G4 Viewport Outline")
    if material is None:
        material = bpy.data.materials.new("Level-5 G4 Viewport Outline")
        material.use_nodes = True
        material.diffuse_color = (0.012, 0.006, 0.012, 1.0)
        material.use_backface_culling = True
        nodes = material.node_tree.nodes
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        emission = nodes.new("ShaderNodeEmission")
        emission.inputs["Color"].default_value = (0.012, 0.006, 0.012, 1.0)
        emission.inputs["Strength"].default_value = 1.0
        material.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])

    configured = 0
    for name in imported_names:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH" or not obj.data.polygons:
            continue
        slot = obj.data.materials.get(material.name)
        if slot is None:
            obj.data.materials.append(material)
        material_index = next(
            (index for index, item in enumerate(obj.data.materials) if item == material),
            len(obj.data.materials) - 1,
        )
        modifier = obj.modifiers.get("Level-5 G4 Viewport Outline")
        if modifier is None:
            modifier = obj.modifiers.new("Level-5 G4 Viewport Outline", "SOLIDIFY")
        modifier.thickness = 0.0007
        modifier.offset = 1.0
        modifier.use_flip_normals = True
        modifier.use_rim = False
        modifier.material_offset = material_index
        modifier.show_in_editmode = False
        obj["g4_viewport_outline"] = True
        configured += 1
    if debug is not None:
        debug.append(f"[outline] viewport back-face hulls={configured}")
    return configured


def pack_images(paths_before: set[str], texture_paths: set[str], enabled: bool) -> None:
    if not enabled:
        return
    for image in bpy.data.images:
        filepath = bpy.path.abspath(image.filepath) if image.filepath else ""
        filepath = str(Path(filepath).resolve()) if filepath else ""
        if filepath in paths_before and filepath not in texture_paths:
            continue
        if not filepath or not Path(filepath).exists():
            continue
        try:
            if not image.has_data:
                image.reload()
            image.pack()
        except RuntimeError:
            continue


def cleanup_generated_files(summary: dict, enabled: bool) -> None:
    if not enabled:
        return
    dae = Path(summary.get("dae", ""))
    report = Path(summary.get("report", ""))
    for file_path in (dae, report):
        if file_path.exists():
            file_path.unlink()

    texture_dirs = {Path(path).parent for path in summary.get("textures", [])}
    for directory in sorted(texture_dirs, key=lambda item: len(item.parts), reverse=True):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)


def discard_secondary_lods(imported_names: set[str]) -> int:
    removed = 0
    for object_name in tuple(imported_names):
        obj = bpy.data.objects.get(object_name)
        if obj is None or obj.type != "MESH" or not re.search(r"_LOD[1-9](?:\.\d+)?$", obj.name, re.IGNORECASE):
            continue
        imported_names.discard(object_name)
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1
    return removed


def import_g4_model(
    path: Path,
    prefs: G4ImporterPreferences,
    create_report_text: bool,
    target_armature=None,
) -> tuple[dict, set[str]]:
    debug = [f"[import] input={path}"]
    if target_armature is not None and target_armature.type == "ARMATURE":
        debug.append(
            f"[target-armature] name={target_armature.name} "
            f"bones={len(target_armature.data.bones)} "
            f"first={list(target_armature.data.bones.keys())[:16]}"
        )
    images_before = image_paths()
    summary = run_exporter(str(path), prefs, target_armature)
    debug.append(f"[exporter] resolved={summary.get('resolved_model')}")
    debug.append(f"[exporter] dae={summary.get('dae')}")
    debug.append(f"[exporter] skeleton_source={summary.get('skeleton_source')}")
    debug.append(f"[exporter] textures={len(summary.get('textures', []))} materials={len(summary.get('materials', {}))}")
    debug.append(f"[exporter] texture_sources={summary.get('texture_sources', [])}")
    dae_path = Path(summary["dae"]).resolve()
    if not dae_path.exists():
        raise RuntimeError(f"Generated DAE not found: {dae_path}")
    imported_names = import_collada(dae_path)
    removed_lods = discard_secondary_lods(imported_names)
    debug.append(f"[collada] imported_objects={sorted(imported_names)}")
    debug.append(f"[collada] secondary_lods_removed={removed_lods}")
    orientation = apply_g4_bone_orientation(
        imported_names,
        summary,
        getattr(prefs, "apply_bone_orientation", True),
    )
    summary["bone_orientation_import"] = orientation
    debug.append(f"[armature] bone_orientation={orientation}")
    compact_skeleton_summary(summary)
    collapse_duplicate_materials(imported_names)
    apply_material_texture_variants(summary, debug)
    apply_auxiliary_textures_to_imported_materials(imported_names, summary, debug)
    configure_level5_outlines(debug)
    configure_viewport_outlines(imported_names, debug)
    texture_paths = {str(Path(path).resolve()) for path in summary.get("textures", [])}
    pack_images(images_before, texture_paths, getattr(prefs, "pack_imported_textures", True))
    write_debug_log(summary, debug)
    if create_report_text:
        make_report_text(summary)
        make_debug_text(summary, debug)
    cleanup_generated_files(summary, getattr(prefs, "cleanup_import_cache", True))
    return summary, imported_names


def model_data_roots(path: Path, prefs: G4ImporterPreferences) -> list[Path]:
    roots = []
    configured = bpy.path.abspath(getattr(prefs, "raw_data_root", "") or "")
    if configured:
        roots.append(Path(configured))
    for parent in path.parents:
        if parent.name == "data" and parent.parent.name in {"raw", "readable"}:
            roots.append(parent)
            work_root = parent.parent.parent
            roots.extend((work_root / "raw" / "data", work_root / "readable" / "data"))
            break
    return list(dict.fromkeys(root.resolve() for root in roots if root.is_dir()))


def load_model_lookup(prefs: G4ImporterPreferences) -> dict:
    lookup_path = Path(bpy.path.abspath(getattr(prefs, "chara_model_lookup", "") or ""))
    if not lookup_path.is_file():
        return {}
    try:
        return json.loads(lookup_path.read_text(encoding="utf-8")).get("models") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def body_stem_from_row(row: dict | None) -> str | None:
    body_path = str((row or {}).get("body_path") or "").replace("\\", "/")
    match = re.search(r"(?:^|/)(c\d{6,8})(?:\.objbin)?$", body_path, re.IGNORECASE)
    return match.group(1).lower() if match else None


def lookup_body_stem_for_identifier(identifier: str, models: dict) -> str | None:
    identifier = identifier.lower()
    rows = (
        row
        for model_path, row in models.items()
        if Path(model_path).stem.lower() == identifier
    )
    return next((stem for row in rows if (stem := body_stem_from_row(row))), None)


def lookup_body_stem(path: Path, prefs: G4ImporterPreferences, models: dict | None = None) -> str | None:
    models = load_model_lookup(prefs) if models is None else models
    relative = None
    parts = path.parts
    for index in range(len(parts) - 2):
        if parts[index:index + 2] == ("common", "chr"):
            relative = Path(*parts[index + 2:]).as_posix()
            break
    row = None
    if relative:
        row = models.get(relative)
        if row is None:
            row = models.get(Path(relative).with_suffix(".objbin").as_posix())
    return body_stem_from_row(row)


def find_character_part(
    path: Path,
    prefix: str,
    prefs: G4ImporterPreferences,
    character_part_stem: str = "",
) -> Path | None:
    match = re.fullmatch(r"c(\d{6,8})", path.stem, re.IGNORECASE)
    if match is None:
        return None
    stems = []
    models = load_model_lookup(prefs)
    override = re.fullmatch(r"c(\d{6,8})", character_part_stem, re.IGNORECASE)
    if override:
        override_body = lookup_body_stem_for_identifier(override.group(0), models)
        if override_body:
            stems.append(f"{prefix}{override_body[1:]}")
        stems.append(f"{prefix}{override.group(1)}")
    stems.append(f"{prefix}{match.group(1)}")
    body_stem = lookup_body_stem(path, prefs, models)
    if body_stem:
        stems.append(f"{prefix}{body_stem[1:]}")
    for data_root in model_data_roots(path, prefs):
        uniform_root = data_root / "common" / "chr" / "_uniform"
        for stem in dict.fromkeys(stems):
            for extension in (".g4pkm", ".g4md"):
                candidate = uniform_root / stem / f"{stem}{extension}"
                if candidate.is_file():
                    return candidate
    return None


def attach_part_to_armature(
    path: Path,
    target_armature,
    prefs: G4ImporterPreferences,
    create_report_text: bool,
    preserve_part_armatures: bool = False,
) -> int:
    _, imported_names = import_g4_model(
        path,
        prefs,
        create_report_text,
        target_armature=target_armature,
    )
    imported_objects = [bpy.data.objects.get(name) for name in imported_names]
    imported_objects = [obj for obj in imported_objects if obj is not None]
    part_armatures = {obj for obj in imported_objects if obj.type == "ARMATURE" and obj != target_armature}

    attached = 0
    for obj in imported_objects:
        if obj.type != "MESH":
            continue
        armature_modifiers = [modifier for modifier in obj.modifiers if modifier.type == "ARMATURE"]
        if not armature_modifiers:
            raise RuntimeError(f"Character part mesh has no armature modifier: {path}::{obj.name}")
        for modifier in armature_modifiers:
            if modifier.object in part_armatures:
                modifier.object = target_armature
        if not any(modifier.object == target_armature for modifier in armature_modifiers):
            raise RuntimeError(f"Character part mesh could not be bound to {target_armature.name}: {path}::{obj.name}")
        if obj.parent in part_armatures:
            obj.parent = target_armature
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_basis = Matrix.Identity(4)
        obj["g4_character_part_source"] = str(path)
        obj["g4_character_part_group_remaps"] = 0
        obj["g4_character_part_actor"] = target_armature.name
        obj["g4_character_part_preserve_armature"] = False
        attached += 1

    for source_armature in part_armatures:
        bpy.data.objects.remove(source_armature, do_unlink=True)
    return attached


def import_character_parts_for_armature(
    model_path: Path,
    target_armature,
    prefs: G4ImporterPreferences,
    automatic: bool,
    body_path: str,
    shoes_path: str,
    create_report_text: bool,
    character_part_stem: str = "",
    preserve_part_armatures: bool = False,
) -> tuple[int, list[Path]]:
    if automatic:
        paths = [
            find_character_part(model_path, "u", prefs, character_part_stem),
            find_character_part(model_path, "s", prefs, character_part_stem),
        ]
    else:
        paths = [Path(bpy.path.abspath(value)) if value else None for value in (body_path, shoes_path)]
    selected = []
    for path in paths:
        if path is None:
            continue
        if not path.is_file() or path.suffix.lower() not in MODEL_EXTENSIONS:
            raise RuntimeError(f"Character part not found or unsupported: {path}")
        selected.append(path)
    attached = sum(
        attach_part_to_armature(
            path,
            target_armature,
            prefs,
            create_report_text,
            preserve_part_armatures=preserve_part_armatures,
        )
        for path in selected
    )
    return attached, selected


class IMPORT_OT_level5_g4(Operator, ImportHelper):
    bl_idname = "import_scene.level5_g4"
    bl_label = "Import Level-5 G4 Model"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".g4md"
    filter_glob: StringProperty(
        default="*.g4md;*.g4pkm",
        options={"HIDDEN"},
    )
    files: CollectionProperty(
        type=OperatorFileListElement,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    directory: StringProperty(
        subtype="DIR_PATH",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    create_report_text: BoolProperty(
        name="Create Report Text",
        default=True,
        description="Create a Blender text block with the exporter summary",
    )
    import_character_parts: BoolProperty(
        name="Import Body and Shoes",
        default=True,
        description="Attach manually selected u* body and s* shoes to the character rig",
    )
    auto_character_parts: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    body_model: StringProperty(
        name="Body Model",
        subtype="FILE_PATH",
        description="Optional u*.g4md/.g4pkm body selected manually",
    )
    shoes_model: StringProperty(
        name="Shoes Model",
        subtype="FILE_PATH",
        description="Optional s*.g4md/.g4pkm shoes selected manually",
    )
    character_part_stem: StringProperty(
        options={"HIDDEN", "SKIP_SAVE"},
    )
    preserve_character_part_armatures: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    def invoke(self, context, event):
        self.auto_character_parts = False
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "create_report_text")
        is_character = bool(re.fullmatch(r"c\d{6,8}", Path(self.filepath).stem, re.IGNORECASE))
        if is_character:
            layout.prop(self, "import_character_parts")
        if is_character and self.import_character_parts:
            layout.prop(self, "body_model")
            layout.prop(self, "shoes_model")

    def execute(self, context):
        base_path = Path(self.filepath)
        if self.files:
            directory = Path(getattr(self, "directory", "") or base_path.parent)
            paths = [directory / item.name for item in self.files if Path(item.name).suffix.lower() in MODEL_EXTENSIONS]
        else:
            paths = [base_path]

        unsupported = [path for path in paths if path.suffix.lower() not in MODEL_EXTENSIONS]
        if unsupported:
            suffixes = ", ".join(sorted({path.suffix or "<none>" for path in unsupported}))
            self.report({"ERROR"}, f"Unsupported G4 model extension: {suffixes}")
            return {"CANCELLED"}
        if not paths:
            self.report({"ERROR"}, "No supported G4 model files selected")
            return {"CANCELLED"}

        prefs = addon_preferences()
        imported_total = 0
        part_mesh_total = 0
        imported_parts = []
        summaries = []
        try:
            for path in paths:
                summary, imported_names = import_g4_model(path, prefs, self.create_report_text)
                summaries.append(summary)
                imported_total += len(imported_names)
                armatures = imported_armatures(imported_names)
                if self.import_character_parts and armatures and re.fullmatch(r"c\d{6,8}", path.stem, re.IGNORECASE):
                    attached, part_paths = import_character_parts_for_armature(
                        path,
                        armatures[0],
                        prefs,
                        False,
                        self.body_model,
                        self.shoes_model,
                        self.create_report_text,
                        self.character_part_stem,
                        self.preserve_character_part_armatures,
                    )
                    part_mesh_total += attached
                    imported_parts.extend(part_paths)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        summary = summaries[-1]
        materials = summary.get("material_textures", {})
        hashes = summary.get("texture_hashes", {})
        missing = materials.get("missing") or []
        if len(summaries) == 1:
            message = (
                f"Imported {Path(summary['resolved_model']).name}: "
                f"{imported_total} objects, "
                f"materials {materials.get('resolved', 0)}/{materials.get('total', 0)}, "
                f"hashes {hashes.get('resolved', 0)}/{hashes.get('total', 0)}"
            )
        else:
            message = f"Imported {len(summaries)} G4 models: {imported_total} objects"
        if missing:
            message += f"; missing: {', '.join(missing[:4])}"
        if imported_parts:
            message += f"; parts {len(imported_parts)} ({part_mesh_total} meshes)"
        self.report({"INFO"}, message)
        return {"FINISHED"}


def collect_model_paths(directory: Path, recursive: bool) -> list[Path]:
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    candidates = sorted(
        path
        for path in iterator
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS
    )
    by_folder_stem: dict[tuple[Path, str], Path] = {}
    for path in candidates:
        key = (path.parent, path.stem)
        previous = by_folder_stem.get(key)
        if previous is None or (previous.suffix.lower() == ".g4md" and path.suffix.lower() == ".g4pkm"):
            by_folder_stem[key] = path
    return sorted(by_folder_stem.values(), key=lambda item: item.as_posix())


def collect_model_paths_auto(directory: Path, recursive: bool) -> list[Path]:
    paths = collect_model_paths(directory, recursive)
    if paths or recursive:
        return paths
    return collect_model_paths(directory, True)


class IMPORT_OT_level5_g4_folder(Operator):
    bl_idname = "import_scene.level5_g4_folder"
    bl_label = "Import Level-5 G4 Model Folder"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(
        name="Folder",
        subtype="DIR_PATH",
        options={"SKIP_SAVE"},
    )
    recursive: BoolProperty(
        name="Recursive",
        default=True,
        description="Import models from subfolders too",
    )
    create_report_text: BoolProperty(
        name="Create Report Text",
        default=True,
        description="Create Blender text blocks with exporter summaries",
    )
    import_character_parts: BoolProperty(
        name="Import Body and Shoes",
        default=False,
    )
    auto_character_parts: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    body_model: StringProperty(name="Body Model", subtype="FILE_PATH")
    shoes_model: StringProperty(name="Shoes Model", subtype="FILE_PATH")
    preserve_character_part_armatures: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    def invoke(self, context, event):
        self.auto_character_parts = False
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "recursive")
        layout.prop(self, "create_report_text")
        layout.prop(self, "import_character_parts")
        if self.import_character_parts:
            layout.prop(self, "body_model")
            layout.prop(self, "shoes_model")

    def execute(self, context):
        directory = Path(bpy.path.abspath(self.directory or ""))
        if not directory.exists() or not directory.is_dir():
            self.report({"ERROR"}, f"Folder not found: {directory}")
            return {"CANCELLED"}

        paths = collect_model_paths_auto(directory, self.recursive)
        if not paths:
            self.report({"ERROR"}, f"No .g4md/.g4pkm models found in {directory}")
            return {"CANCELLED"}

        prefs = addon_preferences()
        imported_total = 0
        try:
            for path in paths:
                _, imported_names = import_g4_model(path, prefs, self.create_report_text)
                imported_total += len(imported_names)
                armatures = imported_armatures(imported_names)
                if self.import_character_parts and armatures and re.fullmatch(r"c\d{6,8}", path.stem, re.IGNORECASE):
                    attached, _ = import_character_parts_for_armature(
                        path,
                        armatures[0],
                        prefs,
                        False,
                        self.body_model,
                        self.shoes_model,
                        self.create_report_text,
                        preserve_part_armatures=self.preserve_character_part_armatures,
                    )
                    imported_total += attached
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        self.report({"INFO"}, f"Imported {len(paths)} G4 models from folder: {imported_total} objects")
        return {"FINISHED"}


class IMPORT_OT_level5_g4_character_parts(Operator):
    bl_idname = "import_scene.level5_g4_character_parts"
    bl_label = "Attach Level-5 G4 Body and Shoes"
    bl_options = {"REGISTER", "UNDO"}

    body_model: StringProperty(name="Body Model", subtype="FILE_PATH")
    shoes_model: StringProperty(name="Shoes Model", subtype="FILE_PATH")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "ARMATURE"

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=620)

    def draw(self, context):
        layout = self.layout
        layout.label(text=f"Target rig: {context.active_object.name}")
        layout.prop(self, "body_model")
        layout.prop(self, "shoes_model")

    def execute(self, context):
        target = context.active_object
        paths = [Path(bpy.path.abspath(value)) for value in (self.body_model, self.shoes_model) if value]
        if not paths:
            self.report({"WARNING"}, "No body or shoes model selected")
            return {"CANCELLED"}
        invalid = [path for path in paths if not path.is_file() or path.suffix.lower() not in MODEL_EXTENSIONS]
        if invalid:
            self.report({"ERROR"}, f"Character part not found or unsupported: {invalid[0]}")
            return {"CANCELLED"}
        prefs = addon_preferences()
        try:
            attached = sum(attach_part_to_armature(path, target, prefs, False) for path in paths)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Attached {len(paths)} character parts: {attached} meshes")
        return {"FINISHED"}


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_level5_g4.bl_idname, text="Level-5 G4 Model (.g4md/.g4pkm)")
    self.layout.operator(IMPORT_OT_level5_g4_folder.bl_idname, text="Level-5 G4 Model Folder")
    self.layout.operator(
        IMPORT_OT_level5_g4_character_parts.bl_idname,
        text="Attach Level-5 G4 Body and Shoes",
    )


classes = [
    G4ImporterPreferences,
    IMPORT_OT_level5_g4,
    IMPORT_OT_level5_g4_folder,
    IMPORT_OT_level5_g4_character_parts,
]


if hasattr(bpy.types, "FileHandler"):
    class G4_FH_import(bpy.types.FileHandler):
        bl_idname = "G4_FH_import"
        bl_label = "Level-5 G4 Model"
        bl_import_operator = IMPORT_OT_level5_g4.bl_idname
        bl_file_extensions = ".g4md;.g4pkm"

        @classmethod
        def poll_drop(cls, context):
            return context.area is not None and context.area.type in {"VIEW_3D", "OUTLINER", "FILE_BROWSER"}

    classes.append(G4_FH_import)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    g4_animation_addon.register()
    g4_port_addon.register()


def unregister():
    g4_port_addon.unregister()
    g4_animation_addon.unregister()
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
