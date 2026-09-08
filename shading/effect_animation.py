"""Apply native UV loops, material colors and geometry clips to effect previews."""
from pathlib import Path
import tempfile

import bpy

from ..effects.loop_animation import read_loop_animation
from ..g4pk_extract_g4mt import entries
from ..g4mt_probe import parse_g4mt
from ..g4mt_motion import decode_motion


def animate_effect_uv_loops(source: Path, materials: list[bpy.types.Material], scene: bpy.types.Scene, start_frame: int) -> int:
    from ..g4_animation_addon import action_fcurve_new, material_crc32b

    reference = read_loop_animation(source)
    if reference is None:
        return 0
    relative = Path(reference.motion_path.replace('\\', '/'))
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Effect loop motion must reference a relative asset path')
    package = next((parent / relative for parent in source.parents if (parent / relative).is_file()), None)
    if package is None:
        raise FileNotFoundError(f'Effect loop motion not found: {relative}')
    data = package.read_bytes()
    motion = None
    with tempfile.TemporaryDirectory(prefix='g4_effect_loop_') as temporary:
        for index, (_, offset, size) in enumerate(entries(data)):
            if data[offset:offset + 4] != b'G4MT':
                continue
            path = Path(temporary) / f'{index}.g4mt'
            path.write_bytes(data[offset:offset + size])
            parsed = parse_g4mt(path)
            clip = next((clip for clip in parsed['clips']
                         if int(clip['crc32b'], 16) == reference.motion_crc), None)
            if clip is not None:
                motion = decode_motion(path, clip['name'], None)
                break
    if motion is None:
        raise ValueError('Referenced effect loop clip was not found in its package')
    tracks = {int(track['target_hash'], 16): track for track in motion['tracks']}
    count = 0
    for material in materials:
        if not material.get('g4_effect_preview') or not material.use_nodes:
            continue
        tree = material.node_tree
        if tree.animation_data and (tree.animation_data.action or tree.animation_data.nla_tracks):
            continue
        action = None
        for binding in reference.bindings:
            if binding.material_crc != material_crc32b(material):
                continue
            if binding.source_type not in (1, 2, 3) or binding.target_type not in (10, 11) or binding.speed <= 0:
                continue
            track = tracks.get(binding.source_crc)
            if track is None or 'translation' not in track['animated_paths']:
                continue
            row_index = binding.target_type - 10
            row = tree.nodes.get(f'Effect UV {binding.texture_index} Row {row_index}')
            if row is None or row.inputs[1].default_value[1-row_index] != 0:
                continue
            if action is None:
                action = bpy.data.actions.new(f'Effect Loop {material.name}')
                action['g4_effect_animation'] = True
            socket = row.inputs[1]
            curve = action_fcurve_new(action, tree, socket.path_from_id('default_value'), 2, 'Effect UV')
            # Native translation is negated by inverse UV composition, including Blender's V conversion.
            scale = -socket.default_value[row_index]
            frame_scale = scene.render.fps / scene.render.fps_base / (motion['clip']['fps'] or 60) / binding.speed
            for frame, value in zip(motion['frames'], track['values']['translation']):
                key = curve.keyframe_points.insert(
                    start_frame + (frame-motion['clip']['start_frame'])*frame_scale,
                    scale*value[binding.source_type-1], options={'FAST'})
                key.interpolation = 'LINEAR'
            curve.modifiers.new('CYCLES').mode_before = 'NONE'
            count += 1
        if action is not None:
            tree.animation_data_create().action = action
    return count


