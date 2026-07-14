import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import defaultdict
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, FloatProperty, StringProperty
from bpy.types import Operator, PropertyGroup
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Matrix, Quaternion, Vector

try:
    from .g4pk_extract_g4mt import select_g4mt_entry
    from .g4mt_probe import parse_g4mt, read_g4sk_data
    from .g4mt_motion import decode_motion, simplify_motion_samples
    from .g4cm_camera import decode_camera, parse_g4cm
    from .g4_event import event_light_parameters, load_event_actor_models, load_event_actor_points
    from .g4_p3lip import read_p3lip
except ImportError:
    from g4pk_extract_g4mt import select_g4mt_entry
    from g4mt_probe import parse_g4mt, read_g4sk_data
    from g4mt_motion import decode_motion, simplify_motion_samples
    from g4cm_camera import decode_camera, parse_g4cm
    from g4_event import event_light_parameters, load_event_actor_models, load_event_actor_points
    from g4_p3lip import read_p3lip


ADDON_ID = __name__
MODEL_ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}\d{4,10})(?![A-Za-z0-9])")
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
        if inferred.parent.name in {"raw", "readable"}:
            work_root = inferred.parent.parent
            roots.extend((work_root / "raw" / "data", work_root / "readable" / "data"))
            if work_root.name == "._work":
                roots.append(work_root.parent / "data")
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
    raise RuntimeError("g4mt_motion.py was not found. Configure it in the addon preferences.")


def resolve_camera_decoder(prefs) -> Path:
    configured = bpy.path.abspath(getattr(prefs, "camera_decoder_script", "") or "")
    decoder = Path(configured) if configured else Path()
    if decoder.is_file():
        return decoder
    fallback = default_camera_decoder_script()
    if fallback:
        return Path(fallback)
    raise RuntimeError("g4cm_camera.py was not found. Configure it in the addon preferences.")


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
    destination = cache_root / safe_name
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


def resolve_skeleton_path(model_path: Path | None) -> Path | None:
    if model_path is None:
        return None
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
    return resolved


def armature_score(obj, names: set[str]) -> int:
    if obj is None or obj.type != "ARMATURE":
        return -1
    return sum(name in obj.data.bones for name in names)


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
) -> tuple[int, int]:
    parts = character_part_armatures(actor)
    if not parts:
        return 0, 0
    created = 0
    keyed = 0
    for part_armature in parts:
        action, keyed_bones = create_action(part_armature, motion, frame_origin)
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
    for curve in tuple(action.fcurves):
        match = re.fullmatch(r'pose\.bones\["(.+)"\]\.(?:location|scale)', curve.data_path)
        if match is None:
            continue
        bone = armature.data.bones.get(match.group(1))
        if bone is None or bone.parent is None:
            continue
        action.fcurves.remove(curve)
        removed += 1
    return removed


def has_display_oriented_bones(armature) -> bool:
    return bool(
        armature
        and armature.type == "ARMATURE"
        and any("g4_rest_rotation_xyzw" in bone for bone in armature.data.bones)
    )


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
):
    if model_path is None:
        raise RuntimeError("Select the G4MD/G4PKM model that will receive the animation.")
    if not hasattr(bpy.ops.import_scene, "level5_g4"):
        raise RuntimeError("Enable the separate 'Level-5 G4 Model Importer' addon before importing the model.")

    before = set(bpy.data.objects)
    original_orientation = getattr(prefs, "apply_bone_orientation", False)
    try:
        prefs.apply_bone_orientation = False
        result = bpy.ops.import_scene.level5_g4(
            filepath=str(model_path),
            create_report_text=False,
            import_character_parts=import_character_parts,
            auto_character_parts=auto_character_parts,
            body_model=body_model,
            shoes_model=shoes_model,
            accessory_model=accessory_model,
            gloves_model=gloves_model,
            armband_model=armband_model,
            nameplate_model=nameplate_model,
            attach_ball=attach_ball,
            ball_model=ball_model,
            character_part_stem=character_part_stem,
            preserve_character_part_armatures=False,
        )
    finally:
        prefs.apply_bone_orientation = original_orientation
    if "FINISHED" not in result:
        raise RuntimeError(f"Level-5 G4 Model Importer could not import {model_path}")
    imported = set(bpy.data.objects) - before
    armature = best_armature(imported, track_bone_names(motion))
    if armature is None:
        raise RuntimeError(f"The imported model contains no usable armature: {model_path}")
    resolve_track_names_from_armature(motion, armature)
    if align_to_motion_rest:
        align_armature_to_motion_rest(armature, motion)
    return armature, model_path


