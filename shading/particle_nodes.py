"""Base FakeParticle geometry; the explicit preview range is not the native emitter clock."""
from pathlib import Path

import bpy
from bpy.app.handlers import persistent

from .effect_nodes import effect_data_image
from .character_lighting import color_transfer


PARTICLE_GEOMETRY_SCHEMA = 4
PARTICLE_WINDOW_NODES = (
    'Particle Start Limit',
    'Particle After Start',
    'Particle End Limit',
    'Particle Before End',
    'Particle Active Window',
)


def _particle_group_signature(group):
    if group.animation_data is not None:
        return None

    def value(socket):
        if socket.is_linked:
            return None
        default = getattr(socket, "default_value", None)
        if isinstance(default, str) or not hasattr(default, "__iter__"):
            return default
        return tuple(default)

    nodes = tuple(
        (
            node.bl_idname,
            node.name,
            getattr(node, "operation", ""),
            getattr(node, "mode", ""),
            tuple(value(socket) for socket in node.inputs),
            str(node.inputs["Image"].default_value.filepath)
            if node.inputs.get("Image") is not None and node.inputs["Image"].default_value else "",
        )
        for node in group.nodes
    )
    node_ids = {node.name: index for index, node in enumerate(group.nodes)}
    links = tuple(sorted(
        (
            node_ids[link.from_node.name], link.from_socket.name,
            node_ids[link.to_node.name], link.to_socket.name,
        )
        for link in group.links
    ))
    interface = tuple(
        (
            item.item_type,
            item.in_out,
            item.name,
            getattr(item, "socket_type", ""),
            getattr(item, "default_value", None),
        )
        for item in group.interface.items_tree
    )
    custom = tuple(sorted((key, repr(value)) for key, value in group.items()))
    return repr((custom, interface, nodes, links))


def deduplicate_particle_geometry_groups(scene) -> int:
    """Share identical, non-animated particle graphs between effect objects."""
    canonical = {}
    replaced = 0
    for obj in scene.objects:
        for modifier in obj.modifiers:
            if modifier.type != "NODES" or modifier.node_group is None:
                continue
            group = modifier.node_group
            if not group.get("g4_particle_geometry"):
                continue
            signature = _particle_group_signature(group)
            if signature is None:
                continue
            target = canonical.setdefault(signature, group)
            if target == group:
                continue
            modifier.node_group = target
            replaced += 1
    for group in tuple(bpy.data.node_groups):
        if group.get("g4_particle_geometry") and group.users == 0:
            bpy.data.node_groups.remove(group)
    return replaced


def ensure_particle_preview_window(group: bpy.types.GeometryNodeTree) -> bool:
    """Stop billboard generation outside the explicit preview lifetime."""
    inputs = {
        item.name: item for item in group.interface.items_tree
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT'
    }
    points = next((node for node in group.nodes if node.bl_idname == 'GeometryNodeMeshToPoints'), None)
    source = next((node for node in group.nodes if node.bl_idname == 'NodeGroupInput'), None)
    scene_time = next((node for node in group.nodes if node.bl_idname == 'GeometryNodeInputSceneTime'), None)
    if (points is None or source is None or scene_time is None
            or 'Preview Start' not in inputs or 'Preview End' not in inputs):
        return False
    if group.nodes.get('Particle Active Window') is not None:
        group['g4_particle_geometry_schema'] = PARTICLE_GEOMETRY_SCHEMA
        return False

    start_limit = group.nodes.new('ShaderNodeMath')
    start_limit.name = 'Particle Start Limit'
    start_limit.operation = 'SUBTRACT'
    start_limit.inputs[1].default_value = 0.5
    group.links.new(source.outputs['Preview Start'], start_limit.inputs[0])

    after_start = group.nodes.new('ShaderNodeMath')
    after_start.name = 'Particle After Start'
    after_start.operation = 'GREATER_THAN'
    group.links.new(scene_time.outputs['Frame'], after_start.inputs[0])
    group.links.new(start_limit.outputs[0], after_start.inputs[1])

    end_limit = group.nodes.new('ShaderNodeMath')
    end_limit.name = 'Particle End Limit'
    end_limit.operation = 'ADD'
    end_limit.inputs[1].default_value = 0.5
    group.links.new(source.outputs['Preview End'], end_limit.inputs[0])

    before_end = group.nodes.new('ShaderNodeMath')
    before_end.name = 'Particle Before End'
    before_end.operation = 'LESS_THAN'
    group.links.new(scene_time.outputs['Frame'], before_end.inputs[0])
    group.links.new(end_limit.outputs[0], before_end.inputs[1])

    active = group.nodes.new('ShaderNodeMath')
    active.name = 'Particle Active Window'
    active.operation = 'MULTIPLY'
    group.links.new(after_start.outputs[0], active.inputs[0])
    group.links.new(before_end.outputs[0], active.inputs[1])
    for link in list(points.inputs['Selection'].links):
        group.links.remove(link)
    group.links.new(active.outputs[0], points.inputs['Selection'])
    group['g4_particle_geometry_schema'] = PARTICLE_GEOMETRY_SCHEMA
    return True


