import importlib.util
import struct
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "g4_port.py"
SPEC = importlib.util.spec_from_file_location("g4_port_skinning_safety_test", SOURCE)
PORT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PORT
SPEC.loader.exec_module(PORT)


class PortSkinningSafetyTests(unittest.TestCase):
    def test_native_joint_table_overrides_shared_compact_indices(self):
        md = bytearray(0x100)
        struct.pack_into("<H", md, 0x74, 0x08)
        struct.pack_into("<H", md, 0x82, 0x18)
        struct.pack_into("<I", md, 0x28, PORT.crc32b("c_global_0_0"))
        struct.pack_into("<I", md, 0x2C, PORT.crc32b("c_c_1_0"))
        indices = PORT.native_joint_name_indices(bytes(md))
        self.assertEqual(indices["c_global_0_0"], 2)
        self.assertEqual(indices["c_c_1_0"], 3)
        self.assertEqual(PORT.compact_joint_index("c_global_0_0", native_joint_indices=indices), 2)

    def test_uniform_template_resolves_aliases_through_its_own_joint_table(self):
        template = Path(
            "/Volumes/BOBI/Proyectos Personales/VictoryRoad/DUMP_702/._work/raw/data/"
            "common/chr/_uniform/u11010060/u11010060.g4md"
        )
        if not template.is_file():
            self.skipTest("requires the local Victory Road dump")
        indices = PORT.native_joint_name_indices(template.read_bytes())
        self.assertEqual(indices["c_global_0_0"], 2)
        self.assertEqual(indices["c_c_1_1"], 4)
        self.assertEqual(PORT.compact_joint_index("root", native_joint_indices=indices), 2)
        self.assertEqual(PORT.compact_joint_index("spine02", native_joint_indices=indices), 4)

    def test_compact_joint_index_uses_the_editable_alias_catalog(self):
        self.assertEqual(PORT.compact_joint_index("spine01"), PORT.COMPACT_JOINT_NAMES.index("c_c_1_1"))
        self.assertEqual(PORT.compact_joint_index("r_index03"), PORT.COMPACT_JOINT_NAMES.index("r_idx_1_2"))

    def test_native_palette_uses_skinning_ancestor_for_helper_joints(self):
        indices = {
            "c_global_0_0": 2,
            "c_c_1_0": 3,
            "l_w_1_0": 8,
            "l_wph_1_0": 9,
            "l_foot_1_0": 86,
            "l_foot_1_1": 87,
        }
        native_palette = [3, 8, 86]
        self.assertEqual(PORT.palette_compatible_joint(2, native_palette, indices), 3)
        self.assertEqual(PORT.palette_compatible_joint(9, native_palette, indices), 8)
        self.assertEqual(PORT.palette_compatible_joint(87, native_palette, indices), 86)

    def test_root_alias_uses_the_native_pelvis_entry_when_global_is_not_in_the_palette(self):
        indices = {"c_global_0_0": 2, "c_c_1_0": 3}
        vertex = PORT.Vertex(
            (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0), (("root", 1.0),)
        )
        resolved, unresolved = PORT.resolve_vertex_influences(vertex, [3], None, {}, indices)
        self.assertEqual((resolved, unresolved), ([(0, 1.0)], 0))

    def test_auto_palette_does_not_expand_for_known_native_helper_ancestors(self):
        indices = {"c_global_0_0": 2, "c_c_1_0": 3, "l_w_1_0": 8, "l_wph_1_0": 9}
        vertex = PORT.Vertex(
            (0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0),
            (("root", 0.5), ("l_hand", 0.5)),
        )
        mesh = PORT.Mesh("body", [vertex], [0, 0, 0], 0, "body")
        rule = PORT.RecordRule("body", "body", ["body"], auto_palette=True)
        self.assertEqual(
            PORT.configured_record_palettes([[3, 8]], [rule], [mesh], {"l_hand": "l_wph_1_0"}, indices),
            [[3, 8]],
        )

    def test_finger_weights_are_bound_to_the_wrist_by_default(self):
        indices = {"l_w_1_0": 8, "l_idx_1_0": 9}
        vertex = PORT.Vertex(
            (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0), (("left_index_01", 1.0),)
        )
        resolved, unresolved = PORT.resolve_vertex_influences(
            vertex,
            [8],
            None,
            {"left_index_01": "l_idx_1_0"},
            indices,
        )
        self.assertEqual((resolved, unresolved), ([(0, 1.0)], 0))

    def test_finger_stabilization_collapses_native_finger_weights_to_the_wrist(self):
        indices = {"l_w_1_0": 8, "l_idx_1_0": 10, "l_idx_1_1": 11}
        vertex = PORT.Vertex(
            (0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0),
            (("l_index01", 0.4), ("l_index02", 0.6)),
        )
        resolved, unresolved = PORT.resolve_vertex_influences(
            vertex,
            [8, 10, 11],
            None,
            {"l_index01": "l_idx_1_0", "l_index02": "l_idx_1_1"},
            indices,
            stabilize_finger_weights=True,
        )
        self.assertEqual((resolved, unresolved), ([(0, 1.0)], 0))

    def test_joint_position_offsets_move_only_the_weighted_arm_vertices(self):
        arm = PORT.Vertex((1.0, 2.0, 3.0), (0.0, 1.0, 0.0), (0.0, 0.0), (("l_arm", 1.0),))
        torso = PORT.Vertex((4.0, 5.0, 6.0), (0.0, 1.0, 0.0), (0.0, 0.0), (("c_c_1_1", 1.0),))
        mesh = PORT.Mesh("body", [arm, torso, torso], [0, 1, 2], 0, "body")
        payload, _ = PORT.build_g4mg(
            [mesh],
            palettes=[[6, 4]],
            native_joint_indices={"l_a_1_0": 6, "c_c_1_1": 4},
            joint_aliases={"l_arm": "l_a_1_0"},
            joint_position_offsets={"l_a_1_0": (0.5, -0.25, 0.125)},
        )
        self.assertEqual(struct.unpack_from("<3f", payload, 0), (1.5, 1.75, 3.125))
        self.assertEqual(struct.unpack_from("<3f", payload, 0x44), (4.0, 5.0, 6.0))

    def test_joint_position_transforms_follow_only_the_weighted_arm_vertices(self):
        arm = PORT.Vertex((1.0, 2.0, 3.0), (0.0, 1.0, 0.0), (0.0, 0.0), (("l_arm", 1.0),))
        torso = PORT.Vertex((4.0, 5.0, 6.0), (0.0, 1.0, 0.0), (0.0, 0.0), (("c_c_1_1", 1.0),))
        mesh = PORT.Mesh("body", [arm, torso, torso], [0, 1, 2], 0, "body")
        payload, _ = PORT.build_g4mg(
            [mesh],
            palettes=[[6, 4]],
            native_joint_indices={"l_a_1_0": 6, "c_c_1_1": 4},
            joint_aliases={"l_arm": "l_a_1_0"},
            joint_position_transforms={
                "l_a_1_0": (1.0, 0.0, 0.0, 0.5, 0.0, 1.0, 0.0, -0.25, 0.0, 0.0, 1.0, 0.125, 0.0, 0.0, 0.0, 1.0),
            },
        )
        self.assertEqual(struct.unpack_from("<3f", payload, 0), (1.5, 1.75, 3.125))
        self.assertEqual(struct.unpack_from("<3f", payload, 0x44), (4.0, 5.0, 6.0))

    def test_analyze_port_reports_unresolved_influences_at_top_level(self):
        config = PORT.PortConfig(Path("chr/test/test.g4md"), ["mat"], [PORT.RecordRule("native", "mat", ["*"])], {})
        original_prepare = PORT.prepare_port_geometry
        try:
            PORT.prepare_port_geometry = lambda *args, **kwargs: ([], [], [[0]], [[0]], [{"unresolved_influences": 3}])
            report = PORT.analyze_port(None, Path("/tmp/raw"), config, chara_model=Path("/tmp/no-chara-model.xml"))
            self.assertEqual(report["unresolved_influences"], 3)
        finally:
            PORT.prepare_port_geometry = original_prepare

    def test_write_port_rejects_unresolved_influences_even_when_not_strict(self):
        vertex = PORT.Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0))
        mesh = PORT.Mesh("native", [vertex, vertex, vertex], [0, 1, 2], 0, "mat")
        config = PORT.PortConfig(Path("chr/test/test.g4md"), ["mat"], [PORT.RecordRule("native", "mat", ["*"])], {}, strict_skinning=False)
        original_prepare = PORT.prepare_port_geometry
        original_build = PORT.build_g4mg
        try:
            PORT.prepare_port_geometry = lambda *args, **kwargs: ([mesh], [mesh], [[0]], [[0]], [{"unresolved_influences": 1}])
            PORT.build_g4mg = lambda *args, **kwargs: (b"", [{"unresolved_influences": 1}])
            with self.assertRaisesRegex(ValueError, "1 skin influences"):
                PORT.write_port(None, Path("/tmp/raw"), Path("/tmp/out"), config)
        finally:
            PORT.prepare_port_geometry = original_prepare
            PORT.build_g4mg = original_build

    def test_manual_source_mapping_overrides_legacy_wildcard_matching(self):
        vertex = PORT.Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0))
        body = PORT.Mesh("body_mesh", [vertex, vertex, vertex], [0, 1, 2], 0, "body")
        hair = PORT.Mesh("hair_mesh", [vertex, vertex, vertex], [0, 1, 2], 0, "hair")
        config = PORT.PortConfig(
            Path("chr/test/test.g4md"), ["mat"], [
                PORT.RecordRule("body", "mat", ["body_mesh"]),
                PORT.RecordRule("hair", "mat", ["*"]),
            ], {}, source_mesh_assignments={"hair mesh": "body"},
        )
        merged = PORT.merged_native_meshes([body, hair], config)
        self.assertEqual(merged[0].source_names, ("body_mesh", "hair_mesh"))
        self.assertEqual(merged[1].source_names, ())

    def test_manual_source_mapping_accepts_blender_duplicate_suffixes(self):
        vertex = PORT.Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0))
        body = PORT.Mesh("body_mesh_007", [vertex, vertex, vertex], [0, 1, 2], 0, "body")
        hand = PORT.Mesh("hand_mesh_004", [vertex, vertex, vertex], [0, 1, 2], 0, "hand")
        config = PORT.PortConfig(
            Path("chr/test/test.g4md"),
            ["mat"],
            [PORT.RecordRule("body", "mat", [])],
            {},
            source_mesh_assignments={"body_mesh": "body", "hand_mesh": "body"},
        )

        merged = PORT.merged_native_meshes([body, hand], config)

        self.assertEqual(merged[0].source_names, ("body_mesh_007", "hand_mesh_004"))

    def test_unassigned_source_meshes_are_omitted_from_native_output(self):
        vertex = PORT.Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0))
        body = PORT.Mesh("body_mesh", [vertex, vertex, vertex], [0, 1, 2], 0, "body")
        hand = PORT.Mesh("hand_mesh", [vertex, vertex, vertex], [0, 1, 2], 0, "hand")
        config = PORT.PortConfig(
            Path("chr/test/test.g4md"), ["mat"], [PORT.RecordRule("body", "mat", ["body_mesh"])], {}
        )

        merged = PORT.merged_native_meshes([body, hand], config)

        self.assertEqual(merged[0].source_names, ("body_mesh",))

    def test_vertex_color_fallback_preserves_the_native_material_contract(self):
        vertex = PORT.Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0))
        packed = PORT.pack_vertex(vertex, fallback_color=(255, 0, 191, 127))
        self.assertEqual(tuple(packed[0x3C:0x40]), (255, 0, 191, 127))

    def test_source_vertex_color_overrides_the_native_fallback(self):
        vertex = PORT.Vertex(
            (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0), color=(12, 34, 56, 78)
        )
        packed = PORT.pack_vertex(vertex, fallback_color=(255, 0, 191, 127))
        self.assertEqual(tuple(packed[0x3C:0x40]), (12, 34, 56, 78))

    def test_g4mg_report_identifies_source_and_fallback_vertex_colors(self):
        vertex = PORT.Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0))
        mesh = PORT.Mesh("native", [vertex, vertex, vertex], [0, 1, 2], 0, "mat")
        _, records = PORT.build_g4mg([mesh], fallback_colors=[(255, 0, 191, 127)])
        self.assertEqual(records[0]["source_color_vertices"], 0)
        self.assertEqual(records[0]["fallback_color"], [255, 0, 191, 127])

    def test_g4mg_index_buffer_uses_the_native_0x40_alignment(self):
        vertex = PORT.Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0))
        mesh = PORT.Mesh("native", [vertex, vertex, vertex], [0, 1, 2], 0, "mat")

        payload, records = PORT.build_g4mg([mesh])

        self.assertEqual(records[0]["index_base"] % 0x40, 0)
        self.assertEqual(len(payload) % 0x40, 0)

    def test_realign_g4mg_repairs_a_legacy_index_boundary_without_changing_indices(self):
        vertex = PORT.Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0))
        payload, records = PORT.build_g4mg([PORT.Mesh("native", [vertex], [0, 0, 0], 0, "mat")])
        vertex_size = records[0]["vertex_buffer_size"]
        index_data = payload[records[0]["index_base"]:records[0]["index_base"] + 6]
        legacy_payload = bytearray(payload[:vertex_size])
        legacy_payload.extend(index_data)
        while len(legacy_payload) % 0x10:
            legacy_payload.append(0)
        legacy_md = bytearray(0x60)
        struct.pack_into("<I", legacy_md, 0x50, vertex_size)
        struct.pack_into("<I", legacy_md, 0x54, len(index_data))
        struct.pack_into("<I", legacy_md, 0x5C, vertex_size)

        repaired_md, repaired_payload = PORT.realign_g4mg_index_buffer(bytes(legacy_md), bytes(legacy_payload))

        repaired_index_base = struct.unpack_from("<I", repaired_md, 0x5C)[0]
        self.assertEqual(repaired_index_base % 0x40, 0)
        self.assertEqual(len(repaired_payload) % 0x40, 0)
        self.assertEqual(repaired_payload[repaired_index_base:repaired_index_base + 6], index_data)

    def test_native_vertex_color_transfer_preserves_authored_source_colors(self):
        generated = PORT.Mesh(
            "native",
            [
                PORT.Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0)),
                PORT.Vertex((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 0.0), color=(1, 2, 3, 4), color_from_source=True),
            ],
            [0, 1, 1],
            0,
            "mat",
        )
        PORT.transfer_native_vertex_colors(
            [generated],
            [[((0.0, 0.0, 0.0), (255, 0, 191, 127)), ((1.0, 0.0, 0.0), (0, 255, 191, 127))]],
            [(255, 0, 191, 127)],
        )
        self.assertEqual(generated.vertices[0].color, (255, 0, 191, 127))
        self.assertEqual(generated.vertices[1].color, (1, 2, 3, 4))

    def test_unmodified_g4tx_is_rebuilt_with_native_payloads(self):
        template = Path(
            "/Volumes/BOBI/Proyectos Personales/VictoryRoad/DUMP_702/._work/raw/data/"
            "dx11/chr/_uniform/u11010060/u11010060.g4tx"
        )
        if not template.is_file():
            self.skipTest("requires the local Victory Road dump")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "u11010060.g4tx"
            PORT.rebuild_native_g4tx_with_custom_textures(template, Path(temporary), output, {})
            _, template_entries, template_payloads = PORT.parse_g4tx_payloads(template)
            _, output_entries, output_payloads = PORT.parse_g4tx_payloads(output)
            self.assertEqual([entry["name"] for entry in output_entries], [entry["name"] for entry in template_entries])
            self.assertEqual(output_payloads, template_payloads)
            self.assertEqual(output.read_bytes(), template.read_bytes())

    def test_replacing_a_native_payload_preserves_the_native_g4tx_tables(self):
        template = Path(
            "/Volumes/BOBI/Proyectos Personales/VictoryRoad/DUMP_702/._work/raw/data/"
            "dx11/chr/_uniform/u11010060/u11010060.g4tx"
        )
        if not template.is_file():
            self.skipTest("requires the local Victory Road dump")
        _, _, payloads = PORT.parse_g4tx_payloads(template)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            replacement = directory / "u11010060_10.dds"
            replacement.write_bytes(payloads["u11010060_10"])
            output = directory / "u11010060.g4tx"
            PORT.rebuild_native_g4tx_with_custom_textures(
                template, directory, output, {"u11010060_10": replacement.name}
            )
            _, _, output_payloads = PORT.parse_g4tx_payloads(output)
            self.assertEqual(output_payloads["u11010060_10"], payloads["u11010060_10"])


if __name__ == "__main__":
    unittest.main()
