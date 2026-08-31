"""Runtime-neutral G4MA channel application for effect previews."""

from __future__ import annotations

from dataclasses import dataclass, replace


_CHANNEL_NAMES = {
    19: "color",
    26: "camera_position",
    27: "camera_rotation",
    28: "camera_fov",
    32: "float",
    33: "vector",
}


def channel_parameter_name(channel_type: int) -> str | None:
    return _CHANNEL_NAMES.get(channel_type)


@dataclass(frozen=True)
class EffectMaterialState:
    values: tuple[tuple[str, tuple[float, ...]], ...] = ()

    def get(self, name: str) -> tuple[float, ...] | None:
        return dict(self.values).get(name)


def apply_g4ma_channel(state: EffectMaterialState, channel_type: int, value: tuple[float, ...]) -> EffectMaterialState:
    name = channel_parameter_name(channel_type)
    if name is None:
        return state
    values = dict(state.values)
    values[name] = tuple(float(item) for item in value)
    return replace(state, values=tuple(sorted(values.items())))


__all__ = ["EffectMaterialState", "apply_g4ma_channel", "channel_parameter_name"]
