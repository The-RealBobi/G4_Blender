"""Resolve the native base-weather map light through the environment tables."""
from dataclasses import dataclass
from pathlib import Path
import math
from ..formats.cfgbin import CfgBinDocument, RdbnpDocument, parse_cfgbin_file


@dataclass(frozen=True)
class MapEnvironment:
    source: Path
    direction: tuple[float, ...]
    color: tuple[float, ...]
    ambient: tuple[float, ...]


def native_map_environment(source: Path) -> MapEnvironment | None:
    common = next((p for p in source.parents if p.name == 'common'), None)
    if common is None:
        return None
    config = next((p / 'config' / f'{p.name}_env_data_0.00.00.cfg.bin' for p in source.parents
                   if (p / 'config' / f'{p.name}_env_data_0.00.00.cfg.bin').is_file()), None)
    table = common / 'map' / 'map_light_set.cfg.bin'
    if config is None or not table.is_file():
        return None
    document = parse_cfgbin_file(config)
    if not isinstance(document, CfgBinDocument):
        return None
    entry = next((e for e in document.entries if e.name == 'ENV_LIGHT_SET_INFO_DATA'), None)
    if entry is None or not entry.values:
        return None
    identifier = int(entry.values[0].value) & 0xffffffff
    document = parse_cfgbin_file(table)
    if not isinstance(document, RdbnpDocument):
        return None
    lists = {item.name: item for item in document.lists}
    sets = lists['m_lightSetList']
    selected = next((r for r in sets.rows if r.get('lightSetID') == identifier), None)
    if selected is None:
        return None
    span = selected.get('lightGroupList')
    groups = lists['m_lightGroupList'].rows[span.offset:span.offset + span.count]
    group = next((r for r in groups if r.get('weatherType') == 0), None)
    if group is None:
        return None
    span = group.get('lightInfoList')
    info = lists['m_lightInfoList'].rows[span.offset:span.offset + span.count]
    base = next((r for r in info if r.get('time') == -1.), None)
    if base is None or not isinstance(base.get('lightBasePath'), str):
        return None
    relative = Path(base.get('lightBasePath').replace('\\', '/'))
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Map light resource escapes the data root')
    path = common.parent / relative
    if not path.is_file():
        return None
    document = parse_cfgbin_file(path)
    if not isinstance(document, CfgBinDocument):
        return None
    parameters = {str(e.values[0].value): tuple(v.value for v in e.values[1:])
                  for e in document.entries if e.name == 'PROP_PARAM' and e.values}
    rows = [parameters.get(k, ()) for k in ('lightDir', 'lightColor', 'lightAmbient', 'mapPbrLightInfo')]
    if any(len(r) < 3 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in r) for r in rows):
        raise ValueError(f'Incomplete native map lighting parameters: {path.name}')
    if sum(v*v for v in rows[0][:3]) <= 1e-12:
        raise ValueError(f'Zero native map light direction: {path.name}')
    return MapEnvironment(path, tuple(rows[0][:3]), tuple(rows[1][:3]),
                          tuple(v * rows[3][1] for v in rows[2][:3]))


def configure_native_map_environment(scene, source: Path) -> bool:
    import bpy
    from ..formats.g4_axes import blender_from_g4_matrix
    from mathutils import Vector
    if scene.get('g4_map_environment_model') == str(source):
        return True
    environment = native_map_environment(source)
    if environment is None:
        return False
    scene['g4_map_environment_model'] = str(source)
    scene['g4_map_environment_source'] = str(environment.source)
    scene['g4_map_ambient'] = environment.ambient
    sun = next((o for o in scene.objects if o.get('g4_map_environment_sun')), None)
    if sun is None:
        sun = bpy.data.objects.new('Map Sun', bpy.data.lights.new('Map Sun', 'SUN'))
        scene.collection.objects.link(sun)
        sun['g4_map_environment_sun'] = True
    direction = blender_from_g4_matrix().to_3x3() @ Vector(environment.direction)
    sun.rotation_euler = (-direction).to_track_quat('-Z', 'Y').to_euler()
    energy = max(environment.color)
    sun.data.color = tuple(v / energy for v in environment.color) if energy > 0 else (0., 0., 0.)
    sun.data.energy = energy
    if hasattr(sun.data, 'shadow_maximum_resolution'):
        sun.data.shadow_maximum_resolution = .02
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = 0.
    scene.view_settings.gamma = 1.
    return True
