"""Compatibility profile for the existing character Toon shader."""

from __future__ import annotations

from dataclasses import dataclass

try:
    from ..formats.contracts import FidelityState, ShaderParameter, ShaderProfile
except ImportError:
    from formats.contracts import FidelityState, ShaderParameter, ShaderProfile


@dataclass(frozen=True)
class CharacterShaderProfile:
    """Describes the current shader without rebuilding its node graph."""

    name: str = "G4 Character Toon"
    family: str = "character_toon"
    fidelity: FidelityState = FidelityState.APPROXIMATE
    uses_eevee_shader_to_rgb: bool = True
    supports_dxt5nm: bool = True
    supports_rgb_masks: bool = True
    supports_rim_and_specular: bool = True

    def as_contract(self) -> ShaderProfile:
        return ShaderProfile(
            name=self.name,
            family=self.family,
            fidelity=self.fidelity,
            parameters=(
                ShaderParameter("base_color", "color", native_source="G4TX"),
                ShaderParameter("normal_dxt5nm", "normal", native_source="G4TX"),
                ShaderParameter("normal_rgb_nml", "normal", native_source="G4TX nml/normal (optional)"),
                ShaderParameter("occlusion", "mask", native_source="G4TX _oc"),
                ShaderParameter("specular_shape", "texture", native_source="G4TX _sp"),
                ShaderParameter("specular_mask", "mask", native_source="G4TX _spm"),
                ShaderParameter("mask_rgb", "color", native_source="G4TX"),
                ShaderParameter("mask_rgb_msk", "color", native_source="G4TX _msk (optional)"),
                ShaderParameter("toon_ramp", "color", native_source="G4TX dp/ramp (optional, diagnostic until bound)"),
                ShaderParameter("scene_gradient", "texture", native_source="DXBC in_texGrd static UNORM gradient alpha rows (captured variant)", supported=False),
                ShaderParameter("scene_gradient_params", "vector", native_source="light_data.cfg.bin u_charaGrTParam", supported=False),
                ShaderParameter("rim", "float", native_source="shader variant"),
                ShaderParameter("specular", "float", native_source="shader variant"),
            ),
            notes=(
                "Preserve the existing apply_level5_toon_shader node graph.",
                "ShaderToRGB requires EEVEE; other engines receive a marked fallback.",
                "Character ambient, dual shadow colors, threshold and rim defaults follow the native light profiles; normal/_oc/_sp/_spm are the authoritative surface maps, while nml/_msk are optional variants and chr_toon in_texGrd alpha rows are approximated from the captured variant.",
            ),
        )


def characterize_current_toon_shader(material=None, variants=None) -> CharacterShaderProfile:
    """Return the shared profile used to annotate the legacy implementation."""

    return CharacterShaderProfile()


def annotate_current_toon_material(material, variants=None) -> None:
    """Attach non-invasive metadata to a material built by the old shader path."""

    if material is None:
        return
    profile = characterize_current_toon_shader(material, variants)
    try:
        material["g4_shader_family"] = profile.family
        material["g4_shader_profile"] = profile.name
        material["g4_shader_fidelity"] = profile.fidelity.value
        material["g4_shader_preserved"] = True
    except (AttributeError, TypeError):
        return


__all__ = [
    "CharacterShaderProfile",
    "annotate_current_toon_material",
    "characterize_current_toon_shader",
]
