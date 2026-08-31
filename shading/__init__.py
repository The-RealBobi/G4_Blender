"""Blender-independent shader profiles and material contracts."""

from .character_profile import CharacterShaderProfile, characterize_current_toon_shader
from .character_textures import (
    character_data_roots,
    character_shader_texture_containers,
    character_texture_base_key,
    character_texture_role,
    is_global_character_ramp_name,
)
from .map_surfaces import MapSurfaceProfile, classify_map_surface
from .map_nodes import apply_map_surface_nodes

__all__ = [
    "CharacterShaderProfile",
    "characterize_current_toon_shader",
    "character_data_roots",
    "character_shader_texture_containers",
    "character_texture_base_key",
    "character_texture_role",
    "is_global_character_ramp_name",
    "MapSurfaceProfile",
    "classify_map_surface",
    "apply_map_surface_nodes",
]
