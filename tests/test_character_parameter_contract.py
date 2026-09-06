import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from shading.character_profile import CharacterShaderProfile


SOURCE = Path(__file__).resolve().parents[1] / "__init__.py"


class CharacterParameterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def function_source(self, name):
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(self.source, node)
        self.fail(f"{name} function is missing")

    def test_character_modifier_decision_is_not_limited_to_toon_materials(self):
        helper = self.function_source("object_needs_character_parameter_modifier")
        self.assertIn("g4_character_model_source", helper)
        self.assertIn("g4_character_part_source", helper)
        self.assertIn("g4_native_model_source", helper)

        configure = self.function_source("configure_character_parameter_modifiers")
        self.assertIn("object_needs_character_parameter_modifier", configure)

    def test_character_texture_slots_are_exposed_from_modifier_context(self):
        self.assertIn("CHARACTER_TEXTURE_NODE_SLOTS", self.source)
        for node_name in (
            "G4 Base",
            "G4 Recolor Mask",
            "G4 Normal",
            "G4 Occlusion",
            "G4 Line Parameter",
            "G4 Specular Mask",
            "G4 Specular Shape",
            "G4 Alpha Mask",
        ):
            self.assertIn(node_name, self.source)
        self.assertIn("OBJECT_PT_level5_character_textures", self.source)

    def test_character_textures_can_be_loaded_from_g4tx_in_modifier_context(self):
        self.assertIn("assign_character_textures_from_g4tx", self.source)
        self.assertIn("extract_character_textures_from_g4tx", self.source)
        self.assertIn("OBJECT_OT_level5_load_character_g4tx", self.source)
        self.assertIn("object.level5_load_character_g4tx", self.source)
        self.assertIn("extract_g4tx", self.source)

        panel = self.source[
            self.source.index("class OBJECT_PT_level5_character_textures"):
            self.source.index("class MATERIAL_PT_level5_character")
        ]
        self.assertIn("OBJECT_OT_level5_load_character_g4tx.bl_idname", panel)

        classes = self.source[self.source.index("classes = ["):]
        self.assertIn("OBJECT_OT_level5_load_character_g4tx", classes)

    def test_character_toon_keeps_native_lighting_controls(self):
        shader = self.function_source("apply_level5_toon_shader") + (SOURCE.parent / "shading/character_lighting.py").read_text()
        for node_name in (
            "G4 Character Ambient",
            "G4 Shadow Color {index}",
            "G4 Dual Toon Ramp",
            "G4 Highlight",
            "G4 Under Light",
        ):
            self.assertIn(node_name, shader)
        self.assertIn("G4 Highlight Threshold", shader)
        self.assertIn("G4 Shadow Threshold", shader)

    def test_edge2_offsets_in_model_space_before_camera_transform(self):
        group = self.function_source("edge2_preview_node_group")
        self.assertIn(
            'links.new(geometry, set_position.inputs["Geometry"])',
            group,
        )
        self.assertIn(
            'links.new(set_position.outputs["Geometry"], camera_transform.inputs["Geometry"])',
            group,
        )
        self.assertNotIn(
            'links.new(camera_transform.outputs["Geometry"], set_position.inputs["Geometry"])',
            group,
        )
        self.assertIn('camera_transform.inputs.get("Transform")', group)
        self.assertIn('restore_transform.inputs.get("Transform")', group)

    def test_internal_detail_marks_support_blender_5_generic_attributes(self):
        helper = self.function_source("mark_level5_internal_edges")
        self.assertIn('mesh.attributes.get("freestyle_edge")', helper)
        self.assertIn('mesh.attributes.new(', helper)
        self.assertIn('domain="EDGE"', helper)
        self.assertIn("edge.use_freestyle_mark = value", helper)

    def test_character_profile_names_authoritative_texture_roles(self):
        names = {
            parameter.name
            for parameter in CharacterShaderProfile().as_contract().parameters
        }
        self.assertTrue(
            {
                "normal_dxt5nm",
                "occlusion",
                "specular_shape",
                "specular_mask",
            }.issubset(names)
        )
        self.assertIn("normal_rgb_nml", names)
        self.assertIn("mask_rgb_msk", names)

        operator = self.source[
            self.source.index("class OBJECT_OT_level5_load_character_g4tx"):
            self.source.index("class OBJECT_PT_level5_character_textures")
        ]
        self.assertNotIn('return {"CANCELLED"}\n        self.report({"WARNING"}, f"No matching Character texture slots found', operator)
        self.assertIn('return {"FINISHED"}', operator)

    def test_scene_gradient_metadata_matches_dxbc_texture_dimension(self):
        self.assertIn("in_texGrd: chrGrd_01 UNORM alpha rows", self.source)
        self.assertNotIn("in_texGrd: texture2dms", self.source)


if __name__ == "__main__":
    unittest.main()
