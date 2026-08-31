from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from effects.material_animation import EffectMaterialState, apply_g4ma_channel
from formats.contracts import FidelityState
from formats.evidence import discover_format_evidence
from formats.g4ma import build_g4ma_effect_bindings
from formats.g4mt import G4MTAnimationBank, G4MTChannel, G4MTHeader, G4MTTarget, G4MTTargetInfo, crc32b
from shading.character_profile import characterize_current_toon_shader
from shading.character_textures import (
    CHARACTER_RAMP_BANDS,
    character_data_roots,
    character_ramp_band_uv,
    character_shader_texture_containers,
    character_texture_base_key,
    character_texture_role,
    is_global_character_ramp_name,
)
from shading.map_surfaces import MapSurfaceKind, classify_map_surface


class NativeFoundationTests(unittest.TestCase):
    def test_format_evidence_requires_real_extensions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "hidden").mkdir()
            (root / "hidden" / "effect.g4ma").write_bytes(b"G4MA")
            evidence = discover_format_evidence("g4ma", (root,))
            self.assertEqual(evidence.status, "confirmed-file")
            self.assertEqual(evidence.count, 1)
            self.assertEqual(evidence.samples[0].identity.extension, ".g4ma")

    def test_missing_extension_is_executable_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = discover_format_evidence("g4cm", (temporary,))
            self.assertEqual(evidence.status, "executable-only")
            self.assertEqual(evidence.count, 0)

    def test_g4ma_channel_mapping_does_not_guess_unknown_channels(self):
        bank = G4MTAnimationBank(
            G4MTHeader(0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            (), (), (G4MTTarget(0, crc32b("effect_material")),),
            (G4MTTargetInfo(0, 0, 0, 2, 0),),
            (G4MTChannel(0, 19, 0, 0, 0, 1, 1, 0, 0, 0, 1, (0,)),
             G4MTChannel(1, 255, 0, 0, 0, 1, 1, 0, 0, 0, 1, (0,))),
        )
        bindings = build_g4ma_effect_bindings(bank, ("effect_material",), {19: "color"})
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].fidelity, FidelityState.APPROXIMATE)

    def test_effect_material_state_is_immutable_and_deterministic(self):
        state = apply_g4ma_channel(EffectMaterialState(), 19, (1, 0.5, 0.25, 1))
        self.assertEqual(state.get("color"), (1.0, 0.5, 0.25, 1.0))
        self.assertEqual(apply_g4ma_channel(state, 254, (1,)).values, state.values)

    def test_map_profiles_are_separate_from_character_shader(self):
        water = classify_map_surface("map_pbrriver_water")
        grass = classify_map_surface("map_pbrgrass_cutout")
        character = characterize_current_toon_shader()
        self.assertEqual(water.kind, MapSurfaceKind.WATER)
        self.assertEqual(grass.kind, MapSurfaceKind.GRASS)
        self.assertEqual(character.family, "character_toon")
        self.assertTrue(character.supports_dxt5nm)

    def test_character_profile_separates_material_ramp_from_scene_gradient(self):
        contract = characterize_current_toon_shader().as_contract()
        ramp = contract.parameter("toon_ramp")
        gradient = contract.parameter("scene_gradient")
        self.assertIsNotNone(ramp)
        self.assertIsNotNone(gradient)
        self.assertIn("diagnostic", ramp.native_source)
        self.assertEqual(gradient.native_source, "DXBC in_texGrd texture2dms scene framebuffer")
        self.assertFalse(gradient.supported)

    def test_character_texture_contract_keeps_optional_toon_ramp_separate(self):
        self.assertEqual(character_texture_role("c06030110_20sp"), "specular")
        self.assertEqual(character_texture_role("c06030110_20spm"), "specular_mask")
        self.assertEqual(character_texture_role("c06030110_20dp"), "ramp")
        self.assertEqual(character_texture_role("c06030110_20_ramp.dds"), "ramp")
        self.assertEqual(character_texture_base_key("c06030110_20dp"), "c06030110_20")
        self.assertEqual(character_texture_role("chrGrd_01.dds"), "ramp")
        self.assertTrue(is_global_character_ramp_name("chrGrd_01.dds"))

    def test_character_ramp_candidates_use_lower_pale_and_lilac_bands(self):
        self.assertEqual(CHARACTER_RAMP_BANDS["main"], (240, 255))
        self.assertEqual(CHARACTER_RAMP_BANDS["occlusion_depth"], (224, 231))
        self.assertAlmostEqual(character_ramp_band_uv("main"), 0.03125)
        self.assertAlmostEqual(character_ramp_band_uv("occlusion_depth"), 0.109375)

    def test_shared_character_shader_container_accepts_raw_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "raw"
            container = raw / "data" / "dx11" / "chr" / "shader" / "texture" / "chr_tex.g4tx"
            container.parent.mkdir(parents=True)
            container.write_bytes(b"G4TX")
            self.assertEqual(character_data_roots(raw), [(raw / "data").resolve()])
            self.assertEqual(character_shader_texture_containers(raw), [container.resolve()])

    def test_real_g4ma_fixture_is_parsed_when_dump_is_available(self):
        root = Path(os.environ.get("G4_DUMP_ROOT", "/Volumes/BOBI/Proyectos Personales/VictoryRoad/DUMP_712/._work/raw"))
        candidates = sorted(root.rglob("*.g4ma")) if root.exists() else []
        if not candidates:
            self.skipTest("no real G4MA fixture is available")
        from formats.g4ma import parse_g4ma_file

        bank = parse_g4ma_file(candidates[0])
        self.assertGreaterEqual(bank.header.target_count, 0)
        self.assertEqual(bank.header.target_count, len(bank.targets))


if __name__ == "__main__":
    unittest.main()
