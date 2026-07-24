import importlib.util
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


if __name__ == "__main__":
    unittest.main()
