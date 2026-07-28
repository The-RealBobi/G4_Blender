import ast
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "g4_port_addon.py"


class PoseExportContextTests(unittest.TestCase):
    def test_pose_export_does_not_use_context_bound_object_operators(self):
        """The file browser is not a valid context for object select/apply ops."""
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        exporter = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "export_collada"
        )
        body = ast.unparse(exporter)
        self.assertNotIn("bpy.ops.object.select_all", body)
        self.assertNotIn("bpy.ops.object.modifier_apply", body)
        self.assertIn("bpy.data.meshes.new_from_object", body)

    def test_pose_export_cleans_its_private_collection_on_every_exit(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("def remove_pose_export_collection", source)
        self.assertIn('remove_pose_export_collection(bpy.data.collections.get("__G4PoseExport"))', source)

    def test_successful_export_collapses_the_export_section(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        operator = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "EXPORT_OT_level5_g4_port"
        )
        execute = next(node for node in operator.body if isinstance(node, ast.FunctionDef) and node.name == "execute")
        self.assertIn("settings(context).show_export = False", ast.unparse(execute))


if __name__ == "__main__":
    unittest.main()
