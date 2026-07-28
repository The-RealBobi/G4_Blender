import ast
import unittest
from pathlib import Path


class PortAddonDefaultsTests(unittest.TestCase):
    def test_bake_current_pose_defaults_to_enabled(self):
        source = Path(__file__).parents[1] / "g4_port_addon.py"
        module = ast.parse(source.read_text(encoding="utf-8"))
        annotation = next(
            node
            for node in ast.walk(module)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "bake_current_pose"
            and isinstance(node.annotation, ast.Call)
        )
        keywords = {keyword.arg: keyword.value for keyword in annotation.annotation.keywords}
        self.assertIsInstance(keywords["default"], ast.Constant)
        self.assertTrue(keywords["default"].value)


if __name__ == "__main__":
    unittest.main()
