"""Minimal Blender smoke test for posed-mesh G4SK bind rebasing.

Run with Blender, not CPython:
  Blender -b --factory-startup --python tests/blender_bind_retarget_smoke.py
"""

import importlib.util
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Vector


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("g4_port_addon_test", ROOT / "g4_port_addon.py")
ADDON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADDON)


def armature(name, bone_name, head):
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = data.edit_bones.new(bone_name)
    bone.head = head
    bone.tail = (head[0] + 0.1, head[1], head[2])
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.select_set(False)
    return obj


source_armature = armature("source", "l_arm", (0, 0, 0))
target_armature = armature("target", "l_a_1_0", (2, 0, 0))
bpy.context.view_layer.objects.active = source_armature
source_armature.select_set(True)
bpy.ops.object.mode_set(mode="POSE")
source_armature.pose.bones["l_arm"].matrix_basis.translation.x = 1
bpy.ops.object.mode_set(mode="OBJECT")
source_armature.select_set(False)

mesh = bpy.data.meshes.new("source_mesh")
mesh.from_pydata([(0, 0, 0)], [], [])
source = bpy.data.objects.new("source_mesh", mesh)
bpy.context.collection.objects.link(source)
group = source.vertex_groups.new(name="l_arm")
group.add([0], 1.0, "REPLACE")
modifier = source.modifiers.new("Armature", "ARMATURE")
modifier.object = source_armature

evaluated = mesh.copy()
evaluated.vertices[0].co = (1, 0, 0)
rebased = ADDON.rebase_evaluated_mesh_to_target_bind(source, evaluated, target_armature)
g4_to_blender = Matrix(((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)))
expected = (
    g4_to_blender
    @ target_armature.data.bones["l_a_1_0"].matrix_local
    @ g4_to_blender
    @ source_armature.pose.bones["l_arm"].matrix.inverted_safe()
    @ Vector((1, 0, 0))
)
assert rebased == 1
assert (evaluated.vertices[0].co - expected).length < 1e-5
print("PASS bind retarget smoke")
