#!/usr/bin/env python3
"""Extract G4MT entries from a Level-5 G4PK/G4PKM container."""

from __future__ import annotations

import argparse
import struct
import zlib
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


def replace_entries(
    data: bytes,
    replacements: dict[int, bytes],
    entry_names: dict[int, str] | None = None,
) -> bytes:
    """Repack selected entry payloads while retaining the G4PK directory and other data."""
    parsed_entries = entries(data)
    if any(index < 0 or index >= len(parsed_entries) for index in replacements):
        raise ValueError("G4PK replacement refers to a missing entry")
    if entry_names and any(index < 0 or index >= len(parsed_entries) for index in entry_names):
        raise ValueError("G4PK rename refers to a missing entry")
    if entry_names:
        return _replace_entries_and_names(data, parsed_entries, replacements, entry_names)

    header_size = struct.unpack_from("<H", data, 0x04)[0]
    file_count = struct.unpack_from("<I", data, 0x20)[0]
    sizes_offset = header_size + file_count * 4
    ordered = sorted(
        (offset, offset + size, index, name)
        for index, (name, offset, size) in enumerate(parsed_entries)
    )
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        raise ValueError("G4PK entries overlap and cannot be safely repacked")

    first_payload = ordered[0][0] if ordered else len(data)
    prefix = bytearray(data[:first_payload])
    output = bytearray(prefix)
    new_offsets = [0] * file_count
    new_sizes = [0] * file_count
    source_cursor = first_payload

    for source_start, source_end, index, _name in ordered:
        output.extend(data[source_cursor:source_start])
        while (len(output) - header_size) % 4:
            output.append(0)
        payload = replacements.get(index, data[source_start:source_end])
        new_offsets[index] = (len(output) - header_size) // 4
        new_sizes[index] = len(payload)
        output.extend(payload)
        source_cursor = source_end

    output.extend(data[source_cursor:])
    for index, (offset, size) in enumerate(zip(new_offsets, new_sizes)):
        struct.pack_into("<I", output, header_size + index * 4, offset)
        struct.pack_into("<I", output, sizes_offset + index * 4, size)
    struct.pack_into("<I", output, 0x0C, len(output) - header_size)

    result = bytes(output)
    rebuilt_entries = entries(result)
    for index, (name, offset, size) in enumerate(rebuilt_entries):
        original_name, original_offset, original_size = parsed_entries[index]
        if name != original_name:
            raise ValueError("G4PK name table changed while repacking")
        expected = replacements.get(index, data[original_offset:original_offset + original_size])
        if size != len(expected) or result[offset:offset + size] != expected:
            raise ValueError(f"G4PK entry {name} failed read-back validation")
    return result


