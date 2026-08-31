"""Read-only readers for the two observed Level-5 CFGBIN families.

The implementation is intentionally independent from the desktop tool.  It
keeps source offsets and raw values so table consumers can explain a failure
without rebuilding or normalising the source resource.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Union

from .diagnostics import Diagnostic, NativeFormatError

__all__ = [
    "CfgBinDocument",
    "CfgBinEntry",
    "CfgBinFormat",
    "CfgBinValue",
    "CfgBinValueType",
    "RdbnpDocument",
    "RdbnpField",
    "RdbnpList",
    "RdbnpRow",
    "RdbnpTuple",
    "RdbnpType",
    "parse_cfgbin",
    "parse_cfgbin_file",
    "parse_rdbnp",
    "parse_t2b",
]


BinaryData = Union[bytes, bytearray, memoryview]
T2B_FOOTER_MAGIC = 0x62327401


class CfgBinFormat:
    T2B = "T2B"
    RDBNP = "RDBNP"


class CfgBinValueType:
    STRING = 0
    INTEGER = 1
    FLOAT = 2
    UNKNOWN = 3


@dataclass(frozen=True)
class CfgBinValue:
    type_code: int
    value: object
    raw_value: int
    file_offset: int


@dataclass(frozen=True)
class CfgBinEntry:
    index: int
    name_crc: int
    name: Optional[str]
    name_candidates: tuple[str, ...]
    values: tuple[CfgBinValue, ...]


@dataclass(frozen=True)
class CfgBinDocument:
    format: str
    source: bytes
    entries: tuple[CfgBinEntry, ...] = ()
    entry_count: int = 0
    string_data_offset: int = 0
    string_data_length: int = 0
    string_data_count: int = 0
    encoding_code: Optional[int] = None
    value_width: Optional[int] = None

    def write_unmodified(self) -> bytes:
        """Return the exact source bytes without allowing semantic rewriting."""

        return bytes(self.source)


@dataclass(frozen=True)
class RdbnpTuple:
    offset: int
    count: int


@dataclass(frozen=True)
class RdbnpField:
    name: str
    type_code: int
    element_size: int
    offset: int
    count: int


@dataclass(frozen=True)
class RdbnpType:
    name: str
    fields: tuple[RdbnpField, ...]


@dataclass(frozen=True)
class RdbnpRow:
    values: Mapping[str, tuple[object, ...]]

    def get_values(self, field_name: str) -> tuple[object, ...]:
        try:
            return self.values[field_name]
        except KeyError as exc:
            raise KeyError(f"RDBNP field '{field_name}' does not exist in this row") from exc

    def get(self, field_name: str) -> object:
        values = self.get_values(field_name)
        if len(values) != 1:
            raise ValueError(f"RDBNP field '{field_name}' is not scalar")
        return values[0]


@dataclass(frozen=True)
class RdbnpList:
    name: str
    type: RdbnpType
    rows: tuple[RdbnpRow, ...]


@dataclass(frozen=True)
class RdbnpDocument:
    source: bytes
    types: tuple[RdbnpType, ...]
    lists: tuple[RdbnpList, ...]
    format: str = CfgBinFormat.RDBNP

    def write_unmodified(self) -> bytes:
        return bytes(self.source)


def _fail(source: str, code: str, message: str, offset: Optional[int] = None) -> None:
    raise NativeFormatError(Diagnostic(code=code, message=message, source=source, offset=offset))


def _view(data: BinaryData) -> memoryview:
    return memoryview(data).cast("B")


def _require(data: memoryview, offset: int, length: int, source: str, label: str) -> None:
    if offset < 0 or length < 0 or offset > len(data) - length:
        _fail(source, "CFGBIN_RANGE", f"{label} is outside the resource", offset)


def _u16(data: memoryview, offset: int, source: str, label: str) -> int:
    _require(data, offset, 2, source, label)
    return struct.unpack_from("<H", data, offset)[0]


def _i16(data: memoryview, offset: int, source: str, label: str) -> int:
    _require(data, offset, 2, source, label)
    return struct.unpack_from("<h", data, offset)[0]


def _u32(data: memoryview, offset: int, source: str, label: str) -> int:
    _require(data, offset, 4, source, label)
    return struct.unpack_from("<I", data, offset)[0]


def _i32(data: memoryview, offset: int, source: str, label: str) -> int:
    _require(data, offset, 4, source, label)
    return struct.unpack_from("<i", data, offset)[0]


def _i64(data: memoryview, offset: int, source: str, label: str) -> int:
    _require(data, offset, 8, source, label)
    return struct.unpack_from("<q", data, offset)[0]


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _encoding(code: int, source: str) -> str:
    if code == 0:
        return "shift_jis"
    if code in (1, 256, 257):
        return "utf-8"
    _fail(source, "CFGBIN_ENCODING", f"unsupported string encoding code {code}")
    raise AssertionError("unreachable")


def _decode(data: memoryview, start: int, length: int, encoding: str, source: str, label: str) -> str:
    _require(data, start, length, source, label)
    try:
        return data[start : start + length].tobytes().decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        _fail(source, "CFGBIN_TEXT", f"{label} is not valid {encoding} text", start)
        raise AssertionError("unreachable") from exc


def _read_cstring(data: memoryview, start: int, end: int, encoding: str, source: str, label: str) -> str:
    _require(data, start, max(0, end - start), source, label)
    tail = data[start:end].tobytes()
    terminator = tail.find(b"\0")
    if terminator < 0:
        _fail(source, "CFGBIN_STRING_TERMINATOR", f"{label} is not null terminated", start)
    return _decode(data, start, terminator, encoding, source, label)


def parse_cfgbin(data: BinaryData, source: str = "<memory>") -> Union[CfgBinDocument, RdbnpDocument]:
    """Dispatch one CFGBIN resource without guessing between layouts."""

    view = _view(data)
    if len(view) >= 5 and view[:5].tobytes() == b"RDBNP":
        return parse_rdbnp(view, source)
    return parse_t2b(view, source)


def parse_cfgbin_file(path: Union[str, Path]) -> Union[CfgBinDocument, RdbnpDocument]:
    file_path = Path(path)
    return parse_cfgbin(file_path.read_bytes(), str(file_path))


def parse_t2b(data: BinaryData, source: str = "<memory>") -> CfgBinDocument:
    view = _view(data)
    if len(view) < 0x30:
        _fail(source, "T2B_HEADER", "resource is too short to contain a T2B envelope")
    footer = len(view) - 0x10
    if _u32(view, footer, source, "T2B footer magic") != T2B_FOOTER_MAGIC:
        _fail(source, "CFGBIN_MAGIC", "resource is neither a recognized T2B nor RDBNP file", footer)

    entry_count = _u32(view, 0, source, "T2B entry count")
    string_offset = _u32(view, 4, source, "T2B string data offset")
    string_length = _u32(view, 8, source, "T2B string data length")
    string_count = _u32(view, 12, source, "T2B string data count")
    if any(value > 0x7FFFFFFF for value in (entry_count, string_offset, string_length, string_count)):
        _fail(source, "T2B_BOUNDS", "T2B envelope value exceeds supported bounds")
    if string_offset < 0x10 or string_offset > footer or string_length > footer - string_offset:
        _fail(source, "T2B_STRING_RANGE", "T2B value-string section is outside the file", 4)
    actual_count = view[string_offset : string_offset + string_length].tobytes().count(b"\0")
    if string_count != actual_count:
        _fail(
            source,
            "T2B_STRING_COUNT",
            f"header declares {string_count} value strings but block contains {actual_count} terminators",
            0x0C,
        )

    encoding_code = _i16(view, footer + 6, source, "T2B encoding code")
    encoding = _encoding(encoding_code, source)
    name_lookup = _parse_t2b_names(view, _align(string_offset + string_length, 0x10), footer, encoding, source)
    maximum_entries = (string_offset - 0x10) // 8
    if entry_count > maximum_entries:
        _fail(source, "T2B_ENTRY_COUNT", "T2B entry table cannot fit before the string block", 0)

    if entry_count == 0:
        return CfgBinDocument(
            CfgBinFormat.T2B,
            view.tobytes(),
            entry_count=0,
            string_data_offset=string_offset,
            string_data_length=string_length,
            string_data_count=string_count,
            encoding_code=encoding_code,
            value_width=None,
        )

    candidates = []
    for width in (4, 8):
        parsed = _try_t2b_entries(view, int(entry_count), int(string_offset), int(string_length), width, source)
        if parsed is not None:
            candidates.append((width, parsed))
    if len(candidates) != 1:
        # A narrow 32-bit entry can occasionally be consumed as a plausible
        # 64-bit entry when the next record starts with a small value count.
        # The name CRC table is independent evidence from the same resource;
        # use it only to break a unique tie, never to accept an otherwise
        # malformed candidate.
        scores = [
            (
                sum(name_crc in name_lookup for name_crc, _, _ in parsed),
                _t2b_padding_length(parsed, width, int(string_offset)),
            )
            for width, parsed in candidates
        ]
        best_score = max(scores, default=(0, 0))
        if not candidates or scores.count(best_score) != 1 or best_score == (0, 0):
            _fail(source, "T2B_VALUE_WIDTH", "T2B value width is invalid or ambiguous")
        candidates = [candidates[scores.index(best_score)]]
    width, raw_entries = candidates[0]
    entries = []
    for index, (name_crc, types, raw_values) in enumerate(raw_entries):
        candidates_for_name = tuple(name_lookup.get(name_crc, ()))
        values = []
        for type_code, (raw_value, file_offset) in zip(types, raw_values):
            if type_code == CfgBinValueType.STRING:
                value = None if raw_value == -1 else _read_t2b_string(
                    view, int(string_offset), int(string_length), raw_value, encoding, source
                )
            elif type_code == CfgBinValueType.INTEGER:
                value = raw_value
            elif type_code == CfgBinValueType.FLOAT:
                value = struct.unpack("<f" if width == 4 else "<d", struct.pack("<i" if width == 4 else "<q", raw_value))[0]
            else:
                _fail(source, "T2B_VALUE_TYPE", f"unsupported T2B value type {type_code}", file_offset)
            values.append(CfgBinValue(type_code, value, raw_value, file_offset))
        entries.append(
            CfgBinEntry(
                index=index,
                name_crc=name_crc,
                name=candidates_for_name[0] if len(candidates_for_name) == 1 else None,
                name_candidates=candidates_for_name,
                values=tuple(values),
            )
        )
    return CfgBinDocument(
        CfgBinFormat.T2B,
        view.tobytes(),
        entries=tuple(entries),
        entry_count=int(entry_count),
        string_data_offset=int(string_offset),
        string_data_length=int(string_length),
        string_data_count=int(string_count),
        encoding_code=encoding_code,
        value_width=width,
    )


def _try_t2b_entries(
    data: memoryview,
    entry_count: int,
    string_offset: int,
    string_length: int,
    width: int,
    source: str,
) -> Optional[list[tuple[int, list[int], list[tuple[int, int]]]]]:
    cursor = 0x10
    result = []
    try:
        for _ in range(entry_count):
            entry_start = cursor
            if cursor > string_offset - 5:
                return None
            name_crc = _u32(data, cursor, source, "T2B entry name CRC")
            value_count = data[cursor + 4]
            cursor += 5
            type_byte_count = (value_count + 3) // 4
            if cursor > string_offset - type_byte_count:
                return None
            type_bytes = data[cursor : cursor + type_byte_count].tobytes()
            types = [(type_bytes[index // 4] >> ((index % 4) * 2)) & 3 for index in range(value_count)]
            if any(type_code == CfgBinValueType.UNKNOWN for type_code in types):
                return None
            cursor = _align(cursor + type_byte_count, 4)
            values = []
            for type_code in types:
                file_offset = cursor
                raw_value = _i32(data, cursor, source, "T2B entry value") if width == 4 else _i64(data, cursor, source, "T2B entry value")
                cursor += width
                if type_code == CfgBinValueType.STRING and raw_value != -1 and not 0 <= raw_value < string_length:
                    return None
                values.append((raw_value, file_offset))
            result.append((name_crc, types, values))
            if cursor < entry_start:
                return None
        if _align(cursor, 0x10) != string_offset:
            return None
        if any(byte != 0xFF for byte in data[cursor:string_offset].tobytes()):
            return None
        return result
    except (NativeFormatError, struct.error, OverflowError):
        return None


def _t2b_padding_length(
    parsed: list[tuple[int, list[int], list[tuple[int, int]]]],
    width: int,
    string_offset: int,
) -> int:
    """Return the confirmed FF padding between entries and value strings."""

    last_end = 0x10
    for _, _, values in parsed:
        if values:
            last_end = values[-1][1] + width
    return max(0, string_offset - last_end)


def _parse_t2b_names(
    data: memoryview,
    section_offset: int,
    footer_offset: int,
    encoding: str,
    source: str,
) -> dict[int, list[str]]:
    _require(data, section_offset, 0x10, source, "T2B name table header")
    section_size = _u32(data, section_offset, source, "T2B name table size")
    count = _u32(data, section_offset + 4, source, "T2B name count")
    string_offset = _u32(data, section_offset + 8, source, "T2B name string offset")
    string_length = _u32(data, section_offset + 12, source, "T2B name string length")
    if section_offset + section_size != footer_offset or section_size < 0x10:
        _fail(source, "T2B_NAME_SECTION", "T2B name section does not end at the footer", section_offset)
    records_length = count * 8
    _require(data, section_offset + 0x10, records_length, source, "T2B name records")
    minimum_string_offset = 0x10 + records_length
    if string_offset < minimum_string_offset or string_offset > section_size - string_length:
        _fail(source, "T2B_NAME_RANGE", "T2B name strings overlap the name records or section boundary", section_offset + 8)
    string_base = section_offset + string_offset
    _require(data, string_base, string_length, source, "T2B name strings")
    lookup: dict[int, list[str]] = {}
    for index in range(count):
        record_offset = section_offset + 0x10 + index * 8
        name_crc = _u32(data, record_offset, source, "T2B name CRC")
        relative = _u32(data, record_offset + 4, source, "T2B name offset")
        if relative >= string_length:
            _fail(source, "T2B_NAME_OFFSET", "T2B name offset points outside the name string block", record_offset + 4)
        name = _read_cstring(data, string_base + relative, string_base + string_length, encoding, source, "T2B name")
        lookup.setdefault(name_crc, []).append(name)
    return lookup


def _read_t2b_string(
    data: memoryview,
    block_offset: int,
    block_length: int,
    relative: int,
    encoding: str,
    source: str,
) -> str:
    if relative < 0 or relative >= block_length:
        _fail(source, "T2B_STRING_OFFSET", "T2B value string offset points outside its block")
    return _read_cstring(data, block_offset + relative, block_offset + block_length, encoding, source, "T2B value string")


def parse_rdbnp(data: BinaryData, source: str = "<memory>") -> RdbnpDocument:
    view = _view(data)
    if len(view) < 0x3C or view[:5].tobytes() != b"RDBNP":
        _fail(source, "RDBNP_HEADER", "resource does not contain an RDBNP header")
    data_base = _i16(view, 0x0A, source, "RDBNP data base") * 4
    data_size = _i32(view, 0x0C, source, "RDBNP data size")
    _require(view, data_base, data_size, source, "RDBNP data section")

    type_offset = data_base + _i16(view, 0x24, source, "RDBNP type offset") * 4
    type_count = _read_count(view, 0x26, source, "RDBNP type count")
    field_offset = data_base + _i16(view, 0x28, source, "RDBNP field offset") * 4
    field_count = _read_count(view, 0x2A, source, "RDBNP field count")
    root_offset = data_base + _i16(view, 0x2C, source, "RDBNP root offset") * 4
    root_count = _read_count(view, 0x2E, source, "RDBNP root count")
    hash_offset = data_base + _i16(view, 0x30, source, "RDBNP hash offset") * 4
    string_offsets_offset = data_base + _i16(view, 0x32, source, "RDBNP string offset table") * 4
    hash_count = _read_count(view, 0x34, source, "RDBNP string hash count")
    value_offset = data_base + _i16(view, 0x36, source, "RDBNP value offset") * 4
    string_offset = data_base + _i32(view, 0x38, source, "RDBNP string section offset")

    _require(view, type_offset, type_count * 0x20, source, "RDBNP type table")
    _require(view, field_offset, field_count * 0x20, source, "RDBNP field table")
    _require(view, root_offset, root_count * 0x20, source, "RDBNP root table")
    _require(view, hash_offset, hash_count * 4, source, "RDBNP hash table")
    _require(view, string_offsets_offset, hash_count * 4, source, "RDBNP string offset table")
    _require(view, string_offset, 1, source, "RDBNP string section")

    strings: dict[int, str] = {}
    for index in range(hash_count):
        name_hash = _u32(view, hash_offset + index * 4, source, "RDBNP string hash")
        relative = _i32(view, string_offsets_offset + index * 4, source, "RDBNP string offset")
        if relative < 0:
            _fail(source, "RDBNP_STRING_OFFSET", "RDBNP string offset is negative", string_offsets_offset + index * 4)
        strings[name_hash] = _read_cstring(view, string_offset + relative, len(view), "utf-8", source, "RDBNP string")

    fields = []
    for index in range(field_count):
        offset = field_offset + index * 0x20
        field = RdbnpField(
            _resolve_name(strings, _u32(view, offset, source, "RDBNP field name"), source),
            _i16(view, offset + 4, source, "RDBNP field type"),
            _i32(view, offset + 8, source, "RDBNP field element size"),
            _i32(view, offset + 12, source, "RDBNP field offset"),
            _i32(view, offset + 16, source, "RDBNP field count"),
        )
        if field.element_size <= 0 or field.offset < 0 or field.count <= 0:
            _fail(source, "RDBNP_FIELD_DIMENSIONS", "RDBNP field descriptor contains invalid dimensions", offset)
        fields.append(field)

    raw_types = []
    public_types = []
    for index in range(type_count):
        offset = type_offset + index * 0x20
        field_index = _read_count(view, offset + 8, source, "RDBNP type field index")
        count = _read_count(view, offset + 10, source, "RDBNP type field count")
        if field_index > len(fields) - count:
            _fail(source, "RDBNP_TYPE_FIELDS", "RDBNP type references fields outside the field table", offset + 8)
        selected = tuple(fields[field_index : field_index + count])
        name = _resolve_name(strings, _u32(view, offset, source, "RDBNP type name"), source)
        raw_types.append((name, selected))
        public_types.append(RdbnpType(name, selected))

    lists = []
    for index in range(root_count):
        offset = root_offset + index * 0x20
        type_index = _read_count(view, offset, source, "RDBNP root type index")
        if type_index >= len(raw_types):
            _fail(source, "RDBNP_ROOT_TYPE", "RDBNP root references an unknown type", offset)
        relative_values = _i32(view, offset + 4, source, "RDBNP root value offset")
        row_size = _i32(view, offset + 8, source, "RDBNP row size")
        row_count = _i32(view, offset + 12, source, "RDBNP row count")
        if relative_values < 0 or row_size <= 0 or row_count < 0:
            _fail(source, "RDBNP_ROOT_DIMENSIONS", "RDBNP root contains invalid row dimensions", offset)
        rows_offset = value_offset + relative_values
        _require(view, rows_offset, row_size * row_count, source, "RDBNP root values")
        row_values = []
        for row_index in range(row_count):
            row_offset = rows_offset + row_index * row_size
            values: dict[str, tuple[object, ...]] = {}
            for field in raw_types[type_index][1]:
                field_size = field.element_size * field.count
                if field.offset > row_size - field_size:
                    _fail(source, "RDBNP_FIELD_RANGE", "RDBNP field lies outside its row", row_offset + field.offset)
                field_values = tuple(
                    _read_rdbnp_value(
                        view,
                        row_offset + field.offset + value_index * field.element_size,
                        field.type_code,
                        field.element_size,
                        string_offset,
                        source,
                    )
                    for value_index in range(field.count)
                )
                values[field.name] = field_values
            row_values.append(RdbnpRow(values))
        list_name = _resolve_name(strings, _u32(view, offset + 16, source, "RDBNP list name"), source)
        lists.append(RdbnpList(list_name, public_types[type_index], tuple(row_values)))

    return RdbnpDocument(view.tobytes(), tuple(public_types), tuple(lists))


def _read_count(data: memoryview, offset: int, source: str, label: str) -> int:
    value = _i16(data, offset, source, label)
    if value < 0:
        _fail(source, "RDBNP_COUNT", f"{label} is negative", offset)
    return value


def _resolve_name(strings: Mapping[int, str], name_hash: int, source: str) -> str:
    try:
        return strings[name_hash]
    except KeyError:
        _fail(source, "RDBNP_NAME_HASH", f"RDBNP name hash 0x{name_hash:08X} has no string entry")
        raise AssertionError("unreachable")


def _read_rdbnp_value(
    data: memoryview,
    offset: int,
    type_code: int,
    element_size: int,
    string_offset: int,
    source: str,
) -> object:
    _require(data, offset, element_size, source, "RDBNP field value")
    if type_code == 3:
        return data[offset] != 0
    if type_code == 4:
        return data[offset]
    if type_code in (5, 9):
        return _i16(data, offset, source, "RDBNP Int16 value")
    if type_code in (6, 10):
        return _i32(data, offset, source, "RDBNP Int32 value")
    if type_code == 13:
        return struct.unpack_from("<f", data, offset)[0]
    if type_code == 15:
        return _u32(data, offset, source, "RDBNP UInt32 value")
    if type_code == 20:
        value = _u32(data, offset, source, "RDBNP condition string value")
        if value > 0x7FFFFFFF:
            return value
        absolute = string_offset + value
        if 0 <= absolute < len(data):
            return _read_cstring(data, absolute, len(data), "utf-8", source, "RDBNP condition string")
        return value
    if type_code == 21:
        if element_size < 4:
            _fail(source, "RDBNP_TUPLE_SIZE", "RDBNP tuple field is smaller than four bytes", offset)
        return RdbnpTuple(_i16(data, offset, source, "RDBNP tuple offset"), _i16(data, offset + 2, source, "RDBNP tuple count"))
    # These codes are fixed-size native payloads in the observed DUMP. Their
    # internal meaning is owned by each game table, so retain the bytes rather
    # than silently interpreting a vector, enum, or effect parameter.
    if type_code in (0, 1, 2, 8, 17, 18, 19):
        return data[offset : offset + element_size].tobytes()
    _fail(source, "RDBNP_VALUE_TYPE", f"unsupported RDBNP field type {type_code}", offset)
    raise AssertionError("unreachable")
