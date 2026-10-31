"""Character lighting reconstructed from the draw shaders in capture 16.

The two white gradient rows carry shadow coverage in alpha. Their RGB is not
an albedo tint. Artistic direction is analytical; EEVEE supplies only the
projected-shadow approximation. See docs/CHARACTER_RENDERDOC.md.
"""
import bpy


def optimize_event_lighting(scene: bpy.types.Scene) -> int:
    """Move imported cut lighting off animated shader trees onto scene attributes."""
    if scene.library or not scene.get("g4_event_light_parameters"):
        return 0
    paths = {
        f'nodes["{name}"].outputs[0].default_value'
        for name in (
            "G4 Shadow Color 0", "G4 Shadow Color 1",
            "G4 Shadow Threshold 0", "G4 Shadow Threshold 1",
            "G4 Ambient Color", "G4 Highlight Threshold", "G4 Under Threshold",
            "G4 Self Shadow", "G4 Shadow Irradiance",
        )
    } | {
        f'nodes["{name}"].inputs[2].default_value'
        for name in ("G4 Highlight", "G4 Under Light")
    }
    paths.update({
        'nodes["G4 Light Direction"].inputs[0].default_value',
        'nodes["G4 Shadow Direction"].inputs[0].default_value',
    })
    from .scene_animation import migrate_material_animation
    materials = {slot.material for obj in scene.objects for slot in obj.material_slots
                 if slot.material and slot.material.get("g4_level5_toon")}
    return migrate_material_animation(scene, materials, paths, {"CONSTANT"})


def color_transfer(
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    source: bpy.types.NodeSocket,
    *,
    encode: bool,
    approximate: bool,
) -> bpy.types.NodeSocket:
    """Convert between Blender linear RGB and the captured UNORM working RGB."""
    suffix = " Approx" if approximate else ""
    name = ("G4 Linear to UNORM" if encode else "G4 UNORM to Linear") + suffix
    group = bpy.data.node_groups.get(name)
    if group is None:
        group = bpy.data.node_groups.new(name, "ShaderNodeTree")
        group.interface.new_socket(name="Color", in_out="INPUT", socket_type="NodeSocketColor")
        group.interface.new_socket(name="Color", in_out="OUTPUT", socket_type="NodeSocketColor")
        ns, ls = group.nodes, group.links
        inp = ns.new("NodeGroupInput")
        out = ns.new("NodeGroupOutput")
        if approximate:
            curve_node = ns.new("ShaderNodeRGBCurve")
            curve = curve_node.mapping.curves[3]
            # ponytail: a short LUT keeps the viewport shader cheap; add samples only
            # if a future capture shows a visible transfer-band artifact.
            points = (0.0, 0.0031308, 0.01, 0.04, 0.08, 0.16, 0.3, 0.5, 0.7, 0.85, 1.0) if encode else (
                0.0, 0.01, 0.04045, 0.08, 0.16, 0.3, 0.5, 0.7, 0.9, 1.0
            )

            def transfer(value):
                if encode:
                    return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1.0 / 2.4) - 0.055
                return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

            curve.points[0].location = (points[0], transfer(points[0]))
            curve.points[1].location = (points[-1], transfer(points[-1]))
            for point in points[1:-1]:
                curve.points.new(point, transfer(point))
            curve_node.mapping.update()
            ls.new(inp.outputs[0], curve_node.inputs["Color"])
            ls.new(curve_node.outputs["Color"], out.inputs[0])
        else:
            split = ns.new("ShaderNodeSeparateColor")
            combine = ns.new("ShaderNodeCombineColor")
            ls.new(inp.outputs[0], split.inputs[0])
            for index in range(3):
                def math(operation, *values):
                    node = ns.new("ShaderNodeMath")
                    node.operation = operation
                    for socket, value in zip(node.inputs, values):
                        if isinstance(value, (int, float)):
                            socket.default_value = value
                        else:
                            ls.new(value, socket)
                    return node.outputs[0]
                value = split.outputs[index] if encode else math("MAXIMUM", split.outputs[index], 0.0)
                small = math("LESS_THAN", value, 0.0031308 if encode else 0.04045)
                low = math("MULTIPLY", value, 12.92 if encode else 1.0 / 12.92)
                if encode:
                    high = math("POWER", value, 1.0 / 2.4)
                    high = math("MULTIPLY_ADD", high, 1.055, -0.055)
                else:
                    high = math("MULTIPLY_ADD", value, 1.0 / 1.055, 0.055 / 1.055)
                    high = math("POWER", high, 2.4)
                result = math("MULTIPLY_ADD", low, small,
                              math("MULTIPLY", high, math("SUBTRACT", 1.0, small)))
                ls.new(result, combine.inputs[index])
            ls.new(combine.outputs[0], out.inputs[0])
    node = nodes.new("ShaderNodeGroup")
    node.node_tree = group
    node.name = name
    links.new(source, node.inputs[0])
    return node.outputs[0]


