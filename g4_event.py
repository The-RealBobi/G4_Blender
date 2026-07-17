"""Helpers for Level-5 event configuration data."""

from __future__ import annotations

import json
import fnmatch
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path


EVENT_ACTOR_COMMAND = 896822880
EVENT_ATTACH_POINT_COMMAND = -1563297470
ACTOR_RE = re.compile(r"^([a-z]{1,3}\d{4,10}(?:_[a-z0-9]+)*?)(?:_s\d+_p\d+)?$", re.IGNORECASE)
ACTOR_INSTANCE_RE = ACTOR_RE
MODEL_RE = re.compile(r"^[a-z]{1,3}\d{4,10}$", re.IGNORECASE)


def raw_cfg_records(path: Path) -> list[tuple[int, list[object]]]:
    data = path.read_bytes()
    if len(data) < 16:
        raise ValueError(f"truncated cfg.bin: {path}")
    entry_count, string_offset = struct.unpack_from("<II", data, 0)
    if entry_count > 1_000_000 or string_offset > len(data):
        raise ValueError(f"invalid cfg.bin header: {path}")
    pos = 16
    encoded = []
    for _ in range(entry_count):
        if pos + 5 > len(data):
            raise ValueError(f"truncated cfg.bin entry: {path}")
        entry_hash, value_count = struct.unpack_from("<IB", data, pos)
        pos += 5
        type_bytes = (value_count + 3) // 4
        packed_types = data[pos:pos + type_bytes]
        pos = (pos + type_bytes + 3) & ~3
        values = struct.unpack_from(f"<{value_count}i", data, pos) if value_count else ()
        pos += value_count * 4
        types = [packed_types[index // 4] >> ((index % 4) * 2) & 3 for index in range(value_count)]
        encoded.append((entry_hash, types, values))

    entries = []
    for entry_hash, types, values in encoded:
        decoded = []
        for value_type, value in zip(types, values):
            if value_type == 0:
                if value < 0:
                    decoded.append(None)
                    continue
                start = string_offset + value
                end = data.find(b"\0", start)
                decoded.append(data[start:end].decode("shift_jis", errors="replace"))
            elif value_type == 1:
                decoded.append(value)
            elif value_type == 2:
                decoded.append(struct.unpack("<f", struct.pack("<i", value))[0])
            else:
                decoded.append(value)
        entries.append((entry_hash, decoded))
    return entries


def raw_cfg_entries(path: Path) -> list[list[object]]:
    return [values for _, values in raw_cfg_records(path)]


def event_command_entries_from_binary(path: Path) -> list[dict]:
    names = {
        0x724243F8: "EVENT_COMMAND_HEADER",
        0x2F2A5B39: "EVENT_COMMAND_ARGS",
    }
    return [
        {
            "Name": names.get(entry_hash, f"0x{entry_hash:08x}"),
            "Values": [
                {"Index": index, "Value": value}
                for index, value in enumerate(values)
            ],
        }
        for entry_hash, values in raw_cfg_records(path)
    ]


def event_light_parameters(path: Path) -> dict[str, list[float]]:
    if path.suffix.lower() == ".json":
        entries = [
            [value.get("Value") for value in entry.get("Values") or ()]
            for entry in json.loads(path.read_text(encoding="utf-8")).get("Entries") or ()
        ]
    elif path.suffix.lower() == ".xml":
        entries = [
            [value.get("Value") for value in entry.get("Values") or ()]
            for entry in entries_from_xml(path)
        ]
    else:
        entries = raw_cfg_entries(path)
    result = {}
    for values in entries:
        if not values or not isinstance(values[0], str):
            continue
        numeric = [float(value) for value in values[1:] if isinstance(value, (int, float))]
        if values[0].startswith("chara") and numeric:
            result[values[0]] = numeric
    return result


def actor_models_from_entries(entries: list[dict]) -> dict[str, str]:
    models = {}
    command = None
    for entry in entries:
        values = {
            int(value.get("Index", index)): value.get("Value")
            for index, value in enumerate(entry.get("Values") or ())
        }
        if entry.get("Name") == "EVENT_COMMAND_HEADER":
            command = values.get(1)
            continue
        if entry.get("Name") != "EVENT_COMMAND_ARGS" or command != EVENT_ACTOR_COMMAND:
            continue
        actor_match = ACTOR_RE.fullmatch(str(values.get(0) or ""))
        model = str(values.get(1) or "").lower()
        if actor_match and MODEL_RE.fullmatch(model):
            models.setdefault(actor_match.group(1).lower(), model)
    return models


def actor_points_from_entries(entries: list[dict]) -> dict[str, str]:
    points = {}
    command = None
    for entry in entries:
        values = {
            int(value.get("Index", index)): value.get("Value")
            for index, value in enumerate(entry.get("Values") or ())
        }
        if entry.get("Name") == "EVENT_COMMAND_HEADER":
            command = values.get(1)
            continue
        if entry.get("Name") != "EVENT_COMMAND_ARGS" or command != EVENT_ATTACH_POINT_COMMAND:
            continue
        actor_match = ACTOR_INSTANCE_RE.fullmatch(str(values.get(0) or ""))
        source = str(values.get(1) or "").lower()
        point = str(values.get(2) or "").lower()
        if actor_match and source.startswith("point_") and re.fullmatch(r"evp\d+", point):
            points.setdefault(actor_match.group(1).lower(), point)
    return points


def actor_point_assignments_from_entries(entries: list[dict]) -> dict[str, dict[str, tuple[str, str]]]:
    """Resolve per-cut point attachments, including Victory Road's TYPE templates."""
    assignments: dict[str, dict[str, tuple[str, str]]] = {}
    command = None
    cut = ""
    template_types: dict[str, str] = {}
    for entry in entries:
        values = {
            int(value.get("Index", index)): value.get("Value")
            for index, value in enumerate(entry.get("Values") or ())
        }
        if entry.get("Name") == "EVENT_COMMAND_HEADER":
            command = values.get(1)
            continue
        if entry.get("Name") != "EVENT_COMMAND_ARGS":
            continue
        ordered = [values.get(index) for index in range(max(values, default=-1) + 1)]
        if len(ordered) >= 2 and str(ordered[1] or "").upper() == "RESET_DEF":
            candidate = str(ordered[0] or "").lower()
            if re.fullmatch(r"c\d+", candidate):
                cut = candidate
        if len(ordered) >= 4 and str(ordered[0] or "").lower().startswith("point_s"):
            candidate = str(ordered[-1] or "").lower()
            if re.fullmatch(r"c\d+", candidate):
                cut = candidate
        if len(ordered) >= 3 and "<type>" in str(ordered[0] or "").lower() and ordered[1] == "TYPE":
            template_types[str(ordered[0]).lower()] = str(ordered[2]).zfill(2)
        if command != EVENT_ATTACH_POINT_COMMAND or len(ordered) < 3:
            continue
        actor = str(ordered[0] or "").lower()
        source = str(ordered[1] or "").lower()
        point = str(ordered[2] or "").lower()
        if not cut or not source.startswith("point_s") or not re.fullmatch(r"evp\d+", point):
            continue
        if "<type>" in actor:
            actor = actor.replace("<type>", template_types.get(actor, "??"))
            actor = actor.replace("<no>", "*").replace("_p<variation>", "")
        else:
            match = ACTOR_INSTANCE_RE.fullmatch(actor)
            if match is None:
                continue
            actor = match.group(1).lower()
        assignments.setdefault(cut, {})[actor] = (source, point)
    return assignments


def point_assignment_for_actor(
    assignments: dict[str, dict[str, tuple[str, str]]], cut: str, actor: str
) -> tuple[str, str] | None:
    candidates = assignments.get(cut.lower()) or {}
    actor = actor.lower()
    base = re.sub(r"_s\d+$", "", actor)
    for candidate in (actor, base):
        if candidate in candidates:
            return candidates[candidate]
    return next((value for pattern, value in candidates.items() if fnmatch.fnmatchcase(actor, pattern)), None)


def actor_points_from_raw_records(path: Path) -> dict[str, str]:
    """Read the inline command layout used by YK4 event_cfg/vis files."""
    points = {}
    for _, values in raw_cfg_records(path):
        if len(values) < 5 or values[1] != EVENT_ATTACH_POINT_COMMAND:
            continue
        actor_match = ACTOR_INSTANCE_RE.fullmatch(str(values[2] or ""))
        source = str(values[3] or "").lower()
        point = str(values[4] or "").lower()
        if actor_match and source.startswith("point_") and re.fullmatch(r"evp\d+", point):
            points.setdefault(actor_match.group(1).lower(), point)
    return points


def actor_models_from_json(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return actor_models_from_entries(data.get("Entries") or [])


def actor_points_from_json(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return actor_points_from_entries(data.get("Entries") or [])


def actor_models_from_xml(path: Path) -> dict[str, str]:
    entries = []
    for entry in ET.parse(path).getroot().findall("entry"):
        values = []
        for value in entry.findall("./values/value"):
            text = value.text or ""
            if value.get("type") == "Integer":
                try:
                    text = int(text)
                except ValueError:
                    pass
            values.append({"Index": int(value.get("index", len(values))), "Value": text})
        entries.append({"Name": entry.get("name"), "Values": values})
    return actor_models_from_entries(entries)


def entries_from_xml(path: Path) -> list[dict]:
    entries = []
    for entry in ET.parse(path).getroot().findall("entry"):
        values = []
        for value in entry.findall("./values/value"):
            text = value.text or ""
            if value.get("type") == "Integer":
                try:
                    text = int(text)
                except ValueError:
                    pass
            values.append({"Index": int(value.get("index", len(values))), "Value": text})
        entries.append({"Name": entry.get("name"), "Values": values})
    return entries


def load_event_actor_models(path: Path) -> dict[str, str]:
    if path.suffix.lower() == ".json":
        return actor_models_from_json(path)
    if path.suffix.lower() == ".xml":
        return actor_models_from_xml(path)
    if path.name.lower().endswith(".cfg.bin"):
        return actor_models_from_entries(event_command_entries_from_binary(path))
    return {}


def load_event_actor_points(path: Path) -> dict[str, str]:
    if path.suffix.lower() == ".json":
        return actor_points_from_json(path)
    if path.suffix.lower() == ".xml":
        return actor_points_from_entries(entries_from_xml(path))
    if path.name.lower().endswith(".cfg.bin"):
        points = actor_points_from_entries(event_command_entries_from_binary(path))
        return points or actor_points_from_raw_records(path)
    return {}


def load_event_actor_point_assignments(path: Path) -> dict[str, dict[str, tuple[str, str]]]:
    if path.suffix.lower() == ".json":
        entries = json.loads(path.read_text(encoding="utf-8")).get("Entries") or []
    elif path.suffix.lower() == ".xml":
        entries = entries_from_xml(path)
    elif path.name.lower().endswith(".cfg.bin"):
        entries = event_command_entries_from_binary(path)
    else:
        return {}
    return actor_point_assignments_from_entries(entries)
