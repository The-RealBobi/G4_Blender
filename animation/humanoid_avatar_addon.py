"""Blender UI and persistence for the humanoid representation profile."""

from __future__ import annotations

import hashlib
import json
import struct

import bpy
from bpy.props import BoolProperty, CollectionProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup, UIList

from .humanoid_avatar import (
    BoneNode,
    MeshNode,
    ROLE_SPECS,
    build_representation,
    suggest_native_mapping,
)

try:
    from ..shared.g4_joint_aliases import load_joint_alias_catalog
except ImportError:
    from shared.g4_joint_aliases import load_joint_alias_catalog


from .humanoid_avatar_preview import IMPORT_SCENE_OT_level5_humanoid_animation


UNASSIGNED_JOINT = "__NONE__"


def reference_joint_names(context, armature_object) -> set[str]:
    try:
        from ..porting.g4_port_addon import configured_target_joints
        from ..porting.g4_model_probe import parse_g4sk
    except ImportError:
        from porting.g4_port_addon import configured_target_joints
        from porting.g4_model_probe import parse_g4sk
    from .g4_animation_addon import materialize_skeleton_source

    joints = set(load_joint_alias_catalog().joints)
    props = getattr(context.scene, "level5_g4_port", None)
    if props is not None:
        joints.update(configured_target_joints(props) or ())
    source = str(armature_object.get("g4_character_skeleton_source", ""))
    if source:
        path = materialize_skeleton_source(source)
        if path is None:
            raise ValueError(f"Original character skeleton is unavailable: {source}")
        joints.update(parse_g4sk(path.read_bytes())["names"])
    return joints


def mark_mapping_dirty(self, _context):
    owner = self.id_data
    if owner is not None and hasattr(owner, "level5_humanoid_avatar"):
        owner.level5_humanoid_avatar.mapping_dirty = True


class G4HumanoidAvatarRole(PropertyGroup):
    native_joint: StringProperty(options={"HIDDEN"})
    role: StringProperty(options={"HIDDEN"})
    required: BoolProperty(options={"HIDDEN"})
    source_bone: StringProperty(name="Bone", update=mark_mapping_dirty)
    ambiguous_candidates: StringProperty(options={"HIDDEN"})


class G4HumanoidAvatarAuxiliary(PropertyGroup):
    source_bone: StringProperty(name="Bone", update=mark_mapping_dirty)
    parent_bone: StringProperty(options={"HIDDEN"})
    category: StringProperty(options={"HIDDEN"})
    native_joint: StringProperty(
        name="G4 Joint",
        update=mark_mapping_dirty,
    )
    chain_names: StringProperty(options={"HIDDEN"})


class G4HumanoidAvatarRestBone(PropertyGroup):
    name: StringProperty()
    parent_bone: StringProperty()
    head: FloatVectorProperty(size=3)
    tail: FloatVectorProperty(size=3)
    local_matrix: FloatVectorProperty(size=16)
    armature_matrix: FloatVectorProperty(size=16)


class G4HumanoidAvatarMesh(PropertyGroup):
    name: StringProperty()
    parent_bone: StringProperty()
    vertex_count: IntProperty()
    matrix_local: FloatVectorProperty(size=16)
    matrix_world: FloatVectorProperty(size=16)
    bounds: FloatVectorProperty(size=6)
    vertex_groups_json: StringProperty()


class G4HumanoidAvatarChain(PropertyGroup):
    name: StringProperty()
    start_joint: StringProperty()
    end_joint: StringProperty()
    start_bone: StringProperty()
    end_bone: StringProperty()
    bones_json: StringProperty()


class G4HumanoidAvatarNativeJoint(PropertyGroup):
    name: StringProperty()


class G4HumanoidAvatarProfile(PropertyGroup):
    schema_version: IntProperty(default=2)
    has_representation: BoolProperty()
    mapping_dirty: BoolProperty()
    mapping_valid: BoolProperty()
    pose_stale: BoolProperty()
    show_fingers: BoolProperty(name="Show Finger Assignments", default=False)
    bone_count: IntProperty()
    mesh_count: IntProperty()
    unresolved_ambiguities: IntProperty()
    missing_required_json: StringProperty()
    validation_json: StringProperty()
    pose_signature: StringProperty(options={"HIDDEN"})
    armature_matrix_world: FloatVectorProperty(size=16)
    roles: CollectionProperty(type=G4HumanoidAvatarRole)
    auxiliaries: CollectionProperty(type=G4HumanoidAvatarAuxiliary)
    rest_pose: CollectionProperty(type=G4HumanoidAvatarRestBone)
    mesh_objects: CollectionProperty(type=G4HumanoidAvatarMesh)
    chains: CollectionProperty(type=G4HumanoidAvatarChain)
    native_joints: CollectionProperty(type=G4HumanoidAvatarNativeJoint)
    active_auxiliary_index: IntProperty()