def remove_particle_preview_window(group: bpy.types.GeometryNodeTree) -> None:
    points = next((node for node in group.nodes if node.bl_idname == 'GeometryNodeMeshToPoints'), None)
    if points is not None:
        for link in list(points.inputs['Selection'].links):
            if link.from_node.name in PARTICLE_WINDOW_NODES:
                group.links.remove(link)
    for name in PARTICLE_WINDOW_NODES:
        node = group.nodes.get(name)
        if node is not None:
            group.nodes.remove(node)


def ensure_particle_gpu_instances(group: bpy.types.GeometryNodeTree) -> bool:
    """Keep billboards instanced instead of rebuilding an equivalent mesh each frame."""
    nodes = group.nodes
    instances = next((node for node in nodes if node.bl_idname == 'GeometryNodeInstanceOnPoints'), None)
    realize = next((node for node in nodes if node.bl_idname == 'GeometryNodeRealizeInstances'), None)
    assign = next((node for node in nodes if node.bl_idname == 'GeometryNodeSetMaterial'), None)
    output = next((node for node in nodes if node.bl_idname == 'NodeGroupOutput'), None)
    if (instances is None or realize is None or assign is None or output is None
            or not instances.inputs['Instance'].is_linked
            or not output.inputs['Geometry'].is_linked):
        return False
    local = output.inputs['Geometry'].links[0].from_node
    if (local.bl_idname != 'GeometryNodeTransform'
            or not assign.inputs['Geometry'].is_linked
            or assign.inputs['Geometry'].links[0].from_node != realize
            or not realize.inputs[0].is_linked
            or realize.inputs[0].links[0].from_node != instances
            or not local.inputs['Geometry'].is_linked
            or local.inputs['Geometry'].links[0].from_node != assign):
        return False

    source_instance = instances.inputs['Instance'].links[0].from_socket
    for socket in (assign.inputs['Geometry'], instances.inputs['Instance'], local.inputs['Geometry']):
        for link in list(socket.links):
            group.links.remove(link)
    group.links.new(source_instance, assign.inputs['Geometry'])
    group.links.new(assign.outputs['Geometry'], instances.inputs['Instance'])
    group.links.new(instances.outputs['Instances'], local.inputs['Geometry'])
    nodes.remove(realize)
    group['g4_particle_geometry_schema'] = PARTICLE_GEOMETRY_SCHEMA
    return True


def upgrade_particle_preview_windows(scene: bpy.types.Scene) -> int:
    upgraded = 0
    seen = set()
    for obj in scene.objects:
        for modifier in obj.modifiers:
            group = modifier.node_group if modifier.type == 'NODES' else None
            if group is None or not group.get('g4_particle_geometry'):
                continue
            if group not in seen:
                seen.add(group)
                upgraded += int(ensure_particle_preview_window(group))
                upgraded += int(ensure_particle_gpu_instances(group))
            if group.get('g4_particle_clock') == 'PTLB':
                inputs = {
                    item.name: item for item in group.interface.items_tree
                    if item.item_type == 'SOCKET' and item.in_out == 'INPUT'
                }
                for name in ('Preview Start', 'Preview End'):
                    socket = inputs.get(name)
                    if socket is not None:
                        set_particle_modifier_input(modifier, socket, socket.default_value)
    return upgraded


def set_particle_modifier_input(modifier, socket, value: float) -> None:
    if bpy.app.version >= (5, 0, 0):
        getattr(modifier.properties.inputs, socket.identifier).value = value
    else:
        modifier[socket.identifier] = value