def animate_effect_material_phases(model: Path, materials: list[bpy.types.Material], scene: bpy.types.Scene) -> int:
    from ..effects.phase_animation import effect_phases, phase_source_frame
    from ..g4ma_motion import material_animation_paths
    from ..g4mt_motion import sample_channel
    from ..g4_animation_addon import action_fcurve_find, action_fcurve_new, material_crc32b

    count = 0
    with tempfile.TemporaryDirectory(prefix='g4_effect_phases_') as temporary:
        for path in material_animation_paths(model, Path(temporary)):
            parsed = parse_g4mt(path)
            clips = {clip['name']: clip for clip in parsed['clips']}
            if not {'in', 'loop', 'out'} <= clips.keys() or any(clips[name]['flags'] & 1 for name in ('in','loop','out')):
                continue
            phases = effect_phases(clips, scene.frame_start, scene.frame_end, scene.render.fps/scene.render.fps_base)
            data = path.read_bytes()
            channels = {}
            for name in ('in', 'loop', 'out'):
                clip = clips[name]
                channels[name] = {}
                for info in parsed['target_infos'][clip['target_info_start']:clip['target_info_start']+clip['target_info_count']]:
                    crc = int(parsed['targets'][info['target_index']]['crc32b'], 16)
                    channels[name][crc] = {c['channel_type']-16: c for c in parsed['channels'][info['channel_start']:info['channel_start']+info['channel_count']]
                                          if 16 <= c['channel_type'] <= 19 and c['encoding'][7] == 0 and c['encoding'][4] == 1}
            frames = sorted(set(range(scene.frame_start, scene.frame_end+1)) | {phases.entry_end, phases.exit_start})
            for material in materials:
                if not material.get('g4_effect_preview') or not material.use_nodes:
                    continue
                tree = material.node_tree
                animation = tree.animation_data
                if animation and (animation.nla_tracks or (animation.action and not animation.action.get('g4_effect_animation'))):
                    continue
                crc = material_crc32b(material)
                for component in range(4):
                    if not all(component in channels[name].get(crc,{}) for name in ('in','loop','out')):
                        continue
                    node = tree.nodes.get(f'Effect Diffuse {"RGBA"[component]}')
                    if node is None:
                        continue
                    animation = tree.animation_data_create()
                    if animation.action is None:
                        animation.action = bpy.data.actions.new(f'Effect Phases {material.name}')
                        animation.action['g4_effect_animation'] = True
                    data_path = node.outputs[0].path_from_id('default_value')
                    if action_fcurve_find(animation.action, data_path, 0) is not None:
                        continue
                    curve = action_fcurve_new(animation.action, tree, data_path, 0, 'Effect Color')
                    for frame in frames:
                        name, source_frame = phase_source_frame(phases, clips, frame)
                        channel = channels[name][crc][component]
                        value = sample_channel(data, parsed['section_offsets']['data'], channel,
                                               parsed['scales'][channel['encoding'][6]], source_frame)[0]
                        point = curve.keyframe_points.insert(frame,value,options={'FAST'})
                        point.interpolation = 'LINEAR'
                    curve.update()
                    count += 1
                if tree.animation_data and tree.animation_data.action:
                    action = tree.animation_data.action
                    tree.animation_data.action = None
                    tree.animation_data.action = action
            source = model.with_suffix('.objbin')
            if source.is_file():
                count += animate_effect_random_offsets(source, materials, phases, clips)
    return count


def animate_effect_geometry_phases(model: Path, armature: bpy.types.Object, scene: bpy.types.Scene) -> int:
    from ..effects.phase_animation import effect_phases
    from ..g4_animation_addon import create_action

    if armature.animation_data and (armature.animation_data.action or armature.animation_data.nla_tracks):
        return 0
    data = model.read_bytes()
    files = entries(data)
    skeleton_entry = next(((offset,size) for _,offset,size in files if data[offset:offset+4] == b'G4SK'), None)
    if skeleton_entry is None:
        return 0
    with tempfile.TemporaryDirectory(prefix='g4_effect_geometry_') as temporary:
        skeleton = Path(temporary)/'skeleton.g4sk'
        offset,size = skeleton_entry
        skeleton.write_bytes(data[offset:offset+size])
        for index,(_,offset,size) in enumerate(files):
            if data[offset:offset+4] != b'G4MT':
                continue
            path = Path(temporary)/f'{index}.g4mt'
            path.write_bytes(data[offset:offset+size])
            clips = {clip['name']:clip for clip in parse_g4mt(path)['clips']}
            if not {'in','loop','out'} <= clips.keys() or any(clips[name]['flags'] & 1 for name in ('in','loop','out')):
                continue
            phases = effect_phases(clips,scene.frame_start,scene.frame_end,scene.render.fps/scene.render.fps_base)
            intervals = ((phases.start,phases.entry_end),(phases.entry_end,phases.exit_start),(phases.exit_start,phases.end))
            if any(end <= start for start,end in intervals):
                continue
            motions = {name:decode_motion(path,name,skeleton) for name in ('in','loop','out')}
            track = armature.animation_data_create().nla_tracks.new()
            track.name = 'Effect Phases'
            for name,(start,end) in zip(('in','loop','out'),intervals):
                action,keyed = create_action(armature,motions[name])
                action.name = f'Effect {name} {armature.name}'
                armature.animation_data.action = None
                strip = track.strips.new(name,scene.frame_end+1,action)
                strip.action_frame_start = 1
                strip.action_frame_end = clips[name]['end_frame']-clips[name]['start_frame']+1
                strip.frame_start = start
                strip.scale = phases.scene_fps/(clips[name]['fps'] or 60)
                if name == 'loop':
                    strip.repeat = (end-start)/((strip.action_frame_end-1)*strip.scale)
                strip.frame_end = end
                strip.blend_type = 'REPLACE'
                strip.extrapolation = 'NOTHING'
            scene.frame_set(scene.frame_current)
            return len(track.strips)
    return 0


