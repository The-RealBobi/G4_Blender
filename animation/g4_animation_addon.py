import json
import hashlib
import math
import mmap
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import zlib
from collections import defaultdict
from pathlib import Path

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Matrix, Quaternion, Vector

try:
    from ..formats.cfgbin import CfgBinDocument, parse_cfgbin_file
    from ..porting.g4_model_probe import g4sk_entries_from_candidate
    from ..porting.g4_model_probe import configure_raw_data_root_from_path, find_skeleton_for_model, parse_g4sk
    from .g4pk_extract_g4mt import entries as g4pk_entries, replace_entries as replace_g4pk_entries
    from .g4pk_extract_g4mt import select_g4mt_entry
    from .g4mt_probe import parse_g4mt, read_g4sk_data
    from .g4mt_motion import (
        decode_motion,
        decode_motion_from_bank,
        prepare_motion_bank,
        simplify_motion_samples,
    )
    from ..shared.g4_joint_aliases import load_joint_alias_catalog, normalize_joint_key
    from .g4ma_motion import decode_material_motion, material_animation_paths
    from .g4cm_camera import decode_camera, parse_g4cm
    from ..formats.g4mt import parse_g4mt as parse_g4mt_bank
    from ..formats.diagnostics import NativeFormatError
    from ..formats.g4_axes import blender_from_g4_matrix, g4_from_blender_matrix
    from ..formats.g4mt_writer import (
        G4MTClipSamples, G4MTLocalTransform, G4MTTargetSamples,
        g4mt_family_target_hierarchy, write_g4mt_clip, write_g4mt_family,
        write_g4mt_family_preserving_additive,
    )
    from ..formats.g4sk_writer import NATIVE_HELPER_NAMES, G4SKNode, copy_native_helper_fields, write_g4sk_v104
    from ..effects.particle_library import particle_library_metadata, read_particle_library
    from ..shading.particle_nodes import deduplicate_particle_geometry_groups
    from .g4_event import (
        event_light_config_references, event_light_parameter_entries, event_light_slots,
        load_event_actor_models, load_event_actor_points,
        load_event_actor_point_assignments, point_assignment_for_actor,
    )
except ImportError:
    from formats.cfgbin import CfgBinDocument, parse_cfgbin_file
    from porting.g4_model_probe import g4sk_entries_from_candidate
    from porting.g4_model_probe import configure_raw_data_root_from_path, find_skeleton_for_model, parse_g4sk
    from animation.g4pk_extract_g4mt import entries as g4pk_entries, replace_entries as replace_g4pk_entries
    from animation.g4pk_extract_g4mt import select_g4mt_entry
    from animation.g4mt_probe import parse_g4mt, read_g4sk_data
    from animation.g4mt_motion import (
        decode_motion,
        decode_motion_from_bank,
        prepare_motion_bank,
        simplify_motion_samples,
    )
    from shared.g4_joint_aliases import load_joint_alias_catalog, normalize_joint_key
    from animation.g4ma_motion import decode_material_motion, material_animation_paths
    from animation.g4cm_camera import decode_camera, parse_g4cm
    from formats.g4mt import parse_g4mt as parse_g4mt_bank
    from formats.diagnostics import NativeFormatError
    from formats.g4_axes import blender_from_g4_matrix, g4_from_blender_matrix
    from formats.g4mt_writer import (
        G4MTClipSamples, G4MTLocalTransform, G4MTTargetSamples,
        g4mt_family_target_hierarchy, write_g4mt_clip, write_g4mt_family,
        write_g4mt_family_preserving_additive,
    )
    from formats.g4sk_writer import NATIVE_HELPER_NAMES, G4SKNode, copy_native_helper_fields, write_g4sk_v104
    from effects.particle_library import particle_library_metadata, read_particle_library
    from shading.particle_nodes import deduplicate_particle_geometry_groups
    from animation.g4_event import (
        event_light_config_references, event_light_parameter_entries, event_light_slots,
        load_event_actor_models, load_event_actor_points,
        load_event_actor_point_assignments, point_assignment_for_actor,
    )


ADDON_ID = __name__
MODEL_ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}\d{4,10})(?![A-Za-z0-9])")


def action_fcurves(action):
    """Return F-Curves for both Blender's legacy and layered Action APIs."""
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return tuple(legacy)
    curves = []
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for channelbag in getattr(strip, "channelbags", ()):
                curves.extend(channelbag.fcurves)
    return tuple(curves)


def deduplicate_static_materials(scene) -> int:
    """Share identical non-animated material trees after an event import."""
    owners = {}
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material is not None:
                owners.setdefault(slot.material, []).append(slot)

    signatures = {}
    merged_slots = 0
    for material in tuple(bpy.data.materials):
        tree = material.node_tree
        if tree is None or material.animation_data is not None:
            continue
        tree_animation = tree.animation_data
        if tree_animation and (tree_animation.action or tree_animation.nla_tracks):
            continue
        if not material.get("g4_effect_preview"):
            continue

        nodes = list(tree.nodes)
        node_ids = {node: index for index, node in enumerate(nodes)}
        node_data = []
        for node in nodes:
            values = []
            for socket in node.inputs:
                value = getattr(socket, "default_value", None)
                if hasattr(value, "__iter__") and not isinstance(value, str):
                    value = tuple(value)
                values.append(value)
            image = node.image.filepath if node.type == "TEX_IMAGE" and node.image else ""
            node_data.append((node.type, node.name, getattr(node, "operation", ""),
                              getattr(node, "blend_type", ""), image, tuple(values)))
        links = tuple(sorted((node_ids[link.from_node], link.from_socket.name,
                              node_ids[link.to_node], link.to_socket.name)
                             for link in tree.links))
        kind = material.get("g4_effect_preview")
        source = material.get("g4_source_model", "")
        signature = repr((kind, source, tuple(node_data), links,
                          getattr(material, "surface_render_method", None),
                          getattr(material, "blend_method", None),
                          getattr(material, "use_transparent_shadow", None)))
        canonical = signatures.get(signature)
        if canonical is None:
            signatures[signature] = material
            continue
        for slot in owners.get(material, ()):
            slot.material = canonical
            merged_slots += 1
        if material.users == 0 and not material.use_fake_user:
            bpy.data.materials.remove(material)
    return merged_slots


def fold_static_material_constants(scene) -> int:
    """Replace unused constant nodes in material trees after animation migration."""
    folded = 0
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is None or not material.get("g4_effect_preview"):
                continue
            tree = material.node_tree
            if tree is None or material.animation_data is not None or tree.animation_data is not None:
                continue
            for node in tuple(tree.nodes):
                if node.bl_idname not in {"ShaderNodeValue", "ShaderNodeRGB"}:
                    continue
                output = node.outputs[0]
                links = tuple(output.links)
                if not links:
                    continue
                value = output.default_value
                if hasattr(value, "__iter__") and not isinstance(value, str):
                    value = tuple(value)
                try:
                    for link in links:
                        link.to_socket.default_value = value
                except (AttributeError, TypeError, ValueError):
                    continue
                for link in links:
                    tree.links.remove(link)
                tree.nodes.remove(node)
                folded += 1
    return folded


def fold_static_math_constants(scene) -> int:
    """Fold constant math chains in non-animated threshold effect trees."""
    folded = 0

    def value(socket, constants):
        if socket.is_linked:
            return constants.get(socket.links[0].from_socket)
        default = getattr(socket, "default_value", None)
        return tuple(default) if hasattr(default, "__iter__") and not isinstance(default, str) else default

    def evaluate(node, inputs):
        operation = node.operation
        if operation == "ABSOLUTE":
            result = abs(inputs[0])
        elif operation == "ADD":
            result = inputs[0] + inputs[1]
        elif operation == "SUBTRACT":
            result = inputs[0] - inputs[1]
        elif operation == "MULTIPLY":
            result = inputs[0] * inputs[1]
        elif operation == "DIVIDE":
            result = inputs[0] / inputs[1] if inputs[1] else 0.0
        elif operation == "MULTIPLY_ADD":
            result = inputs[0] * inputs[1] + inputs[2]
        elif operation == "MINIMUM":
            result = min(inputs[0], inputs[1])
        elif operation == "MAXIMUM":
            result = max(inputs[0], inputs[1])
        elif operation == "LESS_THAN":
            result = float(inputs[0] < inputs[1])
        elif operation == "GREATER_THAN":
            result = float(inputs[0] > inputs[1])
        else:
            return None
        return min(max(result, 0.0), 1.0) if node.use_clamp else result

    materials = {slot.material for obj in scene.objects if obj.type == "MESH"
                 for slot in obj.material_slots if slot.material}
    materials = {material for material in materials
                 if material.get("g4_effect_preview") in {"T3_THRESHOLD", "THRESHOLD_STATIC"}
                 and material.node_tree and material.animation_data is None
                 and material.node_tree.animation_data is None}
    for material in materials:
        tree = material.node_tree
        constants = {}
        for _ in range(len(tree.nodes)):
            progress = False
            for node in tuple(tree.nodes):
                if node.bl_idname != "ShaderNodeMath":
                    continue
                inputs = [value(socket, constants) for socket in node.inputs]
                if len(inputs) < 3:
                    inputs.extend([0.0] * (3 - len(inputs)))
                if any(item is None for item in inputs):
                    continue
                result = evaluate(node, inputs)
                if result is None:
                    continue
                output = node.outputs[0]
                constants[output] = result
                links = tuple(output.links)
                try:
                    for link in links:
                        link.to_socket.default_value = result
                except (AttributeError, TypeError, ValueError):
                    continue
                for link in links:
                    tree.links.remove(link)
                tree.nodes.remove(node)
                folded += 1
                progress = True
                break
            if not progress:
                break
    return folded


def join_event_effect_meshes(scene) -> int:
    """Join VFX meshes that share one effect rig and visibility interval."""
    groups = defaultdict(list)

    def visibility_signature(obj):
        action = obj.animation_data.action if obj.animation_data else None
        return tuple(
            (
                curve.data_path,
                curve.array_index,
                tuple((point.co[0], point.co[1], point.interpolation) for point in curve.keyframe_points),
            )
            for curve in action_fcurves(action)
            if curve.data_path in {"hide_viewport", "hide_render"}
        ) if action else ()

    for obj in scene.objects:
        if obj.type != "MESH" or not any(
            material and material.get("g4_effect_preview") for material in obj.data.materials
        ):
            continue
        parent = obj.parent
        if parent is not None and parent.type == "ARMATURE":
            groups[(parent, visibility_signature(obj))].append(obj)

    joined = 0
    current_frame = scene.frame_current
    for objects in groups.values():
        if len(objects) < 2:
            continue
        bpy.ops.object.select_all(action="DESELECT")
        active = objects[0]
        for obj in objects:
            obj.hide_viewport = False
            obj.hide_set(False)
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
        bpy.ops.object.join()
        joined += len(objects) - 1
    scene.frame_set(current_frame)
    return joined


def action_fcurve_find(action, data_path: str, index: int):
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return legacy.find(data_path, index=index)
    return next(
        (curve for curve in action_fcurves(action) if curve.data_path == data_path and curve.array_index == index),
        None,
    )


def action_fcurve_remove(action, curve) -> None:
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        legacy.remove(curve)
        return
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            for channelbag in getattr(strip, "channelbags", ()):
                for candidate in tuple(channelbag.fcurves):
                    if (
                        candidate.data_path == curve.data_path
                        and candidate.array_index == curve.array_index
                    ):
                        channelbag.fcurves.remove(candidate)
                        return


def action_fcurve_new(action, owner, data_path: str, index: int, group: str = ""):
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return legacy.new(data_path, index=index, action_group=group)
    if owner is None:
        raise RuntimeError("Layered Blender Actions require an assigned datablock owner")
    animation = owner.animation_data_create()
    previous = animation.action
    if previous != action:
        animation.action = action
    try:
        return action.fcurve_ensure_for_datablock(
            owner,
            data_path,
            index=index,
            group_name=group,
        )
    finally:
        if previous != action:
            animation.action = previous


def default_python() -> str:
    for candidate in ("/usr/bin/python3", "/opt/homebrew/bin/python3", sys.executable):
        if candidate and Path(candidate).exists():
            return candidate
    return "python3"


def default_decoder_script() -> str:
    env_path = os.environ.get("LEVEL5_G4MT_DECODER")
    candidates = [Path(env_path)] if env_path else []
    addon_path = Path(__file__).resolve()
    candidates.extend(
        [
            addon_path.parent / "g4mt_motion.py",
        ]
    )
    return next((str(path) for path in candidates if path.is_file()), "")


def default_camera_decoder_script() -> str:
    env_path = os.environ.get("LEVEL5_G4CM_DECODER")
    candidates = [Path(env_path)] if env_path else []
    addon_path = Path(__file__).resolve()
    candidates.extend(
        [
            addon_path.parent / "g4cm_camera.py",
        ]
    )
    return next((str(path) for path in candidates if path.is_file()), "")


def inferred_raw_data_root(path: Path) -> Path | None:
    for parent in path.parents:
        if parent.name == "data" and (parent / "common").is_dir():
            return parent
    return None


def candidate_data_roots(g4mt_path: Path, configured_root: str) -> list[Path]:
    roots = []
    inferred = inferred_raw_data_root(g4mt_path)
    if inferred is not None:
        roots.append(inferred)
    if configured_root:
        roots.append(Path(bpy.path.abspath(configured_root)))
    return list(dict.fromkeys(root.resolve() for root in roots if root.is_dir()))


def addon_preferences():
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is not None:
        return addon.preferences

    class Defaults:
        python_path = default_python()
        decoder_script = default_decoder_script()
        camera_decoder_script = default_camera_decoder_script()
        raw_data_root = os.environ.get("LEVEL5_G4_RAW_ROOT", "")
        keep_decode_json = False
        event_character_parts = "{}"
        character_import_parts = "{}"

    return Defaults()


def resolve_decoder(prefs) -> Path:
    configured = bpy.path.abspath(getattr(prefs, "decoder_script", "") or "")
    decoder = Path(configured) if configured else Path()
    if decoder.is_file():
        return decoder
    fallback = default_decoder_script()
    if fallback:
        return Path(fallback)
    raise RuntimeError("animation/g4mt_motion.py was not found. Configure it in the addon preferences.")


def resolve_camera_decoder(prefs) -> Path:
    configured = bpy.path.abspath(getattr(prefs, "camera_decoder_script", "") or "")
    decoder = Path(configured) if configured else Path()
    if decoder.is_file():
        return decoder
    fallback = default_camera_decoder_script()
    if fallback:
        return Path(fallback)
    raise RuntimeError("animation/g4cm_camera.py was not found. Configure it in the addon preferences.")


def decode_g4mt(
    path: Path,
    clip: str,
    prefs,
    skeleton_path: Path | None = None,
) -> tuple[dict, Path]:
    python_path = bpy.path.abspath(getattr(prefs, "python_path", "") or default_python())
    decoder = resolve_decoder(prefs)
    cache_root = Path(tempfile.gettempdir()) / "level5_g4mt_blender"
    cache_root.mkdir(parents=True, exist_ok=True)
    output = cache_root / f"{path.stem}_{clip.replace('/', '_')}.json"
    command = [str(python_path), str(decoder), str(path), "--clip", clip, "--output", str(output), "--format", "json"]
    if skeleton_path is not None:
        command.extend(("--skeleton", str(skeleton_path)))
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            "G4MT decoder failed\n"
            f"Command: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    try:
        return json.loads(output.read_text(encoding="utf-8")), output
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read decoded G4MT JSON: {output}") from exc


def materialize_g4mt(path: Path, entry: str) -> tuple[Path, Path | None, str | None]:
    if path.suffix.lower() == ".g4mt":
        return path, None, None
    if path.suffix.lower() != ".g4pk":
        raise ValueError(f"unsupported animation container: {path.suffix or '<none>'}")
    entry_name, payload = select_g4mt_entry(path.read_bytes(), entry)
    cache_root = Path(tempfile.gettempdir()) / "level5_g4pk_blender"
    cache_root.mkdir(parents=True, exist_ok=True)
    safe_name = Path(entry_name.replace("\\", "/")).name
    if not safe_name.lower().endswith(".g4mt"):
        safe_name = f"{path.stem}_{entry}.g4mt"
    destination = Path(tempfile.mkdtemp(prefix="animation_", dir=cache_root)) / safe_name
    destination.write_bytes(payload)
    return destination, destination, entry_name


