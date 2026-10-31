"""Project-authored, metre-scale T-pose in native Y-up coordinates.

Only the joint naming contract is shared with the alias catalog. Positions,
neutral auxiliary attachments and bind transforms are authored here; no game
resource, donor armature or scene state is read to construct this prefab.
"""

from __future__ import annotations

import struct

try:
    from ..formats.g4sk_writer import G4SKNode, write_g4sk_v104
    from ..porting.g4_model_writer import validate_authored_bind_pose
except ImportError:
    from formats.g4sk_writer import G4SKNode, write_g4sk_v104
    from porting.g4_model_writer import validate_authored_bind_pose


PREFAB_ID = 'humanoid-v1'
SOURCE_MODEL_STEM = 'c01000010'
CHR_FAMILY = 'c000101'
EVENT_SKELETON = 'c000101'

# Global positions, metres; left is +X and forward is +Z.
_CENTER = (
    ('output', None, (0., 0., 0.)),
    ('boundingBox', 'output', (0., 0., 0.)),
    ('c_global_0_0', 'output', (0., 0., 0.)),
    ('c_ball_1_0', 'c_global_0_0', (0., 0., .35)),
    ('c_c_1_0', 'c_global_0_0', (0., .92, 0.)),
    ('c_c_1_1', 'c_c_1_0', (0., 1.22, 0.)),
    ('c_n_1_0', 'c_c_1_1', (0., 1.47, 0.)),
    ('c_head_1_0', 'c_n_1_0', (0., 1.57, 0.)),
    ('c_atc01_1_0', 'c_head_1_0', (0., 1.76, 0.)),
)
_SIDE = (
    ('s_1_0', 'c_c_1_1', (.075, 1.40, 0.)),
    ('a_1_0', 's_1_0', (.19, 1.40, 0.)),
    ('a_1_1', 'a_1_0', (.46, 1.40, 0.)),
    ('w_1_0', 'a_1_1', (.70, 1.40, 0.)),
    ('wph_1_0', 'w_1_0', (.75, 1.40, 0.)),
    ('l_1_0', 'c_c_1_0', (.10, .90, 0.)),
    ('l_1_1', 'l_1_0', (.10, .50, .025)),
    ('foot_1_0', 'l_1_1', (.10, .10, 0.)),
    ('foot_1_1', 'foot_1_0', (.10, .045, .16)),
    ('pnt_1_0', 'foot_1_1', (.10, .045, .23)),
)
# Finger bases fan across the palm; each tuple gives X positions and Z spread.
_FINGERS = (
    ('thb', (.765, .795, .82), .055),
    ('idx', (.81, .845, .87), .030),
    ('mid', (.82, .86, .89), .005),
    ('rng', (.815, .85, .875), -.020),
    ('pky', (.80, .83, .85), -.045),
)
# Unspecified accessory geometry stays at explicit neutral attachment points.
_CENTER_AUX = (
    ('eye_10', 'c_head_1_0'), ('mouth_10', 'c_head_1_0'),
    ('c_sht1_1_0', 'c_c_1_0'),
    ('c_hir1_1_0', 'c_head_1_0'),
    ('c_hir2_1_0', 'c_hir1_1_0'), ('c_hir2_1_1', 'c_hir2_1_0'),
    ('c_hir2_1_2', 'c_hir2_1_1'),
    ('c_hir3_1_0', 'c_hir1_1_0'), ('c_hir3_1_1', 'c_hir3_1_0'),
    ('c_hir4_1_0', 'c_hir1_1_0'),
    ('c_da_mnt_1_0', 'c_c_1_1'), ('c_da_mnt_1_1', 'c_da_mnt_1_0'),
    ('c_mnt_1_0', 'c_c_1_1'), ('c_mnt_1_1', 'c_mnt_1_0'),
)
_SIDE_AUX = (
    ('soh_1_0', 'wph_1_0'), ('fa_1_0', 'wph_1_0'),
    ('hir1_1_0', 'c_head_1_0'), ('hir1_1_1', 'hir1_1_0'),
    ('hir2_1_0', 'c_head_1_0'), ('hir2_1_1', 'hir2_1_0'),
    ('hir2_1_2', 'hir2_1_1'),
    ('hir3_1_0', 'c_head_1_0'), ('hir3_1_1', 'hir3_1_0'),
    ('hir4_1_0', 'c_head_1_0'),
    ('mnt_1_0', 'c_c_1_1'), ('mnt_1_1', 'mnt_1_0'),
    ('slv2_1_0', 'a_1_0'), ('slv1_1_0', 'a_1_1'),
    ('wng1_1_0', 'c_c_1_1'),
)


def _prefab_nodes() -> list[G4SKNode]:
    joints = list(_CENTER)
    for side, sign in (('l', 1), ('r', -1)):
        for name, parent, (x, y, z) in _SIDE:
            parent_name = parent if parent.startswith('c_') else f'{side}_{parent}'
            joints.append((f'{side}_{name}', parent_name, (sign * x, y, z)))
        for finger, xs, z in _FINGERS:
            parent = f'{side}_wph_1_0'
            for segment, x in enumerate(xs):
                name = f'{side}_{finger}_1_{segment}'
                joints.append((name, parent, (sign * x, 1.40, z)))
                parent = name
    positions = {name: position for name, _, position in joints}
    for name, parent in _CENTER_AUX:
        positions[name] = positions[parent]
        joints.append((name, parent, positions[name]))
    for side in ('l', 'r'):
        for name, parent in _SIDE_AUX:
            name = f'{side}_{name}'
            parent = parent if parent.startswith('c_') else f'{side}_{parent}'
            positions[name] = positions[parent]
            joints.append((name, parent, positions[name]))

    indices = {name: index for index, (name, _, _) in enumerate(joints)}
    nodes = []
    for name, parent, (x, y, z) in joints:
        px, py, pz = positions[parent] if parent is not None else (0., 0., 0.)
        nodes.append(G4SKNode(
            name, indices[parent] if parent is not None else None,
            (1., 1., 1., 0., 0., 0., 0., 1., x-px, y-py, z-pz, 1.),
            (1., 0., 0., x, 0., 1., 0., y, 0., 0., 1., z),
            (1., 0., 0., -x, 0., 1., 0., -y, 0., 0., 1., -z),
            1, 0.,
        ))
    return nodes


def prefab_joint_names() -> frozenset[str]:
    return frozenset(node.name for node in _prefab_nodes())


def prefab_skeleton_bytes() -> bytes:
    nodes = _prefab_nodes()
    validate_authored_bind_pose(nodes)
    header = bytearray(0x40)
    header[:4] = b'G4SK'
    struct.pack_into('<HHB', header, 4, 0x40, 104, 0)
    struct.pack_into('<H', header, 0x0A, 0x40 // 4)
    return write_g4sk_v104(bytes(header), nodes)
