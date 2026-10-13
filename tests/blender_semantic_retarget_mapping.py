import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy
from mathutils import Quaternion, Vector

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo.parent))
from G4_Blender.animation import g4_animation_addon as animation
from G4_Blender.porting import g4_port_addon as port

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
assert tracks[3]["armature_bone_names"] == ["chest"]
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

bpy.ops.object.armature_add(enter_editmode=True)
context_rig = bpy.context.object
context_rig.name = "Contextual Animation Map"
root = context_rig.data.edit_bones[0]
root.name = "SPINE1"
root.head = (0.0, 0.0, 0.0)
root.tail = (0.0, 0.0, 0.2)
expected_source = {
    "CLAVICLE_L": "l_s_1_0", "SHOULDER_L": "l_a_1_0", "ELBOW_L": "l_a_1_1",
    "WRIST_L": "l_w_1_0", "THIGH_L": "l_l_1_0", "CLANK_L": "l_l_1_1",
    "TOE1_L": "l_foot_1_0", "TOE2_L": "l_foot_1_1",
    "CLAVICLE_R": "r_s_1_0", "SHOULDER_R": "r_a_1_0", "ELBOW_R": "r_a_1_1",
    "WRIST_R": "r_w_1_0", "THIGH_R": "r_l_1_0", "CLANK_R": "r_l_1_1",
    "TOE1_R": "r_foot_1_0", "TOE2_R": "r_foot_1_1",
}
for name in expected_source:
    bone = context_rig.data.edit_bones.new(name)
    bone.head = (0.1 if name.endswith("_L") else -0.1, 0.0, 0.2)
    bone.tail = (bone.head.x, 0.0, 0.3)
    if name.startswith(("CLAVICLE", "THIGH")):
        bone.parent = root
hair_root = context_rig.data.edit_bones.new("HAIR_ROOT")
hair_root.head = (0.0, 0.0, 0.6)
hair_root.tail = (0.0, 0.0, 0.8)
hair_root.parent = root
hair_tip = context_rig.data.edit_bones.new("HAIR_TIP")
hair_tip.head = (0.0, 0.0, 0.8)
hair_tip.tail = (0.0, 0.0, 1.0)
hair_tip.parent = hair_root
bpy.ops.object.mode_set(mode="OBJECT")
stale_aliases = [
    SimpleNamespace(source_group=name, target_joint=source)
    for name, source in {
        "CLAVICLE_L": "l_s_1_0", "SHOULDER_L": "l_s_1_0", "ELBOW_L": "l_a_1_1",
        "WRIST_L": "l_w_1_0", "THIGH_L": "l_l_1_0", "CLANK_L": "l_l_1_0",
        "TOE1_L": "l_l_1_0", "TOE2_L": "l_l_1_0",
        "CLAVICLE_R": "r_s_1_0", "SHOULDER_R": "r_s_1_0", "ELBOW_R": "r_a_1_1",
        "WRIST_R": "r_w_1_0", "THIGH_R": "r_l_1_0", "CLANK_R": "r_l_1_0",
        "TOE1_R": "r_l_1_0", "TOE2_R": "r_l_1_0",
    }.items()
]
stale_aliases.extend(
    SimpleNamespace(source_group=name, target_joint="c_head_1_0")
    for name in ("HAIR_ROOT", "HAIR_TIP")
)
port_settings = SimpleNamespace(
    joint_aliases=stale_aliases,
    target_g4sk_path="",
    target_joint_names=json.dumps(sorted(set(expected_source.values()) | {"c_head_1_0"})),
)
source_map = port.animation_source_joint_map(
    port_settings,
    set(expected_source.values()) | {"c_head_1_0"},
    {bone.name for bone in context_rig.data.bones},
)
assert {name: source_map.get(name) for name in expected_source} == expected_source
assert source_map["HAIR_ROOT"] == "c_head_1_0"
assert source_map["HAIR_TIP"] == "c_head_1_0"

helper_rig = context_rig.copy()
helper_rig.data = context_rig.data.copy()
helper_rig.name = "Inherited Helper Retarget"
bpy.context.collection.objects.link(helper_rig)
for bone_name, source_name in source_map.items():
    helper_rig.data.bones[bone_name]["g4_animation_source_joint"] = source_name

