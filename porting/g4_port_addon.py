import json
import hashlib
import math
import os
import re
import shutil
import shlex
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from array import array
from pathlib import Path

import bpy
import bmesh
from mathutils import Matrix, Vector
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy.app.handlers import persistent
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup, UIList
from bpy_extras.io_utils import ImportHelper

try:
    from ..formats.g4_axes import blender_from_g4_matrix, g4_from_blender_matrix
    from .g4_roundtrip import NATIVE_ROUNDTRIP_SIGNATURE_VERSION, native_mesh_signature
    from ..shading import viewport_outline
    from .outline_regions import (
        OUTLINE_AUTHORED_KEY, OUTLINE_COLOR_ATTRIBUTE, OUTLINE_SEAM_ATTRIBUTE,
        STABLE_OUTLINE_COLOR, OUTLINE_BAND_FRACTION, native_outline_edges,
    )
except ImportError:
    from formats.g4_axes import blender_from_g4_matrix, g4_from_blender_matrix
    from porting.g4_roundtrip import NATIVE_ROUNDTRIP_SIGNATURE_VERSION, native_mesh_signature
    from shading import viewport_outline
    from porting.outline_regions import (
        OUTLINE_AUTHORED_KEY, OUTLINE_COLOR_ATTRIBUTE, OUTLINE_SEAM_ATTRIBUTE,
        STABLE_OUTLINE_COLOR, OUTLINE_BAND_FRACTION, native_outline_edges,
    )

try:
    from ..shared.g4_joint_aliases import (
        canonical_export_joint,
        canonical_skirt_joint,
        load_joint_alias_catalog,
        normalize_joint_key,
        resolve_catalog_alias,
    )
    from .g4_blender_contracts import (
        resolve_animation_joint_drivers,
        resolve_effective_joint_mappings,
    )
except ImportError:
    from shared.g4_joint_aliases import (
        canonical_export_joint,
        canonical_skirt_joint,
        load_joint_alias_catalog,
        normalize_joint_key,
        resolve_catalog_alias,
    )
    from porting.g4_blender_contracts import (
        resolve_animation_joint_drivers,
        resolve_effective_joint_mappings,
    )


ADDON_ID = __name__.split(".", 1)[0] if "." in __name__ else __name__
IS_STANDALONE_ADDON = False
MODEL_EXTENSIONS = {".g4md", ".g4pkm"}
MAX_GENERATED_TEXTURE_SIZE = 2048
FACE_ATLAS_COLUMNS = 4
FACE_ATLAS_ROWS = 2
FACE_ATLAS_SLOTS = FACE_ATLAS_COLUMNS * FACE_ATLAS_ROWS
EXPRESSION_PREVIEW_BACKUP_UV = "__G4ExpressionPreviewUV"
EXPRESSION_PREVIEW_SOURCE_UV_KEY = "g4_expression_preview_source_uv"
ATLAS_GUTTER_PIXELS = 2
G4_PORT_ADDON_MARKER = "mesh-json-v1-handoff"
G4_BLENDER_PLUGIN_VERSION_FALLBACK = (2, 0, 0)
G4_PORT_SCRIPT_VERSION_EXPECTED = "1.0.14"
STABLE_CUSTOM_VERTEX_COLOR = STABLE_OUTLINE_COLOR


def g4_blender_plugin_version() -> str:
    package = sys.modules.get(ADDON_ID)
    info = getattr(package, "bl_info", None) if package is not None else None
    version = info.get("version") if isinstance(info, dict) else None
    if isinstance(version, (list, tuple)) and version:
        return ".".join(str(int(part)) for part in version)
    return ".".join(str(part) for part in G4_BLENDER_PLUGIN_VERSION_FALLBACK)


def default_python() -> str:
    for candidate in ("/usr/bin/python3", "/opt/homebrew/bin/python3", sys.executable, "python3"):
        if candidate and Path(candidate).exists():
            return candidate
    return "python3"


def addon_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_port_script() -> str:
    env_path = os.environ.get("LEVEL5_G4_PORT")
    candidates = [Path(env_path)] if env_path else []
    root = addon_root()
    candidates.extend(
        [
            root / "porting" / "g4_port.py",
        ]
    )
    return next((str(path) for path in candidates if path.is_file()), "")


def default_config_dir() -> str:
    env_path = os.environ.get("LEVEL5_G4_PORT_CONFIGS")
    candidates = [Path(env_path)] if env_path else []
    root = addon_root()
    candidates.extend(
        [
            root,
            root / "configs",
        ]
    )
    return next((str(path) for path in candidates if path.is_dir()), "")


def default_probe_script() -> str:
    env_path = os.environ.get("LEVEL5_G4_PROBE")
    candidates = [Path(env_path)] if env_path else []
    root = addon_root()
    candidates.extend(
        [
            root / "porting" / "g4_model_probe.py",
        ]
    )
    return next((str(path) for path in candidates if path.is_file()), "")


def default_cache_dir() -> str:
    return str(Path(tempfile.gettempdir()) / "level5_g4_port_blender")


def default_output_root() -> str:
    return str(Path.home() / "level5_g4_port_package")


def addon_preferences():
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is not None:
        return addon.preferences

    class Defaults:
        python_path = default_python()
        port_script = default_port_script()
        config_dir = default_config_dir()
        raw_data_root = os.environ.get("LEVEL5_G4_RAW_ROOT", "")
        output_root = os.environ.get("LEVEL5_G4_OUT_ROOT", default_output_root())
        chara_model_xml = os.environ.get("LEVEL5_G4_CHARA_MODEL", "")
        cache_dir = default_cache_dir()
        keep_temporary_files = False

    return Defaults()


