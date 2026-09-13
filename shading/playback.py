"""Responsive timeline playback without changing the character material."""

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


def restore_full_outputs() -> None:
    for material in bpy.data.materials:
        tree = material.node_tree
        if tree is None or not material.get("g4_level5_toon"):
            continue
        playback = tree.nodes.get(PLAYBACK_OUTPUT)
        full = tree.nodes.get(FULL_OUTPUT)
        if full is not None:
            full.is_active_output = True
        if playback is not None:
            tree.nodes.remove(playback)
        for node in tuple(tree.nodes):
            if node.name.startswith(PLAYBACK_NODE_PREFIX):
                tree.nodes.remove(node)
        material.pop("g4_fast_playback_output", None)


@persistent
def playback_pre(scene, _depsgraph=None) -> None:
    if not _fast_playback_enabled() or not _character_materials(scene):
        return
    pointer = scene.as_pointer()
    _sync_modes.setdefault(pointer, scene.sync_mode)
    scene.sync_mode = "FRAME_DROP"


@persistent
def playback_post(scene, _depsgraph=None) -> None:
    previous = _sync_modes.pop(scene.as_pointer(), None)
    if previous is not None:
        scene.sync_mode = previous


@persistent
def render_pre(scene, _depsgraph=None) -> None:
    restore_full_outputs()


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
    "load_post",
    "playback_post",
    "playback_pre",
    "register",
    "render_pre",
    "restore_full_outputs",
    "unregister",
]