def source_matrix(transform: dict) -> Matrix:
    translation = Vector(transform["translation"])
    x, y, z, w = transform["rotation"]
    rotation = Quaternion((w, x, y, z))
    scale = Vector(transform["scale"])
    return Matrix.LocRotScale(translation, rotation, scale)


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


def append_curve_samples(action, cache: dict, data_path: str, index: int, group: str, frames, values) -> None:
    if len(values) > 2 and max(values) - min(values) <= 1e-9:
        frames = (frames[0], frames[-1])
        values = (values[0], values[-1])
    key = (data_path, index)
    curve = cache.get(key)
    if curve is None:
        curve = action.fcurves.find(data_path, index=index)
        if curve is None:
            curve = action.fcurves.new(data_path, index=index, action_group=group)
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
) -> tuple[int, int]:
    cache = curve_cache if curve_cache is not None else {}
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
    for track in motion["tracks"]:
        name = track.get("target_name")
        rest = rest_by_name.get(name)
        pose_bones = [armature.pose.bones[name]] if name and name in armature.pose.bones else []
        if not pose_bones:
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
            continue
        parent_track = tracks_by_name.get(parent_by_name.get(name))
        primary_pose_bone = next((bone for bone in pose_bones if bone.name == name), pose_bones[0])
        blender_rest = blender_local_rest_matrix(primary_pose_bone)
        basis_correction = rest.inverted_safe() @ blender_rest
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
                    append_curve_samples(
                        action,
                        cache,
                        data_path,
                        component,
                        pose_bone.name,
                        reduced_frames,
                        [sample[component] for sample in reduced_samples],
                    )
            keyed_bones += 1
    return keyed_bones, relative_rest_tracks


def create_action(armature, motion: dict, frame_origin: int | None = None) -> tuple[bpy.types.Action, int]:
    clip = motion["clip"]
    action = bpy.data.actions.new(name=clip["name"] or "G4MT Animation")
    action.use_fake_user = True
    armature.animation_data_create()
    clear_pose(armature)
    source_start = clip["start_frame"] if frame_origin is None else frame_origin
    keyed_bones, relative_rest_tracks = append_motion_to_action(armature, motion, action, source_start)
    armature.animation_data.action = None
    armature.animation_data.action = action

    action["g4_relative_rest_tracks"] = relative_rest_tracks
    return action, keyed_bones


def g4mt_import_settings(operator) -> str:
    return json.dumps(
        {
            "entry": operator.entry,
            "clip": operator.clip,
            "import_camera": operator.import_camera,
            "set_active_camera": operator.set_active_camera,
            "reuse_selected_armature": operator.reuse_selected_armature,
            "set_scene_fps": operator.set_scene_fps,
        }
    )


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


def defer_blender_call(callback) -> None:
    def run():
        callback()
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
    return bpy.ops.import_scene.level5_g4mt(
        "EXEC_DEFAULT",
        filepath=animation_path,
        model_path=model_path,
        body_model=body_model,
        shoes_model=shoes_model,
        import_model=True,
        import_character_parts=bool(body_model or shoes_model),
        prompt_for_models=False,
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
        default="0",
        description="Clip index or exact clip name inside the G4MT bank",
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
        description="Animate the selected armature when its bone names match the G4MT targets",
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
        layout.prop(self, "clip")
        layout.prop(self, "import_model")
        if self.import_model:
            layout.label(text="Model and character cosmetics will be requested after the animation", icon="FILE_FOLDER")
        layout.prop(self, "import_camera")
        if self.import_camera:
            layout.prop(self, "set_active_camera")
        layout.prop(self, "reuse_selected_armature")
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
        decoded_path = None
        extracted_g4mt_path = None
        package_entry_name = None
        camera_decoded_path = None
        camera_object = None
        camera_path = None
        camera_motion = None
        try:
            g4mt_path, extracted_g4mt_path, package_entry_name = materialize_g4mt(path, self.entry)
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
            skeleton_hint = resolve_skeleton_path(skeleton_model_hint)
            motion, decoded_path = decode_g4mt(g4mt_path, self.clip, prefs, skeleton_hint)
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
                )
            if armature is None:
                armature = best_armature(bpy.data.objects, names)
                if armature is not None:
                    resolve_track_names_from_armature(motion, armature)
            names = track_bone_names(motion)
            if armature is None or armature_score(armature, names) <= 0:
                raise RuntimeError("No armature matches the G4MT targets")

            starts = [motion["clip"]["start_frame"]]
            ends = [motion["clip"]["end_frame"]]
            if camera_motion is not None:
                starts.append(camera_motion["clip"]["start_frame"])
                ends.append(camera_motion["clip"]["end_frame"])
            frame_origin = min(starts)

            action, keyed_bones = create_action(armature, motion, frame_origin)
            part_actions, part_keyed_bones = animate_character_parts(armature, motion, frame_origin)
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
            action["g4mt_source"] = str(path)
            action["g4mt_clip_index"] = motion["clip"]["index"]
            action["g4mt_clip_name"] = motion["clip"]["name"]
            action["g4_source_frame_origin"] = frame_origin
            if model_path is not None:
                action["g4_model_source"] = str(model_path)
            if skeleton_hint is not None:
                action["g4_skeleton_source"] = str(skeleton_hint)
            if package_entry_name is not None:
                action["g4pk_entry"] = package_entry_name
            if camera_path is not None and camera_motion is not None:
                action["g4cm_source"] = str(camera_path)
                action["g4cm_clip_name"] = camera_motion["clip"]["name"]
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
            f"Imported {motion['clip']['name']}: {keyed_bones + part_keyed_bones} bones, {frame_count} actor frames, {part_actions} parts"
            f"{' with matching camera' if camera_object is not None else ''} on {armature.name}",
        )
        return {"FINISHED"}