def apply_particle_library_clock(objects, library, start_frame: float, scene: bpy.types.Scene) -> int:
    """Use an unambiguous PTLB lifetime when no G4TP animation supplies a clock."""
    if len(library.emitters) != 1:
        return 0
    emitter = library.emitters[0]
    duration = emitter.preview_duration_seconds
    scene_fps = scene.render.fps / scene.render.fps_base
    if duration <= 0 or scene_fps <= 0:
        return 0
    end_frame = start_frame + duration * scene_fps
    applied = 0
    seen = set()
    for obj in objects:
        for modifier in obj.modifiers:
            group = modifier.node_group if modifier.type == 'NODES' else None
            if (group is None or group in seen or not group.get('g4_particle_geometry')
                    or group.get('g4_particle_clock')):
                continue
            inputs = {item.name: item for item in group.interface.items_tree
                      if item.item_type == 'SOCKET' and item.in_out == 'INPUT'}
            if 'Preview Start' not in inputs or 'Preview End' not in inputs:
                continue
            inputs['Preview Start'].default_value = start_frame
            inputs['Preview End'].default_value = end_frame
            set_particle_modifier_input(modifier, inputs['Preview Start'], start_frame)
            set_particle_modifier_input(modifier, inputs['Preview End'], end_frame)
            group['g4_particle_clock'] = 'PTLB'
            group['g4_particle_node'] = emitter.node_name
            group['g4_particle_emission_interval_seconds'] = emitter.emission_interval_seconds
            group['g4_particle_phase_seconds'] = emitter.phase_durations_seconds
            group['g4_particle_lifetime_seconds'] = emitter.lifetime_seconds
            seen.add(group)
            applied += 1
    return applied


def migrate_particle_library_clocks(scene: bpy.types.Scene) -> int:
    """Upgrade saved event imports that still use the full-cut preview range."""
    from ..effects.particle_library import read_particle_library

    applied = 0
    for root in scene.objects:
        source = root.get('g4_event_effect_particle')
        if not source:
            continue
        start_frame = root.get('g4_event_effect_start_frame')
        if start_frame is None and root.animation_data:
            starts = [strip.frame_start for track in root.animation_data.nla_tracks
                      if track.name == 'Event Effect Placement' for strip in track.strips]
            start_frame = min(starts) if starts else None
        if start_frame is None:
            continue
        path = Path(source)
        if not path.is_file():
            continue
        try:
            library = read_particle_library(path)
        except (OSError, ValueError) as error:
            root['g4_event_effect_particle_error'] = str(error)
            continue
        count = apply_particle_library_clock(root.children_recursive, library, start_frame, scene)
        if count:
            root['g4_event_effect_particle_clocks'] = count
            applied += count
    return applied


