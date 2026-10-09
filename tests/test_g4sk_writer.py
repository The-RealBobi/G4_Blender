import hashlib
import os
import struct
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from formats.g4sk_writer import G4SKNode, write_g4sk_v104
from g4_model_probe import parse_g4sk


DUMP_ROOT = os.environ.get("G4SK_TEST_DUMP")


def read_nodes(path: Path) -> tuple[bytes, list[G4SKNode]]:
    data = path.read_bytes()
    count = struct.unpack_from("<H", data, 0x20)[0]
    offsets = [0x40 + struct.unpack_from("<H", data, 0x24 + index * 2)[0] * 4 for index in range(11)]
    nodes = []
    for index in range(count):
        relative_name = struct.unpack_from("<H", data, offsets[7] + index * 2)[0]
        name_start = offsets[7] + relative_name
        name_end = data.index(0, name_start)
        name = data[name_start:name_end].decode("ascii")
        raw_parent = struct.unpack_from("<H", data, offsets[3] + index * 2)[0]
        nodes.append(G4SKNode(
            name=name,
            parent_index=None if raw_parent == count else raw_parent,
            local_srt=struct.unpack_from("<12f", data, offsets[1] + index * 0x30),
            global_matrix=struct.unpack_from("<12f", data, 0x40 + index * 0x30),
            inverse_bind_matrix=struct.unpack_from("<12f", data, offsets[0] + index * 0x30),
            flags=data[offsets[5] + index],
            bounds_radius=struct.unpack_from("<f", data, offsets[6] + index * 4)[0],
        ))
    return data, nodes


@unittest.skipUnless(DUMP_ROOT, "set G4SK_TEST_DUMP to the authorized extracted raw data")
class G4SKWriterTests(unittest.TestCase):
    def test_matches_independent_csharp_v104_serializer_cases(self):
        reference_hashes = {
            "c000101": (
                "cc4e8ae69cef683f6d32df523b05a7f33e300a19a507bb8998d9e9b5d4cfa750",
                "30d1b8635079d20d06698711f31bcaf7458e71ca308a53db31561f41ea44f617",
                "d5329c520ccae4122daae4191e4385f782185c3d94605a0fb0e6e58c59bf0eaf",
            ),
            "c000301": (
                "20e5cd167fab57aba2e70e9029cbef569b4f96053e1db9efe139b17d4ca790b9",
                "c2741e3912a4ec23d316123e3561dab9119b0bbe6615c916747c29a9758792c5",
                "5094a80917b38ed371c23523836a4110f00b039c470180a5ebcb56677aa81bb1",
            ),
        }
        for stem, hashes in reference_hashes.items():
            source_path = Path(DUMP_ROOT) / "common" / "chr" / stem / f"{stem}.g4sk"
            source, nodes = read_nodes(source_path)
            parent_indices = {node.parent_index for node in nodes if node.parent_index is not None}
            leaf_index = max(index for index in range(1, len(nodes)) if index not in parent_indices)
            without_leaf = [
                G4SKNode(
                    node.name,
                    None if node.parent_index is None else node.parent_index - int(node.parent_index > leaf_index),
                    node.local_srt,
                    node.global_matrix,
                    node.inverse_bind_matrix,
                    node.flags,
                    node.bounds_radius,
                )
                for index, node in enumerate(nodes)
                if index != leaf_index
            ]
            renamed = [G4SKNode(
                "codec_roundtrip_root" if index == 0 else node.name,
                node.parent_index,
                node.local_srt,
                node.global_matrix,
                node.inverse_bind_matrix,
                node.flags,
                node.bounds_radius,
            ) for index, node in enumerate(nodes)]

            cases = (nodes, renamed, without_leaf)
            for case_nodes, expected_hash in zip(cases, hashes):
                output = write_g4sk_v104(source, case_nodes)
                self.assertEqual(expected_hash, hashlib.sha256(output).hexdigest())
                readback = parse_g4sk(output)
                self.assertEqual(len(case_nodes), readback["joint_count"])
                self.assertEqual([node.name for node in case_nodes], readback["names"])
                self.assertEqual(
                    [len(case_nodes) if node.parent_index is None else node.parent_index for node in case_nodes],
                    readback["parent_indices"],
                )
