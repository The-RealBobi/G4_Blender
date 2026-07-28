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


def add_posed_armature(mesh):
    bpy.ops.object.armature_add(enter_editmode=True)
    armature = bpy.context.object
    armature.name = "pose_export_test_rig"
    bone = armature.data.edit_bones[0]
    bone.name = "pose_export_bone"
    bone.head = (0, 0, 0)
    bone.tail = (0, 0, 1)
    bpy.ops.object.mode_set(mode="OBJECT")
    group = mesh.vertex_groups.new(name=bone.name)
    group.add(range(len(mesh.data.vertices)), 1.0, "REPLACE")
    modifier = mesh.modifiers.new("pose", "ARMATURE")
    modifier.object = armature
    armature.pose.bones[bone.name].location.x = 2.0
    bpy.context.view_layer.update()
    return armature


def exported_position_x_range(path):
    root = ET.parse(path).getroot()
    geometry = next(item for item in root.findall(".//{*}geometry") if "pose_export_test_mesh_a" in item.attrib.get("name", ""))
    array = next(item for item in geometry.findall(".//{*}float_array") if "positions" in item.attrib.get("id", ""))
    values = [float(value) for value in array.text.split()]
    return min(values[0::3]), max(values[0::3])


obj = add_mesh("pose_export_test_mesh_a", 0)
other = add_mesh("pose_export_test_mesh_b", 2)
non_armature_modifier = obj.modifiers.new("triangulate", "TRIANGULATE")
rig = add_posed_armature(obj)
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
low_x, high_x = exported_position_x_range(dae_path)
assert low_x > 1.5 and high_x > 2.5, f"Posed vertices were not baked into the DAE: x=[{low_x}, {high_x}]"
print("POSE_EXPORT_CONTEXT_SMOKE_OK")
