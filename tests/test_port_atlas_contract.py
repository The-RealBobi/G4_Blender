import ast
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "g4_port_addon.py"


class AtlasContractTests(unittest.TestCase):
    def test_addon_exposes_prepared_atlas_helpers(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertTrue(
            {"first_used_material_image", "atlas_status_rows", "atlas_signature"} <= functions,
            "The texture workflow needs deterministic source selection and prepared-atlas status.",
        )

    def test_addon_exposes_explicit_prepare_operator(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('bl_idname = "level5_g4_port.prepare_atlas"', source)

    def test_atlas_summary_reads_the_written_image_size(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("atlas_size = load_image_pixels(str(path))", source)

    def test_unreadable_source_never_becomes_a_generated_atlas(self):
        source = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        builder = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_texture_spritesheet")
        body = ast.unparse(builder)
        self.assertIn("unreadable source image", body)
        self.assertLess(body.index("unreadable source image"), body.index("groups = []"))
        self.assertIn("def discard_generated_atlas", source)

    def test_native_face_records_do_not_export_object_uv_tiles(self):
        source = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        settings = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "G4PortSceneSettings")
        to_config = next(node for node in settings.body if isinstance(node, ast.FunctionDef) and node.name == "to_config")
        body = ast.unparse(to_config)
        self.assertIn("record.texture_key in active_texture_keys", body)
        self.assertIn("not is_face_atlas_record(record)", body)

    def test_only_actual_face_records_reserve_the_shared_base_texture(self):
        source = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        settings = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "G4PortSceneSettings")
        texture_map = next(node for node in settings.body if isinstance(node, ast.FunctionDef) and node.name == "texture_map")
        body = ast.unparse(texture_map)
        self.assertIn("face_texture_is_shared(self.records, texture_names)", body)

    def test_port_defaults_to_generated_tangents(self):
        source = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        settings = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "G4PortSceneSettings")
        tangents = next(node for node in settings.body if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "generate_tangents")
        self.assertIn("default=True", ast.unparse(tangents.annotation))

    def test_file_export_opens_the_full_intermediate_dialog(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        operator = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "EXPORT_OT_level5_g4_port")
        invoke = next(node for node in operator.body if isinstance(node, ast.FunctionDef) and node.name == "invoke")
        invoked = ast.unparse(invoke)
        self.assertIn("context.window_manager.invoke_props_dialog(self, width=760)", invoked)

    def test_intermediate_dialog_includes_sidebar_actions(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        operator = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "EXPORT_OT_level5_g4_port")
        draw = next(node for node in operator.body if isinstance(node, ast.FunctionDef) and node.name == "draw")
        self.assertIn("include_actions=True", ast.unparse(draw))

    def test_custom_atlas_keeps_its_uv_export_path(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "has_unchanged_native_roundtrip")
        body = ast.unparse(function)
        self.assertIn("props.generate_png_set_on_export", body)
        self.assertIn("props.use_source_uv_transforms", body)
        self.assertIn("props.auto_pack_source_uvs", body)

    def test_custom_export_only_regenerates_atlases_when_requested(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        settings = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "G4PortSceneSettings")
        auto_prepare = next(
            node for node in settings.body
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "generate_png_set_on_export"
        )
        self.assertIn("default=False", ast.unparse(auto_prepare.annotation))

    def test_default_texture_export_preserves_authored_uv_layout(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        builder = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_texture_spritesheet")
        body = ast.unparse(builder)
        self.assertIn("if not props.auto_pack_source_uvs", body)
        self.assertIn("preserved original UV layout", body)
        self.assertIn("props.use_source_uv_transforms = False", body)

    def test_prepared_atlas_requires_a_ready_scene_assignment(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        settings = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "G4PortSceneSettings")
        texture_map = next(node for node in settings.body if isinstance(node, ast.FunctionDef) and node.name == "texture_map")
        self.assertIn("atlas_states.get(item.texture_name) != 'ready'", ast.unparse(texture_map))

    def test_atlas_cells_are_stable_and_filter_safe(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("key=lambda item: item.name.casefold()", source)
        self.assertIn("cell_y = (rows - 1 - row) * cell_height", source)
        self.assertIn("cell_y = (FACE_ATLAS_ROWS - 1 - row) * cell_height", source)
        self.assertIn("ATLAS_GUTTER_PIXELS", source)
        self.assertIn("def bleed_transparent_pixels", source)



if __name__ == "__main__":
    unittest.main()
