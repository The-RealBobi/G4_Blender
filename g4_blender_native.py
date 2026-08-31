"""Stable, Blender-free entry points for the new native foundation."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

try:
    from .formats import (
        FormatEvidence,
        build_format_evidence,
        parse_cfgbin_file,
        parse_g4cm_file,
        parse_g4ma_file,
        parse_g4mt_file,
    )
except ImportError:
    from formats import (
        FormatEvidence,
        build_format_evidence,
        parse_cfgbin_file,
        parse_g4cm_file,
        parse_g4ma_file,
        parse_g4mt_file,
    )


DEFAULT_EVIDENCE_EXTENSIONS = (
    ".g4md",
    ".g4mg",
    ".g4sk",
    ".g4pk",
    ".g4pkm",
    ".g4tx",
    ".g4mt",
    ".g4ma",
    ".g4cm",
    ".g4la",
    ".mevbin",
    ".ptlb",
    ".cfg.bin",
)


def collect_format_evidence(
    roots: Iterable[Path | str],
    extensions: Iterable[str] = DEFAULT_EVIDENCE_EXTENSIONS,
) -> tuple[FormatEvidence, ...]:
    return build_format_evidence(roots, extensions)


def parse_native_animation(path: Path | str):
    resource = Path(path)
    suffix = resource.name.casefold()
    if suffix.endswith(".g4ma"):
        return parse_g4ma_file(resource)
    if suffix.endswith(".g4cm"):
        return parse_g4cm_file(resource)
    if suffix.endswith(".g4mt"):
        return parse_g4mt_file(resource)
    raise ValueError(f"unsupported animation resource: {resource}")


def parse_native_config(path: Path | str):
    resource = Path(path)
    if not resource.name.casefold().endswith(".cfg.bin"):
        raise ValueError(f"unsupported configuration resource: {resource}")
    return parse_cfgbin_file(resource)


__all__ = [
    "DEFAULT_EVIDENCE_EXTENSIONS",
    "collect_format_evidence",
    "parse_native_animation",
    "parse_native_config",
]
