"""Character lighting reconstructed from the draw shaders in capture 16.

The two white gradient rows carry shadow coverage in alpha. Their RGB is not
an albedo tint. EEVEE supplies signed scene lighting in place of the game's
character light direction and shadow atlas; see docs/CHARACTER_RENDERDOC.md.
"""
import bpy


def color_transfer(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks, source: bpy.types.NodeSocket, *, encode: bool) -> bpy.types.NodeSocket:
    """Convert between Blender linear RGB and the captured UNORM working RGB."""
    name = "G4 Linear to UNORM" if encode else "G4 UNORM to Linear"
    group = bpy.data.node_groups.get(name)
    if group is None:
        group = bpy.data.node_groups.new(name, "ShaderNodeTree")
        group.interface.new_socket(name="Color", in_out="INPUT", socket_type="NodeSocketColor")
        group.interface.new_socket(name="Color", in_out="OUTPUT", socket_type="NodeSocketColor")
        ns, ls = group.nodes, group.links
        inp = ns.new("NodeGroupInput")
        out = ns.new("NodeGroupOutput")
        split = ns.new("ShaderNodeSeparateColor")
        combine = ns.new("ShaderNodeCombineColor")
        ls.new(inp.outputs[0], split.inputs[0])
        for index in range(3):
            def math(operation, a, b):
                node = ns.new("ShaderNodeMath")
                node.operation = operation
                for socket, value in zip(node.inputs, (a, b)):
                    if isinstance(value, (int, float)):
                        socket.default_value = value
                    else:
                        ls.new(value, socket)
                return node.outputs[0]
            value = math("MAXIMUM", split.outputs[index], 0.0)
            small = math("LESS_THAN", value, 0.0031308 if encode else 0.04045)
            low = math("MULTIPLY", value, 12.92 if encode else 1.0 / 12.92)
            if encode:
                high = math("POWER", value, 1.0 / 2.4)
                high = math("SUBTRACT", math("MULTIPLY", high, 1.055), 0.055)
            else:
                high = math("DIVIDE", math("ADD", value, 0.055), 1.055)
                high = math("POWER", high, 2.4)
            result = math("ADD", math("MULTIPLY", low, small),
                          math("MULTIPLY", high, math("SUBTRACT", 1.0, small)))
            ls.new(result, combine.inputs[index])
        ls.new(combine.outputs[0], out.inputs[0])
    node = nodes.new("ShaderNodeGroup")
    node.node_tree = group
    node.name = name
    links.new(source, node.inputs[0])
    return node.outputs[0]


