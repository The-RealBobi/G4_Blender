"""Export Tsubasa as separate c11010060 face and u11010060 uniform packages."""

import json
import os
import sys
from pathlib import Path

import bpy


REPO = Path(__file__).resolve().parents[1]
RAW = Path("/Volumes/BOBI/Proyectos Personales/VictoryRoad/DUMP_702/._work/raw/data")
OUTPUT = Path(os.environ.get("G4_PORT_TEST_OUTPUT", "/Users/bobi/Documents/MiMod/tsubasa_c11010060_u11010060_v1"))
sys.path.insert(0, str(REPO))
import g4_port_addon as port


FACE = RAW / "common/chr/_face/11_VICTORY/c11010060/c11010060.g4md"
UNIFORM = RAW / "common/chr/_uniform/u11010060/u11010060.g4md"


def select_only(names):
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    selected = []
    for obj in bpy.data.objects:
        if obj.type == "MESH" and obj.name in names:
            obj.select_set(True)
            selected.append(obj)
    if not selected:
        raise RuntimeError(f"No source objects selected for {sorted(names)}")
    bpy.context.view_layer.objects.active = selected[0]
    return selected


def configure(original, assignments):
    props = bpy.context.scene.level5_g4_port
    prefs = port.addon_preferences()
    prefs.keep_temporary_files = True
    prefs.cache_dir = "/private/tmp/tsubasa_modular_port_cache"
    port.addon_preferences = lambda: prefs
    summary = port.run_model_probe(original, prefs)
    port.apply_original_model_to_settings(props, original, summary)
    props.texture_mode = os.environ.get("G4_PORT_TEST_TEXTURE_MODE", "native")
    props.generate_png_set_on_export = props.texture_mode == "custom"
    props.selected_only = True
    props.apply_modifiers = False
    props.bake_current_pose = True
    props.strict_skinning = True
    props.generate_tangents = True
    selected = select_only(assignments)
    for obj in selected:
        obj.level5_g4_port.target_record = assignments[obj.name]
    return props


port.register()
face_assignments = {
    "hair_mesh": "c11010060_20",
    "hair_mesh.001": "c11010060_20",
    "nose_mesh": "c11010060_20",
    "face_mesh": "c11010060_20",
    "face_mesh.001": "c11010060_20",
}
configure(FACE, face_assignments)
face_report, _ = port.run_port(bpy.context, str(OUTPUT))

uniform_assignments = {
    "body_mesh": "u11010060_10",
    "body_mesh buttons": "u11010060_10",
    "body_mesh buttons.001": "u11010060_10",
    "hand_mesh": "u11010060_10",
}
configure(UNIFORM, uniform_assignments)
uniform_report, _ = port.run_port(bpy.context, str(OUTPUT))

print("TSUBASA_MODULAR_REPORT=" + json.dumps({"face": face_report, "uniform": uniform_report}, sort_keys=True))
