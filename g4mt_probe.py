#!/usr/bin/env python3
"""Inspect Level-5 G4MT animation containers without modifying source assets."""

from __future__ import annotations

import argparse
import json
import re
import struct
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def crc32b(text: str) -> int:
    return zlib.crc32(text.encode("ascii")) & 0xFFFFFFFF


def cstr(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("utf-8", errors="replace")


def find_name_table(data: bytes, start: int, count: int) -> tuple[int, list[int], list[str]]:
    search_end = min(len(data), start + max(0x100, count * 8))
    for base in range(start, search_end, 2):
        if base + count * 2 > len(data):
            break
        offsets = list(struct.unpack_from(f"<{count}H", data, base))
        names = []
        valid = True
        for value in offsets:
            absolute = base + value
            if value < count * 2 or absolute >= len(data):
                valid = False
                break
            name = cstr(data, absolute)
            if not name or "\ufffd" in name or any(ord(char) < 0x20 for char in name):
                valid = False
                break
            names.append(name)
        if valid:
            return base, offsets, names
    raise ValueError("could not locate the G4MT clip-name table")


@dataclass
class Clip:
    index: int
    name: str
    crc32b: str
    start_frame: int
    end_frame: int
    frame_count: int
    flags: int
    fps: int
    target_info_start: int
    target_info_count: int


@dataclass
class Target:
    index: int
    crc32b: str
    name: str | None


@dataclass
class TargetInfo:
    index: int
    target_index: int
    channel_start: int
    channel_count: int
    reserved: int


@dataclass
class Channel:
    index: int
    key_count: int
    channel_type: int
    encoding: list[int]
    key_start: int
    data_offset: int
    keys: list[int]


def read_g4sk_data(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:4] == b"G4SK":
        return data
    if data[:4] != b"G4PK" or len(data) < 0x40:
        return b""
    header_size = u16(data, 0x04)
    file_count = u32(data, 0x20)
    for index in range(file_count):
        entry_offset = header_size + u32(data, header_size + index * 4) * 4
        entry_size = u32(data, header_size + file_count * 4 + index * 4)
        if data[entry_offset:entry_offset + 4] == b"G4SK":
            return data[entry_offset:entry_offset + entry_size]
    return b""


def g4sk_name_table_offset(data: bytes, count: int) -> int:
    for header_offset in (0x32, 0x30):
        base = 0x40 + u16(data, header_offset) * 4
        if base + count * 2 > len(data):
            continue
        valid = True
        for index in range(count):
            offset = u16(data, base + index * 2)
            absolute = base + offset
            name = cstr(data, absolute) if absolute < len(data) else ""
            if offset < count * 2 or not name or any(ord(char) < 0x20 or ord(char) > 0x7E for char in name):
                valid = False
                break
        if valid:
            return base
    raise ValueError("could not locate the G4SK joint-name table")


def parse_g4sk_names(path: Path | None) -> dict[int, str]:
    if path is None or not path.is_file():
        return {}
    data = read_g4sk_data(path)
    if data[:4] != b"G4SK" or len(data) < 0x40:
        return {}
    joint_count = u16(data, 0x20)
    try:
        name_table = g4sk_name_table_offset(data, joint_count)
    except ValueError:
        return {}
    result: dict[int, str] = {}
    for index in range(joint_count):
        entry = name_table + index * 2
        if entry + 2 > len(data):
            break
        name = cstr(data, name_table + u16(data, entry))
        if name:
            result[crc32b(name)] = name
    return result


MODEL_ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}\d{6,8})(?![A-Za-z0-9])")


def raw_data_root(path: Path) -> Path | None:
    for parent in path.parents:
        if parent.name == "data" and parent.parent.name in {"raw", "readable"}:
            return parent
    return None


def candidate_data_roots(path: Path) -> list[Path]:
    inferred = raw_data_root(path)
    if inferred is None:
        return []
    work_root = inferred.parent.parent
    roots = [inferred, work_root / "raw" / "data", work_root / "readable" / "data"]
    return list(dict.fromkeys(root.resolve() for root in roots if root.is_dir()))


def skeleton_match_score(path: Path, target_hashes: set[int]) -> tuple[int, int, int]:
    names = parse_g4sk_names(path)
    overlap = len(target_hashes.intersection(names))
    canonical = int(path.parent.name.lower() == path.stem.lower())
    test_penalty = -int(any(part.lower() in {"_test", "test"} for part in path.parts))
    return overlap, canonical, test_penalty


def companion_g4sk(path: Path, target_hashes: set[int] | None = None) -> Path | None:
    stem = path.stem.split("_p", 1)[0]
    candidate = path.with_name(f"{stem}.g4sk")
    if candidate.is_file():
        return candidate

    data_roots = candidate_data_roots(path)
    if not data_roots:
        return None
    identifiers = list(dict.fromkeys(match.lower() for match in MODEL_ID_RE.findall(path.stem)))
    candidates: list[Path] = []
    for data_root in data_roots:
        character_root = data_root / "common" / "chr"
        for identifier in identifiers:
            for suffix in (".g4sk", ".g4pkm"):
                direct = character_root / identifier / f"{identifier}{suffix}"
                if direct.is_file():
                    candidates.append(direct)
            if character_root.is_dir():
                for suffix in (".g4sk", ".g4pkm"):
                    candidates.extend(character_root.rglob(f"{identifier}{suffix}"))
    candidates = list(dict.fromkeys(candidate.resolve() for candidate in candidates))
    if not candidates:
        return None
    hashes = target_hashes or set()
    return max(candidates, key=lambda value: skeleton_match_score(value, hashes))


