#!/usr/bin/env python3
"""Decode Level-5 G4CM camera curves to a Blender-friendly JSON document."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import struct
from pathlib import Path

try:
    from .g4mt_probe import crc32b, find_name_table, u16, u32
except ImportError:
    from g4mt_probe import crc32b, find_name_table, u16, u32


CAMERA_HASH = crc32b("Camera")
CHANNEL_PATHS = {
    22: ("position", 0),
    23: ("position", 1),
    24: ("position", 2),
    26: ("target", 0),
    27: ("target", 1),
    28: ("target", 2),
    30: ("roll", None),
    31: ("fov", None),
}


def select_clip(clips: list[dict], selector: str) -> dict:
    if selector.isdigit():
        index = int(selector)
        if index < len(clips):
            return clips[index]
    for clip in clips:
        if clip["name"] == selector:
            return clip
    raise ValueError(f"clip not found: {selector}")


def parse_g4cm(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:4] != b"G4CM":
        raise ValueError(f"{path} is not a G4CM file")

    header_size = u16(data, 0x04)
    header_words = u16(data, 0x0A)
    if header_size != 0x40 or header_size != header_words * 4:
        raise ValueError("inconsistent G4CM header size")
    if u32(data, 0x0C) + header_size != len(data):
        raise ValueError("inconsistent G4CM content size")

    clip_count = u16(data, 0x20)
    target_count = u16(data, 0x22)
    offset_shift = data[0x36]
    target_info_units = u16(data, 0x24)
    channel_units = u16(data, 0x26)
    section_units = [u16(data, 0x28 + index * 2) for index in range(6)]
    scale_offset, clip_hash_offset, target_hash_offset, name_meta_offset = [
        (header_words + value) * 4 for value in section_units[:4]
    ]
    target_info_offset = (header_words + (target_info_units << offset_shift)) * 4
    channel_offset = (header_words + (channel_units << offset_shift)) * 4
    key_offset = (header_words + (section_units[4] << (offset_shift * 2))) * 4
    data_offset = (header_words + (section_units[5] << (offset_shift * 2))) * 4

    clip_rows = [
        struct.unpack_from("<HHHHBBBBI", data, header_size + index * 0x10)
        for index in range(clip_count)
    ]
    scale_count = (clip_hash_offset - scale_offset) // 4
    scales = list(struct.unpack_from(f"<{scale_count}f", data, scale_offset)) if scale_count else []
    clip_hashes = list(struct.unpack_from(f"<{clip_count}I", data, clip_hash_offset))
    target_hashes = list(struct.unpack_from(f"<{target_count}I", data, target_hash_offset))
    if target_count != 1 or target_hashes != [CAMERA_HASH]:
        hashes = ", ".join(f"{value:08x}" for value in target_hashes)
        raise ValueError(f"unsupported G4CM targets: {hashes or '<none>'}")

    clip_order = list(struct.unpack_from(f"<{clip_count}H", data, name_meta_offset))
    _, _, clip_names = find_name_table(data, name_meta_offset, clip_count)
    target_info_count = max(
        (row[2] + (row[6] << 16) + row[3] for row in clip_rows),
        default=0,
    )
    target_infos = []
    for index in range(target_info_count):
        offset = target_info_offset + index * 8
        target_index, channel_low, channel_count, channel_high, reserved = struct.unpack_from(
            "<HHBBH", data, offset
        )
        if target_index >= target_count:
            raise ValueError(f"target-info {index} refers to missing target {target_index}")
        target_infos.append({
            "index": index,
            "target_index": target_index,
            "channel_start": channel_low + (channel_high << 16),
            "channel_count": channel_count,
            "reserved": reserved,
        })

    channel_count = max(
        (info["channel_start"] + info["channel_count"] for info in target_infos),
        default=0,
    )
    channel_headers = []
    key_count = 0
    for index in range(channel_count):
        offset = channel_offset + index * 20
        encoding, key_start, entry_data_offset, entry_key_count = struct.unpack_from(
            "<8sIII", data, offset
        )
        encoding = list(encoding)
        if encoding[0] not in CHANNEL_PATHS:
            raise ValueError(f"unsupported G4CM channel type {encoding[0]}")
        key_count = max(key_count, key_start + entry_key_count)
        channel_headers.append((encoding, key_start, entry_data_offset, entry_key_count))

    all_keys = list(struct.unpack_from(f"<{key_count}H", data, key_offset)) if key_count else []
    channels = []
    for index, (encoding, key_start, entry_data_offset, entry_key_count) in enumerate(channel_headers):
        channels.append({
            "index": index,
            "channel_type": encoding[0],
            "encoding": encoding,
            "key_start": key_start,
            "data_offset": entry_data_offset,
            "key_count": entry_key_count,
            "keys": all_keys[key_start:key_start + entry_key_count],
        })

    clips = []
    for index, row in enumerate(clip_rows):
        clips.append({
            "index": index,
            "name": clip_names[index],
            "crc32b": f"{clip_hashes[index]:08x}",
            "start_frame": row[0],
            "end_frame": row[1],
            "frame_count": row[1] - row[0] + 1,
            "flags": row[4],
            "fps": row[5],
            "target_info_start": row[2] + (row[6] << 16),
            "target_info_count": row[3],
        })

    return {
        "source": str(path),
        "file_type": u16(data, 0x06),
        "version": u32(data, 0x08),
        "offset_shift": offset_shift,
        "clip_order": clip_order,
        "clips": clips,
        "target_infos": target_infos,
        "channels": channels,
        "scales": scales,
        "section_offsets": {"keys": key_offset, "data": data_offset},
        "data": data,
    }


def decode_key(data: bytes, data_base: int, channel: dict, scale: float, key_index: int) -> float:
    encoding = channel["encoding"]
    if encoding[4] != 1:
        raise ValueError(f"G4CM channel {channel['index']} is not scalar")
    offset = data_base + channel["data_offset"] + key_index * encoding[5]
    codec, variant = encoding[1], encoding[3]
    if codec == 1 and variant == 1:
        return float(struct.unpack_from("<b", data, offset)[0])
    if codec == 1 and variant == 2:
        return float(struct.unpack_from("<h", data, offset)[0])
    if codec == 1 and variant == 4:
        return struct.unpack_from("<f", data, offset)[0]
    if codec == 2 and variant == 2:
        return struct.unpack_from("<H", data, offset)[0] * scale / 65536.0
    if codec == 3 and variant == 2:
        return struct.unpack_from("<h", data, offset)[0] * scale / 32768.0
    raise ValueError(f"unsupported G4CM codec {codec}/{variant}: {encoding}")


def sample_channel(parsed: dict, channel: dict, frame: float) -> float:
    keys = channel["keys"]
    if not keys:
        raise ValueError(f"G4CM channel {channel['index']} has no keys")
    right = bisect.bisect_right(keys, frame)
    if right == 0:
        scale_index = channel["encoding"][6]
        try:
            scale = parsed["scales"][scale_index]
        except IndexError as exc:
            raise ValueError(f"G4CM channel {channel['index']} has an invalid scale index") from exc
        return decode_key(parsed["data"], parsed["section_offsets"]["data"], channel, scale, 0)
    left = max(0, min(len(keys) - 1, right - 1))
    scale_index = channel["encoding"][6]
    try:
        scale = parsed["scales"][scale_index]
    except IndexError as exc:
        raise ValueError(f"G4CM channel {channel['index']} has an invalid scale index") from exc
    first = decode_key(parsed["data"], parsed["section_offsets"]["data"], channel, scale, left)
    if left == len(keys) - 1 or channel["encoding"][2] == 0:
        return first
    span = keys[left + 1] - keys[left]
    amount = (frame - keys[left]) / span if span else 0.0
    second = decode_key(parsed["data"], parsed["section_offsets"]["data"], channel, scale, left + 1)
    return first + (second - first) * amount


def decode_camera(path: Path, clip_selector: str) -> dict:
    parsed = parse_g4cm(path)
    clip = select_clip(parsed["clips"], clip_selector)
    if clip["flags"]:
        raise ValueError(f"unsupported G4CM clip flags: {clip['flags']:#x}")
    infos = parsed["target_infos"][
        clip["target_info_start"]:clip["target_info_start"] + clip["target_info_count"]
    ]
    if len(infos) != 1:
        raise ValueError(f"G4CM clip must contain one camera target, found {len(infos)}")
    info = infos[0]
    channels = parsed["channels"][
        info["channel_start"]:info["channel_start"] + info["channel_count"]
    ]
    if {channel["channel_type"] for channel in channels} != set(CHANNEL_PATHS):
        raise ValueError("G4CM clip does not contain the expected eight camera channels")

    source_frames = list(range(clip["start_frame"], clip["end_frame"] + 1))
    fps = clip["fps"] or 60
    samples = []
    for frame in source_frames:
        sample = {
            "position": [0.0, 0.0, 0.0],
            "target": [0.0, 0.0, 0.0],
            "roll": 0.0,
            "fov": math.radians(50.0),
        }
        for channel in channels:
            path_name, component = CHANNEL_PATHS[channel["channel_type"]]
            value = sample_channel(parsed, channel, frame)
            if component is None:
                sample[path_name] = value
            else:
                sample[path_name][component] = value
        samples.append(sample)

    return {
        "source": str(path),
        "coordinate_system": "right-handed, Y-up",
        "clip": clip,
        "frames": source_frames,
        "times": [(frame - clip["start_frame"]) / fps for frame in source_frames],
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("g4cm", type=Path)
    parser.add_argument("--clip", default="0", help="Clip index or exact clip name")
    parser.add_argument("--output", type=Path, help="Write JSON here instead of stdout")
    args = parser.parse_args()
    payload = json.dumps(decode_camera(args.g4cm, args.clip), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
