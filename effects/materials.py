"""Native identifiers and parameters supported by static effect previews."""
from __future__ import annotations

from dataclasses import dataclass
import zlib


T1_SHADER_HASHES = frozenset(zlib.crc32(name.encode('ascii')) for name in ('Effect_T1', 'Effect_T1_low'))
T1M1_SHADER_HASHES = frozenset(zlib.crc32(name.encode('ascii')) for name in ('Effect_T1M1', 'Effect_T1M1_low'))
T2_SHADER_HASH = zlib.crc32(b'Effect_T2')
T2M1_SHADER_HASH = zlib.crc32(b'Effect_T2M1')
T1M1_AREA_SHADER_HASH = zlib.crc32(b'Effect_T1M1_area')
THRESHOLD_SHADER_HASHES = frozenset(zlib.crc32(name.encode('ascii')) for name in ('Effect_ThresholdGrd', 'Effect_ThresholdGrd_low'))
SOLID_TOON1_SHADER_HASH = zlib.crc32(b'Effect_SolidToon1')
T1M1_FLOW_SHADER_HASH = zlib.crc32(b'Effect_T1M1Flow')
T1_FLOW2_SHADER_HASH = zlib.crc32(b'Effect_T1Flow2')
DIFFERENCE_FLOW_HASHES = {zlib.crc32(b'Effect_DifferenceFlow'): 4, zlib.crc32(b'Effect_DifferenceFlowM'): 5}
BALL_AURA_SHADER_HASH = zlib.crc32(b'Effect_BallAura1')
T3_THRESHOLD_NO_FLOW_HASH = zlib.crc32(b'Effect_T3Threshold')
T1M1_MORPH_SHADER_HASH = zlib.crc32(b'Effect_T1M1Morph')
VDISP_RIM_SHADER_HASH = zlib.crc32(b'Effect_VDispRim')
T1M1_RIM_SHADER_HASH = zlib.crc32(b'Effect_T1M1Rim')
T3_THRESHOLD_SHADER_HASH = zlib.crc32(b'Effect_T3ThresholdF')

DISTORTION_SHADER_HASHES = frozenset(zlib.crc32(name.encode('ascii')) for name in (
    'Effect_Distortion', 'Effect_DistortionBlur',
))

@dataclass(frozen=True)
class EffectTexture:
    uv_rows: tuple[tuple[float, float, float], tuple[float, float, float]]
    address_modes: tuple[str, str]


@dataclass(frozen=True)
class EffectMaterial:
    diffuse: tuple[float, float, float, float]
    gain: float
    additive: bool
    textures: tuple[EffectTexture, ...]
    threshold: tuple[tuple[float, ...], ...] | None


def blender_uv_rows(matrix: list[float]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    # G4 UVs and Blender image coordinates have opposite vertical origins.
    a, b, c, _, d, e, f, _ = matrix
    return ((a, -b, b + c), (-d, e, 1.0 - e - f))


def static_effect_material(record: dict) -> EffectMaterial | None:
    shader = record.get('shader_hash')
    if shader in DIFFERENCE_FLOW_HASHES:
        texture_count = DIFFERENCE_FLOW_HASHES[shader]
    elif shader in (T1_FLOW2_SHADER_HASH, T1M1_FLOW_SHADER_HASH):
        texture_count = 3
        vectors = record.get("shader_parameters", [])
        if not vectors or len(vectors[0]) != 4:
            return None
    elif shader in T1_SHADER_HASHES:
        texture_count = 1
    elif shader in (T2_SHADER_HASH, T2M1_SHADER_HASH, T1M1_AREA_SHADER_HASH):
        texture_count = 3 if shader == T2M1_SHADER_HASH else 2
        vectors = record.get('shader_parameters', [])
        parameter_index = 2 if shader == T1M1_AREA_SHADER_HASH else 0
        if len(vectors) <= parameter_index or len(vectors[parameter_index]) != 4:
            return None
    elif shader in T1M1_SHADER_HASHES or shader == T1M1_MORPH_SHADER_HASH:
        texture_count = 2
    elif shader in THRESHOLD_SHADER_HASHES or shader == BALL_AURA_SHADER_HASH:
        texture_count = 3
    elif shader == VDISP_RIM_SHADER_HASH:
        texture_count = 3
    elif shader == T1M1_RIM_SHADER_HASH:
        texture_count = 4
    elif shader == T3_THRESHOLD_NO_FLOW_HASH:
        texture_count = 5
        vectors = record.get("shader_parameters", [])
        if len(vectors) < 4 or any(len(row) != 4 for row in vectors[:4]):
            return None
    elif shader == T3_THRESHOLD_SHADER_HASH:
        texture_count = 6
    else:
        return None
    colors = record.get('native_colors')
    matrices = record.get('uv_matrices', [])
    states = dict(record.get('render_states', []))
    references = record.get('texture_refs', [])
    threshold = None
    if texture_count == 6:
        parameters = record.get('shader_parameters', [])
        if (len(parameters) < 7 or any(len(row) != 4 for row in parameters[:7])
                or parameters[3][2:] != [1.0, 1.0] or parameters[5][2] != 0.0):
            return None
    if shader in THRESHOLD_SHADER_HASHES or shader == BALL_AURA_SHADER_HASH:
        parameters = record.get('shader_parameters', [])
        if len(parameters) < 3 or any(len(row) != 4 for row in parameters[:3]):
            return None
        if parameters[1][3] >= 1.0:
            return None
        threshold = tuple(tuple(row) for row in parameters[:3])
    if (colors is None or len(colors) != 12 or len(matrices) != texture_count
            or len(references) != texture_count or states.get(7) != 1
            or states.get(9) != 4 or states.get(10) not in (1, 5)):
        return None
    address_modes = {1: 'REPEAT', 2: 'MIRROR', 3: 'EXTEND'}
    textures = []
    for reference, matrix in zip(references, matrices):
        sampler = dict(reference.get('sampler_states', []))
        if (reference.get('slot_type') != 3 or len(matrix) != 8 or sampler.get(3) != 1
                or sampler.get(1) not in address_modes or sampler.get(2) not in address_modes):
            return None
        textures.append(EffectTexture(blender_uv_rows(matrix), (address_modes[sampler[1]], address_modes[sampler[2]])))
    return EffectMaterial(tuple(colors[:4]), 1.0 + colors[4], states[10] == 1, tuple(textures), threshold)
