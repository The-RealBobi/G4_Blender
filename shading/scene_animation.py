"""Keep imported material animation outside shader trees to avoid recompilation."""
import hashlib
import json

import bpy


def migrate_material_animation(
    scene: bpy.types.Scene,
    materials: set[bpy.types.Material],
    paths: set[str],
    interpolations: set[str],
) -> int:
    from ..g4_animation_addon import action_fcurves, action_fcurve_new

    if scene.library:
        return 0
    scene_animation = scene.animation_data
    if scene_animation and (
        scene_animation.nla_tracks or scene_animation.action_blend_type != "REPLACE"
        or scene_animation.action_influence != 1
    ):
        return 0
    # A shared material must not start reading properties absent from another scene.
    shared = {
        slot.material for other in bpy.data.scenes if other != scene
        for obj in other.objects for slot in obj.material_slots if slot.material
    }
    migrated = 0
    for material in materials - shared:
        tree = material.node_tree
        animation = tree.animation_data if tree else None
        if material.library or (tree and tree.library):
            continue
        if not animation or not animation.action:
            continue
        if (
            animation.drivers
            or animation.nla_tracks
            or animation.action_blend_type != "REPLACE"
            or animation.action_influence != 1
        ):
            continue
        curves = action_fcurves(animation.action)
        if len({(curve.data_path, curve.array_index) for curve in curves}) != len(curves):
            continue
        # User-edited interpolation, modifiers and unrelated animation stay untouched.
        if not curves or any(
            curve.data_path not in paths or curve.modifiers or curve.sampled_points
            or curve.mute or (curve.group and curve.group.mute)
            or curve.extrapolation != "CONSTANT" or not curve.keyframe_points
            or any(point.interpolation not in interpolations for point in curve.keyframe_points)
            for curve in curves
        ):
            continue
        grouped = {}
        for curve in curves:
            grouped.setdefault(curve.data_path, []).append(curve)
        for path, channels in grouped.items():
            socket = tree.path_resolve(path.rsplit(".", 1)[0])
            value = socket.default_value
            is_vector = socket.type in ("RGBA", "VECTOR")
            initial = list(value) if is_vector else value
            signature = json.dumps((path, initial, [
                (
                    curve.array_index,
                    [(tuple(point.co), point.interpolation) for point in curve.keyframe_points],
                )
                for curve in sorted(channels, key=lambda curve: curve.array_index)
            ]))
            key = "g4_material_" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
            if key not in scene:
                scene[key] = initial
                scene_animation = scene.animation_data_create()
                if scene_animation.action is None:
                    scene_animation.action = bpy.data.actions.new("Scene material controls")
                for curve in channels:
                    target = action_fcurve_new(
                        scene_animation.action,
                        scene,
                        f'["{key}"]',
                        curve.array_index,
                        "Material controls",
                    )
                    target.keyframe_points.add(len(curve.keyframe_points))
                    for source, point in zip(curve.keyframe_points, target.keyframe_points):
                        point.co = source.co
                        point.interpolation = source.interpolation
                    target.update()
            attribute = next(
                (
                    node for node in tree.nodes
                    if node.get(
                        "g4_scene_animation_socket",
                        node.get("g4_event_lighting_socket"),
                    ) == path
                ),
                None,
            )
            if attribute is None:
                attribute = tree.nodes.new("ShaderNodeAttribute")
                attribute.name = "G4 Event " + socket.node.name
                attribute["g4_scene_animation_socket"] = path
                attribute.attribute_type = "VIEW_LAYER"
                output_name = {"RGBA": "Color", "VECTOR": "Vector"}.get(socket.type, "Fac")
                output = attribute.outputs[output_name]
                if socket.is_output:
                    for link in tuple(socket.links):
                        tree.links.new(output, link.to_socket)
                else:
                    tree.links.new(output, socket)
            attribute.attribute_name = key
        tree.animation_data_clear()
        migrated += 1
    if migrated:
        # Direct F-Curve construction needs a relation refresh for new RNA paths.
        animation = scene.animation_data
        action = animation.action
        animation.action = None
        animation.action = action
        scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)
    return migrated
