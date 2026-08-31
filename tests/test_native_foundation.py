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
