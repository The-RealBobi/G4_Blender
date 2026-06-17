bl_info = {
    "name": "Level-5 G4 Model Importer",
    "author": "Bobi",
    "version": (0, 1, 7),
    "blender": (4, 0, 0),
    "location": "File > Import > Level-5 G4 Model",
    "description": "Import G4MD/G4PKM models.",
    "category": "Import-Export",
}

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, StringProperty
from bpy.types import AddonPreferences, Operator, OperatorFileListElement
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Quaternion, Vector


ADDON_ID = __name__
MODEL_EXTENSIONS = {".g4md", ".g4pkm"}


def default_probe_script() -> str:
    env_path = os.environ.get("LEVEL5_G4_PROBE")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    addon_path = Path(__file__).resolve()
    candidates.extend(
        [
            addon_path.parents[1] / "tools" / "g4_model_probe.py",
            addon_path.parent / "tools" / "g4_model_probe.py",
            addon_path.parent / "g4_model_probe.py",
            Path.cwd() / "MODELS" / "tools" / "g4_model_probe.py",
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
    for candidate in ("/usr/bin/python3", "/opt/homebrew/bin/python3", sys.executable):
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
        "to MODELS/tools/g4_model_probe.py, place g4_model_probe.py next to the addon, "
        "or define LEVEL5_G4_PROBE."
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
        description="Path to MODELS/tools/g4_model_probe.py",
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
        default=True,
        description="Orient imported armature bones using G4SK section-1 rest quaternions",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "python_path")
        layout.prop(self, "probe_script")
        layout.prop(self, "export_dir")
        layout.prop(self, "raw_data_root")
        layout.prop(self, "chara_model_lookup")
        layout.prop(self, "chara_model_xml")
        layout.prop(self, "pack_imported_textures")
        layout.prop(self, "cleanup_import_cache")
        layout.prop(self, "apply_bone_orientation")


def run_exporter(model_path: str, prefs: G4ImporterPreferences) -> dict:
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
    completed = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
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


def import_g4_model(path: Path, prefs: G4ImporterPreferences, create_report_text: bool) -> tuple[dict, set[str]]:
    debug = [f"[import] input={path}"]
    images_before = image_paths()
    summary = run_exporter(str(path), prefs)
    debug.append(f"[exporter] resolved={summary.get('resolved_model')}")
    debug.append(f"[exporter] dae={summary.get('dae')}")
    debug.append(f"[exporter] textures={len(summary.get('textures', []))} materials={len(summary.get('materials', {}))}")
    debug.append(f"[exporter] texture_sources={summary.get('texture_sources', [])}")
    dae_path = Path(summary["dae"]).resolve()
    if not dae_path.exists():
        raise RuntimeError(f"Generated DAE not found: {dae_path}")
    imported_names = import_collada(dae_path)
    debug.append(f"[collada] imported_objects={sorted(imported_names)}")
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
    texture_paths = {str(Path(path).resolve()) for path in summary.get("textures", [])}
    pack_images(images_before, texture_paths, getattr(prefs, "pack_imported_textures", True))
    write_debug_log(summary, debug)
    if create_report_text:
        make_report_text(summary)
        make_debug_text(summary, debug)
    cleanup_generated_files(summary, getattr(prefs, "cleanup_import_cache", True))
    return summary, imported_names


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
        summaries = []
        try:
            for path in paths:
                summary, imported_names = import_g4_model(path, prefs, self.create_report_text)
                summaries.append(summary)
                imported_total += len(imported_names)
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

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

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
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        self.report({"INFO"}, f"Imported {len(paths)} G4 models from folder: {imported_total} objects")
        return {"FINISHED"}


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_level5_g4.bl_idname, text="Level-5 G4 Model (.g4md/.g4pkm)")
    self.layout.operator(IMPORT_OT_level5_g4_folder.bl_idname, text="Level-5 G4 Model Folder")


classes = [
    G4ImporterPreferences,
    IMPORT_OT_level5_g4,
    IMPORT_OT_level5_g4_folder,
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


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
