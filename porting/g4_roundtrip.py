"""Shared identity checks for preserving untouched native model imports."""

from __future__ import annotations

import hashlib


NATIVE_ROUNDTRIP_SIGNATURE_VERSION = 2


def native_mesh_signature(obj) -> str:
    digest = hashlib.sha256()
    digest.update(obj.name.encode("utf-8"))
    for value in obj.matrix_world:
        digest.update(f"{tuple(round(component, 8) for component in value)};".encode("ascii"))
    mesh = obj.data
    for vertex in mesh.vertices:
        digest.update(f"{tuple(round(value, 8) for value in vertex.co)}|".encode("ascii"))
    for polygon in mesh.polygons:
        digest.update(f"{polygon.material_index}:{tuple(polygon.vertices)};".encode("ascii"))
    uv_layer = mesh.uv_layers.active
    if uv_layer is not None:
        for loop in uv_layer.data:
            digest.update(f"{round(loop.uv.x, 8)},{round(loop.uv.y, 8)};".encode("ascii"))
    for vertex in mesh.vertices:
        groups = sorted((group.group, round(group.weight, 8)) for group in vertex.groups)
        digest.update(f"{groups};".encode("ascii"))
    return digest.hexdigest()
