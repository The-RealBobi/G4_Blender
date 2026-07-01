"""Helpers for Level-5 event configuration data."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


EVENT_ACTOR_COMMAND = 896822880
EVENT_ATTACH_POINT_COMMAND = -1563297470
ACTOR_RE = re.compile(r"^([a-z]{1,3}\d{4,10}(?:_[a-z0-9]+)*?)(?:_s\d+_p\d+)?$", re.IGNORECASE)
ACTOR_INSTANCE_RE = ACTOR_RE
MODEL_RE = re.compile(r"^[a-z]{1,3}\d{4,10}$", re.IGNORECASE)


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
    return {}


def load_event_actor_points(path: Path) -> dict[str, str]:
    if path.suffix.lower() == ".json":
        return actor_points_from_json(path)
    if path.suffix.lower() == ".xml":
        return actor_points_from_entries(entries_from_xml(path))
    return {}