def matrix_values(matrix) -> tuple[float, ...]:
    return tuple(float(matrix[row][column]) for row in range(4) for column in range(4))


def bone_nodes(armature_object) -> list[BoneNode]:
    nodes = []
    for bone in armature_object.data.bones:
        parent = bone.parent
        local_matrix = parent.matrix_local.inverted_safe() @ bone.matrix_local if parent is not None else bone.matrix_local.copy()
        nodes.append(BoneNode(
            bone.name,
            parent.name if parent is not None else None,
            tuple(float(value) for value in bone.head_local),
            tuple(float(value) for value in bone.tail_local),
            matrix_values(local_matrix),
            matrix_values(bone.matrix_local),
        ))
    return nodes


def mesh_nodes(context, armature_object) -> list[MeshNode]:
    result = []
    for mesh_object in context.scene.objects:
        if mesh_object.type != "MESH":
            continue
        if not any(modifier.type == "ARMATURE" and modifier.object == armature_object for modifier in mesh_object.modifiers):
            continue
        bounds = [float(value) for corner in mesh_object.bound_box for value in corner]
        result.append(MeshNode(
            mesh_object.name,
            mesh_object.parent.name if mesh_object.parent is not None else None,
            matrix_values(mesh_object.matrix_local),
            matrix_values(mesh_object.matrix_world),
            len(mesh_object.data.vertices),
            tuple(group.name for group in mesh_object.vertex_groups),
            (
                min(bounds[0::3]), min(bounds[1::3]), min(bounds[2::3]),
                max(bounds[0::3]), max(bounds[1::3]), max(bounds[2::3]),
            ),
        ))
    return result


