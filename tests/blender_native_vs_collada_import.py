import json
import os
import sys
from pathlib import Path

import bpy

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo.parent))
from G4_Blender import import_native_g4_mesh

payload_path = Path(os.environ["G4_NATIVE_MESH_PAYLOAD"])
dae_path = Path(os.environ["G4_COMPARE_DAE"])
payload = json.loads(payload_path.read_text(encoding="utf-8"))


def mesh_geometry(objects):
    return sorted(
        (len(obj.data.vertices), len(obj.data.polygons), len(obj.data.uv_layers))
        for obj in objects if obj.type == "MESH"
    )


native_names = import_native_g4_mesh(payload_path)
native_objects = [bpy.data.objects[name] for name in native_names]
native_geometry = mesh_geometry(native_objects)
native_armatures = [obj for obj in native_objects if obj.type == "ARMATURE"]
assert len(native_armatures) == 1
native_bones = set(native_armatures[0].data.bones.keys())

for obj in tuple(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

before = set(bpy.data.objects)
bpy.ops.wm.collada_import(filepath=str(dae_path))
dae_objects = set(bpy.data.objects) - before
dae_geometry = mesh_geometry(dae_objects)
dae_armatures = [obj for obj in dae_objects if obj.type == "ARMATURE"]
assert dae_geometry == native_geometry, (native_geometry, dae_geometry)
assert len(dae_armatures) == 1
assert set(dae_armatures[0].data.bones.keys()) == native_bones
assert len([obj for obj in dae_objects if obj.type == "MESH"]) == len(payload["meshes"])
print(f"NATIVE_DAE_GEOMETRY_MATCH meshes={len(native_geometry)} bones={len(native_bones)}")
