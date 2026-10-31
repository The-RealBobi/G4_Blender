"""Serialize the legacy Avatar baker's static calculations for VictoryTool."""

from __future__ import annotations

import base64
import math
from pathlib import Path

import bpy
from mathutils import Matrix

from ..formats.g4_axes import blender_from_g4_matrix
from .g4_animation_addon import (
    create_motion_armature, create_victorytool_target_armature,
    primary_avatar_target_mapping, native_bind_correction,
)
from .humanoid_avatar import suggest_finger_mapping
from .humanoid_avatar_addon import bone_nodes
from .humanoid_avatar_pose import body_frame, reference_pose_rotations, twist_segments
from .g4mt_motion import parse_skeleton


def quaternion_xyzw(rotation) -> list[float]:
    rotation.normalize()
    return [float(rotation.x), float(rotation.y), float(rotation.z), float(rotation.w)]


def matrix_transform(matrix: Matrix) -> dict:
    translation, rotation, scale = matrix.decompose()
    return {"translation": list(translation), "rotation": quaternion_xyzw(rotation), "scale": list(scale)}


def build_avatar_recipe(
    source_path: Path, target_path: Path, model_path: Path,
    target_joint_mapping: dict[str, str], chr_family: str, event_skeleton: str,
    export_chr: bool, export_event: bool,
) -> dict:
    conversion = blender_from_g4_matrix()
    inverse = conversion.inverted()
    source_data = source_path.read_bytes()
    target_data = target_path.read_bytes()
    parsed_source = parse_skeleton(source_path)
    parsed_target = parse_skeleton(target_path)
    source_indices = {name: index for index, name in enumerate(parsed_source["names"])}
    selected = tuple(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    mode = active.mode if active is not None else "OBJECT"
    rigs = []
    try:
        if mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        source = create_motion_armature({"skeleton": parsed_source})
        rigs.append(source)
        target = create_victorytool_target_armature(target_path, model_path, target_joint_mapping)
        rigs.append(target)
        source_mapping = {bone.name: bone.name for bone in source.data.bones}
        parents = {bone.name: bone.parent.name if bone.parent else None for bone in target.data.bones}
        target_mapping = primary_avatar_target_mapping(target_joint_mapping, parents, set(source_mapping))
        fingers = suggest_finger_mapping(bone_nodes(target), target_mapping)
        for joint, name in fingers.assignments.items():
            source_joint = joint
            if joint not in source_mapping and joint.split('_')[1] in {'mid', 'rng', 'pky'}:
                source_joint = joint.replace('_' + joint.split('_')[1] + '_', '_idx_')
            if source_joint in source_mapping:
                source_mapping.setdefault(joint, source_mapping[source_joint])
                target_mapping.setdefault(joint, name)
        required = {'c_c_1_0', 'c_head_1_0', 'l_l_1_0', 'r_l_1_0', 'l_foot_1_0', 'r_foot_1_0'}
        for label, rig, mapping in (("source", source, source_mapping), ("target", target, target_mapping)):
            missing = required - mapping.keys()
            if missing:
                raise ValueError(f"The {label} Avatar is missing body assignments: {', '.join(sorted(missing))}")
            if any(name not in rig.data.bones for name in mapping.values()):
                raise ValueError(f"The {label} Avatar refers to missing bones")
        source_frame, source_height, source_hip = body_frame(source, source_mapping)
        target_frame, target_height, target_hip = body_frame(target, target_mapping)
        if abs(source_height) < 1e-6 or abs(source_hip) < 1e-6:
            raise ValueError("The source Avatar has a degenerate body or hip height")
        alignment = target_frame @ source_frame.inverted()
        body_scale = target_height / source_height
        references, _offsets = reference_pose_rotations(source, target, source_mapping, target_mapping, alignment)
        segment_scales = {}
        for joint, name in target_mapping.items():
            if joint not in source_mapping:
                continue
            bone = source.data.bones[source_mapping[joint]]
            parent_joint = next((key for key, mapped in source_mapping.items() if bone.parent and mapped == bone.parent.name), None)
            scale = body_scale
            if parent_joint in target_mapping:
                source_length = (bone.head_local - bone.parent.head_local).length
                target_length = (target.data.bones[name].head_local - target.data.bones[target_mapping[parent_joint]].head_local).length
                if source_length > 1e-6:
                    scale = target_length / source_length
            segment_scales[name] = float(scale)

        def nodes(rig, track_rest: bool) -> list[dict]:
            result = []
            rests = {}
            # The external consumer requires exact embedded-skeleton node order.
            names = parsed_source['names'] if track_rest else parsed_target['names']
            for name in names:
                bone = rig.data.bones[name]
                rest = bone.matrix_local
                if not track_rest:
                    correction = native_bind_correction(bone)
                    if correction is not None:
                        scale = sum(correction[index][index] for index in range(3)) / 3.0
                        expected = Matrix.Diagonal((scale, scale, scale, 1.0))
                        if (not math.isfinite(scale) or scale <= 0
                                or any(not math.isfinite(correction[row][column])
                                       or abs(correction[row][column] - expected[row][column]) > 1e-5 * max(1.0, abs(scale))
                                       for row in range(4) for column in range(4))):
                            raise ValueError(
                                f"The humanoid-avatar-v1 recipe cannot represent the native bind correction "
                                f"for bone {bone.name!r}; only positive uniform scale with unchanged axes and origin is supported"
                            )
                        # The v1 baker inherits native scale while applying absolute
                        # Avatar rotations. Uniform bind corrections commute with them.
                        rest = rest @ correction
                rests[bone.name] = rest
                local = rests[bone.parent.name].inverted() @ rest if bone.parent else rest
                item = {
                    "name": bone.name, "parent": bone.parent.name if bone.parent else None,
                    "restMatrix": [float(rest[row][column]) for row in range(4) for column in range(4)],
                    "localRest": matrix_transform(local),
                }
                if track_rest:
                    index = source_indices[bone.name]
                    item["trackRest"] = parsed_source['local_trs'][index]
                result.append(item)
            return result

        return {
            "version": 1, "algorithm": "humanoid-avatar-v1",
            "exportChr": bool(export_chr), "exportEvent": bool(export_event),
            "chrFamily": chr_family, "eventSkeleton": event_skeleton,
            "sourceSkeletonBase64": base64.b64encode(source_data).decode('ascii'),
            "targetSkeletonBase64": base64.b64encode(target_data).decode('ascii'),
            "sourceNodes": nodes(source, True), "targetNodes": nodes(target, False),
            "sourceMapping": source_mapping, "targetMapping": target_mapping,
            "alignment": quaternion_xyzw((inverse @ alignment.to_matrix().to_4x4() @ conversion).to_quaternion()),
            "referenceRotations": {
                name: quaternion_xyzw((inverse @ rotation.to_matrix().to_4x4()).to_quaternion())
                for name, rotation in references.items()
            },
            "bodyScale": float(body_scale), "hipScale": float(target_hip / source_hip),
            "segmentScales": segment_scales,
            "twists": [{"bone": name, "driver": driver, "end": end, "fraction": float(fraction)}
                       for name, (driver, end, fraction) in twist_segments(target, target_mapping).items()],
            "sourceScalePolicy": "parent-track-ratio", "targetCumulativeScale": True,
            "preserveLocalTracks": ["c_ball_1_0"],
        }
    finally:
        if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for rig in reversed(rigs):
            data = rig.data
            bpy.data.objects.remove(rig, do_unlink=True)
            bpy.data.armatures.remove(data)
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        for obj in selected:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
        if active is not None and mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=mode)