def g4cm_vector(value) -> Vector:
    """Convert the game's right-handed Y-up coordinates to Blender Z-up."""
    return Vector((value[0], -value[2], value[1]))


def lens_from_vertical_fov(camera_data, fov: float) -> float:
    safe_fov = max(1e-4, min(float(fov), 3.13))
    return camera_data.sensor_height / (2.0 * math.tan(safe_fov * 0.5))


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
        for fcurve in action.fcurves:
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


def resolve_event_point_skeleton(directory: Path, prefs) -> Path | None:
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        candidate = data_root / "common" / "event" / "ev_point" / "point" / "point.g4sk"
        if candidate.is_file():
            return candidate
    return None


def discover_event_effects(directory: Path, prefs) -> list[dict]:
    family_match = re.match(r"(ev\d+)", directory.name, re.IGNORECASE)
    if family_match is None:
        return []
    family = family_match.group(1).lower()
    results = []
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        family_root = data_root / "common" / "effect" / "event" / family
        event_key = directory.name.lower().replace("_", "")
        exact_root = family_root / event_key
        effect_root = exact_root if exact_root.is_dir() else family_root
        if not effect_root.is_dir():
            continue
        model_paths = sorted(effect_root.rglob("*.g4pkm"))
        for model in model_paths:
            asset_directory = model.parent
            particle = next(asset_directory.glob("*.ptlb"), None)
            cut = None
            suffix = re.search(r"(\d{5})$", model.stem)
            if exact_root.is_dir() and suffix:
                cut = f"c{int(suffix.group(1)) // 10:04d}"
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
                "shaders": sorted(shader_names),
                "cut": cut,
            })
        if results:
            break
    return results


def discover_event_p3lip(directory: Path, prefs) -> list[Path]:
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        for language in ("ja", "en"):
            sound_root = data_root / "common" / "sound" / language
            paths = sorted(sound_root.glob(f"{directory.name}_*.p3lip"))
            if paths:
                return paths
    return []


def import_event_p3lip_controllers(
    paths: list[Path], cut_starts: dict[str, int]
) -> list[object]:
    if not paths:
        return []
    collection = bpy.data.collections.new("Level-5 P3 Lip Sync")
    bpy.context.scene.collection.children.link(collection)
    cursors: dict[str, float] = {}
    controllers = []
    for path in paths:
        suffix = path.stem.rsplit("_", 2)
        cut = f"c{int(suffix[-2]):04d}" if len(suffix) >= 3 and suffix[-2].isdigit() else ""
        start = cursors.get(cut, float(cut_starts.get(cut, 1)))
        controller = bpy.data.objects.new(path.stem, None)
        collection.objects.link(controller)
        controller.empty_display_type = "CIRCLE"
        controller.empty_display_size = 0.08
        controller.hide_render = True
        action, _, duration = create_p3lip_action(controller, path, start)
        controller.animation_data.action = action
        controller["g4_p3lip_cut"] = cut
        cursors[cut] = start + duration * (
            bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        ) + 1.0
        controllers.append(controller)
    return controllers


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


