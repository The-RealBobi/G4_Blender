import unittest

import g4_model_probe


class TargetSkeletonOverrideTests(unittest.TestCase):
    def test_missing_override_stays_absent_across_repeated_lookups(self):
        previous_path = g4_model_probe.TARGET_SKELETON_OVERRIDE
        previous_data = g4_model_probe.TARGET_SKELETON_DATA
        try:
            g4_model_probe.TARGET_SKELETON_OVERRIDE = None
            g4_model_probe.TARGET_SKELETON_DATA = None

            self.assertIsNone(g4_model_probe.load_target_skeleton_override())
            self.assertIsNone(g4_model_probe.load_target_skeleton_override())
        finally:
            g4_model_probe.TARGET_SKELETON_OVERRIDE = previous_path
            g4_model_probe.TARGET_SKELETON_DATA = previous_data


if __name__ == "__main__":
    unittest.main()
