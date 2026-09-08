"""Texture and threshold effect nodes with animation inputs; scene-depth fading is not reconstructed."""
from pathlib import Path

import bpy

from ..effects.materials import static_effect_material
from .character_lighting import color_transfer


def effect_data_image(path: str) -> bpy.types.Image:
    source = bpy.data.images.load(path, check_existing=True)
    for image in bpy.data.images:
        if image.get('g4_effect_data_texture') and image.filepath == source.filepath:
            return image
    # Captured BC7_UNORM samples interpolate RGB and alpha independently.
    # Keep the ordinary image intact for materials outside the effect adapter.
    image = source.copy()
    image.name = f'{source.name} (Effect Data)'
    image.colorspace_settings.name = 'Non-Color'
    image.alpha_mode = 'CHANNEL_PACKED'
    image['g4_effect_data_texture'] = True
    return image


def build_static_effect_material(material: bpy.types.Material, record: dict) -> bool:
    parameters = static_effect_material(record)
    references = record.get('texture_refs', [])
    if parameters is None:
        return False
    texture_paths = [reference.get('texture_path') for reference in references]
    if any(not path or not Path(path).is_file() for path in texture_paths):
        return False
    images = [effect_data_image(str(path)) for path in texture_paths]
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()

    def math(operation, a, b):
        node = nodes.new('ShaderNodeMath')
        node.operation = operation
        for socket, value in zip(node.inputs, (a, b)):
            if isinstance(value, (float, int)):
                socket.default_value = value
            else:
                links.new(value, socket)
        return node.outputs[0]

    def address_coordinate(coordinate, mode):
        if mode == 'REPEAT':
            return math('FRACT', coordinate, 0.0)
        if mode == 'MIRROR':
            return math('PINGPONG', coordinate, 1.0)
        return math('MINIMUM', math('MAXIMUM', coordinate, 0.0), 1.0)

    def sample_texture(slot, settings, coordinates):
        texture = nodes.new('ShaderNodeTexImage')
        texture.name = 'Effect Texture' if slot == 0 else f'Effect Mask {slot}'
        texture.image = images[slot]
        texture.interpolation = 'Linear'
        native_addressing = (
            settings.address_modes[0] == settings.address_modes[1]
            and settings.address_modes[0] in texture.bl_rna.properties['extension'].enum_items.keys()
        )
        texture.extension = settings.address_modes[0] if native_addressing else 'EXTEND'
        if coordinates is not None:
            if not native_addressing:
                split = nodes.new('ShaderNodeSeparateXYZ')
                links.new(coordinates, split.inputs[0])
                combined = nodes.new('ShaderNodeCombineXYZ')
                for index, mode in enumerate(settings.address_modes):
                    links.new(address_coordinate(split.outputs[index], mode), combined.inputs[index])
                coordinates = combined.outputs[0]
            links.new(coordinates, texture.inputs['Vector'])
            return texture
        uv = nodes.new('ShaderNodeUVMap')
        uv.uv_map = 'UVMap' if slot == 0 else f'UVMap{slot}'
        split_uv = nodes.new('ShaderNodeSeparateXYZ')
        links.new(uv.outputs[0], split_uv.inputs[0])
        affine_uv = nodes.new('ShaderNodeCombineXYZ')
        links.new(split_uv.outputs[0], affine_uv.inputs[0])
        links.new(split_uv.outputs[1], affine_uv.inputs[1])
        affine_uv.inputs[2].default_value = 1.0
        transformed_uv = nodes.new('ShaderNodeCombineXYZ')
        for index, row in enumerate(settings.uv_rows):
            dot = nodes.new('ShaderNodeVectorMath')
            dot.operation = 'DOT_PRODUCT'
            dot.name = f'Effect UV {slot} Row {index}'
            links.new(affine_uv.outputs[0], dot.inputs[0])
            dot.inputs[1].default_value = row
            coordinate = dot.outputs['Value']
            if not native_addressing:
                coordinate = address_coordinate(coordinate, settings.address_modes[index])
            links.new(coordinate, transformed_uv.inputs[index])
        links.new(transformed_uv.outputs[0], texture.inputs['Vector'])
        return texture

    vertex = nodes.new('ShaderNodeVertexColor')
    vertex.layer_name = 'G4 Outline Parameters'
    vertex.name = 'Effect Vertex Color'
    color = nodes.new('ShaderNodeMixRGB')
    color.blend_type = 'MULTIPLY'
    color.inputs[0].default_value = 1.0
    links.new(vertex.outputs['Color'], color.inputs[1])
    diffuse = []
    tint = nodes.new('ShaderNodeCombineXYZ')
    for index, value in enumerate(parameters.diffuse):
        node = nodes.new('ShaderNodeValue')
        node.name = f'Effect Diffuse {"RGBA"[index]}'
        node.outputs[0].default_value = value
        diffuse.append(node.outputs[0])
        if index < 3:
            links.new(math('MULTIPLY', node.outputs[0], parameters.gain), tint.inputs[index])
    links.new(tint.outputs[0], color.inputs[2])
    def saturate(value):
        return math('MINIMUM', math('MAXIMUM', value, 0.0), 1.0)

    def red(texture):
        channels = nodes.new('ShaderNodeSeparateColor')
        links.new(texture.outputs['Color'], channels.inputs[0])
        return channels.outputs['Red']

    if parameters.threshold is not None:
        vectors = []
        for index, values in enumerate(parameters.threshold):
            sockets = {}
            for component, value in enumerate(values):
                if index == 2 and component < 2:
                    continue
                node = nodes.new('ShaderNodeValue')
                node.name = f'Effect Parameter {index} {"XYZW"[component]}'
                node.outputs[0].default_value = value
                sockets[component] = node.outputs[0]
            vectors.append(sockets)
        p0, p1, p2 = vectors
        # Keep native branches in the graph so keyed parameters update without rebuilding it.
        alpha_controls_threshold = math('MAXIMUM', math('LESS_THAN', p2[2], 1.0),
                                        math('MULTIPLY', math('SUBTRACT', 1.0, math('LESS_THAN', p2[2], 2.0)),
                                             math('LESS_THAN', p2[2], 3.0)))
        threshold_alpha = math('ADD', 1.0, math('MULTIPLY', alpha_controls_threshold,
                                               math('SUBTRACT', vertex.outputs['Alpha'], 1.0)))
        opacity_alpha = math('ADD', vertex.outputs['Alpha'], math('MULTIPLY', alpha_controls_threshold,
                                                                 math('SUBTRACT', 1.0, vertex.outputs['Alpha'])))
        facing_range = saturate(math('ADD', p1[2], p1[3]))
        geometry = nodes.new('ShaderNodeNewGeometry')
        dot = nodes.new('ShaderNodeVectorMath')
        dot.operation = 'DOT_PRODUCT'
        links.new(geometry.outputs['Normal'], dot.inputs[0])
        links.new(geometry.outputs['Incoming'], dot.inputs[1])
        facing = math('MINIMUM', math('DIVIDE', math('ABSOLUTE', dot.outputs['Value'], 0.0), facing_range), 1.0)
        # Blender returns zero for division by zero; native saturating division yields one here.
        facing = math('MAXIMUM', facing, math('SUBTRACT', 1.0, math('GREATER_THAN', facing_range, 0.0)))
        facing = math('MAXIMUM', math('DIVIDE', math('SUBTRACT', facing, p1[3]),
                                      math('SUBTRACT', 1.0, p1[3])), 0.0)
        mask0 = sample_texture(1, parameters.textures[1], None)
        mask1 = sample_texture(2, parameters.textures[2], None)
        amount = math('MULTIPLY', math('MULTIPLY', red(mask0), p1[1]), math('MULTIPLY', facing, threshold_alpha))
        amount = math('ADD', math('SUBTRACT', red(mask1), 1.0), amount)
        first = saturate(math('DIVIDE', math('SUBTRACT', amount, math('SUBTRACT', 1.0, p0[1])), math('MAXIMUM', math('SUBTRACT', 1.0, p0[0]), 0.005)))
        second = saturate(math('DIVIDE', math('SUBTRACT', amount, math('SUBTRACT', 1.0, p0[3])), math('MAXIMUM', math('SUBTRACT', 1.0, p0[2]), 0.005)))
        gradient_uv = nodes.new('ShaderNodeCombineXYZ')
        gradient_uv.name = 'Effect Threshold Gradient UV'
        links.new(p2[3], gradient_uv.inputs[0])
        gradient_v = math('MULTIPLY', math('MULTIPLY', second, math('ADD', first, 1.0)), 0.5)
        links.new(gradient_v, gradient_uv.inputs[1])
        texture = sample_texture(0, parameters.textures[0], gradient_uv.outputs[0])
        coverage = saturate(math('DIVIDE', amount, math('MAXIMUM', math('SUBTRACT', 1.0, p1[0]), 0.005)))
        alpha = math('MULTIPLY', math('MULTIPLY', coverage, opacity_alpha), diffuse[3])
        alpha = math('MULTIPLY', alpha, texture.outputs['Alpha'])
    else:
        texture = sample_texture(0, parameters.textures[0], None)
        alpha = math('MULTIPLY', vertex.outputs['Alpha'], diffuse[3])
        alpha = math('MULTIPLY', texture.outputs['Alpha'], math('MINIMUM', alpha, 1.0))
        if len(parameters.textures) == 2:
            mask = sample_texture(1, parameters.textures[1], None)
            alpha = math('MULTIPLY', alpha, red(mask))
    product = nodes.new('ShaderNodeMixRGB')
    product.blend_type = 'MULTIPLY'
    product.inputs[0].default_value = 1.0
    links.new(texture.outputs['Color'], product.inputs[1])
    links.new(color.outputs[0], product.inputs[2])
    emission = nodes.new('ShaderNodeEmission')
    links.new(color_transfer(nodes, links, product.outputs[0], encode=False), emission.inputs['Color'])
    transparent = nodes.new('ShaderNodeBsdfTransparent')
    if parameters.additive:
        links.new(alpha, emission.inputs['Strength'])
        blend = nodes.new('ShaderNodeAddShader')
        links.new(transparent.outputs[0], blend.inputs[0])
        links.new(emission.outputs[0], blend.inputs[1])
    else:
        blend = nodes.new('ShaderNodeMixShader')
        links.new(alpha, blend.inputs[0])
        links.new(transparent.outputs[0], blend.inputs[1])
        links.new(emission.outputs[0], blend.inputs[2])
    output = nodes.new('ShaderNodeOutputMaterial')
    links.new(blend.outputs[0], output.inputs['Surface'])
    if hasattr(material, 'surface_render_method'):
        material.surface_render_method = 'BLENDED'
    else:
        material.blend_method = 'BLEND'
    material['g4_effect_preview'] = {1: 'T1_STATIC', 2: 'T1M1_STATIC', 3: 'THRESHOLD_STATIC'}[len(parameters.textures)]
    return True
