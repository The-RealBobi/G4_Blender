"""Shared semantic names for Character G4TX texture entries."""

from __future__ import annotations

import re
from pathlib import Path


def character_texture_role(name: str | Path) -> str:
    """Return the role encoded by a Character texture name.

    ``dp`` is kept as a ramp alias because older Character material tables use
    that short suffix for the fifth toon-control texture.  It is optional:
    most extracted Character containers do not carry it.
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
    if stem.endswith(("toon_ramp", "toonramp", "_ramp", "ramp", "_dp", "dp")):
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
    value = str(name).casefold()
    for suffix in (".dds", ".nxtch", ".bin"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


__all__ = ["character_texture_base_key", "character_texture_role"]