def build_particle_geometry(obj: bpy.types.Object, record: dict, scene: bpy.types.Scene) -> bool:
    if record.get('shader_hash') != 3792297829 or obj.type != 'MESH':
        return False
    refs = record.get('texture_refs', [])
    parameters = record.get('shader_parameters', [])
    colors = record.get('native_colors', [])
    if (len(refs) != 3 or not parameters or len(parameters[0]) != 4 or len(colors) < 5
            or len(obj.data.materials) != 1
            or any(len(p.vertices) != 3 for p in obj.data.polygons)
            or any(name not in obj.data.attributes for name in
                   ('G4 Particle Control', 'G4 Particle Color', 'UVMap1', 'UVMap2'))
            or any(not ref.get('texture_path') or not Path(ref['texture_path']).is_file() for ref in refs)):
        return False
    # Restrict this first adapter to the captured projection and sampler contract.
    matrices = record.get('uv_matrices', [])
    if len(matrices) != 3 or any(len(m) != 8 for m in matrices) or any(any(abs(m[i] - v) > 1e-6 for i, v in
            ((0, 1), (1, 0), (2, 0), (4, 0), (5, 1))) for m in matrices):
        return False
    if abs(matrices[0][6]) > 1e-6:
        return False
    states = dict(record.get('render_states', []))
    if states.get(9) != 4 or states.get(10) not in (1, 5):
        return False
    if any(dict(ref['sampler_states']).get(1) != mode or
           dict(ref['sampler_states']).get(2) != mode or
           dict(ref['sampler_states']).get(3) != 1 for ref, mode in zip(refs, (1, 3, 3))):
        return False
    if any(m.type == 'NODES' and m.node_group and m.node_group.get('g4_particle_geometry') for m in obj.modifiers):
        return True
    images = [effect_data_image(ref['texture_path']) for ref in refs]
    for image in images:
        image.pack()
    group = bpy.data.node_groups.new('Particle texture preview', 'GeometryNodeTree')
    group['g4_particle_geometry'] = True
    group.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    group.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    for name, value in (('Preview Start', scene.frame_start), ('Preview End', scene.frame_end)):
        socket = group.interface.new_socket(name=name, in_out='INPUT', socket_type='NodeSocketFloat')
        socket.default_value = value
        socket.description = 'Texture phase preview range; native emission timing is not reconstructed'
    nodes, links = group.nodes, group.links

    def node(kind, **properties):
        result = nodes.new(kind)
        for key, value in properties.items():
            if key == 'mode' and 'Mode' in result.inputs:
                result.inputs['Mode'].default_value = value.title()
            else:
                setattr(result, key, value)
        return result

    def connect(value, socket):
        if isinstance(value, bpy.types.NodeSocket):
            links.new(value, socket)
        else:
            socket.default_value = value

    def math(operation, a, b):
        result = node('ShaderNodeMath', operation=operation)
        connect(a, result.inputs[0])
        connect(b, result.inputs[1])
        return result.outputs[0]

    def vector(operation, a, b):
        result = node('ShaderNodeVectorMath', operation=operation)
        connect(a, result.inputs[0])
        connect(b, result.inputs['Scale'] if operation == 'SCALE' else result.inputs[1])
        return result.outputs['Value' if operation in ('LENGTH', 'DISTANCE') else 'Vector']

    def components(value):
        result = node('ShaderNodeSeparateXYZ')
        connect(value, result.inputs[0])
        return result.outputs

    source = node('NodeGroupInput')
    self_info = node('GeometryNodeObjectInfo', transform_space='ORIGINAL')
    links.new(node('GeometryNodeSelfObject').outputs[0], self_info.inputs['Object'])
    world = node('GeometryNodeTransform', mode='MATRIX')
    links.new(source.outputs['Geometry'], world.inputs['Geometry'])
    links.new(self_info.outputs['Transform'], world.inputs['Transform'])
    geometry = world.outputs['Geometry']
    index = node('GeometryNodeInputIndex').outputs[0]
    corner = math('MULTIPLY', index, 3)

    def sample(value, data_type, offset):
        result = node('GeometryNodeSampleIndex', data_type=data_type, domain='CORNER')
        links.new(geometry, result.inputs['Geometry'])
        links.new(value, result.inputs['Value'])
        links.new(corner if offset == 0 else math('ADD', corner, offset), result.inputs['Index'])
        return result.outputs['Value']

    def attribute(name, data_type):
        result = node('GeometryNodeInputNamedAttribute', data_type=data_type)
        result.inputs['Name'].default_value = name
        return sample(result.outputs['Attribute'], data_type, 0)

    position_node = node('GeometryNodeInputPosition', name='Particle Face Position')
    position = position_node.outputs[0]
    a, b = (sample(position, 'FLOAT_VECTOR', i) for i in range(2))
    center = position
    normal = node('GeometryNodeInputNormal', name='Particle Face Normal').outputs[0]
    radial = vector('NORMALIZE', vector('SUBTRACT', a, center), (0, 0, 0))
    tangent = vector('CROSS_PRODUCT', normal, radial)
    time = node('GeometryNodeInputSceneTime').outputs['Frame']
    phase = math('DIVIDE', math('SUBTRACT', time, source.outputs['Preview Start']),
                 math('MAXIMUM', math('SUBTRACT', source.outputs['Preview End'], source.outputs['Preview Start']), 1))
    phase = math('MINIMUM', math('MAXIMUM', phase, 0), 1)
    textures = []
    for channel in (1, 2):
        uv = node('ShaderNodeCombineXYZ')
        source_uv = components(attribute(f'UVMap{channel}', 'FLOAT_VECTOR'))
        source_uv[0].node.name = f'Particle Source UV {channel}'
        links.new(source_uv[0], uv.inputs[0])
        uv.name = f'Particle UV {channel}'
        links.new(math('SUBTRACT', 1, phase), uv.inputs[1])
        texture = node('GeometryNodeImageTexture', interpolation='Linear', extension='EXTEND')
        texture.inputs['Image'].default_value = images[channel]
        links.new(uv.outputs[0], texture.inputs['Vector'])
        textures.append(texture)
    color0 = attribute('G4 Particle Control', 'FLOAT_COLOR')
    delta = vector('MULTIPLY', vector('SUBTRACT', vector('SCALE', textures[1].outputs['Color'], 2), (1, 1, 1)), color0)
    sx, sy, sz = components(self_info.outputs['Scale'])
    scale = math('DIVIDE', math('ADD', math('ADD', math('ABSOLUTE', sx, 0), math('ABSOLUTE', sy, 0)), math('ABSOLUTE', sz, 0)), 3)
    p = parameters[0]
    amplitude = math('MULTIPLY', p[1], math('ADD', 1, math('MULTIPLY', p[3], math('SUBTRACT', scale, 1))))
    dx, dy, dz = components(vector('SCALE', delta, amplitude))
    displacement = vector('ADD', vector('ADD', vector('SCALE', tangent, dx), vector('SCALE', normal, dy)), vector('SCALE', radial, dz))
    points = node('GeometryNodeMeshToPoints', mode='FACES')
    links.new(geometry, points.inputs['Mesh'])
    links.new(vector('ADD', center, displacement), points.inputs['Position'])
    radius = math('MULTIPLY', math('MULTIPLY', vector('DISTANCE', a, b), p[0] * .5), textures[1].outputs['Alpha'])
    particle_color = attribute('G4 Particle Color', 'FLOAT_COLOR')
    tint = vector('MULTIPLY', particle_color, textures[0].outputs['Color'])
    tint = vector('MULTIPLY', tint, tuple(colors[i] * (1 + colors[4]) for i in range(3)))
    separate0 = node('FunctionNodeSeparateColor', mode='RGB')
    links.new(color0, separate0.inputs[0])
    separate1 = node('FunctionNodeSeparateColor', mode='RGB')
    links.new(particle_color, separate1.inputs[0])
    alpha = math('MULTIPLY', math('MULTIPLY', separate0.outputs['Alpha'], separate1.outputs['Alpha']), math('MULTIPLY', textures[0].outputs['Alpha'], colors[3]))
    # Capture fields on the source faces before replacing the triangle geometry.
    current = geometry
    for name, value, data_type in (('Particle Tint', tint, 'FLOAT_VECTOR'), ('Particle Alpha', alpha, 'FLOAT'), ('Particle Radius', radius, 'FLOAT')):
        store = node('GeometryNodeStoreNamedAttribute', data_type=data_type, domain='FACE')
        store.inputs['Name'].default_value = name
        links.new(current, store.inputs['Geometry'])
        links.new(value, store.inputs['Value'])
        current = store.outputs['Geometry']
    links.new(current, points.inputs['Mesh'])
    grid = node('GeometryNodeMeshGrid')
    grid.inputs['Size X'].default_value = 2
    grid.inputs['Size Y'].default_value = 2
    grid.inputs['Vertices X'].default_value = 2
    grid.inputs['Vertices Y'].default_value = 2
    uv_store = node('GeometryNodeStoreNamedAttribute', data_type='FLOAT_VECTOR', domain='CORNER')
    uv_store.inputs['Name'].default_value = 'Particle UV'
    links.new(grid.outputs['Mesh'], uv_store.inputs['Geometry'])
    links.new(grid.outputs['UV Map'], uv_store.inputs['Value'])
    camera = node('GeometryNodeObjectInfo', name='Particle Camera', transform_space='ORIGINAL')
    camera.inputs['Object'].default_value = scene.camera
    instances = node('GeometryNodeInstanceOnPoints')
    links.new(points.outputs[0], instances.inputs['Points'])
    links.new(uv_store.outputs[0], instances.inputs['Instance'])
    links.new(camera.outputs['Rotation'], instances.inputs['Rotation'])
    radius_attr = node('GeometryNodeInputNamedAttribute', data_type='FLOAT')
    radius_attr.inputs['Name'].default_value = 'Particle Radius'
    links.new(radius_attr.outputs['Attribute'], instances.inputs['Scale'])
    realize = node('GeometryNodeRealizeInstances')
    links.new(instances.outputs[0], realize.inputs[0])
    material = obj.data.materials[0]
    _particle_material(material, images[0], states[10] == 1)
    assign = node('GeometryNodeSetMaterial')
    assign.inputs['Material'].default_value = material
    links.new(realize.outputs[0], assign.inputs['Geometry'])
    inverse = node('FunctionNodeInvertMatrix')
    links.new(self_info.outputs['Transform'], inverse.inputs['Matrix'])
    local = node('GeometryNodeTransform', mode='MATRIX')
    links.new(assign.outputs[0], local.inputs['Geometry'])
    links.new(inverse.outputs['Matrix'], local.inputs['Transform'])
    links.new(local.outputs[0], node('NodeGroupOutput').inputs['Geometry'])
    ensure_particle_gpu_instances(group)
    ensure_particle_preview_window(group)
    modifier = obj.modifiers.new('Particle texture preview', 'NODES')
    modifier.node_group = group
    return True


