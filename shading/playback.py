"""Lightweight character material output while the timeline is playing."""

import bpy
from bpy.app.handlers import persistent


ADDON_ID = ""
FULL_OUTPUT = "G4 Full Output"
PLAYBACK_OUTPUT = "G4 Playback Output"
PLAYBACK_NODE_PREFIX = "G4 Playback "
_sync_modes: dict[int, str] = {}


def _preferences():
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon is not None else None


def _fast_playback_enabled() -> bool:
    preferences = _preferences()
    return preferences is None or bool(getattr(preferences, "fast_game_playback", True))


def _character_materials(scene: bpy.types.Scene) -> set[bpy.types.Material]:
    return {
        slot.material
        for obj in scene.objects
        for slot in obj.material_slots
        if slot.material and slot.material.get("g4_level5_toon") and slot.material.node_tree
    }


def _active_output(nodes: bpy.types.Nodes):
    return next(
        (node for node in nodes if node.type == "OUTPUT_MATERIAL" and node.is_active_output),
        None,
    )


def ensure_playback_output(material: bpy.types.Material):
    tree = material.node_tree
    if tree is None or not material.get("g4_level5_toon"):
        return None
    nodes = tree.nodes
    existing = nodes.get(PLAYBACK_OUTPUT)
    if existing is not None:
        return existing
    base = nodes.get("G4 Base")
    full = _active_output(nodes)
    if base is None or full is None:
        return None

    full.name = FULL_OUTPUT
    links = tree.links
    output = nodes.new("ShaderNodeOutputMaterial")
    output.name = PLAYBACK_OUTPUT
    output.label = "Fast timeline preview"

    geometry = nodes.new("ShaderNodeNewGeometry")
    geometry.name = PLAYBACK_NODE_PREFIX + "Geometry"
    direction = nodes.new("ShaderNodeCombineXYZ")
    direction.name = PLAYBACK_NODE_PREFIX + "Light Direction"
    direction.inputs[0].default_value = 0.30
    direction.inputs[1].default_value = -0.45
    direction.inputs[2].default_value = 0.84
    dot = nodes.new("ShaderNodeVectorMath")
    dot.name = PLAYBACK_NODE_PREFIX + "Light"
    dot.operation = "DOT_PRODUCT"
    links.new(geometry.outputs["Normal"], dot.inputs[0])
    links.new(direction.outputs["Vector"], dot.inputs[1])
    threshold = nodes.new("ShaderNodeMath")
    threshold.name = PLAYBACK_NODE_PREFIX + "Band"
    threshold.operation = "GREATER_THAN"
    threshold.inputs[1].default_value = -0.08
    links.new(dot.outputs["Value"], threshold.inputs[0])

    light_factor = threshold.outputs[0]
    occlusion = nodes.get("G4 Occlusion")
    if occlusion is not None:
        channels = nodes.new("ShaderNodeSeparateColor")
        channels.name = PLAYBACK_NODE_PREFIX + "Occlusion"
        links.new(occlusion.outputs["Color"], channels.inputs["Color"])
        occlusion_light = nodes.new("ShaderNodeMath")
        occlusion_light.name = PLAYBACK_NODE_PREFIX + "Occlusion Light"
        occlusion_light.operation = "MULTIPLY"
        occlusion_light.inputs[1].default_value = 1.35
        occlusion_light.use_clamp = True
        links.new(channels.outputs["Red"], occlusion_light.inputs[0])
        combined = nodes.new("ShaderNodeMath")
        combined.name = PLAYBACK_NODE_PREFIX + "Light Mask"
        combined.operation = "MULTIPLY"
        links.new(threshold.outputs[0], combined.inputs[0])
        links.new(occlusion_light.outputs[0], combined.inputs[1])
        light_factor = combined.outputs[0]

    shadow = nodes.new("ShaderNodeMixRGB")
    shadow.name = PLAYBACK_NODE_PREFIX + "Shadow"
    shadow.blend_type = "MULTIPLY"
    shadow.inputs[0].default_value = 1.0
    shadow.inputs[2].default_value = (0.75, 0.61, 0.78, 1.0)
    links.new(base.outputs["Color"], shadow.inputs[1])
    toon = nodes.new("ShaderNodeMixRGB")
    toon.name = PLAYBACK_NODE_PREFIX + "Toon"
    links.new(light_factor, toon.inputs[0])
    links.new(shadow.outputs["Color"], toon.inputs[1])
    links.new(base.outputs["Color"], toon.inputs[2])
    emission = nodes.new("ShaderNodeEmission")
    emission.name = PLAYBACK_NODE_PREFIX + "Emission"
    links.new(toon.outputs["Color"], emission.inputs["Color"])

    alpha_source = None
    alpha = nodes.get("G4 Alpha Mask")
    if alpha is not None:
        channels = nodes.new("ShaderNodeSeparateColor")
        channels.name = PLAYBACK_NODE_PREFIX + "Alpha"
        links.new(alpha.outputs["Color"], channels.inputs["Color"])
        alpha_source = channels.outputs["Red"]
    elif full.inputs["Surface"].is_linked:
        surface = full.inputs["Surface"].links[0].from_node
        if surface.type == "MIX_SHADER":
            alpha_source = base.outputs["Alpha"]
    if alpha_source is None:
        links.new(emission.outputs["Emission"], output.inputs["Surface"])
    else:
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        transparent.name = PLAYBACK_NODE_PREFIX + "Transparent"
        mix = nodes.new("ShaderNodeMixShader")
        mix.name = PLAYBACK_NODE_PREFIX + "Alpha Mix"
        links.new(alpha_source, mix.inputs[0])
        links.new(transparent.outputs[0], mix.inputs[1])
        links.new(emission.outputs["Emission"], mix.inputs[2])
        links.new(mix.outputs[0], output.inputs["Surface"])
    full.is_active_output = True
    output.is_active_output = False
    material["g4_fast_playback_output"] = True
    return output


