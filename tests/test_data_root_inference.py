import importlib.util
import os
import sys
import unittest
from pathlib import Path


probe_path = Path(__file__).resolve().parents[1] / "g4_model_probe.py"
spec = importlib.util.spec_from_file_location("g4_model_probe_root_test", probe_path)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


class DataRootInferenceTests(unittest.TestCase):
    def test_versioned_data_folder_is_raw_root(self):
        model = Path("/tmp/data.7.1.1/common/chr/_face/01_IE1/c01001900/c01001900.g4md")
        self.assertEqual(probe.infer_raw_data_root(model), Path("/tmp/data.7.1.1").resolve())

    def test_versioned_face_model_gets_lookup_relative_path(self):
        old_root = probe.RAW_DATA_ROOT
        try:
            model = Path("/tmp/data.7.1.1/common/chr/_face/01_IE1/c01001900/c01001900.g4md")
            probe.RAW_DATA_ROOT = Path("/tmp/data.7.1.1").resolve()
            self.assertIn(
                "_face/01_IE1/c01001900/c01001900.g4md",
                probe.model_relpath_candidates(model),
            )
        finally:
            probe.RAW_DATA_ROOT = old_root

    def test_configured_steamapps_common_is_refined_to_versioned_data_root(self):
        old_root = probe.RAW_DATA_ROOT
        old_env = os.environ.get("LEVEL5_G4_RAW_ROOT")
        try:
            configured = Path("/tmp/SteamLibrary/steamapps/common")
            model = configured / "INAZUMA ELEVEN Victory Road/data.7.1.1/common/chr/_face/01_IE1/c01001900/c01001900.g4md"
            os.environ["LEVEL5_G4_RAW_ROOT"] = str(configured)
            probe.RAW_DATA_ROOT = configured
            probe.configure_raw_data_root_from_path(model)
            self.assertEqual(
                probe.RAW_DATA_ROOT,
                (configured / "INAZUMA ELEVEN Victory Road/data.7.1.1").resolve(),
            )
        finally:
            if old_env is None:
                os.environ.pop("LEVEL5_G4_RAW_ROOT", None)
            else:
                os.environ["LEVEL5_G4_RAW_ROOT"] = old_env
            probe.RAW_DATA_ROOT = old_root


if __name__ == "__main__":
    unittest.main()