def _particle_material(material: bpy.types.Material, image: bpy.types.Image, additive: bool) -> None:
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    texture = nodes.new('ShaderNodeTexImage')
    texture.image = image
    uv = nodes.new('ShaderNodeAttribute')
    uv.attribute_name = 'Particle UV'
    links.new(uv.outputs['Vector'], texture.inputs['Vector'])
    tint = nodes.new('ShaderNodeAttribute')
    tint.attribute_name = 'Particle Tint'
    product = nodes.new('ShaderNodeVectorMath')
    product.operation = 'MULTIPLY'
    links.new(tint.outputs['Vector'], product.inputs[0])
    links.new(texture.outputs['Color'], product.inputs[1])
    opacity = nodes.new('ShaderNodeAttribute')
    opacity.attribute_name = 'Particle Alpha'
    alpha = nodes.new('ShaderNodeMath')
    alpha.operation = 'MULTIPLY'
    links.new(opacity.outputs['Fac'], alpha.inputs[0])
    links.new(texture.outputs['Alpha'], alpha.inputs[1])
    emission = nodes.new('ShaderNodeEmission')
    links.new(color_transfer(nodes, links, product.outputs[0], encode=False, approximate=True), emission.inputs[0])
    if additive:
        links.new(alpha.outputs[0], emission.inputs['Strength'])
        transparent = nodes.new('ShaderNodeBsdfTransparent')
        blend = nodes.new('ShaderNodeAddShader')
        links.new(transparent.outputs[0], blend.inputs[0])
        links.new(emission.outputs[0], blend.inputs[1])
    else:
        transparent = nodes.new('ShaderNodeBsdfTransparent')
        blend = nodes.new('ShaderNodeMixShader')
        links.new(alpha.outputs[0], blend.inputs[0])
        links.new(transparent.outputs[0], blend.inputs[1])
        links.new(emission.outputs[0], blend.inputs[2])
    output = nodes.new('ShaderNodeOutputMaterial')
    links.new(blend.outputs[0], output.inputs[0])
    if hasattr(material, 'surface_render_method'):
        material.surface_render_method = 'BLENDED'
    else:
        material.blend_method = 'BLEND'
    if hasattr(material, 'use_transparent_shadow'):
        material.use_transparent_shadow = False
    material['g4_effect_preview'] = 'FAKE_PARTICLE'


