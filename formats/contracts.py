"""Small, immutable contracts shared by format and shading owners."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping

__all__ = [
    "EffectBinding",
    "FidelityState",
    "ResourceEvidence",
    "ResourceIdentity",
    "ShaderParameter",
    "ShaderProfile",
]


class FidelityState(str, Enum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ResourceIdentity:
    path: str
    extension: str
    source_root: str
    sha256: str | None = None


@dataclass(frozen=True)
class ResourceEvidence:
    identity: ResourceIdentity
    confidence: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShaderParameter:
    name: str
    value_type: str
    default: object = None
    native_source: str = ""
    supported: bool = True


@dataclass(frozen=True)
class ShaderProfile:
    name: str
    family: str
    fidelity: FidelityState = FidelityState.APPROXIMATE
    parameters: tuple[ShaderParameter, ...] = ()
    evidence: tuple[ResourceEvidence, ...] = ()
    notes: tuple[str, ...] = ()

    def parameter(self, name: str) -> ShaderParameter | None:
        return next((item for item in self.parameters if item.name == name), None)


@dataclass(frozen=True)
class EffectBinding:
    material_name: str
    parameter_name: str
    channel_type: int
    fidelity: FidelityState = FidelityState.APPROXIMATE
