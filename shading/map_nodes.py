"""Compact native map inputs; EEVEE owns direct lighting, shadows and IBL."""
from __future__ import annotations

from pathlib import Path
import math

from .map_surfaces import NativeMapMaterial


def map_data_image(path: str):
    import bpy
    absolute = str(Path(path).resolve())
    for image in bpy.data.images:
        if image.get('g4_map_data_texture') and bpy.path.abspath(image.filepath) == absolute:
            return image
    source = bpy.data.images.load(absolute, check_existing=True)
    image = source.copy()
    image.name = source.name + ' (Map Data)'
    image.colorspace_settings.name = 'Non-Color'
    image.alpha_mode = 'CHANNEL_PACKED'
    image['g4_map_data_texture'] = True
    return image


def build_native_map_material(material, profile: NativeMapMaterial, vertex_layer: str | None) -> bool:
    import bpy
    if any(not Path(path).is_file() for path in profile.textures):
        return False
    images = [map_data_image(path) for path in profile.textures]
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()

    def node(kind, name):
        result = nodes.new(kind)
        result.name = name
        return result

    def math_node(name, operation, *values):
        result = node('ShaderNodeMath', name)
        result.operation = operation
        for socket, value in zip(result.inputs, values):
            if isinstance(value, (int, float)):
                socket.default_value = value
            else:
                links.new(value, socket)
        return result.outputs[0]

    def vector(name, operation, *values):
        result = node('ShaderNodeVectorMath', name)
        result.operation = operation
        for socket, value in zip(result.inputs, values):
            if isinstance(value, tuple):
                socket.default_value = value
            else:
                links.new(value, socket)
        return result.outputs['Value' if operation == 'DOT_PRODUCT' else 'Vector']

    def mix(name, data_type, factor, a, b):
        result = node('ShaderNodeMix', name)
        result.data_type = data_type
        for socket, value in zip((result.inputs[0], result.inputs[2 if data_type == 'FLOAT' else 6],
                                  result.inputs[3 if data_type == 'FLOAT' else 7]), (factor, a, b)):
            if isinstance(value, (int, float, tuple)):
                socket.default_value = value
            else:
                links.new(value, socket)
        return result.outputs[0 if data_type == 'FLOAT' else 2]

    uv = node('ShaderNodeUVMap', 'Map UV')
    uv.uv_map = 'UVMap'
    coordinates = uv.outputs['UV']
    def project_uv(source, rows, suffix):
        if rows == ((1., 0., 0.), (0., 1., 0.)):
            return source
        origin = node('ShaderNodeSeparateXYZ', 'Map UV Components' + suffix)
        links.new(source, origin.inputs[0])
        homogeneous = node('ShaderNodeCombineXYZ', 'Map Homogeneous UV' + suffix)
        links.new(origin.outputs['X'], homogeneous.inputs['X'])
        links.new(origin.outputs['Y'], homogeneous.inputs['Y'])
        homogeneous.inputs['Z'].default_value = 1.
        projected = node('ShaderNodeCombineXYZ', 'Map Native UV Transform' + suffix)
        for index, row in enumerate(rows):
            links.new(vector(f'Map UV Row {index}' + suffix, 'DOT_PRODUCT', homogeneous.outputs[0], row), projected.inputs[index])
        return projected.outputs[0]
    coordinates = project_uv(coordinates, profile.uv_rows, '')
    grass_uv = coordinates
    if profile.grass:
        ground_uv = node('ShaderNodeUVMap', 'Map Ground UV')
        ground_uv.uv_map = 'UVMap1'
        grass_uv = project_uv(ground_uv.outputs['UV'], profile.secondary_uv_rows, ' Ground')
    pair_coordinates = []
    for index, scale in enumerate(profile.layer_scales):
        if index == 1 and profile.family != 'Map_PbrT2':
            break
        if scale == 1.:
            pair_coordinates.append(coordinates)
        else:
            pair_coordinates.append(vector(f'Map Layer UV {index}', 'MULTIPLY_ADD', coordinates,
                                           (scale, scale, 1.), (0., 1. - scale, 0.)))
    textures = []
    for index, image in enumerate(images):
        texture = node('ShaderNodeTexImage', f'Map Texture {index}')
        texture.image = image
        texture.interpolation = 'Linear'
        texture.extension = profile.addressing[index]
        texture_uv = pair_coordinates[index // 2] if profile.family == 'Map_PbrT2' else grass_uv if profile.grass and index == 1 else coordinates
        links.new(texture_uv, texture.inputs['Vector'])
        textures.append(texture)
    vertex = None
    if vertex_layer:
        vertex = node('ShaderNodeVertexColor', 'Map Vertex Tint')
        vertex.layer_name = vertex_layer
    if profile.grass and profile.grass[1][1] > 0.:
        geometry = node('ShaderNodeNewGeometry', 'Map Parallax View')
        tangent = node('ShaderNodeTangent', 'Map Parallax Tangent')
        tangent.direction_type = 'UV_MAP'
        tangent.uv_map = 'UVMap'
        bitangent = node('ShaderNodeNormalMap', 'Map Parallax Bitangent')
        bitangent.uv_map = 'UVMap'
        bitangent.inputs['Color'].default_value = (.5, 1., .5, 1.)
        view = geometry.outputs['Incoming']
        facing = vector('Map Parallax Facing', 'DOT_PRODUCT', view, geometry.outputs['Normal'])
        facing = math_node('Map Parallax Front', 'MAXIMUM', facing, 0.)
        # Native (1 - (1 - N.V)^2) / abs(N.V) simplifies to 2 - N.V on front faces.
        gain = math_node('Map Parallax Angle', 'SUBTRACT', 2., facing)
        gain = math_node('Map Parallax Depth', 'MULTIPLY', gain, profile.grass[1][1])
        if vertex:
            gain = math_node('Map Parallax Vertex Depth', 'MULTIPLY', gain, vertex.outputs['Alpha'])
        direction = node('ShaderNodeCombineXYZ', 'Map Parallax Direction')
        for index, axis in enumerate((tangent.outputs['Tangent'], bitangent.outputs['Normal'])):
            links.new(vector(f'Map Parallax Axis {index}', 'DOT_PRODUCT', view, axis), direction.inputs[index])
        ray = vector('Map Parallax Ray', 'SCALE', direction.outputs[0])
        # VectorMath SCALE uses its fourth input, unlike binary vector operations.
        links.new(gain, nodes['Map Parallax Ray'].inputs['Scale'])
        step = vector('Map Parallax Step', 'SCALE', ray)
        nodes['Map Parallax Step'].inputs['Scale'].default_value = -1. / 16.
        previous_uv, result_uv, found = coordinates, coordinates, 0.
        def height_sample(name, uv_socket):
            texture = node('ShaderNodeTexImage', name)
            texture.image = images[0]
            texture.interpolation = 'Linear'
            texture.extension = profile.addressing[0]
            links.new(uv_socket, texture.inputs['Vector'])
            return texture.outputs['Alpha']
        previous_error = math_node('Map Parallax Initial Error', 'SUBTRACT', height_sample('Map Parallax Height 0', coordinates), 1.)
        for layer in range(1, 17):
            candidate = vector(f'Map Parallax Candidate {layer}', 'ADD', previous_uv, step)
            error = math_node(f'Map Parallax Error {layer}', 'SUBTRACT', height_sample(f'Map Parallax Height {layer}', candidate), 1. - layer / 16.)
            below = math_node(f'Map Parallax Below {layer}', 'LESS_THAN', error, 0.)
            hit = math_node(f'Map Parallax Hit {layer}', 'SUBTRACT', 1., below)
            active = math_node(f'Map Parallax Active {layer}', 'SUBTRACT', 1., found)
            first = math_node(f'Map Parallax First {layer}', 'MULTIPLY', active, hit)
            difference = math_node(f'Map Parallax Difference {layer}', 'SUBTRACT', previous_error, error)
            fraction = math_node(f'Map Parallax Fraction {layer}', 'DIVIDE', previous_error, difference)
            nodes[f'Map Parallax Fraction {layer}'].use_clamp = True
            offset = vector(f'Map Parallax Offset {layer}', 'SCALE', step)
            links.new(fraction, nodes[f'Map Parallax Offset {layer}'].inputs['Scale'])
            crossing = vector(f'Map Parallax Crossing {layer}', 'ADD', previous_uv, offset)
            delta = vector(f'Map Parallax Delta {layer}', 'SUBTRACT', crossing, result_uv)
            result_uv = vector('Map Parallax UV' if layer == 16 else f'Map Parallax Result {layer}', 'MULTIPLY_ADD', delta, first, result_uv)
            found = math_node(f'Map Parallax Found {layer}', 'MAXIMUM', found, hit)
            previous_uv, previous_error = candidate, error
        links.new(result_uv, textures[0].inputs['Vector'])
    base, alpha = textures[0].outputs['Color'], textures[0].outputs['Alpha']
    normal = None
    roughness, metallic = None, None
    if profile.family.startswith('Map_Pbr'):
        packed = []
        for slot in ((1, 3) if profile.family == 'Map_PbrT2' else (1,)):
            split = node('ShaderNodeSeparateColor', f'Map Surface Channels {slot}')
            links.new(textures[slot].outputs['Color'], split.inputs[0])
            packed.append(split)
        roughness, metallic = packed[0].outputs['Blue'], textures[1].outputs['Alpha']
        strength = profile.normal_scale
        if profile.family == 'Map_PbrT2':
            height = math_node('Map Height Offset', 'SUBTRACT', textures[2].outputs['Alpha'], 1.)
            weight = math_node('Map Layer Weight', 'MULTIPLY_ADD', vertex.outputs['Alpha'] if vertex else 1., 2., height)
            nodes['Map Layer Weight'].use_clamp = True
            weight = math_node('Map Layer Blend', 'POWER', weight, profile.layer_power)
            base = mix('Map Layer Albedo', 'RGBA', weight, base, textures[2].outputs['Color'])
            alpha = mix('Map Layer Occlusion', 'FLOAT', weight, alpha, textures[2].outputs['Alpha'])
            roughness = mix('Map Layer Roughness', 'FLOAT', weight, roughness, packed[1].outputs['Blue'])
            metallic = mix('Map Layer Metal', 'FLOAT', weight, metallic, textures[3].outputs['Alpha'])
            normals = []
            for slot in (1, 3):
                slope = vector(f'Map Slope {slot}', 'MULTIPLY_ADD', textures[slot].outputs['Color'],
                               (2.*strength, 2.*strength, 0.), (-strength, -strength, 1.))
                slope = vector(f'Map Unit Slope {slot}', 'NORMALIZE', slope)
                normals.append(vector(f'Map Normal Gain {slot}', 'MULTIPLY', slope, (strength, strength, 1.)))
            # The native shader blends the unnormalized slopes before the TBN transform.
            delta = vector('Map Normal Delta', 'SUBTRACT', normals[1], normals[0])
            slope = vector('Map Layer Normal', 'MULTIPLY_ADD', delta, weight, normals[0])
            encoded = vector('Map Encode Normal', 'MULTIPLY_ADD', slope, (.5, .5, .5), (.5, .5, .5))
        else:
            gain = strength * strength
            encoded = vector('Map Encode Normal', 'MULTIPLY_ADD', textures[1].outputs['Color'],
                             (gain, gain, 0.), (.5 - .5*gain, .5 - .5*gain, 1.))
        normal_map = node('ShaderNodeNormalMap', 'Map Surface Normal')
        normal_map.uv_map = 'UVMap'
        links.new(encoded, normal_map.inputs['Color'])
        normal = normal_map.outputs['Normal']
    if profile.grass:
        tint = vector('Map Grass Diffuse', 'MULTIPLY', textures[1].outputs['Color'], profile.diffuse[:3])
        if vertex:
            tint = vector('Map Grass Vertex Color', 'MULTIPLY', tint, vertex.outputs['Color'])
        parameters = profile.grass
        height = math_node('Map Grass Height', 'SUBTRACT', 1., alpha)
        amount = math_node('Map Grass Ground Rate', 'MULTIPLY', textures[1].outputs['Alpha'], parameters[1][3])
        amount = math_node('Map Grass Tint Weight', 'MULTIPLY_ADD', amount, height, alpha)
        tint_color = vector('Map Grass Height Tint', 'MULTIPLY_ADD', amount,
                            tuple(1. - v for v in parameters[2][:3]), parameters[2][:3])
        tint = vector('Map Grass Painted Tint', 'MULTIPLY', tint, tint_color)
        ground = math_node('Map Grass Ground AO', 'MULTIPLY_ADD', textures[1].outputs['Alpha'],
                           parameters[1][2], 1. - parameters[1][2])
        blades = vector('Map Grass Blade Color', 'MULTIPLY', base, ground)
        inverse_tint = vector('Map Grass Inverse Tint', 'SUBTRACT', (1., 1., 1.), tint)
        inverse_blades = vector('Map Grass Inverse Blades', 'SUBTRACT', (1., 1., 1.), blades)
        color = vector('Map Grass Screen Product', 'MULTIPLY', inverse_tint, inverse_blades)
        color = vector('Map Grass Composite', 'SUBTRACT', (1., 1., 1.), color)
    else:
        color = vector('Map Diffuse Tint', 'MULTIPLY', base, profile.diffuse[:3])
        if vertex:
            color = vector('Map Painted Color', 'MULTIPLY', color, vertex.outputs['Color'])
    output = node('ShaderNodeOutputMaterial', 'Map Output')
    unlit = profile.family == 'MapNolight'
    surface = node('ShaderNodeEmission' if unlit else 'ShaderNodeBsdfPrincipled', 'Map Surface')
    links.new(color, surface.inputs['Color' if unlit else 'Base Color'])
    if not unlit:
        surface.inputs['Roughness'].default_value = 1.
        floor = profile.specular_floor * profile.specular_scale * .5
        root = math.sqrt(min(.99, floor))
        surface.inputs['IOR'].default_value = (1. + root) / (1. - root)
        if roughness is not None:
            links.new(roughness, surface.inputs['Roughness'])
            links.new(metallic, surface.inputs['Metallic'])
            links.new(normal, surface.inputs['Normal'])
        environment_ambient = bpy.context.scene.get('g4_map_ambient')
        if profile.ambient > 0. or environment_ambient is not None:
            occlusion = math_node('Map Baked Occlusion', 'MULTIPLY_ADD', alpha,
                                  1. - profile.occlusion_recovery, profile.occlusion_recovery)
            ambient = vector('Map Ambient Color', 'MULTIPLY', color, occlusion)
            links.new(ambient, surface.inputs['Emission Color'])
            # Capture light CB6[52].z; EEVEE replaces the separate native light lists.
            if environment_ambient is not None:
                ambient = vector('Map Environment Ambient', 'MULTIPLY', ambient, tuple(environment_ambient))
                links.new(ambient, surface.inputs['Emission Color'])
                surface.inputs['Emission Strength'].default_value = 1.
            else:
                surface.inputs['Emission Strength'].default_value = profile.ambient * 1.2
    opacity = None
    if profile.alpha_slot is not None:
        mask = node('ShaderNodeSeparateColor', 'Map Alpha Mask')
        links.new(textures[profile.alpha_slot].outputs['Color'], mask.inputs[0])
        opacity = math_node('Map Alpha Cutout', 'LESS_THAN', mask.outputs['Red'], profile.alpha_cutoff)
        opacity = math_node('Map Alpha Coverage', 'SUBTRACT', 1., opacity)
    elif unlit and profile.blended:
        opacity = math_node('Map Alpha', 'MULTIPLY', alpha, profile.diffuse[3])
        if vertex:
            opacity = math_node('Map Vertex Alpha', 'MULTIPLY', opacity, vertex.outputs['Alpha'])
    if opacity is not None and unlit:
        transparent = node('ShaderNodeBsdfTransparent', 'Map Transparent')
        blend = node('ShaderNodeMixShader', 'Map Alpha Surface')
        links.new(opacity, blend.inputs[0]);links.new(transparent.outputs[0], blend.inputs[1]);links.new(surface.outputs[0], blend.inputs[2])
        result = blend.outputs[0]
    else:
        if opacity is not None:
            links.new(opacity, surface.inputs['Alpha'])
        result = surface.outputs[0]
    links.new(result, output.inputs['Surface'])
    if hasattr(material, 'surface_render_method'):
        material.surface_render_method = 'BLENDED' if unlit and profile.blended else 'DITHERED'
    elif opacity is not None:
        material.blend_method = 'HASHED'
    material['g4_map_native_shader'] = profile.family
    material['g4_map_surface_fidelity'] = 'approximate'
    material['g4_map_surface_evidence'] = ('Native grass color and 16-layer parallax with intersection refinement; EEVEE lighting' if profile.grass else 'DXBC map material inputs; EEVEE lighting/IBL; captured ambient scale')
    material.pop('g4_level5_toon', None)
    material.pop('g4_shader_family', None)
    # Keep the graph readable without expanding it into a second renderer.
    for index, item in enumerate(nodes):
        item.location = ((index % 6)*220 - 1000, -(index // 6)*230)
    return True
