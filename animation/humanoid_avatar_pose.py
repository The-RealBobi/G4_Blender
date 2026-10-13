"""Reference-pose calculations for animating a model's original armature."""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence

from bpy.types import Action, Context, Object
from mathutils import Matrix, Quaternion, Vector


def body_frame(rig: Object, mapping: Mapping[str, str]) -> tuple[Quaternion, float, float]:
    point = lambda key: rig.matrix_world @ rig.data.bones[mapping[key]].head_local
    up = (point('c_head_1_0') - point('c_c_1_0')).normalized()
    left = point('l_l_1_0') - point('r_l_1_0')
    left = (left - up * left.dot(up)).normalized()
    forward = left.cross(up).normalized()
    up = forward.cross(left).normalized()
    basis = Matrix((forward, left, up)).transposed()
    ankle = (point('l_foot_1_0') + point('r_foot_1_0')) * .5
    return basis.to_quaternion(), (point('c_head_1_0') - ankle).dot(up), (point('c_c_1_0') - ankle).dot(up)


def reference_pose_rotations(
    source: Object,
    target: Object,
    source_mapping: Mapping[str, str],
    target_mapping: Mapping[str, str],
    alignment: Quaternion,
) -> tuple[dict[str, Quaternion], dict[str, Quaternion]]:
    pairs = {'c_c_1_0': 'c_c_1_1', 'c_c_1_1': 'c_n_1_0', 'c_n_1_0': 'c_head_1_0'}
    for prefix in ['l', 'r']:
        pairs.update({f'{prefix}_s_1_0': f'{prefix}_a_1_0', f'{prefix}_a_1_0': f'{prefix}_a_1_1',
                      f'{prefix}_a_1_1': f'{prefix}_w_1_0', f'{prefix}_l_1_0': f'{prefix}_l_1_1',
                      f'{prefix}_l_1_1': f'{prefix}_foot_1_0', f'{prefix}_foot_1_0': f'{prefix}_foot_1_1'})
        for finger in ['thb', 'idx', 'mid', 'rng', 'pky']:
            for segment in [0, 1]:
                pairs[f'{prefix}_{finger}_1_{segment}'] = f'{prefix}_{finger}_1_{segment + 1}'
        for finger in ['idx', 'mid', 'thb']:
            joint = f'{prefix}_{finger}_1_0'
            if joint in source_mapping and joint in target_mapping:
                pairs[f'{prefix}_w_1_0'] = joint
                break
    inverse_target = {name: joint for joint, name in target_mapping.items()}
    offsets = {}
    rotations = {}
    for bone in sorted(target.data.bones, key=lambda b: len(b.parent_recursive)):
        joint = inverse_target.get(bone.name)
        endpoint = pairs.get(joint)
        offset = offsets.get(bone.parent.name, Quaternion()) if bone.parent else Quaternion()
        if joint == 'c_global_0_0':
            offset = Quaternion()
        if endpoint in source_mapping and endpoint in target_mapping and joint in source_mapping:
            target_direction = target.matrix_world.to_3x3() @ (target.data.bones[target_mapping[endpoint]].head_local - bone.head_local)
            source_direction = source.matrix_world.to_3x3() @ (source.data.bones[source_mapping[endpoint]].head_local - source.data.bones[source_mapping[joint]].head_local)
            if target_direction.length > 1e-6 and source_direction.length > 1e-6:
                offset = target_direction.normalized().rotation_difference(alignment @ source_direction.normalized())
        offsets[bone.name] = offset
        rotations[bone.name] = offset @ (target.matrix_world @ bone.matrix_local).to_quaternion()
    return rotations, offsets



def animated_translation(
    source_pose_world: Matrix,
    source_parent_pose_world: Matrix,
    source_local_rest: Matrix,
    alignment: Quaternion,
    length_ratio: float,
    target_world: Matrix,
) -> Vector:
    expected = source_parent_pose_world @ source_local_rest
    displacement = source_pose_world.translation - expected.translation
    return target_world.inverted().to_3x3() @ (alignment @ displacement * length_ratio)


