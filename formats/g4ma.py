"""G4MA material-animation targets and shader-effect bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .contracts import EffectBinding, FidelityState
from .g4mt import G4MTAnimationBank, crc32b, parse_g4ma, parse_g4ma_file

__all__ = [
    "EffectBinding",
    "G4MAMaterialTarget",
    "G4MAMaterialChannelSummary",
    "parse_g4ma",
    "parse_g4ma_file",
    "resolve_g4ma_material_targets",
    "summarize_g4ma_material_channels",
    "build_g4ma_effect_bindings",
]


@dataclass(frozen=True)
class G4MAMaterialTarget:
    target_index: int
    name_hash: int
    material_index: int | None


@dataclass(frozen=True)
class G4MAMaterialChannelSummary:
    material_index: int
    target_indices: tuple[int, ...]
    channel_types: tuple[int, ...]
    channel_count: int
    target_info_count: int


def resolve_g4ma_material_targets(
    bank: G4MTAnimationBank,
    material_names: Sequence[str],
) -> tuple[G4MAMaterialTarget, ...]:
    by_hash: dict[int, int | None] = {}
    for index, name in enumerate(material_names):
        name_hash = crc32b(name)
        by_hash[name_hash] = index if name_hash not in by_hash else None
    return tuple(
        G4MAMaterialTarget(target.index, target.name_hash, by_hash.get(target.name_hash))
        for target in bank.targets
    )


def summarize_g4ma_material_channels(
    bank: G4MTAnimationBank,
    targets: Sequence[G4MAMaterialTarget],
) -> tuple[G4MAMaterialChannelSummary, ...]:
    target_to_material = {
        target.target_index: target.material_index
        for target in targets
        if target.material_index is not None
    }
    grouped: dict[int, dict[str, object]] = {}
    for target_info in bank.target_infos:
        material_index = target_to_material.get(target_info.target_index)
        if material_index is None:
            continue
        end = target_info.channel_start + target_info.channel_count
        bucket = grouped.setdefault(
            material_index,
            {"targets": set(), "types": set(), "channel_count": 0, "target_info_count": 0},
        )
        bucket["targets"].add(target_info.target_index)
        bucket["types"].update(channel.channel_type for channel in bank.channels[target_info.channel_start:end])
        bucket["channel_count"] += target_info.channel_count
        bucket["target_info_count"] += 1
    return tuple(
        G4MAMaterialChannelSummary(
            material_index=index,
            target_indices=tuple(sorted(bucket["targets"])),
            channel_types=tuple(sorted(bucket["types"])),
            channel_count=int(bucket["channel_count"]),
            target_info_count=int(bucket["target_info_count"]),
        )
        for index, bucket in sorted(grouped.items())
    )


def build_g4ma_effect_bindings(
    bank: G4MTAnimationBank,
    material_names: Sequence[str],
    channel_parameter_map: dict[int, str],
) -> tuple[EffectBinding, ...]:
    """Map only explicitly evidenced channel types to shader parameters."""

    targets = resolve_g4ma_material_targets(bank, material_names)
    target_by_index = {target.target_index: target for target in targets}
    result: list[EffectBinding] = []
    for target_info in bank.target_infos:
        target = target_by_index.get(target_info.target_index)
        if target is None or target.material_index is None:
            continue
        for channel in bank.channels[target_info.channel_start : target_info.channel_start + target_info.channel_count]:
            parameter = channel_parameter_map.get(channel.channel_type)
            if parameter is None:
                continue
            result.append(
                EffectBinding(
                    material_name=material_names[target.material_index],
                    parameter_name=parameter,
                    channel_type=channel.channel_type,
                    fidelity=FidelityState.APPROXIMATE,
                )
            )
    return tuple(result)