def import_event_effect_models(
    candidates: list[dict],
    motions: dict[str, dict],
    cut_starts: dict[str, int],
) -> list[object]:
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
        end_frame = min(following) if following else cut_starts[cut] + 1
        for effect_index, candidate in enumerate(cut_candidates, 1):
            model_path = Path(candidate["model"])
            before = set(bpy.data.objects)
            try:
                result = bpy.ops.import_scene.level5_g4(
                    filepath=str(model_path),
                    create_report_text=False,
                    import_character_parts=False,
                )
            except RuntimeError as exc:
                for obj in set(bpy.data.objects) - before:
                    bpy.data.objects.remove(obj, do_unlink=True)
                failed_models.append({"model": str(model_path), "error": str(exc)})
                continue
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
            root["g4_event_effect_model"] = str(model_path)
            root["g4_event_effect_cut"] = cut
            root["g4_event_effect_particle"] = candidate.get("particle", "")
            root["g4_event_effect_shaders"] = json.dumps(candidate.get("shaders") or [])
            motion = motions.get(cut)
            target = f"evp_eff{effect_index:02d}"
            if motion is not None:
                action = bpy.data.actions.new(f"{candidate['name']}_{cut}_Placement")
                if append_event_placement(action, root, motion, target):
                    track = root.animation_data_create().nla_tracks.new()
                    track.name = "Event Effect Placement"
                    duration = max(1, end_frame - cut_starts[cut])
                    add_nla_strip(root, track, action, cut, cut_starts[cut], duration)
            root.hide_viewport = True
            root.hide_render = True
            root.keyframe_insert("hide_viewport", frame=max(1, cut_starts[cut] - 1))
            root.keyframe_insert("hide_render", frame=max(1, cut_starts[cut] - 1))
            root.hide_viewport = False
            root.hide_render = False
            root.keyframe_insert("hide_viewport", frame=cut_starts[cut])
            root.keyframe_insert("hide_render", frame=cut_starts[cut])
            root.hide_viewport = True
            root.hide_render = True
            root.keyframe_insert("hide_viewport", frame=end_frame)
            root.keyframe_insert("hide_render", frame=end_frame)
            imported_roots.append(root)
    if failed_models:
        bpy.context.scene["g4_event_effect_failures"] = json.dumps(failed_models)
    return imported_roots


def decode_event_point_motions(
    directory: Path,
    prefs,
    temporary_directory: Path,
) -> dict[str, dict]:
    skeleton = resolve_event_point_skeleton(directory, prefs)
    if skeleton is None:
        return {}
    motions = {}
    prefix = f"{directory.name}_point_s00_"
    for package in sorted(directory.glob(f"{prefix}*.g4pk")):
        cut = event_cut_name(package)
        if cut is None:
            continue
        motion, _ = decode_event_package(package, skeleton, temporary_directory)
        motions[cut] = motion
    return motions


