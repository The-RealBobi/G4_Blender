"""Headless smoke test for context-independent temporary pose export."""

import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import bpy


repo = Path(__file__).resolve().parents[1]
if os.environ.get("G4_PORT_USE_INSTALLED"):
    sys.path.insert(0, str(repo.parent))
    import G4_Blender.g4_port_addon as port
else:
    sys.path.insert(0, str(repo))
    import g4_port_addon as port
print("PORT_ADDON", port.__file__)


for obj in tuple(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

def add_mesh(name, offset):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(offset, 0, 0), (offset + 1, 0, 0), (offset, 1, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


obj = add_mesh("pose_export_test_mesh_a", 0)
other = add_mesh("pose_export_test_mesh_b", 2)
non_armature_modifier = obj.modifiers.new("triangulate", "TRIANGULATE")
obj.select_set(True)
bpy.context.view_layer.objects.active = obj

# Simulate an old interrupted export: the production call must remove it.
stale = bpy.data.collections.new("__G4PoseExport")
bpy.context.scene.collection.children.link(stale)

dae_path = Path(tempfile.gettempdir()) / "g4_pose_export_context_smoke.dae"
port.export_collada(
    dae_path,
    selected_only=False,
    align_forward_to_y=True,
    apply_modifiers=False,
    bake_current_pose=True,
)

assert dae_path.is_file() and dae_path.stat().st_size > 0, "Collada export was not produced"
geometries = ET.parse(dae_path).findall(".//{*}library_geometries/{*}geometry")
assert len(geometries) == 2, f"Expected both source meshes, exported {len(geometries)}"
assert bpy.data.collections.get("__G4PoseExport") is None, "Temporary pose collection leaked"
assert obj.select_get(), "Original mesh selection was not restored"
assert bpy.context.view_layer.objects.active is obj, "Original active object was not restored"
assert non_armature_modifier.show_viewport, "Source modifiers were changed by temporary export"
print("POSE_EXPORT_CONTEXT_SMOKE_OK")
