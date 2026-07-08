"""Reader for Level-5 P3 lip-sync resources."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LipKey:
    time: float
    packed_viseme: int


@dataclass(frozen=True)
class LipSequence:
    duration: float
    keys: tuple[LipKey, ...]


def read_p3lip(path: Path) -> LipSequence:
    data = path.read_bytes()
    if len(data) < 0x28 or data[:4] != b"lip\0":
        raise ValueError(f"{path} is not a P3 lip-sync resource")
    declared_size = struct.unpack_from("<I", data, 8)[0]
    duration = struct.unpack_from("<f", data, 0x14)[0]
    key_offset = struct.unpack_from("<I", data, 0x20)[0]
    key_count, stride = struct.unpack_from("<HH", data, 0x24)
    if declared_size > len(data) or stride < 8 or key_offset + key_count * stride > len(data):
        raise ValueError(f"truncated P3 lip-sync resource: {path}")
    keys = tuple(
        LipKey(
            time=struct.unpack_from("<f", data, key_offset + index * stride)[0],
            packed_viseme=struct.unpack_from("<I", data, key_offset + index * stride + 4)[0],
        )
        for index in range(key_count)
    )
    return LipSequence(duration=duration, keys=keys)
