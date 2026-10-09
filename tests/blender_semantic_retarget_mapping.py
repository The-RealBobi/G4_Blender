import json
import sys
from pathlib import Path

import bpy

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo.parent))
from G4_Blender import g4_animation_addon as animation

for obj in tuple(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

bpy.ops.object.armature_add(enter_editmode=True)
armature = bpy.context.object
armature.name = "Semantic Retarget"
base = armature.data.edit_bones[0]
base.name = "mixamorig:Hips"
base.head = (0.0, 0.0, 0.0)
base.tail = (0.0, 0.0, 0.2)
upper_arm = armature.data.edit_bones.new("Bip01_L_UpperArm")
upper_arm.head = (0.1, 0.0, 0.2)
upper_arm.tail = (0.3, 0.0, 0.2)
face = armature.data.edit_bones.new("FaceControl")
face.head = (0.0, 0.0, 0.3)
face.tail = (0.0, 0.0, 0.4)
g_chest = armature.data.edit_bones.new("G_chest")
g_chest.head = (0.0, 0.0, 0.4)
g_chest.tail = (0.0, 0.0, 0.5)
chest = armature.data.edit_bones.new("chest")
chest.head = (0.0, 0.0, 0.5)
chest.tail = (0.0, 0.0, 0.6)
chest.parent = g_chest
bpy.ops.object.mode_set(mode="OBJECT")
armature.data.bones["FaceControl"]["g4_animation_source_joint"] = "c_head_1_0"

names = ["c_c_1_0", "l_a_1_0", "c_head_1_0", "c_c_1_1", "unmapped_joint"]
rest = {"translation": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]}
tracks = [{
    "target_name": name,
    "animated_paths": ["rotation"],
    "values": {
        "translation": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        "rotation": [[0.0, 0.0, 0.0, 1.0], [0.0, 0.38268343, 0.0, 0.92387953]],
        "scale": [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]],
    },
} for name in names]
motion = {
    "clip": {"name": "semantic-map-smoke", "start_frame": 0, "end_frame": 1},
    "frames": [0, 1],
    "skeleton": {
        "names": names,
        "parents": [-1, -1, -1, -1],
        "local_trs": [rest] * len(names),
    },
    "tracks": tracks,
}

animation.resolve_track_names_from_armature(motion, armature)
assert tracks[0]["armature_bone_names"] == ["mixamorig:Hips"]
assert tracks[1]["armature_bone_names"] == ["Bip01_L_UpperArm"]
assert tracks[2]["armature_bone_names"] == ["FaceControl"]
assert tracks[3]["armature_bone_names"] == ["G_chest"]
assert tracks[4]["armature_bone_names"] == []
assert animation.armature_score(armature, set(names)) == 4

action, keyed = animation.create_action(armature, motion, 0)
assert keyed == 4, keyed
mapping = json.loads(action["g4_retarget_mapping"])
assert mapping["c_head_1_0"] == ["FaceControl"]
assert json.loads(action["g4_retarget_unmapped_targets"]) == ["unmapped_joint"]

bpy.ops.object.armature_add(enter_editmode=True)
tie_rig = bpy.context.object
tie_rig.name = "Weighted Alias Tie"
root = tie_rig.data.edit_bones[0]
root.name = "COG"
root.head = (0.0, 0.0, 0.0)
root.tail = (0.0, 0.0, 0.2)
for name, y in (("hip", 0.1), ("waist", -0.1)):
    bone = tie_rig.data.edit_bones.new(name)
    bone.head = (0.0, y, 0.2)
    bone.tail = (0.0, y, 0.4)
bpy.ops.object.mode_set(mode="OBJECT")
tie_motion = {"tracks": [{"target_name": "c_c_1_0"}]}
animation.resolve_track_names_from_armature(tie_motion, tie_rig)
assert tie_motion["tracks"][0]["armature_bone_names"] == []
bpy.context.view_layer.objects.active = tie_rig
tie_rig.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
tie_rig.data.edit_bones["hip"].parent = tie_rig.data.edit_bones["waist"]
bpy.ops.object.mode_set(mode="OBJECT")
animation.resolve_track_names_from_armature(tie_motion, tie_rig)
assert tie_motion["tracks"][0]["armature_bone_names"] == ["waist"]

mesh_data = bpy.data.meshes.new("Alias Influence")
mesh_data.from_pydata([(0.0, 0.0, 0.0)] * 4, [], [])
mesh = bpy.data.objects.new("Alias Influence", mesh_data)
bpy.context.collection.objects.link(mesh)
modifier = mesh.modifiers.new("Armature", "ARMATURE")
modifier.object = tie_rig
hip_group = mesh.vertex_groups.new(name="hip")
waist_group = mesh.vertex_groups.new(name="waist")
hip_group.add([0, 1, 2, 3], 0.75, "REPLACE")
waist_group.add([0, 1, 2, 3], 0.25, "REPLACE")
animation.resolve_track_names_from_armature(tie_motion, tie_rig)
assert tie_motion["tracks"][0]["armature_bone_names"] == ["hip"]
print("SEMANTIC_RETARGET_MAPPING_OK")