def bake_avatar_action(
    context: Context,
    source: Object,
    original_target: Object,
    source_mapping: Mapping[str, str],
    target_mapping: Mapping[str, str],
    frames: Sequence[int],
    action_name: str,
) -> Generator[tuple[int, int, str], None, Action]:
    """Prepare an Action on a temporary rig; closing the iterator cancels it."""
    import math
    import bpy
    from .g4_animation_addon import action_fcurves

    if not frames:
        raise ValueError("Animation has no frames")
    required = {"c_c_1_0", "c_head_1_0", "l_l_1_0", "r_l_1_0", "l_foot_1_0", "r_foot_1_0"}
    for label, rig, mapping in (("source", source, source_mapping), ("target", original_target, target_mapping)):
        missing = required - mapping.keys()
        if missing:
            raise ValueError(f"The {label} Avatar is missing body assignments: {', '.join(sorted(missing))}")
        for name in mapping.values():
            if name not in rig.data.bones:
                raise ValueError(f"The {label} Avatar refers to missing bone {name}")

    original_frame = context.scene.frame_current
    original_subframe = context.scene.frame_subframe
    target = original_target.copy()
    target.data = original_target.data.copy()
    target_data = target.data
    target.animation_data_clear()
    target.parent = None
    target.matrix_world = original_target.matrix_world.copy()
    target.data.pose_position = "POSE"
    context.scene.collection.objects.link(target)
    target.hide_render = True
    target.hide_select = True
    target.hide_set(True)
    action = None
    completed = False
    try:
        source_frame, source_height, source_hip_height = body_frame(source, source_mapping)
        target_frame, target_height, target_hip_height = body_frame(target, target_mapping)
        alignment = target_frame @ source_frame.inverted()
        body_scale = target_height / source_height
        hip_scale = target_hip_height / source_hip_height
        source_rest = {b.name: source.matrix_world @ b.matrix_local for b in source.data.bones}
        target_rest = {b.name: target.matrix_world @ b.matrix_local for b in target.data.bones}
        reference_rotations, _reference_offsets = reference_pose_rotations(source, target, source_mapping, target_mapping, alignment)
        target_to_native = {name: joint for joint, name in target_mapping.items() if joint in source_mapping}
        ordered = sorted(target.data.bones, key=lambda b: len(b.parent_recursive))
        action = bpy.data.actions.new(action_name)
        target.animation_data_create()
        target.animation_data.action = action
        for bone in target.pose.bones:
            bone.matrix_basis = Matrix.Identity(4)
        source_root = source.pose.bones.get(source_mapping.get("c_global_0_0", ""))
        target_root = target_mapping.get('c_global_0_0')
        original_rotation_modes = {bone.name: bone.rotation_mode for bone in target.pose.bones}
        previous_rotations = {}
        expected_rotations = {}
        scene = context.scene
        for frame_index, frame in enumerate(frames):
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            desired = {}
            expected_rotations[frame] = {}
            root_displacement = ((source.matrix_world @ source_root.head) - source_rest[source_root.name].translation) if source_root else Vector()
            for bone in ordered:
                pose = target.pose.bones[bone.name]
                kwargs = {}
                if bone.parent:
                    kwargs = {'parent_matrix': desired[bone.parent.name], 'parent_matrix_local': bone.parent.matrix_local}
                base = bone.convert_local_to_pose(Matrix.Identity(4), bone.matrix_local, **kwargs)
                joint = target_to_native.get(bone.name)
                if joint:
                    source_bone = source.pose.bones[source_mapping[joint]]
                    source_pose_world = source.matrix_world @ source_bone.matrix
                    delta = source_pose_world.to_quaternion() @ source_rest[source_bone.name].to_quaternion().inverted()
                    world_rotation = alignment @ delta @ alignment.inverted() @ reference_rotations[bone.name]
                    rotation = target.matrix_world.to_quaternion().inverted() @ world_rotation
                    position = base.translation.copy()
                    if joint == 'c_global_0_0':
                        position = target.matrix_world.inverted() @ (target_rest[bone.name].translation + alignment @ root_displacement * body_scale)
                    elif joint == 'c_c_1_0':
                        parent = source_bone.parent
                        source_local_rest = parent.bone.matrix_local.inverted() @ source_bone.bone.matrix_local if parent else source_bone.bone.matrix_local
                        expected = source.matrix_world @ (parent.matrix @ source_local_rest if parent else source_local_rest)
                        hip_displacement = source_pose_world.translation - expected.translation
                        displacement = alignment @ hip_displacement * hip_scale
                        if not target_root:
                            displacement += alignment @ root_displacement * body_scale
                        position += target.matrix_world.inverted().to_3x3() @ displacement
                    else:
                        parent = source_bone.parent
                        source_local_rest = parent.bone.matrix_local.inverted() @ source_bone.bone.matrix_local if parent else source_bone.bone.matrix_local
                        segment_scale = body_scale
                        parent_joint = next((key for key, name in source_mapping.items() if parent and name == parent.name), None)
                        if parent_joint in target_mapping:
                            source_length = (source_rest[source_bone.name].translation - source_rest[parent.name].translation).length
                            target_length = (target_rest[bone.name].translation - target_rest[target_mapping[parent_joint]].translation).length
                            if source_length > 1e-6:
                                segment_scale = target_length / source_length
                        parent_world = source.matrix_world @ parent.matrix if parent else source.matrix_world
                        position += animated_translation(source_pose_world, parent_world, source_local_rest,
                                                         alignment, segment_scale, target.matrix_world)
                    base = Matrix.LocRotScale(position, rotation, base.to_scale())
                    expected_rotations[frame][bone.name] = tuple(world_rotation)
                desired[bone.name] = base
                basis = bone.convert_local_to_pose(base, bone.matrix_local, **kwargs, invert=True)
                location, rotation, scale = basis.decompose()
                pose.location = location
                pose.scale = scale
                mode = original_rotation_modes[bone.name]
                if mode == 'QUATERNION':
                    if bone.name in previous_rotations and previous_rotations[bone.name].dot(rotation) < 0.:
                        rotation.negate()
                    pose.rotation_quaternion = rotation
                    previous_rotations[bone.name] = rotation.copy()
                    path = 'rotation_quaternion'
                elif mode == 'AXIS_ANGLE':
                    axis, angle = rotation.to_axis_angle()
                    pose.rotation_axis_angle = (angle, *axis)
                    path = 'rotation_axis_angle'
                else:
                    euler = rotation.to_euler(mode, previous_rotations[bone.name]) if bone.name in previous_rotations else rotation.to_euler(mode)
                    pose.rotation_euler = euler
                    previous_rotations[bone.name] = euler.copy()
                    path = 'rotation_euler'
                for data_path in ("location", path, "scale"):
                    pose.keyframe_insert(data_path=data_path, frame=frame, group=bone.name)
            if (frame_index + 1) % 4 == 0 or frame_index + 1 == len(frames):
                yield frame_index + 1, len(frames) * 2, f"Preparing animation: frame {frame_index + 1}/{len(frames)}"
            slots = getattr(action, 'slots', ())
            if slots:
                target.animation_data.action_slot = slots[0]
        for curve in action_fcurves(action):
            for key in curve.keyframe_points:
                key.interpolation = 'LINEAR'
        for frame_index, frame in enumerate(frames):
            scene.frame_set(frame)
            context.view_layer.update()
            for name, expected in expected_rotations[frame].items():
                actual = (target.matrix_world @ target.pose.bones[name].matrix).to_quaternion()
                difference = actual.rotation_difference(Quaternion(expected))
                error = min(difference.angle, abs(2. * math.pi - difference.angle))
                if error > math.radians(.1):
                    raise ValueError(f"Animation cannot reproduce bone {name} at frame {frame}; check its constraints and drivers")
            if (frame_index + 1) % 4 == 0 or frame_index + 1 == len(frames):
                yield len(frames) + frame_index + 1, len(frames) * 2, f"Checking animation: frame {frame_index + 1}/{len(frames)}"
        completed = True
        return action
    finally:
        context.scene.frame_set(original_frame, subframe=original_subframe)
        bpy.data.objects.remove(target, do_unlink=True)
        bpy.data.armatures.remove(target_data)
        if action is not None and not completed:
            bpy.data.actions.remove(action)
