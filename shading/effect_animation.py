"""Apply native UV loops, material colors and geometry clips to effect previews."""
from pathlib import Path
import re
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
            event_clip = next(iter(clips.values())) if len(clips) == 1 else None
            is_event = event_clip is not None and re.fullmatch(r'c\d{4}', event_clip['name'])
            names = (event_clip['name'],) if is_event else ('in', 'loop', 'out')
            if not set(names) <= clips.keys() or any(clips[name]['flags'] & 1 for name in names):
                continue
            phases = None if is_event else effect_phases(clips, scene.frame_start, scene.frame_end, scene.render.fps/scene.render.fps_base)
            data = path.read_bytes()
            channels = {}
            for name in names:
                clip = clips[name]
                channels[name] = {}
                for info in parsed['target_infos'][clip['target_info_start']:clip['target_info_start']+clip['target_info_count']]:
                    crc = int(parsed['targets'][info['target_index']]['crc32b'], 16)
                    channels[name][crc] = {
                        (c['channel_type']-16 if c['channel_type'] < 32 else (c['encoding'][7], c['channel_type']-32)): c
                        for c in parsed['channels'][info['channel_start']:info['channel_start']+info['channel_count']]
                        if c['encoding'][4] == 1 and
                        ((16 <= c['channel_type'] <= 19 and c['encoding'][7] == 0) or 32 <= c['channel_type'] <= 35)}
            frames = sorted(set(range(scene.frame_start, scene.frame_end+1)) |
                            ({phases.entry_end, phases.exit_start} if phases else set()))
            for material in materials:
                if not material.get('g4_effect_preview') or not material.use_nodes:
                    continue
                tree = material.node_tree
                animation = tree.animation_data
                if animation and (animation.nla_tracks or (animation.action and not animation.action.get('g4_effect_animation'))):
                    continue
                crc = material_crc32b(material)
                inputs = [(i, f'Effect Diffuse {"RGBA"[i]}') for i in range(4)]
                inputs += [((i, j), f'Effect Parameter {i} {"XYZW"[j]}') for i in range(8) for j in range(4)]
                for component, node_name in inputs:
                    if not all(component in channels[name].get(crc,{}) for name in names):
                        continue
                    node = tree.nodes.get(node_name)
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
                        if phases is None:
                            name = event_clip['name']
                            source_frame = min(event_clip['end_frame'], event_clip['start_frame'] +
                                               (frame-scene.frame_start) * (event_clip['fps'] or 60) *
                                               scene.render.fps_base / scene.render.fps)
                        else:
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
            if phases is not None and source.is_file():
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
            event_clip = next(iter(clips.values())) if len(clips) == 1 else None
            if event_clip is not None and re.fullmatch(r'c\d{4}', event_clip['name']):
                if event_clip['flags'] & 1 or event_clip['end_frame'] <= event_clip['start_frame']:
                    continue
                motion = decode_motion(path, event_clip['name'], skeleton)
                action, keyed = create_action(armature, motion)
                armature.animation_data.action = None
                track = armature.animation_data.nla_tracks.new()
                track.name = 'Effect Event'
                strip = track.strips.new(event_clip['name'], scene.frame_start, action)
                strip.action_frame_start = 1
                strip.action_frame_end = event_clip['frame_count']
                strip.scale = scene.render.fps / scene.render.fps_base / (event_clip['fps'] or 60)
                strip.blend_type = 'REPLACE'
                strip.extrapolation = 'HOLD_FORWARD'
                scene.frame_set(scene.frame_current)
                return 1
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


def animate_particle_texture_clock(model: Path, obj: bpy.types.Object, record: dict,
                                   scene: bpy.types.Scene) -> int:
    """Bind the authored stand UV clip, retaining its source frame rate and offsets."""
    from ..g4ma_motion import texture_animation_paths
    from ..g4mt_motion import sample_channel, encoding_step
    from ..g4_animation_addon import action_fcurve_new

    hashes = record.get('uv_animation_hashes', [])
    if len(hashes) != 3:
        return 0
    modifier = next((m for m in obj.modifiers if m.type == 'NODES' and m.node_group
                     and m.node_group.get('g4_particle_geometry')), None)
    if modifier is None:
        return 0
    tree = modifier.node_group
    with tempfile.TemporaryDirectory(prefix='g4_particle_clock_') as temporary:
        for path in texture_animation_paths(model, Path(temporary)):
            parsed = parse_g4mt(path)
            clip = next((c for c in parsed['clips'] if c['name'] == 'stand'), None)
            if clip is None or clip['flags'] & 1 or clip['fps'] <= 0:
                continue
            bindings = {}
            for info in parsed['target_infos'][clip['target_info_start']:clip['target_info_start'] + clip['target_info_count']]:
                target = int(parsed['targets'][info['target_index']]['crc32b'], 16)
                channels = parsed['channels'][info['channel_start']:info['channel_start'] + info['channel_count']]
                if len(channels) == 1 and channels[0]['channel_type'] == 11 and channels[0]['encoding'][4] == 1 and channels[0]['encoding'][7] == 0:
                    bindings[target] = channels[0]
            if any(hashes[index] not in bindings for index in (1, 2)):
                continue
            data = path.read_bytes()
            samples = {}
            for index in (1, 2):
                channel = bindings[hashes[index]]
                samples[index] = [(frame, sample_channel(data, parsed['section_offsets']['data'], channel,
                                  parsed['scales'][channel['encoding'][6]], frame)[0])
                                  for frame in range(clip['start_frame'], clip['end_frame'] + 1)]
            action = bpy.data.actions.new(f'Particle UV {obj.name} {clip["name"]}')
            action['g4_effect_animation'] = True
            frame_scale = scene.render.fps / scene.render.fps_base / clip['fps']
            for index in (1, 2):
                uv = tree.nodes[f'Particle UV {index}']
                for link in list(uv.inputs[1].links):
                    tree.links.remove(link)
                # The VS subtracts matrix V translation. Blender's flipped V adds it.
                offset = tree.nodes.new('ShaderNodeMath')
                offset.operation = 'ADD'
                offset.name = f'Particle Native V {index}'
                tree.links.new(tree.nodes[f'Particle Source UV {index}'].outputs[1], offset.inputs[0])
                tree.links.new(offset.outputs[0], uv.inputs[1])
                curve = action_fcurve_new(action, tree, offset.inputs[1].path_from_id('default_value'), 0, 'Particle UV')
                for frame, value in samples[index]:
                    key = curve.keyframe_points.insert(scene.frame_start + (frame-clip['start_frame'])*frame_scale,
                                                       value, options={'FAST'})
                    key.interpolation = 'CONSTANT' if encoding_step(bindings[hashes[index]]) else 'LINEAR'
                curve.update()
            tree.animation_data_create().action = action
            tree['g4_particle_clock'] = 'G4TP stand'
            tree['g4_particle_source_fps'] = clip['fps']
            # The native action replaces the diagnostic range completely.
            for item in list(tree.interface.items_tree):
                if item.item_type == 'SOCKET' and item.name in ('Preview Start', 'Preview End'):
                    tree.interface.remove(item)
            unused = [node for node in tree.nodes if node.bl_idname != 'NodeGroupOutput'
                      and not any(socket.is_linked for socket in node.outputs)]
            while unused:
                for node in unused:
                    tree.nodes.remove(node)
                unused = [node for node in tree.nodes if node.bl_idname != 'NodeGroupOutput'
                          and not any(socket.is_linked for socket in node.outputs)]
            return 2
    return 0


