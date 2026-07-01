#!/usr/bin/env python3
"""Extract G4MT entries from a Level-5 G4PK/G4PKM container."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path


def align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def cstr(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        raise ValueError("unterminated G4PK entry name")
    return data[offset:end].decode("utf-8", errors="replace")


def entries(data: bytes) -> list[tuple[str, int, int]]:
    if len(data) < 0x40 or data[:4] != b"G4PK":
        raise ValueError("not a G4PK/G4PKM file")
    header_size = struct.unpack_from("<H", data, 0x04)[0]
    file_count = struct.unpack_from("<I", data, 0x20)[0]
    hash_count, table3_count = struct.unpack_from("<HH", data, 0x24)
    if table3_count // 2 < file_count:
        raise ValueError("G4PK name table is shorter than its file table")

    position = header_size
    offsets = struct.unpack_from(f"<{file_count}I", data, position)
    position += file_count * 4
    sizes = struct.unpack_from(f"<{file_count}I", data, position)
    position += file_count * 4 + hash_count * 4 + (table3_count // 2) * 2
    string_base = align(position, 4)
    string_offsets = struct.unpack_from(f"<{table3_count // 2}h", data, string_base)

    result = []
    for index in range(file_count):
        name = cstr(data, string_base + string_offsets[index])
        offset = header_size + offsets[index] * 4
        size = sizes[index]
        if offset < header_size or offset + size > len(data):
            raise ValueError(f"G4PK entry {index} is outside the container")
        result.append((name, offset, size))
    return result


def g4mt_entries(data: bytes) -> list[tuple[str, int, int]]:
    return [
        (name, offset, size)
        for name, offset, size in entries(data)
        if data[offset:offset + 4] == b"G4MT"
    ]


def select_g4mt_entry(data: bytes, selector: str = "0") -> tuple[str, bytes]:
    candidates = g4mt_entries(data)
    if not candidates:
        raise ValueError("G4PK does not contain a G4MT entry")
    selected = None
    if selector.isdigit():
        index = int(selector)
        if index < len(candidates):
            selected = candidates[index]
    if selected is None:
        normalized = selector.replace("\\", "/").lower()
        for candidate in candidates:
            name = candidate[0].replace("\\", "/").lower()
            if name == normalized or Path(name).name == Path(normalized).name or Path(name).stem == normalized:
                selected = candidate
                break
    if selected is None:
        names = ", ".join(name for name, _, _ in candidates[:8])
        raise ValueError(f"G4MT entry not found: {selector}; available: {names}")
    name, offset, size = selected
    return name, data[offset:offset + size]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("g4pk", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--all", action="store_true", help="Extract every entry, not only G4MT payloads")
    parser.add_argument("--entry", help="Extract one G4MT entry by G4MT index, full name, filename or stem")
    args = parser.parse_args()

    data = args.g4pk.read_bytes()
    if args.entry is not None:
        name, payload = select_g4mt_entry(data, args.entry)
        safe_name = Path(name.replace("\\", "/")).name or "entry.g4mt"
        destination = args.output / safe_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        print(f"{safe_name}\t{len(payload)}")
        print("extracted=1")
        return
    extracted = 0
    for name, offset, size in entries(data):
        payload = data[offset:offset + size]
        if not args.all and payload[:4] != b"G4MT":
            continue
        safe_name = Path(name.replace("\\", "/")).name
        if not safe_name:
            safe_name = f"entry_{extracted:04d}.bin"
        destination = args.output / safe_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        print(f"{safe_name}\t{size}")
        extracted += 1
    print(f"extracted={extracted}")


if __name__ == "__main__":
    main()
