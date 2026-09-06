"""Render-space character outline preview; no duplicate meshes or baked ink.

Capture 16 uses edge_mask -> edge_tone_mask -> edge_composi_mask. This Blender
approximation preserves the source RG seams, blue weight, normal/depth edges,
pixel width and multiplicative color. It does not claim native filter parity.
"""
import bpy

AOV_DATA = "G4 Edge Data"
AOV_WEIGHT = "G4 Edge Weight"
AOV_COVERAGE = "G4 Character Coverage"
GROUP_NAME = "G4 Screen Outline"


def add_outline_outputs(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks) -> None:
    attr = nodes.new("ShaderNodeVertexColor")
    attr.layer_name = "G4 Outline Parameters"
    split = nodes.new("ShaderNodeSeparateColor")
    links.new(attr.outputs["Color"], split.inputs[0])
    geometry = nodes.new("ShaderNodeNewGeometry")
    facing = nodes.new("ShaderNodeVectorMath")
    facing.operation = "DOT_PRODUCT"
    links.new(geometry.outputs["Normal"], facing.inputs[0])
    links.new(geometry.outputs["Incoming"], facing.inputs[1])
    packed = nodes.new("ShaderNodeCombineColor")
    links.new(split.outputs["Green"], packed.inputs[0])
    links.new(split.outputs["Red"], packed.inputs[1])
    links.new(facing.outputs["Value"], packed.inputs[2])
    weight = nodes.new("ShaderNodeMath")
    weight.operation = "MULTIPLY"
    weight.inputs[1].default_value = .44
    links.new(split.outputs["Blue"], weight.inputs[0])
    for name, source, kind in (
        (AOV_DATA, packed.outputs[0], "Color"),
        (AOV_WEIGHT, weight.outputs[0], "Value"),
        (AOV_COVERAGE, None, "Value"),
    ):
        node = nodes.new("ShaderNodeOutputAOV")
        node.name = name
        node.aov_name = name
        if source is None:
            node.inputs[kind].default_value = 1.0
        else:
            links.new(source, node.inputs[kind])


def _compositor_tree(scene: bpy.types.Scene) -> bpy.types.NodeTree:
    if hasattr(scene, "node_tree"):
        scene.use_nodes = True
        return scene.node_tree
    if scene.compositing_node_group is None:
        tree = bpy.data.node_groups.new("Compositing", "CompositorNodeTree")
        tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        render = tree.nodes.new("CompositorNodeRLayers")
        output = tree.nodes.new("NodeGroupOutput")
        tree.links.new(render.outputs["Image"], output.inputs["Image"])
        scene.compositing_node_group = tree
    return scene.compositing_node_group


