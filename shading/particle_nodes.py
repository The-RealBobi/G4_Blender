"""Base FakeParticle geometry; the explicit preview range is not the native emitter clock."""
from pathlib import Path

import bpy
from bpy.app.handlers import persistent

from .effect_nodes import effect_data_image
from .character_lighting import color_transfer


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
        links.new(math('ADD', corner, offset), result.inputs['Index'])
        return result.outputs['Value']

    def attribute(name, data_type):
        result = node('GeometryNodeInputNamedAttribute', data_type=data_type)
        result.inputs['Name'].default_value = name
        return sample(result.outputs['Attribute'], data_type, 0)

    position = node('GeometryNodeInputPosition').outputs[0]
    a, b, c = (sample(position, 'FLOAT_VECTOR', i) for i in range(3))
    center = vector('SCALE', vector('ADD', vector('ADD', a, b), c), 1 / 3)
    normal = vector('NORMALIZE', vector('CROSS_PRODUCT', vector('SUBTRACT', b, a), vector('SUBTRACT', c, a)), (0, 0, 0))
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
    radius = math('MULTIPLY', math('ABSOLUTE', math('MULTIPLY', vector('DISTANCE', a, b), p[0] * .5), 0), textures[1].outputs['Alpha'])
    tint = vector('MULTIPLY', attribute('G4 Particle Color', 'FLOAT_COLOR'), textures[0].outputs['Color'])
    tint = vector('MULTIPLY', tint, tuple(colors[i] * (1 + colors[4]) for i in range(3)))
    separate0 = node('FunctionNodeSeparateColor', mode='RGB')
    links.new(color0, separate0.inputs[0])
    separate1 = node('FunctionNodeSeparateColor', mode='RGB')
    links.new(attribute('G4 Particle Color', 'FLOAT_COLOR'), separate1.inputs[0])
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
    links.new(color_transfer(nodes, links, product.outputs[0], encode=False), emission.inputs[0])
    transparent = nodes.new('ShaderNodeBsdfTransparent')
    blend = nodes.new('ShaderNodeAddShader' if additive else 'ShaderNodeMixShader')
    if additive:
        links.new(alpha.outputs[0], emission.inputs['Strength'])
    else:
        links.new(alpha.outputs[0], blend.inputs[0])
    links.new(transparent.outputs[0], blend.inputs[-2])
    links.new(emission.outputs[0], blend.inputs[-1])
    output = nodes.new('ShaderNodeOutputMaterial')
    links.new(blend.outputs[0], output.inputs[0])
    material.surface_render_method = 'BLENDED'
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
        refresh_particle_cameras(scene)


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