def pose_signature(armature_object, bones: list[BoneNode], meshes: list[MeshNode]) -> str:
    payload = {
        "armature_world": matrix_values(armature_object.matrix_world),
        "bones": [(bone.name, bone.parent, bone.rest_local_matrix) for bone in bones],
        "meshes": [(mesh.name, mesh.matrix_local, mesh.matrix_world) for mesh in meshes],
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mapping_from_profile(profile: G4HumanoidAvatarProfile) -> dict[str, str]:
    mapping = {}
    for role in profile.roles:
        if role.native_joint and role.source_bone:
            mapping.setdefault(role.native_joint, role.source_bone)
    for item in profile.auxiliaries:
        if item.native_joint not in {"", UNASSIGNED_JOINT} and item.source_bone:
            mapping.setdefault(item.native_joint, item.source_bone)
    return mapping


def duplicate_native_assignments(profile: G4HumanoidAvatarProfile) -> dict[str, list[str]]:
    source_names: dict[str, list[str]] = {}
    for role in profile.roles:
        if role.source_bone:
            source_names.setdefault(role.native_joint, []).append(role.source_bone)
    for item in profile.auxiliaries:
        if item.native_joint not in {"", UNASSIGNED_JOINT} and item.source_bone:
            source_names.setdefault(item.native_joint, []).append(item.source_bone)
    return {
        native_joint: sorted(set(names))
        for native_joint, names in source_names.items()
        if len(set(names)) > 1
    }


def update_pose_snapshot(profile: G4HumanoidAvatarProfile, armature_object, bones: list[BoneNode], meshes: list[MeshNode]) -> None:
    profile.rest_pose.clear()
    for bone in bones:
        item = profile.rest_pose.add()
        item.name = bone.name
        item.parent_bone = bone.parent or ""
        item.head = bone.head
        item.tail = bone.tail
        item.local_matrix = bone.rest_local_matrix
        item.armature_matrix = bone.rest_armature_matrix

    profile.mesh_objects.clear()
    for mesh in meshes:
        item = profile.mesh_objects.add()
        item.name = mesh.name
        item.parent_bone = mesh.parent or ""
        item.vertex_count = mesh.vertex_count
        item.matrix_local = mesh.matrix_local
        item.matrix_world = mesh.matrix_world
        item.bounds = mesh.bounds
        item.vertex_groups_json = json.dumps(mesh.vertex_groups)
    profile.armature_matrix_world = matrix_values(armature_object.matrix_world)
    profile.pose_signature = pose_signature(armature_object, bones, meshes)
    profile.pose_stale = False


def populate_suggestions(profile: G4HumanoidAvatarProfile, armature_object, bones: list[BoneNode], native_joint_names) -> None:
    old_auxiliary_targets = {}
    legacy_joint_names = [UNASSIGNED_JOINT, *sorted(load_joint_alias_catalog().joints)]
    for item in profile.auxiliaries:
        target = item.get("native_joint", "")
        if isinstance(target, int):
            if not 0 <= target < len(legacy_joint_names):
                raise ValueError(f"Unknown legacy G4 joint selection on {item.source_bone}")
            target = legacy_joint_names[target]
        if item.source_bone and target not in {"", UNASSIGNED_JOINT}:
            old_auxiliary_targets[item.source_bone] = target
    suggestions = suggest_native_mapping(bones, native_joint_names)
    profile.roles.clear()
    for spec in ROLE_SPECS:
        item = profile.roles.add()
        item.native_joint = spec.native_joint
        item.role = spec.role
        item.required = spec.required
        item.source_bone = suggestions.assignments.get(spec.native_joint, "")
        item.ambiguous_candidates = json.dumps(suggestions.ambiguous.get(spec.native_joint, ()))

    role_bones = {item.source_bone for item in profile.roles if item.source_bone}
    profile.auxiliaries.clear()
    initial_mapping = {item.native_joint: item.source_bone for item in profile.roles if item.source_bone}
    representation = build_representation(
        bones,
        initial_mapping,
        native_joint_names,
        matrix_values(armature_object.matrix_world),
        [],
    )
    for auxiliary in representation["auxiliary_bones"]:
        if auxiliary["name"] in role_bones:
            continue
        item = profile.auxiliaries.add()
        item.source_bone = auxiliary["name"]
        item.parent_bone = auxiliary["parent"] or ""
        item.category = auxiliary["category"]
        item.native_joint = old_auxiliary_targets.get(auxiliary["name"], auxiliary["native_joint"])
        item.chain_names = json.dumps(auxiliary["chains"])
    profile.unresolved_ambiguities = sum(
        1 for item in profile.roles
        if json.loads(item.ambiguous_candidates or "[]") and not item.source_bone
    )


def update_validated_representation(context, armature_object, profile: G4HumanoidAvatarProfile) -> dict:
    bones = bone_nodes(armature_object)
    meshes = mesh_nodes(context, armature_object)
    native_joint_names = {item.name for item in profile.native_joints}
    representation = build_representation(
        bones,
        mapping_from_profile(profile),
        native_joint_names,
        matrix_values(armature_object.matrix_world),
        meshes,
    )

    profile.chains.clear()
    for chain in representation["chains"]:
        item = profile.chains.add()
        item.name = chain["name"]
        item.start_joint = chain["start_joint"]
        item.end_joint = chain["end_joint"]
        item.start_bone = chain["start_bone"]
        item.end_bone = chain["end_bone"]
        item.bones_json = json.dumps(chain["bones"])

    validation = representation["validation"]
    duplicate_targets = duplicate_native_assignments(profile)
    profile.bone_count = len(bones)
    profile.mesh_count = len(meshes)
    profile.missing_required_json = json.dumps(validation["missing_required"])
    profile.validation_json = json.dumps({
        "invalid_mappings": validation["invalid_mappings"],
        "duplicate_sources": validation["duplicate_sources"],
        "duplicate_native_targets": duplicate_targets,
        "chain_warnings": validation["chain_warnings"],
    }, sort_keys=True)
    unresolved = [
        item.native_joint for item in profile.roles
        if json.loads(item.ambiguous_candidates or "[]") and not item.source_bone
    ]
    profile.unresolved_ambiguities = len(unresolved)
    signature = pose_signature(armature_object, bones, meshes)
    pose_stale = bool(profile.pose_signature and profile.pose_signature != signature)
    has_errors = bool(
        validation["missing_required"]
        or validation["invalid_mappings"]
        or validation["duplicate_sources"]
        or duplicate_targets
        or validation["chain_warnings"]
        or unresolved
    )
    profile.mapping_valid = not has_errors and not pose_stale
    profile.pose_stale = pose_stale
    profile.mapping_dirty = False
    return {"valid": profile.mapping_valid, "pose_stale": pose_stale, "representation": representation}


def capture_avatar_representation(context, armature_object, native_joint_names) -> dict:
    profile = armature_object.level5_humanoid_avatar
    bones = bone_nodes(armature_object)
    meshes = mesh_nodes(context, armature_object)
    profile.native_joints.clear()
    for joint in sorted(native_joint_names):
        profile.native_joints.add().name = joint
    populate_suggestions(profile, armature_object, bones, native_joint_names)
    profile.schema_version = 2
    update_pose_snapshot(profile, armature_object, bones, meshes)
    profile.has_representation = True
    profile.mapping_dirty = True
    return update_validated_representation(context, armature_object, profile)


class OBJECT_OT_level5_humanoid_analyze(Operator):
    bl_idname = "object.level5_humanoid_analyze"
    bl_label = "Build Avatar Representation"
    bl_description = "Map humanoid roles and capture this rig's complete rest pose"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "ARMATURE"

    def execute(self, context):
        armature_object = context.active_object
        profile = armature_object.level5_humanoid_avatar
        bones = bone_nodes(armature_object)
        meshes = mesh_nodes(context, armature_object)
        try:
            native_joint_names = reference_joint_names(context, armature_object)
        except (OSError, ValueError, struct.error) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        result = capture_avatar_representation(context, armature_object, native_joint_names)
        if result["valid"]:
            self.report({"INFO"}, f"Avatar representation captured: {len(bones)} bones and {len(meshes)} bound meshes")
        else:
            missing = json.loads(profile.missing_required_json)
            self.report({"WARNING"}, f"Representation captured with {len(missing)} required native joints unresolved")
        return {"FINISHED"}


class OBJECT_OT_level5_humanoid_validate(Operator):
    bl_idname = "object.level5_humanoid_validate"
    bl_label = "Validate Mapping"
    bl_description = "Rebuild semantic chains and validate the current bone assignments"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None
            and context.active_object.type == "ARMATURE"
            and context.active_object.level5_humanoid_avatar.has_representation
        )

    def execute(self, context):
        armature_object = context.active_object
        result = update_validated_representation(
            context,
            armature_object,
            armature_object.level5_humanoid_avatar,
        )
        if result["valid"]:
            self.report({"INFO"}, "Avatar mapping and captured pose are valid")
        elif result["pose_stale"]:
            self.report({"WARNING"}, "The rig or attached mesh transforms changed; capture the native pose again")
        else:
            self.report({"WARNING"}, "Some native G4 joints still need assignment or correction")
        return {"FINISHED"}


