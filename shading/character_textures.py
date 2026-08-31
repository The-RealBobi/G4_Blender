"""Shared semantic names for Character G4TX texture entries."""

from __future__ import annotations

import re
from pathlib import Path


GLOBAL_CHARACTER_RAMP_NAMES = frozenset({"chrgrd", "chrgrd_01"})


def character_data_roots(raw_root: str | Path) -> list[Path]:
    """Return data roots for either a dump's ``raw`` folder or ``raw/data``."""

    root = Path(raw_root).expanduser()
    candidates = [root]
    if (root / "data").is_dir():
        candidates.append(root / "data")
    if root.is_dir():
        candidates.extend(
            child for child in sorted(root.iterdir())
            if child.is_dir() and child.name.casefold().startswith("data.")
        )

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if not any(
            (candidate / marker).is_dir()
            for marker in ("common", "dx11", "nx")
        ):
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def character_shader_texture_containers(raw_root: str | Path) -> list[Path]:
    """Find shared Character shader texture containers in a raw game dump."""

    candidates: list[Path] = []
    for data_root in character_data_roots(raw_root):
        for platform in ("dx11", "nx"):
            shader_root = data_root / platform / "chr" / "shader"
            for directory in (shader_root / "texture", shader_root):
                if directory.is_dir():
                    candidates.extend(sorted(directory.glob("*.g4tx")))

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def is_global_character_ramp_name(name: str | Path) -> bool:
    """Whether a shared shader texture name is the game's Character ramp."""

    stem = _texture_stem(name)
    return stem in GLOBAL_CHARACTER_RAMP_NAMES


def character_texture_role(name: str | Path) -> str:
    """Return the role encoded by a Character texture name.

    Ramp detection remains a compatibility contract for imported texture
    inventories, but the legacy Character material does not consume it.
    """

    stem = _texture_stem(name)
    if "cubemap" in stem or re.search(r"(?:^|_)cm\d+_tex$", stem):
        return "environment"
    if stem.endswith("_a.1"):
        return "transparent_base"
    if stem.endswith(".a"):
        return "alpha_red"
    if stem.endswith("msk") or stem.endswith("_mask"):
        return "mask"
    if stem.endswith("nml") or stem.endswith("_normal"):
        return "normal"
    if stem.endswith(("_re.2", "_n.2", "_nm.2")):
        return "normal"
    if stem.endswith(".2") or stem.endswith(("_n", "_nm")) or "_normal" in stem:
        return "normal"
    if is_global_character_ramp_name(stem) or stem.endswith(
        ("toon_ramp", "toonramp", "_ramp", "ramp", "_dp", "dp")
    ):
        return "ramp"
    if stem.endswith("spm"):
        return "specular_mask"
    if stem.endswith("sp"):
        return "specular"
    if stem.endswith("oc"):
        return "occlusion"
    if stem.endswith("line"):
        return "line"
    return "base"


def character_texture_base_key(name: str | Path, role: str | None = None) -> str:
    """Remove a Character texture role suffix while retaining its material key."""

    stem = _texture_stem(name)
    role = role or character_texture_role(stem)
    if role == "transparent_base" and stem.endswith("_a.1"):
        return stem[:-2].strip("_")
    if role == "normal" and stem.endswith(".2"):
        return stem[:-2].strip("_")
    if role == "normal" and stem.endswith("nml"):
        return re.sub(r"_?nml$", "", stem).strip("_")
    if role == "alpha_red" and stem.endswith(".a"):
        return stem[:-2].strip("_")
    if role == "mask":
        return re.sub(r"(?:_?msk|_mask)$", "", stem).strip("_")
    if role == "ramp":
        return re.sub(r"(?:_?toon_?ramp|_?ramp|_?dp)$", "", stem).strip("_")
    if role in {"specular_mask", "specular", "occlusion", "line"}:
        return re.sub(r"(?:spm|sp|oc|line)$", "", stem).strip("_")
    return re.sub(r"\.(?:\d+|[a-z])$", "", stem).strip("_")


def _texture_stem(name: str | Path) -> str:
    value = str(name).replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in (".dds", ".nxtch", ".bin"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


__all__ = [
    "GLOBAL_CHARACTER_RAMP_NAMES",
    "character_data_roots",
    "character_shader_texture_containers",
    "character_texture_base_key",
    "character_texture_role",
    "is_global_character_ramp_name",
]