helper_motion = {
    "clip": {"name": "inherited-helper", "start_frame": 0, "end_frame": 1},
    "frames": [0, 1],
    "skeleton": {
        "names": ["c_head_1_0"],
        "parents": [-1],
        "local_trs": [rest],
    },
    "tracks": [{
        "target_name": "c_head_1_0",
        "animated_paths": ["rotation"],
        "values": {
            "translation": [[0.0, 0.0, 0.0]] * 2,
            "rotation": [[0.0, 0.0, 0.0, 1.0], [0.0, 0.38268343, 0.0, 0.92387953]],
            "scale": [[1.0, 1.0, 1.0]] * 2,
        },
    }],
}
animation.resolve_track_names_from_armature(helper_motion, helper_rig)
helper_action, _mapped_helpers, helper_targets = animation.create_retargeted_action(
    helper_rig, helper_motion, 0, True
)
assert {target.name for target in helper_targets} >= {"HAIR_ROOT", "HAIR_TIP"}
hair_tip_samples = next(target.samples for target in helper_targets if target.name == "HAIR_TIP")
for first, second in zip(hair_tip_samples[0].translation, hair_tip_samples[1].translation):
    assert abs(first - second) < 1e-5
for first, second in zip(hair_tip_samples[0].rotation_xyzw, hair_tip_samples[1].rotation_xyzw):
    assert abs(first - second) < 1e-5
for first, second in zip(hair_tip_samples[0].scale, hair_tip_samples[1].scale):
    assert abs(first - second) < 1e-5
helper_rig_data = helper_rig.data
bpy.data.objects.remove(helper_rig, do_unlink=True)
if helper_rig_data.users == 0:
    bpy.data.armatures.remove(helper_rig_data)

imported_motion = {
    "tracks": [{"target_name": source} for source in expected_source.values()]
}
animation.resolve_track_names_from_armature(imported_motion, context_rig)
track_map = {track["target_name"]: track["armature_bone_names"] for track in imported_motion["tracks"]}
assert track_map["l_a_1_0"] == ["SHOULDER_L"]
assert track_map["r_a_1_0"] == ["SHOULDER_R"]
assert track_map["l_l_1_1"] == ["CLANK_L"]
assert track_map["l_foot_1_0"] == ["TOE1_L"]
assert track_map["l_foot_1_1"] == ["TOE2_L"]

bpy.ops.object.armature_add(enter_editmode=True)
root_rig = bpy.context.object
root_rig.name = "Canonical Root Retarget"
root = root_rig.data.edit_bones[0]
root.name = "c_global_0_0"
root.head = (0.0, 0.0, 0.0)
root.tail = (0.0, 0.0, 0.2)
origin = root_rig.data.edit_bones.new("Origin")
origin.head = root.tail
origin.tail = (0.0, 0.0, 0.4)
origin.parent = root
bpy.ops.object.mode_set(mode="OBJECT")
root_rig.data.bones["Origin"]["g4_animation_source_joint"] = "c_global_0_0"
root_motion = {
    "clip": {"name": "canonical-root", "start_frame": 0, "end_frame": 1},
    "frames": [0, 1],
    "skeleton": {
        "names": ["c_global_0_0"],
        "parents": [-1],
        "local_trs": [rest],
    },
    "tracks": [{
        "target_name": "c_global_0_0",
        "animated_paths": ["translation"],
        "values": {
            "translation": [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]],
            "rotation": [[0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0, 1.0]],
            "scale": [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]],
        },
    }],
}
animation.resolve_track_names_from_armature(root_motion, root_rig)
assert root_motion["tracks"][0]["armature_bone_names"] == ["c_global_0_0"]
root_action, mapped_root, root_targets = animation.create_retargeted_action(
    root_rig, root_motion, 0, True
)
assert mapped_root == 1
assert root_targets[0].name == "c_global_0_0"
assert abs(Vector(root_targets[0].samples[1].translation).length - Vector((1.0, 2.0, 3.0)).length) < 1e-5
if root_action is not None:
    bpy.data.actions.remove(root_action, do_unlink=True)