def parse_g4mt(path: Path, skeleton: Path | None = None) -> dict:
    data = path.read_bytes()
    magic = data[:4]
    if len(data) < 0x40 or magic not in {b"G4MT", b"G4MA", b"G4TP"}:
        raise ValueError(f"{path} is not a G4MT, G4MA or G4TP file")

    header_size = u16(data, 0x04)
    header_words = u16(data, 0x0A)
    if header_size != header_words * 4:
        raise ValueError("inconsistent G4MT header size")
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

    clip_order = list(struct.unpack_from(f"<{clip_count}H", data, name_meta_offset))
    name_offset_table, name_offsets, clip_names = find_name_table(data, name_meta_offset, clip_count)

    target_info_count = max((row[2] + (row[6] << 16) + row[3] for row in clip_rows), default=0)
    target_infos = []
    for index in range(target_info_count):
        offset = target_info_offset + index * 8
        target_index, channel_start_low, channel_count, channel_start_high, reserved = struct.unpack_from("<HHBBH", data, offset)
        if target_index >= target_count:
            raise ValueError(f"target-info {index} refers to missing target {target_index}")
        target_infos.append(
            TargetInfo(index, target_index, channel_start_low + (channel_start_high << 16), channel_count, reserved)
        )

    channel_count = max((row.channel_start + row.channel_count for row in target_infos), default=0)

    channel_headers = []
    key_count = 0
    for index in range(channel_count):
        offset = channel_offset + index * 20
        encoding, key_start, entry_data_offset, entry_key_count = struct.unpack_from("<8sIII", data, offset)
        key_count = max(key_count, key_start + entry_key_count)
        channel_headers.append((entry_key_count, list(encoding), key_start, entry_data_offset))
    raw_data_size = len(data) - data_offset
    data_size = raw_data_size
    while data_size and data[data_offset + data_size - 1] == 0:
        data_size -= 1

    all_keys = list(struct.unpack_from(f"<{key_count}H", data, key_offset)) if key_count else []
    channels = [
        Channel(
            index=index,
            key_count=entry[0],
            channel_type=entry[1][0],
            encoding=entry[1],
            key_start=entry[2],
            data_offset=entry[3],
            keys=all_keys[entry[2]:entry[2] + entry[0]],
        )
        for index, entry in enumerate(channel_headers)
    ]

    skeleton_path = skeleton or companion_g4sk(path, set(target_hashes))
    skeleton_names = parse_g4sk_names(skeleton_path)
    targets = [
        Target(index, f"{value:08x}", skeleton_names.get(value))
        for index, value in enumerate(target_hashes)
    ]
    clips = [
        Clip(
            index=index,
            name=clip_names[index],
            crc32b=f"{clip_hashes[index]:08x}",
            start_frame=row[0],
            end_frame=row[1],
            frame_count=row[1] - row[0] + 1,
            flags=row[4],
            fps=row[5],
            target_info_start=row[2] + (row[6] << 16),
            target_info_count=row[3],
        )
        for index, row in enumerate(clip_rows)
    ]

    return {
        "source": str(path),
        "skeleton": str(skeleton_path) if skeleton_path else None,
        "magic": magic.decode("ascii"),
        "header_size": header_size,
        "offset_shift": offset_shift,
        "file_type": u16(data, 0x06),
        "version": u32(data, 0x08),
        "content_size": u32(data, 0x0C),
        "file_size": len(data),
        "clip_count": clip_count,
        "target_count": target_count,
        "target_info_count": target_info_count,
        "channel_count": channel_count,
        "key_count": key_count,
        "data_size_without_trailing_zeroes": data_size,
        "data_region_size": raw_data_size,
        "section_units_raw": section_units,
        "target_info_units_raw": target_info_units,
        "channel_units_raw": channel_units,
        "section_offsets": {
            "clips": header_size,
            "scales": scale_offset,
            "clip_hashes": clip_hash_offset,
            "target_hashes": target_hash_offset,
            "clip_names": name_meta_offset,
            "target_infos": target_info_offset,
            "channels": channel_offset,
            "keys": key_offset,
            "data": data_offset,
        },
        "scales": scales,
        "clip_order": clip_order,
        "clips": [asdict(value) for value in clips],
        "targets": [asdict(value) for value in targets],
        "target_infos": [asdict(value) for value in target_infos],
        "channels": [asdict(value) for value in channels],
        "notes": {
            "channel_values": (
                "Use g4mt_motion.py to decode and export transform curves."
                if magic == b"G4MT"
                else (
                    "G4MA uses the same curve container for material parameters; its channel types "
                    "must be decoded as material values, not transforms."
                    if magic == b"G4MA"
                    else "G4TP uses the shared curve container, but its target semantics remain "
                    "unresolved and must not be treated as skeletal transforms by default."
                )
            ),
            "clip_fps": "Confirmed by the runtime sampler in nie_602.exe.",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("g4mt", type=Path, help="G4MT, G4MA or G4TP curve container")
    parser.add_argument("--skeleton", type=Path, help="Optional companion G4SK used to resolve target hashes")
    parser.add_argument("--output", type=Path, help="Write JSON to this path instead of stdout")
    parser.add_argument("--compact", action="store_true", help="Omit target-info and channel arrays")
    args = parser.parse_args()

    result = parse_g4mt(args.g4mt, args.skeleton)
    if args.compact:
        result.pop("target_infos", None)
        result.pop("channels", None)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
