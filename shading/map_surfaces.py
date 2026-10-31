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


__all__ = ["MapSurfaceKind", "MapSurfaceProfile", "classify_map_surface", "NativeMapMaterial", "native_map_material"]


@dataclass(frozen=True)
class NativeMapMaterial:
    family: str
    diffuse: tuple[float, ...]
    ambient: float
    normal_scale: float
    occlusion_recovery: float
    specular_floor: float
    specular_scale: float
    layer_scales: tuple[float, float]
    layer_power: float
    textures: tuple[str, ...]
    uv_rows: tuple[tuple[float, ...], tuple[float, ...]]
    addressing: tuple[str, ...]
    alpha_slot: int | None
    alpha_cutoff: float | None
    blended: bool
    grass: tuple[tuple[float, ...], ...] | None
    secondary_uv_rows: tuple[tuple[float, ...], tuple[float, ...]]


def native_map_material(record: dict) -> NativeMapMaterial | None:
    """Accept only verified native families and their material-slot contracts."""
    import math
    import zlib
    families = {
        zlib.crc32(name.encode('ascii')): (name, count, alpha)
        for name, count, alpha in (
            ('Map_PbrT1', 2, None), ('Map_PbrT2', 4, None),
            ('Map_Basic', 1, None), ('MapNolight', 1, None),
            ('Map_PbrT1Cutout', 3, 2), ('Map_BasicCutout', 2, 1),
            ('Map_ParallaxGrass1', 2, None),
        )
    }
    family = families.get(record.get('shader_hash'))
    if family is None:
        return None
    name, count, alpha_slot = family
    colors = record.get('native_colors')
    refs = record.get('texture_refs', [])
    matrices = record.get('uv_matrices', [])
    params = record.get('shader_parameters', [])
    pbr = name.startswith('Map_Pbr')
    grass = name == 'Map_ParallaxGrass1'
    if (not isinstance(colors, (list, tuple)) or len(colors) != 12
            or len(refs) != count or len(matrices) != count
            or any(len(v) != 4 for v in params)
            or (pbr and len(params) < 7) or (grass and len(params) < 3)):
        return None
    numbers = list(colors) + [v for row in matrices for v in row] + [v for row in params for v in row]
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in numbers):
        return None
    modes = {1: 'REPEAT', 2: 'MIRROR', 3: 'EXTEND'}
    addressing, paths = [], []
    for reference, matrix in zip(refs, matrices):
        sampler = dict(reference.get('sampler_states', []))
        path = reference.get('texture_path')
        if (len(matrix) != 8 or reference.get('slot_type') != 3 or not isinstance(path, str) or not path
                or sampler.get(3) != 1 or sampler.get(1) not in modes
                or sampler.get(2) != sampler.get(1)):
            return None
        addressing.append(modes[sampler[1]])
        paths.append(path)
    states = dict(record.get('render_states', []))
    cutoff = None
    if alpha_slot is not None:
        if states.get(2) != 4 or not isinstance(states.get(3), int) or not 0 <= states[3] <= 255:
            return None
        cutoff = states[3] / 255.0
    blended = states.get(6) == 1 and states.get(7) == 1 and states.get(9) == 4 and states.get(10) == 5
    if states.get(6, 0) and not blended:
        return None
    secondary = matrices[1] if grass else matrices[0]
    sa, sb, sc, _, sd, se, sf, _ = secondary
    matrix = matrices[0]
    a, b, c, _, d, e, f, _ = matrix
    return NativeMapMaterial(
        name, tuple(colors[:4]), 0.0 if grass else max(0.0, colors[4]),
        params[6][1] if pbr else 0.0, params[0][2] if params and not grass else 0.0,
        max(0.0, min(.99, params[2][1])) if pbr else 0.0,
        max(0.0, params[1][0]) if pbr else 0.0,
        (params[0][0], params[0][1]) if name == 'Map_PbrT2' else (1.0, 1.0),
        max(0.0, params[3][0]) if name == 'Map_PbrT2' else 1.0,
        tuple(paths), ((a, -b, b + c), (-d, e, 1.0 - e - f)),
        tuple(addressing), alpha_slot, cutoff, blended,
        tuple(tuple(row) for row in params[:3]) if grass else None,
        ((sa, -sb, sb + sc), (-sd, se, 1.0 - se - sf)),
    )