def set_fast_playback(scene: bpy.types.Scene, enabled: bool) -> int:
    changed = 0
    for material in _character_materials(scene):
        tree = material.node_tree
        playback = ensure_playback_output(material) if enabled else tree.nodes.get(PLAYBACK_OUTPUT)
        full = tree.nodes.get(FULL_OUTPUT)
        if full is None:
            full = next(
                (node for node in tree.nodes if node.type == "OUTPUT_MATERIAL" and node != playback),
                None,
            )
            if full is not None:
                full.name = FULL_OUTPUT
        if playback is None or full is None:
            continue
        playback.is_active_output = enabled
        full.is_active_output = not enabled
        changed += 1
    return changed


def restore_full_outputs() -> None:
    for scene in bpy.data.scenes:
        set_fast_playback(scene, False)


@persistent
def playback_pre(scene, _depsgraph=None) -> None:
    if not _fast_playback_enabled() or not _character_materials(scene):
        return
    pointer = scene.as_pointer()
    _sync_modes.setdefault(pointer, scene.sync_mode)
    scene.sync_mode = "FRAME_DROP"
    set_fast_playback(scene, True)


@persistent
def playback_post(scene, _depsgraph=None) -> None:
    set_fast_playback(scene, False)
    previous = _sync_modes.pop(scene.as_pointer(), None)
    if previous is not None:
        scene.sync_mode = previous


@persistent
def render_pre(scene, _depsgraph=None) -> None:
    set_fast_playback(scene, False)


@persistent
def load_post(_unused) -> None:
    _sync_modes.clear()
    restore_full_outputs()


def register() -> None:
    for handlers, callback in (
        (getattr(bpy.app.handlers, "animation_playback_pre", ()), playback_pre),
        (getattr(bpy.app.handlers, "animation_playback_post", ()), playback_post),
        (bpy.app.handlers.render_pre, render_pre),
        (bpy.app.handlers.load_post, load_post),
    ):
        if callback not in handlers:
            handlers.append(callback)


def unregister() -> None:
    restore_full_outputs()
    _sync_modes.clear()
    for handlers, callback in (
        (getattr(bpy.app.handlers, "animation_playback_pre", ()), playback_pre),
        (getattr(bpy.app.handlers, "animation_playback_post", ()), playback_post),
        (bpy.app.handlers.render_pre, render_pre),
        (bpy.app.handlers.load_post, load_post),
    ):
        if callback in handlers:
            handlers.remove(callback)


__all__ = [
    "ensure_playback_output",
    "load_post",
    "playback_post",
    "playback_pre",
    "register",
    "render_pre",
    "restore_full_outputs",
    "set_fast_playback",
    "unregister",
]