bpy.ops.object.armature_add(enter_editmode=True)
translation_rig = bpy.context.object
translation_rig.name = "Proportional Translation Retarget"
root = translation_rig.data.edit_bones[0]
root.name = "c_global_0_0"
root.head = (0.0, 0.0, 0.0)
root.tail = (0.0, 0.0, 0.2)
waist = translation_rig.data.edit_bones.new("WAIST")
waist.head = (0.0, 0.0, 2.0)
waist.tail = (0.0, 0.0, 2.2)
waist.parent = root
upper_arm = translation_rig.data.edit_bones.new("Bip01_L_UpperArm")
upper_arm.head = (2.0, 0.0, 2.0)
upper_arm.tail = (2.2, 0.0, 2.0)
upper_arm.parent = waist
head = translation_rig.data.edit_bones.new("HEAD")
head.head = (0.0, 0.0, 3.0)
head.tail = (0.0, 0.0, 3.2)
head.parent = waist
hair_root = translation_rig.data.edit_bones.new("HAIR_ROOT")
hair_root.head = (0.0, 0.0, 4.0)
hair_root.tail = (0.0, 0.0, 4.2)
hair_root.parent = head
bpy.ops.object.mode_set(mode="OBJECT")
translation_rig.data.bones["WAIST"]["g4_animation_source_joint"] = "c_c_1_0"
translation_rig.data.bones["Bip01_L_UpperArm"]["g4_animation_source_joint"] = "l_a_1_0"
translation_rig.data.bones["HEAD"]["g4_animation_source_joint"] = "c_head_1_0"

