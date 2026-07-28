import importlib.util
import sys
import unittest
from pathlib import Path


probe_path = Path(__file__).resolve().parents[1] / "g4_model_probe.py"
spec = importlib.util.spec_from_file_location("g4_model_probe_palette_test", probe_path)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


class UniformPaletteTests(unittest.TestCase):
    def test_shared_uniform_palette_is_one_based(self):
        names = list(probe.ASSIGNED_SKELETON_JOINT_NAMES)
        skeleton = {"names": names}
        remapped, changed = probe.remap_assigned_joint_palette([3, 4, 5, 86, 87, 88], skeleton)
        self.assertEqual(
            [names[index] for index in remapped],
            ["c_c_1_0", "c_c_1_1", "l_s_1_0", "l_foot_1_0", "l_foot_1_1", "l_pnt_1_0"],
        )
        self.assertGreater(changed, 0)

    def test_shoe_points_use_foot_weight_helpers(self):
        names = [
            "l_foot_1_1",
            "l_foot_1_1_wgt_1_0",
            "l_pnt_1_0",
            "r_foot_1_1",
            "r_foot_1_1_wgt_1_0",
            "r_pnt_1_0",
        ]
        remapped, changed = probe.remap_shoe_point_helpers([2, 5], {"names": names})
        self.assertEqual(remapped, [1, 4])
        self.assertEqual(changed, 2)


if __name__ == "__main__":
    unittest.main()