class OBJECT_OT_level5_humanoid_capture_pose(Operator):
    bl_idname = "object.level5_humanoid_capture_pose"
    bl_label = "Capture Native Pose"
    bl_description = "Refresh rest matrices and bound mesh transforms without changing the rig"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None
            and context.active_object.type == "ARMATURE"
            and context.active_object.level5_humanoid_avatar.has_representation
        )

    def execute(self, context):
        armature_object = context.active_object
        profile = armature_object.level5_humanoid_avatar
        update_pose_snapshot(profile, armature_object, bone_nodes(armature_object), mesh_nodes(context, armature_object))
        update_validated_representation(context, armature_object, profile)
        self.report({"INFO"}, "Native rest pose and bound mesh transforms captured")
        return {"FINISHED"}


class OBJECT_UL_level5_humanoid_auxiliary(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index):
        row = layout.row(align=True)
        row.prop(item, "source_bone", text="", icon="BONE_DATA")
        row.label(text=item.category.replace("_", " ").title())
        row.prop_search(item, "native_joint", item.id_data.level5_humanoid_avatar, "native_joints", text="")




CLASSES = (
    G4HumanoidAvatarRole,
    G4HumanoidAvatarAuxiliary,
    G4HumanoidAvatarRestBone,
    G4HumanoidAvatarMesh,
    G4HumanoidAvatarChain,
    G4HumanoidAvatarNativeJoint,
    G4HumanoidAvatarProfile,
    OBJECT_UL_level5_humanoid_auxiliary,
    OBJECT_OT_level5_humanoid_analyze,
    OBJECT_OT_level5_humanoid_validate,
    OBJECT_OT_level5_humanoid_capture_pose,
    IMPORT_SCENE_OT_level5_humanoid_animation,
)


def register_class_replacing_stale(cls) -> None:
    existing = getattr(bpy.types, cls.__name__, None)
    if existing is cls:
        return
    if existing is not None:
        try:
            bpy.utils.unregister_class(existing)
        except RuntimeError:
            pass
    try:
        bpy.utils.register_class(cls)
    except ValueError:
        pass


def register() -> None:
    panel = getattr(bpy.types, 'VIEW3D_PT_level5_humanoid_avatar', None)
    if panel is not None:
        bpy.utils.unregister_class(panel)
    for cls in CLASSES:
        register_class_replacing_stale(cls)
    bpy.types.Object.level5_humanoid_avatar = bpy.props.PointerProperty(type=G4HumanoidAvatarProfile)


def unregister() -> None:
    if hasattr(bpy.types.Object, "level5_humanoid_avatar"):
        del bpy.types.Object.level5_humanoid_avatar
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
