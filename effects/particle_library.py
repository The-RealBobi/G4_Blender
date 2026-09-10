"""Typed, read-only view of Level-5 PTLB particle timing blocks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    from ..formats.cfgbin import CfgBinDocument, CfgBinFormat, parse_cfgbin_file
except ImportError:
    from formats.cfgbin import CfgBinDocument, CfgBinFormat, parse_cfgbin_file


@dataclass(frozen=True)
class ParticleEmitter:
    index: int
    node_name: str
    emission_interval_seconds: float
    mode: int
    phase_durations_seconds: tuple[float, float, float]
    lifetime_seconds: float
    motion_name: str | None
    sections: tuple[tuple[str, tuple[object, ...]], ...]

    @property
    def preview_duration_seconds(self) -> float:
        return sum(self.phase_durations_seconds) + self.lifetime_seconds


@dataclass(frozen=True)
class ParticleLibrary:
    node_names: tuple[str, ...]
    emitters: tuple[ParticleEmitter, ...]


def _values(entry) -> tuple[object, ...]:
    return tuple(value.value for value in entry.values)


def _single_section(sections, name: str, source: str):
    matches = [entry for entry in sections if entry.name == name]
    if len(matches) != 1:
        raise ValueError(f"{source}: particle block requires exactly one {name}")
    return matches[0]


def decode_particle_library(document: CfgBinDocument, source: str) -> ParticleLibrary:
    if not isinstance(document, CfgBinDocument) or document.format != CfgBinFormat.T2B:
        raise ValueError(f"{source}: PTLB requires the T2B CFGBIN layout")
    entries = document.entries
    begin = [index for index, entry in enumerate(entries) if entry.name == "PARTICLE_NODE_INFO_BGN"]
    end = [index for index, entry in enumerate(entries) if entry.name == "PARTICLE_NODE_INFO_END"]
    if len(begin) != 1 or len(end) != 1 or begin[0] >= end[0]:
        raise ValueError(f"{source}: invalid particle-node envelope")
    declaration = _values(entries[begin[0]])
    node_entries = entries[begin[0] + 1:end[0]]
    if len(declaration) != 1 or any(entry.name != "PARTICLE_NODE_INFO" for entry in node_entries):
        raise ValueError(f"{source}: invalid particle-node declaration")
    node_names = tuple(
        str(values[0])
        for entry in node_entries
        if len(values := _values(entry)) == 1 and isinstance(values[0], str)
    )
    if len(node_names) != len(node_entries) or int(declaration[0]) != len(node_names):
        raise ValueError(f"{source}: declared particle-node count does not match its entries")

    blocks = []
    current = None
    for entry in entries[end[0] + 1:]:
        if entry.name == "PARTICLE_INFO_BGN":
            if current is not None:
                raise ValueError(f"{source}: nested particle block")
            current = []
        elif entry.name == "PARTICLE_INFO_END":
            if current is None:
                raise ValueError(f"{source}: particle block ends before it begins")
            blocks.append(tuple(current))
            current = None
        elif current is None:
            raise ValueError(f"{source}: entry outside a particle block")
        else:
            current.append(entry)
    if current is not None:
        raise ValueError(f"{source}: unterminated particle block")
    if len(blocks) != len(node_names):
        raise ValueError(f"{source}: PTLB requires one particle block per node")

    emitters = []
    for index, (node_name, sections) in enumerate(zip(node_names, blocks)):
        emitter = _values(_single_section(sections, "EMITTER_INFO", source))
        lifetime = _values(_single_section(sections, "LIFE_TIME_INFO", source))
        if len(emitter) < 5 or len(lifetime) < 2:
            raise ValueError(f"{source}: incomplete particle timing block {index}")
        try:
            interval = float(emitter[0])
            mode = int(emitter[1])
            phases = tuple(float(value) for value in emitter[2:5])
            lifetime_seconds = float(lifetime[1])
        except (TypeError, ValueError) as error:
            raise ValueError(f"{source}: invalid particle timing value in block {index}") from error
        if interval < 0 or any(value < 0 for value in phases) or lifetime_seconds < 0:
            raise ValueError(f"{source}: negative particle timing value in block {index}")
        motions = [entry for entry in sections if entry.name == "MOTION_INFO"]
        if len(motions) > 1:
            raise ValueError(f"{source}: particle block has multiple motion sections")
        motion_values = _values(motions[0]) if motions else ()
        motion_name = motion_values[1] if len(motion_values) > 1 and isinstance(motion_values[1], str) else None
        emitters.append(ParticleEmitter(
            index=index,
            node_name=node_name,
            emission_interval_seconds=interval,
            mode=mode,
            phase_durations_seconds=phases,
            lifetime_seconds=lifetime_seconds,
            motion_name=motion_name,
            sections=tuple((entry.name or f"0x{entry.name_crc:08X}", _values(entry))
                           for entry in sections),
        ))
    return ParticleLibrary(node_names, tuple(emitters))


def read_particle_library(path: Path) -> ParticleLibrary:
    return decode_particle_library(parse_cfgbin_file(path), path.name)


def particle_library_metadata(library: ParticleLibrary) -> dict[str, object]:
    return {
        "nodes": list(library.node_names),
        "emitters": [
            {
                "node": emitter.node_name,
                "interval_seconds": emitter.emission_interval_seconds,
                "mode": emitter.mode,
                "phase_seconds": list(emitter.phase_durations_seconds),
                "lifetime_seconds": emitter.lifetime_seconds,
                "preview_seconds": emitter.preview_duration_seconds,
                "motion": emitter.motion_name,
            }
            for emitter in library.emitters
        ],
    }


__all__ = [
    "ParticleEmitter",
    "ParticleLibrary",
    "decode_particle_library",
    "particle_library_metadata",
    "read_particle_library",
]
