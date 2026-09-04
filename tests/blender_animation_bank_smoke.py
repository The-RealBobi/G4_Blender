"""Run with Blender --factory-startup --background --python FILE -- --model ... --animation ..."""
import argparse
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import G4_Blender
from G4_Blender import g4_animation_addon as animation

parser = argparse.ArgumentParser()
parser.add_argument('--model', type=Path, required=True)
parser.add_argument('--animation', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
G4_Blender.register()
path, temporary, _ = animation.materialize_g4mt(args.animation, '0')
clips = animation.parse_g4mt(path)['clips']
if temporary is not None:
    temporary.unlink()
expected = [clip for clip in clips if not clip['flags'] & 1]
result = bpy.ops.import_scene.level5_g4mt(
    filepath=str(args.animation), model_path=str(args.model),
    prompt_for_models=False, import_camera=False,
)
assert result == {'FINISHED'}, result
rig = bpy.context.object
active = rig.animation_data.action
assert len(bpy.data.actions) == len(expected), (len(bpy.data.actions), len(expected))
assert active['g4mt_clip_index'] == next((c['index'] for c in expected if c['frame_count'] > 2), expected[0]['index'])
for action in bpy.data.actions:
    curves = animation.action_fcurves(action)
    assert curves, action.name
    rig.animation_data.action = action
    if hasattr(rig.animation_data, 'action_slot'):
        assert rig.animation_data.action_slot is not None, action.name
    bpy.context.scene.frame_set(1)
    assert all(rig.path_resolve(curve.data_path) is not None for curve in curves)
rig.animation_data.action = active
print(f'ANIMATION_BANK_OK clips={len(expected)} unsupported={len(clips)-len(expected)} active={active.name}')