SOURCE_TO_BLENDER = Matrix(
    (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, -1.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
)


def import_event_character_lighting(directory: Path, cut_starts: dict[str, int]):
    light_directory = directory / f"{directory.name}_light"
    if not light_directory.is_dir():
        return None
    keyed = []
    for path in sorted(light_directory.glob("EventMap_fix_c*.cfg.bin")):
        cut_match = re.search(r"_(c\d+)\.cfg\.bin$", path.name, re.IGNORECASE)
        cut = cut_match.group(1).lower() if cut_match else None
        if cut not in cut_starts:
            continue
        try:
            parameters = event_light_parameters(path)
        except (OSError, ValueError, struct.error):
            continue
        if parameters:
            keyed.append((cut, cut_starts[cut], parameters))
    if not keyed:
        return None

    scene = bpy.context.scene
    if scene.world is None:
        scene.world = bpy.data.worlds.new(f"{directory.name} World")
    light_data = bpy.data.lights.new(f"{directory.name} Character Light", "SUN")
    light_object = bpy.data.objects.new(light_data.name, light_data)
    scene.collection.objects.link(light_object)
    for cut, frame, parameters in keyed:
        direction = parameters.get("charaLightDir")
        if direction and len(direction) >= 3:
            vector = SOURCE_TO_BLENDER.to_3x3() @ Vector(direction[:3])
            if vector.length_squared > 1e-8:
                light_object.rotation_euler = vector.normalized().to_track_quat("-Z", "Y").to_euler()
                light_object.keyframe_insert("rotation_euler", frame=frame)
        highlight = parameters.get("charaHighLightColor")
        if highlight and len(highlight) >= 3:
            light_data.color = tuple(max(0.0, value) for value in highlight[:3])
            light_data.energy = max(0.0, highlight[3] if len(highlight) > 3 else 1.0)
            light_data.keyframe_insert("color", frame=frame)
            light_data.keyframe_insert("energy", frame=frame)
        ambient = parameters.get("charaAmbient")
        if ambient and len(ambient) >= 3:
            scene.world.color = tuple(max(0.0, value) for value in ambient[:3])
            scene.world.keyframe_insert("color", frame=frame)
    for owner in (light_object, light_data, scene.world):
        animation = owner.animation_data
        if animation and animation.action:
            for curve in animation.action.fcurves:
                for point in curve.keyframe_points:
                    point.interpolation = "CONSTANT"

    animated_materials = 0
    for material in bpy.data.materials:
        if material.node_tree is None or not material.get("g4_level5_toon"):
            continue
        highlight_node = material.node_tree.nodes.get("G4 Highlight")
        underlight_node = material.node_tree.nodes.get("G4 Under Light")
        primary_shadow = material.node_tree.nodes.get("G4 Shadow Color 0")
        secondary_shadow = material.node_tree.nodes.get("G4 Shadow Color 1")
        shadow_blend = material.node_tree.nodes.get("G4 Dual Toon Ramp")
        under_rim_width = material.node_tree.nodes.get("G4 Under Rim Width")
        under_rim_strength = material.node_tree.nodes.get("G4 Under Rim Strength")
        keyed_material = False
        for _, frame, parameters in keyed:
            for node, parameter_name in (
                (highlight_node, "charaHighLightColor"),
                (underlight_node, "charaUnderRimColor"),
            ):
                values = parameters.get(parameter_name)
                if node is None or not values or len(values) < 3:
                    continue
                intensity = values[3] if len(values) > 3 else 1.0
                node.inputs[2].default_value = tuple(
                    max(0.0, value * intensity) for value in values[:3]
                ) + (1.0,)
                node.inputs[2].keyframe_insert("default_value", frame=frame)
                keyed_material = True
            high_threshold = parameters.get("charaHighThreshold")
            if high_threshold and primary_shadow is not None and secondary_shadow is not None:
                # The game expresses this threshold around 1.0; Blender's ramps
                # operate in 0..1, so mirror it around 2.0.
                position = max(0.02, min(0.98, 2.0 - high_threshold[0]))
                primary_shadow.color_ramp.elements[-1].position = position
                secondary_shadow.color_ramp.elements[-1].position = min(0.98, position + 0.14)
                primary_shadow.color_ramp.elements[-1].keyframe_insert("position", frame=frame)
                secondary_shadow.color_ramp.elements[-1].keyframe_insert("position", frame=frame)
                keyed_material = True
            for node, parameter_name in (
                (primary_shadow, "charaShadowRate1"),
                (secondary_shadow, "charaShadowRate2"),
            ):
                values = parameters.get(parameter_name)
                if node is None or not values:
                    continue
                shade = max(0.0, min(1.0, values[0]))
                node.color_ramp.elements[0].color = (shade, shade, shade, 1.0)
                node.color_ramp.elements[0].keyframe_insert("color", frame=frame)
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
                for curve in animation.action.fcurves:
                    for point in curve.keyframe_points:
                        point.interpolation = "CONSTANT"
    scene["g4_event_animated_materials"] = animated_materials
    scene["g4_event_light_parameters"] = json.dumps(
        {cut: parameters for cut, _, parameters in keyed}, sort_keys=True
    )
    return light_object


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
) -> int:
    if motion is None or not target_name:
        return 0
    matrices = point_global_samples(motion, target_name)
    if not matrices:
        return 0
    frames = [1 + frame - motion["clip"]["start_frame"] for frame in motion["frames"]]
    locations = []
    rotations = []
    scales = []
    previous_rotation = None
    base_matrix = model_base_matrix or Matrix.Identity(4)
    for matrix in matrices:
        matrix = matrix @ base_matrix
        location, rotation, scale = matrix.decompose()
        if previous_rotation is not None and previous_rotation.dot(rotation) < 0.0:
            rotation.negate()
        previous_rotation = rotation.copy()
        locations.append(tuple(location))
        rotations.append(tuple(rotation))
        scales.append(tuple(scale))
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
            )
    action["g4_event_point"] = target_name
    return len(matrices)


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
    point_motions: dict[str, dict] | None = None,
    head_model: str = "",
    body_model: str = "",
    shoes_model: str = "",
    manifest_model: str = "",
) -> tuple[object, int, list[tuple[str, int]]]:
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

    if model_path is None and generic_actor:
        model_path = resolve_manifest()
    if model_path is None:
        model_path = resolve_model_path(packages[0], getattr(prefs, "raw_data_root", ""))
    if model_path is None:
        model_path = resolve_manifest()
    if model_path is None:
        raise RuntimeError(f"Could not resolve model {actor} for the event")
    skeleton_path = resolve_skeleton_path(model_path)
    if skeleton_path is None and generic_actor:
        skeleton_path = resolve_generic_event_skeleton(packages[0], actor, prefs)
    first_motion, first_entry = decode_event_package(packages[0], skeleton_path, temporary_directory)
    armature, resolved_model = import_model_for_animation(
        packages[0],
        first_motion,
        prefs,
        model_path,
        bool(body_model or shoes_model),
        False,
        body_model,
        shoes_model,
        character_part_stem=character_part_stem,
        align_to_motion_rest=not (generic_actor and bool(head_model)),
    )
    display_actor = actor
    if generic_actor and head_model:
        display_actor = f"{model_path.stem}_{event_actor_slot(actor)}"
    armature.name = display_actor
    armature.data.name = f"{display_actor}_Armature"
    model_base_matrix = armature.matrix_world.copy()
    armature.animation_data_create()
    armature.animation_data.action = None
    track = armature.animation_data.nla_tracks.new()
    track.name = "G4 Event Cuts"
    clear_pose(armature)

    keyed_bones = 0
    placement_samples = 0
    cut_frames = []
    for index, package in enumerate(packages):
        if index == 0:
            motion, entry_name = first_motion, first_entry
        else:
            motion, entry_name = decode_event_package(package, skeleton_path, temporary_directory)
        resolve_track_names_from_armature(motion, armature)
        clip = motion["clip"]
        action, keyed = create_action(armature, motion, clip["start_frame"])
        substituted_generic = generic_actor and bool(head_model)
        if substituted_generic:
            remove_nonroot_translation_curves(action, armature)
        cut = clip["name"] or event_cut_name(package) or f"cut_{index:03d}"
        placement_samples += append_event_placement(
            action,
            armature,
            (point_motions or {}).get(cut),
            point_target,
            model_base_matrix,
        )
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
            rotation_only_retarget=substituted_generic,
        )
        cut_frames.append((cut, strip_start))
        keyed_bones += keyed
        progress()
    armature.animation_data.action = None
    if placement_samples:
        armature.scale = (0.0, 0.0, 0.0)
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
            for curve in animation.action.fcurves:
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
        progress()
    if camera_object is None:
        raise RuntimeError(f"No camera clips found in {path}")
    camera_object["g4cm_source"] = str(path)
    camera_object["g4_source_frame_origin"] = frame_origin
    return camera_object, len(clips)


