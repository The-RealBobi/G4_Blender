"""Structural summaries for G4CM camera banks."""

from __future__ import annotations

from dataclasses import dataclass

from .g4mt import G4MTAnimationBank, parse_g4cm, parse_g4cm_file


@dataclass(frozen=True)
class G4CMAnalysis:
    clip_names: tuple[str, ...]
    target_count: int
    channel_count: int
    channel_types: tuple[int, ...]
    key_count: int
    semantic_status: str = "unmapped"


def analyze_g4cm_bank(bank: G4MTAnimationBank) -> G4CMAnalysis:
    return G4CMAnalysis(
        clip_names=tuple(clip.name for clip in bank.clips),
        target_count=bank.header.target_count,
        channel_count=len(bank.channels),
        channel_types=tuple(sorted({channel.channel_type for channel in bank.channels})),
        key_count=sum(channel.key_count for channel in bank.channels),
    )


__all__ = ["G4CMAnalysis", "analyze_g4cm_bank", "parse_g4cm", "parse_g4cm_file"]