def build_character_lighting(nodes, links, base_color, occlusion_channels, surface_normal):
    def math(name, operation, *values, clamp=False):
        node = nodes.new("ShaderNodeMath")
        node.name = name
        node.operation = operation
        node.use_clamp = clamp
        for socket, value in zip(node.inputs, values):
            if isinstance(value, (int, float)):
                socket.default_value = value
            else:
                links.new(value, socket)
        return node.outputs[0]

    def mix(name, operation, factor, a, b):
        node = nodes.new("ShaderNodeMixRGB")
        node.name = name
        node.blend_type = operation
        for socket, value in zip(node.inputs, (factor, a, b)):
            if isinstance(value, (int, float, tuple)):
                socket.default_value = value
            else:
                links.new(value, socket)
        return node

    geometry = nodes.new("ShaderNodeNewGeometry")
    normal = surface_normal if surface_normal is not None else geometry.outputs["Normal"]
    inverted = nodes.new("ShaderNodeVectorMath")
    inverted.operation = "SCALE"
    inverted.inputs[3].default_value = -1.0
    links.new(normal, inverted.inputs[0])
    intensities = []
    for direction in (normal, inverted.outputs[0]):
        diffuse = nodes.new("ShaderNodeBsdfDiffuse")
        diffuse.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        links.new(direction, diffuse.inputs["Normal"])
        rgb = nodes.new("ShaderNodeShaderToRGB")
        links.new(diffuse.outputs[0], rgb.inputs[0])
        bw = nodes.new("ShaderNodeRGBToBW")
        links.new(rgb.outputs[0], bw.inputs[0])
        intensities.append(bw.outputs[0])
    signed = math("G4 Signed Light", "SUBTRACT", *intensities)
    # Opposing diffuse probes cancel uniform environment light. Character
    # ambient is applied separately, as it is in CBUSE_UB_CHARA_LIGHT_IDX.
    toon = math("G4 Toon Ambient", "MULTIPLY_ADD", signed, 0.5, 0.5, clamp=True)
    signed = math("G4 Light Direction Factor", "MULTIPLY_ADD", toon, 2.0, -1.0)
    attr = nodes.new("ShaderNodeVertexColor")
    attr.layer_name = "G4 Outline Parameters"
    attr.name = "G4 Character Vertex Parameters"
    oc = [1.0, 0.0, 0.0] if occlusion_channels is None else list(occlusion_channels.outputs)[:3]
    oc_weight = math("G4 Occlusion Vertex Weight", "MULTIPLY", oc[0], attr.outputs["Alpha"])
    oc_weight = math("G4 Occlusion Weight", "MULTIPLY", oc_weight, 2.0, clamp=True)
    offset = math("G4 Occlusion Offset", "MULTIPLY_ADD", oc_weight, 0.90, -0.25)
    main = math("G4 Occlusion Light", "MULTIPLY_ADD", signed, 0.35, offset)
    main = math("G4 Gradient Coordinate 0", "MULTIPLY_ADD", main, 0.5, 0.5)
    inv = math("G4 Inverse Gradient", "SUBTRACT", 1.0, main)
    second = math("G4 Gradient Coordinate 1", "MULTIPLY_ADD", oc[1], inv, main)
    covers, colors = [], []
    # Capture 16: all 74 character draws use u_shaderParam2=(.9921875,.9765625).
    # These are the white RGB rows 254/250 of chrGrd_01, with alpha transitions
    # at about .754/.530. Use equivalent short ramps, not a bundled game asset.
    for index, (coordinate, threshold, cutoff, color) in enumerate((
        (main, .5, .754, (.74995, .60020, .77992, 1.0)),
        (second, .54, .530, (.72967, .52992, .69982, 1.0)),
    )):
        parameter = nodes.new("ShaderNodeValue")
        parameter.name = f"G4 Shadow Threshold {index}"
        parameter.outputs[0].default_value = threshold
        coordinate = math(f"G4 Gradient Shift {index}", "SUBTRACT", coordinate, parameter.outputs[0])
        coordinate = math(f"G4 Gradient UV {index}", "ADD", coordinate, .5)
        ramp = nodes.new("ShaderNodeValToRGB")
        ramp.name = f"G4 Shadow Band {index}"
        ramp.color_ramp.interpolation = "LINEAR"
        ramp.color_ramp.elements[0].position = cutoff - .003
        ramp.color_ramp.elements[0].color = (1.0, 1.0, 1.0, 1.0)
        ramp.color_ramp.elements[1].position = cutoff + .003
        ramp.color_ramp.elements[1].color = (0.0, 0.0, 0.0, 0.0)
        links.new(coordinate, ramp.inputs[0])
        covers.append(ramp.outputs["Alpha"])
        color_node = nodes.new("ShaderNodeRGB")
        color_node.name = f"G4 Shadow Color {index}"
        color_node.outputs[0].default_value = color
        colors.append(color_node.outputs[0])
    shadows = mix("G4 Dual Toon Ramp", "MIX", covers[1], *colors)
    luminance = nodes.new("ShaderNodeVectorMath")
    luminance.operation = "DOT_PRODUCT"
    luminance.inputs[1].default_value = (.29891, .58661, .11448)
    links.new(base_color, luminance.inputs[0])
    lift = math("G4 Shadow Luminance", "MULTIPLY", luminance.outputs["Value"], .1)
    shade = mix("G4 Shadow Luminance Composite", "ADD", 1.0, shadows.outputs[0], lift).outputs[0]
    ambient = mix("G4 Character Ambient", "MIX", .04995, base_color, (.924925, .874975, .874975, 1.0))
    ambient_mul = mix("G4 Character Ambient Multiply", "MULTIPLY", 1.0, ambient.outputs[0], tuple(ambient.inputs[2].default_value))
    # Keep a single editable/animated ambient color for both native terms.
    # RGB sockets are linked via a separate color source by the event adapter.
    ambient_color = nodes.new("ShaderNodeRGB")
    ambient_color.name = "G4 Ambient Color"
    ambient_color.outputs[0].default_value = tuple(ambient.inputs[2].default_value)
    links.new(ambient_color.outputs[0], ambient.inputs[2])
    links.new(ambient_color.outputs[0], ambient_mul.inputs[2])
    add_rate = math("G4 Shadow Add Rate", "MULTIPLY", covers[0], -.1)
    mul_rate = math("G4 Shadow Multiply Rate", "MULTIPLY", covers[0], 1.3)
    def vector(operation, a, b):
        node = nodes.new("ShaderNodeVectorMath")
        node.operation = operation
        links.new(a, node.inputs[0])
        links.new(b, node.inputs[3] if operation == "SCALE" else node.inputs[1])
        return node.outputs[0]

    # MixRGB clamps its factor; the native -.1/1.3 blend extrapolates.
    delta = vector("SUBTRACT", shade, ambient_mul.outputs[0])
    added = vector("ADD", ambient_mul.outputs[0], vector("SCALE", delta, add_rate))
    multiplied = vector("MULTIPLY", added, shade)
    delta = vector("SUBTRACT", multiplied, added)
    shaded = vector("ADD", added, vector("SCALE", delta, mul_rate))
    recovery = math("G4 Albedo Recovery", "MULTIPLY_ADD", luminance.outputs["Value"], .2, oc[2], clamp=True)
    color = mix("G4 Occlusion Composite", "MIX", recovery, shaded, base_color).outputs[0]
    # A narrow, directional rim replaces the old broad Fresnel wash.
    facing = nodes.new("ShaderNodeVectorMath")
    facing.operation = "DOT_PRODUCT"
    links.new(normal, facing.inputs[0])
    links.new(geometry.outputs["Incoming"], facing.inputs[1])
    inv_facing = math("G4 Native Grazing", "SUBTRACT", 1.0, facing.outputs["Value"], clamp=True)
    high_signal = math("G4 Native Highlight Signal", "MULTIPLY_ADD", inv_facing, signed, main)
    high_threshold = nodes.new("ShaderNodeValue")
    high_threshold.name = "G4 Highlight Threshold"
    high_threshold.outputs[0].default_value = 1.5999
    high = math("G4 Native Highlight", "SUBTRACT", high_signal, high_threshold.outputs[0])
    high = math("G4 Native Highlight Cut", "MULTIPLY", high, 10000.0, clamp=True)
    under = math("G4 Native Under Direction", "MULTIPLY", signed, -1.0, clamp=True)
    under = math("G4 Native Under Grazing", "MULTIPLY", under, inv_facing)
    under_threshold = nodes.new("ShaderNodeValue")
    under_threshold.name = "G4 Under Threshold"
    under_threshold.outputs[0].default_value = 1.45
    under = math("G4 Native Under Signal", "ADD", under, 1.0)
    under = math("G4 Native Under Offset", "SUBTRACT", under, under_threshold.outputs[0])
    under = math("G4 Native Under Cut", "MULTIPLY", under, 300.0, clamp=True)
    return color, toon, shadows, high, under