def build_character_lighting(nodes, links, base_color, occlusion_channels, surface_normal):
    """Evaluate chr_toon's artistic lighting independently of EEVEE irradiance."""
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

    def vector(name, operation, *values):
        node = nodes.new("ShaderNodeVectorMath")
        node.name = name
        node.operation = operation
        for socket, value in zip(node.inputs, values):
            if isinstance(value, tuple):
                socket.default_value = value
            else:
                links.new(value, socket)
        return node.outputs["Value" if operation == "DOT_PRODUCT" else "Vector"]

    def parameter(name, value):
        node = nodes.new("ShaderNodeRGB" if isinstance(value, tuple) else "ShaderNodeValue")
        node.name = name
        node.outputs[0].default_value = value
        return node.outputs[0]

    def mix(name, operation, factor, a, b):
        node = nodes.new("ShaderNodeMix")
        node.name = name
        node.data_type = "RGBA"
        node.blend_type = operation
        node.clamp_factor = False
        for socket, value in zip((node.inputs[0], node.inputs[6], node.inputs[7]), (factor, a, b)):
            if isinstance(value, (int, float, tuple)):
                socket.default_value = value
            else:
                links.new(value, socket)
        return node

    geometry = nodes.new("ShaderNodeNewGeometry")
    normal = surface_normal if surface_normal is not None else geometry.outputs["Normal"]
    # chr_toon.vfxo emits the world normal in o3. PS instruction 244 dots it
    # with cb7[4], independently of the shadow comparison at instructions 861+.
    direction = vector("G4 Light Direction", "ADD", (-.6805263, -.0009858, .7327231))
    signed = vector("G4 Native Light Dot", "DOT_PRODUCT", normal, direction)
    toon = math("G4 Toon Ambient", "MULTIPLY_ADD", signed, .5, .5, clamp=True)
    signed = math("G4 Light Direction Factor", "MULTIPLY_ADD", toon, 2.0, -1.0)
    diffuse = nodes.new("ShaderNodeBsdfDiffuse")
    diffuse.name = "G4 Shadow Receiver"
    diffuse.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    shadow_direction = vector("G4 Shadow Direction", "ADD", (-.6805263, -.0009858, .7327231))
    links.new(shadow_direction, diffuse.inputs["Normal"])
    probe = nodes.new("ShaderNodeShaderToRGB")
    probe.name = "G4 Shadow Visibility"
    links.new(diffuse.outputs[0], probe.inputs[0])
    # A light-facing receiver removes N.L from this query. White unit sun
    # irradiance is 1/pi in EEVEE; event import supplies energy and RGB luma.
    irradiance = parameter("G4 Shadow Irradiance", 10.0 / 3.141592653589793)
    luminance_probe = vector("G4 Shadow Luminance Probe", "DOT_PRODUCT",
                             probe.outputs["Color"], (.2126, .7152, .0722))
    visibility = math("G4 Shadow Normalize", "DIVIDE", luminance_probe, irradiance)
    shadow = math("G4 Projected Shadow", "SUBTRACT", 1.0, visibility, clamp=True)
    valid = math("G4 Shadow Light Present", "GREATER_THAN", irradiance, 1e-6)
    shadow = math("G4 Shadow Light Weight", "MULTIPLY", shadow, valid)
    strength = parameter("G4 Self Shadow", .65)
    shadow = math("G4 Self Shadow Weight", "MULTIPLY", shadow, strength)

    attr = nodes.new("ShaderNodeVertexColor")
    attr.layer_name = "G4 Outline Parameters"
    attr.name = "G4 Character Vertex Parameters"
    oc = [1.0, 0.0, 0.0] if occlusion_channels is None else list(occlusion_channels.outputs)[:3]
    weight = math("G4 Occlusion Vertex Weight", "MULTIPLY", oc[0], attr.outputs["Alpha"])
    weight = math("G4 Occlusion Weight", "MULTIPLY", weight, 2.0, clamp=True)
    offset = math("G4 Occlusion Offset", "MULTIPLY_ADD", weight, .9, -.25)
    main = math("G4 Occlusion Light", "MULTIPLY_ADD", signed, .35, offset)
    coordinate = math("G4 Gradient Coordinate 0", "MULTIPLY_ADD", main, .5, .5)
    inverse = math("G4 Inverse Gradient", "SUBTRACT", 1.0, coordinate)
    second = math("G4 Gradient Coordinate 1", "MULTIPLY_ADD", oc[1], inverse, coordinate)
    covers, colors = [], []
    # Capture 16 cb7 and chrGrd_01 rows 254/250: RGB is white, alpha is
    # coverage. Fold each short linear ramp into a saturating affine map.
    for index, (value, threshold, cutoff, color) in enumerate((
        (coordinate, .5, .754, (.74995, .60020, .77992, 1.0)),
        (second, .54, .530, (.72967, .52992, .69982, 1.0)),
    )):
        threshold = parameter(f"G4 Shadow Threshold {index}", threshold)
        delta = math(f"G4 Gradient Shift {index}", "SUBTRACT", threshold, value)
        cover = math(f"G4 Shadow Band {index}", "MULTIPLY_ADD", delta, 1.0 / .006,
                     (cutoff - .497) / .006, clamp=True)
        covers.append(cover)
        colors.append(parameter(f"G4 Shadow Color {index}", color))
    shadows = mix("G4 Dual Toon Ramp", "MIX", covers[1], *colors)
    coverage = math("G4 Combined Shadow", "MAXIMUM", covers[0], shadow)
    luminance = vector("G4 Albedo Luminance", "DOT_PRODUCT", base_color, (.29891, .58661, .11448))
    lift = math("G4 Shadow Luminance", "MULTIPLY", luminance, .1)
    shade = vector("G4 Shadow Luminance Composite", "ADD", shadows.outputs[2], lift)
    ambient_color = parameter("G4 Ambient Color", (.924925, .874975, .874975, 1.0))
    ambient = mix("G4 Character Ambient", "MIX", .04995, base_color, ambient_color)
    ambient = vector("G4 Character Ambient Multiply", "MULTIPLY", ambient.outputs[2], ambient_color)
    add_rate = math("G4 Shadow Add Rate", "MULTIPLY", coverage, -.1)
    mul_rate = math("G4 Shadow Multiply Rate", "MULTIPLY", coverage, 1.3)
    added = mix("G4 Native Shadow Add", "MIX", add_rate, ambient, shade)
    multiplied = mix("G4 Native Shadow Multiply", "MULTIPLY", mul_rate, added.outputs[2], shade)
    recovery = math("G4 Albedo Recovery", "MULTIPLY_ADD", luminance, .2, oc[2], clamp=True)
    color = mix("G4 Occlusion Composite", "MIX", recovery, multiplied.outputs[2], base_color).outputs[2]

    facing = vector("G4 Native Facing", "DOT_PRODUCT", normal, geometry.outputs["Incoming"])
    grazing = math("G4 Native Grazing", "SUBTRACT", 1.0, facing, clamp=True)
    high_signal = math("G4 Native Highlight Signal", "MULTIPLY_ADD", grazing, signed, coordinate)
    high_threshold = parameter("G4 Highlight Threshold", 1.5999)
    high = math("G4 Native Highlight", "SUBTRACT", high_signal, high_threshold)
    high = math("G4 Native Highlight Cut", "MULTIPLY", high, 10000.0, clamp=True)
    # PS 354..371, 2118..2134: the under rim also contains the AO offset
    # and signed direction, rather than a broad Fresnel on the unlit side.
    under_direction = math("G4 Native Under Direction", "MULTIPLY", signed, -1.0, clamp=True)
    under_grazing = math("G4 Native Under Grazing", "MULTIPLY", under_direction, grazing)
    under_base = math("G4 Native Under Base", "MULTIPLY_ADD", signed, -.35, offset)
    under_signal = math("G4 Native Under Signal", "MULTIPLY_ADD", under_base, .5, under_grazing)
    under_threshold = parameter("G4 Under Threshold", 1.45)
    under_signal = math("G4 Native Under Offset", "SUBTRACT", under_signal, under_threshold)
    under_signal = math("G4 Native Under Bias", "ADD", under_signal, .5)
    under = math("G4 Native Under Cut", "MULTIPLY", under_signal, 300.0, clamp=True)
    return color, toon, shadows, high, under


def animate_capture_lighting(nodes: bpy.types.Nodes, parameters: dict[str, list[float]], frame: int) -> None:
    """Apply native colors and offsets without treating thresholds as strength."""
    direction = parameters.get("charaLightDir")
    node = nodes.get("G4 Light Direction")
    if node is not None and direction and len(direction) >= 3:
        from ..formats.g4_axes import blender_from_g4_matrix
        from mathutils import Vector
        value = blender_from_g4_matrix().to_3x3() @ Vector(direction[:3])
        if value.length_squared > 1e-8:
            node.inputs[0].default_value = value.normalized()
            node.inputs[0].keyframe_insert("default_value", frame=frame)
    shadow = parameters.get("charaSelfShadow")
    node = nodes.get("G4 Self Shadow")
    if node is not None and shadow:
        node.outputs[0].default_value = max(0.0, min(1.0, shadow[0]))
        node.outputs[0].keyframe_insert("default_value", frame=frame)
    under = parameters.get("charaUnderRimRate")
    if under and nodes.get("G4 Under Threshold"):
        socket = nodes["G4 Under Threshold"].outputs[0]
        socket.default_value = under[0]
        socket.keyframe_insert("default_value", frame=frame)
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
