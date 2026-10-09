"""Write little-endian Victory Road G4SK v104 skeletons."""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
import zlib
from typing import Sequence


HEADER_SIZE = 0x40
MAX_FLOAT32 = 3.4028234663852886e38


@dataclass(frozen=True)
class G4SKNode:
    name: str
    parent_index: int | None
    local_srt: tuple[float, ...]
    global_matrix: tuple[float, ...]
    inverse_bind_matrix: tuple[float, ...]
    flags: int
    bounds_radius: float


def write_g4sk_v104(template: bytes, nodes: Sequence[G4SKNode]) -> bytes:
    if len(template) < HEADER_SIZE or template[:4] != b"G4SK":
        raise ValueError("template is not a G4SK resource")
    if struct.unpack_from("<HHB", template, 4) != (HEADER_SIZE, 104, 0):
        raise ValueError("template must be little-endian G4SK v104")
    if struct.unpack_from("<H", template, 0x0A)[0] * 4 != HEADER_SIZE:
        raise ValueError("template has an unsupported global-matrix base")
    if template[0x23] != 0 or template[0x3A:0x3E] != bytes(4):
        raise ValueError("template uses unsupported optional G4SK tables")
    if not 0 < len(nodes) < 0xFFFF:
        raise ValueError("G4SK requires between 1 and 65534 nodes")

    names: set[str] = set()
    hashes = []
    for index, node in enumerate(nodes):
        try:
            encoded_name = node.name.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError(f"joint name is not ASCII: {node.name!r}") from error
        if not encoded_name or b"\0" in encoded_name:
            raise ValueError(f"joint name is empty or contains NUL: {node.name!r}")
        if node.name.casefold() in names:
            raise ValueError(f"duplicate joint name: {node.name!r}")
        names.add(node.name.casefold())
        if node.parent_index is not None and not 0 <= node.parent_index < index:
            raise ValueError(f"joint {index} parent must precede the joint")
        if not 0 <= node.flags <= 0xFF:
            raise ValueError(f"joint {index} flags exceed one byte")
        if not math.isfinite(node.bounds_radius) or not 0 <= node.bounds_radius <= MAX_FLOAT32:
            raise ValueError(f"joint {index} bounds radius must be finite and nonnegative")
        for label, matrix in (
            ("local SRT", node.local_srt),
            ("global matrix", node.global_matrix),
            ("inverse-bind matrix", node.inverse_bind_matrix),
        ):
            if len(matrix) != 12 or not all(
                math.isfinite(value) and abs(value) <= MAX_FLOAT32 for value in matrix
            ):
                raise ValueError(f"joint {index} has an invalid {label}")
        hashes.append(zlib.crc32(encoded_name) & 0xFFFFFFFF)

    output = bytearray(template[:HEADER_SIZE])
    output.extend(b"".join(struct.pack("<12f", *node.global_matrix) for node in nodes))
    offsets: dict[int, int] = {}
    _append_section(
        output,
        offsets,
        0x24,
        b"".join(struct.pack("<12f", *node.inverse_bind_matrix) for node in nodes),
    )
    _append_section(
        output,
        offsets,
        0x26,
        b"".join(struct.pack("<12f", *node.local_srt) for node in nodes),
    )
    _append_section(output, offsets, 0x28, struct.pack(f"<{len(nodes)}I", *hashes))
    parents = [node.parent_index if node.parent_index is not None else len(nodes) for node in nodes]
    _append_section(output, offsets, 0x2A, struct.pack(f"<{len(nodes)}H", *parents))
    order = sorted(range(len(nodes)), key=lambda index: (hashes[index], index))
    _append_section(output, offsets, 0x2C, struct.pack(f"<{len(nodes)}H", *order))
    _append_section(output, offsets, 0x2E, bytes(node.flags for node in nodes))
    _append_section(
        output,
        offsets,
        0x30,
        struct.pack(f"<{len(nodes)}f", *(node.bounds_radius for node in nodes)),
    )

    name_offsets = bytearray()
    name_bytes = bytearray()
    for node in nodes:
        relative = len(nodes) * 2 + len(name_bytes)
        if relative > 0xFFFF:
            raise ValueError("G4SK name offsets exceed U16")
        name_offsets.extend(struct.pack("<H", relative))
        name_bytes.extend(node.name.encode("ascii"))
        name_bytes.append(0)
    _append_section(output, offsets, 0x32, bytes(name_offsets + name_bytes))
    _pad(output, 16)

    struct.pack_into("<H", output, 0x20, len(nodes))
    for field, offset in offsets.items():
        struct.pack_into("<H", output, field, offset)
    for field in (0x34, 0x36, 0x38):
        struct.pack_into("<H", output, field, offsets[0x28])
    struct.pack_into("<I", output, 0x0C, len(output) - HEADER_SIZE)
    return bytes(output)


def _append_section(output: bytearray, offsets: dict[int, int], field: int, data: bytes) -> None:
    _pad(output, 16)
    relative = len(output) - HEADER_SIZE
    units = relative // 4
    if units > 0xFFFF:
        raise ValueError(f"G4SK section at 0x{field:02X} exceeds U16 offset range")
    offsets[field] = units
    output.extend(data)


def _pad(output: bytearray, alignment: int) -> None:
    output.extend(bytes((-len(output)) % alignment))