def invoke_event_parts_dialog(directory: Path, import_camera: bool, actors: list[str]) -> None:
    defer_blender_call(
        lambda: bpy.ops.import_scene.level5_g4_event_parts(
            "INVOKE_DEFAULT",
            directory=str(directory),
            import_camera=import_camera,
            actors_json=json.dumps(actors),
        )
    )


EVENT_PART_OVERRIDES = {
    "ev72_50010": {
        "c11010019": {"body": "u11010018", "shoes": "s11010018"},
        "c11010069": {"body": "u117692", "shoes": "s117691"},
    },
}


def event_part_defaults(directory: Path, actor: str, prefs) -> dict[str, str]:
    actor_id = event_actor_base_id(actor)
    match = re.fullmatch(r"c(\d{6,8})", actor_id, re.IGNORECASE)
    if match is None:
        return {}
    actor_overrides = EVENT_PART_OVERRIDES.get(directory.name, {}).get(actor_id, {})
    result = {}
    for data_root in candidate_data_roots(directory, getattr(prefs, "raw_data_root", "")):
        uniform_root = data_root / "common" / "chr" / "_uniform"
        for key, prefix in (("body", "u"), ("shoes", "s")):
            stem = actor_overrides.get(key, f"{prefix}{match.group(1)}")
            for extension in (".g4pkm", ".g4md"):
                candidate = uniform_root / stem / f"{stem}{extension}"
                if candidate.is_file():
                    result[key] = str(candidate)
                    break
            if key not in result and uniform_root.is_dir():
                matches = sorted(uniform_root.glob(f"*/{stem}.g4m[dm]"))
                if matches:
                    result[key] = str(matches[0])
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


class G4EventCharacterPart(PropertyGroup):
    actor_id: StringProperty()
    actor_ids: StringProperty()
    head_model: StringProperty(name="Head", subtype="FILE_PATH")
    body_model: StringProperty(name="Body", subtype="FILE_PATH")
    shoes_model: StringProperty(name="Shoes", subtype="FILE_PATH")


