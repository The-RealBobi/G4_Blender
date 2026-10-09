import json
import os
import sys
from pathlib import Path

import bpy

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo.parent))
from G4_Blender import import_native_g4_mesh

payload_path = Path(os.environ["G4_NATIVE_MESH_PAYLOAD"])
payload = json.loads(payload_path.read_text(encoding="utf-8"))
assert payload["format"] == "level5-g4-native-mesh"

imported_names = import_native_g4_mesh(payload_path)
meshes = [bpy.data.objects[name] for name in imported_names if bpy.data.objects[name].type == "MESH"]
armatures = [bpy.data.objects[name] for name in imported_names if bpy.data.objects[name].type == "ARMATURE"]
assert len(meshes) == len(payload["meshes"]) > 0
assert all(len(mesh.data.vertices) > 0 and len(mesh.data.polygons) > 0 for mesh in meshes)
assert len(armatures) == 1
assert all(
    any(modifier.type == "ARMATURE" and modifier.object == armatures[0] for modifier in mesh.modifiers)
    for mesh in meshes
)
assert any(mesh.vertex_groups for mesh in meshes)
print(f"NATIVE_MESH_IMPORT_OK meshes={len(meshes)} armature_bones={len(armatures[0].data.bones)}")