def port_log(log_path: Path | None, message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    print(f"[G4 Port] {line}", flush=True)
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except Exception:
        pass


def resolve_file(path: str, fallback: str = "") -> Path:
    configured = bpy.path.abspath(path or fallback or "")
    return Path(configured) if configured else Path()


def resolve_port_script(prefs) -> Path:
    configured = bpy.path.abspath(getattr(prefs, "port_script", "") or "")
    script = Path(configured) if configured else resolve_file(default_port_script())
    if script.is_file():
        return script
    if script == addon_root() / "g4_port.py":
        bundled = Path(default_port_script())
        if bundled.is_file():
            return bundled
    raise RuntimeError("g4_port.py was not found. Configure it in the addon preferences.")


def resolve_probe_script(prefs) -> Path:
    configured = bpy.path.abspath(getattr(prefs, "probe_script", "") or "")
    script = Path(configured) if configured else resolve_file(default_probe_script())
    if script.is_file():
        return script
    if script == addon_root() / "g4_model_probe.py":
        bundled = Path(default_probe_script())
        if bundled.is_file():
            return bundled
    raise RuntimeError("g4_model_probe.py was not found. Configure it in the addon preferences.")


def config_path(name: str, prefs=None) -> Path:
    prefs = prefs or addon_preferences()
    directory = resolve_file(getattr(prefs, "config_dir", ""), default_config_dir())
    return directory / name


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def join_csv(values) -> str:
    return ", ".join(str(item) for item in values)


def mesh_objects(selected_only: bool) -> list[bpy.types.Object]:
    objects = bpy.context.selected_objects if selected_only else bpy.data.objects
    return [obj for obj in objects if obj.type == "MESH"]


def blender_base_name(name: str) -> str:
    return name.rsplit(".", 1)[0] if name.rsplit(".", 1)[-1].isdigit() else name


def material_image_path(material) -> str:
    if material is None or material.node_tree is None:
        return ""
    for node in material.node_tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None and node.image.filepath:
            return bpy.path.abspath(node.image.filepath)
    return ""


def first_used_material_image(obj: bpy.types.Object) -> str:
    """Return the first usable diffuse image from a material actually used by the mesh."""
    if obj is None or obj.type != "MESH":
        return ""
    used_indices = []
    for polygon in obj.data.polygons:
        if polygon.material_index not in used_indices:
            used_indices.append(polygon.material_index)
    for index in used_indices:
        if 0 <= index < len(obj.data.materials):
            path = material_image_path(obj.data.materials[index])
            if path:
                return path
    return material_image_path(obj.active_material)


def used_material_summary(obj: bpy.types.Object) -> str:
    if obj is None or obj.type != "MESH":
        return "<none>"
    indices = sorted({polygon.material_index for polygon in obj.data.polygons})
    parts = []
    for index in indices:
        material = obj.data.materials[index] if 0 <= index < len(obj.data.materials) else None
        image = material_image_path(material)
        parts.append(f"{index}:{material.name if material else '<none>'}={Path(image).name if image else '<none>'}")
    return "; ".join(parts) or "<none>"


def active_material_image_path(context) -> str:
    obj = context.active_object
    if obj is None or obj.type != "MESH":
        return ""
    return material_image_path(obj.active_material)


def mesh_weights(obj: bpy.types.Object) -> dict:
    group_names = {group.index: group.name for group in obj.vertex_groups}
    influences = []
    for vertex in obj.data.vertices:
        weights = [
            [
                canonical_skirt_joint(group_names[item.group], vertex.co.x)
                or group_names[item.group],
                item.weight,
            ]
            for item in vertex.groups
            if item.group in group_names and item.weight > 0.0
        ]
        weights.sort(key=lambda item: item[1], reverse=True)
        influences.append(weights[:8])
    return {
        "name": obj.name,
        "vertex_count": len(obj.data.vertices),
        "influences": influences,
    }


def write_weights_json(path: Path, selected_only: bool) -> int:
    meshes = [mesh_weights(obj) for obj in mesh_objects(selected_only)]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "g4_blender_plugin_version": g4_blender_plugin_version(),
        "g4_port_script_version_expected": G4_PORT_SCRIPT_VERSION_EXPECTED,
        "g4_blender_addon_marker": G4_PORT_ADDON_MARKER,
        "g4_blender_addon_source": str(Path(__file__).resolve()),
        "meshes": meshes,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return len(meshes)


def remove_pose_export_collection(collection) -> None:
    if collection is None:
        return
    for obj in tuple(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)


def armature_modifier(obj: bpy.types.Object):
    return next((modifier for modifier in obj.modifiers if modifier.type == "ARMATURE" and modifier.object), None)


def t_pose_matrix(bone, head: Vector, direction: Vector) -> Matrix:
    """Keep a bone's local roll while placing its head and tail along a T-pose limb."""
    rest_head = bone.head_local.copy()
    rest_direction = bone.tail_local - rest_head
    if rest_direction.length == 0.0 or direction.length == 0.0:
        return bone.matrix_local.copy()
    rotation = rest_direction.normalized().rotation_difference(direction.normalized()).to_matrix().to_4x4()
    return Matrix.Translation(head) @ rotation @ Matrix.Translation(-rest_head) @ bone.matrix_local


def neutralize_armature_t_pose(rig: bpy.types.Object) -> list[str]:
    """Put conventional arm chains into the T-pose expected by the G4 runtime.

    This operates only on the disposable export rig.  It deliberately does not
    use G4SK bind matrices: those matrices describe runtime skinning, not an
    affine conversion to apply to every exported source vertex.
    """
    if rig is None or rig.type != "ARMATURE":
        return []
    for pose_bone in rig.pose.bones:
        pose_bone.matrix_basis.identity()
    bpy.context.view_layer.update()

    applied = []
    for side, sign in (("l", 1.0), ("r", -1.0)):
        names = {
            "arm": f"{side}_arm",
            "elbow": f"{side}_elbow",
            "hand": f"{side}_hand",
        }
        bones = {key: rig.data.bones.get(name) for key, name in names.items()}
        if any(bone is None for bone in bones.values()):
            continue
        arm = bones["arm"]
        elbow = bones["elbow"]
        hand = bones["hand"]
        shoulder = arm.head_local.copy()
        arm_length = (elbow.head_local - shoulder).length or arm.length
        forearm_length = (hand.head_local - elbow.head_local).length or elbow.length
        direction = Vector((sign, 0.0, 0.0))
        elbow_head = shoulder + direction * arm_length
        hand_head = elbow_head + direction * forearm_length
        targets = {
            arm.name: (shoulder, direction),
            elbow.name: (elbow_head, direction),
            hand.name: (hand_head, direction),
        }
        for helper_name in (
            f"{side}_arm_roll",
            f"{side}_arm_roll_02",
            f"{side}_elbow_sharp",
            f"{side}_hand_roll",
            f"{side}_hand_roll_02",
        ):
            helper = rig.data.bones.get(helper_name)
            if helper is None:
                continue
            if "arm_roll" in helper_name:
                segment_start, source_end, target_end = shoulder, elbow.head_local, elbow_head
            elif "hand_roll" in helper_name:
                segment_start, source_end, target_end = elbow.head_local, hand.head_local, hand_head
            else:
                targets[helper.name] = (elbow_head, direction)
                continue
            source_length = (source_end - segment_start).length
            fraction = 0.0 if source_length == 0.0 else (helper.head_local - segment_start).length / source_length
            targets[helper.name] = (segment_start.lerp(target_end, fraction), direction)
        for bone_name, (head, target_direction) in targets.items():
            rig.pose.bones[bone_name].matrix = t_pose_matrix(rig.data.bones[bone_name], head, target_direction)
            # Blender derives a child's basis from the evaluated parent pose.
            # Refresh after each parent-first assignment so elbow/hand do not
            # inherit a stale A-pose transform.
            bpy.context.view_layer.update()
        applied.append(side)
    bpy.context.view_layer.update()
    return applied


def export_pose_armature(source: bpy.types.Object, collection, rig_copies: dict):
    modifier = armature_modifier(source)
    if modifier is None:
        return None
    original_rig = modifier.object
    rig = rig_copies.get(original_rig.name_full)
    if rig is None:
        rig = original_rig.copy()
        rig.data = original_rig.data.copy()
        rig.matrix_world = original_rig.matrix_world.copy()
        collection.objects.link(rig)
        props = getattr(bpy.context.scene, "level5_g4_port", None)
        if props is not None and props.prepare_victorytool_profile:
            rig.animation_data_clear()
            rig.constraints.clear()
            rig.parent = None
            rig.matrix_world = original_rig.matrix_world.copy()
            rig.data.pose_position = "REST"
            port_log(None, f"Temporary export rig {original_rig.name}: using authored bind pose")
        else:
            sides = neutralize_armature_t_pose(rig)
            port_log(None, f"Temporary export rig {original_rig.name}: neutralized {'/'.join(sides) if sides else 'no standard arm chains'} to T-pose")
        rig_copies[original_rig.name_full] = rig
    return rig


def mesh_symbol(value: str, fallback: str) -> str:
    symbol = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return symbol or fallback


def modular_reference_armature(source: bpy.types.Object):
    """Find the assembled character armature that owns a compatible uniform.

    Some editable scenes keep a face on a high-resolution authoring rig and
    the uniform on the runtime rig.  A baked export must be written in the
    latter's local space or a later modular import interprets scene-space
    coordinates as native character coordinates.
    """
    source_modifier = armature_modifier(source)
    source_rig = source_modifier.object if source_modifier is not None else None
    if source_rig is None:
        return None
    source_center = source.matrix_world @ sum((Vector(corner) for corner in source.bound_box), Vector()) / 8.0
    candidates = []
    for candidate in bpy.data.objects:
        if candidate.type != "ARMATURE" or candidate == source_rig:
            continue
        model_source = str(candidate.get("g4_character_model_source", "")).replace("\\", "/").casefold()
        if "/_uniform/" not in model_source:
            continue
        if candidate.data.bones.get("c_c_1_0") is None or candidate.data.bones.get("c_head_1_0") is None:
            continue
        head_bone = candidate.data.bones.get("c_head_1_0")
        distance = (candidate.matrix_world @ head_bone.head_local - source_center).length
        candidates.append((distance, candidate.name, candidate))
    return min(candidates, key=lambda item: item[:2])[2] if candidates else None


def export_mesh_matrix(source: bpy.types.Object) -> Matrix:
    reference = modular_reference_armature(source)
    if reference is None:
        return source.matrix_world.copy()
    return reference.matrix_world.inverted_safe() @ source.matrix_world


def refresh_authored_outline_mesh(mesh: bpy.types.Mesh) -> None:
    color = tuple(value / 255 for value in STABLE_OUTLINE_COLOR)
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        old = bm.loops.layers.color.get(OUTLINE_COLOR_ATTRIBUTE)
        if old is not None:
            bm.loops.layers.color.remove(old)
        layer = bm.loops.layers.float_color.get(OUTLINE_COLOR_ATTRIBUTE)
        if layer is None:
            layer = bm.loops.layers.float_color.new(OUTLINE_COLOR_ATTRIBUTE)
        for face in bm.faces:
            for loop in face.loops:
                loop[layer] = color
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    else:
        attribute = mesh.attributes.get(OUTLINE_COLOR_ATTRIBUTE)
        if attribute is not None and (attribute.domain != "CORNER" or attribute.data_type != "FLOAT_COLOR"):
            mesh.attributes.remove(attribute)
            attribute = None
        if attribute is None:
            attribute = mesh.color_attributes.new(name=OUTLINE_COLOR_ATTRIBUTE, type="FLOAT_COLOR", domain="CORNER")
        attribute.data.foreach_set("color", color * len(mesh.loops))
        mesh.update()


def import_native_outline_marks(mesh: bpy.types.Mesh) -> None:
    if mesh.attributes.get(OUTLINE_SEAM_ATTRIBUTE) is not None or mesh.get(OUTLINE_AUTHORED_KEY):
        return
    if mesh.is_editmode:
        bm = bmesh.from_edit_mesh(mesh)
        layer = bm.loops.layers.float_color.get(OUTLINE_COLOR_ATTRIBUTE)
        if layer is None:
            layer = bm.loops.layers.color.get(OUTLINE_COLOR_ATTRIBUTE)
        if layer is None:
            return
        bm.verts.index_update()
        positions = [tuple(vertex.co) for vertex in bm.verts]
        faces = [tuple(loop.vert.index for loop in face.loops) for face in bm.faces]
        greens = [[loop[layer][1] for loop in face.loops] for face in bm.faces]
        recovered = native_outline_edges(positions, faces, greens)
        marks = bm.edges.layers.int.new(OUTLINE_SEAM_ATTRIBUTE)
        for edge in bm.edges:
            edge[marks] = int(tuple(sorted(v.index for v in edge.verts)) in recovered)
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    else:
        colors = mesh.color_attributes.get(OUTLINE_COLOR_ATTRIBUTE)
        if colors is None or colors.domain != "CORNER":
            return
        recovered = native_outline_edges(
            [tuple(v.co) for v in mesh.vertices], [tuple(face.vertices) for face in mesh.polygons],
            [[colors.data[i].color[1] for i in face.loop_indices] for face in mesh.polygons])
        marks = mesh.attributes.new(OUTLINE_SEAM_ATTRIBUTE, "INT", "EDGE")
        for edge in mesh.edges:
            marks.data[edge.index].value = int(tuple(sorted(edge.vertices)) in recovered)
    mesh["g4_outline_native"] = True
    if recovered:
        bpy.context.scene.level5_g4_port.preview_authored_outlines = True


def authored_outline_colors(mesh: bpy.types.Mesh) -> list[list[tuple[int, int, int, int]]]:
    marks = mesh.attributes.get(OUTLINE_SEAM_ATTRIBUTE)
    if marks is None or marks.domain != "EDGE" or marks.data_type != "INT":
        raise ValueError("Outline marks are missing; mark the edges again or disable Authored Outlines")
    seams = {
        tuple(edge.vertices) for edge in mesh.edges if marks.data[edge.index].value
    }
    surface_edges = {tuple(sorted((a, b))) for face in mesh.polygons
                     for a, b in zip(face.vertices, tuple(face.vertices[1:]) + (face.vertices[0],))}
    if {tuple(sorted(edge)) for edge in seams} - surface_edges:
        raise ValueError("Outline marks require surface faces; remove marks from loose edges")
    # The red overlay shows authoring marks; export builds the narrow bands.
    return [[STABLE_OUTLINE_COLOR] * len(face.vertices) for face in mesh.polygons]


class LEVEL5_G4PORT_OT_mark_outline(Operator):
    bl_idname = "level5_g4_port.mark_outline"
    bl_label = "Mark Outline"
    bl_description = "Mark selected surface edges for outline export, independently of UV seams"
    bl_options = {"REGISTER", "UNDO"}

    clear: BoolProperty(name="Clear", default=False)

    @classmethod
    def poll(cls, context):
        return context.mode == "EDIT_MESH" and len(context.objects_in_mode_unique_data) == 1

    def execute(self, context):
        mesh = context.edit_object.data
        bm = bmesh.from_edit_mesh(mesh)
        if not any(edge.select and not edge.hide for edge in bm.edges):
            self.report({"ERROR"}, "Select the edges to mark first")
            return {"CANCELLED"}
        layer = bm.edges.layers.int.get(OUTLINE_SEAM_ATTRIBUTE)
        if layer is None:
            layer = bm.edges.layers.int.new(OUTLINE_SEAM_ATTRIBUTE)
        selected = [edge for edge in bm.edges if edge.select and not edge.hide]
        for edge in selected:
            edge[layer] = 0 if self.clear else 1
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        count = len(selected)
        mesh[OUTLINE_AUTHORED_KEY] = True
        refresh_authored_outline_mesh(mesh)
        context.scene.level5_g4_port.use_authored_outlines = True
        context.scene.level5_g4_port.preview_authored_outlines = True
        viewport_outline.configure_authored(context.scene, True)
        self.report({"INFO"}, f"{'Cleared' if self.clear else 'Marked'} {count} outline edges")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_select_outline(Operator):
    bl_idname = "level5_g4_port.select_outline"
    bl_label = "Select Outlines"
    bl_description = "Select the stored outline boundaries"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return LEVEL5_G4PORT_OT_mark_outline.poll(context)

    def execute(self, context):
        mesh = context.edit_object.data
        bm = bmesh.from_edit_mesh(mesh)
        layer = bm.edges.layers.int.get(OUTLINE_SEAM_ATTRIBUTE)
        bpy.ops.mesh.select_all(action="DESELECT")
        bpy.ops.mesh.select_mode(type="EDGE")
        if layer is not None:
            for edge in bm.edges:
                if edge[layer] and not edge.hide:
                    edge.select_set(True)
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_apply_outline_regions(Operator):
    bl_idname = "level5_g4_port.apply_outline_regions"
    bl_label = "Refresh Outlines"
    bl_description = "Refresh outline parameters from marked edges"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (context.active_object is not None and context.active_object.type == "MESH"
                and (context.mode == "OBJECT" or LEVEL5_G4PORT_OT_mark_outline.poll(context)))

    def execute(self, context):
        mesh = context.active_object.data
        edit_mode = context.mode == "EDIT_MESH"
        if edit_mode:
            bpy.ops.object.mode_set(mode="OBJECT")
        try:
            authored_outline_colors(mesh)
            mesh[OUTLINE_AUTHORED_KEY] = True
            refresh_authored_outline_mesh(mesh)
            context.scene.level5_g4_port.use_authored_outlines = True
            context.scene.level5_g4_port.preview_authored_outlines = True
            viewport_outline.configure_authored(context.scene, True)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            if edit_mode:
                bpy.ops.object.mode_set(mode="EDIT")
        message = "Outline marks applied; preview and authored export enabled"
        self.report({"INFO"}, message)
        return {"FINISHED"}


def update_authored_outline_preview(self, context) -> None:
    viewport_outline.configure_authored(self.id_data, self.preview_authored_outlines)
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def export_port_meshes(path: Path, selected_only: bool, apply_modifiers: bool) -> None:
    objects = mesh_objects(selected_only)
    if not objects:
        raise RuntimeError("No mesh objects were found to export.")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    conversion = g4_from_blender_matrix()
    settings = getattr(bpy.context.scene, "level5_g4_port", None)
    if settings is not None and settings.prepare_victorytool_profile:
        try:
            from ..animation.humanoid_avatar_pose import avatar_export_alignment
        except ImportError:
            from animation.humanoid_avatar_pose import avatar_export_alignment
        original = resolve_file(settings.original_model)
        skeleton = active_rig_skeleton_path(settings, original) if original.is_file() else None
        conversion = conversion @ avatar_export_alignment(
            export_armature(), skeleton.read_bytes() if skeleton is not None else None
        )
    authored = getattr(settings, "use_authored_outlines", False)
    geometries = []
    for object_index, obj in enumerate(objects):
        source_name = str(obj.get("g4_source_mesh_name", obj.name))
        evaluated = obj.evaluated_get(depsgraph) if apply_modifiers else obj
        mesh = evaluated.data
        mesh.calc_loop_triangles()
        face_colors = authored_outline_colors(mesh) if authored and obj.data.get(OUTLINE_AUTHORED_KEY) else []
        colors = [None] * len(mesh.loops)
        for face, corner_colors in zip(mesh.polygons, face_colors):
            for loop_index, color in zip(face.loop_indices, corner_colors):
                colors[loop_index] = [value / 255 for value in color]
        object_matrix = conversion @ obj.matrix_world
        normal_matrix = object_matrix.to_3x3().inverted_safe().transposed()
        positions = [list(object_matrix @ vertex.co) for vertex in mesh.vertices]
        normals = []
        for loop in mesh.loops:
            normal = normal_matrix @ loop.normal
            normals.append(list(normal.normalized() if normal.length_squared else loop.normal))
        uv_layer = mesh.uv_layers.active
        triangles_by_material = {}
        for triangle in mesh.loop_triangles:
            triangles_by_material.setdefault(triangle.material_index, []).append(triangle)
        primitives = []
        for material_index, triangles in sorted(triangles_by_material.items()):
            corners = []
            for triangle in triangles:
                for loop_index in triangle.loops:
                    corners.append({
                        "vertex": mesh.loops[loop_index].vertex_index,
                        "normal": normals[loop_index],
                        "uv": list(uv_layer.data[loop_index].uv) if uv_layer else [0.0, 0.0],
                        "color": colors[loop_index],
                    })
            material = mesh.materials[material_index] if material_index < len(mesh.materials) else None
            primitives.append({
                "material": mesh_symbol(material.name if material else f"material_{material_index}", f"material_{material_index}"),
                "corners": corners,
            })
        marks = mesh.attributes.get(OUTLINE_SEAM_ATTRIBUTE) if face_colors else None
        seams = [list(edge.vertices) for edge in mesh.edges if marks and marks.data[edge.index].value]
        geometries.append({
            "id": f"g4_{object_index}_{mesh_symbol(source_name, 'mesh')}",
            "name": source_name,
            "positions": positions,
            "primitives": primitives,
            "seams": seams,
            "outline_width": OUTLINE_BAND_FRACTION * settings.outline_width if settings else OUTLINE_BAND_FRACTION,
        })
    path.write_text(json.dumps({"format": "g4-blender-port-meshes", "version": 1, "meshes": geometries}), encoding="utf-8")

def export_port_meshes_from_scene(
    path: Path,
    selected_only: bool,
    apply_modifiers: bool,
    bake_current_pose: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    authored = getattr(getattr(bpy.context.scene, "level5_g4_port", None), "use_authored_outlines", False)
    if authored:
        for obj in mesh_objects(selected_only):
            obj.update_from_editmode()
    def run_export():
        export_port_meshes(path, selected_only, apply_modifiers)

    if not bake_current_pose:
        run_export()
        return

    remove_pose_export_collection(bpy.data.collections.get("__G4PoseExport"))
    collection = bpy.data.collections.new("__G4PoseExport")
    bpy.context.scene.collection.children.link(collection)
    original_selected = tuple(bpy.context.selected_objects)
    original_active = bpy.context.view_layer.objects.active
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        sources = mesh_objects(selected_only)
        if not sources:
            raise RuntimeError("No mesh objects were found to bake for export.")
        copies = []
        rig_copies = {}
        for source in sources:
            pose_source = source.copy()
            pose_source.data = source.data.copy()
            pose_source.matrix_world = source.matrix_world
            collection.objects.link(pose_source)
            temporary_rig = export_pose_armature(source, collection, rig_copies)
            for modifier in pose_source.modifiers:
                # The game creates its outline from G4 vertex/shader data.
                # Exporting Blender Solidify duplicates the surface and loses
                # its one-to-one skin correspondence, producing a T-pose G4.
                if modifier.type == "SOLIDIFY" or (
                    modifier.name == "G4 edge2 Preview" and pose_source.get("g4_edge2_preview")
                ):
                    modifier.show_viewport = False
                elif modifier.type == "ARMATURE":
                    modifier.object = temporary_rig
                elif not apply_modifiers and modifier.type != "ARMATURE":
                    modifier.show_viewport = False
            bpy.context.view_layer.update()
            mesh = bpy.data.meshes.new_from_object(pose_source.evaluated_get(depsgraph), depsgraph=depsgraph)
            if source.data.get(OUTLINE_AUTHORED_KEY):
                mesh[OUTLINE_AUTHORED_KEY] = True
            copy = bpy.data.objects.new(source.name, mesh)
            copy["g4_source_mesh_name"] = source.name
            copy.matrix_world = export_mesh_matrix(source)
            collection.objects.link(copy)
            copies.append(copy)
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        for copy in copies:
            copy.select_set(True)
        bpy.context.view_layer.objects.active = copies[0]
        export_port_meshes(path, True, False)
    finally:
        for obj in tuple(bpy.context.selected_objects):
            obj.select_set(False)
        remove_pose_export_collection(bpy.data.collections.get("__G4PoseExport"))
        for obj in original_selected:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if original_active is not None and original_active.name in bpy.data.objects:
            bpy.context.view_layer.objects.active = original_active


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Could not read {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON preset: {path}") from exc


TEXTURE_MODE_ITEMS = [
    ("custom", "Rebuild Custom G4TX", "Copy the original G4TX and replace selected texture entries"),
    ("native", "Use Native G4TX", "Copy the original G4TX files from the raw data root"),
    ("keep", "Keep Existing G4TX", "Preserve the G4TX already present in the output folder"),
]

TEXTURE_PLATFORM_ITEMS = [
    ("auto", "Automatic (DX11, then NX)", "Use DX11 when available, otherwise Nintendo Switch NX"),
    ("dx11", "Windows / DX11", "Read and write DDS payloads under data/dx11"),
    ("nx", "Nintendo Switch / NX", "Read and write NXTCH payloads under data/nx"),
]


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def parse_g4tx_entries(path: Path) -> list[dict]:
    data = path.read_bytes()
    if data[:4] != b"G4TX":
        raise ValueError(f"{path} is not a G4TX")
    texture_count = struct.unpack_from("<H", data, 0x20)[0]
    total_count = struct.unpack_from("<H", data, 0x22)[0]
    sub_count = data[0x25]
    pos = 0x60
    entries = []
    for index in range(texture_count):
        raw = data[pos : pos + 0x30]
        values = struct.unpack_from("<IIIIIIHHI", raw, 0)
        entries.append({"index": index, "width": values[6] or 1024, "height": values[7] or 1024})
        pos += 0x30
    pos += sub_count * 0x18
    pos = align(pos, 0x10)
    pos += total_count * 4
    pos = align(pos + total_count, 4)
    string_base = pos
    offsets = list(struct.unpack_from("<" + "H" * total_count, data, pos))
    for entry, offset in zip(entries, offsets[:texture_count]):
        start = string_base + offset
        end = data.find(b"\0", start)
        if end > start:
            entry["name"] = data[start:end].decode("ascii", errors="replace")
    return [entry for entry in entries if entry.get("name")]


def parse_g4tx_names(path: Path) -> list[str]:
    return [entry["name"] for entry in parse_g4tx_entries(path)]


def infer_data_root(path: Path) -> Path | None:
    resolved = path.resolve()
    for parent in resolved.parents:
        if parent.name == "data" and (parent / "common").is_dir():
            return parent
    parts = resolved.parts
    if "common" in parts:
        index = parts.index("common")
        return Path(*parts[:index]) if index > 0 else None
    for platform in ("dx11", "nx"):
        if platform in parts:
            index = parts.index(platform)
            return Path(*parts[:index]) if index > 0 else None
    return None


def relative_model_from_data(path: Path, data_root: Path) -> str:
    rel = path.with_suffix("").resolve().relative_to(data_root.resolve())
    parts = rel.parts
    if parts and parts[0] in {"common", "dx11", "nx"}:
        parts = parts[1:]
    return Path(*parts).as_posix()


def dx11_g4tx_for_model(data_root: Path, model_rel: str) -> Path:
    return data_root / "dx11" / Path(model_rel).with_suffix(".g4tx")


def nx_g4tx_for_model(data_root: Path, model_rel: str) -> Path:
    return data_root / "nx" / Path(model_rel).with_suffix(".g4tx")


def common_g4tx_for_model(data_root: Path, model_rel: str) -> Path:
    return data_root / "common" / Path(model_rel).with_suffix(".g4tx")


def texture_key_for_record(record_name: str, texture_names: list[str]) -> str:
    if record_name in texture_names:
        return record_name
    prefix_matches = [name for name in texture_names if name == record_name or name.startswith(f"{record_name}_")]
    if prefix_matches:
        return prefix_matches[0]
    stem = record_name.rsplit("_", 1)[0]
    stem_matches = [name for name in texture_names if name == stem or name.startswith(f"{stem}_")]
    return stem_matches[0] if stem_matches else ""


def shared_face_texture_key(texture_names: list[str]) -> str:
    """Return the native base map shared by the eye and mouth expression meshes."""
    return next((name for name in texture_names if re.search(r"_10$", name) and not is_special_texture(name)), "")


def face_texture_is_shared(records, texture_names: list[str]) -> bool:
    texture_name = shared_face_texture_key(texture_names)
    return bool(texture_name and any(
        is_face_atlas_record(record) and record.texture_key == texture_name for record in records
    ))


def is_face_atlas_record(record) -> bool:
    name = record.output_name.lower()
    material = record.material_name.lower().removesuffix("m")
    return name in {"eye_10", "mouth_10"} or material in {"eye_10", "mouth_10"}


def resolved_record_texture_key(props, record) -> str:
    """Return the effective native texture key for a record.

    eye_10 and mouth_10 share one native *_10 texture.  Older scene/preset
    state can retain the record names themselves as texture_key, which makes
    the expression atlas invisible to serialization.  Resolve that association
    from the native texture table instead of trusting stale scene state.
    """
    if is_face_atlas_record(record):
        shared = shared_face_texture_key([entry.texture_name for entry in props.texture_entries])
        if shared:
            return shared
    return record.texture_key


def normalize_shared_face_record_keys(props) -> int:
    """Repair stale eye/mouth texture_key state outside Blender draw contexts."""
    shared = shared_face_texture_key([entry.texture_name for entry in props.texture_entries])
    if not shared:
        return 0
    changed = 0
    for record in props.records:
        if is_face_atlas_record(record) and record.texture_key != shared:
            record.texture_key = shared
            changed += 1
    return changed


def face_pool_atlas_active(props, record) -> bool:
    """Return whether the record uses an atlas built from the expression pool."""
    if not is_face_atlas_record(record):
        return False
    entry = texture_entry(props, resolved_record_texture_key(props, record))
    return bool(
        entry
        and entry.expression_atlas
        and entry.expression_atlas_mode == "pool"
    )


def fit_expression_record_uv(
    bounds: tuple[float, float, float, float],
    target_display_rect: tuple[float, float, float, float],
    flip_y: bool = True,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Fit source UV bounds into an atlas rect as Blender displays it after G4 import.

    G4 export flips V for custom atlases and the importer flips stored V back for
    Blender/DDS coordinates.  This helper therefore returns the *stored G4*
    scale/offset that round-trips into the requested displayed rectangle.
    """
    min_u, max_u, min_v, max_v = bounds
    origin_u, origin_v, width, height = target_display_rect
    bounds_u = max(max_u - min_u, 0.0001)
    bounds_v = max(max_v - min_v, 0.0001)
    scale = min(width / bounds_u, height / bounds_v)
    used_u = bounds_u * scale
    used_v = bounds_v * scale

    display_offset_u = origin_u + (width - used_u) * 0.5 - min_u * scale
    display_min_v = origin_v + (height - used_v) * 0.5
    if flip_y:
        display_offset_v = display_min_v - min_v * scale
        stored_offset_v = 1.0 - scale - display_offset_v
    else:
        stored_offset_v = 1.0 - max_v * scale - display_min_v
    return (scale, scale), (display_offset_u, stored_offset_v)


def expression_record_source_bounds(record) -> tuple[float, float, float, float] | None:
    """Return the union of the authored UV bounds for objects assigned to one record."""
    bounds = [object_uv_bounds(obj) for obj in objects_for_record(record)]
    bounds = [item for item in bounds if item is not None]
    if not bounds:
        return None
    return (
        min(item[0] for item in bounds),
        max(item[1] for item in bounds),
        min(item[2] for item in bounds),
        max(item[3] for item in bounds),
    )


def expression_tile_record_uv(
    target_display_rect: tuple[float, float, float, float],
    flip_y: bool = True,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Move a 0..1 source layout into an atlas rect without changing its relative UVs."""
    origin_u, origin_v, scale_u, scale_v = target_display_rect
    # With the normal G4 V flip, importer roundtrip is:
    # displayedV = scaleV * sourceV + (1 - scaleV - storedOffsetV).
    # Solve for storedOffsetV so displayed origin is the requested atlas row.
    stored_offset_v = 1.0 - scale_v - origin_v
    return (scale_u, scale_v), (origin_u, stored_offset_v)


def expression_pool_record_uv(
    record_name: str,
    target_display_rect: tuple[float, float, float, float],
    flip_y: bool = True,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Place a face-pool record in Cell 1 without fitting its mesh UV bounds.

    The expression images are full source textures.  eye_10 and mouth_10 must
    therefore preserve their authored 0..1 UV coordinates and only translate
    that complete texture domain into the atlas cell.  Fitting eye_10 to its
    mesh bounds moves the eye features relative to the texture and is wrong.
    """
    if record_name.lower() not in {"eye_10", "mouth_10"}:
        raise ValueError(f"unsupported expression-pool record {record_name!r}")
    return expression_tile_record_uv(target_display_rect, flip_y)


def face_expression_record_uv(props, record) -> tuple[tuple[float, float], tuple[float, float]]:
    """Resolve the UV window used by eye_10/mouth_10 for expression cell 1.

    Pool atlases prefer per-object source UV transforms, but we still expose
    the precise first-cell rectangle here as a fallback. Authored existing
    atlases retain their source UV domain and never call this path.
    """
    if not face_pool_atlas_active(props, record):
        return (1.0, 1.0), (0.0, 0.0)

    entry = texture_entry(props, resolved_record_texture_key(props, record))
    if entry is not None and entry.expression_cell_scale_u > 0.0 and entry.expression_cell_scale_v > 0.0:
        return (
            (float(entry.expression_cell_scale_u), float(entry.expression_cell_scale_v)),
            (float(entry.expression_cell_origin_u), float(entry.expression_cell_origin_v)),
        )

    return (
        (1.0 / FACE_ATLAS_COLUMNS, 1.0 / FACE_ATLAS_ROWS),
        (0.0, (FACE_ATLAS_ROWS - 1.0) / FACE_ATLAS_ROWS),
    )


def face_expression_prefit_rect(props, record) -> tuple[float, float, float, float]:
    """Return the pre-flip atlas rect that should be fitted into by source UVs."""
    (scale_u, scale_v), (origin_u, origin_v) = face_expression_record_uv(props, record)
    pre_u = 1.0 - origin_u - scale_u if props.global_uv_flip_x else origin_u
    pre_v = 1.0 - origin_v - scale_v if props.global_uv_flip_y else origin_v
    return pre_u, pre_v, scale_u, scale_v


def apply_face_expression_object_transforms(props, record, log_path: Path | None = None) -> None:
    """Fit assigned face/mouth meshes into expression cell 1 before the global flip."""
    if not face_pool_atlas_active(props, record):
        return
    pre_u, pre_v, scale_u, scale_v = face_expression_prefit_rect(props, record)
    for obj in objects_for_record(record):
        set_object_uv_fit(obj, pre_u, pre_v, scale_u, scale_v)
        port_log(
            log_path,
            f"{record.output_name}: pre-fit expression rect -> {obj.name} "
            f"scale=({obj.level5_g4_port.uv_scale_u:.6f},{obj.level5_g4_port.uv_scale_v:.6f}) "
            f"offset=({obj.level5_g4_port.uv_offset_u:.6f},{obj.level5_g4_port.uv_offset_v:.6f})",
        )


def inject_expression_atlas_uv_into_config(
    config: dict,
    texture_name: str,
    scale: tuple[float, float],
    offset: tuple[float, float],
    replacement_name: str,
) -> int:
    """Make the generated config authoritative for the shared 4x2 face atlas.

    This deliberately does not depend on Blender record state.  By the time this
    runs we already know which texture file is the expression atlas, so eye_10
    and mouth_10 must sample Cell 1 in the serialized config even if temporary
    UI/DAE state lost their record properties.
    """
    changed = 0
    for record in config.get("records", []):
        name = str(record.get("output_name", "")).lower()
        material = str(record.get("material_name", "")).lower()
        if material.endswith("m"):
            material = material[:-1]
        if name not in {"eye_10", "mouth_10"} and material not in {"eye_10", "mouth_10"}:
            continue
        record["uv_scale"] = [float(scale[0]), float(scale[1])]
        record["uv_offset"] = [float(offset[0]), float(offset[1])]
        record.pop("source_uv_transforms", None)
        changed += 1
    if texture_name and replacement_name:
        config.setdefault("texture_replacements", {})[texture_name] = replacement_name
    return changed


def assign_shared_face_texture_key(records, texture_names: list[str]) -> str:
    key = shared_face_texture_key(texture_names)
    if not key:
        return ""
    for record in records:
        if is_face_atlas_record(record):
            record.texture_key = key
    return key


def target_mesh_items(self, context):
    props = settings(context)
    items = [("__none__", "Unassigned", "Do not export this object into a native mesh")]
    for record in props.records:
        label = record.output_name or f"mesh_{record.original_index}"
        items.append((label, label, record.material_name or "Original mesh"))
    return items


def guess_joint_alias(group_name: str, allowed_joints: set[str] | None = None) -> str:
    key = normalize_joint_key(group_name)
    if allowed_joints is not None:
        exact = next((name for name in allowed_joints if name.casefold() == key), None)
        if exact:
            return exact
    canonical = canonical_export_joint(group_name)
    if canonical and (
        allowed_joints is None
        or canonical in allowed_joints
        or canonical == "c_head_1_0"
    ):
        return canonical
    try:
        catalog = load_joint_alias_catalog()
    except ValueError as exc:
        print(f"[G4 Port] Joint alias catalog unavailable: {exc}", flush=True)
        catalog = None
    if catalog is not None:
        target = resolve_catalog_alias(group_name, catalog, allowed_joints)
        if target:
            return target
    if "head" in key:
        target = "c_head_1_0"
    elif "neck" in key:
        target = "c_n_1_0"
    else:
        return ""
    if allowed_joints is None or target in allowed_joints:
        return target
    return ""


def vertex_group_names(selected_only: bool = False) -> list[str]:
    names = []
    for obj in mesh_objects(selected_only):
        names.extend(group.name for group in obj.vertex_groups)
    return sorted(dict.fromkeys(names))


class G4PortPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    python_path: StringProperty(
        name="Python",
        subtype="FILE_PATH",
        default=default_python(),
        description="Python executable used to run g4_port.py",
    )
    port_script: StringProperty(
        name="G4 Port Script",
        subtype="FILE_PATH",
        default=default_port_script(),
        description="Path to bundled or external g4_port.py; bundled installations detect it automatically",
    )
    config_dir: StringProperty(
        name="Preset Folder",
        subtype="DIR_PATH",
        default=default_config_dir(),
        description="Folder containing G4 port presets",
    )
    probe_script: StringProperty(
        name="Model Probe Script",
        subtype="FILE_PATH",
        default=default_probe_script(),
        description="Path to bundled or external g4_model_probe.py; used to build records from an original model",
    )
    raw_data_root: StringProperty(
        name="Raw Data Root",
        subtype="DIR_PATH",
        default=os.environ.get("LEVEL5_G4_RAW_ROOT", ""),
        description="Fallback data root containing common/ and dx11/. The selected original model normally defines this automatically",
    )
    output_root: StringProperty(
        name="Package Folder",
        subtype="DIR_PATH",
        default=os.environ.get("LEVEL5_G4_OUT_ROOT", default_output_root()),
        description="Destination folder. The addon writes a data/common and data/dx11 filesystem inside it",
    )
    chara_model_xml: StringProperty(
        name="Chara Model XML",
        subtype="FILE_PATH",
        default=os.environ.get("LEVEL5_G4_CHARA_MODEL", ""),
        description="Optional chara_model XML used by g4_port.py to resolve skeletons",
    )
    cache_dir: StringProperty(
        name="Export Cache",
        subtype="DIR_PATH",
        default=default_cache_dir(),
        description="Temporary folder for mesh data, weights, generated presets and reports",
    )
    keep_temporary_files: BoolProperty(
        name="Keep Temporary Files",
        default=False,
        description="Keep generated mesh data/config/weights files after export",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "python_path")
        layout.prop(self, "port_script")
        layout.prop(self, "config_dir")
        layout.prop(self, "probe_script")
        layout.prop(self, "raw_data_root")
        layout.prop(self, "output_root")
        layout.prop(self, "chara_model_xml")
        layout.prop(self, "cache_dir")
        layout.prop(self, "keep_temporary_files")


class G4PortObjectSettings(PropertyGroup):
    target_record: EnumProperty(name="Target Mesh", items=target_mesh_items)
    source_texture: StringProperty(
        name="Atlas Source",
        subtype="FILE_PATH",
        default="",
        description="Optional image override. Empty uses the first diffuse image from a material used by this mesh",
    )
    uv_scale_u: FloatProperty(name="U Scale", default=1.0, min=0.0001, soft_max=1.0)
    uv_scale_v: FloatProperty(name="V Scale", default=1.0, min=0.0001, soft_max=1.0)
    uv_offset_u: FloatProperty(name="U Offset", default=0.0, soft_min=0.0, soft_max=1.0)
    uv_offset_v: FloatProperty(name="V Offset", default=0.0, soft_min=0.0, soft_max=1.0)
    material_uv_transforms: StringProperty(default="{}", options={"HIDDEN"})


class G4PortJointAlias(PropertyGroup):
    source_group: StringProperty(name="Blender Group", default="")
    target_joint: StringProperty(name="Level-5 Joint", default="")


class G4PortJointTarget(PropertyGroup):
    name: StringProperty()


class G4PortTextureReplacement(PropertyGroup):
    texture_name: StringProperty(name="Texture", default="")
    replacement_path: StringProperty(
        name="Replacement",
        subtype="FILE_PATH",
        default="",
        description="Leave empty to preserve this G4TX texture",
    )
    atlas_signature: StringProperty(default="", options={"HIDDEN"})
    atlas_summary: StringProperty(default="", options={"HIDDEN"})
    expression_atlas: BoolProperty(default=False, options={"HIDDEN"})
    expression_atlas_mode: EnumProperty(
        items=[("pool", "Pool", "Built from the eight expression-pool cells"), ("existing", "Existing", "Use an already prepared 4x2 facial atlas")],
        default="pool",
        options={"HIDDEN"},
    )
    expression_cell_origin_u: FloatProperty(default=0.0, options={"HIDDEN"})
    expression_cell_origin_v: FloatProperty(default=0.0, options={"HIDDEN"})
    expression_cell_scale_u: FloatProperty(default=0.0, options={"HIDDEN"})
    expression_cell_scale_v: FloatProperty(default=0.0, options={"HIDDEN"})


class G4PortExpressionImage(PropertyGroup):
    image_path: StringProperty(name="Expression", subtype="FILE_PATH", default="")


def refresh_joint_target_options(props, context) -> None:
    try:
        joints = configured_target_joints(props) or set()
    except (OSError, ValueError, struct.error):
        joints = set()
    props.joint_target_options.clear()
    for name in sorted(joints):
        props.joint_target_options.add().name = name


class G4PortRecord(PropertyGroup):
    output_name: StringProperty(name="Output", default="c01000010_20")
    material_name: StringProperty(name="Material", default="c01000010_20M")
    match_names: StringProperty(name="Objects", default="*", description="Comma-separated object or material names")
    original_index: IntProperty(name="Original Mesh", default=-1, min=-1)
    texture_key: StringProperty(name="Texture Key", default="", description="Native texture name to replace in custom mode")
    texture_file: StringProperty(name="Texture File", subtype="FILE_PATH", default="")
    uv_flip_x: BoolProperty(name="Flip U", default=False)
    uv_flip_y: BoolProperty(name="Flip V", default=False)
    uv_scale_u: FloatProperty(name="U Scale", default=1.0, min=0.0001, soft_max=1.0)
    uv_scale_v: FloatProperty(name="V Scale", default=1.0, min=0.0001, soft_max=1.0)
    uv_offset_u: FloatProperty(name="U Offset", default=0.0, soft_min=0.0, soft_max=1.0)
    uv_offset_v: FloatProperty(name="V Offset", default=0.0, soft_min=0.0, soft_max=1.0)
    fallback_degenerate: BoolProperty(name="Fallback Triangle", default=True)
    rigid_joint: StringProperty(name="Default Joint", default="c_c_1_0")
    auto_palette: BoolProperty(name="Auto Palette", default=True)
    force_layout_material: BoolProperty(name="Force Layout/Material", default=False)
    layout_index: IntProperty(name="Layout", default=1, min=0)
    material_index: IntProperty(name="Material Slot", default=1, min=0)
    secondary_weight_scale: FloatProperty(
        name="Dynamic Weight",
        default=1.0,
        min=0.0,
        max=1.0,
        description="Scale applied to non-anchor weights; 1 keeps the source rig unchanged",
    )
    weight_anchor_joint: StringProperty(name="Anchor Joint", default="")
    outline_enabled: BoolProperty(
        name="Outline",
        default=True,
        description="Keep the native edge2 outline for this submesh",
    )

    def to_config(self, include_source_uv_transforms: bool = False) -> dict:
        item = {
            "output_name": self.output_name,
            "material_name": self.material_name,
            "match_names": split_csv(self.match_names) or ["*"],
            "fallback_degenerate": self.fallback_degenerate,
        }
        if self.uv_flip_x or self.uv_flip_y:
            item["uv_flip"] = [self.uv_flip_x, self.uv_flip_y]
        if self.uv_scale_u != 1.0 or self.uv_scale_v != 1.0:
            item["uv_scale"] = [self.uv_scale_u, self.uv_scale_v]
        if self.uv_offset_u or self.uv_offset_v:
            item["uv_offset"] = [self.uv_offset_u, self.uv_offset_v]
        if include_source_uv_transforms:
            source_uv_transforms = {}
            for obj in objects_for_record(self):
                uv = obj.level5_g4_port
                if uv.uv_scale_u != 1.0 or uv.uv_scale_v != 1.0 or uv.uv_offset_u or uv.uv_offset_v:
                    source_uv_transforms[obj.name] = {
                        "scale": [uv.uv_scale_u, uv.uv_scale_v],
                        "offset": [uv.uv_offset_u, uv.uv_offset_v],
                    }
                material_transforms = json.loads(uv.material_uv_transforms or "{}")
                if not isinstance(material_transforms, dict):
                    raise ValueError(f"Invalid material UV transforms on {obj.name!r}")
                for material_name, transform in material_transforms.items():
                    source_uv_transforms[f"{obj.name}::{material_name}"] = transform
            if source_uv_transforms:
                item["source_uv_transforms"] = source_uv_transforms
        if self.force_layout_material:
            item["force_layout_material"] = [self.layout_index, self.material_index]
        if self.rigid_joint:
            item["rigid_joint"] = self.rigid_joint
        if self.auto_palette:
            item["auto_palette"] = True
        if self.secondary_weight_scale != 1.0:
            item["secondary_weight_scale"] = self.secondary_weight_scale
            if self.weight_anchor_joint:
                item["weight_anchor_joint"] = self.weight_anchor_joint
        if not self.outline_enabled:
            item["outline_enabled"] = False
        return item


class G4PortSceneSettings(PropertyGroup):
    model_rel: StringProperty(
        name="Model Path",
        default="chr/_face/01_IE1/c01000010/c01000010",
        description="Path inside common/chr without extension",
    )
    native_material_names: StringProperty(
        name="Native Materials",
        default="c01000010_20M, mouth_10M, eye_10M",
    )
    texture_replacements: StringProperty(
        name="Texture Replacements",
        default="c01000010_20=hairTexture.png, c01000010_10=faceTexture.png",
        description="Comma-separated texture=filename pairs",
    )
    original_model: StringProperty(
        name="Original Model",
        subtype="FILE_PATH",
        default="",
        description="Original G4MD/G4PKM used as a record/material template",
    )
    rig_reference_model: StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    target_g4sk_path: StringProperty(
        name="G4SK Template",
        subtype="FILE_PATH",
        default="",
        description="Use this G4SK as a format template; VictoryTool export rebuilds its joints from the target armature",
        update=refresh_joint_target_options,
    )
    target_joint_names: StringProperty(
        default="",
        options={"HIDDEN"},
        update=refresh_joint_target_options,
    )
    joint_target_options: CollectionProperty(type=G4PortJointTarget, options={"HIDDEN"})
    prepare_victorytool_profile: BoolProperty(
        name="Prepare animations for VictoryTool",
        default=False,
        description="Export the model and adaptation profile for VictoryTool to prepare its animations",
    )
    export_chr_animations: BoolProperty(name="Character animations (chr)", default=True)
    export_event_animations: BoolProperty(name="Event animations (event)", default=True)
    use_preset_file: BoolProperty(
        name="Use Preset File Directly",
        default=True,
        description="Use the selected JSON preset without rewriting it from the UI records",
    )
    preset_file: StringProperty(
        name="Preset",
        subtype="FILE_PATH",
        default="",
        description="Optional JSON preset. Empty uses the settings inferred from the selected original model",
    )
    texture_mode: EnumProperty(name="Textures", items=TEXTURE_MODE_ITEMS, default="custom")
    texture_platform: EnumProperty(name="Platform", items=TEXTURE_PLATFORM_ITEMS, default="auto")
    texture_source_dir: StringProperty(name="Texture Source Folder", subtype="DIR_PATH", default="")
    texture_entries: CollectionProperty(type=G4PortTextureReplacement)
    expression_pool: CollectionProperty(type=G4PortExpressionImage)
    generate_png_set_on_export: BoolProperty(
        name="Regenerate Atlas On Export",
        default=False,
        description="Regenerate only missing or outdated prepared atlases before exporting a custom G4TX",
    )
    use_source_uv_transforms: BoolProperty(
        default=True,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    auto_pack_source_uvs: BoolProperty(
        default=True,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    replace_special_textures: BoolProperty(
        name="Replace Special Maps",
        default=False,
        description="Write neutral replacements for line/oc/sp/dp/spm maps instead of keeping bundled G4TX payloads",
    )
    disable_edge2: BoolProperty(
        name="Desactivar edge2",
        default=False,
        description="Disable the native edge2 outline displacement for every exported record",
    )
    outline_width: FloatProperty(
        name="Stroke Width", default=1.0, min=0.2, max=4.0,
        description="Relative width of custom exported outline bands; native edge2 displacement is unchanged",
    )
    use_authored_outlines: BoolProperty(
        name="Authored Outlines",
        default=False,
        description="Export the applied outline regions on custom meshes (experimental game rendering)",
    )
    preview_authored_outlines: BoolProperty(
        name="Preview Outlines",
        default=False,
        description="Show applied outline regions in Solid, Material Preview and Rendered viewports",
        update=update_authored_outline_preview,
    )
    preserve_native_roundtrip: BoolProperty(
        name="Preserve Untouched Native Import",
        default=True,
        description="Copy the original G4MD/G4MG/G4TX byte-for-byte when all assigned imported meshes are unchanged",
    )
    selected_only: BoolProperty(name="Selected Meshes Only", default=False)

    analyze_only: BoolProperty(name="Analyze Only", default=False)
    generate_tangents: BoolProperty(name="Generate Tangents", default=True)
    strict_skinning: BoolProperty(
        name="Strict Skinning",
        default=False,
        description="Fail when source weights cannot be represented; disable for foreign rigs that should fall back to Default Joint",
    )
    global_uv_flip_x: BoolProperty(name="Global Flip U", default=False)
    global_uv_flip_y: BoolProperty(name="Global Flip V", default=True)
    records: CollectionProperty(type=G4PortRecord)
    active_record: IntProperty(default=0)
    joint_aliases: CollectionProperty(type=G4PortJointAlias)
    active_joint_alias: IntProperty(default=0)
    template_signature: StringProperty(default="")
    texture_names: StringProperty(name="Original G4TX Textures", default="")
    show_original: BoolProperty(name="Original Model", default=True)
    show_mapping: BoolProperty(name="Mesh Correspondence", default=True)
    show_rigging: BoolProperty(name="Rigging", default=False)
    show_record_settings: BoolProperty(name="Advanced Mesh Settings", default=False)
    show_textures: BoolProperty(name="Textures", default=False)
    show_export: BoolProperty(name="Export", default=True)
    show_expression_preview: BoolProperty(name="Expression Preview", default=True)

    def preset_path(self, prefs) -> Path:
        configured = bpy.path.abspath(self.preset_file or "")
        if configured:
            return Path(configured)
        return Path()

    def texture_map(self) -> dict:
        result = {}
        atlas_states = {row["name"]: row["state"] for row in atlas_status_rows(self, validate_images=True)}
        texture_names = [entry.texture_name for entry in self.texture_entries]
        face_texture = shared_face_texture_key(texture_names) if face_texture_is_shared(self.records, texture_names) else ""
        for item in self.texture_entries:
            if not item.texture_name:
                continue
            replacement_path = item.replacement_path
            if item.texture_name == face_texture and not item.expression_atlas:
                replacement_path = existing_face_atlas_source(self, item.texture_name)
            if not replacement_path:
                continue
            if item.atlas_signature and atlas_states.get(item.texture_name) != "ready":
                continue
            if self.replace_special_textures or not is_special_texture(item.texture_name):
                result[item.texture_name] = bpy.path.basename(replacement_path)
        for item in split_csv(self.texture_replacements):
            if "=" in item:
                key, value = item.split("=", 1)
                key = key.strip()
                if key == face_texture:
                    continue
                entry = texture_entry(self, key)
                if entry is not None and entry.atlas_signature and atlas_states.get(key) == "warning":
                    continue
                if self.replace_special_textures or not is_special_texture(key):
                    result[key] = value.strip()
        return result

    def to_config(self) -> dict:
        active_texture_keys = set(self.texture_map())
        records = []
        for record in self.records:
            effective_texture_key = resolved_record_texture_key(self, record)
            face_expression_transform = (
                is_face_atlas_record(record)
                and effective_texture_key in active_texture_keys
                and face_pool_atlas_active(self, record)
            )
            entry = texture_entry(self, effective_texture_key) if face_expression_transform else None
            face_expression_pool = bool(entry and entry.expression_atlas_mode == "pool")
            atlas_transform = (
                self.use_source_uv_transforms
                and effective_texture_key in active_texture_keys
                and (not is_face_atlas_record(record) or face_expression_pool or face_expression_transform)
            )
            item = record.to_config(atlas_transform)
            if face_expression_pool:
                # Pool expressions are serialized entirely at record level.
                # eye_10 is a face mask, so fit its complete authored UV island
                # into the first sprite. mouth_10 already has the right size,
                # so preserve its relative UV scale and only move it to Cell 1.
                prepared_scale = (float(record.uv_scale_u), float(record.uv_scale_v))
                prepared_offset = (float(record.uv_offset_u), float(record.uv_offset_v))
                if prepared_scale != (1.0, 1.0) or prepared_offset != (0.0, 0.0):
                    display_scale, display_offset = prepared_scale, prepared_offset
                else:
                    display_scale, display_offset = face_expression_record_uv(self, record)
                target_rect = (
                    display_offset[0], display_offset[1],
                    display_scale[0], display_scale[1],
                )
                scale, offset = expression_pool_record_uv(
                    record.output_name, target_rect, self.global_uv_flip_y
                )
                item["uv_scale"] = [scale[0], scale[1]]
                item["uv_offset"] = [offset[0], offset[1]]
                item.pop("uv_fit_display_rect", None)
                item.pop("source_uv_transforms", None)
            if not atlas_transform and not face_expression_transform:
                # Native G4TX entries retain their authored UV windows.  Atlas
                # controls are meaningful only for an explicitly replaced map.
                item.pop("uv_flip", None)
                item.pop("uv_scale", None)
                item.pop("uv_offset", None)
                item.pop("source_uv_transforms", None)
            if self.disable_edge2:
                item["outline_enabled"] = False
            records.append(item)
        source_mesh_assignments = {
            obj.name: obj.level5_g4_port.target_record
            for obj in mesh_objects(self.selected_only)
            if obj.level5_g4_port.target_record not in {"", "__none__"}
        }
        return {
            "model_rel": self.model_rel,
            "native_material_names": split_csv(self.native_material_names),
            "records": records,
            "texture_replacements": self.texture_map(),
            "texture_platform": self.texture_platform,
            "material_overrides": [],
            "joint_aliases": effective_joint_aliases(self),
            "target_g4sk_path": bpy.path.abspath(self.target_g4sk_path) if self.target_g4sk_path else "",
            "source_mesh_assignments": source_mesh_assignments,
            "generate_tangents": self.generate_tangents,
            "strict_skinning": self.strict_skinning,
            "uv_flip": [self.global_uv_flip_x, self.global_uv_flip_y],
            "disable_edge2": self.disable_edge2,
            "outline_width": self.outline_width,
            "vertex_color_mode": "authored" if self.use_authored_outlines else "constant" if self.texture_mode == "custom" else "native",
            "vertex_color": list(STABLE_CUSTOM_VERTEX_COLOR),
        }


def settings(context) -> G4PortSceneSettings:
    """Return the scene port settings without mutating Blender data.

    Blender may call panel/operator draw methods from a restricted context where
    writing to ID datablocks is forbidden.  Keep this accessor strictly read-only
    and perform migrations/default initialization from operators or handlers.
    """
    return context.scene.level5_g4_port


def ensure_expression_pool(props: G4PortSceneSettings) -> None:
    """Ensure the fixed 4x2 expression pool exists outside UI draw contexts."""
    while len(props.expression_pool) < FACE_ATLAS_SLOTS:
        props.expression_pool.add()


def ensure_scene_defaults(context) -> G4PortSceneSettings:
    props = settings(context)
    ensure_expression_pool(props)
    if not props.joint_target_options:
        refresh_joint_target_options(props, context)
    return props


@persistent
def initialize_g4_port_scene_defaults(_dummy=None) -> None:
    """Migrate existing .blend scenes after registration or file load."""
    for obj in getattr(bpy.data, "objects", ()):
        if obj.type == "MESH":
            if obj.data.get(OUTLINE_AUTHORED_KEY):
                refresh_authored_outline_mesh(obj.data)
            elif obj.get("g4_native_model_source"):
                import_native_outline_marks(obj.data)
    for scene in getattr(bpy.data, "scenes", ()):
        props = getattr(scene, "level5_g4_port", None)
        if props is not None:
            ensure_expression_pool(props)
            if not props.joint_target_options:
                refresh_joint_target_options(props, bpy.context)
            viewport_outline.configure_authored(scene, props.preview_authored_outlines)


def apply_config_to_settings(target: G4PortSceneSettings, config: dict) -> None:
    width = float(config.get("outline_width", 1.0))
    if not 0.2 <= width <= 4.0:
        raise ValueError("Stroke Width must be between 0.2 and 4.0")
    target.outline_width = width
    target.use_authored_outlines = config.get("vertex_color_mode") == "authored"
    target.model_rel = config.get("model_rel", target.model_rel)
    target.native_material_names = join_csv(config.get("native_material_names", []))
    replacements = config.get("texture_replacements", {})
    target.texture_replacements = join_csv(f"{key}={value}" for key, value in replacements.items())
    target.texture_platform = str(config.get("texture_platform", "auto"))
    for entry in target.texture_entries:
        entry.replacement_path = str(replacements.get(entry.texture_name, ""))
    target.generate_tangents = bool(config.get("generate_tangents", False))
    target.strict_skinning = bool(config.get("strict_skinning", False))
    uv_flip = config.get("uv_flip") or [False, True]
    target.global_uv_flip_x = bool(uv_flip[0]) if len(uv_flip) > 0 else False
    target.global_uv_flip_y = bool(uv_flip[1]) if len(uv_flip) > 1 else False
    target.records.clear()
    for source in config.get("records", []):
        record = target.records.add()
        record.output_name = source.get("output_name", "")
        record.material_name = source.get("material_name", "")
        record.texture_key = record.output_name
        record.match_names = join_csv(source.get("match_names", []))
        record.fallback_degenerate = bool(source.get("fallback_degenerate", False))
        record.rigid_joint = str(source.get("rigid_joint", ""))
        record.auto_palette = bool(source.get("auto_palette", True))
        uv = source.get("uv_flip") or [False, False]
        record.uv_flip_x = bool(uv[0]) if len(uv) > 0 else False
        record.uv_flip_y = bool(uv[1]) if len(uv) > 1 else False
        uv_scale = source.get("uv_scale") or [1.0, 1.0]
        record.uv_scale_u = float(uv_scale[0]) if len(uv_scale) > 0 else 1.0
        record.uv_scale_v = float(uv_scale[1]) if len(uv_scale) > 1 else 1.0
        uv_offset = source.get("uv_offset") or [0.0, 0.0]
        record.uv_offset_u = float(uv_offset[0]) if len(uv_offset) > 0 else 0.0
        record.uv_offset_v = float(uv_offset[1]) if len(uv_offset) > 1 else 0.0
        forced = source.get("force_layout_material")
        record.force_layout_material = isinstance(forced, list) and len(forced) == 2
        if record.force_layout_material:
            record.layout_index = int(forced[0])
            record.material_index = int(forced[1])
        record.secondary_weight_scale = float(source.get("secondary_weight_scale", 1.0))
        record.weight_anchor_joint = str(source.get("weight_anchor_joint", ""))
    assign_shared_face_texture_key(
        target.records,
        [entry.texture_name for entry in target.texture_entries if entry.texture_name],
    )
    target.joint_aliases.clear()
    for source_group, target_joint in (config.get("joint_aliases") or {}).items():
        alias = target.joint_aliases.add()
        alias.source_group = str(source_group)
        alias.target_joint = str(target_joint)
    target.active_record = min(target.active_record, max(0, len(target.records) - 1))


def run_model_probe(path: Path, prefs) -> dict:
    command = [
        bpy.path.abspath(getattr(prefs, "python_path", "") or default_python()),
        str(resolve_probe_script(prefs)),
        "--json",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            "G4 model probe failed\n"
            f"Command: {shlex.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    try:
        results = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model probe did not return JSON:\n{completed.stdout}") from exc
    if not results:
        raise RuntimeError("Model probe returned no results")
    return results[0]


def model_rel_from_path(path: Path) -> str:
    data_root = infer_data_root(path)
    if data_root is not None:
        try:
            return relative_model_from_data(path, data_root)
        except ValueError:
            pass
    parts = list(path.with_suffix("").parts)
    for root_name in ("common", "dx11", "nx"):
        if root_name in parts:
            index = parts.index(root_name)
            return "/".join(parts[index + 1 :])
    return path.with_suffix("").name


def original_template_signature(md: dict) -> str:
    material_names = md.get("material_names", [])
    mesh_names = md.get("mesh_names", [])
    rows = []
    for source in md.get("records", []):
        index = int(source.get("index", len(rows)))
        material_index = int(source.get("material_index", source.get("material_or_lod", 0)))
        rows.append(
            [
                mesh_names[index] if index < len(mesh_names) else f"mesh_{index}",
                material_names[material_index] if material_index < len(material_names) else "",
                int(source.get("layout_index", 0)),
                material_index,
            ]
        )
    return json.dumps(rows, separators=(",", ":"))


def g4md_joint_index(path: Path, joint_name: str) -> int | None:
    """Return the native G4MD joint-table index for *joint_name*, if present."""
    try:
        data = path.read_bytes()
        if len(data) < 0x84 or data[:4] != b"G4MD":
            return None
        header_size = struct.unpack_from("<H", data, 0x04)[0]
        start = header_size + struct.unpack_from("<H", data, 0x74)[0] * 4
        end = header_size + struct.unpack_from("<H", data, 0x82)[0] * 4
        if not 0 <= start <= end <= len(data):
            return None
        target_hash = __import__("zlib").crc32(joint_name.encode("ascii")) & 0xFFFFFFFF
        for index, offset in enumerate(range(start, end - 3, 4)):
            if struct.unpack_from("<I", data, offset)[0] == target_hash:
                return index
    except (OSError, UnicodeEncodeError, struct.error):
        return None
    return None


def record_default_joint(md: dict, source: dict, preferred_joint: str | None = None) -> str:
    """Keep an unweighted record on a joint actually present in its palette."""
    flags = int(source.get("flags0", 0))
    palette_length = flags & 0xFF if flags & 0x100 else 0
    palette_offset = int(source.get("palette_or_list", 0))
    palette = md.get("joint_palette_indices") or []
    if palette_length > 0 and 0 <= palette_offset < len(palette):
        return f"joint_{int(palette[palette_offset])}"
    if preferred_joint:
        return preferred_joint
    return "c_head_1_0"


def original_g4tx_path(data_root: Path, model_rel: str) -> Path | None:
    for path in (
        dx11_g4tx_for_model(data_root, model_rel),
        nx_g4tx_for_model(data_root, model_rel),
        common_g4tx_for_model(data_root, model_rel),
    ):
        if path.is_file():
            return path
    return None


def apply_original_model_to_settings(target: G4PortSceneSettings, path: Path, summary: dict) -> None:
    ensure_expression_pool(target)
    md = summary.get("g4md") or {}
    data_root = infer_data_root(path)
    if data_root is None:
        raise RuntimeError("The original model must be inside a data/common or data/dx11 filesystem tree.")
    model_rel = model_rel_from_path(path)
    g4tx_path = original_g4tx_path(data_root, model_rel)
    texture_names = parse_g4tx_names(g4tx_path) if g4tx_path is not None else []
    signature = original_template_signature(md)
    target.original_model = str(path)
    # A face establishes the active character rig.  Uniform templates may use
    # another compatible generic G4SK whose rest pose differs substantially;
    # retaining the face reference makes separately exported parts share the
    # skeleton that the assembled character actually animates with.
    if "_face" in {part.casefold() for part in path.parts}:
        target.rig_reference_model = str(path)
    target.target_joint_names = json.dumps((summary.get("g4sk") or {}).get("names") or [])
    target.model_rel = model_rel
    target.native_material_names = join_csv(md.get("material_names", []))
    target.texture_replacements = ""
    target.texture_names = join_csv(texture_names)
    target.texture_entries.clear()
    for texture_name in texture_names:
        entry = target.texture_entries.add()
        entry.texture_name = texture_name
    if target.template_signature != signature:
        for obj in mesh_objects(False):
            obj.level5_g4_port.target_record = "__none__"
    material_names = md.get("material_names", [])
    mesh_names = md.get("mesh_names", [])
    preferred_rigid_joint = "c_c_1_0" if g4md_joint_index(path, "c_c_1_0") is not None else None
    if target.template_signature == signature and len(target.records) == len(md.get("records", [])):
        for record in target.records:
            if not record.texture_key:
                record.texture_key = texture_key_for_record(record.output_name, texture_names)
        assign_shared_face_texture_key(target.records, texture_names)
        return
    target.records.clear()
    for source in md.get("records", []):
        record = target.records.add()
        index = int(source.get("index", len(target.records) - 1))
        material_index = int(source.get("material_index", source.get("material_or_lod", 0)))
        layout_index = int(source.get("layout_index", 0))
        record.original_index = index
        record.output_name = mesh_names[index] if index < len(mesh_names) else f"mesh_{index}"
        record.material_name = material_names[material_index] if material_index < len(material_names) else ""
        record.match_names = record.output_name
        record.texture_key = texture_key_for_record(record.output_name, texture_names)
        record.fallback_degenerate = True
        record.force_layout_material = True
        record.layout_index = layout_index
        record.material_index = material_index
        record.rigid_joint = record_default_joint(md, source, preferred_rigid_joint)
        record.auto_palette = True
    assign_shared_face_texture_key(target.records, texture_names)
    target.active_record = 0
    target.template_signature = signature
    target.use_preset_file = False


def assign_selected_to_record(context, record: G4PortRecord) -> int:
    selected = mesh_objects(True)
    names = [obj.name for obj in selected]
    if not selected:
        return 0
    for obj in selected:
        obj.level5_g4_port.target_record = record.output_name
    if not record.texture_file:
        for obj in selected:
            image_path = material_image_path(obj.active_material)
            if image_path:
                record.texture_file = image_path
                break
    existing = [item for item in split_csv(record.match_names) if item != "*"]
    merged = list(dict.fromkeys(existing + names))
    record.match_names = join_csv(merged)
    settings(context).use_preset_file = False
    return len(names)


def guess_object_assignments(props: G4PortSceneSettings) -> int:
    records = list(props.records)
    if not records:
        return 0
    assigned = 0
    for obj in mesh_objects(False):
        object_key = blender_base_name(obj.name).lower()
        material_key = blender_base_name(obj.active_material.name).lower() if obj.active_material else ""
        best = None
        for record in records:
            candidates = [
                record.output_name.lower(),
                record.material_name.removesuffix("M").lower(),
                record.material_name.lower(),
            ]
            if object_key in candidates or material_key in candidates:
                best = record
                break
        if best is None:
            continue
        obj.level5_g4_port.target_record = best.output_name
        existing = [item for item in split_csv(best.match_names) if item != "*"]
        if obj.name not in existing:
            best.match_names = join_csv(existing + [obj.name])
        assigned += 1
    if assigned:
        props.use_preset_file = False
    return assigned


def sync_assignment_table(context) -> None:
    props = settings(context)
    assignments: dict[str, list[str]] = {record.output_name: [] for record in props.records}
    for obj in mesh_objects(False):
        target = getattr(obj.level5_g4_port, "target_record", "__none__")
        if target in assignments:
            assignments[target].append(obj.name)
    for record in props.records:
        names = assignments.get(record.output_name) or split_csv(record.match_names)
        record.match_names = join_csv(list(dict.fromkeys(name for name in names if name and name != "*")))
    props.use_preset_file = False


def objects_for_record(record: G4PortRecord) -> list[bpy.types.Object]:
    explicit = set(split_csv(record.match_names))
    assigned = [
        obj for obj in mesh_objects(False)
        if getattr(obj.level5_g4_port, "target_record", "__none__") == record.output_name
    ]
    if assigned:
        return assigned
    return [obj for obj in mesh_objects(False) if obj.name in explicit]


def export_armature() -> bpy.types.Object | None:
    active = bpy.context.active_object
    if active is not None and active.type == "ARMATURE":
        return active
    for obj in mesh_objects(False):
        for modifier in obj.modifiers:
            if modifier.type == "ARMATURE" and modifier.object is not None:
                return modifier.object
        if obj.parent is not None and obj.parent.type == "ARMATURE":
            return obj.parent
    return None


def active_rig_reference_model(props: "G4PortSceneSettings", original_model: Path) -> Path:
    reference = resolve_file(props.rig_reference_model)
    return reference if reference.is_file() else original_model


def active_rig_skeleton_path(props: "G4PortSceneSettings", original_model: Path) -> Path | None:
    """Return the on-disk G4SK used for rest conversion and validation."""
    selected = resolve_file(props.target_g4sk_path)
    if selected.is_file() and selected.suffix.casefold() == ".g4sk":
        return selected
    try:
        from .g4_model_probe import configure_raw_data_root_from_path, find_skeleton_for_model, g4sk_entries_from_candidate
    except ImportError:
        from porting.g4_model_probe import configure_raw_data_root_from_path, find_skeleton_for_model, g4sk_entries_from_candidate

    reference = active_rig_reference_model(props, original_model)
    configure_raw_data_root_from_path(reference)
    _data, source = find_skeleton_for_model(reference)
    if not source:
        return None
    source = source.split(" via ", 1)[0]
    if "::" not in source:
        path = Path(source)
        return path if path.is_file() and path.suffix.casefold() == ".g4sk" else None
    pack_name, entry_name = source.rsplit("::", 1)
    pack_path = Path(pack_name)
    if not pack_path.is_file():
        return None
    cache = Path(tempfile.gettempdir()) / "level5_g4sk_blender"
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / f"{hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]}.g4sk"
    for skeleton_data, candidate_source in g4sk_entries_from_candidate(pack_path):
        if candidate_source.rsplit("::", 1)[-1] != entry_name:
            continue
        if not destination.is_file() or destination.read_bytes() != skeleton_data:
            destination.write_bytes(skeleton_data)
        return destination
    return None


def write_animation_profile(
    props: "G4PortSceneSettings", original_model: Path, target_model: Path,
    target_skeleton_path: Path, raw_root: Path,
) -> Path:
    try:
        from .g4_model_probe import configure_raw_data_root_from_path, parse_g4sk, resolve_g4sk_from_chara_model
    except ImportError:
        from porting.g4_model_probe import configure_raw_data_root_from_path, parse_g4sk, resolve_g4sk_from_chara_model

    reference = active_rig_reference_model(props, original_model)
    configure_raw_data_root_from_path(reference)
    source_data, source_description = resolve_g4sk_from_chara_model(reference)
    if source_data is None or not source_description:
        raise RuntimeError(f"Could not resolve the assigned body G4SK for {reference}")
    source_description = source_description.split(" via ", 1)[0]
    if "::" in source_description:
        raise RuntimeError("The source animation G4SK is embedded in a package; a standalone raw-DUMP G4SK is required for the profile")
    source_path = Path(source_description).resolve()
    source_resource_path = source_path.relative_to(raw_root.resolve()).as_posix()
    source = parse_g4sk(source_data)
    target_data = target_skeleton_path.read_bytes()
    target = parse_g4sk(target_data)

    def parent_indices(data: bytes, joint_count: int) -> list[int | None]:
        table = 0x40 + struct.unpack_from("<H", data, 0x2A)[0] * 4
        if table + 2 + max(0, joint_count - 1) * 2 > len(data):
            raise ValueError("G4SK parent table exceeds the skeleton bounds")
        if struct.unpack_from("<H", data, table)[0] != joint_count:
            raise ValueError("G4SK parent table count does not match its joint count")
        parents: list[int | None] = [None]
        for index in range(1, joint_count):
            parent = struct.unpack_from("<H", data, table + 2 + (index - 1) * 2)[0]
            if parent >= joint_count or parent == index:
                raise ValueError(f"G4SK joint {index} has an invalid parent index {parent}")
            parents.append(parent)
        for index in range(joint_count):
            visited = set()
            current = index
            while current is not None:
                if current in visited:
                    raise ValueError("G4SK parent table contains a hierarchy cycle")
                visited.add(current)
                current = parents[current]
        return parents

    source_names = source["names"]
    target_names = target["names"]
    if (not source_names or len(source_names) != len(set(source_names))
            or not target_names or len(target_names) != len(set(target_names))):
        raise ValueError("Animation profile G4SK files must contain unique joint names")
    for skeleton in (source, target):
        count = len(skeleton["names"])
        if any(len(skeleton[key]) != count for key in ("local_translations", "local_rotations_xyzw", "local_scales")):
            raise ValueError("Animation profile G4SK has an incomplete local rest transform table")
    source_indices = {name: index for index, name in enumerate(source_names)}
    source_parents = parent_indices(source_data, len(source_names))
    target_parents = parent_indices(target_data, len(target_names))
    source_for_target = animation_source_joint_map(props, set(source_indices), set(target_names))
    candidate_sources = dict(source_for_target)
    source_joint_names = set(source_indices)
    source_alias_catalog = load_joint_alias_catalog()
    for target_name in target_names:
        source_name = candidate_sources.get(target_name, "")
        if not source_name and target_name in source_indices:
            source_name = target_name
        if not source_name:
            source_name = resolve_catalog_alias(target_name, source_alias_catalog, source_joint_names)
        if source_name:
            if source_name not in source_indices:
                raise ValueError(f"Mapped source joint {source_name!r} is absent from {source_path.name}")
            candidate_sources[target_name] = source_name

    target_parent_names = {
        name: target_names[parent] if parent is not None else None
        for name, parent in zip(target_names, target_parents)
    }
    source_for_target = resolve_effective_joint_mappings(
        target_parent_names,
        resolve_animation_joint_drivers(target_parent_names, candidate_sources),
    )

    def transform(skeleton: dict, index: int) -> dict:
        return {
            "translation": skeleton["local_translations"][index],
            "rotation": skeleton["local_rotations_xyzw"][index],
            "scale": skeleton["local_scales"][index],
        }

    joints = []
    for index, name in enumerate(target_names):
        source_name = source_for_target.get(name)
        source_index = source_indices.get(source_name) if source_name else None
        source_parent = None
        target_parent = target_parents[index]
        while target_parent is not None:
            candidate = source_for_target.get(target_names[target_parent])
            if candidate and candidate != source_name:
                source_parent = candidate
                break
            target_parent = target_parents[target_parent]
        if source_parent == name:
            source_parent = None
        target_parent_index = target_parents[index]
        target_rest = transform(target, index)
        joints.append({
            "name": name,
            "nameCrc32": zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF,
            "parent": target_names[target_parent_index] if target_parent_index is not None else None,
            "sourceRest": transform(source, source_index) if source_index is not None else target_rest,
            "targetRest": target_rest,
            "modelRest": None,
            "sourceParent": source_parent,
            "sourceName": source_name,
            "sourceNameCrc32": zlib.crc32(source_name.encode("utf-8")) & 0xFFFFFFFF if source_name else None,
        })

    data_root = target_model.parents[0]
    while data_root.name != "data" and data_root.parent != data_root:
        data_root = data_root.parent
    target_relative = target_skeleton_path.relative_to(data_root).as_posix()
    profile = {
        "schemaVersion": 5,
        "producer": "G4_Blender",
        "modelStem": target_model.stem,
        "sourceModelStem": reference.stem,
        "modelPath": Path(props.model_rel).with_suffix(".g4md").as_posix(),
        "sourceRig": {
            "path": source_resource_path,
            "sha256": hashlib.sha256(source_data).hexdigest(),
        },
        "targetRig": {
            "path": target_relative,
            "sha256": hashlib.sha256(target_data).hexdigest(),
        },
        "joints": joints,
    }
    if not getattr(addon_preferences(), "legacy_animation_export", False):
        from ..animation.g4_animation_addon import resolve_animation_family_ids
        from ..animation.victorytool_animation_recipe import build_avatar_recipe

        chr_family, event_skeleton = resolve_animation_family_ids(reference, [raw_root])
        profile["schemaVersion"] = 7
        profile["avatarRecipe"] = build_avatar_recipe(
            source_path, target_skeleton_path, target_model, source_for_target,
            chr_family, event_skeleton, True, True,
        )

    output_path = target_model.with_suffix(".g4anim.json")
    handle, temporary_path = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(profile, stream, indent=2)
            stream.write("\n")
        os.replace(temporary_path, output_path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return output_path


def native_rest_corrections(props: "G4PortSceneSettings", original_model: Path) -> dict[str, list[float]]:
    """Map each source bone's rest space into the active target G4SK."""
    armature = export_armature()
    if armature is None:
        return {}
    try:
        from .g4_model_probe import configure_raw_data_root_from_path, find_skeleton_for_model, parse_g4sk
    except ImportError:
        from porting.g4_model_probe import configure_raw_data_root_from_path, find_skeleton_for_model, parse_g4sk

    reference = active_rig_reference_model(props, original_model)
    configure_raw_data_root_from_path(reference)
    skeleton_data, _source = find_skeleton_for_model(reference)
    if skeleton_data is None:
        return {}
    skeleton = parse_g4sk(skeleton_data)
    targets = {
        name: Matrix((values[0:4], values[4:8], values[8:12], values[12:16]))
        for name, values in zip(skeleton.get("names") or [], skeleton.get("bind_matrices") or [])
        if name and len(values) == 16
    }
    if not targets:
        return {}

    # G4SK bind matrices are Y-up; the importer wraps them in this conversion.
    blender_from_g4 = blender_from_g4_matrix()
    g4_from_blender = blender_from_g4.inverted()
    corrections: dict[str, list[float]] = {}
    for source_name, target_name in effective_joint_aliases(props).items():
        source_bone = armature.data.bones.get(source_name)
        target_matrix = targets.get(target_name)
        if source_bone is None or target_matrix is None:
            continue
        source_matrix = g4_from_blender @ source_bone.matrix_local @ blender_from_g4
        correction = target_matrix @ source_matrix.inverted_safe()
        if max(abs(correction[row][column] - (1.0 if row == column else 0.0)) for row in range(4) for column in range(4)) <= 1e-6:
            continue
        corrections[source_name] = [correction[row][column] for row in range(3) for column in range(4)]
    return corrections


def counter_rotated_rest_translations(props: "G4PortSceneSettings", original_model: Path) -> dict[str, list[float]]:
    """Keep authored vertices in the G4SK rest space.

    The assigned G4SK bind matrices are the runtime authority.  Rebasing each
    source vertex from Blender bone heads changes the authored arm and hair
    positions before skinning, which breaks event animation on separated parts.
    """
    return {}


def local_g4sk_bind_translations(props: "G4PortSceneSettings", original_model: Path) -> dict[str, list[float]]:
    """Export the armature rest heads in G4 space for a model-local G4SK."""
    armature = export_armature()
    skeleton_path = active_rig_skeleton_path(props, original_model)
    if armature is None or skeleton_path is None:
        return {}
    try:
        from .g4_model_probe import parse_g4sk
    except ImportError:
        from porting.g4_model_probe import parse_g4sk
    skeleton = parse_g4sk(skeleton_path.read_bytes())
    target_names = set(skeleton.get("names") or ())
    if not target_names:
        return {}
    g4_from_blender = g4_from_blender_matrix()
    aliases = effective_joint_aliases(props)
    source_by_target: dict[str, list[str]] = {}
    for source_name, target_name in aliases.items():
        source_by_target.setdefault(target_name, []).append(source_name)
    preferred_sources = {
        "c_c_1_0": ("WAIST",),
        "c_c_1_1": ("SPINE3", "SPINE2", "SPINE1"),
        "c_n_1_0": ("NECK",),
        "c_head_1_0": ("HEAD",),
        "l_s_1_0": ("CLAVICLE_L",),
        "l_a_1_0": ("SHOULDER_L",),
        "l_a_1_1": ("ELBOW_L",),
        "l_w_1_0": ("WRIST_L",),
        "r_s_1_0": ("CLAVICLE_R",),
        "r_a_1_0": ("SHOULDER_R",),
        "r_a_1_1": ("ELBOW_R",),
        "r_w_1_0": ("WRIST_R",),
        "l_l_1_0": ("THIGH_L",),
        "l_l_1_1": ("CLANK_L",),
        "l_foot_1_0": ("TOE1_L",),
        "l_foot_1_1": ("TOE2_L",),
        "r_l_1_0": ("THIGH_R",),
        "r_l_1_1": ("CLANK_R",),
        "r_foot_1_0": ("TOE1_R",),
        "r_foot_1_1": ("TOE2_R",),
    }
    translations: dict[str, list[float]] = {}
    for target_name, sources in source_by_target.items():
        if target_name not in target_names or target_name.startswith(("eye_", "mouth_")):
            continue
        candidates = preferred_sources.get(target_name, ())
        source_name = next((name for name in candidates if name in sources), None)
        if source_name is None:
            source_name = sources[0] if sources else None
        bone = armature.data.bones.get(source_name) if source_name else None
        if bone is None:
            continue
        matrix = g4_from_blender @ armature.matrix_world @ bone.matrix_local
        translations.setdefault(
            target_name,
            [
                float(matrix[0][3]),
                float(matrix[1][3]),
                float(matrix[2][3]),
            ],
        )
    return translations


def armature_joint_parents(armature) -> dict[str, str | None]:
    if armature is None or armature.type != "ARMATURE":
        return {}
    return {
        bone.name: bone.parent.name if bone.parent is not None else None
        for bone in armature.data.bones
    }


def contextual_joint_aliases(names: set[str]) -> dict[str, str]:
    """Resolve unambiguous chains whose generic names otherwise collide."""
    by_key = {name.casefold(): name for name in names}
    required = {"clavicle_l", "shoulder_l", "clavicle_r", "shoulder_r"}
    if not required <= set(by_key):
        return {}
    targets = {
        "shoulder_l": "l_a_1_0", "elbow_l": "l_a_1_1", "wrist_l": "l_w_1_0",
        "shoulder_r": "r_a_1_0", "elbow_r": "r_a_1_1", "wrist_r": "r_w_1_0",
        "thigh_l": "l_l_1_0", "clank_l": "l_l_1_1", "toe1_l": "l_foot_1_0", "toe2_l": "l_foot_1_1",
        "thigh_r": "r_l_1_0", "clank_r": "r_l_1_1", "toe1_r": "r_foot_1_0", "toe2_r": "r_foot_1_1",
    }
    return {by_key[name]: target for name, target in targets.items() if name in by_key}


def effective_joint_aliases(props: "G4PortSceneSettings") -> dict[str, str]:
    explicit = {
        alias.source_group: alias.target_joint
        for alias in props.joint_aliases
        if alias.source_group and alias.target_joint
    }
    explicit_user_sources = set(explicit)
    parents = armature_joint_parents(export_armature())
    catalog = load_joint_alias_catalog()
    allowed = configured_target_joints(props)
    # Auxiliary deform bones can be siblings of the joint they follow. Resolve
    # known anatomy before parent inheritance, including blanks in saved scenes.
    for name in parents:
        if allowed and name in allowed and name not in explicit_user_sources:
            explicit[name] = name
            continue
        canonical = canonical_export_joint(name)
        if (
            canonical == "c_head_1_0"
            and name in allowed
            and (name not in explicit_user_sources or explicit[name] == name)
        ):
            explicit[name] = name
            continue
        if canonical and canonical != name and (
            name not in explicit_user_sources
            or canonical == "c_head_1_0"
            or name.casefold() in {"eye", "face", "face_jaw", "mouth", "jaw"}
        ):
            explicit[name] = canonical
            continue
        if name not in explicit:
            target = resolve_catalog_alias(name, catalog, allowed)
            if target:
                explicit[name] = target
    for name, target in contextual_joint_aliases(set(parents)).items():
        # Exact anatomy names are stronger than a persisted auto-detected
        # alias.  Older scenes saved SHOULDER_L as the clavicle slot, which
        # shifts the arm root before the elbow chain is even evaluated.
        if name not in explicit_user_sources or name.casefold() in {
            "shoulder_l", "elbow_l", "wrist_l", "shoulder_r", "elbow_r", "wrist_r",
            "thigh_l", "clank_l", "toe1_l", "toe2_l", "thigh_r", "clank_r", "toe1_r", "toe2_r",
        }:
            explicit[name] = target
    inherited = resolve_effective_joint_mappings(parents, explicit)
    # Direct anatomy aliases must win over parent inheritance.  Otherwise a
    # lower-leg group inherits the thigh target and the runtime collapses the
    # knee; the same mistake puts the arm root on the clavicle.
    return {**{name: target for name, target in inherited.items() if target}, **explicit}


def configured_joint_aliases(props: "G4PortSceneSettings") -> dict[str, str]:
    return {
        alias.source_group: alias.target_joint
        for alias in props.joint_aliases
        if alias.source_group and alias.target_joint
    }


def animation_source_joint_map(
    props: "G4PortSceneSettings", source_joint_names: set[str], target_joint_names: set[str],
) -> dict[str, str]:
    armature = export_armature()
    if armature is None:
        return {}
    parents = armature_joint_parents(armature)
    profile = getattr(armature, "level5_humanoid_avatar", None)
    if profile is not None and profile.has_representation:
        if not profile.mapping_valid or profile.mapping_dirty or profile.pose_stale:
            raise RuntimeError("Validate the humanoid Avatar mapping and rest pose before exporting animations")
        try:
            from ..animation.humanoid_avatar import target_animation_joint_map
            from ..animation.humanoid_avatar_addon import mapping_from_profile
        except ImportError:
            from animation.humanoid_avatar import target_animation_joint_map
            from animation.humanoid_avatar_addon import mapping_from_profile
        explicit = target_animation_joint_map(
            mapping_from_profile(profile), source_joint_names, target_joint_names
        )
        if not explicit:
            raise RuntimeError("The Avatar mapping has no joints in the selected source and target G4SKs")
    else:
        aliases = effective_joint_aliases(props)
        explicit = {
            target_name: source_name
            for target_name, source_name in aliases.items()
            if target_name in parents and source_name in source_joint_names
        }
        for bone in armature.data.bones:
            source_name = str(bone.get("g4_animation_source_joint", "")).strip()
            if source_name in source_joint_names:
                explicit[bone.name] = source_name
    drivers = resolve_animation_joint_drivers(parents, explicit)
    effective = resolve_effective_joint_mappings(parents, drivers)
    return {
        target_name: source_name
        for target_name, source_name in effective.items()
        if target_name in target_joint_names and source_name in source_joint_names
    }


def detect_joint_aliases(props: G4PortSceneSettings, selected_only: bool = False) -> int:
    existing = {alias.source_group: alias for alias in props.joint_aliases}
    allowed_joints = configured_target_joints(props)
    added = 0
    armature = export_armature()
    bone_names = [bone.name for bone in armature.data.bones] if armature is not None else []
    for group_name in dict.fromkeys([*bone_names, *vertex_group_names(selected_only)]):
        if group_name in existing:
            continue
        alias = props.joint_aliases.add()
        alias.source_group = group_name
        alias.target_joint = group_name if group_name in (allowed_joints or set()) else guess_joint_alias(group_name, allowed_joints)
        existing[group_name] = alias
        added += 1
    props.active_joint_alias = min(props.active_joint_alias, max(0, len(props.joint_aliases) - 1))
    return added


def auto_map_joint_aliases(props: G4PortSceneSettings) -> int:
    changed = 0
    allowed_joints = configured_target_joints(props)
    for alias in props.joint_aliases:
        if alias.target_joint:
            continue
        guess = guess_joint_alias(alias.source_group, allowed_joints)
        if guess:
            alias.target_joint = guess
            changed += 1
    return changed


def configured_target_joints(props: G4PortSceneSettings) -> set[str] | None:
    path = resolve_file(props.target_g4sk_path)
    if path.is_file() and path.suffix.casefold() == ".g4sk":
        try:
            from .g4_port import parse_g4sk
        except ImportError:
            from porting.g4_port import parse_g4sk
        return set(parse_g4sk(path)["names"])
    try:
        names = json.loads(props.target_joint_names or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return None
    return set(names)


def generated_config_path(cache: Path) -> Path:
    return cache / "generated_port_config.json"


def prepare_custom_textures(props: G4PortSceneSettings, mesh_data_path: Path) -> Path:
    custom_dir = mesh_data_path.parent / "customTextures"
    custom_dir.mkdir(parents=True, exist_ok=True)
    for record in props.records:
        if not record.texture_file:
            continue
        source = Path(bpy.path.abspath(record.texture_file))
        if source.is_file():
            shutil.copy2(source, custom_dir / source.name)
    texture_source_dir = resolve_file(props.texture_source_dir)
    for entry in props.texture_entries:
        if not entry.replacement_path:
            continue
        source = Path(bpy.path.abspath(entry.replacement_path))
        if source.is_file():
            shutil.copy2(source, custom_dir / source.name)
    for rel_path in props.texture_map().values():
        source = texture_source_dir / rel_path
        if source.is_file():
            shutil.copy2(source, custom_dir / source.name)
    return custom_dir


def export_python(prefs, needs_pillow: bool) -> str:
    configured = bpy.path.abspath(getattr(prefs, "python_path", "") or default_python())
    if not needs_pillow:
        return configured
    candidates = [configured, "/usr/bin/python3", "/opt/homebrew/bin/python3", "python3"]
    for candidate in dict.fromkeys(candidates):
        try:
            completed = subprocess.run(
                [candidate, "-c", "import PIL"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        if completed.returncode == 0:
            return candidate
    raise RuntimeError(
        "PNG texture replacement needs a Python installation with Pillow (PIL). "
        "DDS and NXTCH replacements do not require Pillow."
    )


def auto_assign_record_texture_files(props: G4PortSceneSettings) -> int:
    assigned = 0
    for record in props.records:
        if record.texture_file:
            continue
        for obj in objects_for_record(record):
            image_path = material_image_path(obj.active_material)
            if image_path:
                record.texture_file = image_path
                assigned += 1
                break
    if assigned:
        props.use_preset_file = False
    return assigned


def is_special_texture(name: str) -> bool:
    return name.endswith(("line", "oc", "sp", "dp", "spm"))


def special_texture_suffix(name: str) -> str:
    for suffix in ("line", "spm", "sp", "dp", "oc"):
        if name.endswith(suffix):
            return suffix
    return ""


def base_texture_name(name: str) -> str:
    suffix = special_texture_suffix(name)
    return name[: -len(suffix)] if suffix else name


def special_texture_default_color(name: str) -> tuple[float, float, float, float]:
    return {
        "line": (0.0, 0.0, 1.0, 1.0),
        "oc": (1.0, 1.0, 0.0, 1.0),
        "sp": (0.0, 0.0, 0.0, 0.0),
        "dp": (0.0, 0.0, 0.0, 0.0),
        "spm": (0.0, 0.0, 0.0, 1.0),
    }.get(special_texture_suffix(name), (0.0, 0.0, 0.0, 0.0))


def image_pixels(width: int, height: int, color: tuple[float, float, float, float]) -> array:
    return array("f", color) * (width * height)


def write_special_texture_default(path: Path, texture_name: str) -> None:
    """Write the 32x32 neutral payload required for a replaced special map."""
    save_png(path, 32, 32, image_pixels(32, 32, special_texture_default_color(texture_name)))


def set_pixel(pixels: array, width: int, height: int, x: int, y: int, color: tuple[float, float, float, float]) -> None:
    if 0 <= x < width and 0 <= y < height:
        offset = (y * width + x) * 4
        pixels[offset] = color[0]
        pixels[offset + 1] = color[1]
        pixels[offset + 2] = color[2]
        pixels[offset + 3] = color[3]


def draw_line(
    pixels: array,
    width: int,
    height: int,
    start: tuple[int, int],
    end: tuple[int, int],
    color: tuple[float, float, float, float],
) -> None:
    x0, y0 = start
    x1, y1 = end
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        set_pixel(pixels, width, height, x0, y0, color)
        if x0 == x1 and y0 == y1:
            break
        step = 2 * err
        if step >= dy:
            err += dy
            x0 += sx
        if step <= dx:
            err += dx
            y0 += sy


def uv_to_pixel(
    uv,
    record: G4PortRecord,
    props: G4PortSceneSettings,
    width: int,
    height: int,
    obj_uv: G4PortObjectSettings | None = None,
) -> tuple[int, int]:
    u, v = uv
    if obj_uv is not None:
        u -= math.floor(u)
        v -= math.floor(v)
        u = u * obj_uv.uv_scale_u + obj_uv.uv_offset_u
        v = v * obj_uv.uv_scale_v + obj_uv.uv_offset_v
    flip_x = record.uv_flip_x or props.global_uv_flip_x
    flip_y = record.uv_flip_y or props.global_uv_flip_y
    if flip_x:
        u = 1.0 - u
    if flip_y:
        v = 1.0 - v
    u = u * record.uv_scale_u + record.uv_offset_u
    v = v * record.uv_scale_v + record.uv_offset_v
    return (
        int(round(max(0.0, min(1.0, u)) * (width - 1))),
        int(round(max(0.0, min(1.0, v)) * (height - 1))),
    )


def draw_object_uvs(
    pixels: array,
    width: int,
    height: int,
    obj: bpy.types.Object,
    record: G4PortRecord,
    props: G4PortSceneSettings,
) -> None:
    mesh = obj.data
    if not mesh.uv_layers.active:
        return
    uv_data = mesh.uv_layers.active.data
    if not uv_data:
        return
    color = (0.02, 0.02, 0.02, 1.0)
    for polygon in mesh.polygons:
        loop_indices = list(polygon.loop_indices)
        if len(loop_indices) < 2:
            continue
        valid_indices = [index for index in loop_indices if index < len(uv_data)]
        if len(valid_indices) != len(loop_indices):
            continue
        points = [
            uv_to_pixel(uv_data[index].uv, record, props, width, height, obj.level5_g4_port)
            for index in valid_indices
        ]
        for index, point in enumerate(points):
            draw_line(pixels, width, height, point, points[(index + 1) % len(points)], color)


def save_png(path: Path, width: int, height: int, pixels: array) -> None:
    image = bpy.data.images.new(path.stem, width=width, height=height, alpha=True)
    try:
        image.pixels.foreach_set(pixels)
        image.filepath_raw = str(path)
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)


def load_image_pixels(path: str) -> tuple[int, int, array] | None:
    if not path:
        return None
    source = Path(bpy.path.abspath(path))
    if not source.is_file():
        return None
    image = bpy.data.images.load(str(source), check_existing=True)
    width, height = image.size
    if width <= 0 or height <= 0 or len(image.pixels) < int(width) * int(height) * 4:
        return None
    pixels = array("f", [0.0]) * (int(width) * int(height) * 4)
    image.pixels.foreach_get(pixels)
    return int(width), int(height), pixels


def blit_image_fit(
    target: array,
    target_width: int,
    target_height: int,
    source: tuple[int, int, array],
    cell_x: int,
    cell_y: int,
    cell_width: int,
    cell_height: int,
    gutter: int = 0,
) -> tuple[int, int, int, int]:
    source_width, source_height, source_pixels = source
    if source_width <= 0 or source_height <= 0 or cell_width <= 0 or cell_height <= 0:
        return cell_x, cell_y, cell_width, cell_height
    gutter = max(0, min(gutter, (cell_width - 1) // 2, (cell_height - 1) // 2))
    inner_width = max(1, cell_width - gutter * 2)
    inner_height = max(1, cell_height - gutter * 2)
    scale = min(inner_width / source_width, inner_height / source_height)
    draw_width = max(1, int(round(source_width * scale)))
    draw_height = max(1, int(round(source_height * scale)))
    draw_x = cell_x + gutter + max(0, (inner_width - draw_width) // 2)
    draw_y = cell_y + gutter + max(0, (inner_height - draw_height) // 2)
    for y in range(draw_height):
        src_y = min(source_height - 1, int(y / max(scale, 0.0001)))
        dst_y = draw_y + y
        if not 0 <= dst_y < target_height:
            continue
        for x in range(draw_width):
            src_x = min(source_width - 1, int(x / max(scale, 0.0001)))
            dst_x = draw_x + x
            if not 0 <= dst_x < target_width:
                continue
            src = (src_y * source_width + src_x) * 4
            dst = (dst_y * target_width + dst_x) * 4
            target[dst : dst + 4] = source_pixels[src : src + 4]
    if gutter:
        extend_rect_border(target, target_width, target_height, draw_x, draw_y, draw_width, draw_height, gutter)
    return draw_x, draw_y, draw_width, draw_height


def extend_rect_border(
    pixels: array,
    width: int,
    height: int,
    x: int,
    y: int,
    rect_width: int,
    rect_height: int,
    border: int,
) -> None:
    """Duplicate edge texels so filtering never samples a neighbouring atlas cell."""
    for dst_y in range(max(0, y - border), min(height, y + rect_height + border)):
        source_y = min(y + rect_height - 1, max(y, dst_y))
        for dst_x in range(max(0, x - border), min(width, x + rect_width + border)):
            if x <= dst_x < x + rect_width and y <= dst_y < y + rect_height:
                continue
            source_x = min(x + rect_width - 1, max(x, dst_x))
            source = (source_y * width + source_x) * 4
            target = (dst_y * width + dst_x) * 4
            pixels[target : target + 4] = pixels[source : source + 4]


def bleed_transparent_pixels(source: tuple[int, int, array], radius: int = 2) -> tuple[int, int, array]:
    """Give transparent texels nearby colour, avoiding black fringes after filtering."""
    width, height, source_pixels = source
    pixels = array("f", source_pixels)
    has_color = [pixels[index * 4 + 3] > 0.0 for index in range(width * height)]
    if not any(has_color) or all(has_color):
        return width, height, pixels
    # Two pixels are enough for linear filtering.  Keep the pass bounded so
    # large source maps do not turn atlas preparation into a full image bake.
    for _ in range(max(1, radius)):
        changed = False
        for y in range(height):
            for x in range(width):
                target = (y * width + x) * 4
                target_index = y * width + x
                if has_color[target_index]:
                    continue
                neighbours = []
                if x > 0:
                    neighbours.append(target - 4)
                if x + 1 < width:
                    neighbours.append(target + 4)
                if y > 0:
                    neighbours.append(target - width * 4)
                if y + 1 < height:
                    neighbours.append(target + width * 4)
                source_offset = next((offset for offset in neighbours if has_color[offset // 4]), None)
                if source_offset is not None:
                    pixels[target : target + 3] = pixels[source_offset : source_offset + 3]
                    has_color[target_index] = True
                    changed = True
        if not changed:
            break
    return width, height, pixels


def atlas_grid(count: int) -> tuple[int, int]:
    columns = 1
    while columns * columns < count:
        columns += 1
    rows = (count + columns - 1) // columns
    return columns, rows


def capped_atlas_dimensions(
    entry_width: int, entry_height: int, source_width: int, source_height: int, columns: int, rows: int
) -> tuple[int, int, int, int]:
    cell_width = max(1, max(entry_width, source_width * columns) // columns)
    cell_height = max(1, max(entry_height, source_height * rows) // rows)
    limit = min(MAX_GENERATED_TEXTURE_SIZE, 0xFFFF)
    if cell_width * columns > limit:
        cell_width = max(1, limit // columns)
    if cell_height * rows > limit:
        cell_height = max(1, limit // rows)
    width = min(limit, max(1, cell_width * columns))
    height = min(limit, max(1, cell_height * rows))
    return width, height, cell_width, cell_height


def object_uv_bounds(obj: bpy.types.Object) -> tuple[float, float, float, float] | None:
    mesh = obj.data
    if not mesh.uv_layers.active or not mesh.uv_layers.active.data:
        return None
    values = [loop.uv for loop in mesh.uv_layers.active.data]
    min_u = min(uv.x for uv in values)
    max_u = max(uv.x for uv in values)
    min_v = min(uv.y for uv in values)
    max_v = max(uv.y for uv in values)
    return min_u, max_u, min_v, max_v


def material_uv_bounds(obj: bpy.types.Object, material_index: int) -> tuple[float, float, float, float] | None:
    uv_layer = obj.data.uv_layers.active
    values = [
        uv_layer.data[loop_index].uv
        for polygon in obj.data.polygons if polygon.material_index == material_index
        for loop_index in polygon.loop_indices
    ] if uv_layer else []
    if not values:
        return None
    return (
        min(uv.x for uv in values), max(uv.x for uv in values),
        min(uv.y for uv in values), max(uv.y for uv in values),
    )


def set_object_material_uv_transform(
    obj: bpy.types.Object,
    material_name: str,
    scale: tuple[float, float],
    offset: tuple[float, float],
) -> None:
    settings = obj.level5_g4_port
    transforms = json.loads(settings.material_uv_transforms or "{}")
    transforms[material_name] = {"scale": list(scale), "offset": list(offset)}
    settings.material_uv_transforms = json.dumps(transforms, sort_keys=True)


def clear_object_material_uv_transforms(obj: bpy.types.Object) -> None:
    obj.level5_g4_port.material_uv_transforms = "{}"


def set_material_uv_fit(
    obj: bpy.types.Object,
    material_name: str,
    bounds: tuple[float, float, float, float] | None,
    origin_u: float,
    origin_v: float,
    width: float,
    height: float,
) -> None:
    if bounds is None:
        set_object_material_uv_transform(obj, material_name, (width, height), (origin_u, origin_v))
        return
    min_u, max_u, min_v, max_v = bounds
    bounds_u = max(max_u - min_u, 0.0001)
    bounds_v = max(max_v - min_v, 0.0001)
    scale = min(width / bounds_u, height / bounds_v)
    offset_u = origin_u + (width - bounds_u * scale) * 0.5 - min_u * scale
    offset_v = origin_v + (height - bounds_v * scale) * 0.5 - min_v * scale
    set_object_material_uv_transform(obj, material_name, (scale, scale), (offset_u, offset_v))


def expression_preview_targets() -> list[bpy.types.Object]:
    """Find expression meshes by their Blender object name, independent of export records."""
    return [
        obj for obj in mesh_objects(False)
        if blender_base_name(obj.name).casefold() in {"eye_10", "mouth_10"}
    ]


def expression_preview_backup(mesh: bpy.types.Mesh, source_uv: bpy.types.MeshUVLoopLayer) -> bpy.types.MeshUVLoopLayer:
    backup = mesh.uv_layers.get(EXPRESSION_PREVIEW_BACKUP_UV)
    if backup is None:
        backup = mesh.uv_layers.new(name=EXPRESSION_PREVIEW_BACKUP_UV)
        for target, source in zip(backup.data, source_uv.data):
            target.uv = source.uv
    return backup


def apply_expression_preview(cell: int) -> int:
    """Offset expression-mesh UVs to one visual 4x2 atlas cell.

    The backup is a temporary UV layer rather than an in-memory cache so the
    Restore action remains safe after a panel redraw or operator re-execution.
    """
    if not 1 <= cell <= FACE_ATLAS_SLOTS:
        raise ValueError(f"Expression cell must be between 1 and {FACE_ATLAS_SLOTS}")
    applied = 0
    index = cell - 1
    origin_u = (index % FACE_ATLAS_COLUMNS) / FACE_ATLAS_COLUMNS
    origin_v = (index // FACE_ATLAS_COLUMNS) / FACE_ATLAS_ROWS
    for obj in expression_preview_targets():
        mesh = obj.data
        source_name = str(obj.get(EXPRESSION_PREVIEW_SOURCE_UV_KEY, ""))
        source_uv = mesh.uv_layers.get(source_name) if source_name else mesh.uv_layers.active
        if source_uv is None or source_uv.name == EXPRESSION_PREVIEW_BACKUP_UV:
            continue
        backup = expression_preview_backup(mesh, source_uv)
        obj[EXPRESSION_PREVIEW_SOURCE_UV_KEY] = source_uv.name
        for target, source in zip(source_uv.data, backup.data):
            target.uv = (
                source.uv.x + origin_u,
                source.uv.y + origin_v,
            )
        mesh.uv_layers.active = source_uv
        applied += 1
    return applied


def restore_expression_preview() -> int:
    """Restore all meshes previously changed by :func:`apply_expression_preview`."""
    restored = 0
    for obj in mesh_objects(False):
        source_name = str(obj.get(EXPRESSION_PREVIEW_SOURCE_UV_KEY, ""))
        if not source_name:
            continue
        mesh = obj.data
        source_uv = mesh.uv_layers.get(source_name)
        backup = mesh.uv_layers.get(EXPRESSION_PREVIEW_BACKUP_UV)
        if source_uv is not None and backup is not None:
            for target, source in zip(source_uv.data, backup.data):
                target.uv = source.uv
            mesh.uv_layers.active = source_uv
            mesh.uv_layers.remove(backup)
            restored += 1
        del obj[EXPRESSION_PREVIEW_SOURCE_UV_KEY]
    return restored


def uv_bounds_require_projection(
    bounds: tuple[float, float, float, float] | None,
    extension: str,
) -> bool:
    """Return whether an out-of-range UV needs baking into a new image.

    REPEAT/MIRROR materials deliberately use UVs outside the unit square.  A
    repeated island can be moved into an atlas tile directly; projecting it
    first and then wrapping it again during G4MG writing shifts the island a
    second time (the Tsubasa neck/ear case).  CLIP/EXTEND still need a projected
    source so their out-of-range sampling remains deterministic.
    """
    if not bounds or extension.upper() in {"REPEAT", "MIRROR"}:
        return False
    return bounds[0] < 0.0 or bounds[1] > 1.0 or bounds[2] < 0.0 or bounds[3] > 1.0


def uv_requires_projection(obj: bpy.types.Object) -> bool:
    return uv_bounds_require_projection(object_uv_bounds(obj), object_image_extension(obj))


def object_image_extension(obj: bpy.types.Object) -> str:
    for polygon in obj.data.polygons:
        if not 0 <= polygon.material_index < len(obj.data.materials):
            continue
        material = obj.data.materials[polygon.material_index]
        if material is None or material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None and node.image.filepath:
                return node.extension
    return "REPEAT"


def object_image_extension_for_material(obj: bpy.types.Object, material_index: int) -> str:
    material = obj.data.materials[material_index] if 0 <= material_index < len(obj.data.materials) else None
    if material is not None and material.node_tree is not None:
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None and node.image.filepath:
                return node.extension
    return "REPEAT"


def projected_source_image(
    source: tuple[int, int, array], bounds: tuple[float, float, float, float], extension: str
) -> tuple[int, int, array]:
    """Bake the source image over a mesh's UV domain so repeated UVs survive atlas fitting."""
    width, height, pixels = source
    min_u, max_u, min_v, max_v = bounds
    out = array("f", [0.0]) * (width * height * 4)
    for y in range(height):
        v = min_v + (max_v - min_v) * ((y + 0.5) / height)
        for x in range(width):
            u = min_u + (max_u - min_u) * ((x + 0.5) / width)
            if extension == "CLIP" and not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
                continue
            if extension == "REPEAT":
                u -= math.floor(u)
                v -= math.floor(v)
            else:
                u = max(0.0, min(1.0, u))
                v = max(0.0, min(1.0, v))
            src_x = min(width - 1, int(u * (width - 1)))
            src_y = min(height - 1, int(v * (height - 1)))
            src = (src_y * width + src_x) * 4
            dst = (y * width + x) * 4
            out[dst : dst + 4] = pixels[src : src + 4]
    return width, height, out


def set_object_uv_fit(obj: bpy.types.Object, origin_u: float, origin_v: float, width: float, height: float) -> None:
    bounds = object_uv_bounds(obj)
    uv = obj.level5_g4_port
    if bounds is None:
        uv.uv_scale_u = width
        uv.uv_scale_v = height
        uv.uv_offset_u = origin_u
        uv.uv_offset_v = origin_v
        return
    min_u, max_u, min_v, max_v = bounds
    bounds_u = max(max_u - min_u, 0.0001)
    bounds_v = max(max_v - min_v, 0.0001)
    scale = min(width / bounds_u, height / bounds_v)
    used_u = bounds_u * scale
    used_v = bounds_v * scale
    uv.uv_scale_u = scale
    uv.uv_scale_v = scale
    uv.uv_offset_u = origin_u + (width - used_u) * 0.5 - min_u * scale
    uv.uv_offset_v = origin_v + (height - used_v) * 0.5 - min_v * scale


def set_object_uv_tile(obj: bpy.types.Object, origin_u: float, origin_v: float, width: float, height: float) -> None:
    uv = obj.level5_g4_port
    uv.uv_scale_u = width
    uv.uv_scale_v = height
    uv.uv_offset_u = origin_u
    uv.uv_offset_v = origin_v


def object_uv_transform_summary(obj: bpy.types.Object) -> str:
    uv = obj.level5_g4_port
    bounds = object_uv_bounds(obj)
    bounds_text = "none" if bounds is None else "(" + ", ".join(f"{value:.6f}" for value in bounds) + ")"
    return (
        f"bounds={bounds_text} scale=({uv.uv_scale_u:.6f},{uv.uv_scale_v:.6f}) "
        f"offset=({uv.uv_offset_u:.6f},{uv.uv_offset_v:.6f})"
    )


def assign_texture_uv_tiles(records_by_texture: dict[str, list[G4PortRecord]]) -> None:
    for records in records_by_texture.values():
        items = [(record, obj) for record in records if not is_face_atlas_record(record) for obj in objects_for_record(record)]
        if not items:
            continue
        columns, rows = atlas_grid(len(items))
        scale_u = 1.0 / columns
        scale_v = 1.0 / rows
        for record in records:
            record.uv_scale_u = 1.0
            record.uv_scale_v = 1.0
            record.uv_offset_u = 0.0
            record.uv_offset_v = 0.0
        for index, (_, obj) in enumerate(items):
            column = index % columns
            row = index // columns
            set_object_uv_fit(obj, column * scale_u, row * scale_v, scale_u, scale_v)


def reset_uv_tiles(props: G4PortSceneSettings) -> None:
    for record in props.records:
        record.uv_scale_u = 1.0
        record.uv_scale_v = 1.0
        record.uv_offset_u = 0.0
        record.uv_offset_v = 0.0
    for obj in mesh_objects(False):
        uv = obj.level5_g4_port
        uv.uv_scale_u = 1.0
        uv.uv_scale_v = 1.0
        uv.uv_offset_u = 0.0
        uv.uv_offset_v = 0.0
        clear_object_material_uv_transforms(obj)


def records_grouped_by_texture(props: G4PortSceneSettings) -> dict[str, list[G4PortRecord]]:
    records_by_texture: dict[str, list[G4PortRecord]] = {}
    for record in props.records:
        if record.texture_key:
            records_by_texture.setdefault(record.texture_key, []).append(record)
    return records_by_texture


def auto_configure_uv_handling(props: G4PortSceneSettings) -> None:
    """Keep object atlas transforms enabled for every custom export.

    A native record can merge several Blender objects and source images.  The
    record count therefore cannot determine whether its UVs need packing.
    """
    props.auto_pack_source_uvs = True


def has_explicit_texture_replacements(props: G4PortSceneSettings) -> bool:
    """Return whether the user supplied a prepared image that must be preserved."""
    # The CSV is also rewritten by the exporter and can be stale scene state.
    # An entry path is the explicit user-owned source-of-truth for this decision.
    return any(entry.replacement_path and not entry.atlas_signature for entry in props.texture_entries)


def source_path_for_object(obj: bpy.types.Object) -> str:
    override = bpy.path.abspath(obj.level5_g4_port.source_texture)
    return override if override and Path(override).is_file() else first_used_material_image(obj)


def texture_entry(props: G4PortSceneSettings, texture_name: str) -> G4PortTextureReplacement | None:
    return next((entry for entry in props.texture_entries if entry.texture_name == texture_name), None)


def is_existing_face_atlas_image(path: str) -> bool:
    """Accept only authored images with the native 4:2 facial-atlas ratio."""
    image = load_image_pixels(path)
    return bool(
        image
        and image[0] > 0
        and image[1] > 0
        and image[0] * FACE_ATLAS_ROWS == image[1] * FACE_ATLAS_COLUMNS
    )


def existing_face_atlas_source(props: G4PortSceneSettings, texture_name: str) -> str:
    """Find one unambiguous authored 4:2 atlas for the shared face texture."""
    if not texture_name:
        return ""
    entry = texture_entry(props, texture_name)
    if entry is not None and entry.replacement_path:
        candidate = bpy.path.abspath(entry.replacement_path)
        return candidate if Path(candidate).is_file() and is_existing_face_atlas_image(candidate) else ""
    candidates = []
    for record in props.records:
        if not is_face_atlas_record(record):
            continue
        if record.texture_file:
            candidates.append(bpy.path.abspath(record.texture_file))
        candidates.extend(
            source_path_for_object(obj)
            for obj in objects_for_record(record)
        )
    valid = []
    for candidate in dict.fromkeys(candidates):
        if candidate and Path(candidate).is_file() and is_existing_face_atlas_image(candidate):
            valid.append(candidate)
    return valid[0] if len(valid) == 1 else ""


def adopt_existing_face_atlas(props: G4PortSceneSettings, log_path: Path | None = None) -> bool:
    """Adopt a single authored eye atlas without changing its UV domain."""
    texture_name = shared_face_texture_key([entry.texture_name for entry in props.texture_entries])
    entry = texture_entry(props, texture_name)
    if entry is None or entry.expression_atlas or entry.replacement_path:
        return False
    source = existing_face_atlas_source(props, texture_name)
    if not source:
        return False
    entry.replacement_path = source
    entry.expression_atlas = True
    entry.expression_atlas_mode = "existing"
    entry.atlas_signature = ""
    entry.atlas_summary = f"Existing 4x2 expression atlas, {Path(source).name}"
    for record in props.records:
        if is_face_atlas_record(record) and not record.texture_file:
            record.texture_file = source
    port_log(log_path, f"Adopted authored 4x2 face atlas: {Path(source).name}")
    return True


def atlas_signature(texture_name: str, records: list[G4PortRecord]) -> str:
    items = []
    for record, obj, material_name, material_index, source in texture_material_items_for_records(records):
        bounds = material_uv_bounds(obj, material_index)
        source_path = Path(source) if source else Path()
        items.append({
            "record": record.output_name,
            "object": obj.name,
            "material": material_name,
            "source": str(source_path),
            "source_mtime": source_path.stat().st_mtime_ns if source_path.is_file() else 0,
            "uv_bounds": [round(value, 6) for value in bounds] if bounds else [],
        })
    payload = json.dumps({"texture": texture_name, "items": items}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expression_pool_paths(props: G4PortSceneSettings) -> list[Path]:
    # Keep all eight slots, including an empty one.  Filtering empty slots
    # would shift every later image into an earlier expression cell.
    return [Path(bpy.path.abspath(item.image_path)) if item.image_path else Path() for item in props.expression_pool]


def expression_pool_signature(props: G4PortSceneSettings, texture_name: str) -> str:
    items = []
    for path in expression_pool_paths(props):
        items.append({
            "path": str(path),
            "mtime": path.stat().st_mtime_ns if path.is_file() else 0,
        })
    payload = json.dumps({"texture": texture_name, "layout": [FACE_ATLAS_COLUMNS, FACE_ATLAS_ROWS], "items": items}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def face_atlas_entry_ready(props: G4PortSceneSettings, entry: G4PortTextureReplacement) -> bool:
    if not entry.expression_atlas or not entry.replacement_path:
        return False
    if not Path(bpy.path.abspath(entry.replacement_path)).is_file():
        return False
    return entry.expression_atlas_mode == "existing" or entry.atlas_signature == expression_pool_signature(props, entry.texture_name)


def is_shared_face_atlas(texture_name: str, records) -> bool:
    return any(is_face_atlas_record(record) for record in records) and texture_name == shared_face_texture_key(
        [texture_name]
    )


def atlas_status_rows(props: G4PortSceneSettings, validate_images: bool = False) -> list[dict]:
    rows = []
    grouped = records_grouped_by_texture(props)
    names = list(grouped)
    names.extend(entry.texture_name for entry in props.texture_entries if entry.texture_name not in grouped)
    for texture_name in names:
        if is_special_texture(texture_name):
            continue
        records = grouped.get(texture_name, [])
        entry = texture_entry(props, texture_name)
        shared_face = is_shared_face_atlas(texture_name, records)
        signature = atlas_signature(texture_name, records)
        surfaces = texture_material_items_for_records(records)
        missing = [f"{obj.name}::{material_name}" for _, obj, material_name, _, source in surfaces if not source]
        unreadable = [
            f"{obj.name}::{material_name}" for _, obj, material_name, _, source in surfaces
            if source and load_image_pixels(source) is None
        ] if validate_images and not shared_face else []
        repeated = [
            f"{obj.name}::{material_name}" for _, obj, material_name, material_index, _ in surfaces
            if (bounds := material_uv_bounds(obj, material_index))
            and (bounds[0] < 0.0 or bounds[1] > 1.0 or bounds[2] < 0.0 or bounds[3] > 1.0)
        ]
        prepared = entry and entry.replacement_path and Path(bpy.path.abspath(entry.replacement_path)).is_file()
        fresh = prepared and entry.atlas_signature == signature
        if entry is not None and entry.expression_atlas:
            pool_paths = expression_pool_paths(props)
            signature = expression_pool_signature(props, texture_name)
            prepared = entry.replacement_path and Path(bpy.path.abspath(entry.replacement_path)).is_file()
            if entry.expression_atlas_mode == "existing":
                state, message = (
                    ("ready", entry.atlas_summary or "Existing 4x2 expression atlas")
                    if prepared else ("warning", "Existing 4x2 expression atlas is missing")
                )
            elif len(pool_paths) != FACE_ATLAS_SLOTS or not all(path.is_file() for path in pool_paths):
                state, message = "warning", "Expression pool needs 8 valid images (4x2)"
            elif prepared and entry.atlas_signature == signature:
                state, message = "ready", entry.atlas_summary or "Prepared 4x2 expression atlas"
            else:
                state, message = "stale", "Expression atlas needs rebuilding"
        elif shared_face:
            state, message = "native", "Shared eye/mouth 4x2 atlas preserved"
        elif missing:
            state, message = "warning", f"Missing source: {', '.join(missing)}"
        elif unreadable:
            state, message = "warning", f"Unreadable source: {', '.join(unreadable)}; native G4TX entry will be preserved"
        elif not surfaces:
            state, message = "native", "No assigned meshes; native G4TX entry will be preserved"
        elif fresh:
            state, message = "ready", entry.atlas_summary or "Prepared atlas"
        elif prepared and not entry.atlas_signature:
            state, message = "manual", "Manual replacement"
        elif prepared:
            state, message = "stale", "Atlas needs regeneration"
        else:
            state, message = "native", "Native G4TX entry will be preserved"
        rows.append({"name": texture_name, "records": records, "signature": signature, "state": state, "message": message, "repeated": repeated, "shared_face": shared_face})
    return rows


def object_texture_path(record: G4PortRecord, obj: bpy.types.Object) -> str:
    return source_path_for_object(obj)


def sibling_texture_path(path: str, suffix: str) -> str:
    if not path:
        return ""
    source = Path(bpy.path.abspath(path))
    names = [
        source.with_name(f"{source.stem}{suffix}{source.suffix}"),
        source.with_name(f"{source.stem}_{suffix}{source.suffix}"),
    ]
    for candidate in names:
        if candidate.is_file():
            return str(candidate)
    return ""


def object_special_texture_path(
    record: G4PortRecord,
    obj: bpy.types.Object,
    texture_name: str,
    explicit_map: dict[str, str],
    texture_source_dir: Path,
) -> str:
    explicit = explicit_map.get(texture_name)
    if explicit:
        source = texture_source_dir / explicit
        if source.is_file():
            return str(source)
        absolute = Path(bpy.path.abspath(explicit))
        if absolute.is_file():
            return str(absolute)
    suffix = special_texture_suffix(texture_name)
    for base in (source_path_for_object(obj), bpy.path.abspath(record.texture_file or "")):
        if not base:
            continue
        source = Path(bpy.path.abspath(base))
        if source.parent == texture_source_dir and source.stem.startswith(record.texture_key):
            continue
        sibling = sibling_texture_path(base, suffix)
        if sibling:
            return sibling
    return ""


def texture_items_for_records(records: list[G4PortRecord]) -> list[tuple[G4PortRecord, bpy.types.Object]]:
    items = []
    for record in records:
        items.extend((record, obj) for obj in sorted(objects_for_record(record), key=lambda item: item.name.casefold()))
    return items


def texture_material_items_for_records(records: list[G4PortRecord]) -> list[tuple[G4PortRecord, bpy.types.Object, str, int, str]]:
    items = []
    for record in records:
        for obj in sorted(objects_for_record(record), key=lambda item: item.name.casefold()):
            material_indices = sorted({polygon.material_index for polygon in obj.data.polygons})
            if not material_indices:
                material_indices = [0]
            for material_index in material_indices:
                material = obj.data.materials[material_index] if material_index < len(obj.data.materials) else None
                material_name = material.name if material is not None else "material"
                override = bpy.path.abspath(obj.level5_g4_port.source_texture)
                source_path = (
                    override if override and Path(override).is_file()
                    else material_image_path(material)
                )
                items.append((record, obj, material_name, material_index, source_path))
    return items


def build_texture_spritesheet(
    path: Path,
    entry: dict,
    records: list[G4PortRecord],
    props: G4PortSceneSettings,
    source_path_for_item=None,
    empty_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0),
    draw_missing_guides: bool = True,
    assign_record_texture_file: bool = False,
    log_path: Path | None = None,
    update_uv_transforms: bool = True,
) -> bool:
    items = texture_material_items_for_records(records)
    if not items:
        port_log(log_path, f"{entry['name']}: skipped spritesheet; no assigned material surfaces")
        return False
    port_log(log_path, f"{entry['name']}: loading {len(items)} source material surface(s)")
    sources = []
    source_cache = {}
    for index, (record, obj, material_name, material_index, detected_path) in enumerate(items, 1):
        source_path = source_path_for_item(record, obj) if source_path_for_item is not None else detected_path
        bounds = material_uv_bounds(obj, material_index)
        extension = object_image_extension_for_material(obj, material_index)
        port_log(
            log_path,
            f"{entry['name']}: [{index}/{len(items)}] {obj.name}::{material_name} record={record.output_name} "
            f"source={source_path or '<none>'} uv_bounds={bounds or '<none>'} extension={extension}",
        )
        started = time.perf_counter()
        cache_key = str(Path(bpy.path.abspath(source_path)).resolve()) if source_path else ""
        source = source_cache.get(cache_key) if cache_key else None
        if source is None and cache_key:
            source = load_image_pixels(source_path)
            if source is not None:
                source = bleed_transparent_pixels(source)
                source_cache[cache_key] = source
        if source is not None and uv_bounds_require_projection(bounds, extension):
            source = projected_source_image(source, bounds, extension)
            cache_key = f"{cache_key}|projection:{obj.name}:{material_name}"
        if source is not None:
            source = bleed_transparent_pixels(source)
        elapsed = time.perf_counter() - started
        if source is None:
            port_log(log_path, f"{entry['name']}: [{index}/{len(items)}] no readable source image ({elapsed:.2f}s)")
        else:
            port_log(log_path, f"{entry['name']}: [{index}/{len(items)}] loaded {source[0]}x{source[1]} ({elapsed:.2f}s)")
        sources.append((record, obj, material_name, bounds, source, cache_key))

    unreadable = [f"{obj.name}::{material_name}" for _, obj, material_name, _, source, _ in sources if source is None]
    if unreadable:
        port_log(
            log_path,
            f"{entry['name']}: skipped spritesheet; unreadable source image for {', '.join(unreadable)}",
        )
        return False

    if not props.auto_pack_source_uvs:
        distinct_sources = {cache_key for _, _, _, _, _, cache_key in sources}
        if len(distinct_sources) != 1:
            port_log(log_path, f"{entry['name']}: cannot preserve UVs for multiple source materials without atlas packing")
            return False
        primary = sources[0][4]
        for _, obj, _, _, _, _ in sources:
            obj_uv = obj.level5_g4_port
            obj_uv.uv_scale_u = 1.0
            obj_uv.uv_scale_v = 1.0
            obj_uv.uv_offset_u = 0.0
            obj_uv.uv_offset_v = 0.0
            clear_object_material_uv_transforms(obj)
        save_png(path, *primary)
        props.use_source_uv_transforms = False
        port_log(log_path, f"{entry['name']}: preserved original UV layout using the primary source image")
        return True

    groups = []
    grouped_sources = {}
    for record, obj, material_name, bounds, source, cache_key in sources:
        if source is None or not cache_key:
            groups.append({"surfaces": [(record, obj, material_name, bounds)], "source": source, "key": f"{obj.name}::{material_name}"})
            continue
        group = grouped_sources.get(cache_key)
        if group is None:
            group = {"surfaces": [], "source": source, "key": cache_key, "projected": "|projection:" in cache_key}
            grouped_sources[cache_key] = group
            groups.append(group)
        group["surfaces"].append((record, obj, material_name, bounds))

    columns, rows = atlas_grid(len(groups))
    max_source_width = max((group["source"][0] for group in groups if group["source"] is not None), default=entry["width"])
    max_source_height = max((group["source"][1] for group in groups if group["source"] is not None), default=entry["height"])
    width, height, cell_width, cell_height = capped_atlas_dimensions(
        entry["width"], entry["height"], max_source_width, max_source_height, columns, rows
    )
    port_log(
        log_path,
        f"{entry['name']}: atlas {width}x{height}, grid={columns}x{rows}, cell={cell_width}x{cell_height}, groups={len(groups)}",
    )
    pixels = image_pixels(width, height, empty_color)
    for record in records:
        record.uv_scale_u = 1.0
        record.uv_scale_v = 1.0
        record.uv_offset_u = 0.0
        record.uv_offset_v = 0.0
        for obj in objects_for_record(record):
            obj.level5_g4_port.uv_scale_u = 1.0
            obj.level5_g4_port.uv_scale_v = 1.0
            obj.level5_g4_port.uv_offset_u = 0.0
            obj.level5_g4_port.uv_offset_v = 0.0
            clear_object_material_uv_transforms(obj)
    for index, group in enumerate(groups):
        column = index % columns
        row = index // columns
        cell_x = column * cell_width
        cell_y = (rows - 1 - row) * cell_height
        source = group["source"]
        if source is None:
            origin_u = cell_x / width
            origin_v = cell_y / height
            rect_u = cell_width / width
            rect_v = cell_height / height
            for record, obj, material_name, bounds in group["surfaces"]:
                if update_uv_transforms:
                    set_material_uv_fit(obj, material_name, bounds, origin_u, origin_v, rect_u, rect_v)
            continue
        port_log(log_path, f"{entry['name']}: blitting group {index + 1}/{len(groups)} ({len(group['surfaces'])} surface(s)) into cell ({column}, {row})")
        started = time.perf_counter()
        draw_x, draw_y, draw_width, draw_height = blit_image_fit(
            pixels, width, height, source, cell_x, cell_y, cell_width, cell_height, ATLAS_GUTTER_PIXELS
        )
        port_log(
            log_path,
            f"{entry['name']}: blit group done as {draw_width}x{draw_height} ({time.perf_counter() - started:.2f}s)",
        )
        if update_uv_transforms:
            for _, obj, material_name, bounds in group["surfaces"]:
                if group["projected"]:
                    set_material_uv_fit(
                        obj, material_name, bounds, draw_x / width, draw_y / height,
                        draw_width / width, draw_height / height,
                    )
                    transform_mode = "projection-fit"
                else:
                    set_object_material_uv_transform(
                        obj, material_name, (draw_width / width, draw_height / height),
                        (draw_x / width, draw_y / height),
                    )
                    transform_mode = "shared-image-tile"
                port_log(
                    log_path,
                    f"{entry['name']}: {obj.name}::{material_name} group={index + 1} cell=({column},{row}) "
                    f"content_px=({draw_x},{draw_y},{draw_width},{draw_height}) mode={transform_mode}",
                )
    port_log(log_path, f"{entry['name']}: saving {path}")
    started = time.perf_counter()
    save_png(path, width, height, pixels)
    port_log(log_path, f"{entry['name']}: saved ({time.perf_counter() - started:.2f}s)")
    props.use_source_uv_transforms = True
    return True


def discard_generated_atlas(entry: G4PortTextureReplacement | None, generated_path: Path) -> None:
    """Forget only a failed atlas created by this exporter; manual paths stay intact."""
    if entry is None:
        return
    source = Path(bpy.path.abspath(entry.replacement_path)) if entry.replacement_path else None
    is_generated_path = source is not None and source == generated_path
    if not is_generated_path and not entry.atlas_signature:
        return
    if is_generated_path and source.is_file():
        source.unlink()
    entry.replacement_path = ""
    entry.atlas_signature = ""
    entry.atlas_summary = ""


def generate_texture_png_set(context, output_dir: Path, log_path: Path | None = None) -> int:
    props = settings(context)
    normalize_shared_face_record_keys(props)
    port_log(log_path, "Generate PNG set started")
    port_log(log_path, f"Output folder: {output_dir}")
    port_log(log_path, f"Max generated texture size: {MAX_GENERATED_TEXTURE_SIZE}")
    original_model = resolve_file(props.original_model)
    raw_root = infer_data_root(original_model) if original_model.is_file() else None
    if raw_root is None:
        raise RuntimeError("Choose an original G4MD/G4PKM before generating texture PNGs.")
    port_log(log_path, f"Original model: {original_model}")
    port_log(log_path, f"Raw data root: {raw_root}")
    g4tx_path = original_g4tx_path(raw_root, props.model_rel)
    if g4tx_path is None:
        raise RuntimeError(f"Original G4TX not found for {props.model_rel}")
    adopt_existing_face_atlas(props, log_path)
    entries = parse_g4tx_entries(g4tx_path)
    port_log(log_path, f"Original G4TX entries: {len(entries)}")
    output_dir.mkdir(parents=True, exist_ok=True)
    replacements = [
        f"{entry.texture_name}={Path(entry.replacement_path).name}"
        for entry in props.texture_entries
        if face_atlas_entry_ready(props, entry)
    ]
    records_by_texture = records_grouped_by_texture(props)
    explicit_map = props.texture_map()
    texture_source_dir = resolve_file(props.texture_source_dir)
    port_log(log_path, f"Texture source folder: {texture_source_dir if texture_source_dir else '<none>'}")
    port_log(log_path, f"Texture groups: {', '.join(f'{key}={len(value)}' for key, value in records_by_texture.items()) or '<none>'}")
    port_log(log_path, f"Auto pack source UVs: {props.auto_pack_source_uvs}; use source UV transforms: {props.use_source_uv_transforms}")
    if props.auto_pack_source_uvs:
        port_log(log_path, "Packing source materials into atlas UV tiles")
        props.use_source_uv_transforms = True
    elif not props.use_source_uv_transforms:
        port_log(log_path, "Resetting object UV tiles")
        reset_uv_tiles(props)
    for index, entry in enumerate(entries, 1):
        name = entry["name"]
        path = output_dir / f"{name}.png"
        port_log(log_path, f"[{index}/{len(entries)}] Processing {name} ({entry['width']}x{entry['height']})")
        if is_special_texture(name):
            if not props.replace_special_textures:
                port_log(log_path, f"{name}: preserving native special map")
                continue
            write_special_texture_default(path, name)
            replacements.append(f"{name}={path.name}")
            port_log(
                log_path,
                f"{name}: wrote neutral 32x32 special map {special_texture_default_color(name)}",
            )
        else:
            records = records_by_texture.get(name, [])
            port_log(log_path, f"{name}: base map, records={len(records)}")
            if is_shared_face_atlas(name, records):
                entry = texture_entry(props, name)
                if entry is not None and entry.expression_atlas:
                    port_log(log_path, f"{name}: retaining prepared 4x2 expression atlas")
                else:
                    port_log(log_path, f"{name}: retaining native shared eye/mouth atlas")
                continue
            missing_sources = [
                f"{obj.name}::{material_name}"
                for _, obj, material_name, _, source in texture_material_items_for_records(records)
                if not source
            ]
            if missing_sources:
                port_log(log_path, f"{name}: missing source image for {', '.join(missing_sources)}; preserving native G4TX entry")
                discard_generated_atlas(texture_entry(props, name), path)
                continue
            if build_texture_spritesheet(path, entry, records, props, log_path=log_path):
                replacements.append(f"{name}={path.name}")
                atlas_entry = texture_entry(props, name)
                if atlas_entry is not None:
                    atlas_entry.atlas_signature = atlas_signature(name, records)
                    atlas_size = load_image_pixels(str(path))
                    dimensions = f"{atlas_size[0]}x{atlas_size[1]}" if atlas_size is not None else "unknown size"
                    atlas_entry.atlas_summary = (
                        f"{len(texture_material_items_for_records(records))} material surface(s), {dimensions}, {path.name}"
                    )
            else:
                port_log(log_path, f"{name}: unreadable source; preserving native G4TX entry")
                discard_generated_atlas(texture_entry(props, name), path)
    props.texture_source_dir = str(output_dir)
    props.texture_replacements = join_csv(replacements)
    generated = dict(item.split("=", 1) for item in replacements)
    for entry in props.texture_entries:
        if entry.texture_name in generated:
            entry.replacement_path = str(output_dir / generated[entry.texture_name])
    # Preparing an atlas is an explicit request for custom texture export. Keep
    # that intent for the following Export action so its geometry and G4TX use the
    # same atlas layout.
    props.generate_png_set_on_export = True
    port_log(log_path, f"Generate PNG set finished; replacements={len(replacements)}")
    return len(replacements)


def atlas_pixel_rect_uv_transform(
    atlas_width: int, atlas_height: int, rect: tuple[int, int, int, int]
) -> tuple[tuple[float, float], tuple[float, float]]:
    if atlas_width <= 0 or atlas_height <= 0:
        raise ValueError("atlas dimensions must be positive")
    x, y, width, height = rect
    return (width / atlas_width, height / atlas_height), (x / atlas_width, y / atlas_height)


def build_expression_pool_atlas(props: G4PortSceneSettings, output_dir: Path, log_path: Path | None = None) -> Path:
    normalize_shared_face_record_keys(props)
    texture_name = shared_face_texture_key([entry.texture_name for entry in props.texture_entries])
    entry = texture_entry(props, texture_name)
    if entry is None:
        raise RuntimeError("The original model has no shared eye/mouth texture entry")
    sources = expression_pool_paths(props)
    if len(sources) != FACE_ATLAS_SLOTS or not all(path.is_file() for path in sources):
        raise RuntimeError("Expression pool requires exactly 8 valid images for its 4x2 layout")
    original_model = resolve_file(props.original_model)
    raw_root = infer_data_root(original_model) if original_model.is_file() else None
    g4tx_path = original_g4tx_path(raw_root, props.model_rel) if raw_root is not None else None
    source_entry = next((item for item in parse_g4tx_entries(g4tx_path) if item["name"] == texture_name), None) if g4tx_path else None
    if source_entry is None:
        raise RuntimeError(f"Native G4TX entry not found: {texture_name}")
    width, height = source_entry["width"], source_entry["height"]
    cell_width = width // FACE_ATLAS_COLUMNS
    cell_height = height // FACE_ATLAS_ROWS
    pixels = image_pixels(width, height, (0.0, 0.0, 0.0, 0.0))
    first_sprite_rect: tuple[int, int, int, int] | None = None
    for index, source_path in enumerate(sources):
        source = load_image_pixels(str(source_path))
        if source is None:
            raise RuntimeError(f"Could not read expression image: {source_path}")
        column = index % FACE_ATLAS_COLUMNS
        row = index // FACE_ATLAS_COLUMNS
        # The UI labels Row 1 as the top row, while Blender image pixels start
        # at the bottom.  Convert only at this boundary so slot order remains
        # the visible left-to-right, top-to-bottom order.
        cell_y = (FACE_ATLAS_ROWS - 1 - row) * cell_height
        placed_rect = blit_image_fit(
            pixels,
            width,
            height,
            bleed_transparent_pixels(source),
            column * cell_width,
            cell_y,
            cell_width,
            cell_height,
        )
        if index == 0:
            first_sprite_rect = placed_rect
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{texture_name}.png"
    save_png(output_path, width, height, pixels)
    entry.replacement_path = str(output_path)
    entry.atlas_signature = expression_pool_signature(props, texture_name)
    entry.atlas_summary = f"{FACE_ATLAS_COLUMNS}x{FACE_ATLAS_ROWS} expression atlas, {width}x{height}, {output_path.name}"
    entry.expression_atlas = True
    entry.expression_atlas_mode = "pool"
    props.texture_source_dir = str(output_dir)
    # Expression-atlas cells contain complete face expressions.  The source face and mouth
    # therefore both sample the exact pixel rectangle occupied by Cell 1
    # (top-left).  This transform belongs to the native eye/mouth record, not
    # to individual Blender objects: record UV transforms are applied after
    # the global G4 V flip, so the sprite stays in the intended atlas row and
    # cannot be lost when Blender renames temporary Collada copies.
    face_records = [record for record in props.records if is_face_atlas_record(record) and record.texture_key == texture_name]
    if first_sprite_rect is None:
        raise RuntimeError("Expression atlas did not produce a first sprite rectangle")
    (scale_u, scale_v), (origin_u, origin_v) = atlas_pixel_rect_uv_transform(
        width, height, first_sprite_rect
    )
    for record in face_records:
        record.uv_scale_u = scale_u
        record.uv_scale_v = scale_v
        record.uv_offset_u = origin_u
        record.uv_offset_v = origin_v
        for obj in objects_for_record(record):
            # Face atlas placement is record-wide.  Keep object transforms at
            # identity so the source UVs are not scaled twice.
            set_object_uv_tile(obj, 0.0, 0.0, 1.0, 1.0)
            port_log(
                log_path,
                f"{texture_name}: expression Cell 1 display target -> {obj.name} ({record.output_name}) "
                f"scale=({scale_u:.6f},{scale_v:.6f}) offset=({origin_u:.6f},{origin_v:.6f})",
            )
    if face_records:
        # Keep transforms already prepared for other atlas-backed records.
        # The expression pool adds another transformed texture; it must never
        # disable an existing body/hair atlas mapping.
        props.use_source_uv_transforms = True
    return output_path


def legacy_native_mesh_matches_source(obj, original_model: Path, native_index: int) -> bool:
    """Verify the geometry and UVs of a pre-v2 import before upgrading its snapshot."""
    if original_model.suffix.lower() != ".g4md" or native_index < 0:
        return False
    g4mg_path = original_model.with_suffix(".g4mg")
    if not g4mg_path.is_file():
        return False
    try:
        from .g4_model_probe import parse_g4md, read_uv0
    except ImportError:
        from porting.g4_model_probe import parse_g4md, read_uv0
    g4mg = g4mg_path.read_bytes()
    model = parse_g4md(original_model.read_bytes(), g4mg)
    if native_index >= len(model["records"]):
        return False
    record = model["records"][native_index]
    if len(obj.data.vertices) != record["vertex_count"]:
        return False
    for index, vertex in enumerate(obj.data.vertices):
        offset = record["vertex_offset"] + index * record["vertex_stride"]
        native_position = struct.unpack_from("<3f", g4mg, offset)
        if any(abs(value - expected) > 1e-5 for value, expected in zip(vertex.co, native_position)):
            return False
    uv_layer = obj.data.uv_layers.active
    if uv_layer is None:
        return False
    for loop in obj.data.loops:
        native_uv = read_uv0(g4mg, model, record, loop.vertex_index)
        current_uv = uv_layer.data[loop.index].uv
        if abs(current_uv.x - native_uv[0]) > 1e-5 or abs(current_uv.y - native_uv[1]) > 1e-5:
            return False
    return all(abs(value - expected) <= 1e-5 for row, expected_row in zip(obj.matrix_world, ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))) for value, expected in zip(row, expected_row))


def has_unchanged_native_roundtrip(props: G4PortSceneSettings, original_model: Path) -> bool:
    """True only when the current assignment is an untouched native import."""
    if not props.preserve_native_roundtrip or original_model.suffix.lower() != ".g4md":
        return False
    if props.disable_edge2 or props.use_authored_outlines:
        return False
    if props.texture_mode == "keep":
        return False
    # An on-export rebuild or an enabled object transform is an explicit
    # custom-atlas request even before a PNG exists.  Do not short-circuit it
    # into a byte copy, otherwise the generated atlas has no matching UVs.
    if props.texture_mode == "custom" and (
        props.texture_map()
        or props.generate_png_set_on_export
        or props.use_source_uv_transforms
        or props.auto_pack_source_uvs
    ):
        return False
    source = str(original_model.resolve())
    records = list(props.records)
    if not records:
        return False
    for record in records:
        objects = objects_for_record(record)
        if not objects:
            return False
        for obj in objects:
            if obj.get("g4_native_model_source") != source:
                return False
            signature_matches = (
                obj.get("g4_native_roundtrip_signature_version") == NATIVE_ROUNDTRIP_SIGNATURE_VERSION
                and obj.get("g4_native_roundtrip_signature") == native_mesh_signature(obj)
            )
            if not signature_matches:
                if not legacy_native_mesh_matches_source(obj, original_model, record.original_index):
                    return False
                obj["g4_native_roundtrip_signature"] = native_mesh_signature(obj)
                obj["g4_native_roundtrip_signature_version"] = NATIVE_ROUNDTRIP_SIGNATURE_VERSION
            target = getattr(obj.level5_g4_port, "target_record", "__none__")
            if target not in {"__none__", record.output_name}:
                return False
    return True


def copy_unchanged_native_roundtrip(
    props: G4PortSceneSettings,
    original_model: Path,
    raw_root: Path,
    package_root: Path,
    source_g4tx: Path,
) -> dict:
    """Preserve native bytes for an identity import/export instead of rebuilding records."""
    output_root = package_root / "data"
    common_rel = Path(props.model_rel).with_suffix(".g4md")
    common_out = output_root / "common" / common_rel
    g4mg_source = original_model.with_suffix(".g4mg")
    if not g4mg_source.is_file():
        raise RuntimeError(f"Original G4MG not found next to {original_model.name}")
    common_out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original_model, common_out)
    shutil.copy2(g4mg_source, common_out.with_suffix(".g4mg"))

    texture_out_dir = output_root / source_g4tx.parent.resolve().relative_to(raw_root.resolve())
    texture_out_dir.mkdir(parents=True, exist_ok=True)
    for source in source_g4tx.parent.glob("*.g4tx"):
        shutil.copy2(source, texture_out_dir / source.name)
    entries = parse_g4tx_entries(source_g4tx)
    return {
        "meshes": len(props.records),
        "textures": len(entries),
        "roundtrip_preserved": True,
        "g4md": str(common_out),
        "g4mg": str(common_out.with_suffix(".g4mg")),
        "g4tx": str(texture_out_dir / source_g4tx.name),
        "texture_platform": source_g4tx.parent.parent.name,
        "package_root": str(package_root),
        "data_root": str(output_root),
    }


def write_uv_export_trace(props: G4PortSceneSettings, config: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "G4 atlas UV export trace",
        "UV pipeline: expression-face-v9",
        f"Texture replacements: {config.get('texture_replacements', {})}",
    ]
    for index, record in enumerate(config.get("records", [])):
        transforms = record.get("source_uv_transforms", {})
        props_record = props.records[index] if index < len(props.records) else None
        raw_key = props_record.texture_key if props_record is not None else ""
        effective_key = resolved_record_texture_key(props, props_record) if props_record is not None else ""
        lines.append(
            f"Record {record.get('output_name')}: source transforms={len(transforms)} "
            f"record_scale={record.get('uv_scale', [1.0, 1.0])} record_offset={record.get('uv_offset', [0.0, 0.0])} "
            f"fit_display_rect={record.get('uv_fit_display_rect')} "
            f"texture_key={raw_key!r} effective_texture_key={effective_key!r}"
        )
        for name, transform in transforms.items():
            lines.append(f"  {name}: scale={transform.get('scale')} offset={transform.get('offset')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def enforce_g4_uv_orientation(props: G4PortSceneSettings) -> bool:
    """G4 stores V upside-down; custom atlas transforms require the matching pack flip."""
    if props.texture_mode != "custom":
        return False
    if not (props.texture_map() or props.generate_png_set_on_export or props.use_source_uv_transforms or props.auto_pack_source_uvs):
        return False
    if props.global_uv_flip_y:
        return False
    props.global_uv_flip_y = True
    return True


def run_port(context, filepath: str = "") -> tuple[dict, Path]:
    prefs = addon_preferences()
    props = ensure_scene_defaults(context)
    explicit_texture_replacements = props.texture_mode == "custom" and has_explicit_texture_replacements(props)
    if not explicit_texture_replacements:
        props.use_source_uv_transforms = True
        props.auto_pack_source_uvs = True
    repaired_face_keys = normalize_shared_face_record_keys(props)
    if repaired_face_keys:
        port_log(None, f"Repaired shared face texture key on {repaired_face_keys} record(s)")
    
    # A custom G4TX must always be generated from the source materials.  This
    # is true for a single-record uniform too: skipping the atlas in that case
    # copied the template outfit's G4TX and made a successful mesh port appear
    # to have the wrong texture in game.
    if not explicit_texture_replacements:
        auto_configure_uv_handling(props)
    
    original_model = resolve_file(props.original_model)
    if not original_model.is_file():
        raise RuntimeError(
            "Choose the original model first: in the Level-5 > G4 Port panel, "
            "press 'Choose Original G4MD/G4PKM' and select the base .g4md or .g4pkm "
            "from data/common or data/dx11."
        )
    raw_root = infer_data_root(original_model)
    if raw_root is None:
        raise RuntimeError("The original model must be inside a data/common or data/dx11 filesystem tree.")
    source_g4tx = original_g4tx_path(raw_root, props.model_rel)
    if source_g4tx is None:
        raise RuntimeError(f"Original G4TX not found in DX11 or NX for {props.model_rel}")
    adopt_existing_face_atlas(props)

    package_root = Path(bpy.path.abspath(filepath)) if filepath else resolve_file(getattr(prefs, "output_root", ""))
    if enforce_g4_uv_orientation(props):
        port_log(None, "Enabled Global Flip V for custom atlas export (G4 stores V inverted)")
    if (has_unchanged_native_roundtrip(props, original_model)
            and not props.target_g4sk_path and not props.prepare_victorytool_profile):
        report = copy_unchanged_native_roundtrip(props, original_model, raw_root, package_root, source_g4tx)
        report["g4_blender_plugin_version"] = g4_blender_plugin_version()
        report["g4_port_script_version_expected"] = G4_PORT_SCRIPT_VERSION_EXPECTED
        report["g4_blender_addon_marker"] = G4_PORT_ADDON_MARKER
        report["g4_blender_addon_source"] = str(Path(__file__).resolve())
        if props.prepare_victorytool_profile:
            armature = export_armature()
            if armature is None:
                raise RuntimeError("Could not resolve the character armature for animation export")
            armature["g4_exported_model_path"] = str(Path(report["g4md"]).resolve())
            target_skeleton = active_rig_skeleton_path(props, original_model)
            if target_skeleton is None or not target_skeleton.is_file():
                raise RuntimeError("Could not resolve the G4SK used by the exported model")
            armature["g4_exported_skeleton_path"] = str(target_skeleton.resolve())
            profile_path = write_animation_profile(
                props, original_model, Path(report["g4md"]), target_skeleton, raw_root
            )
            report["animation_profile"] = str(profile_path)
        cache = resolve_file(getattr(prefs, "cache_dir", ""), default_cache_dir())
        cache.mkdir(parents=True, exist_ok=True)
        report_path = cache / "export_report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        port_log(None, "Untouched native import detected; copied original G4MD/G4MG/G4TX bytes")
        return report, report_path

    cache = resolve_file(getattr(prefs, "cache_dir", ""), default_cache_dir())
    cache.mkdir(parents=True, exist_ok=True)

    mesh_data_path = cache / "scene_export.json"
    weights_path = cache / "scene_weights.json"
    report_path = cache / ("analyze_report.json" if props.analyze_only else "export_report.json")
    output_root = package_root / "data"

    if props.texture_mode == "custom":
        model_name = Path(props.model_rel).name or "model"
        prepared_texture_dir = package_root / "texture_sources" / model_name
        atlas_rows = atlas_status_rows(props, validate_images=True)
        refresh_needed = [
            row for row in atlas_rows
            if row["state"] in {"native", "stale"} and not row.get("shared_face", False)
        ]
        warnings = [row for row in atlas_rows if row["state"] == "warning"]
        for row in warnings:
            port_log(None, f"Texture {row['name']}: {row['message']}; preserving native G4TX entry")

        # Auto-packed atlas UVs live on the Blender objects.  A ready PNG can
        # survive between exports while those per-object values are reset or
        # come from an older scene state, so rebuild/reapply the packing on
        # every export that has Auto Pack enabled.  This keeps the PNG and the
        # G4MG UV transforms as one atomic operation.
        if explicit_texture_replacements:
            port_log(None, "Using explicit prepared texture replacements; preserving source UV layout")
        else:
            generate_texture_png_set(context, prepared_texture_dir)

        # Rebuild the expression pool before serialising configuration or
        # copying customTextures.  Besides writing the 4x2 PNG this reapplies
        # the Cell-1 UV transform to the explicitly assigned eye/mouth source
        # objects.
        try:
            pool_paths = expression_pool_paths(props)
            if len(pool_paths) == FACE_ATLAS_SLOTS and all(path.is_file() for path in pool_paths):
                build_expression_pool_atlas(props, prepared_texture_dir)
        except Exception as exc:
            port_log(None, f"Note: Could not auto-build expression atlas: {exc}")

    if not props.use_source_uv_transforms and not props.auto_pack_source_uvs:
        reset_uv_tiles(props)

    # All generated atlases must exist before this copy.  Previously the
    # expression pool was built afterwards, so a fresh c*_10 atlas could miss
    # the G4TX replacement directory entirely.
    prepare_custom_textures(props, mesh_data_path)

    config = generated_config_path(cache)
    config_data = props.to_config()
    if props.texture_mode == "custom" and not props.use_authored_outlines:
        config_data["vertex_color_mode"] = "constant"
        config_data["vertex_color"] = list(STABLE_CUSTOM_VERTEX_COLOR)
    rig_reference = active_rig_reference_model(props, original_model)
    rig_skeleton = active_rig_skeleton_path(props, original_model)
    armature = export_armature()
    generated_target_skeleton = bool(not props.analyze_only and props.prepare_victorytool_profile)
    if generated_target_skeleton:
        config_data["joint_aliases"] = {}
    export_port_meshes_from_scene(
        mesh_data_path, props.selected_only, True, True
    )
    mesh_count = write_weights_json(weights_path, props.selected_only)
    if mesh_count == 0:
        raise RuntimeError("No mesh objects were found to export.")

    if generated_target_skeleton:
        if armature is None or rig_skeleton is None:
            raise RuntimeError("A target armature and G4SK template are required for VictoryTool export")
        try:
            from ..animation.g4_animation_addon import build_g4sk_from_armature
        except ImportError:
            from animation.g4_animation_addon import build_g4sk_from_armature
        skeleton_data, joint_count, mesh_count, srt_error = build_g4sk_from_armature(
            armature, rig_skeleton.read_bytes()
        )
        generated_skeleton_path = cache / f"{Path(props.model_rel).stem}_target.g4sk"
        handle, temporary_skeleton = tempfile.mkstemp(
            prefix=f".{generated_skeleton_path.name}.", suffix=".tmp", dir=cache
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(skeleton_data)
            os.replace(temporary_skeleton, generated_skeleton_path)
        finally:
            if os.path.exists(temporary_skeleton):
                os.unlink(temporary_skeleton)
        rig_skeleton = generated_skeleton_path
        config_data["target_g4sk_path"] = str(generated_skeleton_path)
        port_log(None, f"Built target G4SK from {joint_count} armature bones and {mesh_count} weighted meshes")
    local_bind_translations = (
        {} if generated_target_skeleton else local_g4sk_bind_translations(props, original_model)
    )
    if local_bind_translations:
        config_data["local_g4sk_bind_translations"] = local_bind_translations
        port_log(None, f"Writing {len(local_bind_translations)} local G4SK bind translations")
    rest_translations = counter_rotated_rest_translations(props, original_model)
    if rest_translations:
        config_data["source_joint_position_transforms"] = rest_translations
        port_log(None, f"Translated {len(rest_translations)} source joints onto {rig_reference.name} rest space")
    else:
        port_log(None, f"Using {rig_reference.name} to validate skinning; leaving authored rest positions unchanged")
    config_data["rig_reference_model"] = str(rig_reference)
    config_data["g4_blender_plugin_version"] = g4_blender_plugin_version()
    config_data["g4_port_script_version_expected"] = G4_PORT_SCRIPT_VERSION_EXPECTED
    config_data["g4_blender_addon_marker"] = G4_PORT_ADDON_MARKER
    config_data["g4_blender_addon_source"] = str(Path(__file__).resolve())
    config_data["g4_shared_face_texture_key"] = shared_face_texture_key(
        [entry.texture_name for entry in props.texture_entries]
    )
    config_data["g4_face_record_texture_keys"] = {
        record.output_name: {
            "stored": record.texture_key,
            "effective": resolved_record_texture_key(props, record),
        }
        for record in props.records
        if is_face_atlas_record(record)
    }

    # Final serialization barrier for the expression atlas.  A generated pool
    # owns a first-cell UV window; an authored existing atlas owns its original
    # UV domain.  Do this after props.to_config() so generic filtering cannot
    # erase either contract.
    face_texture_name = shared_face_texture_key([entry.texture_name for entry in props.texture_entries])
    face_entry = texture_entry(props, face_texture_name) if face_texture_name else None
    if face_texture_name and face_texture_name in config_data.get("texture_replacements", {}):
        replacement_name = config_data["texture_replacements"][face_texture_name]
        if face_entry and face_entry.expression_atlas_mode == "existing":
            port_log(None, f"Existing face atlas active for {face_texture_name}; preserving authored UVs")
        elif not (face_entry and face_entry.expression_atlas_mode == "pool"):
            # An undeclared replacement gets the conservative first-cell transform.
            face_scale = (1.0 / FACE_ATLAS_COLUMNS, 1.0 / FACE_ATLAS_ROWS)
            face_offset = (0.0, (FACE_ATLAS_ROWS - 1.0) / FACE_ATLAS_ROWS)
            for face_record in props.records:
                if not is_face_atlas_record(face_record):
                    continue
                candidate_scale, candidate_offset = face_expression_record_uv(props, face_record)
                face_scale = candidate_scale
                face_offset = candidate_offset
                break
            injected = inject_expression_atlas_uv_into_config(
                config_data,
                face_texture_name,
                face_scale,
                face_offset,
                replacement_name,
            )
            if injected:
                port_log(
                    None,
                    f"Forced expression atlas UVs into generated config for {injected} record(s): "
                    f"scale={face_scale} offset={face_offset}",
                )
        else:
            port_log(None, f"Expression atlas pool active for {face_texture_name}; preserving record-space face UV transforms")

    # Never emit a custom atlas while silently dropping the corresponding UV
    # mapping.  This exact mismatch produces a valid-looking G4TX with a
    # scrambled model, so fail before g4_port writes corrupted geometry.
    replacement_keys = set(config_data.get("texture_replacements", {}))
    for record_props, record_data in zip(props.records, config_data.get("records", [])):
        if resolved_record_texture_key(props, record_props) not in replacement_keys:
            continue
        if is_face_atlas_record(record_props) and not face_pool_atlas_active(props, record_props):
            continue
        if not objects_for_record(record_props):
            continue
        face_record_transform = (
            list(record_data.get("uv_scale", [1.0, 1.0])) != [1.0, 1.0]
            or list(record_data.get("uv_offset", [0.0, 0.0])) != [0.0, 0.0]
            or record_data.get("uv_fit_display_rect") is not None
        )
        if is_face_atlas_record(record_props) and face_pool_atlas_active(props, record_props):
            if not face_record_transform:
                raise RuntimeError(
                    f"Expression atlas UV mapping was lost for {record_props.output_name}. "
                    "Regenerate the expression atlas and export again."
                )
            continue
        if props.use_source_uv_transforms and not record_data.get("source_uv_transforms"):
            raise RuntimeError(
                f"Atlas UV mapping was lost for {record_props.output_name}. "
                "Regenerate the prepared atlas and export again."
            )

    config.write_text(json.dumps(config_data, indent=2), encoding="utf-8")
    trace_path = cache / "atlas_uv_trace.log"
    write_uv_export_trace(props, config_data, trace_path)

    needs_pillow = props.texture_mode == "custom" and any(
        Path(path).suffix.lower() not in {".dds", ".nxtch"}
        for path in props.texture_map().values()
    )
    command = [
        export_python(prefs, needs_pillow),
        str(resolve_port_script(prefs)),
        str(mesh_data_path),
        "--raw-root",
        str(raw_root),
        "--config",
        str(config),
        "--weights-json",
        str(weights_path),
        "--report-json",
        str(report_path),
    ]
    chara_model = resolve_file(getattr(prefs, "chara_model_xml", ""))
    if chara_model.is_file():
        command.extend(["--chara-model", str(chara_model)])
    if rig_skeleton is not None:
        command.extend(["--g4sk", str(rig_skeleton)])
    if props.analyze_only:
        command.append("--analyze")
    else:
        command.extend(["--texture-mode", props.texture_mode, "--out-root", str(output_root)])

    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    with trace_path.open("a", encoding="utf-8") as stream:
        stream.write("\nConverter command:\n" + shlex.join(command) + "\n")
        stream.write("\nConverter stdout:\n" + (completed.stdout or "<empty>") + "\n")
        stream.write("\nConverter stderr:\n" + (completed.stderr or "<empty>") + "\n")
    if completed.returncode != 0:
        raise RuntimeError(
            "G4 port export failed\n"
            f"Command: {shlex.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read export report: {report_path}") from exc

    armature = export_armature()
    if not props.analyze_only and armature is not None:
        armature["g4_exported_model_path"] = str(Path(report["g4md"]).resolve())
        if report.get("g4sk"):
            armature["g4_exported_skeleton_path"] = str(Path(report["g4sk"]).resolve())

    if not props.analyze_only and props.prepare_victorytool_profile:
        target_skeleton = Path(report["g4sk"]) if report.get("g4sk") else active_rig_skeleton_path(props, original_model)
        if target_skeleton is None or not target_skeleton.is_file():
            raise RuntimeError("Could not resolve the G4SK used by the exported model")
        if armature is not None:
            armature["g4_exported_skeleton_path"] = str(target_skeleton.resolve())
        profile_path = write_animation_profile(
            props, original_model, Path(report["g4md"]), target_skeleton, raw_root
        )
        report["animation_profile"] = str(profile_path)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not getattr(prefs, "keep_temporary_files", False):
        for path in (mesh_data_path, weights_path, generated_config_path(cache)):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        shutil.rmtree(mesh_data_path.parent / "customTextures", ignore_errors=True)
    report["package_root"] = str(package_root)
    report["data_root"] = str(output_root)
    return report, report_path


class LEVEL5_G4PORT_UL_records(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.label(text=item.output_name or "Record", icon="MESH_DATA")
            row.label(text=item.material_name)
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text=item.output_name[:2])


class LEVEL5_G4PORT_UL_joint_aliases(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.label(text=item.source_group or "Vertex Group", icon="GROUP_VERTEX")
            row.label(text=item.target_joint or "Unmapped")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text=item.source_group[:2])


class LEVEL5_G4PORT_OT_load_original_model(Operator, ImportHelper):
    bl_idname = "level5_g4_port.load_original_model"
    bl_label = "Choose Original G4MD/G4PKM"
    bl_description = "Read an original G4MD/G4PKM and create editable records from its native meshes/materials"

    filename_ext = ".g4md"
    filter_glob: StringProperty(default="*.g4md;*.g4pkm", options={"HIDDEN"})

    def execute(self, context):
        path = Path(self.filepath)
        if path.suffix.lower() not in MODEL_EXTENSIONS:
            self.report({"ERROR"}, "Select a G4MD or G4PKM model")
            return {"CANCELLED"}
        try:
            apply_original_model_to_settings(ensure_scene_defaults(context), path, run_model_probe(path, addon_preferences()))
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Loaded original template: {path.name}")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_add_record(Operator):
    bl_idname = "level5_g4_port.add_record"
    bl_label = "Add Record"

    def execute(self, context):
        props = settings(context)
        record = props.records.add()
        selected = mesh_objects(True)
        record.output_name = selected[0].name if selected else "new_record"
        record.material_name = selected[0].active_material.name if selected and selected[0].active_material else ""
        record.match_names = join_csv(obj.name for obj in selected) if selected else "*"
        props.active_record = len(props.records) - 1
        props.use_preset_file = False
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_assign_selected(Operator):
    bl_idname = "level5_g4_port.assign_selected"
    bl_label = "Assign Selected"
    bl_description = "Assign selected Blender meshes to the active original mesh record"

    def execute(self, context):
        props = settings(context)
        if not props.records:
            self.report({"ERROR"}, "No target record exists")
            return {"CANCELLED"}
        count = assign_selected_to_record(context, props.records[props.active_record])
        if count == 0:
            self.report({"ERROR"}, "Select at least one mesh object")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Assigned {count} mesh object(s)")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_guess_assignments(Operator):
    bl_idname = "level5_g4_port.guess_assignments"
    bl_label = "Guess Assignments"
    bl_description = "Assign scene meshes to original records by matching object and material names"

    def execute(self, context):
        count = guess_object_assignments(settings(context))
        self.report({"INFO"}, f"Assigned {count} mesh object(s) by name/material")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_use_active_texture(Operator):
    bl_idname = "level5_g4_port.use_active_texture"
    bl_label = "Use Active Texture"
    bl_description = "Use the active object's image texture as the custom replacement for the active record"

    def execute(self, context):
        props = settings(context)
        if not props.records:
            self.report({"ERROR"}, "No target record exists")
            return {"CANCELLED"}
        image_path = active_material_image_path(context)
        if not image_path:
            self.report({"ERROR"}, "The active mesh material has no image texture")
            return {"CANCELLED"}
        record = props.records[props.active_record]
        active = context.active_object
        if active is not None and active.type == "MESH":
            active.level5_g4_port.source_texture = image_path
        props.use_preset_file = False
        self.report({"INFO"}, f"Atlas source set for {active.name if active else record.output_name}: {Path(image_path).name}")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_generate_texture_pngs(Operator):
    bl_idname = "level5_g4_port.prepare_atlas"
    bl_label = "Prepare Atlas"
    bl_description = "Build only the assigned base-texture atlases and preserve every other native G4TX entry"

    def execute(self, context):
        prefs = addon_preferences()
        props = ensure_scene_defaults(context)
        sync_assignment_table(context)
        package_root = resolve_file(getattr(prefs, "output_root", ""), default_output_root())
        model_name = Path(props.model_rel).name or "model"
        output_dir = package_root / "texture_sources" / model_name
        log_path = resolve_file(getattr(prefs, "cache_dir", ""), default_cache_dir()) / "generate_png_set.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        try:
            count = generate_texture_png_set(context, output_dir, log_path)
        except Exception as exc:
            self.report({"ERROR"}, f"{exc} (log: {log_path})")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Prepared {count} atlas texture(s) in {output_dir}; log: {log_path}")
        return {"FINISHED"}





class LEVEL5_G4PORT_OT_use_existing_expression_atlas(Operator):
    bl_idname = "level5_g4_port.use_existing_expression_atlas"
    bl_label = "Use Existing 4x2 Atlas"
    bl_description = "Use the selected prepared facial atlas without rebuilding it"

    def execute(self, context):
        props = ensure_scene_defaults(context)
        texture_name = shared_face_texture_key([entry.texture_name for entry in props.texture_entries])
        entry = texture_entry(props, texture_name)
        source = Path(bpy.path.abspath(entry.replacement_path)) if entry is not None and entry.replacement_path else None
        if entry is None or source is None or load_image_pixels(str(source)) is None:
            self.report({"ERROR"}, "Choose a valid prepared 4x2 facial atlas first")
            return {"CANCELLED"}
        entry.expression_atlas = True
        entry.expression_atlas_mode = "existing"
        entry.atlas_signature = ""
        entry.atlas_summary = f"Existing 4x2 expression atlas, {source.name}"
        self.report({"INFO"}, f"Using existing 4x2 expression atlas: {source.name}")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_preview_expression(Operator):
    bl_idname = "level5_g4_port.preview_expression"
    bl_label = "Preview Expression"
    bl_description = "Temporarily move eye and mouth UVs to the selected 4x2 atlas cell"

    cell: IntProperty(default=1, min=1, max=FACE_ATLAS_SLOTS, options={"HIDDEN"})

    def execute(self, context):
        count = apply_expression_preview(self.cell)
        if not count:
            self.report({"ERROR"}, "No object named eye_10 or mouth_10 was found")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Previewing expression cell {self.cell} on {count} mesh(es)")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_restore_expression_preview(Operator):
    bl_idname = "level5_g4_port.restore_expression_preview"
    bl_label = "Restore Expression UVs"
    bl_description = "Restore the original eye and mouth UVs saved by Preview Expression"

    def execute(self, context):
        count = restore_expression_preview()
        if not count:
            self.report({"INFO"}, "No expression UV preview is active")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Restored original expression UVs on {count} mesh(es)")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_detect_vertex_groups(Operator):
    bl_idname = "level5_g4_port.detect_vertex_groups"
    bl_label = "Detect Vertex Groups"
    bl_description = "Add Blender vertex groups to the rigging alias table"

    selected_only: BoolProperty(name="Selected Only", default=False)

    def execute(self, context):
        props = settings(context)
        count = detect_joint_aliases(props, self.selected_only)
        # Auto-configure UV handling after detecting vertices
        auto_configure_uv_handling(props)
        self.report({"INFO"}, f"Detected {count} new vertex group(s)")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_auto_map_joints(Operator):
    bl_idname = "level5_g4_port.auto_map_joints"
    bl_label = "Auto-map Common Joints"
    bl_description = "Fill empty aliases using the bundled G4 joint catalog"

    def execute(self, context):
        count = auto_map_joint_aliases(settings(context))
        self.report({"INFO"}, f"Mapped {count} joint alias(es)")
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_add_joint_alias(Operator):
    bl_idname = "level5_g4_port.add_joint_alias"
    bl_label = "Add Joint Alias"

    def execute(self, context):
        props = settings(context)
        alias = props.joint_aliases.add()
        alias.source_group = "vertex_group"
        alias.target_joint = ""
        props.active_joint_alias = len(props.joint_aliases) - 1
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_remove_joint_alias(Operator):
    bl_idname = "level5_g4_port.remove_joint_alias"
    bl_label = "Remove Joint Alias"

    def execute(self, context):
        props = settings(context)
        if props.joint_aliases:
            props.joint_aliases.remove(props.active_joint_alias)
            props.active_joint_alias = min(props.active_joint_alias, max(0, len(props.joint_aliases) - 1))
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_remove_record(Operator):
    bl_idname = "level5_g4_port.remove_record"
    bl_label = "Remove Record"

    def execute(self, context):
        props = settings(context)
        if props.records:
            props.records.remove(props.active_record)
            props.active_record = min(props.active_record, max(0, len(props.records) - 1))
            props.use_preset_file = False
        return {"FINISHED"}


class LEVEL5_G4PORT_OT_analyze(Operator):
    bl_idname = "level5_g4_port.analyze"
    bl_label = "Analyze Port"
    bl_description = "Validate the scene and generated weights without writing the final G4 files"

    def execute(self, context):
        props = settings(context)
        previous = props.analyze_only
        props.analyze_only = True
        try:
            sync_assignment_table(context)
            report, report_path = run_port(context)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            props.analyze_only = previous
        unresolved = report.get("unresolved_influences", 0)
        records = len(report.get("records", [])) if isinstance(report.get("records"), list) else 0
        self.report({"INFO"}, f"Analysis OK: {records} records, unresolved influences {unresolved}. {report_path}")
        return {"FINISHED"}


class EXPORT_OT_level5_g4_port(Operator):
    bl_idname = "export_scene.level5_g4_port"
    bl_label = "Export Level-5 G4 Port"
    bl_options = {"REGISTER"}

    def execute(self, context):
        ensure_scene_defaults(context)
        props = settings(context)
        animation_armature = None
        source_bank_id = ""
        source_event_skeleton_id = ""
        if props.prepare_victorytool_profile and getattr(addon_preferences(), "legacy_animation_export", False):
            if not (props.export_chr_animations or props.export_event_animations):
                self.report({"ERROR"}, "Select character or event animations to export")
                return {"CANCELLED"}
            animation_armature = export_armature()
            try:
                from ..animation.g4_animation_addon import resolve_animation_family_ids
            except ImportError:
                from animation.g4_animation_addon import resolve_animation_family_ids
            original_model = resolve_file(props.original_model)
            raw_root = infer_data_root(original_model)
            if raw_root is None:
                self.report({"ERROR"}, "Could not resolve the original game's data folder from the model path")
                return {"CANCELLED"}
            try:
                source_bank_id, source_event_skeleton_id = resolve_animation_family_ids(
                    original_model, [raw_root]
                )
            except ValueError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            if animation_armature is None:
                self.report({"ERROR"}, "Assign a target armature to the character model before exporting")
                return {"CANCELLED"}
        try:
            sync_assignment_table(context)
            report, report_path = run_port(context)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        validation = report.get("validation") or {}
        vertices = validation.get("vertices_checked", report.get("vertices", "?"))
        indices = validation.get("indices_checked", report.get("indices", "?"))
        self.report({"INFO"}, f"G4 port exported: {vertices} vertices, {indices} indices. {report_path}")
        if animation_armature is not None:
            if report.get("g4sk"):
                animation_armature["g4_exported_skeleton_path"] = str(Path(report["g4sk"]).resolve())
            model_path = Path(str(animation_armature.get("g4_exported_model_path", ""))).resolve()
            try:
                from ..animation.g4_animation_addon import (
                    defer_blender_call,
                    model_identifiers,
                    target_animation_bank_id,
                )
            except ImportError:
                from animation.g4_animation_addon import (
                    defer_blender_call,
                    model_identifiers,
                    target_animation_bank_id,
                )
            target_bank_id = target_animation_bank_id(animation_armature)
            if not target_bank_id:
                target_bank_id = next(
                    (identifier for identifier in model_identifiers(model_path)
                     if identifier.startswith("c")),
                    source_bank_id,
                ) or source_bank_id
            original_model = resolve_file(props.original_model)
            raw_root = infer_data_root(original_model)
            export_chr_animations = props.export_chr_animations
            export_event_animations = props.export_event_animations
            def start_animation_export():
                data_overlay = next(
                    (parent for parent in model_path.parents if parent.name.casefold() == "data"),
                    None,
                )
                if data_overlay is None:
                    try:
                        from ..animation.g4_animation_addon import write_deferred_call_error
                    except ImportError:
                        from animation.g4_animation_addon import write_deferred_call_error
                    write_deferred_call_error(
                        "The model was exported, but its package has no data overlay root for animation banks."
                    )
                    return
                result = bpy.ops.export_scene.level5_g4mt_bank_family(
                    "EXEC_DEFAULT",
                    armature_name=animation_armature.name,
                    source_bank_id=source_bank_id,
                    source_event_skeleton_id=source_event_skeleton_id,
                    target_bank_id=target_bank_id,
                    output_directory=str(data_overlay),
                    source_data_root=str(raw_root) if raw_root else "",
                    export_chr_animations=export_chr_animations,
                    export_event_animations=export_event_animations,
                    chain_victorytool_candidates=export_chr_animations,
                )
                if "CANCELLED" in result:
                    try:
                        from ..animation.g4_animation_addon import write_deferred_call_error
                    except ImportError:
                        from animation.g4_animation_addon import write_deferred_call_error
                    write_deferred_call_error(
                        "The model was exported, but the animation-family export could not start. "
                        "See Blender's operator report and the G4 Deferred Call Errors text block."
                    )

            defer_blender_call(start_animation_export, label="Start animation bank export after model export")
        return {"FINISHED"}

    def invoke(self, context, event):
        ensure_scene_defaults(context)
        return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        draw_port_workflow(self.layout, context, include_actions=False)


def draw_collapsible_header(layout, props, property_name: str, label: str, icon: str) -> bool:
    expanded = bool(getattr(props, property_name))
    row = layout.row(align=True)
    row.prop(
        props,
        property_name,
        text=label,
        icon="TRIA_DOWN" if expanded else "TRIA_RIGHT",
        emboss=False,
    )
    return expanded


def draw_original_section(layout, context, props: G4PortSceneSettings) -> None:
    if not draw_collapsible_header(layout, props, "show_original", "1. Original model template", "FILE_FOLDER"):
        return

    box = layout.box()
    box.operator(LEVEL5_G4PORT_OT_load_original_model.bl_idname, icon="FILE_FOLDER")
    if props.original_model:
        box.prop(props, "original_model", text="Loaded")
        box.label(text=props.model_rel, icon="FILE")
    else:
        box.label(text="Required before Analyze or Export", icon="ERROR")
    if props.texture_names:
        box.label(text=f"{len(split_csv(props.texture_names))} G4TX texture(s)", icon="TEXTURE")


def draw_rigging_subsection(layout, props: G4PortSceneSettings) -> None:
    if not draw_collapsible_header(layout, props, "show_rigging", "2a. Rigging", "ARMATURE_DATA"):
        return

    box = layout.box()
    box.prop(props, "target_g4sk_path")
    if props.prepare_victorytool_profile:
        box.label(text="Export writes a G4SK rebuilt from this armature's bones.", icon="INFO")
    else:
        box.label(text="Auto-map common joints, then search the G4SK template to correct assignments.", icon="INFO")
    row = box.row(align=True)
    row.operator(LEVEL5_G4PORT_OT_detect_vertex_groups.bl_idname, icon="GROUP_VERTEX")
    row.operator(LEVEL5_G4PORT_OT_auto_map_joints.bl_idname, icon="BONE_DATA")
    aliases = {alias.source_group: alias for alias in props.joint_aliases}
    armature = export_armature()
    if armature is None:
        box.label(text="Detect groups after assigning a rigged mesh to show the bone hierarchy", icon="INFO")
    else:
        children = {bone.name: [] for bone in armature.data.bones}
        roots = []
        for bone in armature.data.bones:
            (children.setdefault(bone.parent.name, []) if bone.parent else roots).append(bone.name)

        def draw_bone(name: str, depth: int) -> None:
            row = box.row(align=True)
            row.separator(factor=min(depth, 8) * 0.7)
            row.label(text=name, icon="BONE_DATA")
            alias = aliases.get(name)
            if alias is None:
                row.label(text="Inherited", icon="LINKED")
            else:
                row.prop_search(alias, "target_joint", props, "joint_target_options", text="")
            for child in children.get(name, []):
                draw_bone(child, depth + 1)

        for root in roots:
            draw_bone(root, 0)
        extra = [alias for name, alias in aliases.items() if name not in children]
        if extra:
            box.label(text="Groups outside the armature", icon="GROUP_VERTEX")
            for alias in extra:
                row = box.row(align=True)
                row.label(text=alias.source_group, icon="GROUP_VERTEX")
                row.prop_search(alias, "target_joint", props, "joint_target_options", text="")
    unresolved = sum(1 for alias in props.joint_aliases if alias.source_group and not alias.target_joint)
    if unresolved:
        box.label(text=f"{unresolved} unmapped group(s)", icon="ERROR")
    box.prop(props, "strict_skinning")


def draw_record_settings_subsection(layout, props: G4PortSceneSettings) -> None:
    if not draw_collapsible_header(
        layout,
        props,
        "show_record_settings",
        "2b. Advanced mesh settings",
        "MODIFIER_DATA",
    ):
        return

    row = layout.row()
    row.template_list(
        "LEVEL5_G4PORT_UL_records",
        "",
        props,
        "records",
        props,
        "active_record",
        rows=5,
    )
    col = row.column(align=True)
    col.operator(LEVEL5_G4PORT_OT_add_record.bl_idname, text="", icon="ADD")
    col.operator(LEVEL5_G4PORT_OT_remove_record.bl_idname, text="", icon="REMOVE")
    if not props.records or not 0 <= props.active_record < len(props.records):
        return

    record = props.records[props.active_record]
    box = layout.box()
    if record.original_index >= 0:
        box.label(text=f"Original mesh #{record.original_index}", icon="MESH_DATA")
    box.prop(record, "output_name")
    box.prop(record, "material_name")
    box.prop(record, "match_names")
    row = box.row(align=True)
    row.prop(record, "uv_flip_x")
    row.prop(record, "uv_flip_y")
    row = box.row(align=True)
    row.prop(record, "uv_scale_u")
    row.prop(record, "uv_scale_v")
    row = box.row(align=True)
    row.prop(record, "uv_offset_u")
    row.prop(record, "uv_offset_v")
    box.prop(record, "rigid_joint")
    box.prop(record, "auto_palette")
    box.prop(record, "secondary_weight_scale")
    box.prop(record, "weight_anchor_joint")
    box.prop(record, "outline_enabled")
    box.prop(record, "texture_key")
    box.operator(
        LEVEL5_G4PORT_OT_use_active_texture.bl_idname,
        text="Use Active Image as Atlas Source",
        icon="TEXTURE",
    )
    box.prop(record, "fallback_degenerate")
    box.prop(record, "force_layout_material")
    if record.force_layout_material:
        row = box.row(align=True)
        row.prop(record, "layout_index")
        row.prop(record, "material_index")


def draw_mapping_section(layout, context, props: G4PortSceneSettings) -> None:
    if not draw_collapsible_header(layout, props, "show_mapping", "2. Mesh correspondence", "OUTLINER_OB_MESH"):
        return

    box = layout.box()
    if not props.records:
        box.label(text="Load the original model first to create native target records", icon="INFO")
    else:
        box.label(text="Assign each Blender mesh to its native G4MD record", icon="OUTLINER_OB_MESH")
        for obj in mesh_objects(False):
            row = box.row(align=True)
            row.label(text=obj.name, icon="MESH_DATA")
            row.prop(obj.level5_g4_port, "target_record", text="")
            row.prop(obj.level5_g4_port, "source_texture", text="")
        row = box.row(align=True)
        row.operator(LEVEL5_G4PORT_OT_assign_selected.bl_idname, icon="RESTRICT_SELECT_OFF")
        row.operator(LEVEL5_G4PORT_OT_guess_assignments.bl_idname, icon="VIEWZOOM")

    # Rigging and record editing are children of mesh correspondence.  Keeping
    # them inside this section gives the sidebar and export dialog the same
    # dependency hierarchy instead of presenting five unrelated top-level panels.
    nested = box.column(align=False)
    nested.separator()
    draw_rigging_subsection(nested, props)
    draw_record_settings_subsection(nested, props)


def draw_texture_replacements(layout, props: G4PortSceneSettings) -> None:
    box = layout.box()
    box.label(text="G4TX Texture Replacements", icon="TEXTURE")
    if props.texture_entries:
        for entry in props.texture_entries:
            row = box.row(align=True)
            row.label(text=entry.texture_name)
            row.prop(entry, "replacement_path", text="")
    else:
        box.label(text="Load an original model to list its G4TX textures", icon="INFO")
    box.label(text="Empty paths preserve the original texture", icon="CHECKMARK")


def draw_textures_section(layout, context, props: G4PortSceneSettings) -> None:
    if not draw_collapsible_header(layout, props, "show_textures", "3. Textures", "TEXTURE"):
        return

    box = layout.box()
    box.prop(props, "texture_mode")
    box.prop(props, "texture_platform")

    if props.texture_mode != "custom":
        if props.texture_mode == "native":
            box.label(text="The original G4TX archive will be copied unchanged", icon="CHECKMARK")
        else:
            box.label(text="The G4TX already present in the output package will be preserved", icon="CHECKMARK")
        return

    box.label(text="Prepare and review atlas", icon="TEXTURE")
    for status in atlas_status_rows(props):
        row = box.row(align=True)
        icon = {
            "ready": "CHECKMARK",
            "manual": "CHECKMARK",
            "stale": "FILE_REFRESH",
            "warning": "ERROR",
        }.get(status["state"], "INFO")
        row.label(text=status["name"], icon=icon)
        row.label(text=status["message"])
        if status["repeated"]:
            box.label(text=f"UVs adjusted for: {', '.join(status['repeated'])}", icon="UV")

    box.prop(props, "texture_source_dir")
    draw_texture_replacements(box, props)

    box.prop(props, "generate_png_set_on_export")
    box.prop(props, "replace_special_textures")
    row = box.row(align=True)
    row.operator(LEVEL5_G4PORT_OT_generate_texture_pngs.bl_idname, icon="TEXTURE")


def draw_expression_pool(layout, props: G4PortSceneSettings) -> None:
    box = layout
    face_key = shared_face_texture_key([entry.texture_name for entry in props.texture_entries])
    if face_key:
        expression_box = box.box()
        expression_box.label(text=f"Expression pool for {face_key} (4x2)", icon="SEQ_CHROMA_SCOPE")
        expression_box.label(
            text="eye_10 and mouth_10 keep the native atlas unless a 4x2 source is explicitly accepted",
            icon="INFO",
        )
        face_entry = texture_entry(props, face_key)
        if face_entry is not None:
            expression_box.prop(face_entry, "replacement_path", text="Existing 4x2 Atlas")
        row = expression_box.row(align=True)
        row.operator(LEVEL5_G4PORT_OT_use_existing_expression_atlas.bl_idname, icon="CHECKMARK")
        expression_box.label(text="Set 8 cell images below (4x2):", icon="INFO")
        if len(props.expression_pool) < FACE_ATLAS_SLOTS:
            expression_box.label(text="Expression pool migration is pending; reopen the exporter once", icon="INFO")
        for index, item in enumerate(props.expression_pool):
            if index >= FACE_ATLAS_SLOTS:
                break
            expression_box.prop(
                item,
                "image_path",
                text=f"Cell {index % FACE_ATLAS_COLUMNS + 1}, row {index // FACE_ATLAS_COLUMNS + 1} (top first)",
            )



def draw_export_settings(layout, props: G4PortSceneSettings) -> None:
    if not draw_collapsible_header(layout, props, "show_export", "4. Export", "EXPORT"):
        return

    prefs = addon_preferences()
    box = layout.box()
    box.prop(prefs, "output_root", text="Package Folder")
    box.prop(props, "selected_only")
    box.prop(props, "disable_edge2")
    if props.disable_edge2:
        box.label(text="edge2 outline displacement will be zeroed on export", icon="INFO")
    box.prop(props, "preserve_native_roundtrip")
    legacy = getattr(prefs, "legacy_animation_export", False)
    box.prop(props, "prepare_victorytool_profile", text="Export Animation Family for VictoryTool" if legacy else "Prepare animations for VictoryTool")
    if props.prepare_victorytool_profile and legacy:
        column = box.column(align=True)
        column.prop(props, "export_chr_animations")
        column.prop(props, "export_event_animations")
        box.label(text="The active imported animation determines the family to export", icon="ACTION")

    if not props.original_model:
        box.label(text="Original model is still required", icon="ERROR")
    elif not props.records:
        box.label(text="No native mesh records are loaded", icon="ERROR")
    else:
        assigned = sum(
            1
            for obj in mesh_objects(False)
            if getattr(obj.level5_g4_port, "target_record", "__none__") not in {"", "__none__"}
        )
        box.label(text=f"{assigned} mesh object(s) currently assigned", icon="CHECKMARK" if assigned else "INFO")


def draw_expression_preview_section(layout, props: G4PortSceneSettings) -> None:
    if not draw_collapsible_header(layout, props, "show_expression_preview", "5. Expression Preview", "HIDE_OFF"):
        return
    box = layout.box()
    box.label(text="Moves UVs on objects named eye_10 or mouth_10 only", icon="UV")
    for row_index in range(FACE_ATLAS_ROWS):
        row = box.row(align=True)
        for column in range(FACE_ATLAS_COLUMNS):
            cell = row_index * FACE_ATLAS_COLUMNS + column + 1
            operator = row.operator(LEVEL5_G4PORT_OT_preview_expression.bl_idname, text=str(cell))
            operator.cell = cell
    box.operator(LEVEL5_G4PORT_OT_restore_expression_preview.bl_idname, text="Restore Original UVs", icon="LOOP_BACK")
    box.label(text="Preview is independent from texture settings and export", icon="INFO")


def draw_port_workflow(layout, context, include_actions: bool) -> None:
    """Render the one canonical G4 Port workflow in both entry points."""
    props = settings(context)
    draw_original_section(layout, context, props)
    draw_mapping_section(layout, context, props)
    draw_textures_section(layout, context, props)
    draw_export_settings(layout, props)
    if not include_actions:
        draw_outline_workflow(layout, context)
        draw_expression_pool(layout, props)
        draw_expression_preview_section(layout, props)

    if include_actions:
        row = layout.row(align=True)
        row.operator(LEVEL5_G4PORT_OT_analyze.bl_idname, icon="VIEWZOOM")
        row.operator(EXPORT_OT_level5_g4_port.bl_idname, icon="EXPORT")


def draw_outline_workflow(layout, context) -> None:
    props = settings(context)
    box = layout.box()
    box.label(text="Outline Edges")
    row = box.row(align=True)
    row.operator(LEVEL5_G4PORT_OT_mark_outline.bl_idname, text="Mark Outline")
    row.operator(LEVEL5_G4PORT_OT_mark_outline.bl_idname, text="Clear Outline").clear = True
    box.operator(LEVEL5_G4PORT_OT_select_outline.bl_idname)
    box.prop(props, "outline_width")
    box.prop(props, "preview_authored_outlines")
    box.prop(props, "use_authored_outlines")
    if context.mode == "EDIT_MESH":
        box.label(text="Red lines show marked outline edges")
    else:
        box.label(text="Select edges in Edit Mode to mark outlines")


class LEVEL5_G4PORT_PT_panel(Panel):
    bl_label = "PORT"
    bl_idname = "LEVEL5_G4PORT_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Level-5"

    def draw(self, context):
        draw_port_workflow(self.layout, context, include_actions=True)


class LEVEL5_G4PORT_PT_outline(Panel):
    bl_label = "OUTLINE"
    bl_idname = "LEVEL5_G4PORT_PT_outline"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Level-5"
    bl_order = 1

    def draw(self, context):
        draw_outline_workflow(self.layout, context)


class LEVEL5_G4PORT_PT_expression(Panel):
    bl_label = "EXPRESSION"
    bl_idname = "LEVEL5_G4PORT_PT_expression"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Level-5"
    bl_order = 2

    def draw(self, context):
        props = settings(context)
        draw_expression_pool(self.layout, props)
        draw_expression_preview_section(self.layout, props)


def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_level5_g4_port.bl_idname, text="Level-5 G4 Port")


classes = []

if IS_STANDALONE_ADDON:
    classes.append(G4PortPreferences)

classes.extend([
    G4PortObjectSettings,
    G4PortJointAlias,
    G4PortJointTarget,
    G4PortTextureReplacement,
    G4PortExpressionImage,
    G4PortRecord,
    G4PortSceneSettings,
    LEVEL5_G4PORT_OT_mark_outline,
    LEVEL5_G4PORT_OT_select_outline,
    LEVEL5_G4PORT_OT_apply_outline_regions,
    LEVEL5_G4PORT_UL_records,
    LEVEL5_G4PORT_UL_joint_aliases,
    LEVEL5_G4PORT_OT_load_original_model,
    LEVEL5_G4PORT_OT_add_record,
    LEVEL5_G4PORT_OT_remove_record,
    LEVEL5_G4PORT_OT_assign_selected,
    LEVEL5_G4PORT_OT_guess_assignments,
    LEVEL5_G4PORT_OT_use_active_texture,
    LEVEL5_G4PORT_OT_generate_texture_pngs,
    LEVEL5_G4PORT_OT_use_existing_expression_atlas,
    LEVEL5_G4PORT_OT_preview_expression,
    LEVEL5_G4PORT_OT_restore_expression_preview,
    LEVEL5_G4PORT_OT_detect_vertex_groups,
    LEVEL5_G4PORT_OT_auto_map_joints,
    LEVEL5_G4PORT_OT_add_joint_alias,
    LEVEL5_G4PORT_OT_remove_joint_alias,
    LEVEL5_G4PORT_OT_analyze,
    EXPORT_OT_level5_g4_port,
    LEVEL5_G4PORT_PT_panel,
    LEVEL5_G4PORT_PT_outline,
    LEVEL5_G4PORT_PT_expression,
])


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


def register():
    for cls in classes:
        register_class_replacing_stale(cls)
    bpy.types.Object.level5_g4_port = bpy.props.PointerProperty(type=G4PortObjectSettings)
    bpy.types.Scene.level5_g4_port = bpy.props.PointerProperty(type=G4PortSceneSettings)
    initialize_g4_port_scene_defaults()
    if initialize_g4_port_scene_defaults not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(initialize_g4_port_scene_defaults)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    for scene in bpy.data.scenes:
        viewport_outline.configure_authored(scene, False)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    if initialize_g4_port_scene_defaults in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(initialize_g4_port_scene_defaults)
    del bpy.types.Scene.level5_g4_port
    del bpy.types.Object.level5_g4_port
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
