#!/usr/bin/env python3
"""Decode Level-5 G4MA material-animation curves without assigning semantics.

G4MA shares the clip/container layout of G4MT but its target hashes and
channel types address runtime material parameters, not skeletal transforms.
This module intentionally exposes those curves verbatim.  Mapping a target to
a Blender node socket belongs to the effect importer once that mapping has
been resolved from the source asset/runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

try:
    from .g4mt_motion import encoding_step, sample_channel, select_clip
    from .g4mt_probe import parse_g4mt
    from .g4pk_extract_g4mt import entries
except ImportError:
    from g4mt_motion import encoding_step, sample_channel, select_clip
    from g4mt_probe import parse_g4mt
    from g4pk_extract_g4mt import entries


def material_animation_paths(model: Path, cache_root: Path) -> list[Path]:
    """Resolve packed animations first, retaining unmatched extracted companions."""
    data = model.read_bytes()
    resolved = []
    packed_names = set()
    destination = cache_root / hashlib.sha256(data).hexdigest()
    for index, (name, offset, size) in enumerate(entries(data)):
        if data[offset:offset + 4] != b"G4MA":
            continue
        safe_name = Path(name.replace("\\", "/")).name
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"{index}_{safe_name}"
        payload = data[offset:offset + size]
        if not path.is_file() or path.read_bytes() != payload:
            path.write_bytes(payload)
        resolved.append(path)
        packed_names.add(safe_name.casefold())
    for path in sorted((model.parent / model.stem).glob("*.g4ma")):
        if path.name.casefold() not in packed_names:
            resolved.append(path)
    return resolved


def decode_material_motion(path: Path, clip_selector: str) -> dict:
    """Return raw G4MA tracks for one clip, sampled at the source frame rate."""
    parsed = parse_g4mt(path)
    if parsed["magic"] != "G4MA":
        raise ValueError(f"{path} is not a G4MA material-animation container")
    clip = select_clip(parsed["clips"], clip_selector)
    if clip["flags"] & 1:
        raise ValueError("additive G4MA clips need a base material state and are not decoded yet")

    data = path.read_bytes()
    frames = list(range(clip["start_frame"], clip["end_frame"] + 1))
    fps = clip["fps"] or 60
    tracks = []
    target_infos = parsed["target_infos"][
        clip["target_info_start"]:clip["target_info_start"] + clip["target_info_count"]
    ]
    for info in target_infos:
        target = parsed["targets"][info["target_index"]]
        channels = parsed["channels"][info["channel_start"]:info["channel_start"] + info["channel_count"]]
        curves = []
        for channel in channels:
            scale_index = channel["encoding"][6]
            if scale_index >= len(parsed["scales"]):
                raise ValueError(f"G4MA channel {channel['index']} has an invalid scale index")
            values = [
                sample_channel(
                    data,
                    parsed["section_offsets"]["data"],
                    channel,
                    parsed["scales"][scale_index],
                    frame,
                )
                for frame in frames
            ]
            curves.append({
                "channel_index": channel["index"],
                "channel_type": channel["channel_type"],
                "encoding": channel["encoding"],
                "keys": channel["keys"],
                "interpolation": "STEP" if encoding_step(channel) else "LINEAR",
                "values": values,
            })
        tracks.append({
            "target_index": info["target_index"],
            "target_hash": target["crc32b"],
            "curves": curves,
        })
    return {
        "source": str(path),
        "clip": clip,
        "frames": frames,
        "times": [(frame - clip["start_frame"]) / fps for frame in frames],
        "tracks": tracks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("g4ma", type=Path)
    parser.add_argument("clip", help="Clip name or zero-based index")
    parser.add_argument("--output", type=Path, help="Write JSON to this path instead of stdout")
    args = parser.parse_args()
    payload = json.dumps(decode_material_motion(args.g4ma, args.clip), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
