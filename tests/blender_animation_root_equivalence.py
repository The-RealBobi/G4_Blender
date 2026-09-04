import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import bpy,G4_Blender
from G4_Blender import g4_animation_addon as a
G4_Blender.register()
import argparse
parser = argparse.ArgumentParser(description="Compare root extraction against native bone motion in Blender")
parser.add_argument("--model", required=True, type=Path)
parser.add_argument("--animation", required=True, type=Path)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
model, package = args.model, args.animation
path,_,_=a.materialize_g4mt(package,'0');m=a.decode_motion(path,'0',model)
rig,_=a.import_model_for_animation(package,m,a.addon_preferences(),model,False)
base=rig.matrix_basis.copy();root=a.event_root_motion_name(m,rig)
print('ROOT',root,len(m['frames']))
plain,_=a.create_action(rig,m)
assert any(curve.data_path == rig.pose.bones[root].path_from_id("location")
           for curve in a.action_fcurves(plain)), "Normal import discarded root translation"
frames=[1,len(m['frames'])//2,len(m['frames'])]
expected={}
mesh_expected={}
def mesh_positions():
 result=[]
 depsgraph=bpy.context.evaluated_depsgraph_get()
 for obj in bpy.context.scene.objects:
  if obj.type!='MESH' or not any(mod.type=='ARMATURE' and mod.object==rig for mod in obj.modifiers):continue
  evaluated=obj.evaluated_get(depsgraph)
  mesh=evaluated.to_mesh()
  try: result.extend(evaluated.matrix_world@vertex.co for vertex in mesh.vertices)
  finally: evaluated.to_mesh_clear()
 return result
for f in frames:
 bpy.context.scene.frame_set(f);expected[f]={b.name:rig.matrix_world@b.matrix for b in rig.pose.bones}
 mesh_expected[f]=mesh_positions()
rig.animation_data.action=None;a.clear_pose(rig);rig.matrix_basis=base
extracted,_=a.create_action(rig,m,event_root_name=root)
assert a.extract_event_root_motion(extracted,m,root,base,rig)>0
maximum=0
for f in frames:
 bpy.context.scene.frame_set(f)
 for b in rig.pose.bones:
  if b.name!=root and root not in [x.name for x in b.parent_recursive]:continue
  actual=rig.matrix_world@b.matrix
  error=max(abs(actual[i][j]-expected[f][b.name][i][j]) for i in range(4) for j in range(4));maximum=max(maximum,error)
mesh_error=0
for f in frames:
 bpy.context.scene.frame_set(f)
 actual=mesh_positions()
 assert len(actual)==len(mesh_expected[f])
 mesh_error=max(mesh_error,max(((v-w).length for v,w in zip(actual,mesh_expected[f])),default=0))
print('MAX_SKINNED_VERTEX_ERROR',mesh_error)
assert mesh_error<2e-4,mesh_error
print('MAX_ROOT_SUBTREE_MATRIX_ERROR',maximum)
assert maximum<2e-4,maximum