class IMPORT_OT_level5_g4_event_parts(Operator):
    bl_idname = "import_scene.level5_g4_event_parts"
    bl_label = "Event Character Parts"

    directory: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    import_camera: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})
    actors_json: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    parts: CollectionProperty(type=G4EventCharacterPart, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        prefs = addon_preferences()
        try:
            saved = json.loads(getattr(prefs, "event_character_parts", "{}") or "{}")
        except json.JSONDecodeError:
            saved = {}
        directory = Path(bpy.path.abspath(self.directory))
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
            item.head_model = generic_saved.get("head", "")
            item.body_model = generic_saved.get("body", "")
            item.shoes_model = generic_saved.get("shoes", "")
        for actor in actors:
            if actor in generic_actors:
                continue
            item = self.parts.add()
            item.actor_id = actor
            item.actor_ids = json.dumps([actor])
            actor_parts = dict(saved.get(actor) or {})
            defaults = event_part_defaults(directory, actor, prefs)
            for key, value in defaults.items():
                if not actor_parts.get(key) or key in EVENT_PART_OVERRIDES.get(directory.name, {}).get(actor, {}):
                    actor_parts[key] = value
            item.head_model = actor_parts.get("head", "")
            item.body_model = actor_parts.get("body", "")
            item.shoes_model = actor_parts.get("shoes", "")
        return context.window_manager.invoke_props_dialog(self, width=820)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Assign optional models. Empty Head uses the model encoded by the event.")
        for item in self.parts:
            box = layout.box()
            box.label(text=item.actor_id, icon="ARMATURE_DATA")
            actor_ids = json.loads(item.actor_ids or "[]")
            if len(actor_ids) > 1:
                box.label(text=f"Chooses one of {len(actor_ids)} compatible skeleton variants")
            box.prop(item, "head_model")
            box.prop(item, "body_model")
            box.prop(item, "shoes_model")

    def execute(self, context):
        selected = {}
        for item in self.parts:
            actor_parts = {
                "head": bpy.path.abspath(item.head_model) if item.head_model else "",
                "body": bpy.path.abspath(item.body_model) if item.body_model else "",
                "shoes": bpy.path.abspath(item.shoes_model) if item.shoes_model else "",
            }
            for path_value in actor_parts.values():
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
        defer_blender_call(
            lambda: bpy.ops.import_scene.level5_g4_event_folder(
                "EXEC_DEFAULT",
                directory=directory,
                import_camera=import_camera,
                prompt_character_parts=False,
                character_parts_json=character_parts_json,
            )
        )
        return {"FINISHED"}


class IMPORT_OT_level5_g4_event_folder(Operator):
    bl_idname = "import_scene.level5_g4_event_folder"
    bl_label = "Import Level-5 G4 Event Folder"
    bl_options = {"REGISTER", "UNDO"}

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
        layout = self.layout
        layout.prop(self, "import_camera")
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
            invoke_event_parts_dialog(directory, self.import_camera, character_actors)
            return {"FINISHED"}
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
        window_manager.progress_begin(0, max(1, total_steps))

        def progress():
            nonlocal completed_steps
            completed_steps += 1
            window_manager.progress_update(completed_steps)

        prefs = addon_preferences()
        actor_models = resolve_event_actor_models(directory, prefs)
        actor_points = resolve_event_actor_points(directory, prefs)
        effect_candidates = discover_event_effects(directory, prefs)
        p3lip_paths = discover_event_p3lip(directory, prefs)
        actors = []
        skipped_actors = {}
        cut_frames = {}
        camera_object = None
        try:
            with tempfile.TemporaryDirectory(prefix="level5_g4_event_") as temporary:
                temporary_directory = Path(temporary)
                frame_origin, frame_end, cut_starts = event_timeline_layout(
                    packages,
                    temporary_directory,
                    camera_paths[0] if camera_paths else None,
                )
                point_motions = decode_event_point_motions(directory, prefs, temporary_directory)
                effect_motions = decode_event_effect_motions(directory, prefs, temporary_directory)
                for actor, actor_packages in sorted(packages.items()):
                    actor_parts = character_parts.get(actor) or {}
                    if actor_parts.get("skip"):
                        for _ in actor_packages:
                            progress()
                        continue
                    actor_base = event_actor_base_id(actor)
                    try:
                        armature, _, actor_cut_frames = import_event_actor(
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
                            point_target=actor_points.get(actor, actor_points.get(actor_base, "")),
                            point_motions=point_motions,
                            head_model=actor_parts.get("head", ""),
                            body_model=actor_parts.get("body", ""),
                            shoes_model=actor_parts.get("shoes", ""),
                            manifest_model=actor_models.get(actor, actor_models.get(actor_base, "")),
                        )
                    except Exception as exc:
                        skipped_actors[actor] = str(exc)
                        for _ in actor_packages:
                            progress()
                        continue
                    actors.append(armature)
                    cut_frames.update(actor_cut_frames)
                import_event_character_lighting(directory, cut_starts)
                effect_roots = import_event_effect_models(effect_candidates, effect_motions, cut_starts)
                p3lip_controllers = import_event_p3lip_controllers(p3lip_paths, cut_starts)
                if camera_paths:
                    camera_object, _ = import_event_camera(
                        camera_paths[0],
                        context.collection,
                        frame_origin,
                        cut_starts,
                        progress,
                    )

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
            scene["g4_event_effect_candidates"] = json.dumps(effect_candidates, sort_keys=True)
            scene["g4_event_effect_count"] = len(effect_roots)
            scene["g4_event_p3lip_count"] = len(p3lip_controllers)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            window_manager.progress_end()

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
            message += f"; skipped {len(skipped_actors)} unresolved actors"
        self.report({"WARNING" if skipped_actors else "INFO"}, message)
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


def p3lip_weight(packed_viseme: int) -> float:
    """Decode the normalized articulation envelope stored above the viseme byte."""
    envelope = (packed_viseme >> 16) & 0xFFFF
    neutral = 0x2CCC
    maximum = 0x6666
    return max(0.0, min(1.0, (envelope - neutral) / (maximum - neutral)))


def create_p3lip_action(target, path: Path, frame_start: float = 1.0):
    sequence = read_p3lip(path)
    target["g4_lip_viseme"] = 0
    target["g4_lip_weight"] = 0.0
    target["g4_p3lip_source"] = str(path)
    target.animation_data_create()
    previous_action = target.animation_data.action
    action = bpy.data.actions.new(f"{path.stem} Lip Sync")
    action.use_fake_user = True
    action["g4_p3lip_source"] = str(path)
    action["g4_p3lip_duration"] = sequence.duration
    target.animation_data.action = action
    fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
    keyed = 0
    for key in sequence.keys:
        viseme = key.packed_viseme & 0xFF
        if viseme in {0xFE, 0xFF}:
            viseme = 0
            weight = 0.0
        else:
            weight = p3lip_weight(key.packed_viseme)
        frame = frame_start + key.time * fps
        target["g4_lip_viseme"] = viseme
        target["g4_lip_weight"] = weight
        target.keyframe_insert('["g4_lip_viseme"]', frame=frame, group="P3 Lip Sync")
        target.keyframe_insert('["g4_lip_weight"]', frame=frame, group="P3 Lip Sync")
        keyed += 1
    for curve in action.fcurves:
        for point in curve.keyframe_points:
            point.interpolation = "CONSTANT" if "viseme" in curve.data_path else "LINEAR"
    target.animation_data.action = previous_action
    return action, keyed, sequence.duration


class IMPORT_OT_level5_p3lip(Operator, ImportHelper):
    bl_idname = "import_scene.level5_p3lip"
    bl_label = "Import Level-5 P3 Lip Sync"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".p3lip"
    filter_glob: StringProperty(default="*.p3lip", options={"HIDDEN"})

    def execute(self, context):
        path = Path(self.filepath)
        target = context.active_object
        if target is None:
            target = bpy.data.objects.new(f"{path.stem} Lip Sync", None)
            context.collection.objects.link(target)
            target.empty_display_type = "CIRCLE"
            target.empty_display_size = 0.25
        try:
            action, keyed, duration = create_p3lip_action(
                target, path, float(context.scene.frame_current)
            )
        except (OSError, ValueError, struct.error) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        target.animation_data.action = action
        context.scene.frame_end = max(
            context.scene.frame_end,
            int(context.scene.frame_current + duration * context.scene.render.fps),
        )
        self.report({"INFO"}, f"Imported {keyed} P3LIP keys on {target.name}")
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
    self.layout.operator(IMPORT_OT_level5_p3lip.bl_idname, text="Level-5 P3 Lip Sync (.p3lip)")
    self.layout.operator(IMPORT_OT_level5_g4_event_folder.bl_idname, text="Level-5 G4 Event Folder")


def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_level5_g4_fbx.bl_idname, text="Level-5 G4 Scene (.fbx)")


classes = [
    G4EventCharacterPart,
    IMPORT_OT_level5_g4mt_pick_model,
    IMPORT_OT_level5_g4mt_pick_body,
    IMPORT_OT_level5_g4mt_pick_shoes,
    IMPORT_OT_level5_g4mt,
    IMPORT_OT_level5_g4cm,
    IMPORT_OT_level5_p3lip,
    IMPORT_OT_level5_g4_event_parts,
    IMPORT_OT_level5_g4_event_folder,
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

    class P3LIP_FH_import(bpy.types.FileHandler):
        bl_idname = "P3LIP_FH_import"
        bl_label = "Level-5 P3 Lip Sync"
        bl_import_operator = IMPORT_OT_level5_p3lip.bl_idname
        bl_file_extensions = ".p3lip"

        @classmethod
        def poll_drop(cls, context):
            return context.area is not None and context.area.type in {"VIEW_3D", "OUTLINER", "FILE_BROWSER"}

    classes.append(P3LIP_FH_import)


def register():
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
