"""Characterize a humanoid rig without changing or exporting it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median
from typing import Collection, Mapping, Sequence


Vector3 = tuple[float, float, float]
Matrix4 = tuple[float, ...]


@dataclass(frozen=True)
class BoneNode:
    name: str
    parent: str | None
    head: Vector3
    tail: Vector3
    rest_local_matrix: Matrix4
    rest_armature_matrix: Matrix4


@dataclass(frozen=True)
class MeshNode:
    name: str
    parent: str | None
    matrix_local: Matrix4
    matrix_world: Matrix4
    vertex_count: int
    vertex_groups: tuple[str, ...]
    bounds: tuple[float, float, float, float, float, float]


@dataclass(frozen=True)
class RoleSpec:
    native_joint: str
    role: str
    aliases: tuple[str, ...]
    required: bool
    side: str


@dataclass(frozen=True)
class MappingSuggestion:
    assignments: dict[str, str]
    ambiguous: dict[str, tuple[str, ...]]
    missing_required: tuple[str, ...]


def _role(native_joint: str, role: str, aliases: tuple[str, ...], required: bool, side: str = "") -> RoleSpec:
    return RoleSpec(native_joint, role, aliases, required, side)


ROLE_SPECS = (
    _role("c_global_0_0", "root", ("root", "root bone", "origin", "master", "cog"), False),
    _role("c_c_1_0", "hips", ("hips", "hip", "pelvis", "waist"), True),
    _role("c_c_1_1", "spine", ("spine", "chest", "upper chest", "torso"), True),
    _role("c_n_1_0", "neck", ("neck",), False),
    _role("c_head_1_0", "head", ("head",), True),
    _role("l_s_1_0", "left_shoulder", ("shoulder", "clavicle", "collar", "shldr"), False, "left"),
    _role("l_a_1_0", "left_upper_arm", ("upper arm", "uparm", "arm"), True, "left"),
    _role("l_a_1_1", "left_lower_arm", ("lower arm", "lowarm", "forearm", "fore arm", "elbow"), True, "left"),
    _role("l_w_1_0", "left_hand", ("wrist", "hand"), True, "left"),
    _role("r_s_1_0", "right_shoulder", ("shoulder", "clavicle", "collar", "shldr"), False, "right"),
    _role("r_a_1_0", "right_upper_arm", ("upper arm", "uparm", "arm"), True, "right"),
    _role("r_a_1_1", "right_lower_arm", ("lower arm", "lowarm", "forearm", "fore arm", "elbow"), True, "right"),
    _role("r_w_1_0", "right_hand", ("wrist", "hand"), True, "right"),
    _role("l_l_1_0", "left_upper_leg", ("upper leg", "upleg", "up leg", "thigh", "leg"), True, "left"),
    _role("l_l_1_1", "left_lower_leg", ("lower leg", "lowerleg", "calf", "shin", "knee", "leg"), True, "left"),
    _role("l_foot_1_0", "left_foot", ("foot", "ankle"), True, "left"),
    _role("l_foot_1_1", "left_toe", ("toe base", "toe", "ball of foot"), False, "left"),
    _role("r_l_1_0", "right_upper_leg", ("upper leg", "upleg", "up leg", "thigh", "leg"), True, "right"),
    _role("r_l_1_1", "right_lower_leg", ("lower leg", "lowerleg", "calf", "shin", "knee", "leg"), True, "right"),
    _role("r_foot_1_0", "right_foot", ("foot", "ankle"), True, "right"),
    _role("r_foot_1_1", "right_toe", ("toe base", "toe", "ball of foot"), False, "right"),
)


for side_name, side_prefix in (("left", "l"), ("right", "r")):
    for finger_name, finger_prefix in (
        ("thumb", "thb"), ("index", "idx"), ("middle", "mid"), ("ring", "rng"), ("little", "pky"),
    ):
        finger_aliases = {
            "thumb": ("thumb",), "index": ("index", "fore"), "middle": ("middle", "mid"),
            "ring": ("ring", "medicinal"), "little": ("little", "pinky"),
        }[finger_name]
        for segment in range(1, 4):
            ROLE_SPECS += (
                _role(
                    f"{side_prefix}_{finger_prefix}_1_{segment - 1}",
                    f"{side_name}_{finger_name}_{segment}",
                    tuple(f"{alias} {segment}" for alias in finger_aliases),
                    False,
                    side_name,
                ),
            )


REQUIRED_NATIVE_JOINTS = frozenset(spec.native_joint for spec in ROLE_SPECS if spec.required)
_ROLE_BY_JOINT = {spec.native_joint: spec for spec in ROLE_SPECS}
_PREFIX_TOKENS = frozenset({"mixamorig", "bip", "bip01", "bip001", "bone", "jnt", "joint", "def", "org"})
_SIDE_TOKENS = {"left": frozenset({"left", "l"}), "right": frozenset({"right", "r"})}
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[a-z]+|[0-9]+")
_BLENDER_SUFFIX = re.compile(r"\.\d{3}$")


def _name_parts(name: str) -> tuple[tuple[str, ...], str]:
    source_name = _BLENDER_SUFFIX.sub("", name.strip())
    source_name = source_name.rsplit(":", 1)[-1]
    source_name = re.sub(r"^([LR])(?=[A-Z][a-z])", r"\1 ", source_name)
    spaced = _CAMEL_BOUNDARY.sub(" ", source_name).lower()
    tokens = list(_WORD.findall(spaced))
    while tokens and tokens[0] in _PREFIX_TOKENS:
        prefix = tokens.pop(0)
        if prefix in {"bip", "bip01", "bip001"} and tokens and tokens[0].isdigit():
            tokens.pop(0)
    side = ""
    if tokens and tokens[0] in _SIDE_TOKENS["left"]:
        side = "left"
        tokens.pop(0)
    elif tokens and tokens[0] in _SIDE_TOKENS["right"]:
        side = "right"
        tokens.pop(0)
    if tokens and tokens[-1] in _SIDE_TOKENS["left"]:
        side = "left"
        tokens.pop()
    elif tokens and tokens[-1] in _SIDE_TOKENS["right"]:
        side = "right"
        tokens.pop()
    return tuple(tokens), side


def _alias_matches(name: str, alias: str, finger_role: bool) -> bool:
    tokens, _side = _name_parts(name)
    alias_tokens = tuple(_WORD.findall(alias.lower()))
    tokens = tuple(token for token in tokens if token not in _PREFIX_TOKENS)
    if len(tokens) > 1 and len(tokens[0]) <= 2 and tokens[0].isalpha() and tokens[0] != "up":
        tokens = tokens[1:]
    tokens = tuple(str(int(token)) if token.isdigit() else token for token in tokens)
    if finger_role:
        tokens = tuple(token for token in tokens if token not in {"hand", "finger", "f"})
    if tokens == alias_tokens:
        return True
    if alias_tokens and not any(token.isdigit() for token in alias_tokens):
        return tokens == alias_tokens + ("1",) or tokens == alias_tokens + ("01",)
    return False


def _matching_alias_length(name: str, aliases: Sequence[str], finger_role: bool) -> int:
    lengths = [
        len(_WORD.findall(alias.lower()))
        for alias in aliases
        if _alias_matches(name, alias, finger_role)
    ]
    return max(lengths, default=0)


def _is_descendant(bones_by_name: Mapping[str, BoneNode], ancestor: str, descendant: str) -> int | None:
    current = descendant
    distance = 0
    while current in bones_by_name:
        if current == ancestor:
            return distance
        parent = bones_by_name[current].parent
        if parent is None:
            return None
        current = parent
        distance += 1
    return None


def _index_bones(bones: Sequence[BoneNode]) -> dict[str, BoneNode]:
    bones_by_name = {bone.name: bone for bone in bones}
    if len(bones_by_name) != len(bones):
        raise ValueError("Bone names must be unique")
    if any(bone.parent is not None and bone.parent not in bones_by_name for bone in bones):
        raise ValueError("Every parent must name a bone in the same armature")
    for name in bones_by_name:
        current = name
        ancestors = set()
        while current is not None:
            if current in ancestors:
                raise ValueError("Bone hierarchy cannot contain cycles")
            ancestors.add(current)
            current = bones_by_name[current].parent
    return bones_by_name


def _bone_center_y(bone: BoneNode) -> float:
    return (bone.head[1] + bone.tail[1]) * 0.5


def _candidate_score(
    bone: BoneNode,
    spec: RoleSpec,
    center_y: float,
    y_threshold: float,
) -> int | None:
    if bone.name == spec.native_joint:
        return 1000
    _tokens, named_side = _name_parts(bone.name)
    if spec.side and named_side and named_side != spec.side:
        return None
    finger_role = any(f"_{finger}_" in spec.role for finger in ("thumb", "index", "middle", "ring", "little"))
    alias_length = _matching_alias_length(bone.name, spec.aliases, finger_role)
    if not alias_length:
        return None
    score = 100 + alias_length * 10
    if "leg" in spec.aliases and _alias_matches(bone.name, "leg", False):
        score -= 5
    if named_side == spec.side and spec.side:
        score += 40
    elif spec.side:
        lateral = _bone_center_y(bone) - center_y
        if abs(lateral) <= y_threshold:
            score -= 10
        elif (lateral > 0.0) == (spec.side == "left"):
            score += 20
        else:
            return None
    return score


def suggest_native_mapping(bones: Sequence[BoneNode], native_joint_names: Collection[str]) -> MappingSuggestion:
    bones_by_name = _index_bones(bones)

    y_values = [_bone_center_y(bone) for bone in bones]
    center_y = median(y_values) if y_values else 0.0
    hips_spec = _ROLE_BY_JOINT["c_c_1_0"]
    hip_candidates = [bone for bone in bones if any(_alias_matches(bone.name, alias, False) for alias in hips_spec.aliases)]
    if len(hip_candidates) == 1:
        center_y = _bone_center_y(hip_candidates[0])
    y_threshold = max(1e-5, (max((bone.head[2] for bone in bones), default=0.0) - min((bone.head[2] for bone in bones), default=0.0)) * 0.01)

    assignments: dict[str, str] = {}
    ambiguities: dict[str, tuple[str, ...]] = {}
    for spec in ROLE_SPECS:
        candidates = []
        for bone in bones:
            score = _candidate_score(bone, spec, center_y, y_threshold)
            if score is not None:
                candidates.append((score, bone.name))
        if not candidates:
            continue
        if spec.native_joint == "c_c_1_1" and "c_c_1_0" in assignments:
            hips_name = assignments["c_c_1_0"]
            distances = {
                name: _is_descendant(bones_by_name, hips_name, name)
                for _score, name in candidates
            }
            descendant_candidates = [item for item in candidates if distances[item[1]] is not None]
            if descendant_candidates:
                candidates = descendant_candidates
                shortest = min(distances[name] for _score, name in candidates if distances[name] is not None)
                candidates = [(score, name) for score, name in candidates if distances[name] == shortest]
        highest_score = max(score for score, _name in candidates)
        best = sorted(name for score, name in candidates if score == highest_score)
        if len(best) > 1 and spec.role != "hips":
            ancestors = [
                name for name in best
                if all(_is_descendant(bones_by_name, name, other) is not None for other in best)
            ]
            if len(ancestors) == 1:
                best = ancestors
            elif spec.role.endswith(("_hand", "_lower_leg")):
                descendants = {name: sum(_is_descendant(bones_by_name, name, other) is not None
                                         for other in bones_by_name if other != name) for name in best}
                maximum = max(descendants.values())
                strongest = [name for name in best if descendants[name] == maximum]
                if maximum > 1 and len(strongest) == 1:
                    best = strongest
        if len(best) == 1:
            assignments[spec.native_joint] = best[0]
        else:
            ambiguities[spec.native_joint] = tuple(best)

    chain_assignments = _suggest_limb_chain_roles(bones_by_name, assignments)
    assignments.update(chain_assignments)
    for joint in chain_assignments:
        ambiguities.pop(joint, None)
    landmarks = [assignments.get(joint) for joint in ("c_c_1_1", "l_l_1_0", "r_l_1_0")]
    if all(landmarks):
        common = [name for name in bones_by_name
                  if all(_is_descendant(bones_by_name, name, landmark) is not None for landmark in landmarks)]
        if common:
            hips = min(common, key=lambda name: sum(_is_descendant(bones_by_name, name, landmark) for landmark in landmarks))
            assignments["c_c_1_0"] = hips
            ambiguities.pop("c_c_1_0", None)
            if assignments.get("c_global_0_0") == hips:
                assignments.pop("c_global_0_0")
    source_targets: dict[str, list[str]] = {}
    for native_joint, bone_name in assignments.items():
        source_targets.setdefault(bone_name, []).append(native_joint)
    for bone_name, native_targets in source_targets.items():
        if len(native_targets) > 1:
            for native_joint in native_targets:
                assignments.pop(native_joint, None)
                ambiguities[native_joint] = (bone_name,)

    for bone in bones:
        if bone.name in native_joint_names and bone.name not in assignments.values():
            assignments[bone.name] = bone.name

    missing_required = tuple(
        spec.native_joint for spec in ROLE_SPECS
        if spec.required and spec.native_joint not in assignments
    )
    return MappingSuggestion(assignments, ambiguities, missing_required)


def _path_between(
    bones_by_name: Mapping[str, BoneNode],
    start: str,
    end: str,
) -> list[str] | None:
    path = [end]
    current = end
    while current != start:
        bone = bones_by_name.get(current)
        if bone is None or bone.parent is None:
            return None
        current = bone.parent
        path.append(current)
    path.reverse()
    return path


def _auxiliary_category(name: str) -> str:
    tokens, _side = _name_parts(name)
    key = " ".join(tokens)
    if any(word in tokens for word in ("hair", "ponytail", "twintail", "braid", "kami", "hir")):
        return "hair"
    if any(word in tokens for word in ("twist", "roll", "rollbone")):
        return "twist"
    if any(word in tokens for word in ("face", "eye", "eyelid", "jaw", "mouth", "brow", "teeth")):
        return "face"
    if any(word in tokens for word in ("cloth", "skirt", "coat", "robe", "hem", "mantle", "sleeve")):
        return "cloth"
    if any(word in tokens for word in ("ctrl", "control", "helper", "pole", "ik", "fk", "eff")):
        return "control"
    if "wing" in key or "tail" in key:
        return "appendage"
    return "other"


def _suggest_limb_chain_roles(
    bones_by_name: Mapping[str, BoneNode], assignments: Mapping[str, str],
) -> dict[str, str]:
    suggestions = {}
    for side, prefix in (("left", "l"), ("right", "r")):
        shoulder = assignments.get(f"{prefix}_s_1_0")
        elbow = assignments.get(f"{prefix}_a_1_1")
        if shoulder and elbow and f"{prefix}_a_1_0" not in assignments:
            path = _path_between(bones_by_name, shoulder, elbow)
            members = [name for name in path or () if _auxiliary_category(name) not in {"twist", "control"}]
            if len(members) == 3:
                suggestions[f"{prefix}_a_1_0"] = members[1]

        thigh = assignments.get(f"{prefix}_l_1_0")
        if not thigh or (f"{prefix}_l_1_1" in assignments and f"{prefix}_foot_1_0" in assignments):
            continue
        toes = []
        for bone in bones_by_name.values():
            tokens, named_side = _name_parts(bone.name)
            if named_side and named_side != side:
                continue
            if any(token in {"toe", "toes"} for token in tokens) and _is_descendant(bones_by_name, thigh, bone.name):
                toes.append(bone.name)
        endpoints = [
            name for name in toes
            if not any(name != other and _is_descendant(bones_by_name, name, other) is not None for other in toes)
        ]
        if len(endpoints) != 1:
            continue
        path = _path_between(bones_by_name, thigh, endpoints[0])
        members = [name for name in path or () if _auxiliary_category(name) not in {"twist", "control"}]
        if len(members) != 4:
            continue
        expected = {
            f"{prefix}_l_1_1": members[1], f"{prefix}_foot_1_0": members[2], f"{prefix}_foot_1_1": members[3],
        }
        if any(assignments.get(joint, expected[joint]) != expected[joint]
               for joint in (f"{prefix}_l_1_1", f"{prefix}_foot_1_0")):
            continue
        previous_toe = assignments.get(f"{prefix}_foot_1_1")
        if previous_toe and previous_toe not in members[2:]:
            continue
        suggestions.update(expected)
    return suggestions


def build_representation(
    bones: Sequence[BoneNode],
    native_to_source: Mapping[str, str],
    native_joint_names: Collection[str],
    armature_matrix_world: Matrix4,
    mesh_objects: Sequence[MeshNode],
) -> dict:
    bones_by_name = _index_bones(bones)
    if len(armature_matrix_world) != 16:
        raise ValueError("Armature world matrix must contain 16 values")
    if any(len(bone.rest_local_matrix) != 16 or len(bone.rest_armature_matrix) != 16 for bone in bones):
        raise ValueError("Bone rest matrices must contain 16 values")
    if any(
        len(mesh.matrix_local) != 16 or len(mesh.matrix_world) != 16 or len(mesh.bounds) != 6 or mesh.vertex_count < 0
        for mesh in mesh_objects
    ):
        raise ValueError("Mesh snapshots must contain transforms, bounds, and a non-negative vertex count")

    invalid_mappings = {
        native_joint: source_bone
        for native_joint, source_bone in native_to_source.items()
        if native_joint not in native_joint_names or source_bone not in bones_by_name
    }
    source_targets: dict[str, list[str]] = {}
    for native_joint, source_bone in native_to_source.items():
        source_targets.setdefault(source_bone, []).append(native_joint)
    duplicate_sources = {
        source_bone: sorted(native_targets)
        for source_bone, native_targets in source_targets.items()
        if len(native_targets) > 1
    }

    valid_mapping = {
        native_joint: source_bone
        for native_joint, source_bone in native_to_source.items()
        if native_joint in native_joint_names and source_bone in bones_by_name
    }
    chains: list[dict] = []
    chain_warnings: list[str] = []

    def add_chain(name: str, joints: tuple[str, ...]) -> None:
        assigned = [valid_mapping[joint] for joint in joints if joint in valid_mapping]
        for parent, child in zip(assigned, assigned[1:]):
            if not _is_descendant(bones_by_name, parent, child):
                chain_warnings.append(f"{name}: {parent!r} must be an ancestor of {child!r}")
        start_joint, end_joint = joints[0], joints[-1]
        start = valid_mapping.get(start_joint)
        end = valid_mapping.get(end_joint)
        if start is None or end is None:
            return
        path = _path_between(bones_by_name, start, end)
        if path is None:
            chain_warnings.append(f"{name}: {start!r} is not an ancestor of {end!r}")
            return
        chains.append({
            "name": name,
            "start_joint": start_joint,
            "end_joint": end_joint,
            "start_bone": start,
            "end_bone": end,
            "bones": path,
        })

    add_chain("root", ("c_global_0_0", "c_c_1_0"))
    add_chain("spine", ("c_c_1_0", "c_c_1_1", "c_n_1_0", "c_head_1_0"))
    for side, prefix in (("left", "l"), ("right", "r")):
        shoulder_joint = f"{prefix}_s_1_0"
        upper_arm_joint = f"{prefix}_a_1_0"
        hand_joint = f"{prefix}_w_1_0"
        arm_joints = (upper_arm_joint, f"{prefix}_a_1_1", hand_joint)
        if shoulder_joint in valid_mapping:
            arm_joints = (shoulder_joint, *arm_joints)
        add_chain(f"{side}_arm", arm_joints)
        leg_joints = (f"{prefix}_l_1_0", f"{prefix}_l_1_1", f"{prefix}_foot_1_0")
        if f"{prefix}_foot_1_1" in valid_mapping:
            leg_joints += (f"{prefix}_foot_1_1",)
        add_chain(f"{side}_leg", leg_joints)
        finger_joint_names = (
            ("thumb", "thb"), ("index", "idx"), ("middle", "mid"), ("ring", "rng"), ("little", "pky"),
        )
        for finger, finger_prefix in finger_joint_names:
            add_chain(
                f"{side}_{finger}",
                tuple(f"{prefix}_{finger_prefix}_1_{segment}" for segment in range(3)),
            )

    chain_memberships: dict[str, list[str]] = {}
    for chain in chains:
        for bone_name in chain["bones"]:
            chain_memberships.setdefault(bone_name, []).append(chain["name"])
    assigned_semantic_bones = {
        source_bone for native_joint, source_bone in valid_mapping.items()
        if native_joint in _ROLE_BY_JOINT
    }
    native_for_source = {
        source_bone: native_joint for native_joint, source_bone in valid_mapping.items()
        if native_joint not in _ROLE_BY_JOINT
    }
    auxiliary_bones = []
    for bone in bones:
        if bone.name in assigned_semantic_bones:
            continue
        category = _auxiliary_category(bone.name)
        if category == "other" and bone.name in chain_memberships:
            category = "chain_intermediate"
        native_joint = native_for_source.get(bone.name, "")
        if not native_joint and bone.name in native_joint_names:
            native_joint = bone.name
        auxiliary_bones.append({
            "name": bone.name,
            "parent": bone.parent,
            "category": category,
            "native_joint": native_joint,
            "chains": chain_memberships.get(bone.name, []),
        })

    missing_required = sorted(REQUIRED_NATIVE_JOINTS - set(valid_mapping))
    rest_pose = [{
        "name": bone.name,
        "parent": bone.parent,
        "head": list(bone.head),
        "tail": list(bone.tail),
        "local_matrix": list(bone.rest_local_matrix),
        "armature_matrix": list(bone.rest_armature_matrix),
    } for bone in bones]
    serialized_meshes = [{
        "name": mesh.name,
        "parent": mesh.parent,
        "matrix_local": list(mesh.matrix_local),
        "matrix_world": list(mesh.matrix_world),
        "vertex_count": mesh.vertex_count,
        "vertex_groups": list(mesh.vertex_groups),
        "bounds": list(mesh.bounds),
    } for mesh in mesh_objects]
    return {
        "schema_version": 1,
        "coordinate_frame": {"up": "Z+", "forward": "X+"},
        "armature_matrix_world": list(armature_matrix_world),
        "bone_count": len(bones),
        "native_mapping": dict(sorted(valid_mapping.items())),
        "chains": chains,
        "auxiliary_bones": auxiliary_bones,
        "rest_pose": rest_pose,
        "mesh_objects": serialized_meshes,
        "validation": {
            "missing_required": missing_required,
            "invalid_mappings": invalid_mappings,
            "duplicate_sources": duplicate_sources,
            "chain_warnings": chain_warnings,
        },
    }