def animate_event_texture_clip(model: Path, materials: list[bpy.types.Material], records: dict,
                               scene: bpy.types.Scene) -> int:
    """Bind event UV translations through the material's native target hashes."""
    from ..g4ma_motion import texture_animation_paths
    from ..g4mt_motion import sample_channel, encoding_step
    from ..g4_animation_addon import action_fcurve_new, action_fcurve_find
    from .. import blender_base_name

    count = 0
    with tempfile.TemporaryDirectory(prefix='g4_event_uv_') as temporary:
        for path in texture_animation_paths(model, Path(temporary)):
            parsed = parse_g4mt(path)
            if len(parsed['clips']) != 1:
                continue
            clip = parsed['clips'][0]
            if not re.fullmatch(r'c\d{4}', clip['name']) or clip['flags'] & 1:
                continue
            channels = {}
            for info in parsed['target_infos'][clip['target_info_start']:clip['target_info_start']+clip['target_info_count']]:
                crc = int(parsed['targets'][info['target_index']]['crc32b'], 16)
                channels[crc] = parsed['channels'][info['channel_start']:info['channel_start']+info['channel_count']]
            data = path.read_bytes()
            for material in materials:
                if not material.get('g4_effect_preview'):
                    continue
                tree = material.node_tree
                animation = tree.animation_data_create()
                if animation.nla_tracks or (animation.action and not animation.action.get('g4_effect_animation')):
                    continue
                for slot, crc in enumerate(records.get(blender_base_name(material.name), {}).get('uv_animation_hashes', [])):
                    for channel in channels.get(crc, []):
                        axis = channel['channel_type']-10
                        if axis not in (0, 1) or channel['encoding'][4] != 1 or channel['encoding'][7] != 0:
                            continue
                        node = tree.nodes.get(f'Effect UV {slot} Row {axis}')
                        if node is None or node.inputs[1].default_value[1-axis] != 0:
                            continue
                        socket = node.inputs[1]
                        if animation.action is None:
                            animation.action = bpy.data.actions.new(f'Event UV {material.name}')
                            animation.action['g4_effect_animation'] = True
                        target = socket.path_from_id('default_value')
                        if action_fcurve_find(animation.action, target, 2) is not None:
                            continue
                        curve = action_fcurve_new(animation.action, tree, target, 2, 'Effect UV')
                        scale = -socket.default_value[axis]
                        for frame in range(clip['start_frame'], clip['end_frame']+1):
                            value = sample_channel(data, parsed['section_offsets']['data'], channel,
                                                   parsed['scales'][channel['encoding'][6]], frame)[0]
                            time = scene.frame_start+(frame-clip['start_frame'])*scene.render.fps/scene.render.fps_base/(clip['fps'] or 60)
                            key = curve.keyframe_points.insert(time, scale*value, options={'FAST'})
                            key.interpolation = 'CONSTANT' if encoding_step(channel) else 'LINEAR'
                        curve.update()
                        count += 1
    return count


def optimize_effect_animation(scene: bpy.types.Scene) -> int:
    from .scene_animation import migrate_material_animation

    paths = {f'nodes["Effect Diffuse {component}"].outputs[0].default_value' for component in "RGBA"}
    paths.update(f'nodes["Effect Parameter {index} {component}"].outputs[0].default_value'
                 for index in range(8) for component in "XYZW")
    paths.update(f'nodes["Effect UV {index} Row {row}"].inputs[1].default_value'
                 for index in range(8) for row in range(2))
    materials = {slot.material for obj in scene.objects for slot in obj.material_slots
                 if slot.material and slot.material.get("g4_effect_preview")}
    materials = {material for material in materials if material.node_tree
                 and material.node_tree.animation_data and material.node_tree.animation_data.action
                 and material.node_tree.animation_data.action.get("g4_effect_animation")}
    return migrate_material_animation(scene, materials, paths, {"CONSTANT", "LINEAR"})