def decode_g4cm(path: Path, clip: str, prefs) -> tuple[dict, Path]:
    python_path = bpy.path.abspath(getattr(prefs, "python_path", "") or default_python())
    decoder = resolve_camera_decoder(prefs)
    cache_root = Path(tempfile.gettempdir()) / "level5_g4cm_blender"
    cache_root.mkdir(parents=True, exist_ok=True)
    output = cache_root / f"{path.stem}_{clip.replace('/', '_')}.json"
    command = [str(python_path), str(decoder), str(path), "--clip", clip, "--output", str(output)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            "G4CM decoder failed\n"
            f"Command: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    try:
        return json.loads(output.read_text(encoding="utf-8")), output
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read decoded G4CM JSON: {output}") from exc


def model_identifiers(path: Path) -> list[str]:
    stem = re.sub(r"_c\d+$", "", path.stem, flags=re.IGNORECASE)
    return list(dict.fromkeys(value.lower() for value in MODEL_ID_RE.findall(stem)))


def discover_animation_bank_resources_by_branch(
    character_bank_id: str, event_skeleton_id: str, data_roots: list[Path],
    export_chr_animations: bool = True, export_event_animations: bool = True,
) -> list[tuple[Path, Path, bool]]:
    bank_ids = {False: character_bank_id.strip().lower(), True: event_skeleton_id.strip().lower()}
    if any(
        enabled and not re.fullmatch(r"c\d{4,10}", bank_ids[is_event])
        for is_event, enabled in ((False, export_chr_animations), (True, export_event_animations))
    ):
        raise ValueError("Selected character and event animation IDs must look like c000101")
    resources = []
    event_groups = set()
    event_point_effects = {}
    seen_paths = set()
    seen_relative_paths = set()
    for data_root in data_roots:
        for folder, is_event in (("chr", False), ("event", True)):
            if not (export_event_animations if is_event else export_chr_animations):
                continue
            source_root = data_root / "common" / folder
            if not source_root.is_dir():
                continue
            for path in source_root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in {".g4mt", ".g4pk"}:
                    continue
                relative = path.relative_to(data_root)
                identifiers = {
                    identifier
                    for part in relative.parts
                    for identifier in model_identifiers(Path(part))
                }
                if is_event:
                    event_parts = event_path_parts(relative)
                    if event_parts is not None and "point_eff" in path.stem.casefold():
                        event_point_effects.setdefault((data_root, *event_parts), []).append(path)
                if bank_ids[is_event] not in identifiers:
                    continue
                resolved = path.resolve()
                if resolved in seen_paths or relative in seen_relative_paths:
                    continue
                seen_paths.add(resolved)
                seen_relative_paths.add(relative)
                resources.append((path, data_root, is_event))
                if is_event:
                    event_parts = event_path_parts(relative)
                    if event_parts is not None:
                        event_groups.add((data_root, *event_parts))
    for data_root, group, event_name in event_groups:
        for path in event_point_effects.get((data_root, group, event_name), ()):
            resolved = path.resolve()
            relative = path.relative_to(data_root)
            if resolved in seen_paths or relative in seen_relative_paths:
                continue
            seen_paths.add(resolved)
            seen_relative_paths.add(relative)
            resources.append((path, data_root, True))
    return sorted(resources, key=lambda item: (item[2], item[0].as_posix().casefold()))


def discover_animation_bank_resources(bank_id: str, data_roots: list[Path]) -> list[tuple[Path, Path, bool]]:
    return discover_animation_bank_resources_by_branch(bank_id, bank_id, data_roots)


def route_root_motion_to_global_helper(
    target_joint_mapping: dict[str, str],
    source_joint_names: set[str],
    target_joint_names: set[str],
) -> dict[str, str]:
    root_name = "c_global_0_0"
    if root_name not in source_joint_names or root_name not in target_joint_names:
        raise ValueError("Root animation requires c_global_0_0 in both source and target G4SKs")
    return {
        **{target: source for target, source in target_joint_mapping.items() if source != root_name},
        root_name: root_name,
    }


def resolve_animation_family_ids(model_path: Path, data_roots: list[Path]) -> tuple[str, str]:
    """Return the character-motion family and generic event skeleton for a model."""
    lookup_path = model_lookup_path()
    relative_path = model_relative_path(model_path)
    if lookup_path is None or relative_path is None:
        raise ValueError(f"Could not resolve the original model family from {model_path}")
    try:
        lookup = json.loads(lookup_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read character model lookup: {lookup_path}") from error
    row = (lookup.get("models") or {}).get(relative_path)
    if not row:
        raise ValueError(f"Original model is not present in the character model lookup: {relative_path}")
    event_skeleton_id = str(row.get("g4sk_stem") or "").lower()
    if not re.fullmatch(r"c\d{4,10}", event_skeleton_id):
        raise ValueError(f"No generic skeleton ID is recorded for {relative_path}")
    try:
        skeleton_crc = int(row["skeleton_crc"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"No valid skeleton CRC is recorded for {relative_path}") from error

    for data_root in data_roots:
        config_paths = sorted((data_root / "common" / "gamedata" / "character").glob("chara_motion*.cfg.bin"))
        for config_path in config_paths:
            try:
                document = parse_cfgbin_file(config_path)
            except (OSError, ValueError):
                continue
            if not isinstance(document, CfgBinDocument):
                continue
            for entry in document.entries:
                if entry.name != "CHARA_MOTION_INFO" or len(entry.values) < 2:
                    continue
                try:
                    entry_crc = int(entry.values[0].value)
                except (TypeError, ValueError):
                    continue
                if entry_crc != skeleton_crc:
                    continue
                bank_path = str(entry.values[1].value).replace("\\", "/")
                match = re.match(r"(c\d{4,10})/", bank_path, re.IGNORECASE)
                if match:
                    return match.group(1).lower(), event_skeleton_id
    raise ValueError(
        f"Could not find a character animation family for {relative_path} in chara_motion config"
    )


def find_animation_bank_resources(bank_id: str, data_roots: list[Path]) -> tuple[int, int]:
    resources = discover_animation_bank_resources(bank_id, data_roots)
    return sum(not is_event for _, _, is_event in resources), sum(is_event for _, _, is_event in resources)


def rewrite_bank_identifier(value: str, source_bank_id: str, target_bank_id: str) -> str:
    return re.sub(
        rf"(?<![A-Za-z0-9]){re.escape(source_bank_id)}(?![A-Za-z0-9])",
        target_bank_id,
        value,
        flags=re.IGNORECASE,
    )


def target_animation_bank_id(armature) -> str:
    model_path = Path(str(armature.get("g4_exported_model_path", ""))) if armature else Path()
    candidates = model_identifiers(model_path) if model_path.name else []
    if not candidates and armature is not None:
        candidates = model_identifiers(Path(armature.name))
    character_id = next((value for value in candidates if value.startswith("c")), "")
    if re.fullmatch(r"c99\d{6}", character_id):
        return f"c00{character_id[3:]}"
    if re.fullmatch(r"c00\d{6}", character_id):
        return character_id
    return ""


def resolve_model_path(g4mt_path: Path, configured_root: str) -> Path | None:
    for raw_root in candidate_data_roots(g4mt_path, configured_root):
        model_roots = (
            raw_root / "common" / "chr",
            raw_root / "common" / "event" / "ev_item",
        )
        for identifier in model_identifiers(g4mt_path):
            for model_root in model_roots:
                if not model_root.is_dir():
                    continue
                for extension in (".g4pkm", ".g4md"):
                    direct = model_root / identifier / f"{identifier}{extension}"
                    if direct.is_file():
                        return direct
                # Ni no Kuni II and YK4 encode actor instances in the final
                # one or two digits while keeping one monolithic base model.
                # Restrict this aliasing to non-modular character archives so
                # HQ/LQ character IDs in modular games are never conflated.
                if model_root.name == "chr" and not any(
                    (model_root / folder).is_dir() for folder in ("_face", "_uniform")
                ):
                    aliases = (f"{identifier[:-1]}0", f"{identifier[:-2]}00")
                    for alias in dict.fromkeys(aliases):
                        for extension in (".g4pkm", ".g4md"):
                            candidate = model_root / alias / f"{alias}{extension}"
                            if candidate.is_file():
                                return candidate
                for extension in (".g4pkm", ".g4md"):
                    matches = sorted(model_root.rglob(f"{identifier}{extension}"))
                    if matches:
                        return matches[0]
    return None


def model_lookup_path() -> Path | None:
    configured = os.environ.get("LEVEL5_G4_CHARA_LOOKUP", "")
    candidates = [Path(configured).expanduser()] if configured else []
    for addon in bpy.context.preferences.addons:
        preferences = getattr(addon, "preferences", None)
        value = getattr(preferences, "chara_model_lookup", "") if preferences is not None else ""
        if value:
            candidates.append(Path(bpy.path.abspath(value)))
    candidates.append(Path(__file__).resolve().parents[1] / "data" / "chara_model_lookup.json")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def model_relative_path(model_path: Path) -> str | None:
    parts = model_path.parts
    for index in range(len(parts) - 2):
        if parts[index:index + 2] == ("common", "chr"):
            return Path(*parts[index + 2:]).as_posix()
    return None


def valid_skeleton_path(path: Path | None) -> Path | None:
    if path is None or not path.is_file():
        return None
    try:
        return path if read_g4sk_data(path)[:4] == b"G4SK" else None
    except OSError:
        return None


def materialize_skeleton_source(source: str) -> Path | None:
    """Return a file path for the exact G4SK source used during model import."""
    source = source.split(" via ", 1)[0]
    if "::" not in source:
        return valid_skeleton_path(Path(source))

    pack_name, entry_name = source.rsplit("::", 1)
    pack_path = Path(pack_name)
    if not pack_path.is_file():
        return None
    for skeleton_data, candidate_source in g4sk_entries_from_candidate(pack_path):
        if candidate_source.rsplit("::", 1)[-1] != entry_name:
            continue
        cache_root = Path(tempfile.gettempdir()) / "level5_g4sk_blender"
        cache_root.mkdir(parents=True, exist_ok=True)
        destination = cache_root / f"{zlib.crc32(source.encode('utf-8')) & 0xFFFFFFFF:08x}.g4sk"
        if not destination.is_file() or destination.read_bytes() != skeleton_data:
            destination.write_bytes(skeleton_data)
        return destination
    return None


def resolve_skeleton_path(model_path: Path | None) -> Path | None:
    if model_path is None:
        return None
    # Keep animation decoding on the same G4SK selected by the native model
    # importer.  A character package can contain an embedded fallback rig
    # whose bind transforms do not match the sibling rig used by its mesh
    # palette; decoding against that fallback stretches skinned descendants.
    try:
        configure_raw_data_root_from_path(model_path)
        skeleton_data, skeleton_source = find_skeleton_for_model(model_path)
    except (OSError, ValueError, RuntimeError):
        skeleton_data, skeleton_source = None, None
    if skeleton_data:
        cache_root = Path(tempfile.gettempdir()) / "level5_g4sk_blender"
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_key = zlib.crc32((str(model_path.resolve()) + str(skeleton_source)).encode("utf-8")) & 0xFFFFFFFF
        destination = cache_root / f"{cache_key:08x}.g4sk"
        if not destination.is_file() or destination.read_bytes() != skeleton_data:
            destination.write_bytes(skeleton_data)
        return destination
    for candidate in (model_path, model_path.with_suffix(".g4sk"), model_path.with_suffix(".g4pkm")):
        resolved = valid_skeleton_path(candidate)
        if resolved is not None:
            return resolved

    lookup_path = model_lookup_path()
    relative_path = model_relative_path(model_path)
    if lookup_path is None or relative_path is None:
        return None
    try:
        lookup = json.loads(lookup_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    row = (lookup.get("models") or {}).get(relative_path)
    if not row:
        return None

    character_root = model_path.parents[len(Path(relative_path).parts) - 1]
    for field in ("g4sk_path", "body_path"):
        value = str(row.get(field) or "").replace("\\", "/")
        if not value:
            continue
        relative = Path(value).with_suffix(".g4sk")
        candidates = [character_root / relative]
        if relative.parts and relative.parts[0] == "_common":
            candidates.insert(0, character_root / Path(*relative.parts[1:]))
        for candidate in candidates:
            resolved = valid_skeleton_path(candidate)
            if resolved is not None:
                return resolved
    return None


def event_path_parts(path: Path) -> tuple[str, str] | None:
    parts = path.parts
    for index, part in enumerate(parts[:-2]):
        if part == "event" and index + 2 < len(parts):
            return parts[index + 1], parts[index + 2]
    return None


def resolve_companion_g4cm(g4mt_path: Path, configured_root: str) -> Path | None:
    event_parts = event_path_parts(g4mt_path)
    if event_parts is None:
        return None
    group, event_name = event_parts
    for data_root in candidate_data_roots(g4mt_path, configured_root):
        event_root = data_root / "common" / "event" / group / event_name
        direct = event_root / f"{event_name}_camera.g4cm"
        if direct.is_file():
            return direct
        if event_root.is_dir():
            matches = sorted(event_root.glob("*camera*.g4cm"))
            if matches:
                return matches[0]
    return None


def track_bone_names(motion: dict) -> set[str]:
    return {track["target_name"] for track in motion["tracks"] if track.get("target_name")}


def resolve_track_names_from_armature(motion: dict, armature) -> int:
    names_by_hash = {
        zlib.crc32(bone.name.encode("utf-8")) & 0xFFFFFFFF: bone.name
        for bone in armature.data.bones
    }
    resolved = 0
    for track in motion["tracks"]:
        if track.get("target_name"):
            continue
        target_hash = track.get("target_hash")
        try:
            value = int(target_hash, 16) if isinstance(target_hash, str) else int(target_hash)
        except (TypeError, ValueError):
            continue
        name = names_by_hash.get(value)
        if name is not None:
            track["target_name"] = name
            resolved += 1
    bone_names_by_source = {name: [] for name in track_bone_names(motion)}
    explicit_names = {name: [] for name in bone_names_by_source}
    explicit_bone_names = set()
    for bone in armature.data.bones:
        source_name = str(bone.get("g4_animation_source_joint", "")).strip()
        if source_name:
            explicit_bone_names.add(bone.name)
        if source_name in explicit_names:
            explicit_names[source_name].append(bone.name)
    catalog = load_joint_alias_catalog()
    alias_targets: dict[str, list[str]] = defaultdict(list)
    for bone in armature.data.bones:
        if bone.name in explicit_bone_names:
            continue
        target_name = catalog.aliases.get(normalize_joint_key(bone.name), "")
        if target_name in bone_names_by_source:
            alias_targets[target_name].append(bone.name)
    try:
        from ..porting.g4_port_addon import contextual_joint_aliases
    except ImportError:
        from porting.g4_port_addon import contextual_joint_aliases
    for bone_name, source_name in contextual_joint_aliases(
        {bone.name for bone in armature.data.bones}
    ).items():
        if (
            bone_name not in explicit_bone_names
            and source_name in bone_names_by_source
            and bone_name not in alias_targets[source_name]
        ):
            alias_targets[source_name].append(bone_name)
    alias_weight_sums = None

    def mesh_weight_sums():
        nonlocal alias_weight_sums
        if alias_weight_sums is None:
            alias_weight_sums = defaultdict(float)
            meshes = (
                obj for obj in bpy.data.objects
                if obj.type == "MESH"
                and any(mod.type == "ARMATURE" and mod.object == armature for mod in obj.modifiers)
            )
            for mesh in meshes:
                group_names = {group.index: group.name for group in mesh.vertex_groups}
                for vertex in mesh.data.vertices:
                    for assignment in vertex.groups:
                        name = group_names.get(assignment.group)
                        if name:
                            alias_weight_sums[name] += assignment.weight
        return alias_weight_sums

    def parent_aliases(source_name):
        candidates = alias_targets[source_name]
        if len(candidates) < 2:
            return candidates

        paired_helpers = [
            name for name in candidates
            if any(token in normalize_joint_key(name) for token in ("twist", "roll", "hiza"))
        ]
        if paired_helpers:
            primary = [name for name in candidates if name not in paired_helpers]
            if len(primary) == 1:
                return [primary[0], *paired_helpers]

        weights = mesh_weight_sums()
        ranked = sorted(candidates, key=lambda name: weights[name], reverse=True)
        # A clearly dominant skinning group identifies the source joint more
        # reliably than an otherwise plausible parent/child name match.
        if weights[ranked[0]] > 0.0 and weights[ranked[0]] >= 2.0 * weights[ranked[1]]:
            return [ranked[0]]

        ancestors = []
        for candidate in candidates:
            if all(
                other == candidate
                or any(parent.name == candidate for parent in iter_bone_parents(armature.data.bones[other]))
                for other in candidates
            ):
                ancestors.append(candidate)
        if len(ancestors) == 1 and source_name == "c_c_1_0":
            return ancestors
        if len(ancestors) == 1 and source_name == "c_c_1_1":
            deepest = [
                candidate for candidate in candidates
                if all(
                    other == candidate
                    or any(parent.name == other for parent in iter_bone_parents(armature.data.bones[candidate]))
                    for other in candidates
                )
            ]
            if len(deepest) == 1:
                return deepest
        return ancestors if len(ancestors) == 1 else []

    def iter_bone_parents(bone):
        parent = bone.parent
        while parent is not None:
            yield parent
            parent = parent.parent

    for source_name in bone_names_by_source:
        exact = armature.data.bones.get(source_name)
        exact_source = str(exact.get("g4_animation_source_joint", "")).strip() if exact else ""
        if source_name == "c_global_0_0" and exact is not None and exact_source in {"", source_name}:
            # Keep game root motion on the canonical G4SK root, not an inherited helper parent.
            bone_names_by_source[source_name] = [exact.name]
        elif explicit_names[source_name]:
            bone_names_by_source[source_name] = explicit_names[source_name]
        elif exact is not None:
            bone_names_by_source[source_name] = [exact.name]
        else:
            bone_names_by_source[source_name] = parent_aliases(source_name)
    for track in motion["tracks"]:
        name = track.get("target_name")
        if name:
            track["armature_bone_names"] = bone_names_by_source.get(name, ())
    return resolved


def armature_score(obj, names: set[str]) -> int:
    if obj is None or obj.type != "ARMATURE":
        return -1
    motion = {"tracks": [{"target_name": name} for name in names]}
    resolve_track_names_from_armature(motion, obj)
    return sum(bool(track.get("armature_bone_names")) for track in motion["tracks"])


def best_armature(objects, names: set[str]):
    candidates = [
        obj for obj in objects
        if obj.type == "ARMATURE"
        and obj.constraints.get("G4 Character Part Actor") is None
    ]
    return max(candidates, key=lambda obj: armature_score(obj, names), default=None)


def character_part_armatures(actor) -> list:
    return [
        obj for obj in bpy.data.objects
        if obj.type == "ARMATURE"
        and (constraint := obj.constraints.get("G4 Character Part Actor")) is not None
        and constraint.target == actor
    ]


def animate_character_parts(
    actor,
    motion: dict,
    frame_origin: int | None = None,
    strip_start: int | None = None,
    duration: int | None = None,
    strip_name: str | None = None,
    rotation_only_retarget: bool = False,
    progress_callback=None,
) -> tuple[int, int]:
    parts = character_part_armatures(actor)
    if not parts:
        return 0, 0
    created = 0
    keyed = 0
    for part_index, part_armature in enumerate(parts):
        part_progress = None
        if progress_callback is not None:
            def part_progress(completed, total, offset=part_index * len(motion["tracks"])):
                progress_callback(offset + completed, len(parts) * total)

        action, keyed_bones = create_action(part_armature, motion, frame_origin, part_progress)
        if rotation_only_retarget:
            remove_nonroot_translation_curves(action, part_armature)
        keyed += keyed_bones
        if strip_start is not None and duration is not None:
            part_armature.animation_data_create()
            track = part_armature.animation_data.nla_tracks.get("G4 Character Parts")
            if track is None:
                track = part_armature.animation_data.nla_tracks.new()
                track.name = "G4 Character Parts"
            add_nla_strip(
                part_armature,
                track,
                action,
                strip_name or action.name,
                strip_start,
                duration,
            )
            part_armature.animation_data.action = None
        created += 1
    return created, keyed


def remove_nonroot_translation_curves(action, armature) -> int:
    """Avoid rest-pose translation/scale deltas stretching a substituted skeleton."""
    removed = 0
    for curve in action_fcurves(action):
        match = re.fullmatch(r'pose\.bones\["(.+)"\]\.(?:location|scale)', curve.data_path)
        if match is None:
            continue
        bone = armature.data.bones.get(match.group(1))
        if bone is None or bone.parent is None:
            continue
        action_fcurve_remove(action, curve)
        removed += 1
    return removed


def event_root_motion_name(motion: dict, armature=None) -> str:
    """Return the animated character root used as the event world transform."""
    tracks = {track.get("target_name"): track for track in motion.get("tracks") or []}
    skeleton = motion.get("skeleton") or {}
    names = skeleton.get("names") or []
    parents = skeleton.get("parents") or []
    if not names and armature is not None:
        # Event packages often contain hashed targets without an external
        # skeleton block.  The native imported armature is authoritative for
        # the target names and parent links in that case.
        names = [bone.name for bone in armature.data.bones]

    def track_has_motion(track: dict | None) -> bool:
        for path, samples in (track or {}).get("values", {}).items():
            if len(samples) < 2:
                continue
            first = samples[0]
            if path == "rotation" and len(first) == 4:
                if any(
                    abs(abs(sum(value * reference for value, reference in zip(sample, first))) - 1.0) > 1e-6
                    for sample in samples[1:]
                ):
                    return True
                continue
            if any(
                len(sample) != len(first)
                or any(abs(float(value) - float(reference)) > 1e-6 for value, reference in zip(sample, first))
                for sample in samples[1:]
            ):
                return True
        return False

    for index, name in enumerate(names):
        track = tracks.get(name)
        if (
            not re.fullmatch(r"c_c_\d+_0", name or "")
            or track is None
            or not track_has_motion(track)
        ):
            continue
        if parents and index < len(parents):
            parent_index = parents[index]
            parent_name = names[parent_index] if 0 <= parent_index < len(names) else ""
        elif armature is not None:
            bone = armature.data.bones.get(name)
            parent_name = bone.parent.name if bone is not None and bone.parent is not None else ""
        else:
            parent_name = ""
        parent_track = tracks.get(parent_name)
        if not track_has_motion(parent_track):
            return name
    return ""


def extract_event_root_motion(
    action,
    motion: dict,
    root_name: str,
    model_base_matrix: Matrix,
    armature=None,
) -> int:
    """Transfer the root bone delta to the object while preserving its world pose."""
    if not root_name:
        return 0
    track = next((item for item in motion.get("tracks") or [] if item.get("target_name") == root_name), None)
    rest = motion_rest_matrices(motion).get(root_name)
    frames = motion.get("frames") or []
    root_pose_bone = armature.pose.bones.get(root_name) if armature is not None else None
    if track is None or (rest is None and root_pose_bone is None) or not frames:
        return 0
    if rest is None:
        # Without the source rest pose the local-to-object transfer is ambiguous.
        return 0
    root_rest = root_pose_bone.bone.matrix_local.copy() if root_pose_bone is not None else rest.copy()
    blender_rest = blender_local_rest_matrix(root_pose_bone) if root_pose_bone is not None else rest
    basis_correction = retarget_basis_correction(rest, blender_rest)
    parent_track = None
    if root_pose_bone is not None and root_pose_bone.parent is not None:
        parent_track = next((item for item in motion["tracks"]
                             if item.get("target_name") == root_pose_bone.parent.name), None)
    if armature is not None:
        armature.rotation_mode = "QUATERNION"
    locations, rotations, scales = [], [], []
    previous_rotation = None
    for index in range(len(frames)):
        values = track["values"]
        animated_scale = values["scale"][index]
        if parent_track is not None:
            animated_scale = [value / parent if abs(parent) > 1e-9 else value
                              for value, parent in zip(animated_scale, parent_track["values"]["scale"][index])]
        source = source_matrix({
            "translation": values["translation"][index],
            "rotation": values["rotation"][index],
            "scale": animated_scale,
        })
        delta = basis_correction.inverted_safe() @ (rest.inverted_safe() @ source) @ basis_correction
        # Conjugate the same local bone delta used by append_motion_to_action.
        # The object's base already contains the source-to-Blender conversion.
        matrix = model_base_matrix @ root_rest @ delta @ root_rest.inverted_safe()
        location, rotation, scale = matrix.decompose()
        if previous_rotation is not None and previous_rotation.dot(rotation) < 0.0:
            rotation.negate()
        previous_rotation = rotation.copy()
        locations.append(tuple(location))
        rotations.append(tuple(rotation))
        scales.append(tuple(scale))
    cache = {}
    action_frames = [1 + frame - motion["clip"]["start_frame"] for frame in frames]
    for data_path, samples in (("location", locations), ("rotation_quaternion", rotations), ("scale", scales)):
        reduced_frames, reduced_samples = simplify_motion_samples(action_frames, samples, root_name)
        for component in range(len(reduced_samples[0])):
            append_curve_samples(
                action, cache, data_path, component, "Event Root Motion", reduced_frames,
                [sample[component] for sample in reduced_samples],
                owner=armature,
            )
    action["g4_event_root_motion"] = root_name
    action["g4_event_root_transform"] = "local_delta_conjugated_to_armature_object"
    return len(frames)


def has_display_oriented_bones(armature) -> bool:
    return bool(
        armature
        and armature.type == "ARMATURE"
        and any("g4_rest_rotation_xyzw" in bone for bone in armature.data.bones)
    )


def align_armature_to_native_bind(armature, model_path: Path) -> int:
    """Use the model's G4SK bind matrices as the animation rig rest axes."""
    try:
        if model_path.suffix.casefold() == ".g4sk":
            skeleton_data = model_path.read_bytes()
        else:
            configure_raw_data_root_from_path(model_path)
            skeleton_data, _source = find_skeleton_for_model(model_path)
        skeleton = parse_g4sk(skeleton_data) if skeleton_data else None
    except (OSError, ValueError, RuntimeError, struct.error):
        return 0
    if not skeleton:
        return 0
    names = skeleton.get("names") or []
    matrices = skeleton.get("bind_matrices") or []
    if not names or not matrices:
        return 0

    previous_active = bpy.context.view_layer.objects.active
    previous_mode = bpy.context.object.mode if bpy.context.object is not None else "OBJECT"
    if previous_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    applied = 0
    try:
        bpy.context.view_layer.objects.active = armature
        armature.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        for name, values in zip(names, matrices):
            edit_bone = armature.data.edit_bones.get(name)
            if edit_bone is None or len(values) != 16:
                continue
            edit_bone.matrix = Matrix(
                (values[0:4], values[4:8], values[8:12], values[12:16])
            )
            applied += 1
        bpy.ops.object.mode_set(mode="OBJECT")
        armature["g4_animation_bind_axes"] = applied
    finally:
        if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        if previous_active is not None:
            bpy.context.view_layer.objects.active = previous_active
    return applied


def import_model_for_animation(
    g4mt_path: Path,
    motion: dict,
    prefs,
    model_path: Path | None = None,
    import_character_parts: bool = True,
    auto_character_parts: bool = True,
    body_model: str = "",
    shoes_model: str = "",
    accessory_model: str = "",
    gloves_model: str = "",
    armband_model: str = "",
    nameplate_model: str = "",
    attach_ball: bool = False,
    ball_model: str = "",
    character_part_stem: str = "",
    align_to_motion_rest: bool = True,
    force_character_styling: bool = False,
):
    if model_path is None:
        raise RuntimeError("Select the G4MD/G4PKM model that will receive the animation.")
    importer = model_importer_module()
    import_model = getattr(importer, "import_g4_model", None) if importer is not None else None
    if import_model is None:
        raise RuntimeError("The Level-5 G4 Model Importer does not expose its model import function")
    model_preferences = getattr(importer, "addon_preferences", None)
    model_prefs = model_preferences() if model_preferences is not None else prefs
    original_orientation = getattr(model_prefs, "apply_bone_orientation", False)
    try:
        model_prefs.apply_bone_orientation = False
        _, imported_names = import_model(
            model_path,
            model_prefs,
            False,
            apply_styling=True if force_character_styling else None,
        )
    finally:
        model_prefs.apply_bone_orientation = original_orientation
    imported = {obj for name in imported_names if (obj := bpy.data.objects.get(name)) is not None}
    armature = best_armature(imported, track_bone_names(motion))
    if armature is None:
        raise RuntimeError(f"The imported model contains no usable armature: {model_path}")
    align_armature_to_native_bind(armature, model_path)
    resolve_track_names_from_armature(motion, armature)
    if align_to_motion_rest:
        align_armature_to_motion_rest(armature, motion)
    if import_character_parts:
        importer = model_importer_module()
        attach_parts = getattr(importer, "import_character_parts_for_armature", None) if importer is not None else None
        model_preferences = getattr(importer, "addon_preferences", None) if importer is not None else None
        if attach_parts is None:
            raise RuntimeError("The Level-5 G4 Model Importer cannot attach character parts")
        model_prefs = model_preferences() if model_preferences is not None else prefs
        attach_parts(
            model_path,
            armature,
            model_prefs,
            auto_character_parts,
            body_model,
            shoes_model,
            accessory_model,
            gloves_model,
            armband_model,
            nameplate_model,
            False,
            character_part_stem,
            False,
        )
        if attach_ball:
            find_ball = getattr(importer, "find_default_ball_model", None)
            attach_ball_to_armature = getattr(importer, "attach_ball_to_armature", None)
            configured_ball = Path(bpy.path.abspath(ball_model)) if ball_model else None
            selected_ball = configured_ball if configured_ball is not None and configured_ball.is_file() else (
                find_ball(model_path, model_prefs) if find_ball is not None else None
            )
            if selected_ball is None or attach_ball_to_armature is None:
                raise RuntimeError("Default ball model b000001 was not found")
            attach_ball_to_armature(selected_ball, armature, model_prefs, False)
    return armature, model_path


def source_matrix(transform: dict) -> Matrix:
    translation = Vector(transform["translation"])
    x, y, z, w = transform["rotation"]
    rotation = Quaternion((w, x, y, z))
    scale = Vector(transform["scale"])
    return Matrix.LocRotScale(translation, rotation, scale)


def retarget_basis_correction(source_rest: Matrix, target_rest: Matrix) -> Matrix:
    correction = source_rest.inverted_safe() @ target_rest
    correction.translation = (0.0, 0.0, 0.0)
    return correction


def clear_pose(armature) -> None:
    for pose_bone in armature.pose.bones:
        pose_bone.location = (0.0, 0.0, 0.0)
        pose_bone.rotation_mode = "QUATERNION"
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.scale = (1.0, 1.0, 1.0)


def align_armature_to_motion_rest(armature, motion: dict) -> int:
    local_rest = motion_rest_matrices(motion)
    global_rest = {}

    def resolve_global(bone):
        cached = global_rest.get(bone.name)
        if cached is not None:
            return cached
        local = local_rest.get(bone.name)
        if local is None:
            return None
        parent = resolve_global(bone.parent) if bone.parent is not None else None
        result = parent @ local if parent is not None else local
        global_rest[bone.name] = result
        return result

    for bone in armature.data.bones:
        resolve_global(bone)
    if not global_rest:
        return 0

    previous_active = bpy.context.view_layer.objects.active
    previous_mode = bpy.context.object.mode if bpy.context.object is not None else "OBJECT"
    if previous_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    lengths = {bone.name: bone.length for bone in armature.data.bones}
    applied = 0
    try:
        bpy.context.view_layer.objects.active = armature
        armature.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        for bone in armature.data.edit_bones:
            source = global_rest.get(bone.name)
            if source is None:
                continue
            matrix = source.to_quaternion().to_matrix().to_4x4()
            matrix.translation = source.translation
            bone.matrix = matrix
            bone.length = lengths[bone.name]
            applied += 1
        bpy.ops.object.mode_set(mode="OBJECT")
        armature["g4_animation_rest_axes"] = applied
        for bone in armature.data.bones:
            bone.inherit_scale = "FULL"
        armature.pop("g4_facial_scale_compensation", None)
        armature.pop("g4_terminal_scale_compensation", None)
        armature["g4_cumulative_scale_conversion"] = True
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
    return applied


def motion_rest_matrices(motion: dict) -> dict[str, Matrix]:
    skeleton = motion.get("skeleton") or {}
    skeleton_names = skeleton.get("names", [])
    rest_transforms = skeleton.get("local_trs", [])
    return {
        name: source_matrix(rest_transforms[index])
        for index, name in enumerate(skeleton_names)
        if name and index < len(rest_transforms)
    }


def blender_local_rest_matrix(pose_bone) -> Matrix:
    bone = pose_bone.bone
    if bone.parent is None:
        return bone.matrix_local.copy()
    return bone.parent.matrix_local.inverted_safe() @ bone.matrix_local


def append_curve_samples(action, cache: dict, data_path: str, index: int, group: str, frames, values, owner=None) -> None:
    if len(values) > 2 and max(values) - min(values) <= 1e-9:
        frames = (frames[0], frames[-1])
        values = (values[0], values[-1])
    key = (data_path, index)
    if key not in cache:
        cache[key] = action_fcurve_find(action, data_path, index)
    curve = cache[key]
    if curve is None:
        curve = action_fcurve_new(action, owner, data_path, index, group)
        cache[key] = curve
    count = len(frames)
    if not count:
        return
    start = len(curve.keyframe_points)
    curve.keyframe_points.add(count)
    coordinates = [component for pair in zip(frames, values) for component in pair]
    if start == 0:
        curve.keyframe_points.foreach_set("co", coordinates)
        curve.keyframe_points.foreach_set("interpolation", [1] * count)
    else:
        for point, frame, value in zip(curve.keyframe_points[start:], frames, values):
            point.co = (frame, value)
            point.interpolation = "LINEAR"
    curve.update()


def append_motion_to_action(
    armature,
    motion: dict,
    action,
    frame_origin: int,
    curve_cache: dict | None = None,
    progress_callback=None,
    event_root_name: str | None = None,
) -> tuple[int, int]:
    cache = curve_cache if curve_cache is not None else {}
    cache.update({
        (curve.data_path, curve.array_index): curve
        for curve in action_fcurves(action)
    })
    rest_by_name = motion_rest_matrices(motion)
    frames = motion["frames"]
    action_frames = [1 + source_frame - frame_origin for source_frame in frames]
    tracks_by_name = {
        track.get("target_name"): track
        for track in motion["tracks"]
        if track.get("target_name")
    }
    skeleton = motion.get("skeleton") or {}
    skeleton_names = skeleton.get("names") or []
    skeleton_parents = skeleton.get("parents") or []
    parent_by_name = {}
    for index, name in enumerate(skeleton_names):
        parent_index = skeleton_parents[index] if index < len(skeleton_parents) else -1
        if 0 <= parent_index < len(skeleton_names) and parent_index != index:
            parent_by_name[name] = skeleton_names[parent_index]
    keyed_bones = 0
    relative_rest_tracks = 0
    track_count = len(motion["tracks"])
    for track_index, track in enumerate(motion["tracks"], start=1):
        name = track.get("target_name")
        if not isinstance(name, str) or not name:
            if progress_callback is not None:
                progress_callback(track_index, track_count)
            continue
        # Only the event importer may explicitly transfer this track to the
        # object. Normal animation imports must retain the root bone curves.
        if event_root_name and name == event_root_name:
            if progress_callback is not None:
                progress_callback(track_index, track_count)
            continue
        rest = rest_by_name.get(name)
        target_names = track.get("armature_bone_names") or (name,)
        pose_bones = [armature.pose.bones[target] for target in target_names if target in armature.pose.bones]
        if not pose_bones:
            if progress_callback is not None:
                progress_callback(track_index, track_count)
            continue
        if rest is None and frames:
            rest = source_matrix(
                {
                    "translation": track["values"]["translation"][0],
                    "rotation": track["values"]["rotation"][0],
                    "scale": track["values"]["scale"][0],
                }
            )
            relative_rest_tracks += 1
        if rest is None:
            if progress_callback is not None:
                progress_callback(track_index, track_count)
            continue
        parent_track = tracks_by_name.get(parent_by_name.get(name))
        primary_pose_bone = next((bone for bone in pose_bones if bone.name == name), pose_bones[0])
        blender_rest = blender_local_rest_matrix(primary_pose_bone)
        basis_correction = retarget_basis_correction(rest, blender_rest)
        locations = []
        rotations = []
        scales = []
        previous_rotation = None
        for index, source_frame in enumerate(frames):
            animated_scale = track["values"]["scale"][index]
            if parent_track is not None:
                parent_scale = parent_track["values"]["scale"][index]
                animated_scale = [
                    value / parent_value if abs(parent_value) > 1e-9 else value
                    for value, parent_value in zip(animated_scale, parent_scale)
                ]
            animated = source_matrix(
                {
                    "translation": track["values"]["translation"][index],
                    "rotation": track["values"]["rotation"][index],
                    "scale": animated_scale,
                }
            )
            source_delta = rest.inverted_safe() @ animated
            blender_delta = basis_correction.inverted_safe() @ source_delta @ basis_correction
            location, rotation, scale = blender_delta.decompose()
            if previous_rotation is not None and previous_rotation.dot(rotation) < 0.0:
                rotation.negate()
            previous_rotation = rotation.copy()
            locations.append(tuple(location))
            rotations.append(tuple(rotation))
            scales.append(tuple(scale))
        for pose_bone in pose_bones:
            pose_bone.rotation_mode = "QUATERNION"
            animated_paths = set(track.get("animated_paths") or ())
            for path_name, data_path, samples in (
                ("translation", pose_bone.path_from_id("location"), locations),
                ("rotation", pose_bone.path_from_id("rotation_quaternion"), rotations),
                ("scale", pose_bone.path_from_id("scale"), scales),
            ):
                if path_name not in animated_paths:
                    continue
                reduced_frames, reduced_samples = simplify_motion_samples(action_frames, samples, name)
                component_count = len(reduced_samples[0]) if reduced_samples else 0
                for component in range(component_count):
                    cache.setdefault((data_path, component), None)
                    append_curve_samples(
                        action,
                        cache,
                        data_path,
                        component,
                        pose_bone.name,
                        reduced_frames,
                        [sample[component] for sample in reduced_samples],
                        owner=armature,
                    )
            keyed_bones += 1
        if progress_callback is not None:
            progress_callback(track_index, track_count)
    return keyed_bones, relative_rest_tracks


def create_action(
    armature,
    motion: dict,
    frame_origin: int | None = None,
    progress_callback=None,
    event_root_name: str | None = None,
) -> tuple[bpy.types.Action, int]:
    clip = motion["clip"]
    action = bpy.data.actions.new(name=clip["name"] or "G4MT Animation")
    action.use_fake_user = True
    action.use_frame_range = True
    armature.animation_data_create()
    clear_pose(armature)
    # Blender 5.2 creates layered Actions without the legacy action.fcurves
    # collection. Assign the owner before adding curves so the compatibility
    # helper can create the correct action slot/channel bag.
    armature.animation_data.action = action
    source_start = clip["start_frame"] if frame_origin is None else frame_origin
    action.frame_start = 1 + clip["start_frame"] - source_start
    action.frame_end = action.frame_start + max(0, len(motion["frames"]) - 1)
    keyed_bones, relative_rest_tracks = append_motion_to_action(
        armature,
        motion,
        action,
        source_start,
        progress_callback=progress_callback,
        event_root_name=event_root_name,
    )
    armature.animation_data.action = None
    armature.animation_data.action = action
    slots = getattr(action, "slots", None)
    if slots is not None and len(slots):
        armature.animation_data.action_slot = slots[0]

    action["g4_relative_rest_tracks"] = relative_rest_tracks
    mapping = {
        track["target_name"]: list(track.get("armature_bone_names") or (track["target_name"],))
        for track in motion["tracks"]
        if track.get("target_name")
        and (track.get("armature_bone_names") or track["target_name"] in armature.data.bones)
    }
    action["g4_retarget_mapping"] = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
    action["g4_retarget_unmapped_targets"] = json.dumps(
        sorted(track["target_name"] for track in motion["tracks"]
               if track.get("target_name") and track["target_name"] not in mapping),
        ensure_ascii=False,
    )
    return action, keyed_bones


def create_motion_armature(motion: dict):
    skeleton = motion.get("skeleton") or {}
    names = skeleton.get("names") or []
    parents = skeleton.get("parents") or []
    rest_local = motion_rest_matrices(motion)
    if not names or len(parents) != len(names) or any(name not in rest_local for name in names):
        raise ValueError("G4 animation skeleton has incomplete rest transforms or parent indices")
    if len(set(names)) != len(names):
        raise ValueError("G4 animation skeleton contains duplicate joint names")

    global_rest = {}
    resolving = set()

    def resolve(index):
        name = names[index]
        if name in global_rest:
            return global_rest[name]
        if name in resolving:
            raise ValueError(f"G4 animation skeleton contains a parent cycle at {name}")
        resolving.add(name)
        parent = parents[index]
        local = rest_local[name]
        if 0 <= parent < len(names) and parent != index:
            matrix = resolve(parent) @ local
        else:
            matrix = local.copy()
        resolving.remove(name)
        global_rest[name] = matrix
        return matrix

    children = {name: [] for name in names}
    for index, parent in enumerate(parents):
        if 0 <= parent < len(names) and parent != index:
            children[names[parent]].append(names[index])
    for index in range(len(names)):
        resolve(index)

    armature_data = bpy.data.armatures.new("G4 Motion Skeleton")
    armature = bpy.data.objects.new("G4 Motion Skeleton", armature_data)
    bpy.context.collection.objects.link(armature)
    previous_active = bpy.context.view_layer.objects.active
    previous_selected = tuple(bpy.context.selected_objects)
    previous_mode = previous_active.mode if previous_active is not None else "OBJECT"
    if previous_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in previous_selected:
        obj.select_set(False)
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = {name: armature_data.edit_bones.new(name) for name in names}
        for index, name in enumerate(names):
            parent = parents[index]
            if 0 <= parent < len(names) and parent != index:
                edit_bones[name].parent = edit_bones[names[parent]]

        def bone_depth(name):
            depth = 0
            parent = parents[names.index(name)]
            while 0 <= parent < len(names) and parent != names.index(name):
                depth += 1
                name = names[parent]
                parent = parents[names.index(name)]
            return depth

        for name in sorted(names, key=bone_depth):
            bone = edit_bones[name]
            bone.head = global_rest[name].translation
            bone.tail = global_rest[name].translation + global_rest[name].to_3x3() @ Vector((0.0, 0.05, 0.0))
            bone.matrix = global_rest[name]
            lengths = [
                (global_rest[child].translation - global_rest[name].translation).length
                for child in children[name]
            ]
            bone.length = max((length for length in lengths if length > 1e-4), default=0.05)
        bpy.ops.object.mode_set(mode="OBJECT")
        armature.matrix_world = blender_from_g4_matrix()
    except Exception:
        if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.data.objects.remove(armature, do_unlink=True)
        bpy.data.armatures.remove(armature_data)
        raise
    finally:
        if armature.name in bpy.data.objects:
            armature.select_set(False)
        for obj in previous_selected:
            if obj.name in bpy.context.view_layer.objects:
                obj.select_set(True)
        if previous_active is not None:
            bpy.context.view_layer.objects.active = previous_active
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
    return armature


def create_g4sk_armature(skeleton_path: Path):
    skeleton = parse_g4sk(skeleton_path.read_bytes())
    names = skeleton["names"]
    local_trs = [
        {
            "translation": skeleton["local_translations"][index],
            "rotation": skeleton["local_rotations_xyzw"][index],
            "scale": skeleton["local_scales"][index],
        }
        for index in range(skeleton["joint_count"])
    ]
    parents = [
        parent if parent < skeleton["joint_count"] and parent != index else -1
        for index, parent in enumerate(skeleton["parent_indices"])
    ]
    return create_motion_armature({
        "skeleton": {"names": names, "parents": parents, "local_trs": local_trs}
    })


def create_victorytool_target_armature(
    skeleton_path: Path, model_path: Path, source_joint_mapping: dict[str, str],
):
    armature = create_g4sk_armature(skeleton_path)
    armature.name = f"{model_path.stem}__AnimationExport"
    armature["g4_character_model_source"] = str(model_path.resolve())
    armature["g4_character_skeleton_source"] = str(skeleton_path.resolve())
    armature["g4_cumulative_scale_conversion"] = True
    for bone_name, source_name in source_joint_mapping.items():
        bone = armature.data.bones.get(bone_name)
        if bone is None:
            raise ValueError(f"Animation mapping refers to a missing target joint: {bone_name}")
        bone["g4_animation_source_joint"] = source_name
    return armature


def requires_semantic_retarget(motion: dict, armature, source_rig_differs: bool) -> bool:
    if source_rig_differs:
        return True
    if armature.get("g4_character_model_source") or armature.get("g4_character_skeleton_source"):
        return False
    return any(
        track.get("target_name")
        and any(name != track["target_name"] for name in track.get("armature_bone_names") or ())
        for track in motion["tracks"]
    )


def create_retargeted_action(armature, motion: dict, frame_origin: int, collect_g4mt_samples: bool):
    source_armature = create_motion_armature(motion)
    source_motion = dict(motion)
    source_motion["tracks"] = [dict(track) for track in motion["tracks"]]
    resolve_track_names_from_armature(source_motion, source_armature)
    action = None
    try:
        source_bones = list(source_armature.data.bones)

        def depth(bone):
            value = 0
            parent = bone.parent
            while parent is not None:
                value += 1
                parent = parent.parent
            return value

        source_bones.sort(key=depth)
        source_tracks = {
            track["target_name"]: track
            for track in source_motion["tracks"]
            if track.get("target_name")
        }
        source_keyed = sum(
            bool(track.get("armature_bone_names"))
            and source_armature.data.bones.get(track["target_name"]) is not None
            for track in source_motion["tracks"]
            if track.get("target_name")
        )
        if source_keyed == 0:
            raise ValueError("G4 animation has no tracks on its source skeleton")

        source_to_target = {
            track["target_name"]: tuple(track.get("armature_bone_names") or ())
            for track in motion["tracks"]
            if track.get("target_name") and track.get("armature_bone_names")
        }
        target_to_source = {}
        for source_name, target_names in source_to_target.items():
            for target_name in target_names:
                previous_source = target_to_source.get(target_name)
                if previous_source is not None and previous_source != source_name:
                    raise ValueError(f"Target bone {target_name} is assigned to multiple G4 joints")
                target_to_source[target_name] = source_name

        target_bones = list(armature.data.bones)
        target_bones.sort(key=depth)
        source_local_rest = {
            bone.name: bone.parent.matrix_local.inverted_safe() @ bone.matrix_local
            if bone.parent else bone.matrix_local.copy()
            for bone in source_bones
        }
        rest_local = {
            bone.name: bone.parent.matrix_local.inverted_safe() @ bone.matrix_local
            if bone.parent else bone.matrix_local.copy()
            for bone in target_bones
        }
        source_world_rotation = source_armature.matrix_world.to_quaternion()
        target_world_rotation = armature.matrix_world.to_quaternion()
        target_inverse_rotation = target_world_rotation.inverted()
        target_inverse_transform = target_inverse_rotation.to_matrix()
        aliases = load_joint_alias_catalog().aliases

        def mapped_target_bone(source_name):
            if source_name == "c_global_0_0":
                canonical_root = armature.data.bones.get("c_global_0_0")
                if canonical_root is not None:
                    return canonical_root
            explicit = [
                bone for bone in target_bones
                if str(bone.get("g4_animation_source_joint", "")).strip() == source_name
            ]
            if explicit:
                if len(explicit) == 1:
                    return explicit[0]
                source_bone = source_armature.data.bones.get(source_name)
                source_parent_name = source_bone.parent.name if source_bone and source_bone.parent else None
                direct = [
                    bone for bone in explicit
                    if (target_to_source.get(bone.parent.name) if bone.parent else None) == source_parent_name
                ]
                candidates = direct or explicit
                shallowest = min(depth(bone) for bone in candidates)
                primary = [bone for bone in candidates if depth(bone) == shallowest]
                return primary[0] if len(primary) == 1 else None
            exact = armature.data.bones.get(source_name)
            if exact is not None and str(exact.get("g4_animation_source_joint", "")).strip() in {"", source_name}:
                return exact
            tracked = [bone for bone in target_bones if target_to_source.get(bone.name) == source_name]
            if len(tracked) == 1:
                return tracked[0]
            if tracked:
                return None
            aliased = [
                bone for bone in target_bones
                if not str(bone.get("g4_animation_source_joint", "")).strip()
                and aliases.get(normalize_joint_key(bone.name)) == source_name
            ]
            return aliased[0] if len(aliased) == 1 else None

        translation_targets = {
            source_name: mapped_target_bone(source_name)
            for source_name in set(target_to_source.values())
        }
        source_root = source_armature.data.bones.get("c_global_0_0")
        source_pelvis = source_armature.data.bones.get("c_c_1_0")
        target_root = translation_targets.get("c_global_0_0")
        target_pelvis = translation_targets.get("c_c_1_0")
        height_joint_names = (
            "c_global_0_0", "c_c_1_0", "c_c_1_1", "c_n_1_0", "c_head_1_0", "c_atc01_1_0",
            "l_l_1_0", "l_l_1_1", "l_foot_1_0", "l_foot_1_1",
            "r_l_1_0", "r_l_1_1", "r_foot_1_0", "r_foot_1_1",
        )
        height_points = [
            (
                source_armature.matrix_world @ source_bone.matrix_local.translation,
                armature.matrix_world @ target_bone.matrix_local.translation,
            )
            for source_name in height_joint_names
            if (source_bone := source_armature.data.bones.get(source_name)) is not None
            and (target_bone := mapped_target_bone(source_name)) is not None
        ]
        source_height = max((source.z for source, _target in height_points), default=0.0) - min(
            (source.z for source, _target in height_points), default=0.0
        )
        target_height = max((_target.z for _source, _target in height_points), default=0.0) - min(
            (_target.z for _source, _target in height_points), default=0.0
        )
        body_translation_scale = target_height / source_height if source_height > 1e-6 else 1.0
        if source_root is not None and source_pelvis is not None and target_root is not None and target_pelvis is not None:
            source_pelvis_height = (
                source_armature.matrix_world @ source_pelvis.matrix_local.translation
                - source_armature.matrix_world @ source_root.matrix_local.translation
            ).length
            target_pelvis_height = (
                armature.matrix_world @ target_pelvis.matrix_local.translation
                - armature.matrix_world @ target_root.matrix_local.translation
            ).length
            pelvis_translation_scale = (
                target_pelvis_height / source_pelvis_height
                if source_pelvis_height > 1e-6 else body_translation_scale
            )
        else:
            pelvis_translation_scale = body_translation_scale
        source_rest = motion_rest_matrices(motion)
        source_basis_correction = {}
        for bone in source_bones:
            rest = source_rest.get(bone.name)
            track = source_tracks.get(bone.name)
            if rest is None and track is not None and motion["frames"]:
                rest = source_matrix({
                    "translation": track["values"]["translation"][0],
                    "rotation": track["values"]["rotation"][0],
                    "scale": track["values"]["scale"][0],
                })
            if rest is not None:
                source_rest[bone.name] = rest
                source_basis_correction[bone.name] = retarget_basis_correction(
                    rest, source_local_rest[bone.name]
                )
        source_rest_world = {
            bone.name: source_world_rotation @ bone.matrix_local.to_quaternion()
            for bone in source_bones
        }
        chain_rotation_targets = {}

        def source_is_ancestor(ancestor_name, child_name):
            current = source_armature.data.bones.get(child_name)
            while current is not None:
                if current.name == ancestor_name:
                    return True
                current = current.parent
            return False

        def normalized_chain_distances(points):
            distances = [0.0]
            for first, second in zip(points, points[1:]):
                distances.append(distances[-1] + (second - first).length)
            total = distances[-1]
            if total <= 1e-8:
                denominator = max(1, len(points) - 1)
                return [index / denominator for index in range(len(points))]
            return [distance / total for distance in distances]

        for target_bone in target_bones:
            source_name = target_to_source.get(target_bone.name)
            if source_name is None or target_bone.parent is None:
                continue
            target_anchor = target_bone.parent
            source_anchor_name = target_to_source.get(target_anchor.name)
            while source_anchor_name is None and target_anchor.parent is not None:
                target_anchor = target_anchor.parent
                source_anchor_name = target_to_source.get(target_anchor.name)
            if source_anchor_name is None or source_anchor_name == source_name:
                continue
            if not source_is_ancestor(source_anchor_name, source_name):
                continue
            while (
                target_anchor.parent is not None
                and target_to_source.get(target_anchor.parent.name) == source_anchor_name
            ):
                target_anchor = target_anchor.parent

            target_path = []
            current_target = target_bone
            while current_target is not None and current_target.name != target_anchor.name:
                target_path.append(current_target)
                current_target = current_target.parent
            if current_target is None or not target_path:
                continue
            target_path.reverse()

            source_path = []
            current_source = source_armature.data.bones.get(source_name)
            while current_source is not None:
                source_path.append(current_source)
                if current_source.name == source_anchor_name:
                    break
                current_source = current_source.parent
            if not source_path or source_path[-1].name != source_anchor_name:
                continue
            source_path.reverse()

            target_points = [target_anchor.matrix_local.translation.copy()]
            target_points.extend(bone.matrix_local.translation.copy() for bone in target_path)
            target_fractions = normalized_chain_distances(target_points)[1:]
            source_points = [bone.matrix_local.translation.copy() for bone in source_path]
            source_fractions = normalized_chain_distances(source_points)
            for bone, target_fraction in zip(target_path, target_fractions):
                chain_rotation_targets[bone.name] = (
                    tuple(source_bone.name for source_bone in source_path),
                    tuple(source_fractions),
                    target_fraction,
                )
        frame_start = 1 + motion["clip"]["start_frame"] - frame_origin
        frame_end = frame_start + max(0, len(motion["frames"]) - 1)
        action_frames = range(round(frame_start), round(frame_end) + 1)
        target_samples = None if collect_g4mt_samples else {}
        if target_samples is not None:
            action = bpy.data.actions.new(name=motion["clip"]["name"] or "G4 Retargeted Animation")
            action.use_fake_user = True
            action.use_frame_range = True
            action.frame_start = frame_start
            action.frame_end = frame_end
            armature.animation_data_create()
            armature.animation_data.action = action
        for bone in target_bones:
            source_name = target_to_source.get(bone.name)
            if source_name not in source_rest_world:
                continue
            if target_samples is not None:
                armature.pose.bones[bone.name].rotation_mode = "QUATERNION"
                target_samples[bone.name] = ([], [], [])
        g4mt_samples = (
            {bone.name: [] for bone in target_bones if bone.name in target_to_source}
            if collect_g4mt_samples else None
        )

        skeleton = motion.get("skeleton") or {}
        skeleton_names = skeleton.get("names") or []
        skeleton_parents = skeleton.get("parents") or []
        source_parent_names = {}
        for index, name in enumerate(skeleton_names):
            parent_index = skeleton_parents[index] if index < len(skeleton_parents) else -1
            if 0 <= parent_index < len(skeleton_names) and parent_index != index:
                source_parent_names[name] = skeleton_names[parent_index]
        previous_rotations = {}
        # Compose source poses from decoded channels instead of evaluating a source Action per frame.
        for frame_index, _frame in enumerate(action_frames):
            source_pose_matrices = {}
            for bone in source_bones:
                basis = Matrix.Identity(4)
                track = source_tracks.get(bone.name)
                correction = source_basis_correction.get(bone.name)
                if track is not None and correction is not None:
                    values = track["values"]
                    animated_scale = list(values["scale"][frame_index])
                    parent_track = source_tracks.get(source_parent_names.get(bone.name))
                    if parent_track is not None:
                        parent_scale = parent_track["values"]["scale"][frame_index]
                        animated_scale = [
                            value / parent_value if abs(parent_value) > 1e-9 else value
                            for value, parent_value in zip(animated_scale, parent_scale)
                        ]
                    animated = source_matrix({
                        "translation": values["translation"][frame_index],
                        "rotation": values["rotation"][frame_index],
                        "scale": animated_scale,
                    })
                    source_delta = source_rest.get(bone.name, animated).inverted_safe() @ animated
                    blender_delta = correction.inverted_safe() @ source_delta @ correction
                    location, rotation, scale = blender_delta.decompose()
                    previous_rotation = previous_rotations.get(bone.name)
                    if previous_rotation is not None and previous_rotation.dot(rotation) < 0.0:
                        rotation.negate()
                    previous_rotations[bone.name] = rotation.copy()
                    animated_paths = set(track.get("animated_paths") or ())
                    if "translation" not in animated_paths:
                        location = Vector((0.0, 0.0, 0.0))
                    if "rotation" not in animated_paths:
                        rotation = Quaternion((1.0, 0.0, 0.0, 0.0))
                    if "scale" not in animated_paths:
                        scale = Vector((1.0, 1.0, 1.0))
                    basis = Matrix.LocRotScale(location, rotation, scale)

                parent_pose = source_pose_matrices.get(bone.parent.name) if bone.parent else None
                local_rest = source_local_rest[bone.name]
                source_pose_matrices[bone.name] = (
                    parent_pose @ local_rest @ basis if parent_pose is not None
                    else local_rest @ basis
                )

            source_world_deltas = {}
            for source_name, source_rest_rotation in source_rest_world.items():
                source_pose = source_pose_matrices.get(source_name)
                if source_pose is None:
                    continue
                source_pose_world = source_world_rotation @ source_pose.to_quaternion()
                source_world_deltas[source_name] = source_pose_world @ source_rest_rotation.inverted()

            def interpolated_source_delta(source_names, source_fractions, target_fraction):
                if len(source_names) == 1:
                    return source_world_deltas[source_names[0]]
                for index in range(1, len(source_fractions)):
                    if target_fraction <= source_fractions[index]:
                        lower = source_fractions[index - 1]
                        upper = source_fractions[index]
                        weight = (target_fraction - lower) / max(upper - lower, 1e-8)
                        return source_world_deltas[source_names[index - 1]].slerp(
                            source_world_deltas[source_names[index]], weight
                        )
                return source_world_deltas[source_names[-1]]

            pose_matrices = {}
            for bone in target_bones:
                local_rest = rest_local[bone.name]
                parent_pose = pose_matrices.get(bone.parent.name) if bone.parent else None
                inherited = parent_pose @ local_rest if parent_pose is not None else local_rest.copy()
                source_name = target_to_source.get(bone.name)
                source_pose = source_pose_matrices.get(source_name) if source_name else None
                has_source_pose = source_pose is not None and source_name in source_world_deltas
                chain_rotation = chain_rotation_targets.get(bone.name)
                if has_source_pose or chain_rotation is not None:
                    target_rest_world = target_world_rotation @ bone.matrix_local.to_quaternion()
                    if chain_rotation is not None:
                        source_names, source_fractions, target_fraction = chain_rotation
                        source_world_delta = interpolated_source_delta(
                            source_names, source_fractions, target_fraction
                        )
                    else:
                        source_world_delta = source_world_deltas[source_name]
                    desired_world = source_world_delta @ target_rest_world
                    desired_local = target_inverse_rotation @ desired_world
                    location, _rotation, scale = inherited.decompose()
                    if has_source_pose:
                        source_bone = source_armature.data.bones[source_name]
                        source_parent = source_bone.parent
                        if source_parent is not None:
                            source_parent_pose = source_pose_matrices[source_parent.name]
                            source_local_pose = source_parent_pose.inverted_safe() @ source_pose
                            source_parent_world = source_world_rotation @ source_parent_pose.to_quaternion()
                        else:
                            source_local_pose = source_pose
                            source_parent_world = source_world_rotation
                        source_translation_delta = (
                            source_local_pose.translation - source_local_rest[source_name].translation
                        )
                        carries_root_translation = (
                            source_name in {"c_global_0_0", "c_c_1_0"} or source_parent is None
                        ) and translation_targets.get(source_name) == bone
                        if carries_root_translation:
                            target_translation_delta = target_inverse_transform @ (
                                source_parent_world @ source_translation_delta
                            )
                            translation_scale = (
                                pelvis_translation_scale
                                if source_name == "c_c_1_0" else body_translation_scale
                            )
                            location += target_translation_delta * translation_scale
                    pose_matrix = Matrix.LocRotScale(location, desired_local, scale)
                else:
                    pose_matrix = inherited
                pose_matrices[bone.name] = pose_matrix
                if target_samples is not None and source_pose is not None and source_name in source_rest_world:
                    parent_pose = pose_matrices.get(bone.parent.name) if bone.parent else None
                    local_pose = parent_pose.inverted_safe() @ pose_matrix if parent_pose is not None else pose_matrix
                    basis = local_rest.inverted_safe() @ local_pose
                    location, rotation, scale = basis.decompose()
                    channels = target_samples[bone.name]
                    channels[0].append(tuple(location))
                    channels[1].append(tuple(rotation))
                    channels[2].append(tuple(scale))
            if g4mt_samples is not None:
                for bone in target_bones:
                    if bone.name not in g4mt_samples:
                        continue
                    parent_pose = pose_matrices.get(bone.parent.name) if bone.parent else None
                    local_pose = (
                        parent_pose.inverted_safe() @ pose_matrices[bone.name]
                        if parent_pose is not None else pose_matrices[bone.name].copy()
                    )
                    location, rotation, scale = local_pose.decompose()
                    g4mt_samples[bone.name].append(G4MTLocalTransform(
                        translation=tuple(location),
                        rotation_xyzw=(rotation.x, rotation.y, rotation.z, rotation.w),
                        scale=tuple(scale),
                    ))
        if target_samples is not None:
            curve_cache = {}
            curve_samples = []
            frames = tuple(action_frames)
            for bone_name, channels in target_samples.items():
                pose_bone = armature.pose.bones[bone_name]
                for data_path, samples in zip(("location", "rotation_quaternion", "scale"), channels):
                    path = pose_bone.path_from_id(data_path)
                    for component in range(len(samples[0])):
                        curve_cache[(path, component)] = None
                        curve_samples.append((bone_name, path, component, samples))
            for bone_name, path, component, samples in curve_samples:
                append_curve_samples(
                    action,
                    curve_cache,
                    path,
                    component,
                    bone_name,
                    frames,
                    [sample[component] for sample in samples],
                    owner=armature,
                )

            slots = getattr(action, "slots", None)
            if slots is not None and len(slots):
                armature.animation_data.action_slot = slots[0]

            action["g4_retarget_mapping"] = json.dumps(
                {name: list(targets) for name, targets in source_to_target.items()},
                ensure_ascii=False,
                sort_keys=True,
            )
            action["g4_retarget_unmapped_targets"] = json.dumps(
                sorted(track["target_name"] for track in motion["tracks"]
                       if track.get("target_name") and track["target_name"] not in source_to_target),
                ensure_ascii=False,
            )
        g4mt_targets = None
        if g4mt_samples is not None:
            g4mt_target_bones = [bone for bone in target_bones if bone.name in g4mt_samples]
            g4mt_target_indices = {
                bone.name: index for index, bone in enumerate(g4mt_target_bones)
            }
            g4mt_targets = tuple(
                G4MTTargetSamples(
                    bone.name,
                    g4mt_target_indices.get(bone.parent.name) if bone.parent else None,
                    tuple(g4mt_samples[bone.name]),
                )
                for bone in g4mt_target_bones
            )
        return action, len(target_to_source), g4mt_targets
    except Exception:
        if action is not None and action.name in bpy.data.actions:
            bpy.data.actions.remove(action, do_unlink=True)
        raise
    finally:
        armature_data = source_armature.data
        bpy.data.objects.remove(source_armature, do_unlink=True)
        if armature_data.name in bpy.data.armatures:
            bpy.data.armatures.remove(armature_data)


def g4mt_import_settings(operator) -> str:
    return json.dumps(
        {
            "entry": operator.entry,
            "clip": operator.clip,
            "import_all_clips": operator.import_all_clips,
            "import_camera": operator.import_camera,
            "set_active_camera": operator.set_active_camera,
            "reuse_selected_armature": operator.reuse_selected_armature,
            "source_skeleton_path": operator.source_skeleton_path,
            "set_scene_fps": operator.set_scene_fps,
        }
    )


def tag_g4mt_action(action, source: str, clip: dict, rig_id: str, family_id: str,
                    package_entry_name: str | None, part_armature=None) -> None:
    action["g4mt_source"] = source
    action["g4mt_clip_index"] = clip["index"]
    action["g4mt_clip_name"] = clip["name"]
    action["g4mt_rig_id"] = rig_id
    action["g4mt_family_import_id"] = family_id
    if package_entry_name is not None:
        action["g4pk_entry"] = package_entry_name
    if part_armature is not None:
        action["g4mt_part_armature"] = part_armature.name


def tag_character_part_actions(actor, motion: dict, source: str, rig_id: str,
                               family_id: str, package_entry_name: str | None) -> None:
    for part in character_part_armatures(actor):
        action = part.animation_data.action if part.animation_data else None
        if action is not None:
            tag_g4mt_action(
                action,
                source,
                motion["clip"],
                rig_id,
                family_id,
                package_entry_name,
                part,
            )


def imported_g4mt_family_actions(armature) -> list:
    action = armature.animation_data.action if armature and armature.animation_data else None
    if action is None or action.get("g4mt_part_armature"):
        return []
    family_id = action.get("g4mt_family_import_id")
    rig_id = action.get("g4mt_rig_id")
    source = action.get("g4mt_source")
    if not family_id or not rig_id or not source:
        return []
    entry_name = action.get("g4pk_entry")
    return sorted(
        (
            candidate for candidate in bpy.data.actions
            if candidate.get("g4mt_family_import_id") == family_id
            and candidate.get("g4mt_rig_id") == rig_id
            and candidate.get("g4mt_source") == source
            and candidate.get("g4pk_entry") == entry_name
            and not candidate.get("g4mt_part_armature")
        ),
        key=lambda candidate: candidate.get("g4mt_clip_index", -1),
    )


_G4MT_ACTION_MENU_ITEMS = ()
_G4MT_ACTION_MENU_NAMES = {}


def g4mt_action_menu_items(_operator, context):
    global _G4MT_ACTION_MENU_ITEMS
    global _G4MT_ACTION_MENU_NAMES
    actions = imported_g4mt_family_actions(context.active_object)
    _G4MT_ACTION_MENU_NAMES = {
        f"CLIP_{index}": action.name
        for index, action in enumerate(actions)
    }
    _G4MT_ACTION_MENU_ITEMS = tuple(
        (
            f"CLIP_{index}",
            f"{action.get('g4mt_clip_index', '?')}: {action.get('g4mt_clip_name', action.name)}",
            f"Clip {action.get('g4mt_clip_index', '?')} from the imported G4MT bank",
            "ACTION",
            index,
        )
        for index, action in enumerate(actions)
    ) or (("__NONE", "No imported clips", "Import a G4MT bank first", "INFO", 0),)
    return _G4MT_ACTION_MENU_ITEMS


def assign_action_with_slot(armature, action) -> None:
    animation_data = armature.animation_data_create()
    animation_data.action = action
    slots = getattr(action, "slots", None)
    if slots is not None and len(slots):
        animation_data.action_slot = slots[0]


class VIEW3D_OT_level5_g4mt_select_action(Operator):
    bl_idname = "view3d.level5_g4mt_select_action"
    bl_label = "Select Imported G4MT Clip"

    action_name: EnumProperty(name="Animation", items=g4mt_action_menu_items)

    def execute(self, context):
        armature = context.active_object
        family = imported_g4mt_family_actions(armature)
        action = bpy.data.actions.get(_G4MT_ACTION_MENU_NAMES.get(self.action_name, ""))
        if action is None or action not in family:
            self.report({"ERROR"}, "The selected action is no longer part of this imported bank")
            return {"CANCELLED"}

        assign_action_with_slot(armature, action)
        for part in character_part_armatures(armature):
            part_action = next(
                (
                    candidate for candidate in bpy.data.actions
                    if candidate.get("g4mt_family_import_id") == action.get("g4mt_family_import_id")
                    and candidate.get("g4mt_clip_index") == action.get("g4mt_clip_index")
                    and candidate.get("g4mt_part_armature") == part.name
                ),
                None,
            )
            if part_action is not None:
                assign_action_with_slot(part, part_action)

        context.scene.frame_set(context.scene.frame_current)
        self.report({"INFO"}, f"Active G4MT clip: {action.get('g4mt_clip_name', action.name)}")
        return {"FINISHED"}


class VIEW3D_PT_level5_g4mt_bank(Panel):
    bl_label = "Imported G4MT Bank"
    bl_idname = "VIEW3D_PT_level5_g4mt_bank"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "G4"

    @classmethod
    def poll(cls, context):
        return len(imported_g4mt_family_actions(context.active_object)) > 0

    def draw(self, context):
        armature = context.active_object
        actions = imported_g4mt_family_actions(armature)
        current = armature.animation_data.action
        current_index = actions.index(current) + 1 if current in actions else 0
        self.layout.operator_menu_enum(
            VIEW3D_OT_level5_g4mt_select_action.bl_idname,
            "action_name",
            text=f"{current_index}/{len(actions)}  {current.get('g4mt_clip_name', current.name)}",
            icon="ACTION",
        )
        self.layout.operator(
            EXPORT_OT_level5_g4mt_bank_family.bl_idname,
            text="Retarget Original Bank Family",
            icon="EXPORT",
        )
        self.layout.operator(
            EXPORT_OT_level5_g4mt_victorytool_family.bl_idname,
            text="Export Animation Family for VictoryTool",
            icon="EXPORT",
        )
        if len(actions) == 1:
            self.layout.label(text="Only one clip was imported into this Action family", icon="INFO")


def character_model_start_path() -> str:
    configured = bpy.path.abspath(getattr(addon_preferences(), "raw_data_root", "") or "")
    candidate = Path(configured) / "common" / "chr" if configured else None
    return str(candidate) + os.sep if candidate is not None and candidate.is_dir() else ""


def character_parts_start_path(model_path: str) -> str:
    for parent in Path(model_path).parents:
        if parent.name == "chr":
            candidate = parent / "_uniform"
            if candidate.is_dir():
                return str(candidate) + os.sep
            break
    return ""


def expected_character_part_path(model_path: str, prefix: str) -> str:
    match = re.fullmatch(r"c(\d{6,8})", Path(model_path).stem, re.IGNORECASE)
    if match is None:
        return ""
    stem = f"{prefix}{match.group(1)}"
    for parent in Path(model_path).parents:
        if parent.name != "chr":
            continue
        uniform_root = parent / "_uniform"
        for extension in (".g4pkm", ".g4md"):
            candidate = uniform_root / stem / f"{stem}{extension}"
            if candidate.is_file():
                return str(candidate)
        break
    return ""


def event_import_log_path() -> Path:
    try:
        config_directory = bpy.utils.user_resource("CONFIG", create=True)
        if config_directory:
            directory = Path(config_directory) / "level5_g4"
            directory.mkdir(parents=True, exist_ok=True)
            return directory / "event_import.log"
    except (AttributeError, OSError, RuntimeError, TypeError):
        pass
    return Path(tempfile.gettempdir()) / "level5_g4_event_import.log"


def append_event_file_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with event_import_log_path().open("a", encoding="utf-8") as stream:
            stream.write(f"[{timestamp}] {message.rstrip()}\n")
    except (AttributeError, OSError, RuntimeError, TypeError):
        pass


def write_deferred_call_error(error: str) -> None:
    text = bpy.data.texts.get("G4 Deferred Call Errors")
    if text is None:
        text = bpy.data.texts.new("G4 Deferred Call Errors")
    text.write(error.rstrip() + "\n\n")


def defer_blender_call(callback, label: str = "Deferred Blender operation") -> None:
    append_event_file_log(f"deferred scheduled: {label}")

    def run():
        try:
            append_event_file_log(f"deferred running: {label}")
            callback()
            append_event_file_log(f"deferred finished: {label}")
        except Exception:
            error = f"[{label}]\n{traceback.format_exc()}"
            write_deferred_call_error(error)
            append_event_file_log(error)
            print(error)
        return None

    bpy.app.timers.register(run, first_interval=0.01)


def finish_chained_g4mt_import(
    animation_path: str,
    model_path: str,
    body_model: str,
    shoes_model: str,
    settings_json: str,
):
    settings = json.loads(settings_json or "{}")
    settings["prompt_for_models"] = False
    return bpy.ops.import_scene.level5_g4mt(
        "EXEC_DEFAULT",
        filepath=animation_path,
        model_path=model_path,
        body_model=body_model,
        shoes_model=shoes_model,
        import_model=True,
        import_character_parts=bool(body_model or shoes_model),
        **settings,
    )


class IMPORT_OT_level5_g4mt_pick_model(Operator, ImportHelper):
    bl_idname = "import_scene.level5_g4mt_pick_model"
    bl_label = "Select Character Model"

    filename_ext = ".g4md"
    filter_glob: StringProperty(default="*.g4md;*.g4pkm", options={"HIDDEN"})
    animation_path: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    settings_json: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = character_model_start_path()
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        model_path = Path(self.filepath)
        if not model_path.is_file() or model_path.suffix.lower() not in {".g4md", ".g4pkm"}:
            self.report({"ERROR"}, "Select a G4MD/G4PKM character model")
            return {"CANCELLED"}
        animation_path = self.animation_path
        settings_json = self.settings_json
        defer_blender_call(
            lambda: bpy.ops.import_scene.level5_g4_character_setup(
                "INVOKE_DEFAULT",
                model_path=str(model_path),
                animation_path=animation_path,
                animation_settings_json=settings_json,
                allow_head_override=True,
            )
        )
        return {"FINISHED"}


class IMPORT_OT_level5_g4mt_pick_body(Operator, ImportHelper):
    bl_idname = "import_scene.level5_g4mt_pick_body"
    bl_label = "Select Body (Cancel to Skip)"

    filename_ext = ".g4md"
    filter_glob: StringProperty(default="*.g4md;*.g4pkm", options={"HIDDEN"})
    animation_path: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    model_path: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    settings_json: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = (
                expected_character_part_path(self.model_path, "u")
                or character_parts_start_path(self.model_path)
            )
        return ImportHelper.invoke(self, context, event)

    def open_shoes_picker(self, body_model: str) -> None:
        animation_path = self.animation_path
        model_path = self.model_path
        settings_json = self.settings_json
        defer_blender_call(
            lambda: bpy.ops.import_scene.level5_g4mt_pick_shoes(
                "INVOKE_DEFAULT",
                animation_path=animation_path,
                model_path=model_path,
                body_model=body_model,
                settings_json=settings_json,
            )
        )

    def execute(self, context):
        body_path = Path(self.filepath)
        if not body_path.is_file() or body_path.suffix.lower() not in {".g4md", ".g4pkm"}:
            self.report({"ERROR"}, "Select a u*.g4md/u*.g4pkm body or cancel to skip it")
            return {"CANCELLED"}
        self.open_shoes_picker(str(body_path))
        return {"FINISHED"}

    def cancel(self, context):
        self.open_shoes_picker("")


class IMPORT_OT_level5_g4mt_pick_shoes(Operator, ImportHelper):
    bl_idname = "import_scene.level5_g4mt_pick_shoes"
    bl_label = "Select Shoes (Cancel to Skip)"

    filename_ext = ".g4md"
    filter_glob: StringProperty(default="*.g4md;*.g4pkm", options={"HIDDEN"})
    animation_path: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    model_path: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    body_model: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    settings_json: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = (
                expected_character_part_path(self.model_path, "s")
                or character_parts_start_path(self.model_path)
            )
        return ImportHelper.invoke(self, context, event)

    def finish(self, shoes_model: str) -> None:
        animation_path = self.animation_path
        model_path = self.model_path
        body_model = self.body_model
        settings_json = self.settings_json
        defer_blender_call(
            lambda: finish_chained_g4mt_import(
                animation_path,
                model_path,
                body_model,
                shoes_model,
                settings_json,
            )
        )

    def execute(self, context):
        shoes_path = Path(self.filepath)
        if not shoes_path.is_file() or shoes_path.suffix.lower() not in {".g4md", ".g4pkm"}:
            self.report({"ERROR"}, "Select an s*.g4md/s*.g4pkm model or cancel to skip it")
            return {"CANCELLED"}
        self.finish(str(shoes_path))
        return {"FINISHED"}

    def cancel(self, context):
        self.finish("")


class IMPORT_OT_level5_g4mt(Operator, ImportHelper):
    bl_idname = "import_scene.level5_g4mt"
    bl_label = "Import Level-5 G4 Animation"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".g4mt"
    filter_glob: StringProperty(default="*.g4mt;*.g4pk", options={"HIDDEN"})
    entry: StringProperty(
        name="G4MT Entry",
        default="0",
        description="G4MT entry index or internal name when importing a G4PK",
    )
    clip: StringProperty(
        name="Clip",
        default="",
        description="Animation name or index; leave empty to select the first animated clip",
    )
    import_all_clips: BoolProperty(
        name="Import All Animations",
        default=True,
        description="Create a separate action for every clip in the selected animation bank",
    )
    import_model: BoolProperty(
        name="Import Matching Model",
        default=True,
        description="Import the manually selected character model when no compatible armature is selected",
    )
    model_path: StringProperty(
        name="Character Model",
        subtype="FILE_PATH",
        description="G4MD/G4PKM model whose rig will receive the animation",
    )
    source_skeleton_path: StringProperty(
        name="Animation Source G4SK",
        subtype="FILE_PATH",
        description="Source rig for this animation bank; leave empty when it uses the selected model rig",
    )
    import_character_parts: BoolProperty(
        name="Import Body and Shoes",
        default=False,
        description="Attach manually selected body and shoes to the imported character rig",
    )
    auto_character_parts: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    body_model: StringProperty(name="Body Model", subtype="FILE_PATH")
    shoes_model: StringProperty(name="Shoes Model", subtype="FILE_PATH")
    accessory_model: StringProperty(name="Arms / Neck", subtype="FILE_PATH")
    gloves_model: StringProperty(name="Gloves Model", subtype="FILE_PATH")
    armband_model: StringProperty(name="Captain Armband Model", subtype="FILE_PATH")
    nameplate_model: StringProperty(name="Nameplate Model", subtype="FILE_PATH")
    attach_ball: BoolProperty(name="Attach Ball", default=False)
    ball_model: StringProperty(name="Ball Model", subtype="FILE_PATH")
    prompt_for_models: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    import_camera: BoolProperty(
        name="Import Matching Camera",
        default=True,
        description="Import the clip with the same name from the event's companion G4CM",
    )
    set_active_camera: BoolProperty(
        name="Set Active Camera",
        default=True,
        description="Use the matching G4CM camera as the active scene camera",
    )
    reuse_selected_armature: BoolProperty(
        name="Reuse Selected Armature",
        default=True,
        description="Use exact joint names, unambiguous catalog aliases, and bone-level source-joint overrides",
    )
    set_scene_fps: BoolProperty(
        name="Set Scene FPS",
        default=True,
        description="Set the scene frame rate to the FPS stored in the G4MT clip",
    )

    def invoke(self, context, event):
        self.auto_character_parts = False
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "entry")
        layout.prop(self, "import_all_clips")
        layout.prop(self, "clip", text="Active Animation")
        layout.prop(self, "import_model")
        if self.import_model:
            layout.label(text="Model and character cosmetics will be requested after the animation", icon="FILE_FOLDER")
        layout.prop(self, "source_skeleton_path")
        layout.prop(self, "import_camera")
        if self.import_camera:
            layout.prop(self, "set_active_camera")
        layout.prop(self, "reuse_selected_armature")
        if self.reuse_selected_armature:
            layout.label(text="For a manual map, set bone property g4_animation_source_joint to a G4 joint name.")
        layout.prop(self, "set_scene_fps")

    def execute(self, context):
        path = Path(self.filepath)
        if path.suffix.lower() not in {".g4mt", ".g4pk"}:
            self.report({"ERROR"}, "Select a G4MT animation file or a G4PK containing G4MT")
            return {"CANCELLED"}
        if self.import_model and self.prompt_for_models:
            animation_path = str(path)
            settings_json = g4mt_import_settings(self)
            if self.model_path:
                defer_blender_call(
                    lambda: bpy.ops.import_scene.level5_g4_character_setup(
                        "INVOKE_DEFAULT",
                        model_path=self.model_path,
                        animation_path=animation_path,
                        animation_settings_json=settings_json,
                        allow_head_override=True,
                    )
                )
            else:
                defer_blender_call(
                    lambda: bpy.ops.import_scene.level5_g4mt_pick_model(
                        "INVOKE_DEFAULT",
                        animation_path=animation_path,
                        settings_json=settings_json,
                    )
                )
            return {"FINISHED"}
        prefs = addon_preferences()
        window_manager = context.window_manager
        decoded_path = None
        extracted_g4mt_path = None
        package_entry_name = None
        camera_decoded_path = None
        camera_object = None
        camera_path = None
        camera_motion = None
        progress_active = True
        last_progress_refresh = 0.0
        window_manager.progress_begin(0, 100)

        def refresh_progress(current: int, status: str):
            nonlocal last_progress_refresh
            window_manager.progress_update(current)
            now = time.monotonic()
            if now - last_progress_refresh < 0.12 and current < 100:
                return
            last_progress_refresh = now
            context.workspace.status_text_set(status)
            for window in window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()
            try:
                bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
            except RuntimeError:
                pass

        refresh_progress(0, "Preparing G4 animation import…")
        try:
            g4mt_path, extracted_g4mt_path, package_entry_name = materialize_g4mt(path, self.entry)
            refresh_progress(5, "Decoding G4 animation…")
            selected = context.active_object if self.reuse_selected_armature else None
            configured_model = bpy.path.abspath(self.model_path or "")
            model_path_hint = Path(configured_model) if configured_model else None
            if model_path_hint is not None and (
                not model_path_hint.is_file() or model_path_hint.suffix.lower() not in {".g4md", ".g4pkm"}
            ):
                raise RuntimeError(f"Character model not found or unsupported: {model_path_hint}")
            skeleton_model_hint = model_path_hint
            if skeleton_model_hint is None and selected is not None and selected.type == "ARMATURE":
                source = Path(str(selected.get("g4_character_model_source", "")))
                if source.is_file():
                    skeleton_model_hint = source
            skeleton_hint = None
            if skeleton_model_hint is None and selected is not None and selected.type == "ARMATURE":
                skeleton_hint = materialize_skeleton_source(
                    str(selected.get("g4_character_skeleton_source", ""))
                )
            if skeleton_hint is None:
                skeleton_hint = resolve_skeleton_path(skeleton_model_hint)
            source_skeleton_path = bpy.path.abspath(self.source_skeleton_path or "")
            source_skeleton = valid_skeleton_path(Path(source_skeleton_path)) if source_skeleton_path else None
            if source_skeleton_path and source_skeleton is None:
                raise RuntimeError(f"Animation source is not a readable G4SK: {source_skeleton_path}")
            source_rig_differs = (
                source_skeleton is not None
                and (skeleton_hint is None or source_skeleton.read_bytes() != skeleton_hint.read_bytes())
            )
            if source_skeleton is not None:
                skeleton_hint = source_skeleton
            clip_selector = self.clip.strip()
            if not clip_selector:
                available_clips = parse_g4mt(g4mt_path, skeleton_hint)["clips"]
                playable = [clip for clip in available_clips if not clip["flags"] & 1]
                if not playable:
                    raise RuntimeError("This bank requires a base animation")
                selected_clip = next((clip for clip in playable if clip["frame_count"] > 2), playable[0])
                clip_selector = str(selected_clip["index"])
            motion, decoded_path = decode_g4mt(g4mt_path, clip_selector, prefs, skeleton_hint)
            refresh_progress(15, "Preparing G4 animation rig…")
            if self.import_camera:
                camera_path = resolve_companion_g4cm(path, getattr(prefs, "raw_data_root", ""))
                if camera_path is not None:
                    try:
                        camera_motion, camera_decoded_path = decode_g4cm(
                            camera_path,
                            motion["clip"]["name"],
                            prefs,
                        )
                    except Exception as exc:
                        self.report({"WARNING"}, f"Matching G4CM was not imported: {exc}")
                        camera_motion = None

            names = track_bone_names(motion)
            if selected is not None and selected.type == "ARMATURE":
                resolve_track_names_from_armature(motion, selected)
                names = track_bone_names(motion)
            if has_display_oriented_bones(selected):
                if not self.import_model:
                    raise RuntimeError(
                        "The selected rig has display-oriented bones and cannot reproduce G4MT axes accurately. "
                        "Import a fresh matching model or disable 'Apply Bone Orientation' when importing it."
                    )
                self.report({"WARNING"}, "Selected rig uses display-oriented bones; importing a fresh animation-safe rig")
                selected = None
            armature = selected if armature_score(selected, names) > 0 else None
            model_path = None
            if armature is None and self.import_model:
                if model_path_hint is None:
                    raise RuntimeError("Select a Character Model or reuse a compatible selected armature")
                armature, model_path = import_model_for_animation(
                    path,
                    motion,
                    prefs,
                    model_path_hint,
                    self.import_character_parts,
                    False,
                    self.body_model,
                    self.shoes_model,
                    self.accessory_model,
                    self.gloves_model,
                    self.armband_model,
                    self.nameplate_model,
                    self.attach_ball,
                    self.ball_model,
                    align_to_motion_rest=not source_rig_differs,
                )
                refresh_progress(30, "Creating G4 animation keyframes…")
            if armature is None:
                armature = best_armature(bpy.data.objects, names)
                if armature is not None:
                    resolve_track_names_from_armature(motion, armature)
            names = track_bone_names(motion)
            if armature is None or armature_score(armature, names) <= 0:
                raise RuntimeError("No armature matches the G4MT targets")

            rig_id = armature.get("g4_animation_rig_id")
            if not rig_id:
                rig_id = uuid.uuid4().hex
                armature["g4_animation_rig_id"] = rig_id
            family_import_id = uuid.uuid4().hex

            starts = [motion["clip"]["start_frame"]]
            ends = [motion["clip"]["end_frame"]]
            if camera_motion is not None:
                starts.append(camera_motion["clip"]["start_frame"])
                ends.append(camera_motion["clip"]["end_frame"])
            frame_origin = min(starts)

            track_count = len(motion["tracks"])
            part_count = len(character_part_armatures(armature))
            semantic_retarget = requires_semantic_retarget(motion, armature, source_rig_differs)
            progress_total = max(1, track_count * (1 + part_count))

            def update_progress(completed, total, offset=0):
                current = min(progress_total, offset + completed)
                percent = 30 + round(70 * current / progress_total)
                refresh_progress(percent, f"Importing animation: {current}/{progress_total} bone tracks")

            if semantic_retarget:
                action, keyed_bones, _g4mt_targets = create_retargeted_action(
                    armature, motion, frame_origin, False
                )
            else:
                action, keyed_bones = create_action(
                    armature,
                    motion,
                    frame_origin,
                    lambda completed, total: update_progress(completed, total),
                )
            if keyed_bones == 0:
                bpy.data.actions.remove(action)
                raise RuntimeError("The animation contains no matching bone tracks")
            part_actions, part_keyed_bones = animate_character_parts(
                armature,
                motion,
                frame_origin,
                progress_callback=lambda completed, total: update_progress(completed, total, track_count),
            )
            tag_character_part_actions(
                armature, motion, str(path), rig_id, family_import_id, package_entry_name
            )
            imported_clip_count = 1
            if self.import_all_clips:
                clips = parse_g4mt(g4mt_path, skeleton_hint)["clips"]
                # Keep the requested clip active after creating the bank actions.
                part_active = [(part, part.animation_data.action, getattr(part.animation_data, "action_slot", None))
                               for part in character_part_armatures(armature)]
                active_slot = getattr(armature.animation_data, "action_slot", None)
                try:
                    for clip in clips:
                        if clip["index"] == motion["clip"]["index"]:
                            continue
                        if clip["flags"] & 1:
                            self.report({"WARNING"}, f"Skipped {clip['name']}: requires a base animation")
                            continue
                        refresh_progress(95, f"Importing animation {imported_clip_count + 1}/{len(clips)}")
                        extra_motion = decode_motion(g4mt_path, str(clip["index"]), skeleton_hint)
                        resolve_track_names_from_armature(extra_motion, armature)
                        if semantic_retarget:
                            extra_action, extra_keyed, _extra_g4mt_targets = create_retargeted_action(
                                armature,
                                extra_motion,
                                clip["start_frame"],
                                False,
                            )
                        else:
                            extra_action, extra_keyed = create_action(armature, extra_motion)
                        if extra_keyed == 0:
                            bpy.data.actions.remove(extra_action)
                            raise RuntimeError(f"Animation {clip['name']} has no matching bone tracks")
                        tag_g4mt_action(
                            extra_action, str(path), clip, rig_id, family_import_id, package_entry_name
                        )
                        if not semantic_retarget:
                            animate_character_parts(armature, extra_motion, clip["start_frame"])
                            tag_character_part_actions(
                                armature, extra_motion, str(path), rig_id, family_import_id,
                                package_entry_name,
                            )
                        imported_clip_count += 1
                finally:
                    armature.animation_data.action = action
                    if active_slot is not None:
                        armature.animation_data.action_slot = active_slot
                    clear_pose(armature)
                    for part, part_action, part_slot in part_active:
                        part.animation_data.action = part_action
                        if part_slot is not None:
                            part.animation_data.action_slot = part_slot
                        clear_pose(part)
            frame_count = len(motion["frames"])
            context.scene.frame_start = 1
            context.scene.frame_end = max(1, max(ends) - frame_origin + 1)
            if self.set_scene_fps and motion["clip"].get("fps"):
                context.scene.render.fps = motion["clip"]["fps"]
                context.scene.render.fps_base = 1.0

            if camera_motion is not None and camera_path is not None:
                camera_object = existing_g4cm_camera(camera_path, camera_motion["clip"]["name"])
                if camera_object is None:
                    camera_object, _ = create_camera_animation(
                        camera_path,
                        camera_motion,
                        context.collection,
                        frame_origin,
                    )
                if self.set_active_camera:
                    context.scene.camera = camera_object

            context.scene.frame_set(1)
            context.view_layer.objects.active = armature
            armature.select_set(True)
            tag_g4mt_action(action, str(path), motion["clip"], rig_id, family_import_id, package_entry_name)
            action["g4_source_frame_origin"] = frame_origin
            if model_path is not None:
                action["g4_model_source"] = str(model_path)
            if skeleton_hint is not None:
                action["g4_skeleton_source"] = str(skeleton_hint)
            if camera_path is not None and camera_motion is not None:
                action["g4cm_source"] = str(camera_path)
                action["g4cm_clip_name"] = camera_motion["clip"]["name"]
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            if progress_active:
                window_manager.progress_end()
                context.workspace.status_text_set(None)
            if decoded_path and not getattr(prefs, "keep_decode_json", False):
                try:
                    decoded_path.unlink()
                    if not any(decoded_path.parent.iterdir()):
                        shutil.rmtree(decoded_path.parent, ignore_errors=True)
                except OSError:
                    pass
            if camera_decoded_path and not getattr(prefs, "keep_decode_json", False):
                try:
                    camera_decoded_path.unlink()
                    if not any(camera_decoded_path.parent.iterdir()):
                        shutil.rmtree(camera_decoded_path.parent, ignore_errors=True)
                except OSError:
                    pass
            if extracted_g4mt_path is not None:
                try:
                    extracted_g4mt_path.unlink()
                    if not any(extracted_g4mt_path.parent.iterdir()):
                        shutil.rmtree(extracted_g4mt_path.parent, ignore_errors=True)
                except OSError:
                    pass

        self.report(
            {"INFO"},
            f"Imported {imported_clip_count} animations; {motion['clip']['name']}: {keyed_bones + part_keyed_bones} bones, {frame_count} actor frames, {part_actions} parts"
            f"{' with matching camera' if camera_object is not None else ''} on {armature.name}",
        )
        return {"FINISHED"}


def g4cm_vector(value) -> Vector:
    """Convert the game's right-handed Y-up coordinates to Blender Z-up."""
    return Vector((value[0], -value[2], value[1]))


def lens_from_vertical_fov(camera_data, fov: float) -> float:
    safe_fov = max(1e-4, min(float(fov), 3.13))
    return camera_data.sensor_height / (2.0 * math.tan(safe_fov * 0.5))


EVENT_CAMERA_CLIP_END = 100_000.0


def configure_event_camera_clipping() -> int:
    """Extend clipping only in scenes containing imported G4 cameras."""
    cameras = [
        obj for obj in bpy.data.objects
        if obj.type == "CAMERA" and obj.get("g4cm_source")
    ]
    if not cameras:
        return 0

    changed = 0
    for camera in cameras:
        if camera.data.clip_end < EVENT_CAMERA_CLIP_END:
            camera.data.clip_end = EVENT_CAMERA_CLIP_END
            changed += 1

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type == "VIEW_3D" and space.clip_end < EVENT_CAMERA_CLIP_END:
                    space.clip_end = EVENT_CAMERA_CLIP_END
                    changed += 1
    return changed


def create_camera_animation(
    path: Path,
    motion: dict,
    collection,
    frame_origin: int | None = None,
) -> tuple[bpy.types.Object, int]:
    clip = motion["clip"]
    name = f"{path.stem}_{clip['name']}"
    camera_data = bpy.data.cameras.new(name=name)
    camera_data.sensor_fit = "VERTICAL"
    camera_data.clip_end = EVENT_CAMERA_CLIP_END
    camera_object = bpy.data.objects.new(name=name, object_data=camera_data)
    collection.objects.link(camera_object)
    camera_object.rotation_mode = "QUATERNION"

    transform_action = bpy.data.actions.new(name=f"{name}_Camera")
    lens_action = bpy.data.actions.new(name=f"{name}_Lens")
    transform_action.use_fake_user = True
    lens_action.use_fake_user = True
    camera_object.animation_data_create()
    camera_object.animation_data.action = transform_action
    camera_data.animation_data_create()
    camera_data.animation_data.action = lens_action

    previous_rotation = None
    source_start = clip["start_frame"] if frame_origin is None else frame_origin
    for source_frame, sample in zip(motion["frames"], motion["samples"]):
        frame = 1 + source_frame - source_start
        position = g4cm_vector(sample["position"])
        target = g4cm_vector(sample["target"])
        direction = target - position
        if direction.length_squared > 1e-12:
            rotation = direction.to_track_quat("-Z", "Y")
            rotation = Quaternion(direction.normalized(), float(sample["roll"])) @ rotation
        elif previous_rotation is not None:
            rotation = previous_rotation.copy()
        else:
            rotation = Quaternion()
        if previous_rotation is not None and previous_rotation.dot(rotation) < 0.0:
            rotation.negate()
        previous_rotation = rotation.copy()

        camera_object.location = position
        camera_object.rotation_quaternion = rotation
        camera_object.keyframe_insert("location", frame=frame, group="Camera Transform")
        camera_object.keyframe_insert("rotation_quaternion", frame=frame, group="Camera Transform")
        camera_data.lens = lens_from_vertical_fov(camera_data, sample["fov"])
        camera_data.keyframe_insert("lens", frame=frame, group="Camera Lens")

    for action in (transform_action, lens_action):
        for fcurve in action_fcurves(action):
            for point in fcurve.keyframe_points:
                point.interpolation = "LINEAR"
        action["g4cm_source"] = str(path)
        action["g4cm_clip_index"] = clip["index"]
        action["g4cm_clip_name"] = clip["name"]
        action["g4cm_fov_axis"] = "vertical"
        action["g4_source_frame_origin"] = source_start
    camera_object["g4cm_source"] = str(path)
    camera_object["g4cm_clip_name"] = clip["name"]
    camera_object["g4_source_frame_origin"] = source_start
    return camera_object, len(motion["frames"])


def existing_g4cm_camera(path: Path, clip_name: str):
    source = str(path)
    return next(
        (
            obj
            for obj in bpy.data.objects
            if obj.type == "CAMERA"
            and obj.get("g4cm_source") == source
            and obj.get("g4cm_clip_name") == clip_name
        ),
        None,
    )


def add_nla_strip(owner, track, action, name: str, start: int, duration: int):
    owner.animation_data_create()
    if owner.animation_data.action == action:
        owner.animation_data.action = None
    strip = track.strips.new(name, start, action)
    strip.action_frame_start = 1.0
    strip.action_frame_end = float(duration)
    strip.frame_start = float(start)
    strip.frame_end = float(start + duration - 1)
    strip.extrapolation = "NOTHING"
    strip.blend_type = "REPLACE"
    return strip


def event_actor_id(path: Path) -> str | None:
    identifiers = model_identifiers(path)
    return identifiers[-1] if identifiers else None


def event_cut_name(path: Path) -> str | None:
    match = re.search(r"_(c\d+)$", path.stem, re.IGNORECASE)
    return match.group(1).lower() if match else None


def cut_sort_key(name: str) -> tuple[int, str]:
    match = re.fullmatch(r"c(\d+)", name, re.IGNORECASE)
    return (int(match.group(1)), name) if match else (sys.maxsize, name)


def collect_event_packages(directory: Path) -> dict[str, list[Path]]:
    by_actor_slot: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for path in sorted(directory.glob("*.g4pk")):
        prefix = f"{directory.name}_"
        if not path.stem.lower().startswith(prefix.lower()):
            continue
        payload = path.stem[len(prefix):]
        cut_match = re.search(r"_(c\d+)$", payload, re.IGNORECASE)
        if cut_match is None:
            continue
        actor_spec = payload[:cut_match.start()].lower()
        slot_match = re.search(r"_(s\d+)_p\d+$", actor_spec, re.IGNORECASE)
        slot = slot_match.group(1).lower() if slot_match else ""
        actor = actor_spec[:slot_match.start()] if slot_match else actor_spec
        if not actor or actor == "camera" or actor.startswith("point"):
            continue
        by_actor_slot[(actor, slot)].append(path)
    slot_count = defaultdict(int)
    for actor, _ in by_actor_slot:
        slot_count[actor] += 1
    grouped = {}
    for (actor, slot), paths in by_actor_slot.items():
        key = f"{actor}_{slot}" if slot_count[actor] > 1 else actor
        grouped[key] = paths
    for paths in grouped.values():
        paths.sort(key=lambda path: int(event_cut_name(path)[1:]))
    return dict(grouped)


def event_actor_base_id(actor: str) -> str:
    return re.sub(r"_s\d+$", "", actor, flags=re.IGNORECASE)


def event_actor_slot(actor: str) -> str:
    match = re.search(r"_(s\d+)$", actor, re.IGNORECASE)
    return match.group(1).lower() if match else "s00"


def generic_actor_groups(actors: list[str]) -> dict[str, list[str]]:
    groups = defaultdict(list)
    for actor in actors:
        if re.fullmatch(r"c0{2,}\d{3,5}", event_actor_base_id(actor), re.IGNORECASE):
            groups[event_actor_slot(actor)].append(actor)
    return {slot: sorted(slot_actors) for slot, slot_actors in sorted(groups.items())}


def model_lookup_row(model_path: Path) -> dict:
    lookup_path = model_lookup_path()
    relative = model_relative_path(model_path)
    if lookup_path is None or relative is None:
        return {}
    try:
        models = json.loads(lookup_path.read_text(encoding="utf-8")).get("models") or {}
    except (OSError, json.JSONDecodeError):
        return {}
    return models.get(relative) or models.get(Path(relative).with_suffix(".objbin").as_posix()) or {}


def compatible_generic_actor(actor_ids: list[str], head_model: str) -> str:
    skeleton = ""
    if head_model:
        path = Path(bpy.path.abspath(head_model))
        family = re.fullmatch(r"c\d([1-4])\d{4,6}", path.stem, re.IGNORECASE)
        if family:
            skeleton = f"c000{family.group(1)}01"
        if not skeleton:
            skeleton = str(model_lookup_row(path).get("g4sk_stem") or "").lower()
        if not skeleton:
            resolved_skeleton = resolve_skeleton_path(path)
            skeleton = resolved_skeleton.stem.lower() if resolved_skeleton is not None else ""
    if skeleton:
        match = next(
            (actor for actor in actor_ids if event_actor_base_id(actor).lower() == skeleton),
            None,
        )
        if match:
            return match
    return next(
        (actor for actor in actor_ids if event_actor_base_id(actor).lower() == "c000101"),
        actor_ids[0],
    )


def resolve_generic_event_skeleton(package: Path, actor: str, prefs) -> Path | None:
    actor_base = event_actor_base_id(actor)
    if not re.fullmatch(r"c000[1-4]01", actor_base, re.IGNORECASE):
        return None
    for data_root in candidate_data_roots(package, getattr(prefs, "raw_data_root", "")):
        candidate = data_root / "common" / "chr" / actor_base / f"{actor_base}.g4sk"
        if candidate.is_file():
            return candidate
    return None


def resolve_event_actor_models(directory: Path, prefs) -> dict[str, str]:
    event_name = directory.name
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        base = data_root / "common" / "event_cfg" / "evt" / f"{event_name}.cfg.bin"
        for path in (base, base.with_suffix(base.suffix + ".json"), base.with_suffix(base.suffix + ".xml")):
            if not path.is_file():
                continue
            try:
                models = load_event_actor_models(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if models:
                return models
    return {}


def resolve_event_actor_points(directory: Path, prefs) -> dict[str, str]:
    event_name = directory.name
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        for config_group in ("evt", "vis"):
            base = data_root / "common" / "event_cfg" / config_group / f"{event_name}.cfg.bin"
            for path in (base, base.with_suffix(base.suffix + ".json"), base.with_suffix(base.suffix + ".xml")):
                if not path.is_file():
                    continue
                try:
                    points = load_event_actor_points(path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if points:
                    return points
    return {}


def resolve_event_actor_point_assignments(directory: Path, prefs) -> dict[str, dict[str, tuple[str, str]]]:
    event_name = directory.name
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        for config_group in ("evt", "vis"):
            base = data_root / "common" / "event_cfg" / config_group / f"{event_name}.cfg.bin"
            for path in (base, base.with_suffix(base.suffix + ".json"), base.with_suffix(base.suffix + ".xml")):
                if not path.is_file():
                    continue
                try:
                    assignments = load_event_actor_point_assignments(path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if assignments:
                    return assignments
    return {}


def resolve_event_point_skeleton(directory: Path, prefs) -> Path | None:
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        candidate = data_root / "common" / "event" / "ev_point" / "point" / "point.g4sk"
        if candidate.is_file():
            return candidate
    return None


_CUSTOM_SHADER_PARAM_FIELDS = re.compile(
    r'PROP_PARAM\s+"(matNameCrc|refParamNameCrc|refParamType|setShaderParamIdx)",\s*(-?\d+);'
)


def parse_effect_shader_params(path: Path | None) -> list[dict]:
    """Read CCustomShaderParam links exported alongside an effect OBJBIN."""
    if path is None or not path.is_file():
        return []
    entries = []
    current = {}
    active = False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        if 'PROP_INFO_BGN\t"CCustomShaderParam"' in line:
            active = True
            current = {}
        elif active and line.startswith("PROP_INFO_END"):
            active = False
        elif active and line.startswith("PROP_PARAM_BGN"):
            current = {}
        elif active and line.startswith("PROP_PARAM_END"):
            if "matNameCrc" in current and "setShaderParamIdx" in current:
                entries.append(current)
            current = {}
        elif active:
            match = _CUSTOM_SHADER_PARAM_FIELDS.search(line)
            if match:
                current[match.group(1)] = int(match.group(2)) & 0xFFFFFFFF
    return entries


def effect_g4ma_material_targets(paths: list[Path], shader_params: list[dict]) -> list[dict]:
    """Resolve only G4MA targets explicitly declared as material CRCs."""
    slots_by_material = defaultdict(list)
    for entry in shader_params:
        slots_by_material[entry["matNameCrc"]].append(entry)
    resolved = []
    for path in paths:
        try:
            parsed = parse_g4mt(path)
        except (OSError, ValueError, struct.error):
            continue
        if parsed.get("magic") != "G4MA":
            continue
        for target in parsed.get("targets", []):
            material_crc = int(target["crc32b"], 16)
            slots = slots_by_material.get(material_crc)
            if not slots:
                continue
            resolved.append({
                "source": str(path),
                "material_crc": f"{material_crc:08x}",
                "shader_params": slots,
            })
    return resolved


def create_effect_g4ma_actions(paths: list[Path], materials_by_crc: dict[int, list[object]]) -> list[str]:
    """Preserve resolved G4MA curves as Blender actions without guessing sockets."""
    action_names = []
    for path in paths:
        try:
            parsed = parse_g4mt(path)
        except (OSError, ValueError, struct.error):
            continue
        if parsed.get("magic") != "G4MA":
            continue
        for clip in parsed.get("clips", []):
            try:
                motion = decode_material_motion(path, clip["name"])
            except (OSError, ValueError, struct.error):
                continue
            start_frame = clip["start_frame"]
            for track in motion["tracks"]:
                material_crc = int(track["target_hash"], 16)
                for material in materials_by_crc.get(material_crc, []):
                    action = bpy.data.actions.new(
                        f"G4MA_{path.stem}_{clip['name']}_{material.name}"
                    )
                    for curve in track["curves"]:
                        for component in range(len(curve["values"][0]) if curve["values"] else 0):
                            property_name = (
                                f"g4_g4ma_{track['target_hash']}_{curve['channel_index']}_{component}"
                            )
                            material[property_name] = curve["values"][0][component]
                            fcurve = action_fcurve_new(action, material, f'["{property_name}"]', 0, "G4MA")
                            for frame, value in zip(motion["frames"], curve["values"]):
                                point = fcurve.keyframe_points.insert(
                                    frame - start_frame + 1,
                                    value[component],
                                    options={"FAST"},
                                )
                                point.interpolation = (
                                    "CONSTANT" if curve["interpolation"] == "STEP" else "LINEAR"
                                )
                    if action_fcurves(action):
                        action.use_fake_user = True
                        action_names.append(action.name)
                    else:
                        bpy.data.actions.remove(action)
    return action_names


def discover_event_effects(directory: Path, prefs) -> list[dict]:
    family_match = re.match(r"(ev\d+)", directory.name, re.IGNORECASE)
    if family_match is None:
        return []
    family = family_match.group(1).lower()
    available_cuts = {cut for path in directory.glob("*.g4pk")
                      if (cut := event_cut_name(path)) is not None}
    results = []
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        family_root = data_root / "common" / "effect" / "event" / family
        event_key = directory.name.lower().replace("_", "")
        exact_roots = (family_root / directory.name.lower(), family_root / event_key,
                       family_root / f"{event_key}0")
        effect_root = next((path for path in exact_roots if path.is_dir()), None)
        if effect_root is None:
            continue
        model_paths = sorted(effect_root.rglob("*.g4pkm"))
        for model in model_paths:
            asset_directory = model.parent
            particle = next(asset_directory.glob("*.ptlb"), None)
            particle_library = None
            particle_library_error = ""
            if particle is not None:
                try:
                    particle_library = read_particle_library(particle)
                except (OSError, ValueError) as error:
                    particle_library_error = str(error)
            objbin_cfg = next(asset_directory.glob("*.objbin.cfg"), None)
            material_animations = material_animation_paths(
                model, Path(tempfile.gettempdir()) / "level5_g4ma_blender"
            )
            shader_params = parse_effect_shader_params(objbin_cfg)
            cut = None
            suffix = re.search(r"(\d{5})$", model.stem)
            if suffix:
                number = int(suffix.group(1)) // 10
                # Event effect IDs are one-based relative to the five-digit suffix.
                # Prefer that native mapping when both neighboring cuts exist;
                # retain the direct mapping for older event families.
                cut = next((name for name in (f"c{number + 1:04d}", f"c{number:04d}")
                            if name in available_cuts), None)
            shader_names = set()
            for source in (particle, next(asset_directory.glob("*.objbin"), None)):
                if source is None:
                    continue
                data = source.read_bytes()
                shader_names.update(
                    match.decode("ascii", errors="ignore")
                    for match in re.findall(rb"[A-Za-z0-9_./-]+\.(?:vfxo|pfxo|cfxo|gfxo)", data)
                )
            results.append({
                "name": asset_directory.name,
                "model": str(model) if model else "",
                "particle": str(particle) if particle else "",
                "particle_library": particle_library,
                "particle_library_error": particle_library_error,
                "objbin_cfg": str(objbin_cfg) if objbin_cfg else "",
                "material_animations": [str(path) for path in material_animations],
                "shader_params": shader_params,
                "g4ma_material_targets": effect_g4ma_material_targets(material_animations, shader_params),
                "shaders": sorted(shader_names),
                "cut": cut,
            })
        if results:
            break
    return results



def decode_event_effect_motions(directory: Path, prefs, temporary_directory: Path) -> dict[str, dict]:
    skeleton = None
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        candidate = data_root / "common" / "event" / "ev_point" / "point_eff" / "point_eff.g4sk"
        if candidate.is_file():
            skeleton = candidate
            break
    if skeleton is None:
        return {}
    motions = {}
    prefix = f"{directory.name}_point_eff_"
    for package in sorted(directory.glob(f"{prefix}*.g4pk")):
        cut = event_cut_name(package)
        if cut is None:
            continue
        motion, _ = decode_event_package(package, skeleton, temporary_directory)
        motions[cut] = motion
    return motions


def configure_event_effect_materials(imported: set[object]) -> list[str]:
    """Preserve reconstructed shaders and provide a fallback for other effects."""
    effect_tokens = ("effect_", "ma_smoke", "_aura", "threshold")
    converted = []
    seen = set()
    for obj in imported:
        if obj.type != "MESH":
            continue
        for material in obj.data.materials:
            if material is None or material in seen:
                continue
            seen.add(material)
            if material.get("g4_effect_preview") in {"T1_STATIC", "T1M1_STATIC", "THRESHOLD_STATIC", "T3_THRESHOLD", "FAKE_PARTICLE"}:
                converted.append(material.name)
                continue
            name = material.name.lower()
            if not any(token in name for token in effect_tokens):
                continue
            nodes = material.node_tree.nodes if material.use_nodes and material.node_tree else None
            if nodes is None:
                continue
            images = [node.image for node in nodes if node.bl_idname == "ShaderNodeTexImage" and node.image]
            image = images[0] if images else None
            nodes.clear()
            links = material.node_tree.links
            output = nodes.new("ShaderNodeOutputMaterial")
            output.location = (520, 0)
            transparent = nodes.new("ShaderNodeBsdfTransparent")
            transparent.location = (260, -80)
            emission = nodes.new("ShaderNodeEmission")
            emission.location = (260, 80)
            emission.inputs["Strength"].default_value = 1.8 if "smoke" not in name else 0.8
            mix = nodes.new("ShaderNodeMixShader")
            mix.location = (420, 0)
            links.new(transparent.outputs[0], mix.inputs[1])
            links.new(emission.outputs[0], mix.inputs[2])
            links.new(mix.outputs[0], output.inputs["Surface"])
            if image is not None:
                texture = nodes.new("ShaderNodeTexImage")
                texture.image = image
                texture.location = (-220, 80)
                mapping = nodes.new("ShaderNodeMapping")
                mapping.name = "G4 Effect UV Flow"
                mapping.label = "G4 Effect UV Flow"
                mapping.location = (-440, 80)
                texcoord = nodes.new("ShaderNodeTexCoord")
                texcoord.location = (-640, 80)
                links.new(texcoord.outputs["UV"], mapping.inputs["Vector"])
                links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
                links.new(texture.outputs["Color"], emission.inputs["Color"])
                links.new(texture.outputs["Alpha"], mix.inputs[0])
            else:
                mix.inputs[0].default_value = 0.85
            if hasattr(material, "surface_render_method"):
                material.surface_render_method = "DITHERED"
            else:
                material.blend_method = "HASHED"
            if hasattr(material, "use_transparent_shadow"):
                material.use_transparent_shadow = False
            material["g4_effect_shader_family"] = (
                "threshold" if "threshold" in name else "basic"
            )
            material["g4_effect_blend"] = "additive"
            converted.append(material.name)
    return converted


def import_event_effect_models(
    candidates: list[dict],
    motions: dict[str, dict],
    cut_starts: dict[str, int],
    event_end: int,
    progress,
):
    imported_roots = []
    failed_models = []
    by_cut = defaultdict(list)
    for candidate in candidates:
        if candidate.get("cut"):
            by_cut[candidate["cut"]].append(candidate)
    for cut, cut_candidates in sorted(by_cut.items(), key=lambda item: cut_sort_key(item[0])):
        if cut not in cut_starts:
            continue
        following = [frame for frame in cut_starts.values() if frame > cut_starts[cut]]
        end_frame = min(following) if following else event_end + 1
        for effect_index, candidate in enumerate(cut_candidates, 1):
            yield progress(f"Importing event effect: {Path(candidate['model']).name}")
            model_path = Path(candidate["model"])
            before = set(bpy.data.objects)
            scene = bpy.context.scene
            previous_timing = (scene.frame_start, scene.frame_end, scene.render.fps, scene.render.fps_base)
            scene.frame_end = max(scene.frame_end, end_frame-1)
            scene.frame_start = cut_starts[cut]
            scene.frame_end = end_frame-1
            scene.render.fps = 60
            scene.render.fps_base = 1.0
            try:
                result = bpy.ops.import_scene.level5_g4(
                    filepath=str(model_path),
                    create_report_text=False,
                    import_character_parts=False,
                    event_effects=True,
                )
            except RuntimeError as exc:
                for obj in set(bpy.data.objects) - before:
                    bpy.data.objects.remove(obj, do_unlink=True)
                failed_models.append({"model": str(model_path), "error": str(exc)})
                continue
            finally:
                scene.frame_start, scene.frame_end, scene.render.fps, scene.render.fps_base = previous_timing
            if "FINISHED" not in result:
                continue
            imported = set(bpy.data.objects) - before
            root = bpy.data.objects.new(f"{candidate['name']} [{cut}]", None)
            bpy.context.scene.collection.objects.link(root)
            for obj in imported:
                if obj.parent not in imported:
                    world = obj.matrix_world.copy()
                    obj.parent = root
                    obj.matrix_world = world
            effect_materials = configure_event_effect_materials(imported)
            root["g4_event_effect_model"] = str(model_path)
            root["g4_event_effect_cut"] = cut
            root["g4_event_effect_start_frame"] = cut_starts[cut]
            root["g4_event_effect_end_frame"] = end_frame - 1
            root["g4_event_effect_particle"] = candidate.get("particle", "")
            particle_library = candidate.get("particle_library")
            if particle_library is not None:
                root["g4_event_effect_particle_emitters"] = json.dumps(
                    particle_library_metadata(particle_library)
                )
                from ..shading.particle_nodes import apply_particle_library_clock
                root["g4_event_effect_particle_clocks"] = apply_particle_library_clock(
                    imported, particle_library, cut_starts[cut], scene
                )
            elif candidate.get("particle_library_error"):
                root["g4_event_effect_particle_error"] = candidate["particle_library_error"]
            root["g4_event_effect_shaders"] = json.dumps(candidate.get("shaders") or [])
            root["g4_event_effect_objbin_cfg"] = candidate.get("objbin_cfg", "")
            root["g4_event_effect_g4ma"] = json.dumps(candidate.get("material_animations") or [])
            root["g4_event_effect_shader_params"] = json.dumps(candidate.get("shader_params") or [])
            root["g4_event_effect_g4ma_material_targets"] = json.dumps(
                candidate.get("g4ma_material_targets") or []
            )
            root["g4_event_effect_materials"] = json.dumps(effect_materials)
            shader_params_by_crc = defaultdict(list)
            for parameter in candidate.get("shader_params") or []:
                shader_params_by_crc[parameter.get("matNameCrc")].append(parameter)
            materials_by_crc = defaultdict(list)
            for obj in imported:
                if obj.type != "MESH":
                    continue
                for material in obj.data.materials:
                    if material is None:
                        continue
                    material_crc = material_crc32b(material)
                    if material not in materials_by_crc[material_crc]:
                        materials_by_crc[material_crc].append(material)
                    parameters = shader_params_by_crc.get(material_crc)
                    if parameters:
                        material["g4_effect_shader_params"] = json.dumps(parameters)
            root["g4_event_effect_g4ma_actions"] = json.dumps(
                create_effect_g4ma_actions(
                    [Path(path) for path in candidate.get("material_animations") or []],
                    materials_by_crc,
                )
            )
            motion = motions.get(cut)
            target = f"evp_eff{effect_index:02d}"
            if motion is not None:
                action = bpy.data.actions.new(f"{candidate['name']}_{cut}_Placement")
                if append_event_placement(action, root, motion, target):
                    track = root.animation_data_create().nla_tracks.new()
                    track.name = "Event Effect Placement"
                    duration = max(1, end_frame - cut_starts[cut])
                    add_nla_strip(root, track, action, cut, cut_starts[cut], duration)
            # Empty visibility does not propagate to children in rendered output.
            for obj in imported:
                for frame, hidden in ((cut_starts[cut]-1, True), (cut_starts[cut], False), (end_frame, True)):
                    obj.hide_viewport = hidden
                    obj.hide_render = hidden
                    obj.keyframe_insert("hide_viewport", frame=frame)
                    obj.keyframe_insert("hide_render", frame=frame)
                for curve in action_fcurves(obj.animation_data.action):
                    if curve.data_path in ("hide_viewport", "hide_render"):
                        for point in curve.keyframe_points:
                            point.interpolation = 'CONSTANT'
            imported_roots.append(root)
    if failed_models:
        bpy.context.scene["g4_event_effect_failures"] = json.dumps(failed_models)
    return imported_roots


def decode_event_point_motions(
    directory: Path,
    prefs,
    temporary_directory: Path,
) -> dict[tuple[str, str], dict]:
    skeleton = resolve_event_point_skeleton(directory, prefs)
    if skeleton is None:
        return {}
    motions = {}
    prefix = f"{directory.name}_point_s"
    for package in sorted(directory.glob(f"{prefix}*_c*.g4pk")):
        cut = event_cut_name(package)
        source_match = re.search(r"_point_(s\d+)_c\d+$", package.stem, re.IGNORECASE)
        if cut is None or source_match is None:
            continue
        motion, _ = decode_event_package(package, skeleton, temporary_directory)
        motions[(cut, f"point_{source_match.group(1).lower()}")] = motion
    return motions


SOURCE_TO_BLENDER = Matrix(
    (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, -1.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
)


def migrate_event_light_visibility(scene) -> int:
    updated = 0
    for obj in scene.objects:
        if obj.type != "LIGHT" or obj.get("g4_viewport_light_visibility") == 1:
            continue
        native = obj.get("g4_event_light") or any(
            re.fullmatch(r"ev\d+_\d+ Event Lights(?:\.\d+)?", collection.name)
            for collection in obj.users_collection
        )
        animation = obj.animation_data
        if not native or animation is None or animation.action is None:
            continue
        curves = tuple(action_fcurves(animation.action))
        if any(curve.data_path == "hide_viewport" for curve in curves):
            continue
        source = next((curve for curve in curves if curve.data_path == "hide_render"), None)
        if source is None:
            continue
        for point in source.keyframe_points:
            obj.hide_viewport = bool(point.co[1])
            obj.keyframe_insert("hide_viewport", frame=point.co[0])
        for curve in action_fcurves(animation.action):
            if curve.data_path == "hide_viewport":
                for point in curve.keyframe_points:
                    point.interpolation = "CONSTANT"
        obj.hide_viewport = obj.hide_render
        obj["g4_event_light"] = True
        obj["g4_viewport_light_visibility"] = 1
        updated += 1
    return updated


def import_event_character_lighting(directory: Path, cut_starts: dict[str, int]):
    light_directory = directory / f"{directory.name}_light"
    if not light_directory.is_dir():
        return None
    config_specs = []
    manifest = light_directory / "light_list.cfg.bin"
    if manifest.is_file():
        try:
            for kind, cut, resource in event_light_config_references(manifest):
                resource_name = Path(resource.replace("\\", "/")).name
                path = light_directory / resource_name
                if path.is_file() and cut in cut_starts:
                    config_specs.append((kind, cut, path))
        except (OSError, ValueError, struct.error):
            config_specs = []
    if not config_specs:
        for path in sorted(light_directory.iterdir(), key=lambda item: item.name.casefold()):
            cut_match = re.search(
                r"_(c\d+)\.cfg\.bin(?:\.(?:json|xml))?$", path.name, re.IGNORECASE
            )
            if not path.is_file() or not path.name.casefold().startswith("eventmap_fix_"):
                continue
            cut = cut_match.group(1).lower() if cut_match else None
            if cut in cut_starts:
                config_specs.append(("fix", cut, path))
    grouped = {}
    for kind, cut, path in config_specs:
        try:
            entries = event_light_parameter_entries(path)
            slots = event_light_slots(entries)
        except (OSError, ValueError, struct.error):
            continue
        group = grouped.setdefault(
            cut, {"frame": cut_starts[cut], "parameters": {}, "lights": []}
        )
        for name, values in entries:
            group["parameters"].setdefault(name, values)
        for index, slot in enumerate(slots):
            slot = dict(slot)
            slot["id"] = f"{kind}:{path.name}:{index}"
            slot["kind"] = kind
            slot["source"] = path.name
            group["lights"].append(slot)
    keyed = [
        (cut, item["frame"], item["parameters"], item["lights"])
        for cut, item in grouped.items()
        if item["parameters"] or item["lights"]
    ]
    if not keyed:
        return None

    scene = bpy.context.scene
    edge2_parameters = {}
    edge_parameters = {}
    edge2_group = bpy.data.node_groups.get("G4 edge2 Preview Geometry")
    edge2_max_node = edge2_group.nodes.get("G4 edge2 Depth Max") if edge2_group is not None else None
    edge2_offset_node = edge2_group.nodes.get("G4 edge2 Depth Offset") if edge2_group is not None else None
    edge2_scale_node = edge2_group.nodes.get("G4 edge2 Scale") if edge2_group is not None else None
    edge2_material_scale_node = (
        edge2_group.nodes.get("G4 edge2 Material Scale") if edge2_group is not None else None
    )
    edge2_max_socket = edge2_max_node.inputs[1] if edge2_max_node is not None else None
    edge2_offset_socket = edge2_offset_node.inputs[1] if edge2_offset_node is not None else None
    edge2_scale_socket = edge2_scale_node.inputs[1] if edge2_scale_node is not None else None
    edge2_material_scale_socket = (
        edge2_material_scale_node.outputs[0] if edge2_material_scale_node is not None else None
    )
    edge2_material = bpy.data.materials.get("G4 edge2 Preview")
    if edge2_material is not None and edge2_material.node_tree is not None:
        # edgeColor is retained below as event metadata, but the debugger
        # capture shows it is the raw edge buffer colour.  Feeding its
        # typical (0.3, 0.3, 0.3) value directly to the render shell creates
        # the grey halo rejected by the character references.
        edge2_material.node_tree.animation_data_clear()
        edge2_color_node = edge2_material.node_tree.nodes.get("G4 edge2 Color")
        if edge2_color_node is not None:
            edge2_color_node.inputs["Color"].default_value = (0.018, 0.012, 0.018, 1.0)
    for cut, frame, parameters, _ in keyed:
        values = {
            name: parameters[name][0]
            for name in (
                "edge2OutlineDepthScaleMax",
                "edge2OutlineDepthScaleOffset",
                "edge2OutlineScale",
            )
            if parameters.get(name)
        }
        if values:
            edge2_parameters[cut] = values
        values = {
            name: parameters[name]
            for name in (
                "edgeColor",
                "edgeWeight0",
                "edgeWeight1",
                "edgePixel",
                "useExtrutedEdge",
                "writeDepthLumaThreshold",
            )
            if parameters.get(name)
        }
        if values:
            edge_parameters[cut] = values
        edge_color = parameters.get("edgeColor")
        if edge2_material_scale_socket is not None and edge_color and len(edge_color) >= 4:
            # `chr_toon_edge2.vfxo` multiplies the shell displacement by the
            # same cb4[29].w whose RGB components the edge PS writes.
            edge2_material_scale_socket.default_value = max(0.0, edge_color[3])
            edge2_material_scale_socket.keyframe_insert("default_value", frame=frame)
        scale = parameters.get("edge2OutlineScale")
        if edge2_scale_socket is not None and scale:
            # The 0.5 factor is applied after COLOR.b * depth_factor in the
            # Geometry Nodes graph, matching the native vertex shader.
            edge2_scale_socket.default_value = max(0.0, scale[0]) * 0.01
            edge2_scale_socket.keyframe_insert("default_value", frame=frame)
        depth_max = parameters.get("edge2OutlineDepthScaleMax")
        if edge2_max_socket is not None and depth_max:
            edge2_max_socket.default_value = depth_max[0]
            edge2_max_socket.keyframe_insert("default_value", frame=frame)
        depth_offset = parameters.get("edge2OutlineDepthScaleOffset")
        if edge2_offset_socket is not None and depth_offset:
            edge2_offset_socket.default_value = depth_offset[0]
            edge2_offset_socket.keyframe_insert("default_value", frame=frame)
    if scene.world is None:
        scene.world = bpy.data.worlds.new(f"{directory.name} World")
    light_keys = []
    for _, _, _, slots in keyed:
        for slot in slots:
            key = slot["id"]
            if key not in light_keys:
                light_keys.append(key)
    light_collection = bpy.data.collections.new(f"{directory.name} Event Lights")
    scene.collection.children.link(light_collection)
    light_objects = []
    light_data_blocks = []
    for index, key in enumerate(light_keys):
        source = next(
            slot.get("source", "Character Light")
            for _, _, _, slots in keyed
            for slot in slots
            if slot["id"] == key
        )
        light_data = bpy.data.lights.new(
            f"{directory.name} {Path(str(source)).stem} Light {index + 1:02d}", "SUN"
        )
        light_data.use_shadow = True
        light_object = bpy.data.objects.new(light_data.name, light_data)
        light_object["g4_event_light"] = True
        light_object["g4_viewport_light_visibility"] = 1
        light_collection.objects.link(light_object)
        light_objects.append(light_object)
        light_data_blocks.append(light_data)

    for cut, frame, parameters, slots in keyed:
        slots_by_id = {slot["id"]: slot for slot in slots}
        for index, (light_object, light_data) in enumerate(zip(light_objects, light_data_blocks)):
            slot = slots_by_id.get(light_keys[index])
            light_object.hide_render = slot is None
            light_object.hide_viewport = slot is None
            light_object.keyframe_insert("hide_render", frame=frame)
            light_object.keyframe_insert("hide_viewport", frame=frame)
            if slot is None:
                continue
            direction = slot.get("direction")
            if direction and len(direction) >= 3:
                vector = SOURCE_TO_BLENDER.to_3x3() @ Vector(direction[:3])
                if vector.length_squared > 1e-8:
                    light_object.rotation_euler = vector.normalized().to_track_quat("-Z", "Y").to_euler()
                    light_object.keyframe_insert("rotation_euler", frame=frame)
            color = slot.get("color")
            if color and len(color) >= 3:
                light_data.color = tuple(max(0.0, value) for value in color[:3])
                light_data.energy = max(0.0, color[3] if len(color) > 3 else 1.0)
            energy = slot.get("energy")
            if energy:
                light_data.energy = max(0.0, energy[0])
            if color or energy:
                light_data.keyframe_insert("color", frame=frame)
                light_data.keyframe_insert("energy", frame=frame)
        ambient = parameters.get("charaAmbient")
        if ambient and len(ambient) >= 3:
            scene.world.color = tuple(max(0.0, value) for value in ambient[:3])
            scene.world.keyframe_insert("color", frame=frame)
    for owner in (*light_objects, *light_data_blocks, scene.world):
        animation = owner.animation_data
        if animation and animation.action:
            for curve in action_fcurves(animation.action):
                for point in curve.keyframe_points:
                    point.interpolation = "CONSTANT"

    animated_materials = 0
    for material in bpy.data.materials:
        if material.node_tree is None or not material.get("g4_level5_toon"):
            continue
        highlight_node = material.node_tree.nodes.get("G4 Highlight")
        underlight_node = material.node_tree.nodes.get("G4 Under Light")
        ambient_node = material.node_tree.nodes.get("G4 Character Ambient")
        primary_shadow = material.node_tree.nodes.get("G4 Shadow Color 0")
        secondary_shadow = material.node_tree.nodes.get("G4 Shadow Color 1")
        primary_shadow_rate = material.node_tree.nodes.get("G4 Shadow Rate 0")
        secondary_shadow_rate = material.node_tree.nodes.get("G4 Shadow Rate 1")
        shadow_blend = material.node_tree.nodes.get("G4 Dual Toon Ramp")
        under_rim_width = material.node_tree.nodes.get("G4 Under Rim Width")
        under_rim_strength = material.node_tree.nodes.get("G4 Under Rim Strength")
        keyed_material = False
        for _, frame, parameters, _ in keyed:
            if primary_shadow is not None and primary_shadow.type == "RGB":
                from ..shading.character_lighting import animate_capture_lighting
                animate_capture_lighting(material.node_tree.nodes, parameters, frame)
                keyed_material = True
                continue

            for node, parameter_names in (
                (highlight_node, ("charaHighLightColor", "charaHighColor")),
                (underlight_node, ("charaUnderRimColor", "charaUnderRim")),
                (ambient_node, ("charaAmbient",)),
            ):
                values = next(
                    (
                        parameters.get(name)
                        for name in parameter_names
                        if parameters.get(name)
                    ),
                    None,
                )
                if node is None or not values or len(values) < 3:
                    continue
                intensity = values[3] if len(values) > 3 and node is not ambient_node else 1.0
                node.inputs[2].default_value = tuple(
                    max(0.0, value * intensity) for value in values[:3]
                ) + (1.0,)
                node.inputs[2].keyframe_insert("default_value", frame=frame)
                keyed_material = True
            high_threshold = parameters.get("charaHighThreshold")
            if high_threshold and primary_shadow is not None and secondary_shadow is not None:
                # The game expresses this threshold around 1.0; Blender's ramps
                # operate in 0..1, so mirror it around 2.0. Both native shadow
                # colors use this same terminator; their fourth component is
                # the per-band strength and is handled below.
                position = max(0.02, min(0.98, 2.0 - high_threshold[0]))
                primary_shadow.color_ramp.elements[-1].position = position
                secondary_shadow.color_ramp.elements[-1].position = position
                primary_shadow.color_ramp.elements[-1].keyframe_insert("position", frame=frame)
                secondary_shadow.color_ramp.elements[-1].keyframe_insert("position", frame=frame)
                keyed_material = True
            for node, color_names, rate_name, node_rate in (
                (
                    primary_shadow,
                    ("charaShadowColor1", "charaShadow1"),
                    "charaShadowRate1",
                    primary_shadow_rate,
                ),
                (
                    secondary_shadow,
                    ("charaShadowColor2", "charaShadow2"),
                    "charaShadowRate2",
                    secondary_shadow_rate,
                ),
            ):
                values = next(
                    (
                        parameters.get(name)
                        for name in color_names
                        if parameters.get(name)
                    ),
                    None,
                )
                if node is not None and values and len(values) >= 3:
                    node.color_ramp.elements[0].color = tuple(
                        max(0.0, value) for value in values[:3]
                    ) + (1.0,)
                    node.color_ramp.elements[0].keyframe_insert("color", frame=frame)
                    keyed_material = True
                    if len(values) >= 4:
                        rate = values[3]
                    else:
                        rate_values = parameters.get(rate_name)
                        rate = rate_values[0] if rate_values else None
                    if rate is not None and node_rate is not None:
                        node_rate.inputs[0].default_value = max(0.0, min(1.0, rate))
                        node_rate.inputs[0].keyframe_insert("default_value", frame=frame)
                        keyed_material = True
                    continue
                values = parameters.get(rate_name)
                if node is None or not values:
                    continue
                rate = max(0.0, min(1.0, values[0]))
                if node_rate is not None:
                    node_rate.inputs[0].default_value = rate
                    node_rate.inputs[0].keyframe_insert("default_value", frame=frame)
                    keyed_material = True
            shadow_blend_rate = parameters.get("charaShadowBlendRate")
            if shadow_blend is not None and shadow_blend_rate:
                rate = max(0.0, shadow_blend_rate[0])
                shadow_blend.inputs[0].default_value = rate / (1.0 + rate)
                shadow_blend.inputs[0].keyframe_insert("default_value", frame=frame)
                keyed_material = True
            under_rim_rate = parameters.get("charaUnderRimRate")
            if under_rim_rate:
                rate = max(0.0, under_rim_rate[0])
                if under_rim_width is not None:
                    under_rim_width.inputs[1].default_value = max(0.25, rate * 2.0)
                    under_rim_width.inputs[1].keyframe_insert("default_value", frame=frame)
                    keyed_material = True
                if under_rim_strength is not None:
                    under_rim_strength.inputs[1].default_value = min(1.0, rate * 0.33)
                    under_rim_strength.inputs[1].keyframe_insert("default_value", frame=frame)
                    keyed_material = True
        if keyed_material:
            animated_materials += 1
            animation = material.node_tree.animation_data
            if animation and animation.action:
                for curve in action_fcurves(animation.action):
                    for point in curve.keyframe_points:
                        point.interpolation = "CONSTANT"
    scene["g4_event_animated_materials"] = animated_materials
    scene["g4_event_light_parameters"] = json.dumps(
        {cut: parameters for cut, _, parameters, _ in keyed}, sort_keys=True
    )
    scene["g4_event_edge2_parameters"] = json.dumps(edge2_parameters, sort_keys=True)
    scene["g4_event_edge_parameters"] = json.dumps(edge_parameters, sort_keys=True)
    scene["g4_event_lights"] = json.dumps(
        {cut: slots for cut, _, _, slots in keyed}, sort_keys=True
    )
    scene["g4_event_light_count"] = len(light_keys)
    from ..shading.character_lighting import optimize_event_lighting
    optimize_event_lighting(scene)
    return light_objects[0] if light_objects else None


def point_global_samples(motion: dict, target_name: str) -> list[Matrix]:
    skeleton = motion.get("skeleton") or {}
    names = skeleton.get("names") or []
    parents = skeleton.get("parents") or []
    rest = motion_rest_matrices(motion)
    tracks = {track.get("target_name"): track for track in motion.get("tracks") or []}
    try:
        target_index = names.index(target_name)
    except ValueError:
        return []

    result = []
    for frame_index in range(len(motion.get("frames") or [])):
        cache = {}

        def resolve(index: int) -> Matrix:
            cached = cache.get(index)
            if cached is not None:
                return cached
            name = names[index]
            track = tracks.get(name)
            if track is None:
                local = rest.get(name, Matrix.Identity(4))
            else:
                local = source_matrix(
                    {
                        "translation": track["values"]["translation"][frame_index],
                        "rotation": track["values"]["rotation"][frame_index],
                        "scale": track["values"]["scale"][frame_index],
                    }
                )
            parent = parents[index] if index < len(parents) else len(names)
            global_matrix = resolve(parent) @ local if 0 <= parent < len(names) and parent != index else local
            cache[index] = global_matrix
            return global_matrix

        source = resolve(target_index)
        result.append(SOURCE_TO_BLENDER @ source @ SOURCE_TO_BLENDER.inverted())
    return result


def append_event_placement(
    action,
    armature,
    motion: dict | None,
    target_name: str | None,
    model_base_matrix: Matrix | None = None,
    frame_origin: int | None = None,
    frame_count: int | None = None,
) -> int:
    if motion is None or not target_name:
        return 0
    matrices = point_global_samples(motion, target_name)
    if not matrices:
        return 0
    armature.animation_data_create()
    armature.animation_data.action = action
    origin = motion["clip"]["start_frame"] if frame_origin is None else frame_origin
    count = len(motion["frames"]) if frame_count is None else frame_count
    frames = list(range(1, count + 1))
    locations, rotations, scales = [], [], []
    previous_rotation = None
    base_matrix = model_base_matrix if model_base_matrix is not None else armature.matrix_basis.copy()
    base_location, base_rotation, base_scale = base_matrix.decompose()
    object_paths = (("location", base_location), ("rotation_quaternion", base_rotation), ("scale", base_scale))
    curves = {
        path: [action_fcurve_find(action, path, index) for index in range(len(default))]
        for path, default in object_paths
    }
    for frame in frames:
        # Point motion is another transform layer, outside the actor's root.
        # Sample before replacing object curves so extraction is not discarded.
        values = {
            path: [curve.evaluate(frame) if curve is not None else default[index]
                   for index, curve in enumerate(curves[path])]
            for path, default in object_paths
        }
        local = Matrix.LocRotScale(
            Vector(values["location"]), Quaternion(values["rotation_quaternion"]), Vector(values["scale"])
        )
        point_index = max(0, min(len(matrices) - 1, origin + frame - 1 - motion["frames"][0]))
        matrix = matrices[point_index] @ local
        location, rotation, scale = matrix.decompose()
        if previous_rotation is not None and previous_rotation.dot(rotation) < 0.0:
            rotation.negate()
        previous_rotation = rotation.copy()
        locations.append(tuple(location))
        rotations.append(tuple(rotation))
        scales.append(tuple(scale))
    for path_curves in curves.values():
        for curve in path_curves:
            if curve is not None:
                action_fcurve_remove(action, curve)
    armature.rotation_mode = "QUATERNION"
    cache = {}
    for data_path, samples in (
        ("location", locations),
        ("rotation_quaternion", rotations),
        ("scale", scales),
    ):
        reduced_frames, reduced_samples = simplify_motion_samples(frames, samples, None)
        for component in range(len(reduced_samples[0])):
            append_curve_samples(
                action,
                cache,
                data_path,
                component,
                "Event Placement",
                reduced_frames,
                [sample[component] for sample in reduced_samples],
                owner=armature,
            )
    action["g4_event_point"] = target_name
    return len(frames)


def decode_event_package(path: Path, skeleton_path: Path | None, temporary_directory: Path) -> tuple[dict, str]:
    entry_name, payload = select_g4mt_entry(path.read_bytes(), "0")
    extracted = temporary_directory / f"{path.stem}.g4mt"
    extracted.write_bytes(payload)
    try:
        return decode_motion(extracted, "0", skeleton_path), entry_name
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not decode {path.name} ({entry_name}): {exc}") from exc


def event_timeline_layout(
    packages: dict[str, list[Path]],
    temporary_directory: Path,
    camera_path: Path | None,
) -> tuple[int, int, dict[str, int]]:
    clips_by_cut = {}
    if camera_path is not None:
        for clip in parse_g4cm(camera_path)["clips"]:
            clips_by_cut[clip["name"]] = clip

    for paths in packages.values():
        for path in paths:
            cut = event_cut_name(path)
            if cut in clips_by_cut:
                continue
            _, payload = select_g4mt_entry(path.read_bytes(), "0")
            extracted = temporary_directory / f"range_{path.stem}.g4mt"
            extracted.write_bytes(payload)
            clips = parse_g4mt(extracted)["clips"][:1]
            if clips and cut:
                clips_by_cut[cut] = clips[0]
    if not clips_by_cut:
        raise RuntimeError("No G4MT clips found in the event packages")

    clips = sorted(clips_by_cut.values(), key=lambda clip: (clip["start_frame"], clip["end_frame"]))
    overlaps = any(clip["start_frame"] <= previous["end_frame"] for previous, clip in zip(clips, clips[1:]))
    if overlaps:
        frame = 1
        cut_starts = {}
        for cut in sorted(clips_by_cut, key=cut_sort_key):
            clip = clips_by_cut[cut]
            cut_starts[cut] = frame
            frame += clip["end_frame"] - clip["start_frame"] + 1
        return 0, frame - 1, cut_starts

    frame_origin = min(clip["start_frame"] for clip in clips)
    frame_end = max(clip["end_frame"] for clip in clips)
    cut_starts = {
        cut: 1 + clip["start_frame"] - frame_origin
        for cut, clip in clips_by_cut.items()
    }
    return frame_origin, frame_end, cut_starts


FACE_G4MA_CHANNEL = 32
FACE_ATLAS_COLUMNS = 4
FACE_ATLAS_ROWS = 2


def event_g4ma_path(package: Path) -> Path:
    """Return the material-animation sidecar stored beside an event G4PK."""
    return package.with_suffix("") / f"{package.stem}.g4ma"


def materialize_event_g4ma(package: Path, temporary_directory: Path) -> Path | None:
    """Resolve an event G4MA from its sidecar or from the package entry."""
    sidecar = event_g4ma_path(package)
    if sidecar.is_file():
        return sidecar
    if package.suffix.lower() != ".g4pk":
        return None
    try:
        data = package.read_bytes()
        entry = next(
            (
                (name, offset, size)
                for name, offset, size in g4pk_entries(data)
                if data[offset : offset + 4] == b"G4MA"
            ),
            None,
        )
    except (OSError, ValueError, struct.error):
        return None
    if entry is None:
        return None
    name, offset, size = entry
    destination = temporary_directory / f"{package.stem}_{Path(name).name}"
    try:
        destination.write_bytes(data[offset : offset + size])
    except OSError:
        return None
    return destination


def material_crc32b(material) -> int:
    """Resolve an imported material name back to the CRC used by G4MA."""
    stored_crc = material.get("g4_material_crc32b")
    if stored_crc is not None:
        return int(str(stored_crc), 16) & 0xFFFFFFFF
    name = re.sub(r"\.\d{3}$", "", material.name)
    return zlib.crc32(name.encode("ascii", errors="ignore")) & 0xFFFFFFFF


def actor_materials_by_crc(armature) -> dict[int, list[tuple[object, int, object]]]:
    materials = defaultdict(list)
    for obj in event_actor_objects(armature):
        if obj.type != "MESH":
            continue
        for index, slot in enumerate(obj.material_slots):
            if slot.material is not None:
                materials[material_crc32b(slot.material)].append((obj, index, slot.material))
    return materials


def actor_local_material(entries: list[tuple[object, int, object]], actor: str):
    """Give an actor its own material so simultaneous facial states do not clash."""
    source = entries[0][2]
    local_name = f"{source.name}__{actor}"
    material = bpy.data.materials.get(local_name)
    if material is None:
        material = source.copy()
        material.name = local_name
        # Blender custom-property integers are signed 32-bit.  Material CRCs
        # are unsigned, so retain their canonical hexadecimal representation.
        material["g4_material_crc32b"] = f"{material_crc32b(source):08x}"
    for obj, index, _ in entries:
        obj.material_slots[index].material = material
    return material


def face_uv_mapping(material):
    tree = material.node_tree
    if tree is None:
        return None
    nodes = tree.nodes
    base = nodes.get("G4 Base")
    if base is None or base.type != "TEX_IMAGE":
        return None
    mapping = nodes.get("G4 Face UV")
    if mapping is None:
        texture_coordinates = nodes.new("ShaderNodeTexCoord")
        texture_coordinates.name = "G4 Face UV Coordinates"
        texture_coordinates.label = "G4 Face UV Coordinates"
        texture_coordinates.location = (base.location.x - 430, base.location.y)
        mapping = nodes.new("ShaderNodeMapping")
        mapping.name = "G4 Face UV"
        mapping.label = "G4 Face Expression Atlas (4x2)"
        mapping.vector_type = "POINT"
        mapping.location = (base.location.x - 210, base.location.y)
        tree.links.new(texture_coordinates.outputs["UV"], mapping.inputs["Vector"])
        tree.links.new(mapping.outputs["Vector"], base.inputs["Vector"])
    # Face meshes already carry UVs for one atlas window.  G4MA moves that
    # window; it does not scale it again.  Scaling here would collapse the
    # eye/mouth geometry into a quarter-sized portion of the expression.
    mapping.inputs["Scale"].default_value = (1.0, 1.0, 1.0)
    return mapping


def animate_event_face_materials(
    armature,
    actor: str,
    package: Path,
    clip: dict,
    strip_start: int,
    duration: int,
    temporary_directory: Path,
) -> int:
    """Apply channel-32 G4MA material states as stepped 4x2 facial-atlas UV offsets."""
    path = materialize_event_g4ma(package, temporary_directory)
    if path is None:
        return 0
    try:
        motion = decode_material_motion(path, clip["name"])
    except (OSError, ValueError, struct.error):
        return 0

    materials_by_crc = actor_materials_by_crc(armature)
    animated = 0
    for source_track in motion["tracks"]:
        material_crc = int(source_track["target_hash"], 16)
        entries = materials_by_crc.get(material_crc)
        if not entries:
            continue
        curves = [curve for curve in source_track["curves"] if curve["channel_type"] == FACE_G4MA_CHANNEL]
        if not curves:
            continue
        material = actor_local_material(entries, actor)
        mapping = face_uv_mapping(material)
        if mapping is None:
            continue
        values = curves[0]["values"]
        if not values:
            continue
        action = bpy.data.actions.new(f"{actor}_{clip['name']}_{material.name}_Face")
        path_x = f'{mapping.path_from_id()}.inputs["Location"].default_value'
        curve_x = action_fcurve_new(action, material.node_tree, path_x, 0, "G4 Face Expressions")
        curve_y = action_fcurve_new(action, material.node_tree, path_x, 1, "G4 Face Expressions")
        previous = None
        for source_frame, value in zip(motion["frames"], values):
            state = int(round(value[0]))
            if state == previous:
                continue
            previous = state
            frame = source_frame - clip["start_frame"] + 1
            column = state % FACE_ATLAS_COLUMNS
            row = (state // FACE_ATLAS_COLUMNS) % FACE_ATLAS_ROWS
            # Source UVs begin in the top atlas row.  Later rows move down in
            # Blender UV space, while columns advance by one quarter-width.
            offset_x = column / FACE_ATLAS_COLUMNS
            offset_y = -row / FACE_ATLAS_ROWS
            for curve, offset in ((curve_x, offset_x), (curve_y, offset_y)):
                point = curve.keyframe_points.insert(frame, offset, options={"FAST"})
                point.interpolation = "CONSTANT"
        if not action_fcurves(action):
            bpy.data.actions.remove(action)
            continue
        tree = material.node_tree
        tree.animation_data_create()
        track = tree.animation_data.nla_tracks.get("G4 Face Expressions")
        if track is None:
            track = tree.animation_data.nla_tracks.new()
            track.name = "G4 Face Expressions"
        add_nla_strip(tree, track, action, clip["name"], strip_start, duration)
        material["g4_face_g4ma_source"] = str(path)
        material["g4_face_atlas_layout"] = "4x2"
        animated += 1
    return animated


def import_event_actor(
    actor: str,
    packages: list[Path],
    prefs,
    temporary_directory: Path,
    frame_origin: int,
    cut_starts: dict[str, int],
    progress,
    import_character_parts: bool,
    auto_character_parts: bool,
    character_part_stem: str = "",
    point_target: str = "",
    point_assignments: dict[str, tuple[str, str]] | None = None,
    point_motions: dict[tuple[str, str], dict] | None = None,
    head_model: str = "",
    body_model: str = "",
    shoes_model: str = "",
    accessory_model: str = "",
    skin_color: tuple[float, float, float, float] | None = None,
    gloves_model: str = "",
    armband_model: str = "",
    nameplate_model: str = "",
    attach_ball: bool = False,
    ball_model: str = "",
    manifest_model: str = "",
    debug_log: list[str] | None = None,
) -> tuple[object, int, list[tuple[str, int]]]:
    package_cuts = {event_cut_name(package) for package in packages}
    for cut, (point_source, target_name) in (point_assignments or {}).items():
        if cut not in package_cuts:
            continue
        point_motion = (point_motions or {}).get((cut, point_source))
        if (
            point_motion is None or not point_motion.get("frames")
            or target_name not in (point_motion.get("skeleton") or {}).get("names", ())
        ):
            raise RuntimeError(f"Missing event placement {cut}/{point_source}/{target_name} for {actor}")
    model_path = None
    if head_model:
        configured_model = Path(bpy.path.abspath(head_model))
        if configured_model.is_file():
            model_path = configured_model
        elif re.fullmatch(r"[A-Za-z]{1,3}\d{4,10}", head_model):
            alias_path = packages[0].with_name(f"{head_model}_s00_p00_c0000.g4pk")
            model_path = resolve_model_path(alias_path, getattr(prefs, "raw_data_root", ""))
    generic_actor = bool(re.fullmatch(r"c0{2,}\d{3,5}(?:_s\d+)?", actor, re.IGNORECASE))

    def resolve_manifest() -> Path | None:
        if not manifest_model:
            return None
        configured = Path(bpy.path.abspath(manifest_model))
        if configured.is_file():
            return configured
        if re.fullmatch(r"[A-Za-z]{1,3}\d{4,10}", manifest_model):
            alias_path = packages[0].with_name(f"{manifest_model}_s00_p00_c0000.g4pk")
            return resolve_model_path(alias_path, getattr(prefs, "raw_data_root", ""))
        return None

    if model_path is None:
        model_path = resolve_manifest()
    if model_path is None:
        model_path = resolve_model_path(packages[0], getattr(prefs, "raw_data_root", ""))
    if model_path is None:
        model_path = resolve_manifest()
    if model_path is None:
        raise RuntimeError(f"Could not resolve model {actor} for the event")
    if debug_log is not None:
        debug_log.append(f"{actor}: model={model_path}")
    skeleton_path = resolve_skeleton_path(model_path)
    if skeleton_path is None and generic_actor:
        skeleton_path = resolve_generic_event_skeleton(packages[0], actor, prefs)
    first_motion, first_entry = decode_event_package(packages[0], skeleton_path, temporary_directory)
    if debug_log is not None:
        debug_log.append(
            f"{actor}: skeleton={skeleton_path or '<embedded/auto>'}; "
            f"first_clip={first_motion['clip']['name']}; targets={len(track_bone_names(first_motion))}"
        )
    armature, resolved_model = import_model_for_animation(
        packages[0],
        first_motion,
        prefs,
        model_path,
        bool(body_model or shoes_model or accessory_model or gloves_model or armband_model or nameplate_model or attach_ball),
        False,
        body_model,
        shoes_model,
        accessory_model,
        gloves_model,
        armband_model,
        nameplate_model,
        attach_ball,
        ball_model,
        character_part_stem=character_part_stem,
        # Event G4MT skeletons can carry a motion-local rest pose that differs
        # from the native bind skeleton in the model package.  Replacing the
        # native rest bones here breaks the skin bind and stretches the mesh;
        # the event action retargets onto the imported native rest instead.
        align_to_motion_rest=False,
        force_character_styling=True,
    )
    importer = model_importer_module()
    apply_skin_color = getattr(importer, "apply_character_skin_mask_color", None) if importer is not None else None
    resolved_skin_color = (
        event_skin_color_value(skin_color)
        if skin_color is not None
        else event_skin_color_from_head(str(resolved_model), prefs)
    )
    if apply_skin_color is not None and resolved_skin_color is not None:
        apply_skin_color(event_actor_objects(armature), resolved_skin_color)
        armature["g4_skin_color_rgba"] = "".join(f"{round(channel * 255):02X}" for channel in resolved_skin_color)
    display_actor = actor
    if generic_actor and head_model:
        display_actor = f"{model_path.stem}_{event_actor_slot(actor)}"
    armature.name = display_actor
    armature.data.name = f"{display_actor}_Armature"
    if debug_log is not None:
        debug_log.append(f"{actor}: armature={armature.name}; bones={len(armature.data.bones)}")
    armature.animation_data_create()
    armature.animation_data.action = None
    track = armature.animation_data.nla_tracks.new()
    track.name = "G4 Event Cuts"
    clear_pose(armature)
    event_root_base_matrix = armature.matrix_basis.copy()

    keyed_bones = 0
    cut_frames = []
    for index, package in enumerate(packages):
        if index == 0:
            motion, entry_name = first_motion, first_entry
        else:
            motion, entry_name = decode_event_package(package, skeleton_path, temporary_directory)
        resolve_track_names_from_armature(motion, armature)
        clip = motion["clip"]
        event_root_name = event_root_motion_name(motion, armature)
        action, keyed = create_action(
            armature,
            motion,
            clip["start_frame"],
            event_root_name=event_root_name,
        )
        event_root_frames = 0
        if event_root_name:
            event_root_frames = extract_event_root_motion(
                action,
                motion,
                event_root_name,
                event_root_base_matrix,
                armature,
            )
            if not event_root_frames:
                root_motion = dict(motion, tracks=[item for item in motion["tracks"]
                                                  if item.get("target_name") == event_root_name])
                append_motion_to_action(armature, root_motion, action, clip["start_frame"], event_root_name="")
            if event_root_frames:
                action["g4_event_root_bone"] = event_root_name
                action["g4_event_root_space"] = "armature_object_local_delta"
                action["g4_event_root_base_matrix"] = [
                    float(event_root_base_matrix[row][column])
                    for row in range(4)
                    for column in range(4)
                ]
        substituted_model = bool(
            head_model
            or body_model
            or shoes_model
            or accessory_model
            or gloves_model
            or armband_model
            or nameplate_model
            or manifest_model
        )
        if substituted_model:
            remove_nonroot_translation_curves(action, armature)
        cut = clip["name"] or event_cut_name(package) or f"cut_{index:03d}"
        placement_frames = 0
        assignment = (point_assignments or {}).get(cut)
        if assignment is not None:
            point_source, target_name = assignment
            point_motion = (point_motions or {}).get((cut, point_source))
            placement_frames = append_event_placement(
                action, armature, point_motion, target_name, event_root_base_matrix,
                frame_origin=clip["start_frame"], frame_count=len(motion["frames"]),
            )
            if not placement_frames:
                raise RuntimeError(f"Missing event placement {cut}/{point_source}/{target_name} for {actor}")
            action["g4_event_point_source"] = point_source
        action.name = f"{actor}_{cut}"
        action["g4mt_source"] = str(package)
        action["g4pk_entry"] = entry_name
        action["g4_model_source"] = str(resolved_model)
        if skeleton_path is not None:
            action["g4_skeleton_source"] = str(skeleton_path)
        action["g4_source_frame_origin"] = frame_origin
        duration = len(motion["frames"])
        strip_start = cut_starts.get(cut, 1 + clip["start_frame"] - frame_origin)
        following_starts = [start for start in cut_starts.values() if start > strip_start]
        if following_starts:
            duration = min(duration, min(following_starts) - strip_start)
        try:
            add_nla_strip(armature, track, action, cut, strip_start, duration)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Could not add actor strip {actor}/{cut} at {strip_start} for {duration} frames"
            ) from exc
        animate_character_parts(
            armature,
            motion,
            clip["start_frame"],
            strip_start,
            duration,
            cut,
            rotation_only_retarget=substituted_model,
        )
        face_materials = animate_event_face_materials(
            armature,
            actor,
            package,
            clip,
            strip_start,
            duration,
            temporary_directory,
        )
        if debug_log is not None:
            debug_log.append(
                f"{actor}/{cut}: keyed={keyed}; frames={duration}; "
                f"fcurves={len(action_fcurves(action))}; event_root={event_root_name or '<none>'}; "
                f"event_root_frames={event_root_frames}; placement_frames={placement_frames}; face_materials={face_materials}; "
                f"strip_start={strip_start}"
            )
        cut_frames.append((cut, strip_start))
        keyed_bones += keyed
        yield progress(f"Imported event cut: {cut}")
    armature.animation_data.action = None
    animate_event_actor_visibility(armature, {cut for cut, _ in cut_frames}, cut_starts)
    return armature, keyed_bones, cut_frames


def event_actor_objects(armature) -> set[object]:
    related_armatures = {
        obj for obj in bpy.data.objects
        if obj.type == "ARMATURE" and (
            obj == armature
            or any(
                constraint.name == "G4 Character Part Actor" and constraint.target == armature
                for constraint in obj.constraints
            )
        )
    }
    objects = set(related_armatures)
    objects.update(
        obj for obj in bpy.data.objects
        if obj.type == "MESH" and any(
            modifier.type == "ARMATURE" and modifier.object in related_armatures
            for modifier in obj.modifiers
        )
    )
    return objects


def animate_event_actor_visibility(armature, active_cuts: set[str], cut_starts: dict[str, int]) -> None:
    if not cut_starts:
        return
    objects = event_actor_objects(armature)
    first_frame = min(cut_starts.values())
    for obj in objects:
        obj.hide_viewport = True
        obj.hide_render = True
        obj.keyframe_insert("hide_viewport", frame=max(0, first_frame - 1))
        obj.keyframe_insert("hide_render", frame=max(0, first_frame - 1))
        for cut, frame in sorted(cut_starts.items(), key=lambda item: item[1]):
            hidden = cut not in active_cuts
            obj.hide_viewport = hidden
            obj.hide_render = hidden
            obj.keyframe_insert("hide_viewport", frame=frame)
            obj.keyframe_insert("hide_render", frame=frame)
        animation = obj.animation_data
        if animation and animation.action:
            for curve in action_fcurves(animation.action):
                if curve.data_path not in {"hide_viewport", "hide_render"}:
                    continue
                for point in curve.keyframe_points:
                    point.interpolation = "CONSTANT"


def import_event_camera(
    path: Path,
    collection,
    frame_origin: int,
    cut_starts: dict[str, int],
    progress,
):
    parsed = parse_g4cm(path)
    clips = sorted(parsed["clips"], key=lambda clip: clip["start_frame"])
    camera_object = None
    transform_track = None
    lens_track = None
    for clip in clips:
        motion = decode_camera(path, clip["name"])
        imported, _ = create_camera_animation(path, motion, collection, clip["start_frame"])
        transform_action = imported.animation_data.action
        lens_action = imported.data.animation_data.action
        duration = len(motion["frames"])
        start = cut_starts.get(clip["name"], 1 + clip["start_frame"] - frame_origin)
        if camera_object is None:
            camera_object = imported
            camera_object.name = f"{path.stem}_Scene"
            camera_object.data.name = f"{path.stem}_Scene"
            camera_object.animation_data.action = None
            camera_object.data.animation_data.action = None
            transform_track = camera_object.animation_data.nla_tracks.new()
            transform_track.name = "G4 Camera Cuts"
            lens_track = camera_object.data.animation_data.nla_tracks.new()
            lens_track.name = "G4 Lens Cuts"
        else:
            camera_data = imported.data
            imported.animation_data.action = None
            camera_data.animation_data.action = None
            bpy.data.objects.remove(imported, do_unlink=True)
            bpy.data.cameras.remove(camera_data)
        try:
            add_nla_strip(camera_object, transform_track, transform_action, clip["name"], start, duration)
            add_nla_strip(camera_object.data, lens_track, lens_action, clip["name"], start, duration)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Could not add camera strip {clip['name']} at {start} for {duration} frames"
            ) from exc
        yield progress(f"Importing event camera: {clip['name']}")
    if camera_object is None:
        raise RuntimeError(f"No camera clips found in {path}")
    camera_object["g4cm_source"] = str(path)
    camera_object["g4_source_frame_origin"] = frame_origin
    return camera_object, len(clips)


def invoke_event_parts_dialog(
    directory: Path,
    import_camera: bool,
    import_effects: bool,
    actors: list[str],
) -> None:
    defer_blender_call(
        lambda: bpy.ops.import_scene.level5_g4_event_parts(
            "INVOKE_DEFAULT",
            directory=str(directory),
            import_camera=import_camera,
            import_effects=import_effects,
            actors_json=json.dumps(actors),
        ),
        label="Open event character slots",
    )


def write_event_import_log(lines: list[str]) -> None:
    text = bpy.data.texts.get("G4 Event Import Log")
    if text is None:
        text = bpy.data.texts.new("G4 Event Import Log")
    text.clear()
    text.write("\n".join(lines) + "\n")


EVENT_PART_OVERRIDES = {
    "ev72_50010": {
        "c11010019": {"body": "u11010018", "shoes": "s11010018"},
        "c11010069": {"body": "u117692", "shoes": "s117691"},
    },
}


def is_base_uniform_part_path(value: str) -> bool:
    """Identify a persisted part fallback such as _uniform/u000101/u000101.g4md."""
    if not value:
        return False
    path = Path(bpy.path.abspath(str(value)))
    return (
        path.parent.name.casefold() == path.stem.casefold()
        and path.parent.parent.name.casefold() == "_uniform"
    )


def event_part_needs_default(value: str, key: str, overrides: dict[str, str]) -> bool:
    """Use event_cfg for empty or legacy base parts while preserving explicit variants."""
    return (
        not value
        or key in overrides
        or (key in {"body", "shoes", "accessory"} and is_base_uniform_part_path(value))
    )


def event_part_defaults(directory: Path, actor: str, prefs) -> dict[str, str]:
    actor_id = event_actor_base_id(actor)
    match = re.fullmatch(r"c(\d{6,8})", actor_id, re.IGNORECASE)
    if match is None:
        return {}
    actor_overrides = EVENT_PART_OVERRIDES.get(directory.name, {}).get(actor_id, {})
    result = {}
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        uniform_root = data_root / "common" / "chr" / "_uniform"
        effective_head = None
        event_models = resolve_event_actor_models(directory, prefs)
        manifest_model = event_models.get(actor, event_models.get(actor_id, ""))
        if manifest_model and re.fullmatch(r"[A-Za-z]{1,3}\d{4,10}", manifest_model):
            alias_path = directory / f"{manifest_model}_s00_p00_c0000.g4pk"
            effective_head = resolve_model_path(alias_path, getattr(prefs, "raw_data_root", ""))
        if effective_head is None:
            packages = collect_event_packages(directory).get(actor) or []
            if packages:
                effective_head = resolve_model_path(packages[0], getattr(prefs, "raw_data_root", ""))
        if effective_head is not None:
            result.update(event_part_defaults_from_head(str(effective_head), prefs))
        for key, prefix in (("body", "u"), ("shoes", "s")):
            stem = actor_overrides.get(key, f"{prefix}{match.group(1)}")
            for extension in (".g4pkm", ".g4md"):
                candidate = uniform_root / stem / f"{stem}{extension}"
                if candidate.is_file():
                    if key in actor_overrides:
                        result[key] = str(candidate)
                    else:
                        result.setdefault(key, str(candidate))
                    break
            if key not in result and uniform_root.is_dir():
                matches = sorted(uniform_root.glob(f"*/{stem}.g4m[dm]"))
                if matches:
                    if key in actor_overrides:
                        result[key] = str(matches[0])
                    else:
                        result.setdefault(key, str(matches[0]))

        # Event imports can bypass the parts dialog (saved assignments,
        # scripts, and direct operator calls).  Those paths still need the
        # matching sk000xxx arms mesh, which is derived from the selected
        # body's model family rather than from the event actor id.
        importer = model_importer_module()
        find_accessory = getattr(importer, "find_accessory_part_for_body", None) if importer is not None else None
        if "body" not in result:
            if effective_head is not None:
                result.update(event_part_defaults_from_head(str(effective_head), prefs))
        if "accessory" not in result and find_accessory is not None and result.get("body"):
            accessory = find_accessory(Path(result["body"]), prefs)
            if accessory is not None:
                result["accessory"] = str(accessory)
        elif "body" in actor_overrides and find_accessory is not None and result.get("body"):
            accessory = find_accessory(Path(result["body"]), prefs)
            if accessory is not None:
                result["accessory"] = str(accessory)
        if result:
            break
    return result


def event_uses_modular_characters(directory: Path, prefs) -> bool:
    """Return whether this data set assembles characters from face/uniform parts."""
    inferred = inferred_raw_data_root(directory)
    if inferred is not None:
        character_root = inferred / "common" / "chr"
        return (character_root / "_face").is_dir() or (character_root / "_uniform").is_dir()
    configured_root = getattr(prefs, "raw_data_root", "")
    for data_root in candidate_data_roots(directory, configured_root):
        character_root = data_root / "common" / "chr"
        if (character_root / "_face").is_dir() or (character_root / "_uniform").is_dir():
            return True
    return False


def model_importer_module():
    """Return the loaded model-importer package without creating an import cycle."""
    return sys.modules.get(__package__.split(".")[0]) if __package__ else None


def resolve_event_setup_actor_model(directory: Path, actor: str, prefs) -> Path | None:
    importer = model_importer_module()
    resolver = getattr(importer, "event_setup_actor_model", None) if importer is not None else None
    if resolver is None:
        return None
    return resolver(directory, actor, prefs)


def event_part_defaults_from_head(head_model: str, prefs) -> dict[str, str]:
    """Resolve modular defaults through the model importer's CFG-aware lookup."""
    head_path = Path(bpy.path.abspath(head_model)) if head_model else None
    importer = model_importer_module()
    if head_path is None or not head_path.is_file() or importer is None:
        return {}
    find_part = getattr(importer, "find_character_part", None)
    find_accessory = getattr(importer, "find_accessory_part_for_body", None)
    if find_part is None:
        return {}
    body = find_part(head_path, "u", prefs)
    if body is not None:
        body = importer.declared_body_variant_for_character(body, head_path, prefs)
    shoes = find_part(head_path, "s", prefs)
    accessory = find_accessory(body, prefs) if body is not None and find_accessory is not None else None
    return {
        key: str(path)
        for key, path in (("body", body), ("shoes", shoes), ("accessory", accessory))
        if path is not None
    }


def event_skin_color_from_head(head_model: str, prefs) -> tuple[float, float, float, float] | None:
    importer = model_importer_module()
    resolver = getattr(importer, "character_skin_color_from_model", None) if importer is not None else None
    if resolver is None or not head_model:
        return None
    path = Path(bpy.path.abspath(head_model))
    if not path.is_file():
        return None
    try:
        return resolver(path, prefs)
    except (OSError, ValueError, TypeError):
        return None


def event_skin_color_value(value, fallback=(1.0, 1.0, 1.0, 1.0)) -> tuple[float, float, float, float]:
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) in {6, 8}:
            try:
                packed = int(text, 16)
            except ValueError:
                packed = None
            if packed is not None:
                if len(text) == 6:
                    packed = (packed << 8) | 0xFF
                return tuple(((packed >> shift) & 0xFF) / 255.0 for shift in (24, 16, 8, 0))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        channels = tuple(max(0.0, min(1.0, float(channel))) for channel in value[:4])
        return channels if len(channels) == 4 else (*channels, 1.0)
    return fallback


def event_accessory_from_body(body_model: str, prefs) -> str:
    importer = model_importer_module()
    find_accessory = getattr(importer, "find_accessory_part_for_body", None) if importer is not None else None
    body_path = Path(bpy.path.abspath(body_model)) if body_model else None
    if find_accessory is None or body_path is None or not body_path.is_file():
        return ""
    accessory = find_accessory(body_path, prefs)
    return str(accessory) if accessory is not None else ""


def event_part_head_changed(item, _context) -> None:
    defaults = event_part_defaults_from_head(item.head_model, addon_preferences())
    for key, value in defaults.items():
        setattr(item, f"{key}_model", value)
    color = event_skin_color_from_head(item.head_model, addon_preferences())
    if color is not None:
        item.skin_color = color


class G4EventCharacterPart(PropertyGroup):
    actor_id: StringProperty()
    actor_ids: StringProperty()
    head_model: StringProperty(name="Head", subtype="FILE_PATH", update=event_part_head_changed)
    body_model: StringProperty(name="Body", subtype="FILE_PATH")
    shoes_model: StringProperty(name="Shoes", subtype="FILE_PATH")
    accessory_model: StringProperty(name="Arms/Neck", subtype="FILE_PATH")
    skin_color: FloatVectorProperty(
        name="Skin Mask",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(1.0, 1.0, 1.0, 1.0),
    )
    gloves_model: StringProperty(name="Gloves", subtype="FILE_PATH")
    armband_model: StringProperty(name="Captain Armband", subtype="FILE_PATH")
    nameplate_model: StringProperty(name="Nameplate", subtype="FILE_PATH")
    attach_ball: BoolProperty(name="Attach Ball", default=False)
    ball_model: StringProperty(name="Ball", subtype="FILE_PATH")


class IMPORT_OT_level5_g4_event_parts(Operator):
    bl_idname = "import_scene.level5_g4_event_parts"
    bl_label = "Event Character Parts"

    directory: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    import_camera: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})
    import_effects: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    actors_json: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    parts: CollectionProperty(type=G4EventCharacterPart, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        prefs = addon_preferences()
        try:
            saved = json.loads(getattr(prefs, "event_character_parts", "{}") or "{}")
        except json.JSONDecodeError:
            saved = {}
        directory = Path(bpy.path.abspath(self.directory))
        packages = collect_event_packages(directory)
        self.parts.clear()
        actors = json.loads(self.actors_json or "[]")
        generic_groups = generic_actor_groups(actors)
        generic_actors = {actor for slot_actors in generic_groups.values() for actor in slot_actors}
        for slot, slot_actors in sorted(generic_groups.items()):
            item = self.parts.add()
            item.actor_id = f"Generic character {slot}"
            item.actor_ids = json.dumps(slot_actors)
            generic_saved = saved.get(f"__generic_{slot}__") or {}
            if not generic_saved and slot == "s00":
                generic_saved = saved.get("__generic__") or {}
            event_defaults = event_part_defaults(directory, slot_actors[0], prefs) if slot_actors else {}
            default_head = (
                resolve_event_setup_actor_model(directory, slot_actors[0], prefs)
                if slot_actors else None
            )
            item.head_model = generic_saved.get("head") or (str(default_head) if default_head else "")
            item.skin_color = event_skin_color_value(
                generic_saved.get("skin_color"),
                event_skin_color_from_head(item.head_model, prefs) or item.skin_color,
            )
            generic_overrides = EVENT_PART_OVERRIDES.get(directory.name, {}).get(slot_actors[0], {}) if slot_actors else {}
            item.body_model = (
                event_defaults.get("body", "")
                if event_part_needs_default(generic_saved.get("body", ""), "body", generic_overrides)
                else generic_saved.get("body", "")
            )
            item.shoes_model = (
                event_defaults.get("shoes", "")
                if event_part_needs_default(generic_saved.get("shoes", ""), "shoes", generic_overrides)
                else generic_saved.get("shoes", "")
            )
            item.accessory_model = (
                event_defaults.get("accessory", "")
                if event_part_needs_default(generic_saved.get("accessory", ""), "accessory", generic_overrides)
                else generic_saved.get("accessory", "")
            )
            item.gloves_model = generic_saved.get("gloves", "")
            item.armband_model = generic_saved.get("armband", "")
            item.nameplate_model = generic_saved.get("nameplate", "")
            item.attach_ball = bool(generic_saved.get("attach_ball", False))
            item.ball_model = generic_saved.get("ball", "")
        for actor in actors:
            if actor in generic_actors:
                continue
            item = self.parts.add()
            item.actor_id = actor
            item.actor_ids = json.dumps([actor])
            actor_parts = dict(saved.get(actor) or {})
            if not actor_parts.get("head") and packages.get(actor):
                default_head = resolve_model_path(packages[actor][0], getattr(prefs, "raw_data_root", ""))
                if default_head is not None:
                    actor_parts["head"] = str(default_head)
            actor_skin_color = event_skin_color_from_head(actor_parts.get("head", ""), prefs)
            if actor_parts.get("head"):
                actor_parts.update({
                    key: value
                    for key, value in event_part_defaults_from_head(actor_parts["head"], prefs).items()
                    if not actor_parts.get(key)
                })
            defaults = event_part_defaults(directory, actor, prefs)
            overrides = EVENT_PART_OVERRIDES.get(directory.name, {}).get(actor, {})
            for key, value in defaults.items():
                if event_part_needs_default(actor_parts.get(key, ""), key, overrides):
                    actor_parts[key] = value
            if "body" in EVENT_PART_OVERRIDES.get(directory.name, {}).get(actor, {}):
                accessory = event_accessory_from_body(actor_parts.get("body", ""), prefs)
                if accessory:
                    actor_parts["accessory"] = accessory
            item.head_model = actor_parts.get("head", "")
            item.skin_color = event_skin_color_value(
                actor_parts.get("skin_color"), actor_skin_color or item.skin_color
            )
            item.body_model = actor_parts.get("body", "")
            item.shoes_model = actor_parts.get("shoes", "")
            item.accessory_model = actor_parts.get("accessory", "")
            item.gloves_model = actor_parts.get("gloves", "")
            item.armband_model = actor_parts.get("armband", "")
            item.nameplate_model = actor_parts.get("nameplate", "")
            item.attach_ball = bool(actor_parts.get("attach_ball", False))
            item.ball_model = actor_parts.get("ball", "")
        return context.window_manager.invoke_props_dialog(self, width=820)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Configure each named actor or generic slot once. Changing Head re-fills its modular parts.")
        for item in self.parts:
            box = layout.box()
            box.label(text=item.actor_id, icon="ARMATURE_DATA")
            actor_ids = json.loads(item.actor_ids or "[]")
            if len(actor_ids) > 1:
                box.label(text=f"Chooses one of {len(actor_ids)} compatible skeleton variants")
            box.prop(item, "head_model")
            box.prop(item, "body_model")
            box.prop(item, "shoes_model")
            box.prop(item, "accessory_model")
            box.prop(item, "skin_color")
            box.prop(item, "gloves_model")
            box.prop(item, "armband_model")
            box.prop(item, "nameplate_model")
            box.prop(item, "attach_ball")
            if item.attach_ball:
                box.prop(item, "ball_model")

    def execute(self, context):
        selected = {}
        for item in self.parts:
            actor_parts = {
                "head": bpy.path.abspath(item.head_model) if item.head_model else "",
                "body": bpy.path.abspath(item.body_model) if item.body_model else "",
                "shoes": bpy.path.abspath(item.shoes_model) if item.shoes_model else "",
                "accessory": bpy.path.abspath(item.accessory_model) if item.accessory_model else "",
                "skin_color": list(item.skin_color),
                "gloves": bpy.path.abspath(item.gloves_model) if item.gloves_model else "",
                "armband": bpy.path.abspath(item.armband_model) if item.armband_model else "",
                "nameplate": bpy.path.abspath(item.nameplate_model) if item.nameplate_model else "",
                "attach_ball": item.attach_ball,
                "ball": bpy.path.abspath(item.ball_model) if item.ball_model else "",
            }
            for key in ("head", "body", "shoes", "accessory", "gloves", "armband", "nameplate", "ball"):
                path_value = actor_parts[key]
                path = Path(path_value) if path_value else None
                if path is not None and (not path.is_file() or path.suffix.lower() not in {".g4md", ".g4pkm"}):
                    self.report({"ERROR"}, f"Character part not found or unsupported: {path}")
                    return {"CANCELLED"}
            actor_ids = json.loads(item.actor_ids or "[]") or [item.actor_id]
            if len(actor_ids) > 1:
                chosen = compatible_generic_actor(actor_ids, actor_parts["head"])
                for actor_id in actor_ids:
                    selected[actor_id] = actor_parts if actor_id == chosen else {"skip": True}
            else:
                selected[actor_ids[0]] = actor_parts

        prefs = addon_preferences()
        try:
            saved = json.loads(getattr(prefs, "event_character_parts", "{}") or "{}")
        except json.JSONDecodeError:
            saved = {}
        saved.update(selected)
        for generic_item in (
            item for item in self.parts if len(json.loads(item.actor_ids or "[]")) > 1
        ):
            actor_ids = json.loads(generic_item.actor_ids)
            chosen = compatible_generic_actor(actor_ids, generic_item.head_model)
            saved[f"__generic_{event_actor_slot(chosen)}__"] = selected[chosen]
        prefs.event_character_parts = json.dumps(saved, sort_keys=True)
        try:
            bpy.ops.wm.save_userpref()
        except RuntimeError:
            pass

        character_parts_json = json.dumps(selected)
        directory = self.directory
        import_camera = self.import_camera
        import_effects = self.import_effects
        defer_blender_call(
            lambda: bpy.ops.import_scene.level5_g4_event_folder(
                "EXEC_DEFAULT",
                directory=directory,
                import_camera=import_camera,
                import_effects=import_effects,
                prompt_character_parts=False,
                character_parts_json=character_parts_json,
            )
        )
        return {"FINISHED"}


class IMPORT_OT_level5_g4_event_folder(Operator):
    bl_idname = "import_scene.level5_g4_event_folder"
    bl_label = "Import Level-5 G4 Event Folder"
    bl_options = {"REGISTER", "UNDO"}

    progress_text: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    progress_value: FloatProperty(options={"HIDDEN", "SKIP_SAVE"}, min=0.0, max=1.0)
    progress_window_open: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    directory: StringProperty(
        name="Event Folder",
        subtype="DIR_PATH",
        options={"SKIP_SAVE"},
    )
    import_camera: BoolProperty(
        name="Import Camera",
        default=True,
        description="Import the event G4CM and assemble all camera cuts",
    )
    import_effects: BoolProperty(
        name="Import Effects",
        default=True,
        description="Import and animate effects across the complete event",
    )
    import_character_parts: BoolProperty(
        name="Import Body and Shoes",
        default=False,
        description="Body and shoes are selected manually after the bulk event import",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    auto_character_parts: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    prompt_character_parts: BoolProperty(
        name="Configure Body and Shoes",
        default=True,
        description="Open one persistent assignment list for all character actors before import",
    )
    character_parts_json: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        self.auto_character_parts = False
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        if self.progress_window_open:
            self.layout.label(text=self.progress_text, icon="FILE_REFRESH")
            self.layout.progress(factor=self.progress_value, type="BAR", text="Event import")
            return
        layout = self.layout
        layout.prop(self, "import_camera")
        layout.prop(self, "import_effects")
        layout.prop(self, "prompt_character_parts")

    def execute(self, context):
        directory = Path(bpy.path.abspath(self.directory or ""))
        if not directory.is_dir():
            self.report({"ERROR"}, f"Event folder not found: {directory}")
            return {"CANCELLED"}
        packages = collect_event_packages(directory)
        if not packages:
            self.report({"ERROR"}, f"No character G4PK cuts found in {directory}")
            return {"CANCELLED"}
        character_actors = sorted(actor for actor in packages if actor.startswith("c"))
        if (
            self.prompt_character_parts
            and character_actors
            and not self.character_parts_json
            and event_uses_modular_characters(directory, addon_preferences())
        ):
            invoke_event_parts_dialog(
                directory,
                self.import_camera,
                self.import_effects,
                character_actors,
            )
            return {"FINISHED"}

        addon = sys.modules.get(__package__.split(".")[0])
        start_progress = getattr(addon, "start_import_progress", None) if addon else None
        if start_progress is None:
            self.report({"ERROR"}, "The G4 model importer progress handler is unavailable")
            return {"CANCELLED"}
        return start_progress(
            self,
            context,
            1,
            "Preparing event import…",
            self._import_event_task(context),
        )

    def modal(self, context, event):
        addon = sys.modules.get(__package__.split(".")[0])
        return addon.modal_import_progress(self, context, event)

    def _import_event_task(self, context):
        yield 0, 1, "Preparing event import…"
        directory = Path(bpy.path.abspath(self.directory or ""))
        log_path = event_import_log_path()
        try:
            context.scene["g4_event_import_log"] = str(log_path)
        except (AttributeError, TypeError):
            pass
        append_event_file_log(f"--- event import started; log={log_path} ---")
        append_event_file_log(f"event-folder execute: directory={directory}")
        append_event_file_log(
            f"options: import_camera={self.import_camera}; import_effects={self.import_effects}; "
            f"prompt_character_parts={self.prompt_character_parts}"
        )
        if not directory.is_dir():
            append_event_file_log("cancelled: selected directory does not exist")
            self.report({"ERROR"}, f"Event folder not found: {directory}")
            return {"CANCELLED"}
        packages = collect_event_packages(directory)
        append_event_file_log(f"character package groups={len(packages)}")
        if not packages:
            append_event_file_log("cancelled: no character G4PK cuts")
            self.report({"ERROR"}, f"No character G4PK cuts found in {directory}")
            return {"CANCELLED"}
        write_event_import_log([
            f"event={directory}",
            "stage=event-folder-selected",
            f"import_camera={self.import_camera}",
            f"import_effects={self.import_effects}",
        ])
        character_actors = sorted(actor for actor in packages if actor.startswith("c"))
        modular_characters = event_uses_modular_characters(directory, addon_preferences())
        append_event_file_log(
            f"character actors={character_actors}; modular_characters={modular_characters}; "
            f"has_saved_parts={bool(self.character_parts_json)}"
        )
        if (
            self.prompt_character_parts
            and character_actors
            and not self.character_parts_json
            and modular_characters
        ):
            invoke_event_parts_dialog(
                directory,
                self.import_camera,
                self.import_effects,
                character_actors,
            )
            append_event_file_log("opening event character slots")
            return {"FINISHED"}
        append_event_file_log("continuing without event character setup")
        character_parts = json.loads(self.character_parts_json or "{}")
        generic_by_slot = generic_actor_groups(character_actors)
        for slot_actors in generic_by_slot.values():
            if any(actor in character_parts for actor in slot_actors):
                continue
            chosen = compatible_generic_actor(slot_actors, "")
            for actor in slot_actors:
                character_parts[actor] = {} if actor == chosen else {"skip": True}

        camera_paths = sorted(directory.glob("*camera*.g4cm")) if self.import_camera else []
        camera_clip_count = len(parse_g4cm(camera_paths[0])["clips"]) if camera_paths else 0
        total_steps = sum(len(paths) for paths in packages.values()) + camera_clip_count
        completed_steps = 0
        window_manager = context.window_manager
        window_manager.progress_end()
        window_manager.progress_begin(0, max(1, total_steps))
        self._import_progress_total = max(1, total_steps)
        last_progress_refresh = 0.0

        def refresh_batch_progress(status: str):
            nonlocal last_progress_refresh
            now = time.monotonic()
            if now - last_progress_refresh < 0.12 and completed_steps < total_steps:
                return
            last_progress_refresh = now
            self.progress_text = status
            self.progress_value = min(1.0, max(0.0, completed_steps / max(1, total_steps)))
            context.workspace.status_text_set(status)

        def progress(status: str = ""):
            nonlocal completed_steps
            completed_steps += 1
            window_manager.progress_update(completed_steps)
            refresh_batch_progress(
                status or f"Importing event: {completed_steps}/{total_steps} cuts"
            )
            return completed_steps, total_steps, self.progress_text

        refresh_batch_progress("Preparing G4 event import…")

        prefs = addon_preferences()
        actors = []
        skipped_actors = {}
        has_event_point_packages = any(directory.glob(f"{directory.name}_point_s*.g4pk"))
        point_motions = {}
        event_log = [f"event={directory}", f"actors={','.join(character_actors)}"]
        write_event_import_log(event_log)
        yield completed_steps, total_steps, "Resolving event characters and effects…"
        try:
            actor_models = resolve_event_actor_models(directory, prefs)
            point_assignments = resolve_event_actor_point_assignments(directory, prefs)
            effect_candidates = discover_event_effects(directory, prefs) if self.import_effects else []
        except Exception as exc:
            event_log.append(f"FATAL before timeline setup: {traceback.format_exc()}")
            write_event_import_log(event_log)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        event_log.append(f"effects_enabled={self.import_effects}")
        event_log.append(f"effect_candidates={len(effect_candidates)}")
        event_log.append(f"point_packages={int(has_event_point_packages)}")
        event_log.append(f"placement_assignments={len(point_assignments)}")
        write_event_import_log(event_log)
        cut_frames = {}
        camera_object = None
        effect_roots = []
        p3lip_controllers = []
        shared_particle_groups = 0
        try:
            with tempfile.TemporaryDirectory(prefix="level5_g4_event_") as temporary:
                temporary_directory = Path(temporary)
                yield completed_steps, total_steps, "Reading event timeline…"
                frame_origin, frame_end, cut_starts = event_timeline_layout(
                    packages,
                    temporary_directory,
                    camera_paths[0] if camera_paths else None,
                )
                total_steps += sum(
                    1 for candidate in effect_candidates
                    if candidate.get("cut") in cut_starts
                )
                window_manager.progress_end()
                window_manager.progress_begin(0, max(1, total_steps))
                self._import_progress_total = max(1, total_steps)
                yield completed_steps, total_steps, "Decoding character placement…"
                point_motions = (
                    decode_event_point_motions(directory, prefs, temporary_directory) if point_assignments else {}
                )
                event_log.append(f"placement_cuts={len(point_assignments)}; point_motions={len(point_motions)}")
                yield completed_steps, total_steps, "Decoding event effects…"
                effect_motions = (
                    decode_event_effect_motions(directory, prefs, temporary_directory)
                    if self.import_effects else {}
                )
                event_log.append(f"effect_motion_cuts={len(effect_motions)}")
                write_event_import_log(event_log)
                for actor, actor_packages in sorted(packages.items()):
                    actor_parts = dict(character_parts.get(actor) or {})
                    if actor_parts.get("skip"):
                        event_log.append(f"{actor}: skipped by selected generic profile")
                        for _ in actor_packages:
                            progress()
                        continue
                    defaults = event_part_defaults(directory, actor, prefs)
                    overrides = EVENT_PART_OVERRIDES.get(directory.name, {}).get(actor, {})
                    for key, value in defaults.items():
                        current = actor_parts.get(key, "")
                        current_path = Path(bpy.path.abspath(current)) if current else None
                        if (
                            event_part_needs_default(current, key, overrides)
                            or (current_path is not None and not current_path.is_file())
                        ):
                            actor_parts[key] = value
                    event_log.append(
                        f"{actor}: parts=" + json.dumps(
                            {key: actor_parts.get(key, "") for key in ("head", "body", "shoes", "accessory")},
                            sort_keys=True,
                        )
                    )
                    actor_base = event_actor_base_id(actor)
                    try:
                        yield completed_steps, total_steps, f"Importing character: {actor}"
                        armature, _, actor_cut_frames = yield from import_event_actor(
                            actor=actor,
                            packages=actor_packages,
                            prefs=prefs,
                            temporary_directory=temporary_directory,
                            frame_origin=frame_origin,
                            cut_starts=cut_starts,
                            progress=progress,
                            import_character_parts=False,
                            auto_character_parts=False,
                            character_part_stem="",
                            point_assignments={
                                cut: assignment for cut in cut_starts
                                if (assignment := point_assignment_for_actor(point_assignments, cut, actor)) is not None
                            },
                            point_motions=point_motions,
                            head_model=actor_parts.get("head", ""),
                            body_model=actor_parts.get("body", ""),
                            shoes_model=actor_parts.get("shoes", ""),
                            accessory_model=actor_parts.get("accessory", ""),
                            skin_color=event_skin_color_value(
                                actor_parts.get("skin_color"),
                                event_skin_color_from_head(actor_parts.get("head", ""), prefs)
                                or (1.0, 1.0, 1.0, 1.0),
                            ),
                            gloves_model=actor_parts.get("gloves", ""),
                            armband_model=actor_parts.get("armband", ""),
                            nameplate_model=actor_parts.get("nameplate", ""),
                            attach_ball=bool(actor_parts.get("attach_ball", False)),
                            ball_model=actor_parts.get("ball", ""),
                            manifest_model=actor_models.get(actor, actor_models.get(actor_base, "")),
                            debug_log=event_log,
                        )
                    except Exception as exc:
                        skipped_actors[actor] = str(exc)
                        error_trace = traceback.format_exc()
                        event_log.append(f"{actor}: ERROR {exc}\n{error_trace}")
                        append_event_file_log(f"{actor}: ERROR {exc}\n{error_trace}")
                        write_event_import_log(event_log)
                        for _ in actor_packages:
                            progress()
                        continue
                    actors.append(armature)
                    if event_log is not None:
                        attached_parts = sorted({
                            obj.get("g4_character_part_source", "")
                            for obj in event_actor_objects(armature)
                            if obj.get("g4_character_part_source")
                        })
                        event_log.append(f"{actor}: attached_parts={json.dumps(attached_parts)}")
                    cut_frames.update(actor_cut_frames)
                import_event_character_lighting(directory, cut_starts)
                event_log.append("stage=importing-effects")
                write_event_import_log(event_log)
                effect_roots = yield from import_event_effect_models(
                    effect_candidates,
                    effect_motions,
                    cut_starts,
                    frame_end-frame_origin+1,
                    progress,
                )
                joined_effect_meshes = join_event_effect_meshes(context.scene)
                shared_particle_groups = deduplicate_particle_geometry_groups(context.scene)
                event_log.append(f"effect_roots={len(effect_roots)}")
                event_log.append(f"joined_effect_meshes={joined_effect_meshes}")
                event_log.append(f"shared_particle_groups={shared_particle_groups}")
                write_event_import_log(event_log)
                if camera_paths:
                    camera_object, _ = yield from import_event_camera(
                        camera_paths[0],
                        context.collection,
                        frame_origin,
                        cut_starts,
                        progress,
                    )
                folded_material_constants = fold_static_material_constants(context.scene)
                folded_static_math = fold_static_math_constants(context.scene)
                event_log.append(
                    f"folded_static_material_constants={folded_material_constants + folded_static_math}"
                )
                shared_material_slots = deduplicate_static_materials(context.scene)
                event_log.append(f"shared_static_material_slots={shared_material_slots}")

            scene = context.scene
            scene.frame_start = 1
            scene.frame_end = frame_end - frame_origin + 1
            scene.render.fps = 60
            scene.render.fps_base = 1.0
            if camera_object is not None:
                scene.camera = camera_object
            for cut, frame in sorted(cut_frames.items(), key=lambda item: item[1]):
                scene.timeline_markers.new(cut, frame=frame)
            scene.frame_set(1)
            scene["g4_event_skipped_actors"] = json.dumps(skipped_actors, sort_keys=True)
            effect_candidate_metadata = []
            for candidate in effect_candidates:
                metadata = dict(candidate)
                particle_library = metadata.get("particle_library")
                if particle_library is not None:
                    metadata["particle_library"] = particle_library_metadata(particle_library)
                effect_candidate_metadata.append(metadata)
            scene["g4_event_effect_candidates"] = json.dumps(effect_candidate_metadata, sort_keys=True)
            scene["g4_event_effect_count"] = len(effect_roots)
            scene["g4_event_effect_shared_particle_groups"] = shared_particle_groups
            scene["g4_event_p3lip_count"] = len(p3lip_controllers)
            scene["g4_event_point_packages"] = has_event_point_packages
            scene["g4_event_point_assignment_count"] = len(point_assignments)
            scene["g4_event_point_motion_count"] = len(point_motions)
            scene["g4_event_point_placement_enabled"] = bool(point_assignments and point_motions)
            scene["g4_event_import_log"] = "G4 Event Import Log"
            write_event_import_log(event_log)
        except Exception as exc:
            event_log.append(f"FATAL: {exc}")
            write_event_import_log(event_log)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            context.workspace.status_text_set(None)

        message = (
            f"Imported event {directory.name}: {len(actors)} actors, "
            f"{sum(len(paths) for paths in packages.values())} animation cuts, "
            f"{camera_clip_count} camera cuts"
        )
        if effect_roots:
            message += f", {len(effect_roots)} effect meshes"
        if p3lip_controllers:
            message += f", {len(p3lip_controllers)} P3LIP tracks"
        if skipped_actors:
            message += f"; skipped {len(skipped_actors)} actors; details are in G4 Event Import Log"
        placement_warning = has_event_point_packages and (not point_assignments or not point_motions)
        if placement_warning:
            message += "; event point placement data was not fully resolved"
        self.report({"WARNING" if skipped_actors or placement_warning else "INFO"}, message)
        return {"FINISHED"}


class IMPORT_OT_level5_g4cm(Operator, ImportHelper):
    bl_idname = "import_scene.level5_g4cm"
    bl_label = "Import Level-5 G4 Camera"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".g4cm"
    filter_glob: StringProperty(default="*.g4cm", options={"HIDDEN"})
    clip: StringProperty(
        name="Clip",
        default="0",
        description="Clip index or exact clip name inside the G4CM bank",
    )
    set_scene_fps: BoolProperty(
        name="Set Scene FPS",
        default=True,
        description="Set the scene frame rate to the FPS stored in the G4CM clip",
    )
    set_active_camera: BoolProperty(
        name="Set Active Camera",
        default=True,
        description="Use the imported camera as the active scene camera",
    )

    def execute(self, context):
        path = Path(self.filepath)
        if path.suffix.lower() != ".g4cm":
            self.report({"ERROR"}, "Select a G4CM camera animation file")
            return {"CANCELLED"}
        prefs = addon_preferences()
        decoded_path = None
        try:
            motion, decoded_path = decode_g4cm(path, self.clip, prefs)
            camera_object, frame_count = create_camera_animation(path, motion, context.collection)
            context.scene.frame_start = 1
            context.scene.frame_end = max(1, frame_count)
            if self.set_scene_fps and motion["clip"].get("fps"):
                context.scene.render.fps = motion["clip"]["fps"]
                context.scene.render.fps_base = 1.0
            if self.set_active_camera:
                context.scene.camera = camera_object
            for obj in context.view_layer.objects:
                if obj.select_get():
                    obj.select_set(False)
            camera_object.select_set(True)
            context.view_layer.objects.active = camera_object
            context.scene.frame_set(1)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            if decoded_path and not getattr(prefs, "keep_decode_json", False):
                try:
                    decoded_path.unlink()
                    if not any(decoded_path.parent.iterdir()):
                        shutil.rmtree(decoded_path.parent, ignore_errors=True)
                except OSError:
                    pass

        self.report(
            {"INFO"},
            f"Imported camera {motion['clip']['name']}: {frame_count} frames on {camera_object.name}",
        )
        return {"FINISHED"}








def sample_action_for_g4mt(armature, action, scene, view_layer):
    animation_data = armature.animation_data
    if animation_data is None:
        raise ValueError("The armature has no animation data")
    if armature.data.bones.get("c_global_0_0") is None:
        ensure_native_motion_helpers(armature)
    if action.get("g4_event_root_motion"):
        raise ValueError("Event root motion is stored on the armature object and cannot be exported as bone tracks")

    bones = list(armature.data.bones)

    def depth(bone):
        value = 0
        parent = bone.parent
        while parent is not None:
            value += 1
            parent = parent.parent
        return value

    bones.sort(key=depth)
    parent_indices = {bone.name: index for index, bone in enumerate(bones)}
    samples = {bone.name: [] for bone in bones}
    uses_game_axes = bool(
        armature.get("g4_character_model_source")
        or armature.get("g4_character_skeleton_source")
        or armature.get("g4_cumulative_scale_conversion")
    )
    blender_to_g4 = g4_from_blender_matrix()
    g4_to_blender = blender_to_g4.inverted()
    first_frame = round(action.frame_start)
    last_frame = round(action.frame_end)
    if last_frame < first_frame:
        raise ValueError("The active Action has an empty frame range")

    previous_frame = scene.frame_current
    previous_action = animation_data.action
    previous_slot = getattr(animation_data, "action_slot", None)
    try:
        animation_data.action = action
        slots = getattr(action, "slots", None)
        if slots is not None and len(slots):
            animation_data.action_slot = slots[0]
        for frame in range(first_frame, last_frame + 1):
            scene.frame_set(frame)
            view_layer.update()
            for bone in bones:
                pose_bone = armature.pose.bones[bone.name]
                local = (
                    pose_bone.parent.matrix.inverted_safe() @ pose_bone.matrix
                    if pose_bone.parent else pose_bone.matrix.copy()
                )
                if not uses_game_axes:
                    object_scale = armature.matrix_world.to_scale()
                    local.translation = Vector(tuple(
                        local.translation[index] * object_scale[index]
                        for index in range(3)
                    ))
                    local = blender_to_g4 @ local @ g4_to_blender
                location, rotation, scale = local.decompose()
                samples[bone.name].append(G4MTLocalTransform(
                    translation=tuple(location),
                    rotation_xyzw=(rotation.x, rotation.y, rotation.z, rotation.w),
                    scale=tuple(scale),
                ))
    finally:
        animation_data.action = previous_action
        if previous_slot is not None:
            animation_data.action_slot = previous_slot
        scene.frame_set(previous_frame)
        view_layer.update()

    return tuple(
        G4MTTargetSamples(
            bone.name,
            parent_indices[bone.parent.name] if bone.parent else None,
            tuple(samples[bone.name]),
        )
        for bone in bones
    )


def primary_avatar_target_mapping(target_joint_mapping, target_parents, source_joint_names):
    source_names = set(source_joint_names)
    by_source = defaultdict(list)
    for target_name, source_name in target_joint_mapping.items():
        if source_name not in source_names:
            continue
        if target_name not in target_parents:
            raise ValueError(f"Avatar mapping refers to missing target bone {target_name!r}")
        by_source[source_name].append(target_name)

    if "c_global_0_0" in source_names:
        if target_joint_mapping.get("c_global_0_0") != "c_global_0_0":
            raise ValueError("Avatar root motion must map to target bone c_global_0_0")
        by_source["c_global_0_0"] = ["c_global_0_0"]

    def depth(name):
        result = 0
        seen = set()
        while target_parents[name] is not None:
            if name in seen:
                raise ValueError("Target armature hierarchy contains a cycle")
            seen.add(name)
            name = target_parents[name]
            if name not in target_parents:
                raise ValueError(f"Target armature is missing parent bone {name!r}")
            result += 1
        return result

    result = {}
    for source_name, target_names in by_source.items():
        shallowest = min(depth(name) for name in target_names)
        primary = sorted(name for name in target_names if depth(name) == shallowest)
        if len(primary) != 1:
            raise ValueError(
                f"Avatar source joint {source_name!r} has multiple primary target bones: {', '.join(primary)}"
            )
        result[source_name] = primary[0]
    return result


def avatar_retargeted_g4mt_samples(
    context, source, target, source_action, source_mapping, target_joint_mapping
):
    from .humanoid_avatar_pose import bake_avatar_action

    scene = context.scene
    source_animation = source.animation_data_create()
    target_animation = target.animation_data_create()
    previous_frame = scene.frame_current
    previous_source_action = source_animation.action
    previous_source_slot = getattr(source_animation, "action_slot", None)
    previous_target_action = target_animation.action
    previous_target_slot = getattr(target_animation, "action_slot", None)
    avatar_action = None
    try:
        source_animation.action = source_action
        source_slots = getattr(source_action, "slots", ())
        if source_slots:
            source_animation.action_slot = source_slots[0]
        target_parents = {
            bone.name: bone.parent.name if bone.parent else None
            for bone in target.data.bones
        }
        target_mapping = primary_avatar_target_mapping(
            target_joint_mapping,
            target_parents,
            set(source_mapping),
        )
        frames = tuple(range(round(source_action.frame_start), round(source_action.frame_end) + 1))
        baker = bake_avatar_action(
            context,
            source,
            target,
            source_mapping,
            target_mapping,
            frames,
            f"{target.name} · {source_action.name}",
        )
        while True:
            try:
                completed, total, message = next(baker)
            except StopIteration as result:
                avatar_action = result.value
                break
            yield completed / max(total, 1), message
        target_animation.action = avatar_action
        target_slots = getattr(avatar_action, "slots", ())
        if target_slots:
            target_animation.action_slot = target_slots[0]
        targets = sample_action_for_g4mt(target, avatar_action, scene, context.view_layer)
        ball = source.pose.bones.get("c_ball_1_0")
        if ball is not None and "c_ball_1_0" in target.data.bones:
            ball_samples = []
            for frame in frames:
                scene.frame_set(frame)
                context.view_layer.update()
                local = ball.parent.matrix.inverted_safe() @ ball.matrix if ball.parent else ball.matrix.copy()
                location, rotation, scale = local.decompose()
                ball_samples.append(G4MTLocalTransform(
                    translation=tuple(location),
                    rotation_xyzw=(rotation.x, rotation.y, rotation.z, rotation.w),
                    scale=tuple(scale),
                ))
            targets = tuple(
                G4MTTargetSamples(item.name, item.parent_index, tuple(ball_samples))
                if item.name == "c_ball_1_0" else item
                for item in targets
            )
        return targets
    finally:
        target_animation.action = previous_target_action
        if previous_target_slot is not None:
            target_animation.action_slot = previous_target_slot
        source_animation.action = previous_source_action
        if previous_source_slot is not None:
            source_animation.action_slot = previous_source_slot
        scene.frame_set(previous_frame)
        context.view_layer.update()
        if avatar_action is not None and avatar_action.name in bpy.data.actions:
            bpy.data.actions.remove(avatar_action, do_unlink=True)


def character_bank_id_from_path(path: Path) -> str:
    bank = re.fullmatch(r"(c\d{4,10})_p\d{3}", path.stem, re.IGNORECASE)
    if bank is not None:
        return bank.group(1).lower()
    parent = path.parent.name.lower()
    if re.fullmatch(r"c\d{4,10}", parent):
        return parent
    candidates = [
        identifier
        for part in reversed(path.parts)
        for identifier in model_identifiers(Path(part))
        if identifier.startswith("c")
    ]
    return candidates[0] if candidates else ""


class EXPORT_OT_level5_g4mt(Operator, ExportHelper):
    bl_idname = "export_scene.level5_g4mt"
    bl_label = "Export G4MT Animation Candidate"
    bl_options = {"PRESET"}

    filename_ext = ".g4mt"
    filter_glob: StringProperty(default="*.g4mt", options={"HIDDEN"})
    template_path: StringProperty(name="G4MT Template", subtype="FILE_PATH")
    template_clip_index: IntProperty(name="Template Clip Index", default=0, min=0)
    clip_name: StringProperty(name="Clip Name")
    related_bank_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    related_bank_summary: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    export_all_clips: BoolProperty(
        name="Export All Clips from Imported Bank",
        default=False,
        description="Write every imported clip from this bank family; turn off to write only the active Action",
    )

    def invoke(self, context, event):
        armature = context.active_object
        action = (
            armature.animation_data.action
            if armature and armature.type == "ARMATURE" and armature.animation_data
            else None
        )
        if action is not None:
            self.clip_name = action.get("g4mt_clip_name", action.name)
            clip_index = action.get("g4mt_clip_index")
            if isinstance(clip_index, int) and clip_index >= 0:
                self.template_clip_index = clip_index
            source = action.get("g4mt_source")
            if source and Path(source).suffix.lower() in {".g4mt", ".g4pk"}:
                self.template_path = source
                self.filepath = str(generated_g4mt_output_path(armature, Path(source)) or
                                    Path(source).with_name(f"{Path(source).stem}_candidate.g4mt"))
                bank_id = character_bank_id_from_path(Path(source))
                if bank_id:
                    self.related_bank_id = bank_id
                    character_count, event_count = find_animation_bank_resources(
                        self.related_bank_id,
                        candidate_data_roots(Path(source), addon_preferences().raw_data_root),
                    )
                    self.related_bank_summary = (
                        f"Bank ID {self.related_bank_id}: {character_count} character and "
                        f"{event_count} event resources found"
                    )
        return ExportHelper.invoke(self, context, event)

    def draw(self, context):
        self.layout.prop(self, "template_path")
        self.layout.prop(self, "template_clip_index")
        self.layout.prop(self, "clip_name")
        if self.related_bank_summary:
            self.layout.label(text=self.related_bank_summary, icon="FILE_FOLDER")
            self.layout.label(text="The output template below is still exported on its own.", icon="INFO")
        active_object = context.active_object
        action = active_object.animation_data.action if active_object and active_object.animation_data else None
        if imported_g4mt_family_actions(active_object):
            self.layout.prop(self, "export_all_clips")
            if self.export_all_clips:
                self.layout.label(text="All clips in this imported rig family will be written.", icon="ACTION")
            else:
                self.layout.label(text="Only the active Action will be written.", icon="ACTION")

    def execute(self, context):
        armature = context.active_object
        if armature is None or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select the target armature with its Action active")
            return {"CANCELLED"}
        animation_data = armature.animation_data
        action = animation_data.action if animation_data else None
        if action is None:
            self.report({"ERROR"}, "The selected armature has no active Action")
            return {"CANCELLED"}

        template_path = Path(bpy.path.abspath(self.template_path))
        output_path = Path(bpy.path.abspath(self.filepath))
        if not template_path.is_file():
            self.report({"ERROR"}, "Select an existing G4MT template file")
            return {"CANCELLED"}
        if template_path.resolve() == output_path.resolve():
            self.report({"ERROR"}, "Output path must not overwrite the source G4MT template")
            return {"CANCELLED"}
        if not output_path.parent.is_dir():
            self.report({"ERROR"}, "Output directory does not exist")
            return {"CANCELLED"}

        clip_name = self.clip_name.strip() or action.get("g4mt_clip_name", action.name)
        try:
            template_source = template_path.read_bytes()
            if template_path.suffix.lower() == ".g4pk":
                entry_name = action.get("g4pk_entry")
                if not entry_name:
                    raise ValueError("The imported Action does not identify its G4MT package entry")
                selected_entry, template = select_g4mt_entry(template_source, entry_name)
                if selected_entry != entry_name:
                    raise ValueError("The selected G4PK entry does not match the imported Action family")
            else:
                template = template_source
            bank = parse_g4mt_bank(template, str(template_path))
            rig_id = action.get("g4mt_rig_id")
            family_id = action.get("g4mt_family_import_id")
            source = action.get("g4mt_source")
            family = []
            if rig_id and family_id and source:
                if Path(source).resolve() != template_path.resolve():
                    raise ValueError("Select the original G4MT bank used to import this Action family")
                family = [candidate for candidate in bpy.data.actions
                          if candidate.get("g4mt_rig_id") == rig_id
                          and candidate.get("g4mt_family_import_id") == family_id
                          and candidate.get("g4mt_source") == source
                          and candidate.get("g4pk_entry") == action.get("g4pk_entry")]
            if family and self.export_all_clips:
                indices = [candidate.get("g4mt_clip_index") for candidate in family]
                if any(not isinstance(index, int) or not 0 <= index < len(bank.clips) for index in indices):
                    raise ValueError("Imported family contains a clip index outside the selected G4MT template")
                if len(set(indices)) != len(indices):
                    raise ValueError("Imported family contains duplicate G4MT clip indices")
                family.sort(key=lambda candidate: candidate["g4mt_clip_index"])
                clips = []
                for candidate in family:
                    targets = sample_action_for_g4mt(
                        armature, candidate, context.scene, context.view_layer
                    )
                    clips.append(G4MTClipSamples(
                        candidate["g4mt_clip_index"],
                        candidate.get("g4mt_clip_name", candidate.name),
                        targets,
                    ))
                result = write_g4mt_family_preserving_additive(template, clips)
            else:
                if self.template_clip_index >= len(bank.clips):
                    raise ValueError("Template clip index is outside the G4MT bank")
                targets = sample_action_for_g4mt(
                    armature, action, context.scene, context.view_layer
                )
                result = write_g4mt_clip(template, self.template_clip_index, clip_name, targets)
            handle, temporary_path = tempfile.mkstemp(
                prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
            )
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(result)
                os.replace(temporary_path, output_path)
            finally:
                if os.path.exists(temporary_path):
                    os.unlink(temporary_path)
        except (OSError, RuntimeError, ValueError, struct.error) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        self.report({"INFO"}, "Wrote G4MT candidate")
        return {"FINISHED"}


def ensure_native_motion_helpers(armature) -> None:
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Select the target armature")

    view_layer = bpy.context.view_layer
    previous_active = view_layer.objects.active
    previous_mode = previous_active.mode if previous_active is not None else "OBJECT"
    selection = {obj.name: obj.select_get() for obj in view_layer.objects}
    try:
        if previous_active is not None and previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for obj in view_layer.objects:
            obj.select_set(False)
        armature.select_set(True)
        view_layer.objects.active = armature
        bpy.ops.object.mode_set(mode="EDIT")
        bones = armature.data.edit_bones

        def get_or_create(name: str):
            bone = bones.get(name)
            if bone is None:
                bone = bones.new(name)
                bone.head = (0.0, 0.0, 0.0)
                bone.tail = (0.0, 0.1, 0.0)
                bone.use_deform = False
            return bone

        output = get_or_create("output")
        bounding_box = get_or_create("boundingBox")
        global_root = get_or_create("c_global_0_0")
        ball = get_or_create("c_ball_1_0")
        output.parent = None
        bounding_box.parent = output
        global_root.parent = output
        ball.parent = global_root
        for bone in bones:
            if bone.name not in {"output", "boundingBox", "c_global_0_0", "c_ball_1_0"} and bone.parent is None:
                bone.parent = global_root
                bone.use_connect = False
    finally:
        if armature.mode == "EDIT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for obj in view_layer.objects:
            obj.select_set(selection.get(obj.name, False))
        view_layer.objects.active = previous_active
        if previous_active is not None and previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=previous_mode)


def source_skeleton_for_bank(resource: Path, bank_id: str, data_root: Path, override: Path | None) -> Path | None:
    if override is not None:
        return valid_skeleton_path(override)
    model_root = data_root / "common" / "chr" / bank_id
    for suffix in (".g4sk", ".g4pkm", ".g4md"):
        model = model_root / f"{bank_id}{suffix}"
        if model.is_file():
            skeleton = valid_skeleton_path(model) if suffix == ".g4sk" else resolve_skeleton_path(model)
            if skeleton is not None:
                return skeleton
    model_path = resolve_model_path(resource, str(data_root))
    skeleton = resolve_skeleton_path(model_path)
    if skeleton is not None:
        return skeleton
    skeleton = valid_skeleton_path(resource)
    if skeleton is not None:
        return skeleton
    for index, (skeleton_data, _source) in enumerate(g4sk_entries_from_candidate(resource)):
        cache_root = Path(tempfile.gettempdir()) / "level5_g4sk_blender"
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_key = zlib.crc32(f"{data_root}:{bank_id}:{resource}:{index}".encode("utf-8")) & 0xFFFFFFFF
        destination = cache_root / f"{cache_key:08x}.g4sk"
        if not destination.is_file() or destination.read_bytes() != skeleton_data:
            handle, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=cache_root)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(skeleton_data)
                os.replace(temporary_name, destination)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return destination
    return None


def retarget_g4mt_clip_samples(
    template: bytes, resource: Path, entry_name: str, skeleton_path: Path,
    armature, scene, view_layer, clip_indices: tuple[int, ...],
):
    bank = parse_g4mt_bank(template, f"{resource}::{entry_name}")
    playable_by_index = {clip.index: clip for clip in bank.clips if not clip.flags & 1}
    playable = [playable_by_index[index] for index in clip_indices if index in playable_by_index]
    if len(playable) != len(clip_indices):
        raise ValueError(f"Clip group for {resource.name} contains missing or additive clips")
    if not playable:
        return ()

    animation_data = armature.animation_data_create()
    original_action = animation_data.action
    original_slot = getattr(animation_data, "action_slot", None)
    original_frame = scene.frame_current
    original_pose = {
        bone.name: (
            bone.location.copy(), bone.rotation_mode, bone.rotation_euler.copy(),
            bone.rotation_quaternion.copy(), tuple(bone.rotation_axis_angle), bone.scale.copy(),
        )
        for bone in armature.pose.bones
    }
    handle, temporary_name = tempfile.mkstemp(prefix="level5_g4mt_bank_", suffix=".g4mt")
    os.close(handle)
    temporary_path = Path(temporary_name)
    clips = []
    try:
        temporary_path.write_bytes(template)
        parsed_motion_bank, motion_data, motion_skeleton = prepare_motion_bank(
            temporary_path, skeleton_path
        )
        source_names = set(motion_skeleton["names"])
        target_joint_mapping = {
            bone.name: str(bone.get("g4_animation_source_joint", "")).strip()
            for bone in armature.data.bones
            if str(bone.get("g4_animation_source_joint", "")).strip() in source_names
        }
        if "c_global_0_0" in source_names:
            target_joint_mapping["c_global_0_0"] = "c_global_0_0"
        if not target_joint_mapping:
            raise RuntimeError("The target G4SK has no validated Avatar animation assignments")
        for clip_number, clip in enumerate(playable, 1):
            motion = decode_motion_from_bank(
                resource, str(clip.index), parsed_motion_bank, motion_data, motion_skeleton
            )
            source = create_motion_armature(motion)
            source_action = None
            try:
                names_by_hash = {
                    zlib.crc32(bone.name.encode("utf-8")) & 0xFFFFFFFF: bone.name
                    for bone in source.data.bones
                }
                for track in motion["tracks"]:
                    name = track.get("target_name")
                    if not name:
                        target_hash = track.get("target_hash")
                        try:
                            target_hash = int(target_hash, 16) if isinstance(target_hash, str) else int(target_hash)
                        except (TypeError, ValueError):
                            target_hash = None
                        name = names_by_hash.get(target_hash)
                        if name is not None:
                            track["target_name"] = name
                    track["armature_bone_names"] = (
                        (name,) if isinstance(name, str) and name in source.data.bones else ()
                    )
                source_action, keyed_bones = create_action(source, motion, clip.start_frame)
                if keyed_bones == 0:
                    raise RuntimeError(f"Source G4MT has no tracks for {resource.name} / {clip.name or clip.index}")
                source_mapping = {bone.name: bone.name for bone in source.data.bones}
                targets = yield from avatar_retargeted_g4mt_samples(
                    bpy.context, source, armature, source_action, source_mapping, target_joint_mapping
                )
            except (RuntimeError, ValueError) as error:
                if "no target tracks" in str(error).casefold():
                    yield f"No target tracks matched; preserving {resource.name} / {clip.name or clip.index}"
                    return None
                raise
            finally:
                if source_action is not None and source_action.name in bpy.data.actions:
                    bpy.data.actions.remove(source_action, do_unlink=True)
                source_data = source.data
                bpy.data.objects.remove(source, do_unlink=True)
                bpy.data.armatures.remove(source_data)

            clips.append(G4MTClipSamples(clip.index, clip.name, targets))
            yield f"{resource.name}: {clip_number}/{len(playable)} — {clip.name or clip.index}"

        return tuple(clips)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
        animation_data.action = original_action
        if original_slot is not None:
            animation_data.action_slot = original_slot
        if original_action is None:
            for bone in armature.pose.bones:
                state = original_pose.get(bone.name)
                if state is None:
                    continue
                location, rotation_mode, euler, quaternion, axis_angle, scale = state
                bone.rotation_mode = rotation_mode
                bone.location = location
                bone.rotation_euler = euler
                bone.rotation_quaternion = quaternion
                bone.rotation_axis_angle = axis_angle
                bone.scale = scale
        scene.frame_set(original_frame)
        view_layer.update()


def retarget_g4mt_payload(template: bytes, resource: Path, entry_name: str, skeleton_path: Path,
                          armature, scene, view_layer):
    bank = parse_g4mt_bank(template, f"{resource}::{entry_name}")
    playable_indices = tuple(clip.index for clip in bank.clips if not clip.flags & 1)
    if not playable_indices:
        return template
    clips = yield from retarget_g4mt_clip_samples(
        template, resource, entry_name, skeleton_path, armature, scene, view_layer,
        playable_indices,
    )
    if clips is None:
        return None
    return write_g4mt_family_preserving_additive(template, clips)


def retarget_g4mt_payload_with_progress(
    template: bytes, resource: Path, entry_name: str, skeleton_path: Path,
    armature, scene, view_layer, resource_index: int, resource_total: int, playable_count: int,
):
    generator = retarget_g4mt_payload(
        template, resource, entry_name, skeleton_path, armature, scene, view_layer
    )
    completed_clips = 0
    clip_progress = 0.0
    while True:
        try:
            update = next(generator)
        except StopIteration as result:
            return result.value
        if isinstance(update, tuple):
            fraction, message = update
            if playable_count:
                clip_progress = max(
                    clip_progress,
                    min(1.0, (completed_clips + fraction) / playable_count),
                )
                progress = (resource_index - 1 + clip_progress) / resource_total
                yield progress, f"{resource.name}: {message}"
            else:
                yield update
            continue
        if isinstance(update, str):
            match = re.search(r":\s*(\d+)/(\d+)\s+—", update)
            if match is not None and playable_count:
                completed_clips = int(match.group(1))
                clip_progress = max(clip_progress, completed_clips / playable_count)
                yield (resource_index - 1 + clip_progress) / resource_total, update
                continue
        yield update


def retarget_g4mt_clip_samples_with_progress(
    template: bytes, resource: Path, entry_name: str, skeleton_path: Path,
    armature, scene, view_layer, clip_indices: tuple[int, ...],
    resource_index: int, resource_total: int,
):
    generator = retarget_g4mt_clip_samples(
        template, resource, entry_name, skeleton_path, armature, scene, view_layer,
        clip_indices,
    )
    completed_clips = 0
    clip_progress = 0.0
    clip_total = len(clip_indices)
    while True:
        try:
            update = next(generator)
        except StopIteration as result:
            return result.value
        if isinstance(update, tuple):
            fraction, message = update
            clip_progress = max(
                clip_progress,
                min(1.0, (completed_clips + fraction) / clip_total),
            )
            yield (resource_index - 1 + clip_progress) / resource_total, f"{resource.name}: {message}"
            continue
        if isinstance(update, str):
            match = re.search(r":\s*(\d+)/(\d+)\s+—", update)
            if match is not None:
                completed_clips = int(match.group(1))
                clip_progress = max(clip_progress, completed_clips / clip_total)
                yield (resource_index - 1 + clip_progress) / resource_total, update
                continue
        yield update


def write_bank_resource(source: Path, relative: Path, output_root: Path, data: bytes) -> None:
    destination = output_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    sidecar = source.with_suffix(".mevbin")
    if sidecar.is_file():
        shutil.copy2(sidecar, destination.with_suffix(".mevbin"))


def publish_animation_overlay(stage: Path, output: Path) -> None:
    for path in stage.rglob("*"):
        if path.is_file() and path.name.casefold() == ".ds_store":
            path.unlink()
    staged_files = sorted(
        path for path in stage.rglob("*")
        if path.is_file() and path.name.casefold() != ".ds_store"
    )
    if not output.exists():
        os.rename(stage, output)
        return
    if not output.is_dir():
        raise RuntimeError(f"Animation output is not a directory: {output}")
    moved = []
    backups = []
    backup_root = Path(tempfile.mkdtemp(prefix=f".{output.name}.backup.", dir=output.parent))
    try:
        for source in staged_files:
            relative = source.relative_to(stage)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.is_dir():
                    raise RuntimeError(f"Animation output file conflicts with a directory: {destination}")
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup)
                backups.append((destination, backup))
            os.replace(source, destination)
            moved.append(destination)
    except Exception:
        for path in reversed(moved):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        for original, backup in reversed(backups):
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, original)
        shutil.rmtree(backup_root, ignore_errors=True)
        raise
    shutil.rmtree(backup_root, ignore_errors=True)
    shutil.rmtree(stage)


def export_animation_bank_resources(resources, character_bank_id: str, event_skeleton_id: str, output_root: Path,
                                    armature, source_skeleton_override: Path | None,
                                    target_bank_id: str):
    ensure_native_motion_helpers(armature)
    skeleton_cache = {}
    total = len(resources)
    written = 0
    skipped = 0
    warnings = []
    for resource_index, item in enumerate(resources, 1):
        if isinstance(item, dict):
            resource = Path(item["path"])
            data_root = Path(item["data_root"])
            is_event = bool(item["is_event"])
            source_bank_id = event_skeleton_id if is_event else character_bank_id
            if item["kind"] == "g4mt_clip_group":
                entry_name = item["entry_name"]
                if item["offset"] is None:
                    payload = resource.read_bytes()
                else:
                    with resource.open("rb") as stream:
                        stream.seek(item["offset"])
                        payload = stream.read(item["size"])
                if len(payload) != item["size"]:
                    raise RuntimeError(f"G4MT bank {entry_name or resource.name} was truncated while reading {resource.name}")
                skeleton_key = data_root, source_bank_id
                skeleton = skeleton_cache.get(skeleton_key)
                if skeleton is None:
                    skeleton = source_skeleton_for_bank(
                        resource, source_bank_id, data_root, source_skeleton_override
                    )
                    if skeleton is None:
                        raise RuntimeError(f"Could not resolve a source G4SK for {resource.name}; select one manually")
                    skeleton_cache[skeleton_key] = skeleton
                clip_indices = tuple(int(index) for index in item["clip_indices"])
                samples = yield from retarget_g4mt_clip_samples_with_progress(
                    payload, resource, entry_name or resource.name, skeleton, armature,
                    bpy.context.scene, bpy.context.view_layer, clip_indices,
                    resource_index, total,
                )
                status_path = Path(item["status_path"])
                status_path.parent.mkdir(parents=True, exist_ok=True)
                if samples is None:
                    status_path.write_text(json.dumps({"status": "skipped"}), encoding="utf-8")
                else:
                    part = write_g4mt_family(payload, samples)
                    part_path = Path(item["result_path"])
                    part_path.parent.mkdir(parents=True, exist_ok=True)
                    part_path.write_bytes(part)
                    hierarchy_path = Path(item["hierarchy_path"])
                    hierarchy_path.write_text(json.dumps(
                        g4mt_family_target_hierarchy(samples), separators=(",", ":")
                    ), encoding="utf-8")
                    status_path.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
                written += 1
                yield f"Prepared {resource.name}: {len(clip_indices)} clips"
                yield (resource_index / total, f"Prepared clip group {resource_index}/{total}: {resource.name}")
                continue

            entry_name = item["entry_name"]
            with resource.open("rb") as stream:
                stream.seek(item["offset"])
                payload = stream.read(item["size"])
            if len(payload) != item["size"]:
                raise RuntimeError(f"G4PK entry {entry_name} was truncated while reading {resource.name}")
            try:
                bank = parse_g4mt_bank(payload, f"{resource}::{entry_name}")
            except NativeFormatError as error:
                if not (is_event and "point_eff" in f"{resource.name} {entry_name}".casefold()):
                    raise
                skipped += 1
                written += 1
                warnings.append(str(error))
                yield f"Skipped invalid point-effect bank: {entry_name} ({error})"
                yield (resource_index / total, f"Exported {resource_index}/{total}: {resource.name} / {entry_name}")
                continue
            skeleton_key = data_root, source_bank_id
            skeleton = skeleton_cache.get(skeleton_key)
            if skeleton is None:
                skeleton = source_skeleton_for_bank(resource, source_bank_id, data_root, source_skeleton_override)
                if skeleton is None:
                    raise RuntimeError(f"Could not resolve a source G4SK for {resource.name}; select one manually")
                skeleton_cache[skeleton_key] = skeleton
            playable_count = sum(not clip.flags & 1 for clip in bank.clips)
            result = yield from retarget_g4mt_payload_with_progress(
                payload, resource, entry_name, skeleton, armature, bpy.context.scene,
                bpy.context.view_layer, resource_index, total, playable_count,
            )
            if result is None:
                skipped += 1
            elif result != payload:
                result_path = Path(item["result_path"])
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_bytes(result)
            written += 1
            yield f"{resource.name}: {entry_name}"
            yield (resource_index / total, f"Exported {resource_index}/{total}: {resource.name} / {entry_name}")
            continue

        resource, data_root, is_event = item
        source_bank_id = event_skeleton_id if is_event else character_bank_id
        source = resource.read_bytes()
        relative = Path(*(
            rewrite_bank_identifier(part, source_bank_id, target_bank_id)
            for part in resource.relative_to(data_root).parts
        ))
        if is_native_event_effect(resource, is_event):
            write_bank_resource(resource, relative, output_root, source)
            written += 1
            yield f"Copied native event effect unchanged: {resource.name}"
            yield (resource_index / total, f"Exported {resource_index}/{total}: {resource.name}")
            continue
        if resource.suffix.lower() == ".g4mt":
            try:
                bank = parse_g4mt_bank(source, f"{resource}::{resource.name}")
            except NativeFormatError as error:
                if not (is_event and "point_eff" in resource.stem.casefold()):
                    raise
                write_bank_resource(resource, relative, output_root, source)
                written += 1
                warnings.append(str(error))
                yield f"Skipped invalid point-effect bank: {resource.name} ({error})"
                yield (resource_index / total, f"Exported {resource_index}/{total}: {resource.name}")
                continue
            skeleton_key = data_root, source_bank_id
            skeleton = skeleton_cache.get(skeleton_key)
            if skeleton is None:
                skeleton = source_skeleton_for_bank(resource, source_bank_id, data_root, source_skeleton_override)
                if skeleton is None:
                    raise RuntimeError(f"Could not resolve a source G4SK for {resource.name}; select one manually")
                skeleton_cache[skeleton_key] = skeleton
            playable_count = sum(not clip.flags & 1 for clip in bank.clips)
            result = yield from retarget_g4mt_payload_with_progress(
                source, resource, resource.name, skeleton, armature, bpy.context.scene,
                bpy.context.view_layer, resource_index, total, playable_count,
            )
            if result is None:
                skipped += 1
                write_bank_resource(resource, relative, output_root, source)
                written += 1
            else:
                write_bank_resource(resource, relative, output_root, result)
                written += 1
        else:
            replacements = {}
            renamed_entries = {}
            skipped_entries = 0
            package_entries = g4pk_entries(source)
            g4mt_indices = [index for index, (_name, offset, _size) in enumerate(package_entries)
                            if source[offset:offset + 4] == b"G4MT"]
            for entry_number, entry_index in enumerate(g4mt_indices, 1):
                entry_name, offset, size = package_entries[entry_index]
                renamed_name = rewrite_bank_identifier(entry_name, source_bank_id, target_bank_id)
                if renamed_name != entry_name:
                    renamed_entries[entry_index] = renamed_name
                payload = source[offset:offset + size]
                try:
                    bank = parse_g4mt_bank(payload, f"{resource}::{entry_name}")
                except NativeFormatError as error:
                    if not (is_event and "point_eff" in f"{resource.name} {entry_name}".casefold()):
                        raise
                    skipped_entries += 1
                    warnings.append(str(error))
                    yield f"Skipped invalid point-effect bank: {entry_name} ({error})"
                    continue
                skeleton_key = data_root, source_bank_id
                skeleton = skeleton_cache.get(skeleton_key)
                if skeleton is None:
                    skeleton = source_skeleton_for_bank(resource, source_bank_id, data_root, source_skeleton_override)
                    if skeleton is None:
                        raise RuntimeError(
                            f"Could not resolve a source G4SK for {resource.name}; select one manually"
                        )
                    skeleton_cache[skeleton_key] = skeleton
                playable_count = sum(not clip.flags & 1 for clip in bank.clips)
                result = yield from retarget_g4mt_payload_with_progress(
                    payload, resource, entry_name, skeleton,
                    armature, bpy.context.scene, bpy.context.view_layer,
                    resource_index, total, playable_count,
                )
                if result is None:
                    skipped_entries += 1
                elif result != source[offset:offset + size]:
                    replacements[entry_index] = result
                yield f"{resource.name}: bank {entry_number}/{len(g4mt_indices)}"
            if g4mt_indices:
                skipped += skipped_entries
                result = replace_g4pk_entries(source, replacements, renamed_entries)
                write_bank_resource(resource, relative, output_root, result)
                written += 1
            else:
                write_bank_resource(resource, relative, output_root, source)
                written += 1
        yield (resource_index / total, f"Exported {resource_index}/{total}: {resource.name}")
    return written, skipped, warnings


def discover_victorytool_animation_sources(bank_id: str, data_roots: list[Path]):
    bank_files = {}
    for resource, data_root, is_event in discover_animation_bank_resources(bank_id, data_roots):
        if is_event or resource.parent.name.casefold() != bank_id.casefold():
            continue
        match = re.fullmatch(rf"{re.escape(bank_id)}_p(\d{{3}})", resource.stem, re.IGNORECASE)
        if match is None:
            continue
        bank_type = match.group(1)
        previous = bank_files.get(bank_type)
        if previous is None or (resource.suffix.lower() == ".g4pk" and previous[0].suffix.lower() != ".g4pk"):
            bank_files[bank_type] = (resource, data_root)
    return [(bank_type, *bank_files[bank_type]) for bank_type in sorted(bank_files)]


def publish_victorytool_candidates_from_overlay(
    bank_sources, source_bank_id: str, target_bank_id: str, model_path: Path, data_overlay: Path,
) -> int:
    if not re.fullmatch(r"c\d{4,10}", source_bank_id) or not re.fullmatch(r"c\d{4,10}", target_bank_id):
        raise ValueError("Source and target animation IDs must look like c000101")
    if not model_path.is_file() or not data_overlay.is_dir():
        raise RuntimeError("The exported model or its animation overlay is missing")
    model_stem = model_path.stem
    profile_path = model_path.with_suffix(".g4anim.json")
    if not profile_path.is_file():
        raise RuntimeError(f"VictoryTool animation profile is missing: {profile_path.name}")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if profile.get("modelStem") != model_stem:
        raise RuntimeError("VictoryTool animation profile does not match the exported model")

    stage = Path(tempfile.mkdtemp(prefix=f".{model_stem}_animation_", dir=model_path.parent))
    expected_prefix = f"{model_stem}_from_{source_bank_id}_p"
    existing = list(model_path.parent.glob(f"{model_stem}_from_*_p*.g4mt"))
    foreign = [path for path in existing if not path.name.startswith(expected_prefix)]
    invalid = [path for path in existing if not re.fullmatch(
        rf"{re.escape(model_stem)}_from_{re.escape(source_bank_id)}_p\d{{3}}\.g4mt",
        path.name, re.IGNORECASE,
    )]
    if foreign or invalid:
        shutil.rmtree(stage, ignore_errors=True)
        raise RuntimeError("Remove previous or nonstandard animation candidates beside the model before exporting another family")

    staged = []
    try:
        for bank_type, resource, data_root in bank_sources:
            relative = Path(*(
                rewrite_bank_identifier(part, source_bank_id, target_bank_id)
                for part in resource.relative_to(data_root).parts
            ))
            patched_resource = data_overlay / relative
            if not patched_resource.is_file():
                raise RuntimeError(f"Selected patched animation resource is missing: {relative.as_posix()}")
            data = patched_resource.read_bytes()
            if patched_resource.suffix.lower() == ".g4mt":
                payloads = [data]
            else:
                payloads = [
                    data[offset:offset + size]
                    for _name, offset, size in g4pk_entries(data)
                    if data[offset:offset + 4] == b"G4MT"
                ]
                if len(payloads) > 1:
                    raise RuntimeError(
                        f"{relative.name} contains multiple G4MT banks; VictoryTool expects one bank per type"
                    )
            if len(payloads) != 1:
                raise RuntimeError(f"Selected animation resource {relative.name} does not contain one G4MT bank")
            parse_g4mt_bank(payloads[0], f"{relative.as_posix()}::VictoryTool candidate")
            destination = stage / f"{model_stem}_from_{source_bank_id}_p{bank_type}.g4mt"
            destination.write_bytes(payloads[0])
            staged.append(destination)

        if not staged:
            raise RuntimeError(f"No selected CHR animation banks found for {source_bank_id}")

        overwritten = {}
        replaced = []
        stale = [path for path in existing if path.name not in {item.name for item in staged}]
        try:
            for source in staged:
                destination = model_path.parent / source.name
                overwritten[destination] = destination.read_bytes() if destination.exists() else None
                os.replace(source, destination)
                replaced.append(destination)
            for path in stale:
                overwritten[path] = path.read_bytes()
                path.unlink()
        except Exception:
            for path in reversed(replaced):
                original = overwritten[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(original)
            for path in stale:
                if path in overwritten and not path.exists():
                    path.write_bytes(overwritten[path])
            raise
        return len(staged)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def export_victorytool_animation_candidates(
    bank_sources, bank_id: str, source_skeleton_id: str, model_path: Path, staging_directory: Path,
    armature, source_skeleton_override: Path | None,
):
    model_stem = model_path.stem
    skeleton_cache = {}
    written = 0
    skipped = 0
    for resource_index, (bank_type, resource, data_root) in enumerate(bank_sources, 1):
        source = resource.read_bytes()
        if resource.suffix.lower() == ".g4mt":
            payloads = [(resource.name, source)]
        else:
            payloads = [
                (entry_name, source[offset:offset + size])
                for entry_name, offset, size in g4pk_entries(source)
                if source[offset:offset + 4] == b"G4MT"
            ]
            if len(payloads) > 1:
                raise RuntimeError(f"{resource.name} contains multiple G4MT banks; VictoryTool expects one bank per type")
        if not payloads:
            skipped += 1
            yield (resource_index / len(bank_sources), f"Skipped {resource.name}: no G4MT payload")
            continue

        skeleton_key = data_root, source_skeleton_id
        skeleton = skeleton_cache.get(skeleton_key)
        if skeleton is None:
            skeleton = source_skeleton_for_bank(
                resource, source_skeleton_id, data_root, source_skeleton_override
            )
            if skeleton is None:
                raise RuntimeError(f"Could not resolve a source G4SK for {resource.name}; select one manually")
            skeleton_cache[skeleton_key] = skeleton

        entry_name, payload = payloads[0]
        playable_count = sum(not clip.flags & 1 for clip in parse_g4mt_bank(payload).clips)
        retarget = retarget_g4mt_payload(
            payload, resource, entry_name, skeleton, armature,
            bpy.context.scene, bpy.context.view_layer,
        )
        completed_clips = 0
        while True:
            try:
                message = next(retarget)
            except StopIteration as completed:
                result = completed.value
                break
            completed_clips += 1
            clip_progress = min(1.0, completed_clips / max(1, playable_count))
            progress = (resource_index - 1 + clip_progress) / len(bank_sources)
            yield progress, message
        if result is None:
            skipped += 1
        else:
            parse_g4mt_bank(result, f"{resource.name}::{entry_name}")
            destination = staging_directory / f"{model_stem}_from_{bank_id}_p{bank_type}.g4mt"
            destination.write_bytes(result)
            written += 1
        yield (resource_index / len(bank_sources), f"Exported bank {resource_index}/{len(bank_sources)}: p{bank_type}")
    if written == 0:
        raise RuntimeError(f"No animation banks from {bank_id} matched the target rig")
    return written, skipped


def animation_worker_resource_groups(resources):
    def is_event(item):
        return item["is_event"] if isinstance(item, dict) else item[2]

    def size(item):
        if isinstance(item, dict):
            return int(item.get("work_weight", item["size"]))
        return item[0].stat().st_size

    branch_resources = {
        "chr": [item for item in resources if not is_event(item)],
        "event": [item for item in resources if is_event(item)],
    }
    active_branches = [branch for branch, items in branch_resources.items() if items]
    available_cpus = max(1, os.cpu_count() or 1)
    worker_limit = min(available_cpus, len(resources))
    worker_counts = {branch: 1 for branch in active_branches}
    while sum(worker_counts.values()) < worker_limit:
        branch = max(
            (name for name in active_branches if worker_counts[name] < len(branch_resources[name])),
            key=lambda name: len(branch_resources[name]) / worker_counts[name],
            default=None,
        )
        if branch is None:
            break
        worker_counts[branch] += 1

    groups = []
    for branch in active_branches:
        items = branch_resources[branch]
        bins = [[] for _ in range(worker_counts[branch])]
        loads = [0] * worker_counts[branch]
        weighted_items = sorted(((item, size(item)) for item in items), key=lambda pair: pair[1], reverse=True)
        for item, item_size in weighted_items:
            group_index = min(range(len(bins)), key=loads.__getitem__)
            bins[group_index].append(item)
            loads[group_index] += item_size
        groups.extend((branch, group) for group in bins)
    return groups


def is_native_event_effect(resource: Path, is_event: bool) -> bool:
    return is_event and "point_eff" in resource.stem.casefold()


def prepare_animation_work_units(
    resources, character_bank_id: str, event_skeleton_id: str, target_bank_id: str, worker_root: Path,
):
    work_units = []
    assemblies = []
    minimum_resource_size = 512 * 1024
    minimum_clip_work = 200_000
    minimum_clip_count = 4
    minimum_multi_bank_bytes = 16 * 1024 * 1024
    available_workers = max(1, os.cpu_count() or 1)

    def parallel_clip_groups(bank):
        playable = [clip for clip in bank.clips if not clip.flags & 1]
        weights = {
            clip.index: max(1, (clip.end_frame - clip.start_frame + 1) * max(1, clip.target_info_count))
            for clip in playable
        }
        if len(playable) < minimum_clip_count or sum(weights.values()) < minimum_clip_work:
            return ()
        group_count = min(available_workers, len(playable))
        groups = [[] for _ in range(group_count)]
        loads = [0] * group_count
        for clip in sorted(playable, key=lambda item: weights[item.index], reverse=True):
            group_index = min(range(group_count), key=loads.__getitem__)
            groups[group_index].append(clip.index)
            loads[group_index] += weights[clip.index]
        return tuple(
            (tuple(sorted(indices)), loads[index])
            for index, indices in enumerate(groups) if indices
        )

    def animation_relative_path(resource, data_root, is_event):
        source_id = event_skeleton_id if is_event else character_bank_id
        return Path(*(
            rewrite_bank_identifier(part, source_id, target_bank_id)
            for part in resource.relative_to(data_root).parts
        ))

    def add_clip_tasks(resource, data_root, is_event, entry_name, offset, size, groups, path_prefix):
        tasks = []
        for group_index, (clip_indices, weight) in enumerate(groups):
            result_path = worker_root / "g4mt_parts" / f"{path_prefix}_{group_index:04d}.g4mt"
            tasks.append({
                "kind": "g4mt_clip_group",
                "path": str(resource),
                "data_root": str(data_root),
                "is_event": is_event,
                "entry_name": entry_name,
                "offset": offset,
                "size": size,
                "clip_indices": clip_indices,
                "result_path": str(result_path),
                "hierarchy_path": str(result_path.with_suffix(".hierarchy.json")),
                "status_path": str(result_path.with_suffix(".status.json")),
                "work_weight": weight,
            })
        return tasks

    for resource_index, (resource, data_root, is_event) in enumerate(resources):
        suffix = resource.suffix.lower()
        if is_native_event_effect(resource, is_event) or resource.stat().st_size < minimum_resource_size:
            work_units.append((resource, data_root, is_event))
            continue

        if suffix == ".g4mt":
            payload = resource.read_bytes()
            try:
                bank = parse_g4mt_bank(payload, str(resource))
            except NativeFormatError:
                work_units.append((resource, data_root, is_event))
                continue
            groups = parallel_clip_groups(bank)
            if not groups:
                work_units.append((resource, data_root, is_event))
                continue
            tasks = add_clip_tasks(
                resource, data_root, is_event, resource.name, None, len(payload), groups,
                f"{resource_index:06d}_standalone",
            )
            work_units.extend(tasks)
            assemblies.append({
                "kind": "g4mt",
                "source_path": str(resource),
                "relative_path": animation_relative_path(resource, data_root, is_event).as_posix(),
                "clip_groups": [
                    {key: task[key] for key in ("clip_indices", "result_path", "hierarchy_path", "status_path")}
                    for task in tasks
                ],
                "worker_unit_count": len(tasks),
            })
            continue

        if suffix not in {".g4pk", ".g4pkm"}:
            work_units.append((resource, data_root, is_event))
            continue

        with resource.open("rb") as stream:
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as source:
                package_entries = g4pk_entries(source)
                g4mt_entries = [
                    (index, name, offset, size)
                    for index, (name, offset, size) in enumerate(package_entries)
                    if source[offset:offset + 4] == b"G4MT"
                ]
                bank_groups = {}
                for entry_index, entry_name, offset, size in g4mt_entries:
                    try:
                        bank = parse_g4mt_bank(
                            memoryview(source)[offset:offset + size], f"{resource}::{entry_name}"
                        )
                    except NativeFormatError:
                        continue
                    groups = parallel_clip_groups(bank)
                    if groups:
                        bank_groups[entry_index] = groups
                animation_bytes = sum(size for _index, _name, _offset, size in g4mt_entries)

        split_by_size = len(g4mt_entries) >= 2 and animation_bytes >= minimum_multi_bank_bytes
        if not bank_groups and not split_by_size:
            work_units.append((resource, data_root, is_event))
            continue

        source_bank_id = event_skeleton_id if is_event else character_bank_id
        package = {
            "kind": "g4pk",
            "source_path": str(resource),
            "relative_path": animation_relative_path(resource, data_root, is_event).as_posix(),
            "entries": [],
            "worker_unit_count": 0,
        }
        for entry_index, entry_name, offset, size in g4mt_entries:
            renamed_name = rewrite_bank_identifier(entry_name, source_bank_id, target_bank_id)
            entry = {
                "index": entry_index,
                "name": renamed_name if renamed_name != entry_name else "",
            }
            groups = bank_groups.get(entry_index)
            if groups:
                tasks = add_clip_tasks(
                    resource, data_root, is_event, entry_name, offset, size, groups,
                    f"{resource_index:06d}_{entry_index:06d}",
                )
                package_entry = {**entry, "clip_groups": []}
                for task in tasks:
                    work_units.append(task)
                    package_entry["clip_groups"].append({
                        key: task[key] for key in ("clip_indices", "result_path", "hierarchy_path", "status_path")
                    })
                package["worker_unit_count"] += len(tasks)
            else:
                result_path = worker_root / "g4pk_parts" / f"{resource_index:06d}_{entry_index:06d}.g4mt"
                work_units.append({
                    "kind": "g4pk_entry",
                    "path": str(resource),
                    "data_root": str(data_root),
                    "is_event": is_event,
                    "entry_name": entry_name,
                    "offset": offset,
                    "size": size,
                    "result_path": str(result_path),
                    "work_weight": size,
                })
                package_entry["result_path"] = str(result_path)
                package["worker_unit_count"] += 1
            package["entries"].append(package_entry)
        assemblies.append(package)
    return work_units, assemblies


def export_animation_candidates_with_progress(
    bank_sources, bank_id: str, source_skeleton_id: str, model_path: Path, staging_directory: Path,
    armature, source_skeleton_override: Path | None,
):
    return export_victorytool_animation_candidates(
        bank_sources, bank_id, source_skeleton_id, model_path, staging_directory, armature,
        source_skeleton_override,
    )


class EXPORT_OT_level5_g4mt_bank_family(Operator):
    bl_idname = "export_scene.level5_g4mt_bank_family"
    bl_label = "Retarget Original G4MT Bank Family"
    bl_options = {"REGISTER"}

    source_bank_id: StringProperty(name="Original Bank ID", default="")
    source_event_skeleton_id: StringProperty(name="Event Skeleton ID", default="")
    target_bank_id: StringProperty(name="Destination Bank ID", default="")
    source_skeleton_path: StringProperty(name="Original Source G4SK Override", subtype="FILE_PATH", default="")
    output_directory: StringProperty(name="Data Overlay Folder", subtype="DIR_PATH", default="")
    armature_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    chain_victorytool_candidates: BoolProperty(options={"HIDDEN", "SKIP_SAVE"}, default=False)
    export_chr_animations: BoolProperty(name="Character animations (chr)", default=True)
    export_event_animations: BoolProperty(name="Event animations (event)", default=True)
    source_data_root: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    bank_summary: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    progress_text: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    progress_value: FloatProperty(options={"HIDDEN", "SKIP_SAVE"}, min=0.0, max=1.0)
    progress_window_open: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        armature = context.scene.objects.get(self.armature_name) if self.armature_name else context.active_object
        action = armature.animation_data.action if armature and armature.animation_data else None
        source = Path(str(action.get("g4mt_source", ""))) if action is not None else Path()
        self.source_bank_id = character_bank_id_from_path(source) or self.source_bank_id
        self.target_bank_id = target_animation_bank_id(armature) or self.target_bank_id
        source_model = Path()
        if not self.source_bank_id or not self.source_event_skeleton_id:
            try:
                from ..porting import g4_port_addon
            except ImportError:
                from porting import g4_port_addon
            source_model = g4_port_addon.resolve_file(g4_port_addon.settings(context).original_model)
        prefs = addon_preferences()
        roots = candidate_data_roots(source_model if source_model.is_file() else source, getattr(prefs, "raw_data_root", ""))
        if self.source_data_root:
            data_root = Path(bpy.path.abspath(self.source_data_root)).resolve()
            if data_root.is_dir() and data_root not in roots:
                roots.append(data_root)
        if source_model.is_file() and roots:
            try:
                self.source_bank_id, self.source_event_skeleton_id = resolve_animation_family_ids(source_model, roots)
            except ValueError:
                pass
        if self.source_bank_id and self.source_event_skeleton_id:
            resources = discover_animation_bank_resources_by_branch(
                self.source_bank_id, self.source_event_skeleton_id, roots,
                self.export_chr_animations, self.export_event_animations,
            )
            characters = sum(not is_event for _, _, is_event in resources)
            events = len(resources) - characters
            self.bank_summary = (
                f"CHR family {self.source_bank_id}; event skeleton {self.source_event_skeleton_id} "
                f"({characters} character, {events} event resources)"
            )
        if armature is not None:
            model_path = Path(str(armature.get("g4_exported_model_path", "")))
            if model_path.suffix.lower() == ".g4md" and not self.output_directory:
                data_root = next((parent for parent in model_path.parents if parent.name.casefold() == "data"), None)
                self.output_directory = str(data_root or model_path.parent)
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        if self.progress_window_open:
            self.layout.label(text=self.progress_text, icon="FILE_REFRESH")
            self.layout.progress(factor=self.progress_value, type="BAR", text="Animation bank export")
            return
        self.layout.prop(self, "source_bank_id")
        self.layout.prop(self, "source_event_skeleton_id")
        self.layout.prop(self, "target_bank_id")
        self.layout.prop(self, "source_skeleton_path")
        self.layout.prop(self, "output_directory")
        self.layout.prop(self, "export_chr_animations")
        self.layout.prop(self, "export_event_animations")
        if self.bank_summary:
            self.layout.label(text=self.bank_summary, icon="FILE_FOLDER")
            self.layout.label(text="The target G4SK and animation profile use the exported character rig.", icon="INFO")
            self.layout.label(text="Selected animation branches are written into the data overlay.", icon="INFO")
            self.layout.label(text="Banks are written into the selected data overlay.", icon="FILE_FOLDER")

    def execute(self, context):
        if not (self.export_chr_animations or self.export_event_animations):
            self.report({"ERROR"}, "Select character or event animations to export")
            return {"CANCELLED"}
        armature = context.scene.objects.get(self.armature_name) if self.armature_name else context.active_object
        if armature is None or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select the target character armature")
            return {"CANCELLED"}
        prefs = addon_preferences()
        action = armature.animation_data.action if armature.animation_data else None
        source = Path(str(action.get("g4mt_source", ""))) if action is not None else Path()
        roots = candidate_data_roots(source, getattr(prefs, "raw_data_root", ""))
        if self.source_data_root:
            data_root = Path(bpy.path.abspath(self.source_data_root)).resolve()
            if data_root.is_dir() and data_root not in roots:
                roots.append(data_root)
        try:
            resources = discover_animation_bank_resources_by_branch(
                self.source_bank_id, self.source_event_skeleton_id, roots,
                self.export_chr_animations, self.export_event_animations,
            )
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if not resources:
            self.report({"ERROR"}, f"No original G4MT/G4PK resources found for {self.source_bank_id}")
            return {"CANCELLED"}
        if not re.fullmatch(r"c\d{4,10}", self.target_bank_id.strip().lower()):
            self.report({"ERROR"}, "Destination animation ID must be a character ID such as c000101")
            return {"CANCELLED"}

        output = Path(bpy.path.abspath(self.output_directory)).resolve()
        existing_data_overlay = output.is_dir() and output.name.casefold() == "data" and (output / "common").is_dir()
        if (output.exists() and not existing_data_overlay) or not output.parent.is_dir():
            self.report({"ERROR"}, "Choose a new output folder or an existing package data folder")
            return {"CANCELLED"}
        if any(output == root or root in output.parents for root in roots):
            self.report({"ERROR"}, "Output folder cannot be inside a source data directory")
            return {"CANCELLED"}
        override = None
        if self.source_skeleton_path:
            override = valid_skeleton_path(Path(bpy.path.abspath(self.source_skeleton_path)))
            if override is None:
                self.report({"ERROR"}, "Source G4SK override is not a readable skeleton")
                return {"CANCELLED"}

        self._output = output
        self._stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        self._worker_root = Path(tempfile.mkdtemp(prefix="level5_g4_animation_workers_"))
        self._cancel_path = self._worker_root / "cancel"
        self._workers = []
        self._assembler_process = None
        self._assembler_started = False
        target_armature = None
        try:
            self._work_units, self._assemblies = prepare_animation_work_units(
                resources,
                self.source_bank_id.strip().lower(),
                self.source_event_skeleton_id.strip().lower(),
                self.target_bank_id.strip().lower(),
                self._worker_root,
            )
            target_skeleton_value = str(armature.get("g4_exported_skeleton_path", ""))
            target_skeleton_path = Path(target_skeleton_value) if target_skeleton_value else None
            if target_skeleton_path is None or not target_skeleton_path.is_file():
                raise RuntimeError("Export the target model first so its G4SK can be used for animation retargeting")
            target_armature = create_g4sk_armature(target_skeleton_path)
            target_armature.name = f"{armature.name}__G4SKExportTarget"
            worker_armature_name = target_armature.name
            target_armature["g4_character_model_source"] = str(
                Path(str(armature.get("g4_exported_model_path", ""))).resolve()
            )
            target_armature["g4_character_skeleton_source"] = str(target_skeleton_path.resolve())
            target_armature["g4_cumulative_scale_conversion"] = True

            rig_snapshot = self._worker_root / "target_rig.blend"
            target_joint_mappings = {}
            if armature.get("g4_exported_model_path"):
                try:
                    from ..porting import g4_port_addon
                except ImportError:
                    from porting import g4_port_addon
                port_settings = g4_port_addon.settings(context)
                target_names = set(parse_g4sk(target_skeleton_path.read_bytes())["names"])
                for branch_name, is_event in (("chr", False), ("event", True)):
                    branch = [item for item in resources if item[2] == is_event]
                    if not branch:
                        continue
                    source_skeleton_id = (
                        self.source_event_skeleton_id.strip().lower()
                        if is_event else self.source_bank_id.strip().lower()
                    )
                    source_skeleton = source_skeleton_for_bank(
                        branch[0][0], source_skeleton_id, branch[0][1], override
                    )
                    if source_skeleton is None:
                        raise RuntimeError(
                            f"Could not resolve the generic source G4SK for the {branch_name.upper()} branch"
                        )
                    source_names = set(parse_g4sk(source_skeleton.read_bytes())["names"])
                    target_joint_mapping = g4_port_addon.animation_source_joint_map(
                        port_settings,
                        source_names,
                        target_names,
                    )
                    target_joint_mappings[branch_name] = route_root_motion_to_global_helper(
                        target_joint_mapping,
                        source_names,
                        target_names,
                    )
            bpy.data.libraries.write(str(rig_snapshot), {target_armature}, fake_user=True)
            armature_data = target_armature.data
            bpy.data.objects.remove(target_armature, do_unlink=True)
            target_armature = None
            if armature_data.users == 0:
                bpy.data.armatures.remove(armature_data)
            worker_script = Path(__file__).with_name("g4_animation_worker.py")
            worker_groups = animation_worker_resource_groups(self._work_units)
            threads_per_worker = max(1, (os.cpu_count() or 1) // len(worker_groups))
            for index, (branch, group) in enumerate(worker_groups):
                job_path = self._worker_root / f"job_{index}.json"
                progress_path = self._worker_root / f"progress_{index}.json"
                result_path = self._worker_root / f"result_{index}.json"
                log_path = self._worker_root / f"worker_{index}.log"
                job_path.write_text(json.dumps({
                    "armature_name": worker_armature_name,
                    "branch": branch,
                    "character_bank_id": self.source_bank_id.strip().lower(),
                    "event_skeleton_id": self.source_event_skeleton_id.strip().lower(),
                    "target_bank_id": self.target_bank_id.strip().lower(),
                    "source_skeleton_override": str(override) if override else "",
                    "target_joint_mapping": target_joint_mappings.get(branch, {}),
                    "output_root": str(self._stage),
                    "progress_path": str(progress_path),
                    "cancel_path": str(self._cancel_path),
                    "result_path": str(result_path),
                    "resources": [
                        (item if isinstance(item, dict) else {
                            "kind": "resource",
                            "path": str(item[0]),
                            "data_root": str(item[1]),
                            "is_event": item[2],
                        })
                        for item in group
                    ],
                }), encoding="utf-8")
                command = [
                    bpy.app.binary_path,
                    "--background",
                    "--factory-startup",
                    "--python-exit-code",
                    "1",
                    "--threads",
                    str(threads_per_worker),
                    str(rig_snapshot),
                    "--python",
                    str(worker_script),
                    "--",
                    str(job_path),
                ]
                with log_path.open("wb") as log_stream:
                    process = subprocess.Popen(command, stdout=log_stream, stderr=subprocess.STDOUT)
                self._workers.append({
                    "process": process,
                    "branch": branch,
                    "total": len(group),
                    "progress_path": progress_path,
                    "result_path": result_path,
                    "log_path": log_path,
                })
        except Exception as exc:
            if target_armature is not None and target_armature.name in bpy.data.objects:
                armature_data = target_armature.data
                bpy.data.objects.remove(target_armature, do_unlink=True)
                if armature_data.users == 0:
                    bpy.data.armatures.remove(armature_data)
            self._cancel_path.touch()
            for worker in self._workers:
                worker["process"].terminate()
            for worker in self._workers:
                worker["process"].wait()
            shutil.rmtree(self._stage, ignore_errors=True)
            shutil.rmtree(self._worker_root, ignore_errors=True)
            self.report({"ERROR"}, f"Could not start animation workers: {exc}")
            return {"CANCELLED"}
        self._worker_work_count = len(self._work_units)
        self._resources = self._worker_work_count + len(self._assemblies)
        self._window_manager = context.window_manager
        self._timer = self._window_manager.event_timer_add(0.05, window=context.window)
        self._window_manager.progress_begin(0, self._resources)
        self.progress_text = "Starting animation workers…"
        self.progress_value = 0.0
        self.progress_window_open = True
        context.window_manager.modal_handler_add(self)
        self._cancel_requested = False
        addon = sys.modules.get(__package__.split(".")[0])
        addon.begin_progress_dialog(self, context)
        return {"RUNNING_MODAL"}

    def _finish(self, context, succeeded=False):
        self.progress_window_open = False
        if getattr(self, "_timer", None) is not None:
            self._window_manager.event_timer_remove(self._timer)
            self._timer = None
        self._window_manager.progress_end()
        context.workspace.status_text_set(None)
        addon = sys.modules.get(__package__.split(".")[0])
        if addon is not None:
            addon.end_progress_dialog(self)
        if not succeeded and getattr(self, "_stage", None) is not None:
            shutil.rmtree(self._stage, ignore_errors=True)
        if getattr(self, "_worker_root", None) is not None:
            shutil.rmtree(self._worker_root, ignore_errors=True)

    def modal(self, context, event):
        addon = sys.modules.get(__package__.split(".")[0])
        if addon is not None and addon.progress_cancel_clicked(self, context, event):
            self._cancel_requested = True
        if event.type == "ESC" or getattr(self, "_cancel_requested", False):
            self._cancel_requested = True
            self._cancel_path.touch(exist_ok=True)
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        completed_resources = 0.0
        active_messages = []
        for worker in self._workers:
            try:
                progress = json.loads(worker["progress_path"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                progress = {"completed": 0.0, "message": f"Starting {worker['branch']} worker…"}
            completed_resources += min(worker["total"], max(0.0, float(progress.get("completed", 0.0))))
            active_messages.append((
                float(progress.get("updated_at", 0.0)),
                f"{worker['branch']}: {progress.get('message', 'Working…')}",
            ))
            code = worker["process"].poll()
            if code not in (None, 0) and not self._cancel_requested:
                self._worker_failure = worker
                self._cancel_requested = True
                self._cancel_path.touch(exist_ok=True)

        self.progress_value = min(1.0, completed_resources / max(1, self._resources))
        self._window_manager.progress_update(completed_resources)
        self.progress_text = "Stopping workers…" if self._cancel_requested else max(active_messages)[1]
        context.workspace.status_text_set(self.progress_text)
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()

        if self._cancel_requested:
            assembler = getattr(self, "_assembler_process", None)
            if assembler is not None and assembler.poll() is None:
                assembler.terminate()
                assembler.wait()
            if any(worker["process"].poll() is None for worker in self._workers):
                return {"RUNNING_MODAL"}
            failure = getattr(self, "_worker_failure", None)
            failure_log = ""
            if failure is not None:
                try:
                    failure_log = failure["log_path"].read_text(encoding="utf-8", errors="replace")[-3000:]
                except OSError:
                    pass
            failure_detail = ""
            if failure is not None:
                try:
                    failure_result = json.loads(failure["result_path"].read_text(encoding="utf-8"))
                    error_lines = [line.strip() for line in failure_result.get("error", "").splitlines() if line.strip()]
                    failure_detail = error_lines[-1] if error_lines else ""
                except (OSError, json.JSONDecodeError):
                    pass
            self._finish(context)
            if failure is not None:
                self.report({"ERROR"}, f"{failure['branch']} animation worker failed: {failure_detail or failure_log[-240:] or 'see worker log'}")
            else:
                self.report({"WARNING"}, "Animation bank export cancelled; partial files were removed")
            return {"CANCELLED"}

        if all(worker["process"].poll() is not None for worker in self._workers):
            failed = next((worker for worker in self._workers if worker["process"].returncode != 0), None)
            if failed is not None:
                self._worker_failure = failed
                self._cancel_requested = True
                self._cancel_path.touch(exist_ok=True)
                return {"RUNNING_MODAL"}
            assembled = {}
            try:
                if self._assemblies:
                    if not self._assembler_started:
                        python_path = sys.executable
                        if not python_path or not Path(python_path).is_file():
                            raise RuntimeError("Blender's Python executable is unavailable for G4PK assembly")
                        self._assembler_progress_path = self._worker_root / "assembly_progress.json"
                        self._assembler_result_path = self._worker_root / "assembly_result.json"
                        self._assembler_log_path = self._worker_root / "assembly_worker.log"
                        assembler_job = self._worker_root / "assembly_job.json"
                        assembler_job.write_text(json.dumps({
                            "assemblies": self._assemblies,
                            "output_root": str(self._stage),
                            "progress_path": str(self._assembler_progress_path),
                            "result_path": str(self._assembler_result_path),
                            "cancel_path": str(self._cancel_path),
                        }), encoding="utf-8")
                        assembler_script = Path(__file__).with_name("g4pk_assembly_worker.py")
                        with self._assembler_log_path.open("wb") as log_stream:
                            self._assembler_process = subprocess.Popen(
                                [python_path, str(assembler_script), str(assembler_job)],
                                stdout=log_stream,
                                stderr=subprocess.STDOUT,
                            )
                        self._assembler_started = True
                        self.progress_text = f"Rebuilding {len(self._assemblies)} animation banks…"
                        return {"RUNNING_MODAL"}

                    assembly_progress = {"completed": 0, "message": "Rebuilding animation banks…"}
                    try:
                        assembly_progress = json.loads(
                            self._assembler_progress_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError):
                        pass
                    assembly_completed = min(
                        len(self._assemblies), max(0, int(assembly_progress.get("completed", 0)))
                    )
                    self._window_manager.progress_update(completed_resources + assembly_completed)
                    self.progress_value = min(
                        1.0, (completed_resources + assembly_completed) / max(1, self._resources)
                    )
                    self.progress_text = assembly_progress.get("message", "Rebuilding animation banks…")
                    context.workspace.status_text_set(self.progress_text)
                    if self._assembler_process.poll() is None:
                        return {"RUNNING_MODAL"}
                    if self._assembler_process.returncode != 0:
                        result_data = {}
                        try:
                            result_data = json.loads(
                                self._assembler_result_path.read_text(encoding="utf-8")
                            )
                        except (OSError, json.JSONDecodeError):
                            pass
                        detail = str(result_data.get("error", "")).strip().splitlines()
                        message = detail[-1] if detail else "see the animation worker log"
                        raise RuntimeError(f"G4PK assembly failed: {message}")
                    result_data = json.loads(self._assembler_result_path.read_text(encoding="utf-8"))
                    if result_data.get("status") != "complete":
                        raise RuntimeError("G4PK assembly finished without a complete result")
                    assembled = result_data

                result = [json.loads(worker["result_path"].read_text(encoding="utf-8")) for worker in self._workers]
                if any(item.get("status") != "complete" for item in result):
                    raise RuntimeError("An animation worker finished without a complete result")
                publish_animation_overlay(self._stage, self._output)
            except Exception as exc:
                self._finish(context)
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            self._finish(context, succeeded=True)
            assembled_units = sum(item["worker_unit_count"] for item in self._assemblies)
            written = sum(item["written"] for item in result) - assembled_units + int(assembled.get("written", 0))
            skipped = sum(item["skipped"] for item in result) + int(assembled.get("skipped", 0))
            invalid_point_effects = sum(len(item.get("warnings", ())) for item in result)
            self.report(
                {"INFO"},
                f"Exported {written} resources; {skipped} unchanged for unmatched bones; "
                f"{invalid_point_effects} invalid point-effect banks copied unchanged",
            )
            if self.chain_victorytool_candidates and self.export_chr_animations:
                armature_name = self.armature_name
                source_bank_id = self.source_bank_id.strip().lower()
                target_bank_id = self.target_bank_id.strip().lower()
                data_overlay = self._output

                def start_victorytool_candidate_export():
                    try:
                        from ..porting import g4_port_addon
                    except ImportError:
                        from porting import g4_port_addon
                    try:
                        armature = bpy.context.scene.objects.get(armature_name)
                        if armature is None:
                            raise RuntimeError("The target armature could not be found")
                        props = g4_port_addon.settings(bpy.context)
                        original_model = g4_port_addon.resolve_file(props.original_model)
                        prefs = g4_port_addon.addon_preferences()
                        roots = candidate_data_roots(
                            original_model, getattr(prefs, "raw_data_root", "")
                        )
                        bank_sources = discover_victorytool_animation_sources(source_bank_id, roots)
                        model_path = Path(str(armature.get("g4_exported_model_path", ""))).resolve()
                        count = publish_victorytool_candidates_from_overlay(
                            bank_sources,
                            source_bank_id,
                            target_bank_id,
                            model_path,
                            data_overlay,
                        )
                    except Exception as exc:
                        write_deferred_call_error(
                            "The game animation overlay was exported, but VictoryTool candidate banks "
                            f"could not be created from it: {exc}"
                        )
                        return
                    print(f"[EXPORT] Published {count} selected CHR banks for VictoryTool beside {model_path.name}")

                defer_blender_call(
                    start_victorytool_candidate_export,
                    label="Publish selected patched CHR banks for VictoryTool",
                )
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        if getattr(self, "_cancel_path", None) is not None:
            self._cancel_path.touch(exist_ok=True)
        assembler = getattr(self, "_assembler_process", None)
        if assembler is not None and assembler.poll() is None:
            assembler.terminate()
            assembler.wait()
        for worker in getattr(self, "_workers", ()):
            if worker["process"].poll() is None:
                worker["process"].terminate()
        for worker in getattr(self, "_workers", ()):
            if worker["process"].poll() is None:
                worker["process"].wait()
        self._finish(context)


class EXPORT_OT_level5_g4mt_victorytool_family(Operator):
    bl_idname = "export_scene.level5_g4mt_victorytool_family"
    bl_label = "Export Animation Family for VictoryTool"
    bl_options = {"REGISTER"}

    source_bank_id: StringProperty(name="Source Bank Family", default="")
    armature_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    progress_text: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    progress_value: FloatProperty(options={"HIDDEN", "SKIP_SAVE"}, min=0.0, max=1.0)
    progress_window_open: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        armature = context.active_object
        if armature is None or armature.type != "ARMATURE":
            armature = next((obj for obj in context.scene.objects
                             if obj.type == "ARMATURE" and obj.get("g4_exported_model_path")), None)
        self.armature_name = armature.name if armature is not None else ""
        action = armature.animation_data.action if armature and armature.animation_data else None
        source = Path(str(action.get("g4mt_source", ""))) if action is not None else Path()
        self.source_bank_id = character_bank_id_from_path(source) or self.source_bank_id
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        if self.progress_window_open:
            self.layout.label(text=self.progress_text, icon="FILE_REFRESH")
            self.layout.progress(factor=self.progress_value, type="BAR", text="Animation export")
            return
        self.layout.prop(self, "source_bank_id")
        armature = context.scene.objects.get(self.armature_name)
        model_value = str(armature.get("g4_exported_model_path", "")) if armature else ""
        model_path = Path(model_value) if model_value else None
        if model_path is not None:
            self.layout.label(text=f"Output beside {model_path.name}", icon="FILE_FOLDER")
        self.layout.label(text="Writes VictoryTool-named G4MT banks and the matching rig profile.", icon="ACTION")

    def execute(self, context):
        armature = context.scene.objects.get(self.armature_name)
        if armature is None or armature.type != "ARMATURE":
            self.report({"ERROR"}, "Select the exported character armature")
            return {"CANCELLED"}
        bank_id = self.source_bank_id.strip().lower()
        if not re.fullmatch(r"c\d{4,10}", bank_id):
            self.report({"ERROR"}, "Source bank family must be a character ID such as c000101")
            return {"CANCELLED"}

        model_path = Path(str(armature.get("g4_exported_model_path", ""))).resolve()
        if model_path.suffix.lower() != ".g4md" or not model_path.is_file():
            self.report({"ERROR"}, "Export the model before exporting animations")
            return {"CANCELLED"}
        try:
            from ..porting import g4_port_addon
        except ImportError:
            from porting import g4_port_addon

        props = g4_port_addon.settings(context)
        original_model = g4_port_addon.resolve_file(props.original_model)
        raw_root = g4_port_addon.infer_data_root(original_model)
        if not original_model.is_file() or raw_root is None:
            self.report({"ERROR"}, "The original model must be inside the configured game data tree")
            return {"CANCELLED"}
        try:
            resolved_bank_id, source_skeleton_id = resolve_animation_family_ids(
                original_model, [raw_root]
            )
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if bank_id != resolved_bank_id:
            self.report(
                {"ERROR"},
                f"Animation family {bank_id} does not match the selected original model's family {resolved_bank_id}",
            )
            return {"CANCELLED"}
        prefs = g4_port_addon.addon_preferences()
        roots = candidate_data_roots(Path(), getattr(prefs, "raw_data_root", ""))
        if raw_root.resolve() not in roots:
            roots.append(raw_root.resolve())
        bank_sources = discover_victorytool_animation_sources(bank_id, roots)
        if not bank_sources:
            self.report({"ERROR"}, f"No common/chr animation banks found for {bank_id}")
            return {"CANCELLED"}
        exported_skeleton = str(armature.get("g4_exported_skeleton_path", ""))
        target_skeleton_path = Path(exported_skeleton) if exported_skeleton else None
        if target_skeleton_path is None or not target_skeleton_path.is_file():
            target_skeleton_path = g4_port_addon.active_rig_skeleton_path(props, original_model)
        if target_skeleton_path is None or not target_skeleton_path.is_file():
            self.report({"ERROR"}, "Could not resolve the G4SK used by the original model")
            return {"CANCELLED"}

        stem = model_path.stem
        expected_prefix = f"{stem}_from_{bank_id}_p"
        existing = list(model_path.parent.glob(f"{stem}_from_*_p*.g4mt"))
        foreign = [path for path in existing if not path.name.startswith(expected_prefix)]
        invalid = [path for path in existing if not re.fullmatch(
            rf"{re.escape(stem)}_from_{re.escape(bank_id)}_p\d{{3}}\.g4mt", path.name, re.IGNORECASE
        )]
        if foreign or invalid:
            self.report({"ERROR"}, "Remove previous or nonstandard animation candidates beside the model before exporting another family")
            return {"CANCELLED"}

        try:
            previous_active = context.view_layer.objects.active
            previous_selection = [obj for obj in context.view_layer.objects if obj.select_get()]
            self._previous_active = previous_active
            self._previous_selection = previous_selection
            context.view_layer.objects.active = armature
            source_skeleton = source_skeleton_for_bank(
                bank_sources[0][1], source_skeleton_id, bank_sources[0][2], None
            )
            if source_skeleton is None:
                raise RuntimeError(f"Could not resolve a source G4SK for {bank_sources[0][1].name}")
            source_skeleton_data = source_skeleton.read_bytes()
            source_hash = hashlib.sha256(source_skeleton_data).hexdigest()
            try:
                from ..porting.g4_model_probe import configure_raw_data_root_from_path, resolve_g4sk_from_chara_model
            except ImportError:
                from porting.g4_model_probe import configure_raw_data_root_from_path, resolve_g4sk_from_chara_model
            reference = g4_port_addon.active_rig_reference_model(props, original_model)
            configure_raw_data_root_from_path(reference)
            reference_data, _reference_source = resolve_g4sk_from_chara_model(reference)
            if reference_data is None or hashlib.sha256(reference_data).hexdigest().casefold() != source_hash.casefold():
                raise RuntimeError("Choose the source model/G4SK that belongs to the selected animation family")

            source_joint_names = set(parse_g4sk(source_skeleton_data)["names"])
            target_joint_names = set(parse_g4sk(target_skeleton_path.read_bytes())["names"])
            source_joint_mapping = g4_port_addon.animation_source_joint_map(
                props, source_joint_names, target_joint_names
            )
            source_joint_mapping = route_root_motion_to_global_helper(
                source_joint_mapping,
                source_joint_names,
                target_joint_names,
            )
            temporary_armature = create_victorytool_target_armature(
                target_skeleton_path, model_path, source_joint_mapping
            )
            self._armature = temporary_armature
            for obj in context.view_layer.objects:
                obj.select_set(False)
            temporary_armature.select_set(True)
            context.view_layer.objects.active = temporary_armature

            stage = Path(tempfile.mkdtemp(prefix=f".{stem}_animation_", dir=model_path.parent))
            self._output_directory = model_path.parent
            self._stage = stage
            self._output_files = {path.name for path in existing}
            self._generator = export_victorytool_animation_candidates(
                bank_sources, bank_id, source_skeleton_id, model_path, stage,
                temporary_armature, None,
            )
            self._profile = {
                "props": props,
                "original_model": original_model,
                "model_path": model_path,
                "target_skeleton_path": target_skeleton_path,
                "raw_root": raw_root,
                "source_hash": source_hash,
            }
            self._window_manager = context.window_manager
            self._timer = self._window_manager.event_timer_add(0.01, window=context.window)
            self._window_manager.progress_begin(0, len(bank_sources))
            self._progress_started = True
            self._resources = len(bank_sources)
            self._last_progress = 0.0
            self.progress_text = "Preparing animation banks"
            self.progress_value = 0.0
            self.progress_window_open = True
            self._cancel_requested = False
            context.window_manager.modal_handler_add(self)
            addon = sys.modules.get(__package__.split(".")[0])
            addon.begin_progress_dialog(self, context)
            return {"RUNNING_MODAL"}
        except Exception as exc:
            self._finish(context)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def _finish(self, context, succeeded=False):
        self.progress_window_open = False
        if getattr(self, "_generator", None) is not None:
            self._generator.close()
            self._generator = None
        if getattr(self, "_timer", None) is not None:
            self._window_manager.event_timer_remove(self._timer)
            self._timer = None
        if getattr(self, "_progress_started", False):
            self._window_manager.progress_end()
            self._progress_started = False
        context.workspace.status_text_set(None)
        addon = sys.modules.get(__package__.split(".")[0])
        if addon is not None:
            addon.end_progress_dialog(self)
        temporary = getattr(self, "_armature", None)
        if temporary is not None and temporary.name in bpy.data.objects:
            data = temporary.data
            bpy.data.objects.remove(temporary, do_unlink=True)
            if data.users == 0:
                bpy.data.armatures.remove(data)
        for obj in getattr(self, "_previous_selection", ()):
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        previous_active = getattr(self, "_previous_active", None)
        if previous_active is not None and previous_active.name in bpy.data.objects:
            context.view_layer.objects.active = previous_active
        stage = getattr(self, "_stage", None)
        if stage is not None and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)

    def modal(self, context, event):
        addon = sys.modules.get(__package__.split(".")[0])
        if addon is not None and addon.progress_cancel_clicked(self, context, event):
            self._cancel_requested = True
        if event.type == "ESC" or getattr(self, "_cancel_requested", False):
            self._finish(context)
            self.report({"WARNING"}, "Animation export cancelled; staged files were removed")
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        try:
            update = next(self._generator)
            if isinstance(update, tuple):
                self._last_progress, message = update
            else:
                message = update
            self._window_manager.progress_update(self._last_progress * self._resources)
            self.progress_value = min(1.0, max(0.0, self._last_progress))
            self.progress_text = message
            context.workspace.status_text_set(message)
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type in {"WINDOW", "UI"}:
                        area.tag_redraw()
        except StopIteration as completed:
            written, skipped = completed.value
            staged_files = list(self._stage.glob("*.g4mt"))
            if len(staged_files) != written:
                self._finish(context)
                self.report({"ERROR"}, "Animation staging did not contain every exported bank")
                return {"CANCELLED"}
            model_path = self._profile["model_path"]
            profile_path = model_path.with_suffix(".g4anim.json")
            previous_profile = profile_path.read_bytes() if profile_path.exists() else None
            replaced = []
            overwritten = {}
            try:
                for path in staged_files:
                    destination = self._output_directory / path.name
                    overwritten[destination] = destination.read_bytes() if destination.exists() else None
                    os.replace(path, destination)
                    replaced.append(destination)
                from ..porting import g4_port_addon
                profile_path = g4_port_addon.write_animation_profile(
                    self._profile["props"], self._profile["original_model"], model_path,
                    self._profile["target_skeleton_path"], self._profile["raw_root"],
                )
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                if profile["sourceRig"]["sha256"].casefold() != self._profile["source_hash"].casefold():
                    raise RuntimeError("The VictoryTool profile source rig does not match the selected animation family")
            except Exception:
                for path in replaced:
                    original = overwritten[path]
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(original)
                if previous_profile is None:
                    profile_path.unlink(missing_ok=True)
                else:
                    profile_path.write_bytes(previous_profile)
                raise
            staged_names = {path.name for path in staged_files}
            for name in self._output_files - staged_names:
                (self._output_directory / name).unlink(missing_ok=True)
            self._finish(context, succeeded=True)
            self.report({"INFO"}, f"Exported {written} VictoryTool animation banks; {skipped} banks had no matching tracks")
            return {"FINISHED"}
        except Exception as exc:
            self._finish(context)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        self._finish(context)


def generated_g4mt_output_path(armature, source_path: Path) -> Path | None:
    model_value = str(armature.get("g4_exported_model_path", "")) if armature else ""
    model_path = Path(model_value) if model_value else None
    bank = re.fullmatch(r"(.+)_p([0-9]{3})", source_path.stem, re.IGNORECASE)
    if (model_path is None or model_path.suffix.lower() != ".g4md" or not model_path.is_file()
            or not model_path.with_suffix(".g4anim.json").is_file() or bank is None
            or not source_path.parent.name):
        return None
    return model_path.with_name(f"{model_path.stem}_from_{source_path.parent.name}_p{bank.group(2)}.g4mt")


def build_g4sk_from_armature(armature, template: bytes) -> tuple[bytes, int, int, float]:
    if armature is None or armature.type != "ARMATURE":
        raise ValueError("Select the target armature")

    prepared = armature.copy()
    prepared.data = armature.data.copy()
    bpy.context.scene.collection.objects.link(prepared)
    try:
        ensure_native_motion_helpers(prepared)
        return _build_g4sk_from_prepared_armature(armature, prepared, template)
    finally:
        prepared_data = prepared.data
        bpy.data.objects.remove(prepared, do_unlink=True)
        if prepared_data.users == 0:
            bpy.data.armatures.remove(prepared_data)


def _build_g4sk_from_prepared_armature(mesh_armature, armature, template: bytes) -> tuple[bytes, int, int, float]:
    from .humanoid_avatar_pose import avatar_export_alignment

    bones = list(armature.data.bones)

    def depth(bone):
        value = 0
        parent = bone.parent
        while parent is not None:
            value += 1
            parent = parent.parent
        return value

    source_order = {bone.name: index for index, bone in enumerate(bones)}
    if any(
        bone.parent is not None and source_order[bone.parent.name] >= source_order[bone.name]
        for bone in bones
    ):
        bones.sort(key=depth)
    index_by_name = {bone.name: index for index, bone in enumerate(bones)}
    if len(index_by_name) != len(bones):
        raise ValueError("The armature contains duplicate joint names")

    parents = [index_by_name[bone.parent.name] if bone.parent else None for bone in bones]
    conversion = g4_from_blender_matrix()
    inverse_conversion = conversion.inverted()
    alignment = avatar_export_alignment(mesh_armature, template)
    rotation_alignment = alignment.to_quaternion().to_matrix().to_4x4()
    world_rotation = armature.matrix_world.to_quaternion().to_matrix().to_4x4()
    globals_ = []
    for bone in bones:
        global_matrix = conversion @ rotation_alignment @ world_rotation @ bone.matrix_local @ inverse_conversion
        global_matrix.translation = conversion @ alignment @ (armature.matrix_world @ bone.matrix_local.translation)
        globals_.append(global_matrix)
    native_skeleton = parse_g4sk(template)
    native_indices = {name: index for index, name in enumerate(native_skeleton["names"])}
    for name in NATIVE_HELPER_NAMES:
        if name not in native_indices or name not in index_by_name:
            raise ValueError(f"G4SK template or target is missing required helper joint {name}")
        values = native_skeleton["bind_matrices"][native_indices[name]]
        globals_[index_by_name[name]] = Matrix([values[row * 4:row * 4 + 4] for row in range(4)])
    # Child locals must use the native helper bases that will be serialized.
    locals_ = [
        globals_[parent].inverted() @ globals_[index] if parent is not None else globals_[index]
        for index, parent in enumerate(parents)
    ]
    radii = [0.0] * len(bones)
    weighted_assignments = 0
    mesh_count = 0
    unmatched_groups = set()

    for mesh in bpy.context.scene.objects:
        if mesh.type != "MESH" or not any(
            modifier.type == "ARMATURE" and modifier.object == mesh_armature
            for modifier in mesh.modifiers
        ):
            continue
        mesh_count += 1
        group_bones = {group.index: index_by_name.get(group.name) for group in mesh.vertex_groups}
        for vertex in mesh.data.vertices:
            position = conversion @ alignment @ (mesh.matrix_world @ vertex.co)
            for assignment in vertex.groups:
                if assignment.weight <= 0.0:
                    continue
                joint_index = group_bones.get(assignment.group)
                if joint_index is None:
                    unmatched_groups.add(mesh.vertex_groups[assignment.group].name)
                    continue
                radii[joint_index] = max(
                    radii[joint_index],
                    (position - globals_[joint_index].translation).length,
                )
                weighted_assignments += 1

    if unmatched_groups:
        names = ", ".join(sorted(unmatched_groups))
        raise ValueError(f"Positive skin weights reference joints outside the armature: {names}")
    if mesh_count == 0 or weighted_assignments == 0:
        raise ValueError("The armature has no positively weighted attached meshes")

    nodes = []
    max_srt_error = 0.0

    def matrix12(matrix):
        return tuple(float(matrix[row][column]) for row in range(3) for column in range(4))

    for index, (bone, parent, global_matrix, local_matrix) in enumerate(
        zip(bones, parents, globals_, locals_)
    ):
        location, rotation, scale = local_matrix.decompose()
        rotation.normalize()
        reconstructed = Matrix.LocRotScale(location, rotation, scale)
        max_srt_error = max(
            max_srt_error,
            max(abs(reconstructed[row][column] - local_matrix[row][column])
                for row in range(4) for column in range(4)),
        )
        if max_srt_error > 1e-4:
            raise ValueError(f"Joint transforms contain non-SRT shear near {bone.name}")
        local_srt = (
            float(scale.x), float(scale.y), float(scale.z), 0.0,
            float(rotation.x), float(rotation.y), float(rotation.z), float(rotation.w),
            float(location.x), float(location.y), float(location.z), 0.0,
        )
        nodes.append(G4SKNode(
            name=bone.name,
            parent_index=parent,
            local_srt=local_srt,
            global_matrix=matrix12(global_matrix),
            inverse_bind_matrix=matrix12(global_matrix.inverted()),
            flags=1,
            bounds_radius=float(radii[index]),
        ))

    nodes = copy_native_helper_fields(template, nodes)
    return write_g4sk_v104(template, nodes), len(bones), mesh_count, max_srt_error


class EXPORT_OT_level5_g4sk(Operator, ExportHelper):
    bl_idname = "export_scene.level5_g4sk"
    bl_label = "Export Level-5 G4 Skeleton Candidate"
    bl_options = {"PRESET"}

    filename_ext = ".g4sk"
    filter_glob: StringProperty(default="*.g4sk", options={"HIDDEN"})
    template_path: StringProperty(name="G4SK Template", subtype="FILE_PATH")

    def draw(self, context):
        self.layout.prop(self, "template_path")
        self.layout.label(text="Exports the selected rig and its positively weighted meshes.", icon="ARMATURE_DATA")

    def execute(self, context):
        armature = context.active_object
        template_path = Path(bpy.path.abspath(self.template_path))
        output_path = Path(bpy.path.abspath(self.filepath))
        if not template_path.is_file():
            self.report({"ERROR"}, "Select an existing G4SK v104 template")
            return {"CANCELLED"}
        if template_path.resolve() == output_path.resolve():
            self.report({"ERROR"}, "Output path must not overwrite the G4SK template")
            return {"CANCELLED"}
        if not output_path.parent.is_dir():
            self.report({"ERROR"}, "Output directory does not exist")
            return {"CANCELLED"}

        temporary_path = None
        try:
            candidate, joint_count, mesh_count, srt_error = build_g4sk_from_armature(
                armature, template_path.read_bytes()
            )
            handle, temporary_path = tempfile.mkstemp(
                prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
            )
            with os.fdopen(handle, "wb") as stream:
                stream.write(candidate)
            os.replace(temporary_path, output_path)
        except (OSError, RuntimeError, ValueError, struct.error) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)

        self.report({"INFO"}, f"Wrote {joint_count} joints from {mesh_count} skinned meshes (SRT error {srt_error:.2g})")
        return {"FINISHED"}


class EXPORT_OT_level5_g4_fbx(Operator, ExportHelper):
    bl_idname = "export_scene.level5_g4_fbx"
    bl_label = "Export Level-5 G4 Scene"
    bl_options = {"PRESET"}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={"HIDDEN"})
    selected_only: BoolProperty(
        name="Selected Objects Only",
        default=False,
    )
    export_camera: BoolProperty(
        name="Export Camera",
        default=True,
    )
    include_meshes: BoolProperty(
        name="Include Meshes",
        default=True,
        description="Include skinned meshes; disable when the destination already has the models",
    )
    simplify_factor: FloatProperty(
        name="Animation Simplification",
        default=0.0,
        min=0.0,
        soft_max=1.0,
        description="FBX curve reduction; zero preserves subtle facial and finger motion",
    )

    def execute(self, context):
        object_types = {"EMPTY", "ARMATURE"}
        if self.include_meshes:
            object_types.add("MESH")
        if self.export_camera:
            object_types.add("CAMERA")
        try:
            result = bpy.ops.export_scene.fbx(
                filepath=self.filepath,
                use_selection=self.selected_only,
                object_types=object_types,
                use_custom_props=False,
                add_leaf_bones=False,
                path_mode="AUTO",
                embed_textures=False,
                bake_anim=True,
                bake_anim_use_all_bones=False,
                bake_anim_use_nla_strips=False,
                bake_anim_use_all_actions=False,
                bake_anim_force_startend_keying=False,
                bake_anim_step=1.0,
                bake_anim_simplify_factor=self.simplify_factor,
            )
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if "FINISHED" not in result:
            return {"CANCELLED"}
        size_mb = Path(self.filepath).stat().st_size / (1024 * 1024)
        self.report({"INFO"}, f"Exported optimized FBX: {size_mb:.1f} MB")
        return {"FINISHED"}


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_level5_g4mt.bl_idname, text="Level-5 G4 Animation (.g4mt/.g4pk)")
    self.layout.operator(IMPORT_OT_level5_g4cm.bl_idname, text="Level-5 G4 Camera (.g4cm)")
    self.layout.operator(IMPORT_OT_level5_g4_event_folder.bl_idname, text="Level-5 G4 Event Folder")


def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_level5_g4mt.bl_idname, text="Level-5 G4 Animation Candidate (.g4mt)")
    self.layout.operator(EXPORT_OT_level5_g4mt_bank_family.bl_idname, text="Retarget Original Bank Family")
    self.layout.operator(
        EXPORT_OT_level5_g4mt_victorytool_family.bl_idname,
        text="Export Animation Family for VictoryTool",
    )
    self.layout.operator(EXPORT_OT_level5_g4sk.bl_idname, text="Level-5 G4 Skeleton Candidate (.g4sk)")
    self.layout.operator(EXPORT_OT_level5_g4_fbx.bl_idname, text="Level-5 G4 Scene (.fbx)")