def animate_effect_random_offsets(source: Path, materials: list[bpy.types.Material], phases, clips: dict) -> int:
    import random
    import zlib
    from ..effects.random_animation import read_random_animation, sample_parameters
    from ..g4_animation_addon import action_fcurve_new, material_crc32b

    animation = read_random_animation(source)
    # A reproducible preview realization; the game's global RNG state is not in the asset.
    generator = random.Random(zlib.crc32(source.stem.encode('utf-8')))
    state = animation.initial
    states = [(phases.start-1, state)]
    for name, frame in (('in', phases.start), ('loop', phases.entry_end), ('out', phases.exit_start)):
        state = sample_parameters(animation, int(clips[name]['crc32b'], 16), state, generator)
        states.append((frame, state))
    count = 0
    for material in materials:
        if not material.get('g4_effect_preview') or not material.use_nodes:
            continue
        tree = material.node_tree
        data = tree.animation_data
        if data and (data.nla_tracks or (data.action and not data.action.get('g4_effect_animation'))):
            continue
        bindings = [b for b in animation.bindings if b.material_crc == material_crc32b(material)]
        for slot in sorted({b.texture_index for b in bindings}):
            for axis in range(2):
                relevant = [b for b in bindings if b.texture_index == slot and b.mask & (1 << axis)]
                if not relevant or any(b.parameter_crc not in animation.initial for b in relevant):
                    continue
                row = tree.nodes.get(f'Effect UV {slot} Row {axis}')
                if row is None or row.inputs[1].default_value[1-axis] != 0:
                    continue
                name = f'Effect Random UV {slot} {axis}'
                if tree.nodes.get(name) is not None:
                    continue
                node = tree.nodes.new('ShaderNodeValue')
                node.name = name
                add = tree.nodes.new('ShaderNodeMath')
                add.operation = 'ADD'
                destinations = [link.to_socket for link in row.outputs['Value'].links]
                for destination in destinations:
                    tree.links.new(add.outputs[0], destination)
                tree.links.new(row.outputs['Value'], add.inputs[0])
                tree.links.new(node.outputs[0], add.inputs[1])
                data = tree.animation_data_create()
                if data.action is None:
                    data.action = bpy.data.actions.new(f'Effect Random {material.name}')
                    data.action['g4_effect_animation'] = True
                curve = action_fcurve_new(data.action, tree, node.outputs[0].path_from_id('default_value'), 0, 'Effect Random UV')
                scale = -row.inputs[1].default_value[axis]
                for frame, state in states:
                    value = sum(state[b.parameter_crc][axis] for b in relevant)*scale
                    point = curve.keyframe_points.insert(frame, value, options={'FAST'})
                    point.interpolation = 'CONSTANT'
                curve.update()
                count += 1
        if tree.animation_data and tree.animation_data.action:
            action = tree.animation_data.action
            tree.animation_data.action = None
            tree.animation_data.action = action
    return count
