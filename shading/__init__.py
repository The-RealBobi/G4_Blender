"""Blender-independent shader profiles and material contracts."""

from .character_profile import CharacterShaderProfile, characterize_current_toon_shader
from .character_textures import character_texture_base_key, character_texture_role
from .map_surfaces import MapSurfaceProfile, classify_map_surface
from .map_nodes import apply_map_surface_nodes

__all__ = [
    "CharacterShaderProfile",
    "characterize_current_toon_shader",
    "character_texture_base_key",
    "character_texture_role",
    "MapSurfaceProfile",
    "classify_map_surface",
    "apply_map_surface_nodes",
]