@persistent
def refresh_particle_cameras(scene, *unused) -> None:
    for obj in scene.objects:
        for modifier in obj.modifiers:
            if modifier.type == 'NODES' and modifier.node_group and modifier.node_group.get('g4_particle_geometry'):
                socket = modifier.node_group.nodes['Particle Camera'].inputs['Object']
                if socket.default_value != scene.camera:
                    socket.default_value = scene.camera


@persistent
def load_particle_cameras(*unused) -> None:
    for scene in bpy.data.scenes:
        upgrade_particle_preview_windows(scene)
        refresh_particle_cameras(scene)
        migrate_particle_library_clocks(scene)


def register() -> None:
    for handlers, callback in ((bpy.app.handlers.frame_change_pre, refresh_particle_cameras),
                               (bpy.app.handlers.render_pre, refresh_particle_cameras),
                               (bpy.app.handlers.load_post, load_particle_cameras)):
        if callback not in handlers:
            handlers.append(callback)


def unregister() -> None:
    for handlers, callback in ((bpy.app.handlers.frame_change_pre, refresh_particle_cameras),
                               (bpy.app.handlers.render_pre, refresh_particle_cameras),
                               (bpy.app.handlers.load_post, load_particle_cameras)):
        if callback in handlers:
            handlers.remove(callback)