def _outline_group() -> bpy.types.NodeTree:
    group = bpy.data.node_groups.get(GROUP_NAME)
    if group is not None and group.get("g4_outline_schema") == 2:
        return group
    if group is not None:
        group.nodes.clear()
    else:
        group = bpy.data.node_groups.new(GROUP_NAME, "CompositorNodeTree")
    for name, kind in (("Image", "Color"), ("Data", "Color"), ("Weight", "Float"),
                       ("Coverage", "Float"), ("Depth", "Float"), ("Width", "Float")):
        socket = next((item for item in group.interface.items_tree if item.name == name and getattr(item, "in_out", None) == "INPUT"), None)
        if socket is None:
            socket = group.interface.new_socket(name=name, in_out="INPUT", socket_type=f"NodeSocket{kind}")
        if name == "Width":
            socket.default_value = 1.0
            socket.min_value = .25
            socket.max_value = 6.0
    if not any(getattr(item, "in_out", None) == "OUTPUT" for item in group.interface.items_tree):
        group.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    nodes, links = group.nodes, group.links
    inp = nodes.new("NodeGroupInput")
    out = nodes.new("NodeGroupOutput")

    def math(operation, a, b, clamp=False):
        node = nodes.new("CompositorNodeMath" if hasattr(bpy.types, "CompositorNodeMath") else "ShaderNodeMath")
        node.operation = operation
        node.use_clamp = clamp
        for socket, value in zip(node.inputs, (a, b)):
            if isinstance(value, (float, int)):
                socket.default_value = value
            else:
                links.new(value, socket)
        return node.outputs[0]

    def separate(source):
        node = nodes.new("CompositorNodeSeparateColor")
        links.new(source, node.inputs[0])
        return node.outputs

    def shifted(source, dx, dy):
        node = nodes.new("CompositorNodeTranslate")
        links.new(source, node.inputs[0])
        links.new(math("MULTIPLY", inp.outputs["Width"], dx), node.inputs[1])
        links.new(math("MULTIPLY", inp.outputs["Width"], dy), node.inputs[2])
        return node.outputs[0]

    center = separate(inp.outputs["Data"])
    # Log depth makes the jump criterion relative to viewing distance. Clamp
    # sky/background values before filtering, and mask the result to characters.
    depth = math("MAXIMUM", inp.outputs["Depth"], .001)
    depth = math("LOGARITHM", depth, 2.718281828)
    seam, normal, silhouette, normal_mask = 0.0, 0.0, 0.0, 0.0
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        adjacent = separate(shifted(inp.outputs["Data"], dx, dy))
        delta_r = math("ABSOLUTE", math("SUBTRACT", center[0], adjacent[0]), 0.0)
        delta_g = math("ABSOLUTE", math("SUBTRACT", center[1], adjacent[1]), 0.0)
        seam = math("MAXIMUM", seam, delta_r)
        normal_mask = math("ADD", normal_mask, delta_g)
        delta_n = math("ABSOLUTE", math("SUBTRACT", center[2], adjacent[2]), 0.0)
        normal = math("MAXIMUM", normal, delta_n)
        adjacent_depth = shifted(depth, dx, dy)
        delta_z = math("ABSOLUTE", math("SUBTRACT", depth, adjacent_depth), 0.0)
        silhouette = math("MAXIMUM", silhouette, delta_z)
    seam = math("MULTIPLY", seam, 5.0, clamp=True)
    normal = math("MULTIPLY", math("SUBTRACT", normal, .30), 8.0, clamp=True)
    silhouette = math("MULTIPLY", math("SUBTRACT", silhouette, .015), 40.0, clamp=True)
    # Packed R (vertex green) draws seams; packed G (vertex red) only gates
    # normal edges. Treating G as ink exposes interpolated facial triangles.
    normal_gate = math("MULTIPLY", math("SUBTRACT", .25, math("MULTIPLY", normal_mask, .5)), 256.0, clamp=True)
    normal = math("MULTIPLY", normal, normal_gate)
    edge = math("MAXIMUM", seam, math("MAXIMUM", normal, silhouette))
    weight = math("MULTIPLY", inp.outputs["Weight"], 1.4, clamp=True)
    edge = math("MULTIPLY", edge, weight)
    edge = math("MULTIPLY", edge, inp.outputs["Coverage"], clamp=True)
    # Composite in display RGB, like the captured UNORM pipeline. Multiplying
    # linear RGB by the squared factor keeps bright skin/hair lines colored.
    factor = math("SUBTRACT", 1.0, edge)
    factor = math("POWER", factor, 2.2)
    composite = nodes.new("CompositorNodeMixRGB" if hasattr(bpy.types, "CompositorNodeMixRGB") else "ShaderNodeMixRGB")
    composite.blend_type = "MULTIPLY"
    composite.inputs[0].default_value = 1.0
    links.new(inp.outputs["Image"], composite.inputs[1])
    links.new(factor, composite.inputs[2])
    alpha = nodes.new("CompositorNodeSetAlpha")
    if hasattr(alpha, "mode"):
        alpha.mode = "REPLACE_ALPHA"
    links.new(composite.outputs[0], alpha.inputs[0])
    source_alpha = separate(inp.outputs["Image"])[3]
    links.new(source_alpha, alpha.inputs[1])
    links.new(alpha.outputs[0], out.inputs[0])
    group["g4_outline_schema"] = 2
    return group


def configure_screen_outline(scene: bpy.types.Scene, view_layer: bpy.types.ViewLayer, width: float) -> bool:
    tree = _compositor_tree(scene)
    view_layer.use_pass_z = True
    for name, kind in ((AOV_DATA, "COLOR"), (AOV_WEIGHT, "VALUE"), (AOV_COVERAGE, "VALUE")):
        aov = next((a for a in view_layer.aovs if a.name == name), None)
        if aov is None:
            aov = view_layer.aovs.add()
            aov.name = name
        aov.type = kind
    view_layer.update_render_passes()
    configured = False
    for render in tuple(tree.nodes):
        if render.type != "R_LAYERS" or (render.layer and render.layer != view_layer.name):
            continue
        image = render.outputs.get("Image")
        if image is None or not image.is_linked:
            continue
        existing = next((link.to_node for link in image.links if link.to_node.get("g4_screen_outline")), None)
        if existing is not None:
            existing.node_tree = _outline_group()
            existing.inputs["Width"].default_value = width
            configured = True
            continue
        if any(render.outputs.get(name) is None for name in (AOV_DATA, AOV_WEIGHT, AOV_COVERAGE, "Depth")):
            continue
        destinations = [link.to_socket for link in image.links]
        node = tree.nodes.new("CompositorNodeGroup")
        node.node_tree = _outline_group()
        node.name = GROUP_NAME
        node["g4_screen_outline"] = True
        node.inputs["Width"].default_value = width
        node.location = (render.location.x + 280, render.location.y)
        tree.links.new(image, node.inputs["Image"])
        for aov, socket in ((AOV_DATA, "Data"), (AOV_WEIGHT, "Weight"), (AOV_COVERAGE, "Coverage"), ("Depth", "Depth")):
            tree.links.new(render.outputs[aov], node.inputs[socket])
        for destination in destinations:
            tree.links.new(node.outputs["Image"], destination)
        configured = True
    return configured


def remove_screen_outline(scene: bpy.types.Scene) -> None:
    tree = scene.node_tree if hasattr(scene, "node_tree") else scene.compositing_node_group
    if tree is None:
        return
    for node in tuple(tree.nodes):
        if not node.get("g4_screen_outline"):
            continue
        source = node.inputs["Image"].links[0].from_socket if node.inputs["Image"].is_linked else None
        if source is not None:
            for link in tuple(node.outputs["Image"].links):
                tree.links.new(source, link.to_socket)
        tree.nodes.remove(node)