def _replace_entries_and_names(
    data: bytes,
    parsed_entries: list[tuple[str, int, int]],
    replacements: dict[int, bytes],
    entry_names: dict[int, str],
) -> bytes:
    header_size = struct.unpack_from("<H", data, 0x04)[0]
    file_count = struct.unpack_from("<I", data, 0x20)[0]
    hash_count, table3_count = struct.unpack_from("<HH", data, 0x24)
    id_count = table3_count // 2
    if hash_count < file_count or id_count < file_count:
        raise ValueError("G4PK directory tables are shorter than the file table")

    position = header_size + file_count * 8
    hashes = list(struct.unpack_from(f"<{hash_count}I", data, position))
    position += hash_count * 4
    ids = data[position:position + id_count * 2]
    position = align(position + id_count * 2, 4)
    string_base = position
    offsets = struct.unpack_from(f"<{id_count}h", data, string_base)
    names = [cstr(data, string_base + offset) for offset in offsets]
    old_string_end = max(
        (string_base + offset + len(name.encode("utf-8")) + 1 for offset, name in zip(offsets, names)),
        default=string_base + id_count * 2,
    )

    for index, new_name in entry_names.items():
        if not new_name or "\0" in new_name:
            raise ValueError(f"G4PK entry {index} name is empty or contains NUL")
        if hashes[index] != zlib.crc32(names[index].encode("utf-8")) & 0xFFFFFFFF:
            raise ValueError(f"G4PK entry {index} name hash is not the expected CRC32")
        names[index] = new_name
        hashes[index] = zlib.crc32(new_name.encode("utf-8")) & 0xFFFFFFFF

    string_offsets = []
    string_pool = bytearray()
    unique_offsets = {}
    for name in names:
        offset = unique_offsets.get(name)
        if offset is None:
            offset = id_count * 2 + len(string_pool)
            if offset > 0x7FFF:
                raise ValueError("G4PK string offsets exceed the signed 16-bit limit")
            unique_offsets[name] = offset
            string_pool.extend(name.encode("utf-8"))
            string_pool.append(0)
        string_offsets.append(offset)

    first_payload = min((offset for _name, offset, _size in parsed_entries), default=len(data))
    ordered = sorted(
        (offset, offset + size, index)
        for index, (_name, offset, size) in enumerate(parsed_entries)
    )
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        raise ValueError("G4PK entries overlap and cannot be safely repacked")
    if old_string_end > first_payload:
        raise ValueError("G4PK string table overlaps its first payload")

    output = bytearray(data[:header_size])
    offsets_position = len(output)
    output.extend(bytes(file_count * 8))
    hashes_position = len(output)
    output.extend(struct.pack(f"<{hash_count}I", *hashes))
    output.extend(ids)
    while len(output) % 4:
        output.append(0)
    output.extend(struct.pack(f"<{id_count}h", *string_offsets))
    output.extend(string_pool)
    output.extend(data[old_string_end:first_payload])
    while (len(output) - header_size) % 4:
        output.append(0)

    new_offsets = [0] * file_count
    new_sizes = [0] * file_count
    source_cursor = first_payload
    for source_start, source_end, index in ordered:
        output.extend(data[source_cursor:source_start])
        while (len(output) - header_size) % 4:
            output.append(0)
        payload = replacements.get(index, data[source_start:source_end])
        new_offsets[index] = (len(output) - header_size) // 4
        new_sizes[index] = len(payload)
        output.extend(payload)
        source_cursor = source_end
    output.extend(data[source_cursor:])

    struct.pack_into(f"<{file_count}I", output, offsets_position, *new_offsets)
    struct.pack_into(f"<{file_count}I", output, offsets_position + file_count * 4, *new_sizes)
    struct.pack_into("<I", output, 0x0C, len(output) - header_size)

    result = bytes(output)
    rebuilt_entries = entries(result)
    for index, (name, offset, size) in enumerate(rebuilt_entries):
        expected_name = entry_names.get(index, parsed_entries[index][0])
        expected_payload = replacements.get(
            index,
            data[parsed_entries[index][1]:parsed_entries[index][1] + parsed_entries[index][2]],
        )
        if name != expected_name or size != len(expected_payload) or result[offset:offset + size] != expected_payload:
            raise ValueError(f"G4PK entry {index} failed read-back validation")
    for index, (name, _offset, _size) in enumerate(rebuilt_entries):
        stored_hash = struct.unpack_from("<I", result, hashes_position + index * 4)[0]
        if stored_hash != zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF:
            raise ValueError(f"G4PK entry {name} has an invalid name hash after repacking")
    return result


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


def pack_g4mt(name: str, motion: bytes) -> bytes:
    """Build a single-motion G4PK with its native name directory."""
    if not name or '\0' in name or '/' in name or '\\' in name or not name.endswith('.g4mt'):
        raise ValueError('G4PK motion requires a .g4mt entry filename')
    encoded = name.encode('utf-8')
    if len(encoded) > 32764 or motion[:4] != b'G4MT':
        raise ValueError('Invalid G4PK entry name or G4MT payload')
    output = bytearray(0x50)
    struct.pack_into('<4sHH', output, 0, b'G4PK', 0x40, 100)
    struct.pack_into('<H', output, 0x0A, 0x10)
    struct.pack_into('<I4H', output, 0x20, 1, 1, 2, 3, 4)
    struct.pack_into('<II', output, 0x44, len(motion), zlib.crc32(encoded) & 0xffffffff)
    output.extend(struct.pack('<h', 2) + encoded + b'\0')
    output.extend(bytes(-len(output) % 16))
    struct.pack_into('<I', output, 0x40, (len(output) - 0x40) // 4)
    output.extend(motion)
    struct.pack_into('<I', output, 0x0C, len(output) - 0x40)
    result = bytes(output)
    if select_g4mt_entry(result, '0') != (name, motion):
        raise ValueError('G4PK motion failed read-back validation')
    return result


if __name__ == "__main__":
    main()
