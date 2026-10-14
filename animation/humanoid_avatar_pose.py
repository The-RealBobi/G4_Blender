"""Reference-pose calculations for animating a model's original armature."""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence

from bpy.types import Action, Context, Object
from mathutils import Matrix, Quaternion, Vector


def avatar_export_alignment(rig: Object | None, native_skeleton: bytes | None) -> Matrix:
    profile = getattr(rig, 'level5_humanoid_avatar', None) if rig is not None else None
    if profile is None or not profile.has_representation:
        return Matrix.Identity(4)
    if not profile.mapping_valid or profile.mapping_dirty or profile.pose_stale:
        raise ValueError("Validate the humanoid Avatar mapping and rest pose before exporting")
    from .humanoid_avatar_addon import mapping_from_profile

    frame, height, _hip_height = body_frame(rig, mapping_from_profile(profile))
    scale = 1.0
    if native_skeleton is not None:
        from ..porting.g4_model_probe import parse_g4sk
        from ..formats.g4_axes import blender_from_g4_matrix

        skeleton = parse_g4sk(native_skeleton)
        names = {name: index for index, name in enumerate(skeleton['names'])}
        required = ('c_head_1_0', 'l_foot_1_0', 'r_foot_1_0')
        if all(name in names for name in required):
            points = {}
            for name in required:
                matrix = skeleton['bind_matrices'][names[name]]
                points[name] = blender_from_g4_matrix() @ Vector((matrix[3], matrix[7], matrix[11]))
            up = (points['c_head_1_0'] - (points['l_foot_1_0'] + points['r_foot_1_0']) * .5).length
            if up > 1e-6 and height > 1e-6:
                scale = up / height
    return Matrix.Scale(scale, 4) @ frame.inverted().to_matrix().to_4x4()


