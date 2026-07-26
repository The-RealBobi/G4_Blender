"""Load and validate the editable G4 joint alias catalog."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


CATALOG_FILENAME = "g4_joint_aliases.json"
_BLENDER_COPY_SUFFIX = re.compile(r"\.\d+$")
_DEFAULT_CATALOG_CACHE: tuple[int, JointAliasCatalog] | None = None


@dataclass(frozen=True)
class JointAliasCatalog:
    joints: frozenset[str]
    aliases: dict[str, str]


def normalize_joint_key(name: str) -> str:
    """Normalize a vertex-group name without losing meaningful separators."""
    return _BLENDER_COPY_SUFFIX.sub("", name.strip()).lower().replace("-", "_").replace(" ", "_")


def catalog_path() -> Path:
    return Path(__file__).with_name(CATALOG_FILENAME)


def resolve_catalog_alias(
    name: str, catalog: JointAliasCatalog, allowed_joints: set[str] | None = None
) -> str:
    target = catalog.aliases.get(normalize_joint_key(name), "")
    if target and (allowed_joints is None or target in allowed_joints):
        return target
    return ""


def load_joint_alias_catalog(path: Path | None = None) -> JointAliasCatalog:
    global _DEFAULT_CATALOG_CACHE
    source = path or catalog_path()
    if path is None:
        try:
            modified = source.stat().st_mtime_ns
        except OSError:
            modified = -1
        if _DEFAULT_CATALOG_CACHE is not None and _DEFAULT_CATALOG_CACHE[0] == modified:
            return _DEFAULT_CATALOG_CACHE[1]
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read joint alias catalog: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid joint alias catalog JSON: {source}") from exc

    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Joint alias catalog must declare version 1")
    joints = payload.get("joints")
    if not isinstance(joints, dict) or not joints:
        raise ValueError("Joint alias catalog must contain a non-empty joints object")

    aliases: dict[str, str] = {}
    targets: set[str] = set()
    for target, names in joints.items():
        if not isinstance(target, str) or not target:
            raise ValueError("Joint alias catalog contains an invalid target joint")
        if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
            raise ValueError(f"Joint alias catalog entry {target!r} must be a list of names")
        targets.add(target)
        for name in (target, *names):
            key = normalize_joint_key(name)
            if not key:
                raise ValueError(f"Joint alias catalog entry {target!r} contains an empty alias")
            existing = aliases.get(key)
            if existing is not None and existing != target:
                raise ValueError(
                    f"Joint alias {name!r} is assigned to both {existing!r} and {target!r}"
                )
            aliases[key] = target
    catalog = JointAliasCatalog(frozenset(targets), aliases)
    if path is None:
        _DEFAULT_CATALOG_CACHE = (source.stat().st_mtime_ns, catalog)
    return catalog
