"""Non-destructive Blender node adapters for map surface profiles."""

from __future__ import annotations

from .map_surfaces import MapSurfaceKind, MapSurfaceProfile


def _node(tree, node_type: str, name: str, location):
    node = tree.nodes.get(name)
    if node is None or node.bl_idname != node_type:
        if node is not None:
            tree.nodes.remove(node)
        node = tree.nodes.new(node_type)
        node.name = name
    node.location = location
    return node


def _link(tree, output, input_socket) -> None:
    for link in list(input_socket.links):
        tree.links.remove(link)
    tree.links.new(output, input_socket)


def _input(node, *names):
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            return socket
    return None


def apply_map_surface_nodes(material, principled, profile: MapSurfaceProfile) -> bool:
    """Add the profile's approximate effect without replacing existing inputs."""

    tree = getattr(material, "node_tree", None)
    if tree is None or principled is None:
        return False
    nodes = tree.nodes
    coordinates = _node(tree, "ShaderNodeTexCoord", "G4 Map Coordinates", (-900, -420))
    noise = _node(tree, "ShaderNodeTexNoise", "G4 Map Surface Detail", (-680, -420))
    scale = _input(noise, "Scale")
    detail = _input(noise, "Detail")
    if scale is not None:
        scale.default_value = 3.0 if profile.kind == MapSurfaceKind.WATER else 5.0
    if detail is not None:
        detail.default_value = 4.0
    vector = _input(noise, "Vector")
    generated = coordinates.outputs.get("Generated")
    if vector is not None and generated is not None:
        _link(tree, generated, vector)

    bump = _node(tree, "ShaderNodeBump", "G4 Map Surface Normal", (-360, -360))
    strength = _input(bump, "Strength")
    distance = _input(bump, "Distance")
    if strength is not None:
        strength.default_value = 0.18 if profile.kind == MapSurfaceKind.WATER else 0.08
    if distance is not None:
        distance.default_value = 0.12 if profile.kind == MapSurfaceKind.WATER else 0.05
    height = _input(bump, "Height")
    fac = noise.outputs.get("Fac")
    if height is not None and fac is not None:
        _link(tree, fac, height)
    normal = _input(principled, "Normal")
    if normal is not None and not normal.links:
        bump_normal = bump.outputs.get("Normal")
        if bump_normal is not None:
            _link(tree, bump_normal, normal)

    if profile.kind == MapSurfaceKind.WATER:
        transmission = _input(principled, "Transmission Weight", "Transmission")
        if transmission is not None and not transmission.links:
            transmission.default_value = 0.12
        ior = _input(principled, "IOR")
        if ior is not None and not ior.links:
            ior.default_value = 1.333
        material["g4_map_water_nodes"] = "detail_bump_transmission"
    elif profile.kind == MapSurfaceKind.GRASS:
        material["g4_map_grass_nodes"] = "detail_bump_cutout_ready"
    else:
        material["g4_map_surface_nodes"] = "detail_bump"
    return True


__all__ = ["apply_map_surface_nodes"]