translation_tracks = []
for name, initial, animated in (
    ("c_global_0_0", (0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    ("c_c_1_0", (0.0, 1.0, 0.0), (0.0, 1.5, 0.0)),
    ("l_a_1_0", (1.0, 0.0, 0.0), (1.25, 0.0, 0.0)),
    ("c_head_1_0", (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
):
    translation_tracks.append({
        "target_name": name,
        "animated_paths": ["translation"],
        "values": {
            "translation": [list(initial), list(animated)],
            "rotation": [[0.0, 0.0, 0.0, 1.0]] * 2,
            "scale": [[1.0, 1.0, 1.0]] * 2,
        },
    })
translation_motion = {
    "clip": {"name": "preserve-target-chain-lengths", "start_frame": 0, "end_frame": 1},
    "frames": [0, 1],
    "skeleton": {
        "names": ["c_global_0_0", "c_c_1_0", "l_a_1_0", "c_head_1_0", "c_hair_1_0"],
        "parents": [-1, 0, 1, 1, 3],
        "local_trs": [
            {"translation": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]},
            {"translation": [0.0, 1.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]},
            {"translation": [1.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]},
            {"translation": [0.0, 1.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]},
            {"translation": [0.0, 2.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]},
        ],
    },
    "tracks": translation_tracks,
}
animation.resolve_track_names_from_armature(translation_motion, translation_rig)
translation_action, translation_keyed, _targets = animation.create_retargeted_action(
    translation_rig, translation_motion, 0, False
)
assert translation_keyed == 4
bpy.context.scene.frame_set(2)
bpy.context.view_layer.update()
assert max(abs(value) for value in translation_rig.pose.bones["Bip01_L_UpperArm"].location) < 1e-5
root_position = translation_rig.pose.bones["c_global_0_0"].matrix.translation
assert (root_position - Vector((1.5, 0.0, 0.0))).length < 1e-5, tuple(root_position)
waist_position = translation_rig.pose.bones["WAIST"].matrix.translation
assert (waist_position - Vector((1.5, 0.0, 3.0))).length < 1e-5, tuple(waist_position)
translation_rig.animation_data.action = None
if translation_action is not None:
    bpy.data.actions.remove(translation_action, do_unlink=True)

bpy.ops.object.armature_add(enter_editmode=True)
chain_rig = bpy.context.object
chain_rig.name = "Unequal Spine Chain Retarget"
chain_names = ("ROOT", "PELVIS", "SPINE_A", "SPINE_B", "CHEST", "HEAD")
chain_heads = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0)
chain_bones = {}
for index, name in enumerate(chain_names):
    bone = chain_rig.data.edit_bones[0] if index == 0 else chain_rig.data.edit_bones.new(name)
    bone.name = name
    bone.head = (0.0, 0.0, chain_heads[index])
    bone.tail = (0.0, 0.0, chain_heads[index] + 0.2)
    if index:
        bone.parent = chain_bones[chain_names[index - 1]]
    chain_bones[name] = bone
bpy.ops.object.mode_set(mode="OBJECT")
for target_name, source_name in {
    "ROOT": "c_global_0_0",
    "PELVIS": "c_c_1_0",
    "SPINE_A": "c_c_1_0",
    "SPINE_B": "c_c_1_0",
    "CHEST": "c_c_1_1",
    "HEAD": "c_head_1_0",
}.items():
    chain_rig.data.bones[target_name]["g4_animation_source_joint"] = source_name

chain_source_names = ("c_global_0_0", "c_c_1_0", "c_c_1_1", "c_head_1_0")
chain_rest = [
    {"translation": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]},
    {"translation": [0.0, 1.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]},
    {"translation": [0.0, 1.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]},
    {"translation": [0.0, 1.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0], "scale": [1.0, 1.0, 1.0]},
]
chain_motion = {
    "clip": {"name": "distribute-spine-pose", "start_frame": 0, "end_frame": 1},
    "frames": [0, 1],
    "skeleton": {
        "names": list(chain_source_names),
        "parents": [-1, 0, 1, 2],
        "local_trs": chain_rest,
    },
    "tracks": [
        {
            "target_name": name,
            "animated_paths": ["rotation"],
            "values": {
                "translation": [[0.0, 0.0, 0.0]] * 2,
                "rotation": (
                    [[0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.70710678, 0.70710678]]
                    if name == "c_c_1_1"
                    else [[0.0, 0.0, 0.0, 1.0]] * 2
                ),
                "scale": [[1.0, 1.0, 1.0]] * 2,
            },
        }
        for name in chain_source_names
    ],
}
animation.resolve_track_names_from_armature(chain_motion, chain_rig)
_chain_action, _chain_keyed, chain_targets = animation.create_retargeted_action(
    chain_rig, chain_motion, 0, True
)
chain_samples = {target.name: target.samples for target in chain_targets}
for target_name in ("SPINE_A", "SPINE_B", "CHEST"):
    before = Quaternion(chain_samples[target_name][0].rotation_xyzw[::-1])
    after = Quaternion(chain_samples[target_name][1].rotation_xyzw[::-1])
    angle_degrees = math.degrees(before.rotation_difference(after).angle)
    assert 25.0 < angle_degrees < 35.0, (target_name, angle_degrees)
chain_rig_data = chain_rig.data
bpy.data.objects.remove(chain_rig, do_unlink=True)
if chain_rig_data.users == 0:
    bpy.data.armatures.remove(chain_rig_data)

bpy.context.view_layer.objects.active = context_rig
bpy.ops.object.mode_set(mode="EDIT")
left_hand = context_rig.data.edit_bones.new("LHand")
left_hand.head = (-0.5, 0.0, 0.2)
left_hand.tail = (-0.6, 0.0, 0.2)
left_hand.parent = context_rig.data.edit_bones["SPINE1"]
bpy.ops.object.mode_set(mode="OBJECT")
context_rig.data.bones["LHand"]["g4_animation_source_joint"] = "l_w_1_0"
explicit_alias_motion = {
    "tracks": [
        {"target_name": "l_w_1_0"},
        {"target_name": "l_wph_1_0"},
    ]
}
animation.resolve_track_names_from_armature(explicit_alias_motion, context_rig)
assert explicit_alias_motion["tracks"][0]["armature_bone_names"] == ["LHand"]
assert explicit_alias_motion["tracks"][1]["armature_bone_names"] == []
print("SEMANTIC_RETARGET_MAPPING_OK")
