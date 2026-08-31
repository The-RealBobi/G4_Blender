"""Evidence-aware profiles for world surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

try:
    from ..formats.contracts import FidelityState, ShaderParameter, ShaderProfile
except ImportError:
    from formats.contracts import FidelityState, ShaderParameter, ShaderProfile


class MapSurfaceKind(str, Enum):
    TERRAIN = "terrain"
    GRASS = "grass"
    WATER = "water"
    CUTOUT = "cutout"
    PBR = "map_pbr"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MapSurfaceProfile:
    kind: MapSurfaceKind
    fidelity: FidelityState
    parameters: tuple[ShaderParameter, ...]
    evidence_names: tuple[str, ...] = ()

    def as_shader_profile(self) -> ShaderProfile:
        return ShaderProfile(
            name=f"G4 Map {self.kind.value}",
            family=self.kind.value,
            fidelity=self.fidelity,
            parameters=self.parameters,
            notes=self.evidence_names,
        )


def classify_map_surface(material_name: str, resource_path: Path | None = None) -> MapSurfaceProfile:
    haystack = f"{material_name} {resource_path or ''}".casefold()
    if any(token in haystack for token in ("river", "water", "wave")):
        return MapSurfaceProfile(
            MapSurfaceKind.WATER,
            FidelityState.APPROXIMATE,
            (
                ShaderParameter("water_speed", "vector", native_source="mapRiverWaterSpeed"),
                ShaderParameter("water_distortion", "float", native_source="mapRiverWaterDistortion"),
                ShaderParameter("water_height", "float", native_source="mapRiverWaterHeight"),
                ShaderParameter("ibl_rate", "float", native_source="waterRef_IblRate"),
            ),
            ("Effect_Water1", "wave_simulate", "wave_update"),
        )
    if any(token in haystack for token in ("grass", "foliage", "parallax_grass")):
        return MapSurfaceProfile(
            MapSurfaceKind.GRASS,
            FidelityState.APPROXIMATE,
            (
                ShaderParameter("near_sample", "float", native_source="mapParallaxGrassNearSample"),
                ShaderParameter("far_sample", "float", native_source="mapParallaxGrassFarSample"),
                ShaderParameter("far_length", "float", native_source="mapParallaxGrassFarLen"),
            ),
            ("MapGrassShaderParam", "map_pbrgrass", "map_parallax_grass1"),
        )
    if any(token in haystack for token in ("cutout", "dither", "tree", "flower")):
        return MapSurfaceProfile(
            MapSurfaceKind.CUTOUT,
            FidelityState.APPROXIMATE,
            (ShaderParameter("alpha", "float", native_source="native alpha/cutout"),),
            ("map_pbrnst1l_cutout_dither",),
        )
    if "pbr" in haystack or "map" in haystack:
        return MapSurfaceProfile(
            MapSurfaceKind.PBR,
            FidelityState.APPROXIMATE,
            (
                ShaderParameter("base_color", "color", native_source="map_pbr"),
                ShaderParameter("normal", "normal", native_source="map_pbr"),
                ShaderParameter("occlusion", "float", native_source="mapPbrParamScaleInfo"),
                ShaderParameter("ibl", "float", native_source="mapPbrIBLInfo"),
            ),
            ("map_pbr", "reflection_probe", "map_ibl"),
        )
    return MapSurfaceProfile(MapSurfaceKind.UNKNOWN, FidelityState.INCOMPLETE, ())


__all__ = ["MapSurfaceKind", "MapSurfaceProfile", "classify_map_surface"]
