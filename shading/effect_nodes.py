"""Texture and threshold effect nodes with animation inputs; scene-depth fading is not reconstructed."""
from pathlib import Path

import bpy

from ..effects.materials import (
    BALL_AURA_SHADER_HASH, DIFFERENCE_FLOW_HASHES, T1_FLOW2_SHADER_HASH, T1M1_FLOW_SHADER_HASH, T1M1_RIM_SHADER_HASH,
    T1M1_AREA_SHADER_HASH, T2_SHADER_HASH, T2M1_SHADER_HASH, T3_THRESHOLD_NO_FLOW_HASH,
    T1M1_MORPH_SHADER_HASH, VDISP_RIM_SHADER_HASH, static_effect_material,
)
from .character_lighting import color_transfer


def disable_effect_transparent_shadows(materials) -> int:
    changed = 0
    for material in materials:
        if not material or not (material.get('g4_effect_preview') or material.get('g4_effect_shader_family')):
            continue
        if hasattr(material, 'use_transparent_shadow') and material.use_transparent_shadow:
            material.use_transparent_shadow = False
            changed += 1
    return changed


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

    def sample_texture(slot, settings, coordinates, displacement=None):
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
        source_uv = uv.outputs[0]
        if displacement is not None:
            offset = nodes.new('ShaderNodeVectorMath')
            offset.operation = 'ADD'
            links.new(source_uv, offset.inputs[0])
            links.new(displacement, offset.inputs[1])
            source_uv = offset.outputs[0]
        row_u, row_v = settings.uv_rows
        if (row_u == (1.0, 0.0, 0.0) and row_v == (0.0, 1.0, 0.0)):
            coordinates = source_uv
            if not native_addressing:
                split = nodes.new('ShaderNodeSeparateXYZ')
                links.new(coordinates, split.inputs[0])
                combined = nodes.new('ShaderNodeCombineXYZ')
                for index, mode in enumerate(settings.address_modes):
                    links.new(address_coordinate(split.outputs[index], mode), combined.inputs[index])
                coordinates = combined.outputs[0]
            links.new(coordinates, texture.inputs['Vector'])
            return texture
        split_uv = nodes.new('ShaderNodeSeparateXYZ')
        links.new(source_uv, split_uv.inputs[0])
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
    color = nodes.new('ShaderNodeVectorMath')
    color.operation = 'MULTIPLY'
    links.new(vertex.outputs['Color'], color.inputs[0])
    tint = nodes.new('ShaderNodeRGB')
    tint.name = 'Effect Diffuse Tint'
    tint.outputs[0].default_value = (*tuple(value * parameters.gain for value in parameters.diffuse[:3]), 1.0)
    links.new(tint.outputs[0], color.inputs[1])
    diffuse_alpha = nodes.new('ShaderNodeValue')
    diffuse_alpha.name = 'Effect Diffuse A'
    diffuse_alpha.outputs[0].default_value = parameters.diffuse[3]
    def saturate(value):
        source = getattr(value, 'node', None)
        if source is not None and source.bl_idname == 'ShaderNodeMath':
            source.use_clamp = True
            return value
        return math('MINIMUM', math('MAXIMUM', value, 0.0), 1.0)

    def red(texture):
        channels = nodes.new('ShaderNodeSeparateColor')
        links.new(texture.outputs['Color'], channels.inputs[0])
        return channels.outputs['Red']

    def parameter(index, component):
        node = nodes.new('ShaderNodeValue')
        node.name = f'Effect Parameter {index} {"XYZW"[component]}'
        node.outputs[0].default_value = record['shader_parameters'][index][component]
        return node.outputs[0]

    shader = record.get('shader_hash')
    if shader in (T2_SHADER_HASH, T2M1_SHADER_HASH):
        first, second = (sample_texture(i, parameters.textures[i], None) for i in range(2))
        second_alpha = second.outputs['Alpha']
        first_weight = math('SUBTRACT', 1.0, math('MULTIPLY', second_alpha, parameter(0, 3)))
        layers = []
        for sample, weight in ((first, first_weight), (second, second_alpha)):
            weighted = nodes.new('ShaderNodeVectorMath')
            weighted.operation = 'SCALE'
            links.new(sample.outputs['Color'], weighted.inputs[0])
            links.new(weight, weighted.inputs['Scale'])
            layers.append(weighted.outputs[0])
        composite = nodes.new('ShaderNodeMixRGB')
        composite.blend_type = 'ADD'
        composite.use_clamp = True
        composite.inputs[0].default_value = 1.0
        for index, layer in enumerate(layers, 1):
            links.new(layer, composite.inputs[index])
        texture_color = composite.outputs[0]
        coverage = saturate(math('ADD', first.outputs['Alpha'], second_alpha))
        if shader == T2M1_SHADER_HASH:
            coverage = math('MULTIPLY', coverage, red(sample_texture(2, parameters.textures[2], None)))
        alpha = math('MULTIPLY', coverage, math('MULTIPLY', vertex.outputs['Alpha'], diffuse_alpha.outputs[0]))
    elif shader in DIFFERENCE_FLOW_HASHES:
        # Preview the difference and palette using the native texture slots.
        flow = sample_texture(3, parameters.textures[3], None)
        displacement = nodes.new('ShaderNodeVectorMath')
        displacement.operation = 'MULTIPLY_ADD'
        strength = (record.get('shader_parameters') or [(0.0,)])[0][0]
        links.new(flow.outputs['Color'], displacement.inputs[0])
        displacement.inputs[1].default_value = (2 * strength, -2 * strength, 0)
        displacement.inputs[2].default_value = (-strength, strength, 0)
        first = sample_texture(0, parameters.textures[0], None, displacement.outputs[0])
        second = sample_texture(1, parameters.textures[1], None)
        difference = saturate(math('SUBTRACT', red(first), red(second)))
        uv = nodes.new('ShaderNodeCombineXYZ')
        links.new(difference, uv.inputs['X'])
        palette = sample_texture(2, parameters.textures[2], uv.outputs[0])
        texture_color = palette.outputs['Color']
        alpha = math('MULTIPLY', difference, palette.outputs['Alpha'])
        alpha = math('MULTIPLY', alpha, math('MULTIPLY', vertex.outputs['Alpha'], diffuse_alpha.outputs[0]))
        if len(parameters.textures) == 5:
            alpha = math('MULTIPLY', alpha, red(sample_texture(4, parameters.textures[4], None)))
    elif shader == T3_THRESHOLD_NO_FLOW_HASH:
        # Preview the five material inputs without framebuffer depth.
        mask = red(sample_texture(3, parameters.textures[3], None))
        coverage = red(sample_texture(4, parameters.textures[4], None))
        samples = [sample_texture(i, parameters.textures[i], None) for i in range(3)]
        texture_color = samples[2].outputs['Color']
        texture_alpha = samples[2].outputs['Alpha']
        for index in (1, 0):
            row = record['shader_parameters'][index]
            threshold = nodes.new('ShaderNodeValue')
            threshold.name = f'Effect Parameter {index} Y'
            threshold.outputs[0].default_value = row[1]
            weight = saturate(math('DIVIDE', math('SUBTRACT', mask,
                math('SUBTRACT', 1.0, threshold.outputs[0])), max(0.01, 1.0 - row[0])))
            mix = nodes.new('ShaderNodeMixRGB')
            links.new(weight, mix.inputs[0])
            links.new(texture_color, mix.inputs[1])
            links.new(samples[index].outputs['Color'], mix.inputs[2])
            texture_color = mix.outputs[0]
            texture_alpha = math('ADD', texture_alpha, math('MULTIPLY', weight,
                math('SUBTRACT', samples[index].outputs['Alpha'], texture_alpha)))
        alpha = math('MULTIPLY', math('MULTIPLY', coverage, texture_alpha),
                     math('MULTIPLY', vertex.outputs['Alpha'], diffuse_alpha.outputs[0]))
    elif len(parameters.textures) == 6:
        p = record['shader_parameters']
        animated_parameters = {}
        for index in (1, 2):
            node = nodes.new('ShaderNodeValue')
            node.name = f'Effect Parameter {index} Y'
            node.outputs[0].default_value = p[index][1]
            animated_parameters[index] = node.outputs[0]
        controls = nodes.new('ShaderNodeVertexColor')
        controls.layer_name = 'G4 Particle Color'
        channels = nodes.new('ShaderNodeSeparateColor')
        links.new(controls.outputs['Color'], channels.inputs[0])

        def vector(x, y):
            node = nodes.new('ShaderNodeCombineXYZ')
            for socket, value in zip(node.inputs, (x, y, 1.0)):
                if isinstance(value, (int, float)):
                    socket.default_value = value
                else:
                    links.new(value, socket)
            return node.outputs[0]

        def uv(slot):
            node = nodes.new('ShaderNodeUVMap')
            node.uv_map = 'UVMap' if slot == 0 else f'UVMap{slot}'
            split = nodes.new('ShaderNodeSeparateXYZ')
            links.new(node.outputs[0], split.inputs[0])
            return vector(split.outputs[0], split.outputs[1])

        def affine(slot, coordinates):
            if parameters.textures[slot].uv_rows == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
                return coordinates
            result = []
            for axis, row in enumerate(parameters.textures[slot].uv_rows):
                dot = nodes.new('ShaderNodeVectorMath')
                dot.operation = 'DOT_PRODUCT'
                dot.name = f'Effect UV {slot} Row {axis}'
                links.new(coordinates, dot.inputs[0])
                dot.inputs[1].default_value = row
                result.append(dot.outputs['Value'])
            return vector(*result)

        def displace(coordinates, direction, amount):
            node = nodes.new('ShaderNodeVectorMath')
            node.operation = 'MULTIPLY_ADD'
            links.new(direction, node.inputs[0])
            if isinstance(amount, (int, float)):
                node.inputs[1].default_value = (amount, amount, amount)
            else:
                links.new(amount, node.inputs[1])
            links.new(coordinates, node.inputs[2])
            return node.outputs[0]

        def flow_direction(texture):
            # Keep Z zero: affine UV rows consume the homogeneous Z=1 of
            # coordinates after displacement, not the flow texture's blue.
            node = nodes.new('ShaderNodeVectorMath')
            node.operation = 'MULTIPLY_ADD'
            node.inputs[1].default_value = (2.0, 2.0, 0.0)
            node.inputs[2].default_value = (-1.0, -1.0, 0.0)
            links.new(texture.outputs['Color'], node.inputs[0])
            return node.outputs[0]

        flow = flow_direction(sample_texture(5, parameters.textures[5], affine(5, uv(5))))
        mask_uv = displace(affine(3, uv(3)), flow,
                           math('MULTIPLY', controls.outputs['Alpha'], p[5][0]))
        mask = sample_texture(3, parameters.textures[3], mask_uv)
        direction = flow_direction(mask)
        color_uv = displace(uv(0), flow, math('MULTIPLY', controls.outputs['Alpha'], p[4][3]))
        color_uv = affine(0, displace(color_uv, direction, p[4][1]))
        coverage_uv = displace(uv(4), direction, p[4][2])
        coverage_uv = affine(4, displace(coverage_uv, flow, math('MULTIPLY', controls.outputs['Alpha'], p[5][1])))
        coverage = red(sample_texture(4, parameters.textures[4], coverage_uv))
        geometry = nodes.new('ShaderNodeNewGeometry')
        dot = nodes.new('ShaderNodeVectorMath')
        dot.operation = 'DOT_PRODUCT'
        links.new(geometry.outputs['Normal'], dot.inputs[0])
        links.new(geometry.outputs['Incoming'], dot.inputs[1])
        facing_range = min(max(p[5][3] + p[6][0], 0.0), 1.0)
        facing = math('MINIMUM', math('DIVIDE', math('ABSOLUTE', dot.outputs['Value'], 0.0), facing_range), 1.0) if facing_range else 1.0
        facing = math('MAXIMUM', math('DIVIDE', math('SUBTRACT', facing, p[6][0]), 1.0-p[6][0]), 0.0)
        threshold_alpha = math('ADD', 1.0, math('MULTIPLY', p[2][2], math('SUBTRACT', vertex.outputs['Alpha'], 1.0)))
        amount = math('MULTIPLY', mask.outputs['Alpha'], math('MULTIPLY', facing, math('MULTIPLY', threshold_alpha, animated_parameters[2])))
        weights = []
        for i, boundary in enumerate((2.0-p[0][1], math('SUBTRACT', 2.0, animated_parameters[1]), 1.0)):
            threshold = math('SUBTRACT', boundary, math('MULTIPLY', amount, channels.outputs[i]))
            weights.append(saturate(math('DIVIDE', math('SUBTRACT', coverage, threshold), min(max(1.0-p[i][0], .01), 1.0))))
        samples = [sample_texture(i, parameters.textures[i], color_uv) for i in range(3)]
        first = nodes.new('ShaderNodeMixRGB')
        links.new(weights[0], first.inputs[0])
        links.new(samples[1].outputs['Color'], first.inputs[1])
        links.new(samples[0].outputs['Color'], first.inputs[2])
        second = nodes.new('ShaderNodeMixRGB')
        links.new(weights[1], second.inputs[0])
        links.new(samples[2].outputs['Color'], second.inputs[1])
        links.new(first.outputs[0], second.inputs[2])
        texture_color = second.outputs[0]
        def lerp(a, b, weight):
            node = nodes.new('ShaderNodeMix')
            node.data_type = 'FLOAT'
            node.clamp_factor = False
            for socket, value in zip((node.inputs[0], node.inputs[2], node.inputs[3]), (weight, a, b)):
                if isinstance(value, (int, float)):
                    socket.default_value = value
                else:
                    links.new(value, socket)
            return node.outputs[0]
        opacity = lerp(vertex.outputs['Alpha'], 1.0, p[2][2])
        alpha = lerp(samples[1].outputs['Alpha'], samples[0].outputs['Alpha'], weights[0])
        alpha = lerp(samples[2].outputs['Alpha'], alpha, weights[1])
        alpha = math('MULTIPLY', alpha, math('MULTIPLY', weights[2], math('MULTIPLY', opacity, diffuse_alpha.outputs[0])))
    elif parameters.threshold is not None:
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
        alpha = math('MULTIPLY', math('MULTIPLY', coverage, opacity_alpha), diffuse_alpha.outputs[0])
        alpha = math('MULTIPLY', alpha, texture.outputs['Alpha'])
    else:
        displacement = None
        if shader in (T1_FLOW2_SHADER_HASH, T1M1_FLOW_SHADER_HASH):
            native = record['shader_parameters'][0]
            strength = nodes.new('ShaderNodeValue')
            strength.name = 'Effect Parameter 0 X'
            strength.outputs[0].default_value = native[0]
            directions = []
            for slot in ((1, 2) if shader == T1_FLOW2_SHADER_HASH else (2,)):
                flow = sample_texture(slot, parameters.textures[slot], None)
                direction = nodes.new('ShaderNodeVectorMath')
                direction.operation = 'MULTIPLY_ADD'
                links.new(flow.outputs['Color'], direction.inputs[0])
                direction.inputs[1].default_value = (2.0, -2.0, 0.0)
                direction.inputs[2].default_value = (-1.0, 1.0, 0.0)
                directions.append(direction.outputs[0])
            offset = directions[0]
            if shader == T1_FLOW2_SHADER_HASH:
                weight = nodes.new('ShaderNodeValue')
                weight.name = 'Effect Parameter 0 W'
                weight.outputs[0].default_value = native[3]
                weighted = nodes.new('ShaderNodeVectorMath')
                weighted.operation = 'MULTIPLY_ADD'
                links.new(directions[1], weighted.inputs[0])
                links.new(weight.outputs[0], weighted.inputs[1])
                links.new(offset, weighted.inputs[2])
                offset = weighted.outputs[0]
            scale = nodes.new('ShaderNodeVectorMath')
            scale.operation = 'SCALE'
            links.new(offset, scale.inputs[0])
            links.new(strength.outputs[0], scale.inputs['Scale'])
            displacement = scale.outputs[0]
        texture = sample_texture(0, parameters.textures[0], None, displacement)
        alpha = math('MULTIPLY', vertex.outputs['Alpha'], diffuse_alpha.outputs[0])
        alpha = math('MULTIPLY', texture.outputs['Alpha'], math('MINIMUM', alpha, 1.0))
        if len(parameters.textures) == 2 or shader in (T1M1_FLOW_SHADER_HASH, T1M1_RIM_SHADER_HASH):
            mask = sample_texture(1, parameters.textures[1], None)
            alpha = math('MULTIPLY', alpha, red(mask))
        if shader == T1M1_AREA_SHADER_HASH:
            near, far = parameter(2, 0), parameter(2, 1)
            distance = nodes.new('ShaderNodeCameraData').outputs['View Distance']
            fade = saturate(math('DIVIDE', math('SUBTRACT', distance, near), math('SUBTRACT', far, near)))
            alpha = math('MULTIPLY', alpha, math('SUBTRACT', 1.0, fade))
        if shader in (T1M1_RIM_SHADER_HASH, VDISP_RIM_SHADER_HASH):
            # Approximate the rim with a view-facing lookup and its native mask.
            facing = nodes.new('ShaderNodeLayerWeight')
            coords = nodes.new('ShaderNodeCombineXYZ')
            links.new(facing.outputs['Facing'], coords.inputs['X'])
            ramp_slot = 2 if shader == T1M1_RIM_SHADER_HASH else 1
            ramp = sample_texture(ramp_slot, parameters.textures[ramp_slot], coords.outputs[0])
            rim_mask = sample_texture(ramp_slot + 1, parameters.textures[ramp_slot + 1], None)
            rim = nodes.new('ShaderNodeMixRGB')
            rim.blend_type = 'MULTIPLY'
            links.new(red(rim_mask), rim.inputs[0])
            links.new(texture.outputs['Color'], rim.inputs[1])
            links.new(ramp.outputs['Color'], rim.inputs[2])
            texture_color = rim.outputs[0]
    if (len(parameters.textures) < 5 and shader not in
            (T1M1_RIM_SHADER_HASH, VDISP_RIM_SHADER_HASH, T2_SHADER_HASH, T2M1_SHADER_HASH)
            and shader not in DIFFERENCE_FLOW_HASHES):
        texture_color = texture.outputs['Color']
    product = nodes.new('ShaderNodeVectorMath')
    product.operation = 'MULTIPLY'
    links.new(texture_color, product.inputs[0])
    links.new(color.outputs[0], product.inputs[1])
    emission = nodes.new('ShaderNodeEmission')
    links.new(color_transfer(nodes, links, product.outputs[0], encode=False, approximate=True), emission.inputs['Color'])
    if parameters.additive:
        links.new(alpha, emission.inputs['Strength'])
        blend = nodes.new('ShaderNodeAddShader')
        transparent = nodes.new('ShaderNodeBsdfTransparent')
        links.new(transparent.outputs[0], blend.inputs[0])
        links.new(emission.outputs[0], blend.inputs[1])
    else:
        transparent = nodes.new('ShaderNodeBsdfTransparent')
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
    if hasattr(material, 'use_transparent_shadow'):
        material.use_transparent_shadow = False
    if shader in (T2_SHADER_HASH, T2M1_SHADER_HASH):
        material['g4_effect_preview'] = 'T2' if shader == T2_SHADER_HASH else 'T2M1'
    elif shader == T1M1_AREA_SHADER_HASH:
        material['g4_effect_preview'] = 'T1M1_AREA_PREVIEW'
    elif shader in DIFFERENCE_FLOW_HASHES:
        material['g4_effect_preview'] = 'DIFFERENCE_FLOW_PREVIEW'
    elif shader == T1M1_MORPH_SHADER_HASH:
        material['g4_effect_preview'] = 'T1M1_MORPH_SURFACE'
    elif shader == VDISP_RIM_SHADER_HASH:
        material['g4_effect_preview'] = 'VDISP_RIM_SURFACE'
    elif shader == BALL_AURA_SHADER_HASH:
        material['g4_effect_preview'] = 'BALL_AURA_PREVIEW'
    elif shader in (T1_FLOW2_SHADER_HASH, T1M1_FLOW_SHADER_HASH):
        material['g4_effect_preview'] = 'T1_FLOW2' if shader == T1_FLOW2_SHADER_HASH else 'T1M1_FLOW'
    else:
        material['g4_effect_preview'] = {
            1: 'T1_STATIC', 2: 'T1M1_STATIC', 3: 'THRESHOLD_STATIC', 4: 'T1M1_RIM', 5: 'T3_THRESHOLD_PREVIEW', 6: 'T3_THRESHOLD'
        }[len(parameters.textures)]
    return True