classes = [
    VIEW3D_OT_level5_g4mt_select_action,
    VIEW3D_PT_level5_g4mt_bank,
    IMPORT_OT_level5_g4mt_pick_model,
    IMPORT_OT_level5_g4mt_pick_body,
    IMPORT_OT_level5_g4mt_pick_shoes,
    IMPORT_OT_level5_g4mt,
    IMPORT_OT_level5_g4cm,
    G4EventCharacterPart,
    IMPORT_OT_level5_g4_event_parts,
    IMPORT_OT_level5_g4_event_folder,
    EXPORT_OT_level5_g4mt,
    EXPORT_OT_level5_g4mt_bank_family,
    EXPORT_OT_level5_g4mt_victorytool_family,
    EXPORT_OT_level5_g4sk,
    EXPORT_OT_level5_g4_fbx,
]


if hasattr(bpy.types, "FileHandler"):
    class G4MT_FH_import(bpy.types.FileHandler):
        bl_idname = "G4MT_FH_import"
        bl_label = "Level-5 G4 Animation"
        bl_import_operator = IMPORT_OT_level5_g4mt.bl_idname
        bl_file_extensions = ".g4mt;.g4pk"

        @classmethod
        def poll_drop(cls, context):
            return context.area is not None and context.area.type in {"VIEW_3D", "OUTLINER", "FILE_BROWSER"}

    classes.append(G4MT_FH_import)

    class G4CM_FH_import(bpy.types.FileHandler):
        bl_idname = "G4CM_FH_import"
        bl_label = "Level-5 G4 Camera"
        bl_import_operator = IMPORT_OT_level5_g4cm.bl_idname
        bl_file_extensions = ".g4cm"

        @classmethod
        def poll_drop(cls, context):
            return context.area is not None and context.area.type in {"VIEW_3D", "OUTLINER", "FILE_BROWSER"}

    classes.append(G4CM_FH_import)




def register():
    # Older installed builds registered a separate Event Character Parts
    # operator.  Remove it on reload so it cannot survive in Blender's Python
    # session and reintroduce the obsolete first dialog.
    legacy = getattr(bpy.types, "IMPORT_SCENE_OT_level5_g4_event_parts", None)
    if legacy is not None:
        try:
            bpy.utils.unregister_class(legacy)
        except RuntimeError:
            pass
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
