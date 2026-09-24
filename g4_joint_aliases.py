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


def canonical_export_joint(name: str) -> str:
    """Map Blender helper bones to the compact joints used by G4 resources."""
    key = normalize_joint_key(name)
    if not key:
        return ""
    if key in {"c_head_1_0", "c_n_1_0"} or key.startswith("c_head_"):
        return key

    # Hair is authored as part of the head for the separated custom models.
    # Keeping native hir/twin-tail helpers here lets animation move the hair
    # independently and recreates the neck/shoulder drift seen in-game.
    if (
        "hair" in key
        or "ponytail" in key
        or "twintail" in key
        or "braid" in key
        or "kami" in key
        or re.search(r"(^|_)hir", key)
    ):
        return "c_head_1_0"

    # Kyoko and similar rigs use separate facial controls, while the game
    # evaluates this geometry from the head joint alone.
    if key == "eye" or key.startswith(("eye_", "face_eye", "eyelid", "double_eyelid")):
        return "c_head_1_0"
    if key in {"face", "jaw", "face_jaw", "mouth"} or key.startswith(("mouth_", "tooth", "teeth", "brow_")):
        return "c_head_1_0"

    side = "l" if key.endswith("_l") or key.endswith("l") else "r" if key.endswith("_r") or key.endswith("r") else ""
    if side:
        stem = key[:-2] if key.endswith(("_l", "_r")) else key[:-1]
        if stem.startswith("elbowroll"):
            return f"{side}_a_1_1"

        finger_match = re.fullmatch(r"f_(thumb|fore|index|middle|mid|ring|medicinal|little|pinky)(\d+)", stem)
        if finger_match:
            family, number = finger_match.groups()
            family_target = {
                "thumb": "thb", "fore": "idx", "index": "idx", "middle": "mid", "mid": "mid",
                "ring": "rng", "medicinal": "rng", "little": "pky", "pinky": "pky",
            }[family]
            index = max(0, int(number) - 1)
            return f"{side}_{family_target}_1_{index}"

    if key == "skirt" or key.startswith("skirt_"):
        if key.startswith(("skirt_mr", "skirt_r", "skirt_rb")):
            return "r_l_1_0"
        if key.startswith(("skirt_ml", "skirt_l", "skirt_lb")):
            return "l_l_1_0"
        return "c_c_1_0"
    return ""


def canonical_skirt_joint(name: str, lateral_position: float) -> str:
    """Return the thigh that owns a skirt panel at this vertex position."""
    key = normalize_joint_key(name)
    if key == "skirt" or key.startswith(("skirt_f", "skirt_b")):
        if lateral_position < 0.0:
            return "r_l_1_0"
        if lateral_position > 0.0:
            return "l_l_1_0"
    return ""


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