def twist_segments(rig: Object, mapping: Mapping[str, str]) -> dict[str, tuple[str, str, float]]:
    from .humanoid_avatar import _auxiliary_category

    segments: list[tuple[str, str]] = []
    for side in ('l', 'r'):
        for start, ends in ((f'{side}_a_1_0', (f'{side}_a_1_1', f'{side}_w_1_0')),
                            (f'{side}_a_1_1', (f'{side}_w_1_0',)),
                            (f'{side}_l_1_0', (f'{side}_l_1_1', f'{side}_foot_1_0')),
                            (f'{side}_l_1_1', (f'{side}_foot_1_0',))):
            end = next((key for key in ends if key in mapping), None)
            if start in mapping and end is not None:
                segments.append((mapping[start], mapping[end]))
    assignments: dict[str, tuple[str, str, float]] = {}
    for bone in sorted(rig.data.bones, key=lambda item: len(item.parent_recursive)):
        if bone.name in mapping.values() or _auxiliary_category(bone.name) != 'twist':
            continue
        candidates: list[tuple[float, int, str, str, float]] = []
        chain_vectors = [child.head_local - bone.head_local for child in bone.children
                         if _auxiliary_category(child.name) == 'twist'
                         and (child.head_local - bone.head_local).length > 1e-6]
        direction = chain_vectors[0].normalized() if chain_vectors else None
        parent_segment = assignments.get(bone.parent.name) if bone.parent else None
        candidates_segments = [(parent_segment[0], parent_segment[1])] if parent_segment else segments
        for start, end in candidates_segments:
            driver = rig.data.bones[start]
            if bone in driver.parent_recursive:
                continue
            vector = rig.data.bones[end].head_local - driver.head_local
            if vector.length < 1e-6:
                continue
            fraction = (bone.head_local - driver.head_local).dot(vector) / vector.length_squared
            distance = (bone.head_local - (driver.head_local + vector * fraction)).length / vector.length
            if (-.05 <= fraction <= 1.05 and distance < .15
                    and (direction is None or abs(direction.dot(vector.normalized())) > .8)):
                fraction = 0. if fraction < .001 else 1. if fraction > .999 else fraction
                candidates.append((distance, int(fraction == 1.), start, end, fraction))
        if candidates:
            _, _, start, end, fraction = min(candidates)
            assignments[bone.name] = (start, end, fraction)
    return assignments


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
    from .g4_animation_addon import append_curve_samples

    if not frames:
        raise ValueError("Animation has no frames")
    from .humanoid_avatar import suggest_finger_mapping
    from .humanoid_avatar_addon import bone_nodes

    source_mapping = dict(source_mapping)
    target_mapping = dict(target_mapping)
    fingers = suggest_finger_mapping(bone_nodes(original_target), target_mapping)
    for joint, name in fingers.assignments.items():
        source_joint = joint
        if joint not in source_mapping and joint.split('_')[1] in {'mid', 'rng', 'pky'}:
            source_joint = joint.replace('_' + joint.split('_')[1] + '_', '_idx_')
        if source_joint in source_mapping:
            source_mapping.setdefault(joint, source_mapping[source_joint])
            target_mapping.setdefault(joint, name)
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
        twists = twist_segments(target, target_mapping)
        ordered = []
        pending = list(target.data.bones)
        resolved = set()
        while pending:
            ready = [bone for bone in pending
                     if (bone.parent is None or bone.parent.name in resolved)
                     and (bone.name not in twists or twists[bone.name][0] in resolved)]
            if not ready:
                raise ValueError("Twist segment assignments conflict with the armature hierarchy")
            ordered.extend(ready)
            resolved.update(bone.name for bone in ready)
            pending = [bone for bone in pending if bone.name not in resolved]
        action = bpy.data.actions.new(action_name)
        action.use_frame_range = True
        action.frame_start = frames[0]
        action.frame_end = frames[-1]
        target.animation_data_create()
        target.animation_data.action = action
        for bone in target.pose.bones:
            bone.matrix_basis = Matrix.Identity(4)
        source_root = source.pose.bones.get(source_mapping.get("c_global_0_0", ""))
        target_root = target_mapping.get('c_global_0_0')
        original_rotation_modes = {bone.name: bone.rotation_mode for bone in target.pose.bones}
        previous_rotations = {}
        expected_rotations = {}
        curve_samples = {}
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
                        position = target.matrix_world.inverted() @ (target_rest[bone.name].translation + root_displacement * body_scale)
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
                elif bone.name in twists:
                    driver_name, end_name, fraction = twists[bone.name]
                    driver_rest = target.data.bones[driver_name].matrix_local
                    relative_rest = driver_rest.inverted() @ bone.matrix_local
                    end_joint = target_to_native[end_name]
                    source_end = source.pose.bones[source_mapping[end_joint]]
                    end_delta = ((source.matrix_world @ source_end.matrix).to_quaternion()
                                 @ source_rest[source_end.name].to_quaternion().inverted())
                    end_rotation = (target.matrix_world.to_quaternion().inverted()
                                    @ alignment @ end_delta @ alignment.inverted()
                                    @ reference_rotations[end_name])
                    relative_rotation = desired[driver_name].to_quaternion().inverted() @ end_rotation
                    rest_rotation = driver_rest.to_quaternion().inverted() @ target.data.bones[end_name].matrix_local.to_quaternion()
                    delta = relative_rotation @ rest_rotation.inverted()
                    axis = (driver_rest.inverted().to_3x3()
                            @ (target.data.bones[end_name].head_local - target.data.bones[driver_name].head_local)).normalized()
                    projection = axis * Vector((delta.x, delta.y, delta.z)).dot(axis)
                    twist = Quaternion((delta.w, *projection))
                    if twist.magnitude < 1e-6:
                        twist = Quaternion()
                    else:
                        twist.normalize()
                    if twist.w < 0.:
                        twist.negate()
                    distributed = Quaternion().slerp(twist, fraction).to_matrix().to_4x4()
                    base = desired[driver_name] @ distributed @ relative_rest
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
                    curve_samples.setdefault((bone.name, data_path), []).append(tuple(getattr(pose, data_path)))
            if (frame_index + 1) % 4 == 0 or frame_index + 1 == len(frames):
                yield frame_index + 1, len(frames) * 2, f"Preparing animation: frame {frame_index + 1}/{len(frames)}"
            slots = getattr(action, 'slots', ())
            if slots:
                target.animation_data.action_slot = slots[0]
        curve_cache = {}
        for (name, data_path), values in curve_samples.items():
            pose = target.pose.bones[name]
            for component in range(len(values[0])):
                append_curve_samples(action, curve_cache, pose.path_from_id(data_path), component,
                                     name, frames, [value[component] for value in values], target)
        slots = getattr(action, 'slots', ())
        if slots:
            target.animation_data.action_slot = slots[0]
        for frame_index, frame in enumerate(frames):
            scene.frame_set(frame)
            context.view_layer.update()
            for name, expected in expected_rotations[frame].items():
                actual = (target.matrix_world @ target.pose.bones[name].matrix).to_quaternion()
                difference = actual.rotation_difference(Quaternion(expected))
                error = min(difference.angle, abs(2. * math.pi - difference.angle))
                if error > math.radians(.25):
                    raise ValueError(
                        f"Animation cannot reproduce bone {name} at frame {frame} "
                        f"(rotation error {math.degrees(error):.3f} degrees, mode "
                        f"{target.pose.bones[name].rotation_mode}); check its constraints and drivers"
                    )
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
