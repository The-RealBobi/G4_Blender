import os
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo.parent))
from G4_Blender import import_native_g4_mesh, run_exporter, write_debug_log

source_path = Path(os.environ["G4_SOURCE_MODEL"])
export_dir = Path(os.environ["G4_EXPORT_DIR"])
raw_root = Path(os.environ["LEVEL5_G4_RAW_ROOT"])
prefs = SimpleNamespace(
    python_path=os.environ["G4_PYTHON"],
    probe_script=str(repo / "porting" / "g4_model_probe.py"),
    export_dir=str(export_dir),
    raw_data_root=str(raw_root),
    chara_model_lookup=str(repo / "data" / "chara_model_lookup.json"),
    chara_model_xml="",
)

summary = run_exporter(str(source_path), prefs)
assert summary.get("dae") in (None, ""), summary
native_path = Path(summary["native_mesh"])
assert native_path.is_file()
assert not (export_dir / f"{source_path.stem}.dae").exists()
write_debug_log(summary, ["native mesh pipeline smoke"])
assert native_path.with_suffix(".g4_import_debug.log").is_file()
imported = import_native_g4_mesh(native_path)
assert any(bpy.data.objects[name].type == "MESH" for name in imported)
print(f"NATIVE_ONLY_PIPELINE_OK meshes={summary['mesh_count']} payload={native_path.name}")
