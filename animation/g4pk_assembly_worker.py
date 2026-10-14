from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from g4pk_extract_g4mt import entries, replace_entries


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def merge_clip_groups(template: bytes, clip_groups: list[dict], writer):
    parts = []
    for group in clip_groups:
        status = json.loads(Path(group["status_path"]).read_text(encoding="utf-8"))
        if status.get("status") == "skipped":
            return None
        if status.get("status") != "complete":
            raise RuntimeError(f"Animation clip group has unexpected status: {status.get('status')}")
        hierarchy_data = json.loads(Path(group["hierarchy_path"]).read_text(encoding="utf-8"))
        hierarchy = tuple(
            (str(name), parent, int(clip_index), int(target_order))
            for name, parent, clip_index, target_order in hierarchy_data
        )
        parts.append((
            tuple(int(index) for index in group["clip_indices"]),
            Path(group["result_path"]).read_bytes(),
            hierarchy,
        ))
    return writer(template, parts)


def run(job_path: Path) -> int:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    progress_path = Path(job["progress_path"])
    result_path = Path(job["result_path"])
    cancel_path = Path(job["cancel_path"])
    assemblies = job["assemblies"]
    addon_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(addon_root))
    from formats.g4mt_writer import merge_g4mt_family_preserving_additive

    try:
        skipped_banks_total = 0
        for assembly_index, assembly in enumerate(assemblies, 1):
            if cancel_path.exists():
                write_json(result_path, {"status": "cancelled"})
                return 2
            source_path = Path(assembly["source_path"])
            write_json(progress_path, {
                "completed": assembly_index - 1,
                "total": len(assemblies),
                "message": f"Rebuilding {source_path.name}",
            })
            source = source_path.read_bytes()
            destination = Path(job["output_root"]) / Path(assembly["relative_path"])

            if assembly["kind"] == "g4mt":
                output = merge_clip_groups(source, assembly["clip_groups"], merge_g4mt_family_preserving_additive)
                if output is None:
                    output = source
                    skipped_banks = 1
                else:
                    skipped_banks = 0
            else:
                replacements = {}
                entry_names = {}
                skipped_banks = 0
                parsed_entries = entries(source)
                for entry in assembly["entries"]:
                    index = int(entry["index"])
                    if entry["name"]:
                        entry_names[index] = entry["name"]
                    if "clip_groups" in entry:
                        _name, offset, size = parsed_entries[index]
                        template = source[offset:offset + size]
                        output_bank = merge_clip_groups(
                            template, entry["clip_groups"], merge_g4mt_family_preserving_additive
                        )
                        if output_bank is None:
                            skipped_banks += 1
                        else:
                            replacements[index] = output_bank
                    elif Path(entry["result_path"]).is_file():
                        replacements[index] = Path(entry["result_path"]).read_bytes()
                output = replace_entries(source, replacements, entry_names)

            skipped_banks_total += skipped_banks
            write_bytes(destination, output)
            sidecar = source_path.with_suffix(".mevbin")
            if sidecar.is_file():
                shutil.copy2(sidecar, destination.with_suffix(".mevbin"))

        write_json(progress_path, {
            "completed": len(assemblies),
            "total": len(assemblies),
            "message": "Rebuilt all animation resources",
        })
        write_json(result_path, {
            "status": "complete",
            "written": len(assemblies),
            "skipped": skipped_banks_total,
        })
        return 0
    except Exception:
        write_json(result_path, {"status": "failed", "error": traceback.format_exc()})
        raise


def main() -> int:
    try:
        job_path = Path(sys.argv[1])
    except IndexError:
        raise RuntimeError("Expected an assembly job JSON path")
    return run(job_path)


if __name__ == "__main__":
    sys.exit(main())
