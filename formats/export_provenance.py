"""Internal provenance for model and package formats tested in the game."""
import struct


def mark_export(data: bytes, extension: str, version: tuple[int, ...]) -> bytes:
    marker = ("G4B_v" + ".".join(map(str, version))).encode("ascii")
    if extension in {".g4mg", ".g4tx"}:
        return data + marker.ljust((len(marker) + 15) & ~15, b"\0")
    if extension not in {".g4md", ".g4pk"}:
        return data
    header, = struct.unpack_from("<H", data, 4)
    declared, = struct.unpack_from("<I", data, 12)
    if header + declared != len(data):
        return data
    alignment = 64 if extension == ".g4pk" else 4
    marked = bytearray(data + marker)
    marked.extend(bytes(-len(marked) % alignment))
    struct.pack_into("<I", marked, 12, len(marked) - header)
    return bytes(marked)