def animate_capture_lighting(nodes: bpy.types.Nodes, parameters: dict[str, list[float]], frame: int) -> None:
    """Apply native colors and offsets without treating thresholds as strength."""
    for index in range(2):
        names = (f"charaShadowColor{index + 1}", f"charaShadow{index + 1}")
        values = next((parameters[n] for n in names if parameters.get(n)), None)
        color = nodes.get(f"G4 Shadow Color {index}")
        if values and len(values) >= 3:
            color.outputs[0].default_value = tuple(max(0.0, v) for v in values[:3]) + (1.0,)
            color.outputs[0].keyframe_insert("default_value", frame=frame)
        rate = parameters.get(f"charaShadowRate{index + 1}")
        threshold = values[3] if values and len(values) >= 4 else rate[0] if rate else None
        if threshold is not None:
            socket = nodes[f"G4 Shadow Threshold {index}"].outputs[0]
            socket.default_value = threshold
            socket.keyframe_insert("default_value", frame=frame)
    ambient = parameters.get("charaAmbient")
    if ambient and len(ambient) >= 3:
        socket = nodes["G4 Ambient Color"].outputs[0]
        socket.default_value = tuple(max(0.0, v) for v in ambient[:3]) + (1.0,)
        socket.keyframe_insert("default_value", frame=frame)
    for parameter, node_name in (("charaHighThreshold", "G4 Highlight Threshold"),):
        values = parameters.get(parameter)
        if values:
            socket = nodes[node_name].outputs[0]
            socket.default_value = values[0]
            socket.keyframe_insert("default_value", frame=frame)
    for aliases, node_name, threshold_name in (
        (("charaHighLightColor", "charaHighColor"), "G4 Highlight", "G4 Highlight Threshold"),
        (("charaUnderRimColor", "charaUnderRim"), "G4 Under Light", "G4 Under Threshold"),
    ):
        values = next((parameters[name] for name in aliases if parameters.get(name)), None)
        if values and len(values) >= 3:
            socket = nodes[node_name].inputs[2]
            socket.default_value = tuple(max(0.0, v) for v in values[:3]) + (1.0,)
            socket.keyframe_insert("default_value", frame=frame)
            if len(values) >= 4:
                threshold = nodes[threshold_name].outputs[0]
                threshold.default_value = values[3]
                threshold.keyframe_insert("default_value", frame=frame)
