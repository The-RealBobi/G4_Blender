import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector


repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo.parent))
sys.path.insert(0, str(repo))
import G4_Blender
G4_Blender.__path__ = [str(repo)]
if 'G4_Blender.formats' in sys.modules:
    sys.modules['G4_Blender.formats'].__path__ = [str(repo / 'formats')]
sys.modules.pop('G4_Blender.g4_animation_addon', None)
from G4_Blender.g4_animation_addon import sample_action_for_g4mt


bpy.ops.object.armature_add(enter_editmode=True)
armature = bpy.context.object
root = armature.data.edit_bones[0]
root.name = 'root'
root.head = (0.0, 0.0, 0.0)
root.tail = (0.0, 0.0, 1.0)
child = armature.data.edit_bones.new('child')
child.head = (0.0, 0.0, 1.0)
child.tail = (0.0, 0.0, 2.0)
child.parent = root
bpy.ops.object.mode_set(mode='OBJECT')

action = bpy.data.actions.new('Axis conversion check')
armature.animation_data_create()
armature.animation_data.action = action
for frame, root_values, child_values in (
    (1, ((0.2, -0.1, 0.3), (0.35, 0.0, 0.9)), ((0.0, 0.1, 0.15), (-0.4, 0.2, 0.5))),
    (3, ((-0.1, 0.25, 0.05), (-0.2, 0.6, -0.3)), ((0.1, -0.1, 0.0), (0.1, -0.3, 0.7))),
):
    for name, (location, rotation) in (('root', root_values), ('child', child_values)):
        pose_bone = armature.pose.bones[name]
        pose_bone.rotation_mode = 'XYZ'
        pose_bone.location = location
        pose_bone.rotation_euler = rotation
        pose_bone.keyframe_insert(data_path='location', frame=frame)
        pose_bone.keyframe_insert(data_path='rotation_euler', frame=frame)
action.use_frame_range = True
action.frame_start = 1
action.frame_end = 3

blender_to_g4 = Matrix.Rotation(-math.pi / 2.0, 4, 'X')
g4_to_blender = blender_to_g4.inverted()


def sampled_matrix(sample):
    return Matrix.LocRotScale(
        Vector(sample.translation),
        Quaternion((sample.rotation_xyzw[3], *sample.rotation_xyzw[:3])),
        Vector(sample.scale),
    )


def verify(convert_to_g4):
    if convert_to_g4:
        if 'g4_character_model_source' in armature:
            del armature['g4_character_model_source']
    else:
        armature['g4_character_model_source'] = 'native test rig'
    targets = sample_action_for_g4mt(armature, action, bpy.context.scene, bpy.context.view_layer)
    assert [target.name for target in targets] == ['root', 'child']
    for frame_index, frame in enumerate((1, 2, 3)):
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        for target in targets:
            pose_bone = armature.pose.bones[target.name]
            local = pose_bone.parent.matrix.inverted_safe() @ pose_bone.matrix if pose_bone.parent else pose_bone.matrix.copy()
            expected = blender_to_g4 @ local @ g4_to_blender if convert_to_g4 else local
            actual = sampled_matrix(target.samples[frame_index])
            error = max(abs(expected[row][column] - actual[row][column]) for row in range(4) for column in range(4))
            assert error < 1e-5, (target.name, frame, convert_to_g4, error)


verify(True)
verify(False)
print('G4MT_AXIS_CONVERSION_OK external=BlenderToG4 native=unchanged')
