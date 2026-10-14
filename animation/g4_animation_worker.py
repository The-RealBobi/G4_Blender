from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

import bpy


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def run(job_path: Path) -> int:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    addon_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(addon_root.parent))
    from G4_Blender.animation.g4_animation_addon import export_animation_bank_resources

    armature = bpy.data.objects.get(job["armature_name"])
    if armature is None or armature.type != "ARMATURE":
        raise RuntimeError(f"Worker blend has no target armature named {job['armature_name']}")
    if armature.name not in bpy.context.scene.objects:
        bpy.context.scene.collection.objects.link(armature)
    for bone_name, source_name in job.get("target_joint_mapping", {}).items():
        bone = armature.data.bones.get(bone_name)
        if bone is not None and source_name:
            bone["g4_animation_source_joint"] = source_name
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)

    resources = []
    for item in job["resources"]:
        if item["kind"] in {"g4pk_entry", "g4mt_clip_group"}:
            resources.append(item)
        else:
            resources.append((Path(item["path"]), Path(item["data_root"]), bool(item["is_event"])))
    generator = export_animation_bank_resources(
        resources,
        job["character_bank_id"],
        job["event_skeleton_id"],
        Path(job["output_root"]),
        armature,
        Path(job["source_skeleton_override"]) if job["source_skeleton_override"] else None,
        job["target_bank_id"],
    )
    progress_path = Path(job["progress_path"])
    cancel_path = Path(job["cancel_path"])
    result_path = Path(job["result_path"])
    progress = {
        "branch": job["branch"],
        "completed": 0.0,
        "total": len(resources),
        "message": "Starting…",
        "updated_at": time.time(),
    }
    write_json(progress_path, progress)
    last_progress_write = time.monotonic()
    try:
        while True:
            if cancel_path.exists():
                generator.close()
                write_json(result_path, {"status": "cancelled"})
                return 2
            try:
                update = next(generator)
            except StopIteration as completed:
                written, skipped, warnings = completed.value
                write_json(result_path, {
                    "status": "complete",
                    "written": written,
                    "skipped": skipped,
                    "warnings": warnings,
                })
                return 0
            if isinstance(update, tuple):
                fraction, message = update
                progress["completed"] = fraction * progress["total"]
                progress["message"] = message
            else:
                progress["message"] = update
            now = time.monotonic()
            if isinstance(update, tuple) or now - last_progress_write >= 0.2:
                progress["updated_at"] = time.time()
                write_json(progress_path, progress)
                last_progress_write = now
    except Exception:
        error = traceback.format_exc()
        try:
            generator.close()
        except Exception:
            error += "\nDuring generator cleanup:\n" + traceback.format_exc()
        write_json(result_path, {"status": "failed", "error": error})
        raise


def main() -> int:
    try:
        separator = sys.argv.index("--")
        job_path = Path(sys.argv[separator + 1])
    except (ValueError, IndexError):
        raise RuntimeError("Expected a worker job file after --")
    return run(job_path)


if __name__ == "__main__":
    sys.exit(main())
