"""Read the confirmed G4MT animation-bank directory without decoding motion yet."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import struct
from pathlib import Path
from typing import Union
import zlib

from .diagnostics import Diagnostic, NativeFormatError

__all__ = [
    "G4MTHeader",
    "G4MTClip",
    "G4MTTarget",
    "G4MTTargetInfo",
    "G4MTChannel",
    "G4MTAnimationBank",
    "crc32b",
    "parse_g4mt",
    "parse_g4mt_file",
    "parse_g4ma",
    "parse_g4ma_file",
    "parse_g4cm",
    "parse_g4cm_file",
]


BinaryData = Union[bytes, bytearray, memoryview]


@dataclass(frozen=True)
class G4MTHeader:
    header_size: int
    file_type: int
    version: int
    content_size: int
    clip_count: int
    target_count: int
    target_info_offset: int
    channel_offset: int
    scale_offset: int
    clip_hash_offset: int
    target_hash_offset: int
    name_offset: int
    key_offset: int
    data_offset: int
    offset_shift: int


@dataclass(frozen=True)
class G4MTClip:
    index: int
    name: str
    name_hash: int
    start_frame: int
    end_frame: int
    flags: int
    fps: int
    target_info_start: int
    target_info_count: int


@dataclass(frozen=True)
class G4MTTarget:
    index: int
    name_hash: int


@dataclass(frozen=True)
class G4MTTargetInfo:
    index: int
    target_index: int
    channel_start: int
    channel_count: int
    reserved: int


@dataclass(frozen=True)
class G4MTChannel:
    index: int
    channel_type: int
    codec: int
    interpolate: int
    variant: int
    component_count: int
    bytes_per_key: int
    scale_index: int
    key_start: int
    data_offset: int
    key_count: int
    keys: tuple[int, ...]


@dataclass(frozen=True)
class G4MTAnimationBank:
    header: G4MTHeader
    scales: tuple[float, ...]
    clips: tuple[G4MTClip, ...]
    targets: tuple[G4MTTarget, ...]
    target_infos: tuple[G4MTTargetInfo, ...]
    channels: tuple[G4MTChannel, ...]


def _fail(source: str, code: str, message: str, offset: int | None = None) -> None:
    raise NativeFormatError(Diagnostic(code=code, message=message, source=source, offset=offset))


def crc32b(name: str) -> int:
    """Return the native CRC32B of a name encoded as UTF-8."""

    return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF


def _require(data: memoryview, offset: int, size: int, source: str, label: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        _fail(source, "G4MT_RANGE", f"{label} exceeds the animation resource", offset)


def _u16(data: memoryview, offset: int, source: str, label: str) -> int:
    _require(data, offset, 2, source, label)
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: memoryview, offset: int, source: str, label: str) -> int:
    _require(data, offset, 4, source, label)
    return struct.unpack_from("<I", data, offset)[0]


def _section_offset(header_words: int, units: int) -> int:
    return (header_words + units) * 4


def _scaled_offset(header_words: int, units: int, shift: int, quadratic: bool = False) -> int:
    return (header_words + (units << (shift * 2 if quadratic else shift))) * 4


def _read_name(data: memoryview, offset: int, source: str) -> str:
    _require(data, offset, 1, source, "G4MT clip name")
    end = data[offset:].tobytes().find(b"\0")
    if end < 0:
        _fail(source, "G4MT_NAME_TERMINATOR", "a clip name is not null terminated", offset)
    raw = data[offset : offset + end].tobytes()
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        _fail(source, "G4MT_NAME_ENCODING", f"a clip name is not valid UTF-8: {error}", offset)
    if not name or any(ord(char) < 0x20 for char in name):
        _fail(source, "G4MT_NAME_VALUE", "a clip name is empty or contains a control character", offset)
    return name


def _read_clip_names(data: memoryview, name_offset: int, count: int, source: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    order_size = count * 2
    _require(data, name_offset, order_size, source, "G4MT clip order")
    order = struct.unpack_from(f"<{count}H", data, name_offset) if count else ()
    if tuple(sorted(order)) != tuple(range(count)):
        _fail(source, "G4MT_CLIP_ORDER", "the clip order table is not a permutation", name_offset)
    search_end = min(len(data), name_offset + max(0x1000, count * 8))
    for table_offset in range((name_offset + order_size + 3) & ~3, search_end, 2):
        if table_offset + count * 2 > len(data):
            break
        offsets = struct.unpack_from(f"<{count}H", data, table_offset) if count else ()
        if any(relative < count * 2 or table_offset + relative >= len(data) for relative in offsets):
            continue
        try:
            names = tuple(_read_name(data, table_offset + relative, source) for relative in offsets)
        except NativeFormatError:
            continue
        return tuple(order), names
    _fail(source, "G4MT_NAME_TABLE", "could not locate a valid clip-name table", name_offset)
    raise AssertionError("unreachable")


def parse_g4mt(data: BinaryData, source: str = "<memory>") -> G4MTAnimationBank:
    """Read the G4MT directory, tables and key references with bounds checks."""

    view = memoryview(data)
    _require(view, 0, 0x40, source, "G4MT header")
    if view[:4].tobytes() != b"G4MT":
        _fail(source, "G4MT_MAGIC", "expected the native G4MT magic", 0)
    header_size = _u16(view, 0x04, source, "G4MT header size")
    header_words = _u16(view, 0x0A, source, "G4MT header words")
    if header_size != 0x40 or header_size != header_words * 4:
        _fail(source, "G4MT_HEADER_SIZE", "the G4MT header is not the confirmed 0x40-byte layout", 0x04)
    clip_count = _u16(view, 0x20, source, "G4MT clip count")
    target_count = _u16(view, 0x22, source, "G4MT target count")
    offset_shift = view[0x36]
    if offset_shift > 4:
        _fail(source, "G4MT_OFFSET_SHIFT", "the G4MT offset shift is outside the supported range", 0x36)

    section_units = tuple(_u16(view, 0x28 + index * 2, source, f"G4MT section {index}") for index in range(6))
    scale_offset, clip_hash_offset, target_hash_offset, name_offset = (
        _section_offset(header_words, units) for units in section_units[:4]
    )
    target_info_offset = _scaled_offset(header_words, _u16(view, 0x24, source, "G4MT target-info offset"), offset_shift)
    channel_offset = _scaled_offset(header_words, _u16(view, 0x26, source, "G4MT channel offset"), offset_shift)
    key_offset = _scaled_offset(header_words, section_units[4], offset_shift, quadratic=True)
    data_offset = _scaled_offset(header_words, section_units[5], offset_shift, quadratic=True)
    offsets = (scale_offset, clip_hash_offset, target_hash_offset, name_offset, target_info_offset, channel_offset, key_offset, data_offset)
    if any(offset > len(view) for offset in offsets):
        _fail(source, "G4MT_SECTION_RANGE", "a G4MT section starts outside the resource")

    record_end = header_size + clip_count * 0x10
    _require(view, header_size, clip_count * 0x10, source, "G4MT clip table")
    if scale_offset < record_end or clip_hash_offset < scale_offset or target_hash_offset < clip_hash_offset:
        _fail(source, "G4MT_SECTION_ORDER", "the G4MT fixed sections are not monotonically ordered")
    scale_count = (clip_hash_offset - scale_offset) // 4
    if (clip_hash_offset - scale_offset) % 4:
        _fail(source, "G4MT_SCALE_TABLE", "the G4MT scale table is not float aligned", scale_offset)
    _require(view, scale_offset, scale_count * 4, source, "G4MT scales")
    scales = struct.unpack_from(f"<{scale_count}f", view, scale_offset) if scale_count else ()
    if not all(math.isfinite(value) for value in scales):
        _fail(source, "G4MT_SCALE_VALUE", "the G4MT scale table contains a non-finite value", scale_offset)

    _require(view, clip_hash_offset, clip_count * 4, source, "G4MT clip hashes")
    _require(view, target_hash_offset, target_count * 4, source, "G4MT target hashes")
    clip_hashes = struct.unpack_from(f"<{clip_count}I", view, clip_hash_offset) if clip_count else ()
    target_hashes = struct.unpack_from(f"<{target_count}I", view, target_hash_offset) if target_count else ()
    clip_order, clip_names = _read_clip_names(view, name_offset, clip_count, source)

    rows = [struct.unpack_from("<HHHHBBBBI", view, header_size + index * 0x10) for index in range(clip_count)]
    target_info_count = max((row[2] + (row[6] << 16) + row[3] for row in rows), default=0)
    _require(view, target_info_offset, target_info_count * 8, source, "G4MT target-info table")
    target_infos = []
    for index in range(target_info_count):
        offset = target_info_offset + index * 8
        target_index, channel_low, channel_count, channel_high, reserved = struct.unpack_from("<HHBBH", view, offset)
        if target_index >= target_count:
            _fail(source, "G4MT_TARGET_INDEX", f"target-info {index} refers to target {target_index}", offset)
        target_infos.append(G4MTTargetInfo(index, target_index, channel_low + (channel_high << 16), channel_count, reserved))
    channel_count = max((item.channel_start + item.channel_count for item in target_infos), default=0)
    _require(view, channel_offset, channel_count * 20, source, "G4MT channel table")
    channels = []
    max_key = 0
    for index in range(channel_count):
        offset = channel_offset + index * 20
        encoding = struct.unpack_from("<8B", view, offset)
        key_start, value_offset, key_count = struct.unpack_from("<III", view, offset + 8)
        if encoding[4] == 0 or encoding[5] == 0:
            _fail(source, "G4MT_CHANNEL_ENCODING", f"channel {index} has no components or key width", offset)
        if encoding[6] >= scale_count:
            _fail(source, "G4MT_CHANNEL_SCALE", f"channel {index} refers to scale {encoding[6]}", offset)
        max_key = max(max_key, key_start + key_count)
        channels.append(G4MTChannel(index, encoding[0], encoding[1], encoding[2], encoding[3], encoding[4], encoding[5], encoding[6], key_start, value_offset, key_count, ()))
    _require(view, key_offset, max_key * 2, source, "G4MT key table")
    all_keys = struct.unpack_from(f"<{max_key}H", view, key_offset) if max_key else ()
    for index, channel in enumerate(channels):
        keys = tuple(all_keys[channel.key_start : channel.key_start + channel.key_count])
        if any(left > right for left, right in zip(keys, keys[1:])):
            _fail(source, "G4MT_KEY_ORDER", f"channel {index} key frames are not ordered", channel_offset + index * 20)
        if channel.key_count and channel.data_offset + channel.key_count * channel.bytes_per_key > len(view) - data_offset:
            _fail(source, "G4MT_DATA_RANGE", f"channel {index} data exceeds the G4MT data blob", data_offset + channel.data_offset)
        channels[index] = replace(channel, keys=keys)

    clips = tuple(
        G4MTClip(index, clip_names[index], clip_hashes[index], row[0], row[1], row[4], row[5], row[2] + (row[6] << 16), row[3])
        for index, row in enumerate(rows)
    )
    return G4MTAnimationBank(
        header=G4MTHeader(header_size, _u16(view, 0x06, source, "G4MT file type"), _u32(view, 0x08, source, "G4MT version"), _u32(view, 0x0C, source, "G4MT content size"), clip_count, target_count, target_info_offset, channel_offset, scale_offset, clip_hash_offset, target_hash_offset, name_offset, key_offset, data_offset, offset_shift),
        scales=tuple(scales),
        clips=clips,
        targets=tuple(G4MTTarget(index, value) for index, value in enumerate(target_hashes)),
        target_infos=tuple(target_infos),
        channels=tuple(channels),
    )


def parse_g4mt_file(path: Path | str) -> G4MTAnimationBank:
    path = Path(path)
    try:
        return parse_g4mt(path.read_bytes(), str(path))
    except OSError as error:
        _fail(str(path), "G4MT_READ", f"could not read the animation resource: {error}")
    raise AssertionError("unreachable")


def _parse_animation_family(
    data: BinaryData,
    expected_magic: bytes,
    source: str,
) -> G4MTAnimationBank:
    """Parse a G4MT-compatible directory while preserving its native magic."""

    view = memoryview(data)
    label = expected_magic.decode("ascii")
    _require(view, 0, 4, source, f"{label} magic")
    if view[:4].tobytes() != expected_magic:
        _fail(source, f"{label}_MAGIC", f"expected the native {label} magic", 0)

    # The directory/channel layout is shared by G4MT, G4MA and G4CM. The
    # established parser remains the single implementation of its bounds and
    # offset rules; only the FourCC differs at the resource boundary.
    normalized = bytearray(view)
    normalized[:4] = b"G4MT"
    return parse_g4mt(normalized, source)


def parse_g4ma(data: BinaryData, source: str = "<memory>") -> G4MTAnimationBank:
    """Read a G4MA material-animation bank using the shared native layout."""

    return _parse_animation_family(data, b"G4MA", source)


def parse_g4ma_file(path: Path | str) -> G4MTAnimationBank:
    """Read a G4MA material-animation bank from disk."""

    file_path = Path(path)
    try:
        return parse_g4ma(file_path.read_bytes(), str(file_path))
    except OSError as error:
        _fail(str(file_path), "G4MA_READ", f"could not read the material animation resource: {error}")
    raise AssertionError("unreachable")


def parse_g4cm(data: BinaryData, source: str = "<memory>") -> G4MTAnimationBank:
    """Read a G4CM camera-animation bank using the shared native layout."""

    return _parse_animation_family(data, b"G4CM", source)


def parse_g4cm_file(path: Path | str) -> G4MTAnimationBank:
    """Read a G4CM camera-animation bank from disk."""

    file_path = Path(path)
    try:
        return parse_g4cm(file_path.read_bytes(), str(file_path))
    except OSError as error:
        _fail(str(file_path), "G4CM_READ", f"could not read the camera animation resource: {error}")
    raise AssertionError("unreachable")
