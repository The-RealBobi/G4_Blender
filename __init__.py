bl_info = {
    "name": "Level-5 G4 Blender Tools",
    "author": "Bobi",
    "version": (0, 14, 15),
    "blender": (4, 0, 0),
    "location": "File > Import/Export > G4MD / G4PKM",
    "description": "",
    "category": "Import-Export",
}

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, StringProperty
from bpy.types import AddonPreferences, Operator, OperatorFileListElement
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Quaternion, Vector

ADDON_ID = __name__
MODEL_EXTENSIONS = {".g4md", ".g4pkm"}

try:
    from . import g4_port_addon
except ImportError:
    import g4_port_addon

try:
    from . import g4_animation_addon
except ImportError:
    import g4_animation_addon

try:
    from .g4_model_probe import map_scene_placements
except ImportError:
    from g4_model_probe import map_scene_placements

g4_port_addon.ADDON_ID = ADDON_ID
g4_animation_addon.ADDON_ID = ADDON_ID


def outline_mode_changed(preferences, _context) -> None:
    refresh = globals().get("refresh_existing_level5_outlines")
    if refresh is not None:
        refresh(preferences.outline_mode)


def default_probe_script() -> str:
    env_path = os.environ.get("LEVEL5_G4_PROBE")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    addon_path = Path(__file__).resolve()
    candidates.extend(
        [
            addon_path.parent / "g4_model_probe.py",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def default_export_dir() -> str:
    env_path = os.environ.get("LEVEL5_G4_EXPORT_DIR")
    if env_path:
        return env_path
    return str(Path.home() / "level5_g4_exports")


def default_chara_model_xml() -> str:
    env_path = os.environ.get("LEVEL5_G4_CHARA_MODEL")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))

    addon_path = Path(__file__).resolve()
    search_dirs = [addon_path.parent, addon_path.parent / "TOOLS", addon_path.parents[1] / "TOOLS"]
    for directory in search_dirs:
        candidates.append(directory / "chara_model_1.03.49.00.cfg.bin.xml")
        candidates.extend(sorted(directory.glob("chara_model*.xml")))

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return str(candidate)
    return ""


def default_chara_model_lookup() -> str:
    env_path = os.environ.get("LEVEL5_G4_CHARA_LOOKUP")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))

    addon_path = Path(__file__).resolve()
    candidates.extend(
        [
            addon_path.parent / "chara_model_lookup.json",
            addon_path.parent / "data" / "chara_model_lookup.json",
            addon_path.parents[1] / "data" / "chara_model_lookup.json",
        ]
    )

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return str(candidate)
    return ""


def default_python() -> str:
    for candidate in ("/usr/bin/python3", "/opt/homebrew/bin/python3", sys.executable, "python3"):
        if candidate and Path(candidate).exists():
            return candidate
    return "python3"


def addon_preferences() -> "G4ImporterPreferences":
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    if addon is not None:
        return addon.preferences

    class Defaults:
        python_path = default_python()
        probe_script = default_probe_script()
        export_dir = default_export_dir()
        raw_data_root = os.environ.get("LEVEL5_G4_RAW_ROOT", "")
        chara_model_lookup = default_chara_model_lookup()
        chara_model_xml = default_chara_model_xml()
        pack_imported_textures = True
        cleanup_import_cache = True
        apply_bone_orientation = True
        outline_mode = "OFF"

    return Defaults()


def resolve_probe_script(prefs: "G4ImporterPreferences") -> Path:
    configured_probe = bpy.path.abspath(getattr(prefs, "probe_script", "") or "")
    probe_script = Path(configured_probe) if configured_probe else Path()
    if probe_script.exists():
        return probe_script

    fallback_path = default_probe_script()
    if fallback_path:
        fallback = Path(fallback_path)
        if fallback.exists():
            return fallback

    raise RuntimeError(
        "Exporter script not found. Set the addon preference 'Exporter Script' "
        "to g4_model_probe.py, place g4_model_probe.py next to the addon, or define LEVEL5_G4_PROBE."
    )


def exporter_environment(prefs: "G4ImporterPreferences", export_dir: Path) -> dict:
    env = os.environ.copy()
    raw_data_root = bpy.path.abspath(getattr(prefs, "raw_data_root", "") or "")
    if raw_data_root:
        env["LEVEL5_G4_RAW_ROOT"] = raw_data_root
    chara_model_lookup = bpy.path.abspath(getattr(prefs, "chara_model_lookup", "") or default_chara_model_lookup())
    if chara_model_lookup:
        env["LEVEL5_G4_CHARA_LOOKUP"] = chara_model_lookup
    chara_model_xml = bpy.path.abspath(getattr(prefs, "chara_model_xml", "") or default_chara_model_xml())
    if chara_model_xml:
        env["LEVEL5_G4_CHARA_MODEL"] = chara_model_xml
    env["LEVEL5_G4_SKELETON_CACHE"] = str(export_dir / "g4_skeleton_cache.json")
    return env


class G4ImporterPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    python_path: StringProperty(
        name="Python",
        subtype="FILE_PATH",
        default=default_python(),
        description="Python executable used to run g4_model_probe.py",
    )
    probe_script: StringProperty(
        name="Exporter Script",
        subtype="FILE_PATH",
        default=default_probe_script(),
        description="Path to the bundled or external g4_model_probe.py",
    )
    export_dir: StringProperty(
        name="Export Cache",
        subtype="DIR_PATH",
        default=default_export_dir(),
        description="Directory where native mesh caches, textures and reports are generated",
    )
    raw_data_root: StringProperty(
        name="Raw Data Root",
        subtype="DIR_PATH",
        default="",
        description="Optional raw/data root. Required when importing by model name instead of selecting a file",
    )
    chara_model_xml: StringProperty(
        name="Chara Model XML",
        subtype="FILE_PATH",
        default=default_chara_model_xml(),
        description="Optional chara_model XML fallback used to resolve shared character skeletons",
    )
    chara_model_lookup: StringProperty(
        name="Chara Model Lookup",
        subtype="FILE_PATH",
        default=default_chara_model_lookup(),
        description="Optional preprocessed chara_model lookup JSON. It can be placed next to the addon",
    )
    pack_imported_textures: BoolProperty(
        name="Pack Imported Textures",
        default=True,
        description="Pack imported DDS images into the Blender file after loading the model",
    )
    cleanup_import_cache: BoolProperty(
        name="Clean Temporary Imports",
        default=True,
        description="Delete generated native mesh cache, report and textures after Blender has loaded and packed them",
    )
    apply_bone_orientation: BoolProperty(
        name="Apply Bone Orientation",
        default=False,
        description="Orient bones for display; leave disabled when the rig will receive G4MT animation",
    )
    outline_mode: EnumProperty(
        name="Character Outline",
        items=(
            (
                "SIMPLE",
                "Simple",
                "Non-destructive object silhouette in the viewport and render",
            ),
            (
                "HULL",
                "Detailed",
                "Use source-weighted silhouettes and add depth/normal cavity detail in the viewport",
            ),
            ("OFF", "Off", "Do not add render or viewport outlines"),
        ),
        default="SIMPLE",
        update=outline_mode_changed,
    )
    outline_thickness: FloatProperty(
        name="Outline Thickness",
        description="Main character silhouette thickness in render pixels; internal lines scale proportionally",
        default=1.65,
        min=0.25,
        max=6.0,
        step=5,
        precision=2,
        update=outline_mode_changed,
    )
    decoder_script: StringProperty(
        name="G4MT Decoder",
        subtype="FILE_PATH",
        default=g4_animation_addon.default_decoder_script(),
        description="Path to bundled or external g4mt_motion.py",
    )
    camera_decoder_script: StringProperty(
        name="G4CM Decoder",
        subtype="FILE_PATH",
        default=g4_animation_addon.default_camera_decoder_script(),
        description="Path to bundled or external g4cm_camera.py",
    )
    keep_decode_json: BoolProperty(
        name="Keep Animation Decode JSON",
        default=False,
        description="Keep intermediate G4MT/G4CM JSON files for investigation",
    )
    event_character_parts: StringProperty(
        default="{}",
        options={"HIDDEN"},
        description="Persistent body and shoes selections keyed by event actor ID",
    )
    character_import_parts: StringProperty(
        default="{}",
        options={"HIDDEN"},
        description="Persistent character-part selections used by model and animation imports",
    )
    port_script: StringProperty(
        name="G4 Port Script",
        subtype="FILE_PATH",
        default=g4_port_addon.default_port_script(),
        description="Path to bundled or external g4_port.py; bundled installations detect it automatically",
    )
    config_dir: StringProperty(
        name="Port Preset Folder",
        subtype="DIR_PATH",
        default=g4_port_addon.default_config_dir(),
        description="Folder containing G4 port presets",
    )
    output_root: StringProperty(
        name="Port Package Folder",
        subtype="DIR_PATH",
        default=os.environ.get("LEVEL5_G4_OUT_ROOT", g4_port_addon.default_output_root()),
        description="Destination folder. The exporter writes data/common and data/dx11 inside it",
    )
    cache_dir: StringProperty(
        name="Port Export Cache",
        subtype="DIR_PATH",
        default=g4_port_addon.default_cache_dir(),
        description="Temporary folder for DAE, weights, generated presets and reports",
    )
    keep_temporary_files: BoolProperty(
        name="Keep Port Temporary Files",
        default=False,
        description="Keep generated DAE/config/weights files after export",
    )

    def draw(self, context):
        layout = self.layout
        shared_box = layout.box()
        shared_box.label(text="Shared Runtime")
        shared_box.prop(self, "python_path")
        shared_box.prop(self, "probe_script")
        shared_box.prop(self, "raw_data_root")
        shared_box.prop(self, "chara_model_xml")

        import_box = layout.box()
        import_box.label(text="Import")
        import_box.prop(self, "export_dir")
        import_box.prop(self, "chara_model_lookup")
        import_box.prop(self, "pack_imported_textures")
        import_box.prop(self, "cleanup_import_cache")
        import_box.prop(self, "apply_bone_orientation")
        import_box.prop(self, "outline_mode")
        thickness_row = import_box.row()
        thickness_row.enabled = self.outline_mode != "OFF"
        thickness_row.prop(self, "outline_thickness")

        animation_box = layout.box()
        animation_box.label(text="Animation Import")
        animation_box.prop(self, "decoder_script")
        animation_box.prop(self, "camera_decoder_script")
        animation_box.prop(self, "keep_decode_json")

        port_box = layout.box()
        port_box.label(text="Port Export")
        port_box.prop(self, "port_script")
        port_box.prop(self, "config_dir")
        port_box.prop(self, "output_root")
        port_box.prop(self, "cache_dir")
        port_box.prop(self, "keep_temporary_files")


def matrix_to_flat_list(matrix) -> list[float]:
    return [float(matrix[row][column]) for row in range(4) for column in range(4)]


def armature_skeleton_info(armature) -> dict | None:
    if armature is None or armature.type != "ARMATURE":
        return None
    bones = list(armature.data.bones)
    if not bones:
        return None
    index_by_name = {bone.name: index for index, bone in enumerate(bones)}
    names = [bone.name for bone in bones]
    joint_count = len(bones)
    parent_indices = [
        index_by_name.get(bone.parent.name, joint_count) if bone.parent is not None else joint_count
        for bone in bones
    ]
    bind_matrices = [matrix_to_flat_list(bone.matrix_local) for bone in bones]
    inverse_bind_matrices = [matrix_to_flat_list(bone.matrix_local.inverted_safe()) for bone in bones]
    local_matrices = []
    local_scales = []
    local_rotations_xyzw = []
    local_translations = []
    for bone in bones:
        local_matrix = (
            bone.parent.matrix_local.inverted_safe() @ bone.matrix_local
            if bone.parent is not None
            else bone.matrix_local.copy()
        )
        local_matrices.append(matrix_to_flat_list(local_matrix))
        local_scale = local_matrix.to_scale()
        local_translation = local_matrix.to_translation()
        local_rotation = local_matrix.to_quaternion()
        local_scales.append([float(local_scale.x), float(local_scale.y), float(local_scale.z)])
        local_rotations_xyzw.append(
            [
                float(local_rotation.x),
                float(local_rotation.y),
                float(local_rotation.z),
                float(local_rotation.w),
            ]
        )
        local_translations.append(
            [
                float(local_translation.x),
                float(local_translation.y),
                float(local_translation.z),
            ]
        )
    return {
        "magic": "BLENDER_ARMATURE",
        "joint_count": joint_count,
        "table_count": 0,
        "section_offsets": [],
        "parent_indices": parent_indices,
        "names": names,
        "bind_matrices": bind_matrices,
        "inverse_bind_matrices": inverse_bind_matrices,
        "local_matrices": local_matrices,
        "local_scales": local_scales,
        "local_rotations_xyzw": local_rotations_xyzw,
        "local_translations": local_translations,
    }


def run_exporter(model_path: str, prefs: G4ImporterPreferences, target_armature=None) -> dict:
    python_path = Path(bpy.path.abspath(prefs.python_path or default_python()))
    probe_script = resolve_probe_script(prefs)
    export_dir = Path(bpy.path.abspath(prefs.export_dir))
    export_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(python_path),
        str(probe_script),
        "--export-model",
        "--json",
        "--dae-dir",
        str(export_dir),
        model_path,
    ]
    env = exporter_environment(prefs, export_dir)
    skeleton_override_path: Path | None = None
    skeleton_info = armature_skeleton_info(target_armature)
    if skeleton_info is not None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="level5_g4_target_skeleton_",
            delete=False,
        ) as handle:
            json.dump(skeleton_info, handle, separators=(",", ":"))
            skeleton_override_path = Path(handle.name)
        env["LEVEL5_G4_TARGET_SKELETON"] = str(skeleton_override_path)
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    finally:
        if skeleton_override_path is not None:
            skeleton_override_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "G4 exporter failed\n"
            f"Command: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    try:
        summaries = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Exporter did not return JSON:\n{completed.stdout}") from exc
    if not summaries:
        raise RuntimeError("Exporter returned no model summary")
    summary = summaries[0]
    report_path = Path(summary.get("report", ""))
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}
        for key in (
            "textures",
            "materials",
            "texture_variants",
            "material_records",
            "g4md_texture_hashes",
            "meshes",
            "native_mesh",
            "skeleton_source",
            "skeleton",
            "bone_orientation",
        ):
            if key in report:
                summary[key] = report[key]
    return summary


def import_collada(dae_path: Path) -> set[str]:
    before = set(bpy.data.objects)
    bpy.ops.wm.collada_import(filepath=str(dae_path))
    after = set(bpy.data.objects)
    return {obj.name for obj in after - before}


def matrix_from_flat(values: list[float]) -> Matrix:
    if len(values) != 16:
        return Matrix.Identity(4)
    return Matrix((values[0:4], values[4:8], values[8:12], values[12:16]))


def import_native_g4_mesh(native_path: Path) -> set[str]:
    payload = json.loads(native_path.read_text(encoding="utf-8"))
    if payload.get("format") != "level5-g4-native-mesh" or payload.get("version") != 1:
        raise RuntimeError(f"Unsupported native G4 mesh payload: {native_path}")

    imported: set[str] = set()
    conversion = Matrix.Rotation(math.radians(90.0), 4, "X")
    skeleton = payload.get("skeleton") or {}
    names = skeleton.get("names") or []
    parents = skeleton.get("parent_indices") or []
    bind_matrices = skeleton.get("bind_matrices") or []
    armature = None
    if names and bind_matrices:
        armature_data = bpy.data.armatures.new("skeleton_root")
        armature = bpy.data.objects.new("skeleton_root", armature_data)
        bpy.context.collection.objects.link(armature)
        armature.matrix_world = conversion
        imported.add(armature.name)
        bpy.context.view_layer.objects.active = armature
        armature.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        global_matrices = [matrix_from_flat(values) for values in bind_matrices[: len(names)]]
        edit_bones = []
        for index, name in enumerate(names):
            bone = armature_data.edit_bones.new(name or f"joint_{index:03d}")
            matrix = global_matrices[index] if index < len(global_matrices) else Matrix.Identity(4)
            bone.matrix = matrix
            child_index = next(
                (child for child, parent in enumerate(parents) if parent == index and child < len(global_matrices)),
                None,
            )
            if child_index is not None:
                length = (global_matrices[child_index].translation - matrix.translation).length
            else:
                length = 0.02
            bone.length = max(length, 0.005)
            edit_bones.append(bone)
        for index, bone in enumerate(edit_bones):
            parent = parents[index] if index < len(parents) else len(edit_bones)
            if 0 <= parent < len(edit_bones) and parent != index:
                bone.parent = edit_bones[parent]
        bpy.ops.object.mode_set(mode="OBJECT")

    source_model = str(payload.get("source") or native_path)
    materials: dict[str, bpy.types.Material] = {}
    for material_name in payload.get("materials", {}):
        existing = bpy.data.materials.get(material_name)
        if existing is not None and existing.get("g4_source_model") != source_model:
            existing = None
        elif existing is not None and existing.users == 0:
            bpy.data.materials.remove(existing)
            existing = None
        material = existing or bpy.data.materials.new(material_name)
        material["g4_source_model"] = source_model
        materials[material_name] = material

    for mesh_payload in payload.get("meshes", []):
        name = mesh_payload.get("name") or "G4 Mesh"
        flat_positions = mesh_payload.get("positions") or []
        positions = [
            Vector(flat_positions[index : index + 3])
            for index in range(0, len(flat_positions) - 2, 3)
        ]
        indices = mesh_payload.get("indices") or []
        faces = [tuple(indices[index : index + 3]) for index in range(0, len(indices) - 2, 3)]
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(positions, [], faces)
        mesh.update()

        flat_uvs = mesh_payload.get("texcoords") or []
        if len(flat_uvs) >= len(positions) * 2:
            uv_layer = mesh.uv_layers.new(name="UVMap")
            for loop in mesh.loops:
                offset = loop.vertex_index * 2
                uv_layer.data[loop.index].uv = flat_uvs[offset : offset + 2]

        flat_colors = mesh_payload.get("vertex_colors") or []
        if len(flat_colors) >= len(positions) * 4:
            colors = mesh.color_attributes.new(
                name="G4 Outline Parameters", type="BYTE_COLOR", domain="CORNER"
            )
            for loop in mesh.loops:
                offset = loop.vertex_index * 4
                colors.data[loop.index].color = flat_colors[offset : offset + 4]

        flat_normals = mesh_payload.get("normals") or []
        if len(flat_normals) >= len(positions) * 3 and hasattr(mesh, "normals_split_custom_set_from_vertices"):
            normals = [
                Vector(flat_normals[index : index + 3]).normalized()
                for index in range(0, len(flat_normals) - 2, 3)
            ]
            for polygon in mesh.polygons:
                polygon.use_smooth = True
            mesh.normals_split_custom_set_from_vertices(normals)

        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        imported.add(obj.name)
        material_name = mesh_payload.get("material")
        if material_name in materials:
            mesh.materials.append(materials[material_name])

        if armature is not None:
            palette = mesh_payload.get("joint_palette") or []
            palette_base = int(mesh_payload.get("palette_base") or 0)
            influences = mesh_payload.get("skin_influences") or []
            groups: dict[int, bpy.types.VertexGroup] = {}
            for vertex_index, vertex_influences in enumerate(influences[: len(positions)]):
                for local_index, weight in vertex_influences:
                    if weight <= 0.0:
                        continue
                    global_index = palette[local_index] if local_index < len(palette) else palette_base + local_index
                    if not 0 <= global_index < len(names):
                        continue
                    group = groups.get(global_index)
                    if group is None:
                        group = obj.vertex_groups.new(name=names[global_index] or f"joint_{global_index:03d}")
                        groups[global_index] = group
                    group.add([vertex_index], float(weight), "REPLACE")
            modifier = obj.modifiers.new(name="Armature", type="ARMATURE")
            modifier.object = armature
            obj.parent = armature

    return imported


def blender_base_name(name: str) -> str:
    return re.sub(r"\.\d{3}$", "", name)


def material_image_paths(material) -> set[str]:
    tree = material.node_tree
    if tree is None:
        return set()
    paths = set()
    for node in tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None and node.image.filepath:
            paths.add(str(Path(bpy.path.abspath(node.image.filepath)).resolve()))
    return paths


def materials_are_compatible(left, right) -> bool:
    left_source = left.get("g4_source_model")
    right_source = right.get("g4_source_model")
    if left_source and right_source and left_source != right_source:
        return False
    left_paths = material_image_paths(left)
    right_paths = material_image_paths(right)
    return not left_paths or not right_paths or bool(left_paths & right_paths)


def collapse_duplicate_materials(imported_names: set[str]) -> None:
    for object_name in imported_names:
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            base_name = blender_base_name(material.name)
            if base_name == material.name:
                continue
            target = bpy.data.materials.get(base_name)
            if target is None or target == material or not materials_are_compatible(material, target):
                continue
            slot.material = target
            if material.users == 0:
                bpy.data.materials.remove(material)


def global_orientation_matrices(names: list[str], parents: list[int], rotations: list[list[float]]) -> list[Matrix]:
    local_matrices = []
    for rotation in rotations:
        if len(rotation) != 4:
            local_matrices.append(Matrix.Identity(4))
            continue
        x, y, z, w = rotation
        local_matrices.append(Quaternion((w, x, y, z)).to_matrix().to_4x4())

    global_matrices: list[Matrix] = [Matrix.Identity(4) for _ in names]
    for index, local_matrix in enumerate(local_matrices[: len(names)]):
        parent = parents[index] if index < len(parents) else len(names)
        if 0 <= parent < index:
            global_matrices[index] = global_matrices[parent] @ local_matrix
        else:
            global_matrices[index] = local_matrix
    return global_matrices


def imported_armatures(imported_names: set[str]) -> list[bpy.types.Object]:
    armatures = []
    for item in imported_names:
        obj = item if hasattr(item, "type") else bpy.data.objects.get(item)
        if obj is not None and obj.type == "ARMATURE":
            armatures.append(obj)
    return armatures


def apply_g4_bone_orientation(imported_names: set[str], summary: dict, enabled: bool) -> dict:
    orientation = summary.get("bone_orientation")
    if not enabled or not isinstance(orientation, dict):
        return {"enabled": bool(enabled), "applied": 0, "missing": 0}

    names = orientation.get("names") or []
    parents = orientation.get("parent_indices") or []
    rotations = orientation.get("local_rotations_xyzw") or []
    if not names or not rotations:
        return {"enabled": bool(enabled), "applied": 0, "missing": len(names)}

    matrices = global_orientation_matrices(names, parents, rotations)
    result = {"enabled": True, "applied": 0, "missing": 0, "armatures": []}
    previous_active = bpy.context.view_layer.objects.active
    previous_mode = bpy.context.object.mode if bpy.context.object is not None else "OBJECT"
    if previous_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    try:
        for armature in imported_armatures(imported_names):
            result["armatures"].append(armature.name)
            bpy.context.view_layer.objects.active = armature
            armature.select_set(True)
            bpy.ops.object.mode_set(mode="EDIT")
            name_to_index = {name: index for index, name in enumerate(names)}
            child_indices: dict[int, list[int]] = {index: [] for index in range(len(names))}
            for index, parent in enumerate(parents):
                if 0 <= parent < len(names):
                    child_indices.setdefault(parent, []).append(index)

            original_heads = {
                bone_name: edit_bone.head.copy()
                for bone_name, edit_bone in armature.data.edit_bones.items()
            }
            original_tails = {
                bone_name: edit_bone.tail.copy()
                for bone_name, edit_bone in armature.data.edit_bones.items()
            }
            directions: dict[int, Vector] = {}

            for index, bone_name in enumerate(names):
                head = original_heads.get(bone_name)
                if head is None:
                    continue
                candidates = []
                for child_index in child_indices.get(index, []):
                    if child_index >= len(names):
                        continue
                    child_head = original_heads.get(names[child_index])
                    if child_head is None:
                        continue
                    delta = child_head - head
                    if delta.length > 1e-6:
                        candidates.append(delta)
                if candidates:
                    directions[index] = max(candidates, key=lambda item: item.length).normalized()

            for index, bone_name in enumerate(names):
                edit_bone = armature.data.edit_bones.get(bone_name)
                if edit_bone is None or index >= len(matrices):
                    result["missing"] += 1
                    continue
                length = max(edit_bone.length, 0.004)
                head = edit_bone.head.copy()
                rotation = matrices[index].to_3x3().normalized()
                parent = parents[index] if index < len(parents) else len(names)
                if index in directions:
                    y_axis = directions[index]
                elif 0 <= parent < len(names) and parent in directions:
                    y_axis = directions[parent]
                else:
                    original_tail = original_tails.get(bone_name)
                    fallback = (original_tail - head) if original_tail is not None else Vector()
                    y_axis = fallback.normalized() if fallback.length > 1e-6 else (rotation @ Vector((0.0, 1.0, 0.0))).normalized()
                z_axis = (rotation @ Vector((0.0, 0.0, 1.0))).normalized()
                edit_bone.tail = head + y_axis * length
                edit_bone.align_roll(z_axis)
                result["applied"] += 1
            bpy.ops.object.mode_set(mode="OBJECT")
            for index, bone_name in enumerate(names):
                bone = armature.data.bones.get(bone_name)
                if bone is not None and index < len(rotations):
                    bone["g4_rest_rotation_xyzw"] = rotations[index]
    finally:
        if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        if previous_active is not None:
            bpy.context.view_layer.objects.active = previous_active
        if previous_active is not None and previous_mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode=previous_mode)
            except RuntimeError:
                pass
    return result


def compact_skeleton_summary(summary: dict) -> None:
    skeleton = summary.get("skeleton")
    if not isinstance(skeleton, dict):
        return
    for key in ("names", "parent_indices", "bind_matrices", "local_matrices"):
        skeleton.pop(key, None)


def make_report_text(summary: dict) -> None:
    model_name = Path(summary.get("resolved_model", "g4_model")).stem
    text = bpy.data.texts.new(f"G4 Import Report - {model_name}")
    text.write(json.dumps(summary, indent=2))


def make_debug_text(summary: dict, lines: list[str]) -> None:
    model_name = Path(summary.get("resolved_model", "g4_model")).stem
    text = bpy.data.texts.new(f"G4 Import Debug - {model_name}")
    text.write("\n".join(lines) + "\n")


def write_debug_log(summary: dict, lines: list[str]) -> None:
    dae = Path(summary.get("dae", ""))
    if not dae:
        return
    log_path = dae.with_suffix(".g4_import_debug.log")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary["debug_log"] = str(log_path)


def image_paths() -> set[str]:
    return {str(Path(bpy.path.abspath(image.filepath)).resolve()) for image in bpy.data.images if image.filepath}


def strip_texture_variant(name: str) -> str:
    lower = name.lower()
    lower = lower.removesuffix(".dds")
    lower = lower.removesuffix(".nxtch")
    lower = lower.removesuffix(".bin")
    return lower.rsplit(".", 1)[0] if "." in lower else lower


def texture_role(path: Path) -> str:
    stem = path.stem.lower()
    if "cubemap" in stem or re.search(r"(?:^|_)cm\d+_tex$", stem):
        return "environment"
    if stem.endswith("_a.1"):
        return "transparent_base"
    if stem.endswith(".a"):
        return "alpha_red"
    if stem.endswith("msk") or stem.endswith("_mask"):
        return "mask"
    if stem.endswith("nml") or stem.endswith("_normal"):
        return "normal"
    if stem.endswith("_re.2") or stem.endswith("_n.2") or stem.endswith("_nm.2"):
        return "normal"
    if stem.endswith(".2") or stem.endswith("_n") or stem.endswith("_nm") or "_normal" in stem:
        return "normal"
    if stem.endswith("spm"):
        return "specular_mask"
    if stem.endswith("sp"):
        return "specular"
    if stem.endswith("oc"):
        return "occlusion"
    if stem.endswith("line"):
        return "line"
    return "base"


def texture_base_key(path: Path) -> str:
    stem = path.stem.lower()
    role = texture_role(path)
    if role == "transparent_base" and stem.endswith("_a.1"):
        return stem[:-2].strip("_")
    if role == "normal" and stem.endswith(".2"):
        return stem[:-2].strip("_")
    if role == "normal" and stem.endswith("nml"):
        return re.sub(r"_?nml$", "", stem).strip("_")
    if role == "alpha_red" and stem.endswith(".a"):
        return stem[:-2].strip("_")
    if role == "mask":
        return re.sub(r"(?:_?msk|_mask)$", "", stem).strip("_")
    if role in {"specular_mask", "specular", "occlusion", "line"}:
        return re.sub(r"(?:spm|sp|oc|line)$", "", stem).strip("_")
    return strip_texture_variant(stem).strip("_")


def texture_base_keys(path: Path) -> list[str]:
    key = texture_base_key(path)
    keys = [key]
    if texture_role(path) == "alpha_red" and key and not key.endswith("_a"):
        keys.append(f"{key}_a")
    if key.endswith("_a"):
        keys.append(key[:-2])

    unique = []
    seen = set()
    for item in keys:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def normalized_material_key(name: str) -> str:
    key = blender_base_name(name).lower()
    key = key[:-1] if key.endswith("m") else key
    key = key.strip("_")
    return key


def material_variant_keys(material_name: str, diffuse_path: Path | None = None) -> list[str]:
    keys = []
    if diffuse_path is not None:
        keys.extend(texture_base_keys(diffuse_path))

    key = normalized_material_key(material_name)
    if key:
        keys.append(key)
        keys.append(re.sub(r"_of$", "", key).strip("_"))
        keys.append(re.sub(r"_of_", "_", key).strip("_"))
        keys.append(re.sub(r"_of.*$", "", key).strip("_"))
        if "_a_of" in key:
            keys.append(key.replace("_a_of", "_a").strip("_"))
        if key.endswith("_a"):
            keys.append(key[:-2])

    unique = []
    seen = set()
    for item in keys:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def load_image(path: Path):
    if not path.exists():
        return None
    for image in bpy.data.images:
        if image.filepath and Path(bpy.path.abspath(image.filepath)).resolve() == path.resolve():
            return image
    try:
        return bpy.data.images.load(str(path), check_existing=True)
    except RuntimeError:
        return None


def principled_node(nodes):
    for node in nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def node_input(node, *names: str):
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            return socket
    return None


def image_texture_node(nodes, image):
    for node in nodes:
        if node.type == "TEX_IMAGE" and node.image == image:
            return node
    node = nodes.new(type="ShaderNodeTexImage")
    node.image = image
    return node


def material_uses_image(material, path: Path) -> bool:
    tree = material.node_tree
    if tree is None:
        return False
    expected = path.resolve()
    for node in tree.nodes:
        if node.type != "TEX_IMAGE" or node.image is None or not node.image.filepath:
            continue
        if Path(bpy.path.abspath(node.image.filepath)).resolve() == expected:
            return True
    return False


def find_material(name: str, diffuse_path: Path | None = None):
    named = [
        material for material in bpy.data.materials
        if material.name == name or material.name.startswith(f"{name}.")
    ]
    if diffuse_path is not None:
        for material in reversed(named):
            if material_uses_image(material, diffuse_path):
                return material
    if named:
        return named[-1]
    if diffuse_path is not None:
        for material in bpy.data.materials:
            if material_uses_image(material, diffuse_path):
                return material
    return None


def first_variant_path(variants: dict[str, list[str] | str | Path], *roles: str) -> Path | None:
    for role in roles:
        value = variants.get(role)
        if not value:
            continue
        if isinstance(value, list):
            return Path(value[0]) if value else None
        return Path(value)
    return None


def variants_from_report(
    summary: dict, material_name: str, diffuse_path: Path, fallback: dict[str, Path]
) -> dict[str, list[str] | str | Path]:
    report_variants = summary.get("texture_variants") or {}
    merged: dict[str, list[str] | str | Path] = {}
    for key in material_variant_keys(material_name, diffuse_path):
        roles = report_variants.get(key)
        if roles:
            merged.update(roles)
    for role, path in fallback.items():
        merged.setdefault(role, path)
    return merged


def clear_input_links(links, socket) -> None:
    for link in list(socket.links):
        links.remove(link)


def link_to_input(links, output, input_socket) -> None:
    if input_socket is None:
        return
    clear_input_links(links, input_socket)
    links.new(output, input_socket)


def set_transparent_material(material) -> None:
    material.blend_method = "BLEND"
    material.use_screen_refraction = False
    material.show_transparent_back = True


def base_color_texture_node(material, principled):
    base_input = node_input(principled, "Base Color")
    if base_input is None:
        return None
    for link in base_input.links:
        node = link.from_node
        if node.type == "TEX_IMAGE" and node.image is not None:
            return node

    tree = material.node_tree
    if tree is None:
        return None
    image_nodes = [node for node in tree.nodes if node.type == "TEX_IMAGE" and node.image is not None]
    if len(image_nodes) == 1:
        return image_nodes[0]
    return None


def connect_base_texture_alpha(material, principled) -> bool:
    tree = material.node_tree
    if tree is None:
        return False
    alpha_input = node_input(principled, "Alpha")
    if alpha_input is None:
        return False
    node = base_color_texture_node(material, principled)
    if node is None:
        return False
    alpha_output = node.outputs.get("Alpha")
    if alpha_output is None:
        return False
    link_to_input(tree.links, alpha_output, alpha_input)
    set_transparent_material(material)
    return True


def material_base_image_path(material) -> Path | None:
    tree = material.node_tree
    if tree is None:
        return None
    principled = principled_node(tree.nodes)
    if principled is None:
        return None
    node = base_color_texture_node(material, principled)
    if node is None or node.image is None or not node.image.filepath:
        return None
    return Path(bpy.path.abspath(node.image.filepath)).resolve()


def name_implies_alpha(material_name: str, diffuse_path: Path) -> bool:
    text = f"{material_name}_{diffuse_path.stem}".lower()
    return (
        "_a_" in text
        or text.endswith("_a")
        or "_a." in text
        or "_of_" in text
        or "_of" in text
        or "alpha" in text
    )


def build_texture_index(paths: list[Path]) -> dict[str, dict[str, Path]]:
    by_key: dict[str, dict[str, Path]] = {}
    for texture in paths:
        for key in texture_base_keys(texture):
            by_key.setdefault(key, {})[texture_role(texture)] = texture
    return by_key


def all_available_texture_paths(summary: dict) -> list[Path]:
    paths = [Path(path) for path in summary.get("textures", [])]
    for path in list(paths):
        parent = path.parent
        if parent.exists():
            paths.extend(sorted(parent.glob("*.dds")))
    unique = []
    seen = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def material_variants_from_index(material_name: str, base_path: Path, by_key: dict[str, dict[str, Path]]) -> dict[str, Path]:
    variants: dict[str, Path] = {}
    for key in material_variant_keys(material_name, base_path):
        variants.update(by_key.get(key, {}))
    return variants


def connect_alpha_mask(material, principled, mask_path: Path, debug: list[str] | None = None) -> bool:
    image = load_image(mask_path)
    alpha_input = node_input(principled, "Alpha")
    if image is None or alpha_input is None or material.node_tree is None:
        if debug is not None:
            debug.append(
                f"[connect] {material.name}: failed alpha mask {mask_path.name} "
                f"image={image is not None} input={alpha_input is not None} tree={material.node_tree is not None}"
            )
        return False
    image.colorspace_settings.name = "Non-Color"
    tex = image_texture_node(material.node_tree.nodes, image)
    sep = material.node_tree.nodes.new(type="ShaderNodeSeparateColor")
    material.node_tree.links.new(tex.outputs["Color"], sep.inputs["Color"])
    link_to_input(material.node_tree.links, sep.outputs["Red"], alpha_input)
    set_transparent_material(material)
    if debug is not None:
        debug.append(f"[connect] {material.name}: linked alpha mask {mask_path.name}")
    return True


def connect_normal_map(material, principled, normal_path: Path, debug: list[str] | None = None) -> bool:
    image = load_image(normal_path)
    normal_input = node_input(principled, "Normal")
    if image is None or normal_input is None or material.node_tree is None:
        if debug is not None:
            debug.append(
                f"[connect] {material.name}: failed normal {normal_path.name} "
                f"image={image is not None} input={normal_input is not None} tree={material.node_tree is not None}"
            )
        return False
    image.colorspace_settings.name = "Non-Color"
    tex = image_texture_node(material.node_tree.nodes, image)
    normal = material.node_tree.nodes.new(type="ShaderNodeNormalMap")
    material.node_tree.links.new(tex.outputs["Color"], normal.inputs["Color"])
    link_to_input(material.node_tree.links, normal.outputs["Normal"], normal_input)
    if debug is not None:
        debug.append(f"[connect] {material.name}: linked normal {normal_path.name}")
    return True


def apply_level5_toon_shader(
    material,
    base_path: Path,
    variants: dict[str, Path],
    debug: list[str] | None = None,
) -> bool:
    base_image = load_image(base_path)
    if base_image is None or material.node_tree is None:
        return False
    tree = material.node_tree
    nodes = tree.nodes
    links = tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (1040, 120)
    base = nodes.new("ShaderNodeTexImage")
    base.name = "G4 Base"
    base.label = base_path.name
    base.image = base_image
    base.interpolation = "Linear"
    base.location = (-980, 280)

    saturation = nodes.new("ShaderNodeHueSaturation")
    saturation.name = "G4 Saturation"
    saturation.location = (-760, 280)
    # Standard display transform preserves the authored palette; this small
    # correction matches the in-game red-hair reference without clipping it.
    saturation.inputs["Saturation"].default_value = 1.10
    saturation.inputs["Value"].default_value = 1.04
    links.new(base.outputs["Color"], saturation.inputs["Color"])

    # Cool authored regions (uniforms, ribbons and cyan accents) need a
    # channel-aware correction: a global exposure that fixes skin and hair
    # otherwise washes their red channel towards grey.
    palette_channels = nodes.new("ShaderNodeSeparateColor")
    palette_channels.name = "G4 Palette Channels"
    palette_channels.location = (-760, 500)
    cool_delta = nodes.new("ShaderNodeMath")
    cool_delta.name = "G4 Cool Color Delta"
    cool_delta.operation = "SUBTRACT"
    cool_delta.location = (-570, 500)
    cool_mask = nodes.new("ShaderNodeMath")
    cool_mask.name = "G4 Cool Palette Mask"
    cool_mask.operation = "GREATER_THAN"
    cool_mask.inputs[1].default_value = 0.08
    cool_mask.location = (-380, 500)
    cool_correction = nodes.new("ShaderNodeMixRGB")
    cool_correction.name = "G4 Cool Palette Correction"
    cool_correction.blend_type = "MULTIPLY"
    cool_correction.inputs[2].default_value = (0.60, 0.90, 0.94, 1.0)
    cool_correction.location = (-170, 360)
    links.new(base.outputs["Color"], palette_channels.inputs["Color"])
    links.new(palette_channels.outputs["Blue"], cool_delta.inputs[0])
    links.new(palette_channels.outputs["Red"], cool_delta.inputs[1])
    links.new(cool_delta.outputs[0], cool_mask.inputs[0])
    links.new(cool_mask.outputs[0], cool_correction.inputs[0])
    links.new(saturation.outputs["Color"], cool_correction.inputs[1])
    base_color = cool_correction.outputs["Color"]

    surface_normal = None
    normal_path = first_variant_path(variants, "normal")
    if normal_path is not None:
        image = load_image(normal_path)
        if image is not None:
            image.colorspace_settings.name = "Non-Color"
            normal_texture = nodes.new("ShaderNodeTexImage")
            normal_texture.name = "G4 Normal"
            normal_texture.image = image
            normal_texture.location = (-980, -160)
            normal_channels = nodes.new("ShaderNodeSeparateColor")
            normal_channels.name = "G4 DXT5nm Channels"
            normal_channels.location = (-780, -160)
            normal_x = nodes.new("ShaderNodeMath")
            normal_x.operation = "MULTIPLY_ADD"
            normal_x.inputs[1].default_value = 2.0
            normal_x.inputs[2].default_value = -1.0
            normal_x.location = (-580, -200)
            normal_y = nodes.new("ShaderNodeMath")
            normal_y.operation = "MULTIPLY_ADD"
            normal_y.inputs[1].default_value = 2.0
            normal_y.inputs[2].default_value = -1.0
            normal_y.location = (-580, -280)
            normal_x2 = nodes.new("ShaderNodeMath")
            normal_x2.operation = "MULTIPLY"
            normal_x2.location = (-390, -200)
            normal_y2 = nodes.new("ShaderNodeMath")
            normal_y2.operation = "MULTIPLY"
            normal_y2.location = (-390, -280)
            normal_xy2 = nodes.new("ShaderNodeMath")
            normal_xy2.operation = "ADD"
            normal_xy2.location = (-210, -240)
            normal_z2 = nodes.new("ShaderNodeMath")
            normal_z2.operation = "SUBTRACT"
            normal_z2.inputs[0].default_value = 1.0
            normal_z2.location = (-30, -240)
            normal_z2.use_clamp = True
            normal_z = nodes.new("ShaderNodeMath")
            normal_z.operation = "SQRT"
            normal_z.location = (150, -240)
            encoded_z = nodes.new("ShaderNodeMath")
            encoded_z.operation = "MULTIPLY_ADD"
            encoded_z.inputs[1].default_value = 0.5
            encoded_z.inputs[2].default_value = 0.5
            encoded_z.location = (330, -240)
            packed_normal = nodes.new("ShaderNodeCombineColor")
            packed_normal.name = "G4 Reconstructed Normal"
            packed_normal.location = (510, -180)
            normal_map = nodes.new("ShaderNodeNormalMap")
            normal_map.name = "G4 Surface Normal"
            normal_map.location = (700, -180)
            normal_map.inputs["Strength"].default_value = 1.0
            links.new(normal_texture.outputs["Color"], normal_channels.inputs["Color"])
            links.new(normal_texture.outputs["Alpha"], normal_x.inputs[0])
            links.new(normal_channels.outputs["Green"], normal_y.inputs[0])
            links.new(normal_x.outputs[0], normal_x2.inputs[0])
            links.new(normal_x.outputs[0], normal_x2.inputs[1])
            links.new(normal_y.outputs[0], normal_y2.inputs[0])
            links.new(normal_y.outputs[0], normal_y2.inputs[1])
            links.new(normal_x2.outputs[0], normal_xy2.inputs[0])
            links.new(normal_y2.outputs[0], normal_xy2.inputs[1])
            links.new(normal_xy2.outputs[0], normal_z2.inputs[1])
            links.new(normal_z2.outputs[0], normal_z.inputs[0])
            links.new(normal_z.outputs[0], encoded_z.inputs[0])
            links.new(normal_texture.outputs["Alpha"], packed_normal.inputs["Red"])
            links.new(normal_channels.outputs["Green"], packed_normal.inputs["Green"])
            links.new(encoded_z.outputs[0], packed_normal.inputs["Blue"])
            links.new(packed_normal.outputs["Color"], normal_map.inputs["Color"])
            surface_normal = normal_map.outputs["Normal"]

    mask_path = first_variant_path(variants, "mask")
    if mask_path is not None:
        image = load_image(mask_path)
        if image is not None:
            image.colorspace_settings.name = "Non-Color"
            mask = nodes.new("ShaderNodeTexImage")
            mask.name = "G4 Recolor Mask"
            mask.image = image
            mask.location = (-980, 580)
            channels = nodes.new("ShaderNodeSeparateColor")
            channels.location = (-760, 580)
            links.new(mask.outputs["Color"], channels.inputs["Color"])
            for index, channel in enumerate(("Red", "Green", "Blue")):
                tint = nodes.new("ShaderNodeMixRGB")
                tint.name = f"G4 Mask {channel} Tint"
                tint.label = f"G4 Mask {channel} Tint"
                tint.blend_type = "MULTIPLY"
                tint.location = (-520 + index * 180, 430)
                tint.inputs[2].default_value = (1.0, 1.0, 1.0, 1.0)
                links.new(channels.outputs[channel], tint.inputs[0])
                links.new(base_color, tint.inputs[1])
                base_color = tint.outputs["Color"]

    occlusion_channels = None
    occlusion_path = first_variant_path(variants, "occlusion")
    if occlusion_path is not None:
        image = load_image(occlusion_path)
        if image is not None:
            image.colorspace_settings.name = "Non-Color"
            occlusion = nodes.new("ShaderNodeTexImage")
            occlusion.name = "G4 Occlusion"
            occlusion.image = image
            occlusion.location = (-980, 760)
            occlusion_channels = nodes.new("ShaderNodeSeparateColor")
            occlusion_channels.name = "G4 Occlusion Channels"
            occlusion_channels.location = (-760, 760)
            links.new(occlusion.outputs["Color"], occlusion_channels.inputs["Color"])

    diffuse = nodes.new("ShaderNodeBsdfDiffuse")
    diffuse.location = (-720, -100)
    diffuse.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    diffuse.inputs["Roughness"].default_value = 0.0
    if surface_normal is not None:
        links.new(surface_normal, diffuse.inputs["Normal"])
    diffuse_rgb = nodes.new("ShaderNodeShaderToRGB")
    diffuse_rgb.location = (-520, -100)
    diffuse_bw = nodes.new("ShaderNodeRGBToBW")
    diffuse_bw.location = (-340, -100)
    light_floor = nodes.new("ShaderNodeMath")
    light_floor.name = "G4 Toon Ambient"
    light_floor.operation = "MULTIPLY_ADD"
    light_floor.inputs[1].default_value = 0.88
    light_floor.inputs[2].default_value = 0.12
    light_floor.use_clamp = True
    light_floor.location = (-160, -100)
    links.new(diffuse.outputs["BSDF"], diffuse_rgb.inputs["Shader"])
    links.new(diffuse_rgb.outputs["Color"], diffuse_bw.inputs["Color"])
    links.new(diffuse_bw.outputs["Val"], light_floor.inputs[0])

    safe_brightness = nodes.new("ShaderNodeMath")
    safe_brightness.operation = "MAXIMUM"
    safe_brightness.inputs[1].default_value = 0.001
    safe_brightness.location = (-330, -250)
    light_chroma = nodes.new("ShaderNodeVectorMath")
    light_chroma.operation = "DIVIDE"
    light_chroma.location = (-140, -250)
    light_chroma_limit = nodes.new("ShaderNodeVectorMath")
    light_chroma_limit.operation = "MINIMUM"
    light_chroma_limit.inputs[1].default_value = (1.5, 1.5, 1.5)
    light_chroma_limit.location = (40, -250)
    light_tint = nodes.new("ShaderNodeMixRGB")
    light_tint.name = "G4 Point Light Color"
    light_tint.inputs[0].default_value = 0.18
    light_tint.inputs[1].default_value = (1.0, 1.0, 1.0, 1.0)
    light_tint.location = (220, -250)
    links.new(diffuse_bw.outputs["Val"], safe_brightness.inputs[0])
    links.new(diffuse_rgb.outputs["Color"], light_chroma.inputs[0])
    links.new(safe_brightness.outputs[0], light_chroma.inputs[1])
    links.new(light_chroma.outputs["Vector"], light_chroma_limit.inputs[0])
    links.new(light_chroma_limit.outputs["Vector"], light_tint.inputs[2])

    toon_factor = light_floor.outputs[0]
    if occlusion_channels is not None:
        occlusion_offset = nodes.new("ShaderNodeMath")
        occlusion_offset.operation = "MULTIPLY_ADD"
        occlusion_offset.inputs[1].default_value = 0.18
        occlusion_offset.inputs[2].default_value = -0.18
        occlusion_offset.location = (-520, 680)
        occlusion_add = nodes.new("ShaderNodeMath")
        occlusion_add.name = "G4 Occlusion Threshold"
        occlusion_add.operation = "ADD"
        occlusion_add.use_clamp = True
        occlusion_add.location = (-320, 680)
        links.new(occlusion_channels.outputs["Red"], occlusion_offset.inputs[0])
        links.new(toon_factor, occlusion_add.inputs[0])
        links.new(occlusion_offset.outputs[0], occlusion_add.inputs[1])
        toon_factor = occlusion_add.outputs[0]

    shadow_primary = nodes.new("ShaderNodeValToRGB")
    shadow_primary.name = "G4 Shadow Color 0"
    shadow_primary.location = (20, -80)
    shadow_primary.color_ramp.interpolation = "CONSTANT"
    shadow_primary.color_ramp.elements[0].color = (0.58, 0.58, 0.58, 1.0)
    shadow_primary.color_ramp.elements[1].position = 0.32
    shadow_primary.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
    links.new(toon_factor, shadow_primary.inputs["Fac"])

    secondary_factor = toon_factor
    if occlusion_channels is not None:
        inverse_light = nodes.new("ShaderNodeMath")
        inverse_light.operation = "SUBTRACT"
        inverse_light.inputs[0].default_value = 1.0
        inverse_light.location = (-300, 580)
        secondary_offset = nodes.new("ShaderNodeMath")
        secondary_offset.operation = "MULTIPLY"
        secondary_offset.location = (-120, 580)
        secondary_add = nodes.new("ShaderNodeMath")
        secondary_add.operation = "ADD"
        secondary_add.use_clamp = True
        secondary_add.location = (60, 580)
        links.new(toon_factor, inverse_light.inputs[1])
        links.new(inverse_light.outputs[0], secondary_offset.inputs[0])
        links.new(occlusion_channels.outputs["Green"], secondary_offset.inputs[1])
        links.new(toon_factor, secondary_add.inputs[0])
        links.new(secondary_offset.outputs[0], secondary_add.inputs[1])
        secondary_factor = secondary_add.outputs[0]

    shadow_secondary = nodes.new("ShaderNodeValToRGB")
    shadow_secondary.name = "G4 Shadow Color 1"
    shadow_secondary.location = (240, -80)
    shadow_secondary.color_ramp.interpolation = "CONSTANT"
    shadow_secondary.color_ramp.elements[0].color = (0.82, 0.82, 0.82, 1.0)
    shadow_secondary.color_ramp.elements[1].position = 0.46
    shadow_secondary.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
    links.new(secondary_factor, shadow_secondary.inputs["Fac"])

    shadow_mix = nodes.new("ShaderNodeMixRGB")
    shadow_mix.name = "G4 Dual Toon Ramp"
    shadow_mix.blend_type = "MIX"
    shadow_mix.inputs[0].default_value = 0.18
    shadow_mix.location = (450, -40)
    links.new(shadow_primary.outputs["Color"], shadow_mix.inputs[1])
    links.new(shadow_secondary.outputs["Color"], shadow_mix.inputs[2])

    lit = nodes.new("ShaderNodeMixRGB")
    lit.name = "G4 Cel Diffuse"
    lit.blend_type = "MULTIPLY"
    lit.inputs[0].default_value = 1.0
    lit.location = (60, 250)
    links.new(base_color, lit.inputs[1])
    links.new(shadow_mix.outputs["Color"], lit.inputs[2])
    color_output = lit.outputs["Color"]

    if occlusion_channels is not None:
        base_luminance = nodes.new("ShaderNodeRGBToBW")
        base_luminance.location = (40, 700)
        luminance_scale = nodes.new("ShaderNodeMath")
        luminance_scale.operation = "MULTIPLY"
        luminance_scale.inputs[1].default_value = 0.2
        luminance_scale.location = (220, 700)
        recovery = nodes.new("ShaderNodeMath")
        recovery.name = "G4 Albedo Recovery"
        recovery.operation = "ADD"
        recovery.use_clamp = True
        recovery.location = (400, 700)
        recovery_mix = nodes.new("ShaderNodeMixRGB")
        recovery_mix.name = "G4 Occlusion Composite"
        recovery_mix.location = (590, 500)
        links.new(base_color, base_luminance.inputs["Color"])
        links.new(base_luminance.outputs["Val"], luminance_scale.inputs[0])
        links.new(luminance_scale.outputs[0], recovery.inputs[0])
        links.new(occlusion_channels.outputs["Blue"], recovery.inputs[1])
        links.new(recovery.outputs[0], recovery_mix.inputs[0])
        links.new(color_output, recovery_mix.inputs[1])
        links.new(base_color, recovery_mix.inputs[2])
        color_output = recovery_mix.outputs["Color"]

        painted_occlusion = nodes.new("ShaderNodeValToRGB")
        painted_occlusion.name = "G4 Painted Occlusion"
        painted_occlusion.color_ramp.interpolation = "EASE"
        painted_occlusion.color_ramp.elements[0].position = 0.0
        painted_occlusion.color_ramp.elements[0].color = (0.48, 0.48, 0.48, 1.0)
        middle = painted_occlusion.color_ramp.elements.new(0.4)
        # 0.4 is the authored neutral plateau used across character hair and
        # clothing, not a 37% cavity shadow. Preserve it while keeping true
        # zero-valued creases visibly occluded.
        middle.color = (0.90, 0.90, 0.90, 1.0)
        painted_occlusion.color_ramp.elements[-1].position = 0.62
        painted_occlusion.color_ramp.elements[-1].color = (1.0, 1.0, 1.0, 1.0)
        painted_occlusion.location = (570, 680)
        occlusion_multiply = nodes.new("ShaderNodeMixRGB")
        occlusion_multiply.name = "G4 Painted Occlusion Composite"
        occlusion_multiply.blend_type = "MULTIPLY"
        occlusion_multiply.inputs[0].default_value = 1.0
        occlusion_multiply.location = (760, 520)
        links.new(occlusion_channels.outputs["Red"], painted_occlusion.inputs["Fac"])
        links.new(color_output, occlusion_multiply.inputs[1])
        links.new(painted_occlusion.outputs["Color"], occlusion_multiply.inputs[2])
        color_output = occlusion_multiply.outputs["Color"]

    colored_light = nodes.new("ShaderNodeMixRGB")
    colored_light.name = "G4 Colored Light Composite"
    colored_light.blend_type = "MULTIPLY"
    colored_light.inputs[0].default_value = 1.0
    colored_light.location = (760, 440)
    links.new(color_output, colored_light.inputs[1])
    links.new(light_tint.outputs["Color"], colored_light.inputs[2])
    color_output = colored_light.outputs["Color"]

    line_path = first_variant_path(variants, "line")
    line_informative = False
    if line_path is not None:
        image = load_image(line_path)
        if image is not None:
            image.colorspace_settings.name = "Non-Color"
            line_texture = nodes.new("ShaderNodeTexImage")
            line_texture.name = "G4 Line Parameter"
            line_texture.label = line_path.name
            line_texture.image = image
            line_texture.location = (-180, -520)
            line_informative = max(image.size) > 8
            line_channels = nodes.new("ShaderNodeSeparateColor")
            line_channels.name = "G4 Line Channels"
            line_channels.location = (20, -520)
            line_facing = nodes.new("ShaderNodeLayerWeight")
            line_facing.name = "G4 Line Facing"
            line_facing.location = (20, -620)
            line_edge = nodes.new("ShaderNodeMath")
            line_edge.name = "G4 UV Outline Width"
            line_edge.operation = "POWER"
            line_edge.inputs[1].default_value = 4.0 if line_informative else 10.0
            line_edge.location = (210, -620)
            line_weight = nodes.new("ShaderNodeMath")
            line_weight.name = "G4 UV Outline Mask"
            line_weight.operation = "MULTIPLY"
            line_weight.location = (390, -560)
            contour_edge = nodes.new("ShaderNodeMath")
            contour_edge.name = "G4 Under Rim Width"
            contour_edge.operation = "POWER"
            contour_edge.inputs[1].default_value = 2.5
            contour_edge.location = (210, -700)
            contour_mask = nodes.new("ShaderNodeMath")
            contour_mask.name = "G4 Under Rim Mask"
            contour_mask.operation = "MULTIPLY"
            contour_mask.location = (390, -680)
            contour_strength = nodes.new("ShaderNodeMath")
            contour_strength.name = "G4 Under Rim Strength"
            contour_strength.operation = "MULTIPLY"
            contour_strength.inputs[1].default_value = 0.48 if line_informative else 0.12
            contour_strength.location = (570, -680)
            contour_shadow = nodes.new("ShaderNodeMixRGB")
            contour_shadow.name = "G4 Warm Under Rim"
            contour_shadow.blend_type = "MULTIPLY"
            contour_shadow.inputs[2].default_value = (0.68, 0.52, 0.48, 1.0)
            contour_shadow.location = (570, 440)
            line_composite = nodes.new("ShaderNodeMixRGB")
            line_composite.name = "G4 UV Outline Composite"
            line_composite.inputs[2].default_value = (0.018, 0.012, 0.018, 1.0)
            line_composite.location = (570, 360)
            links.new(line_texture.outputs["Color"], line_channels.inputs["Color"])
            links.new(line_facing.outputs["Fresnel"], line_edge.inputs[0])
            links.new(line_facing.outputs["Fresnel"], contour_edge.inputs[0])
            links.new(line_edge.outputs[0], line_weight.inputs[0])
            links.new(line_channels.outputs["Blue"], line_weight.inputs[1])
            links.new(contour_edge.outputs[0], contour_mask.inputs[0])
            links.new(line_channels.outputs["Blue"], contour_mask.inputs[1])
            links.new(contour_mask.outputs[0], contour_strength.inputs[0])
            links.new(contour_strength.outputs[0], contour_shadow.inputs[0])
            links.new(color_output, contour_shadow.inputs[1])
            links.new(line_weight.outputs[0], line_composite.inputs[0])
            links.new(contour_shadow.outputs["Color"], line_composite.inputs[1])
            color_output = line_composite.outputs["Color"]

    specular_mask_path = first_variant_path(variants, "specular_mask")
    specular_path = first_variant_path(variants, "specular")
    if specular_mask_path is not None and specular_path is not None:
        image = load_image(specular_mask_path)
        specular_image = load_image(specular_path)
        if image is not None and specular_image is not None:
            image.colorspace_settings.name = "Non-Color"
            specular_image.colorspace_settings.name = "Non-Color"
            specular_mask = nodes.new("ShaderNodeTexImage")
            specular_mask.name = "G4 Specular Mask"
            specular_mask.image = image
            specular_mask.location = (-420, -760)
            coordinates = nodes.new("ShaderNodeNewGeometry")
            coordinates.location = (-660, -980)
            view_normal = nodes.new("ShaderNodeVectorTransform")
            view_normal.name = "G4 View Normal"
            view_normal.vector_type = "NORMAL"
            view_normal.convert_from = "WORLD"
            view_normal.convert_to = "CAMERA"
            view_normal.location = (-460, -980)
            sphere_mapping = nodes.new("ShaderNodeMapping")
            sphere_mapping.name = "G4 Sphere Projection"
            sphere_mapping.inputs["Location"].default_value = (0.5, 0.5, 0.0)
            sphere_mapping.inputs["Scale"].default_value = (0.5, 0.5, 1.0)
            sphere_mapping.location = (-260, -980)
            specular_shape = nodes.new("ShaderNodeTexImage")
            specular_shape.name = "G4 Specular Shape"
            specular_shape.image = specular_image
            specular_shape.extension = "CLIP"
            specular_shape.location = (-40, -980)
            shape_multiply = nodes.new("ShaderNodeMixRGB")
            shape_multiply.name = "G4 Matcap Mask"
            shape_multiply.blend_type = "MULTIPLY"
            shape_multiply.inputs[0].default_value = 1.0
            shape_multiply.location = (180, -820)
            lit_specular = nodes.new("ShaderNodeMixRGB")
            lit_specular.name = "G4 Lit Matcap"
            lit_specular.blend_type = "MULTIPLY"
            lit_specular.inputs[0].default_value = 1.0
            lit_specular.location = (380, -720)
            normal_source = surface_normal if surface_normal is not None else coordinates.outputs["Normal"]
            links.new(normal_source, view_normal.inputs["Vector"])
            links.new(view_normal.outputs["Vector"], sphere_mapping.inputs["Vector"])
            links.new(sphere_mapping.outputs["Vector"], specular_shape.inputs["Vector"])
            links.new(specular_shape.outputs["Color"], shape_multiply.inputs[1])
            links.new(specular_mask.outputs["Color"], shape_multiply.inputs[2])
            links.new(shape_multiply.outputs["Color"], lit_specular.inputs[1])
            links.new(shadow_mix.outputs["Color"], lit_specular.inputs[2])
            spec_multiply = nodes.new("ShaderNodeMixRGB")
            spec_multiply.name = "G4 Specular Strength"
            spec_multiply.blend_type = "MULTIPLY"
            spec_multiply.inputs[0].default_value = 0.22
            spec_multiply.inputs[2].default_value = (0.42, 0.42, 0.42, 1.0)
            spec_multiply.location = (560, -680)
            spec_add = nodes.new("ShaderNodeMixRGB")
            spec_add.name = "G4 Specular Composite"
            spec_add.blend_type = "ADD"
            spec_add.inputs[0].default_value = 1.0
            spec_add.location = (760, 80)
            links.new(lit_specular.outputs["Color"], spec_multiply.inputs[1])
            links.new(color_output, spec_add.inputs[1])
            links.new(spec_multiply.outputs["Color"], spec_add.inputs[2])
            color_output = spec_add.outputs["Color"]

    layer_weight = nodes.new("ShaderNodeLayerWeight")
    layer_weight.name = "G4 View Facing"
    layer_weight.location = (260, -430)
    grazing = nodes.new("ShaderNodeMath")
    grazing.name = "G4 Grazing Angle"
    grazing.operation = "SUBTRACT"
    grazing.inputs[0].default_value = 1.0
    grazing.location = (440, -430)
    inverse_toon = nodes.new("ShaderNodeMath")
    inverse_toon.operation = "SUBTRACT"
    inverse_toon.inputs[0].default_value = 1.0
    inverse_toon.location = (440, -520)
    highlight_factor = nodes.new("ShaderNodeMath")
    highlight_factor.name = "G4 Highlight Factor"
    highlight_factor.operation = "MULTIPLY"
    highlight_factor.location = (620, -390)
    underlight_factor = nodes.new("ShaderNodeMath")
    underlight_factor.name = "G4 Underlight Factor"
    underlight_factor.operation = "MULTIPLY"
    underlight_factor.location = (620, -500)
    highlight_add = nodes.new("ShaderNodeMixRGB")
    highlight_add.name = "G4 Highlight"
    highlight_add.blend_type = "ADD"
    highlight_add.inputs[2].default_value = (0.16, 0.12, 0.09, 1.0)
    highlight_add.location = (800, 250)
    underlight_add = nodes.new("ShaderNodeMixRGB")
    underlight_add.name = "G4 Under Light"
    underlight_add.blend_type = "ADD"
    underlight_add.inputs[2].default_value = (0.05, 0.025, 0.022, 1.0)
    underlight_add.location = (980, 250)
    links.new(layer_weight.outputs["Facing"], grazing.inputs[1])
    links.new(toon_factor, inverse_toon.inputs[1])
    links.new(grazing.outputs[0], highlight_factor.inputs[0])
    links.new(toon_factor, highlight_factor.inputs[1])
    links.new(grazing.outputs[0], underlight_factor.inputs[0])
    links.new(inverse_toon.outputs[0], underlight_factor.inputs[1])
    links.new(highlight_factor.outputs[0], highlight_add.inputs[0])
    links.new(color_output, highlight_add.inputs[1])
    links.new(underlight_factor.outputs[0], underlight_add.inputs[0])
    links.new(highlight_add.outputs["Color"], underlight_add.inputs[1])
    color_output = underlight_add.outputs["Color"]

    wetness = nodes.new("ShaderNodeValue")
    wetness.name = "G4 Wetness"
    wetness.label = "G4 Wetness"
    wetness.outputs[0].default_value = 0.0
    wetness.location = (620, -610)
    wet_diffuse = nodes.new("ShaderNodeMixRGB")
    wet_diffuse.name = "G4 Wet Diffuse"
    wet_diffuse.blend_type = "MULTIPLY"
    wet_diffuse.inputs[2].default_value = (0.82, 0.85, 0.88, 1.0)
    wet_diffuse.location = (1160, 250)
    wet_glossy = nodes.new("ShaderNodeBsdfGlossy")
    wet_glossy.inputs["Roughness"].default_value = 0.16
    wet_glossy.location = (800, -610)
    if surface_normal is not None:
        links.new(surface_normal, wet_glossy.inputs["Normal"])
    wet_glossy_rgb = nodes.new("ShaderNodeShaderToRGB")
    wet_glossy_rgb.location = (980, -610)
    wet_glossy_bw = nodes.new("ShaderNodeRGBToBW")
    wet_glossy_bw.location = (1160, -610)
    wet_glossy_ramp = nodes.new("ShaderNodeValToRGB")
    wet_glossy_ramp.name = "G4 Wet Specular"
    wet_glossy_ramp.color_ramp.interpolation = "CONSTANT"
    wet_glossy_ramp.color_ramp.elements[0].color = (0.0, 0.0, 0.0, 1.0)
    wet_glossy_ramp.color_ramp.elements[1].position = 0.72
    wet_glossy_ramp.color_ramp.elements[1].color = (0.22, 0.26, 0.30, 1.0)
    wet_glossy_ramp.location = (1340, -610)
    wet_add = nodes.new("ShaderNodeMixRGB")
    wet_add.name = "G4 Wet Composite"
    wet_add.blend_type = "ADD"
    wet_add.location = (1520, 250)
    links.new(wetness.outputs[0], wet_diffuse.inputs[0])
    links.new(color_output, wet_diffuse.inputs[1])
    links.new(wet_glossy.outputs["BSDF"], wet_glossy_rgb.inputs["Shader"])
    links.new(wet_glossy_rgb.outputs["Color"], wet_glossy_bw.inputs["Color"])
    links.new(wet_glossy_bw.outputs["Val"], wet_glossy_ramp.inputs["Fac"])
    links.new(wetness.outputs[0], wet_add.inputs[0])
    links.new(wet_diffuse.outputs["Color"], wet_add.inputs[1])
    links.new(wet_glossy_ramp.outputs["Color"], wet_add.inputs[2])
    color_output = wet_add.outputs["Color"]

    emission = nodes.new("ShaderNodeEmission")
    emission.location = (820, 180)
    emission.inputs["Strength"].default_value = 1.0
    links.new(color_output, emission.inputs["Color"])
    alpha_output = base.outputs.get("Alpha")
    alpha_path = first_variant_path(variants, "alpha_red")
    if alpha_path is not None:
        image = load_image(alpha_path)
        if image is not None:
            image.colorspace_settings.name = "Non-Color"
            alpha_texture = nodes.new("ShaderNodeTexImage")
            alpha_texture.image = image
            alpha_texture.location = (420, 600)
            alpha_separate = nodes.new("ShaderNodeSeparateColor")
            alpha_separate.location = (610, 600)
            links.new(alpha_texture.outputs["Color"], alpha_separate.inputs["Color"])
            alpha_output = alpha_separate.outputs["Red"]
    if alpha_output is not None and (alpha_path is not None or name_implies_alpha(material.name, base_path)):
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        transparent.location = (820, 20)
        mix_shader = nodes.new("ShaderNodeMixShader")
        mix_shader.location = (1020, 100)
        links.new(alpha_output, mix_shader.inputs[0])
        links.new(transparent.outputs["BSDF"], mix_shader.inputs[1])
        links.new(emission.outputs["Emission"], mix_shader.inputs[2])
        links.new(mix_shader.outputs["Shader"], output.inputs["Surface"])
        set_transparent_material(material)
    else:
        links.new(emission.outputs["Emission"], output.inputs["Surface"])
    material["g4_level5_toon"] = True
    material["g4_base_texture"] = str(base_path)
    material["g4_recolor_mask"] = str(mask_path) if mask_path is not None else ""
    material["g4_normal_texture"] = str(normal_path) if normal_path is not None else ""
    material["g4_line_texture"] = str(line_path) if line_path is not None else ""
    material["g4_line_informative"] = line_informative
    if debug is not None:
        debug.append(
            f"[toon] {material.name}: oc={occlusion_path and occlusion_path.name} "
            f"line={line_path and line_path.name} sp={specular_path and specular_path.name} "
            f"spm={specular_mask_path and specular_mask_path.name} "
            f"nrm={normal_path and normal_path.name} line_map={line_informative}"
        )
    return True


def apply_material_texture_variants(summary: dict, debug: list[str] | None = None) -> None:
    textures = [Path(path) for path in summary.get("textures", [])]
    if not textures:
        if debug is not None:
            debug.append("[summary-pass] no textures in exporter summary")
        return

    by_key = build_texture_index(textures)
    if debug is not None:
        debug.append(f"[summary-pass] textures={len(textures)} keys={len(by_key)} materials={len(summary.get('materials', {}))}")

    for material_name, diffuse_path_text in summary.get("materials", {}).items():
        if not diffuse_path_text:
            if debug is not None:
                debug.append(f"[summary-pass] {material_name}: no diffuse path in summary")
            continue
        diffuse_path = Path(diffuse_path_text)
        material = find_material(material_name, diffuse_path)
        fallback_variants = {}
        for key in material_variant_keys(material_name, diffuse_path):
            fallback_variants.update(by_key.get(key, {}))
        variants = variants_from_report(summary, material_name, diffuse_path, fallback_variants)
        if material is None:
            if debug is not None:
                debug.append(
                    f"[summary-pass] {material_name}: material not found; diffuse={diffuse_path.name} "
                    f"keys={material_variant_keys(material_name, diffuse_path)} variants={list(variants)}"
                )
            continue
        if debug is not None:
            debug.append(
                f"[summary-pass] {material.name}: diffuse={diffuse_path.name} "
                f"keys={material_variant_keys(material_name, diffuse_path)} variants={list(variants)}"
            )

        material.use_nodes = True
        tree = material.node_tree
        if tree is None:
            continue
        nodes = tree.nodes
        links = tree.links
        principled = principled_node(nodes)
        if principled is None:
            continue

        base_image = load_image(diffuse_path)
        base_input = node_input(principled, "Base Color")
        if base_image is not None and base_input is not None:
            tex = image_texture_node(nodes, base_image)
            link_to_input(links, tex.outputs["Color"], base_input)
            if debug is not None:
                debug.append(f"[summary-pass] {material.name}: linked base {diffuse_path.name}")
        elif debug is not None:
            debug.append(f"[summary-pass] {material.name}: skipped base image={base_image is not None} input={base_input is not None}")

        alpha_path = first_variant_path(variants, "alpha_red")
        if alpha_path is None and name_implies_alpha(material_name, diffuse_path):
            alpha_path = first_variant_path(variants, "mask")
        if alpha_path is not None:
            image = load_image(alpha_path)
            alpha_input = node_input(principled, "Alpha")
            if image is not None and alpha_input is not None:
                image.colorspace_settings.name = "Non-Color"
                tex = image_texture_node(nodes, image)
                sep = nodes.new(type="ShaderNodeSeparateColor")
                links.new(tex.outputs["Color"], sep.inputs["Color"])
                link_to_input(links, sep.outputs["Red"], alpha_input)
                set_transparent_material(material)
                if debug is not None:
                    debug.append(f"[summary-pass] {material.name}: linked alpha mask {alpha_path.name}")
            elif debug is not None:
                debug.append(
                    f"[summary-pass] {material.name}: failed alpha mask {alpha_path.name} "
                    f"image={image is not None} input={alpha_input is not None}"
                )
        elif texture_role(diffuse_path) == "transparent_base":
            image = load_image(diffuse_path)
            alpha_input = node_input(principled, "Alpha")
            if image is not None and alpha_input is not None:
                tex = image_texture_node(nodes, image)
                link_to_input(links, tex.outputs["Alpha"], alpha_input)
                set_transparent_material(material)
                if debug is not None:
                    debug.append(f"[summary-pass] {material.name}: linked base alpha {diffuse_path.name}")
        elif name_implies_alpha(material_name, diffuse_path):
            if not connect_base_texture_alpha(material, principled):
                image = load_image(diffuse_path)
                alpha_input = node_input(principled, "Alpha")
                if image is not None and alpha_input is not None:
                    tex = image_texture_node(nodes, image)
                    link_to_input(links, tex.outputs["Alpha"], alpha_input)
                    set_transparent_material(material)
                    if debug is not None:
                        debug.append(f"[summary-pass] {material.name}: linked implied base alpha {diffuse_path.name}")

        normal_path = first_variant_path(variants, "normal", "normal_or_packed")
        if normal_path is not None:
            image = load_image(normal_path)
            normal_input = node_input(principled, "Normal")
            if image is not None and normal_input is not None:
                image.colorspace_settings.name = "Non-Color"
                tex = image_texture_node(nodes, image)
                normal = nodes.new(type="ShaderNodeNormalMap")
                links.new(tex.outputs["Color"], normal.inputs["Color"])
                link_to_input(links, normal.outputs["Normal"], normal_input)
                if debug is not None:
                    debug.append(f"[summary-pass] {material.name}: linked normal {normal_path.name}")
            elif debug is not None:
                debug.append(
                    f"[summary-pass] {material.name}: failed normal {normal_path.name} "
                    f"image={image is not None} input={normal_input is not None}"
                )


def apply_auxiliary_textures_to_imported_materials(
    imported_names: set[str],
    summary: dict,
    debug: list[str] | None = None,
    apply_styling: bool = True,
) -> None:
    materials = []
    seen = set()
    for object_name in imported_names:
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is not None and material.name not in seen:
                seen.add(material.name)
                materials.append(material)

    available = all_available_texture_paths(summary)
    by_key = build_texture_index(available)
    if debug is not None:
        debug.append(
            f"[post-pass] imported_objects={len(imported_names)} materials={len(materials)} "
            f"available_textures={len(available)} keys={len(by_key)}"
        )
        for key in sorted(by_key)[:200]:
            roles = ", ".join(f"{role}:{path.name}" for role, path in sorted(by_key[key].items()))
            debug.append(f"[texture-key] {key} -> {roles}")
    for material in materials:
        material.use_nodes = True
        tree = material.node_tree
        if tree is None:
            if debug is not None:
                debug.append(f"[post-pass] {material.name}: no node tree")
            continue
        principled = principled_node(tree.nodes)
        if principled is None:
            if debug is not None:
                debug.append(f"[post-pass] {material.name}: no principled node; nodes={[node.type for node in tree.nodes]}")
            continue
        base_path = material_base_image_path(material)
        if base_path is None:
            if debug is not None:
                image_nodes = [
                    node.image.name if node.type == "TEX_IMAGE" and node.image is not None else node.type
                    for node in tree.nodes
                ]
                debug.append(f"[post-pass] {material.name}: no base image path; nodes={image_nodes}")
            continue
        variants = material_variants_from_index(material.name, base_path, by_key)
        keys = material_variant_keys(material.name, base_path)
        if debug is not None:
            debug.append(
                f"[post-pass] {material.name}: base={base_path.name} keys={keys} "
                f"variants={{{', '.join(f'{role}:{path.name}' for role, path in sorted(variants.items()))}}} "
                f"implies_alpha={name_implies_alpha(material.name, base_path)}"
            )
        alpha_path = first_variant_path(variants, "alpha_red")
        if alpha_path is None and name_implies_alpha(material.name, base_path):
            alpha_path = first_variant_path(variants, "mask")
        if alpha_path is not None:
            connect_alpha_mask(material, principled, alpha_path, debug)
        elif name_implies_alpha(material.name, base_path):
            linked = connect_base_texture_alpha(material, principled)
            if debug is not None:
                debug.append(f"[post-pass] {material.name}: fallback base alpha linked={linked}")
        elif debug is not None:
            debug.append(f"[post-pass] {material.name}: no alpha candidate")

        normal_path = first_variant_path(variants, "normal", "normal_or_packed")
        if normal_path is not None:
            connect_normal_map(material, principled, normal_path, debug)
        elif debug is not None:
            debug.append(f"[post-pass] {material.name}: no normal candidate")
        if apply_styling:
            apply_level5_toon_shader(material, base_path, variants, debug)


CHARACTER_PARAMETER_SOCKETS = (
    ("Saturation", "g4_saturation", 1.10, 0.0, 2.0),
    ("Brightness", "g4_brightness", 1.04, 0.0, 2.0),
    ("Light Floor", "g4_light_floor", 0.88, 0.0, 1.0),
    ("Shadow Floor", "g4_shadow_floor", 0.12, 0.0, 1.0),
    ("Normal Strength", "g4_normal_strength", 1.0, 0.0, 2.0),
    ("Specular Strength", "g4_specular_strength", 0.22, 0.0, 2.0),
    ("Wetness", "g4_wetness", 0.0, 0.0, 1.0),
)


def character_parameter_node_group():
    name = "Level-5 Character Parameters"
    group = bpy.data.node_groups.get(name)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    geometry_in = group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    geometry_out = group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    sockets = []
    for label, _, default, minimum, maximum in CHARACTER_PARAMETER_SOCKETS:
        socket = group.interface.new_socket(name=label, in_out="INPUT", socket_type="NodeSocketFloat")
        socket.default_value = default
        socket.min_value = minimum
        socket.max_value = maximum
        sockets.append(socket)

    input_node = group.nodes.new("NodeGroupInput")
    input_node.location = (-520, 0)
    output_node = group.nodes.new("NodeGroupOutput")
    output_node.location = (420, 0)
    geometry = input_node.outputs[geometry_in.identifier]
    for index, ((label, attribute_name, _, _, _), socket) in enumerate(zip(CHARACTER_PARAMETER_SOCKETS, sockets)):
        store = group.nodes.new("GeometryNodeStoreNamedAttribute")
        store.data_type = "FLOAT"
        store.domain = "POINT"
        store.label = label
        store.location = (-300 + index * 100, -index * 90)
        store.inputs["Name"].default_value = attribute_name
        group.links.new(geometry, store.inputs["Geometry"])
        group.links.new(input_node.outputs[socket.identifier], store.inputs["Value"])
        geometry = store.outputs["Geometry"]
    group.links.new(geometry, output_node.inputs[geometry_out.identifier])
    return group


def character_shader_attribute(material, label: str, attribute_name: str):
    nodes = material.node_tree.nodes
    node_name = f"G4 Control {label}"
    node = nodes.get(node_name) or nodes.new("ShaderNodeAttribute")
    node.name = node_name
    node.label = label
    node.attribute_name = attribute_name
    return node.outputs["Fac"]


def connect_character_parameter_material(material) -> None:
    if material.node_tree is None or not material.get("g4_level5_toon"):
        return
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    targets = {
        "Saturation": ("G4 Saturation", "Saturation"),
        "Brightness": ("G4 Saturation", "Value"),
        "Light Floor": ("G4 Toon Ambient", 1),
        "Shadow Floor": ("G4 Toon Ambient", 2),
        "Normal Strength": ("G4 Surface Normal", "Strength"),
        "Specular Strength": ("G4 Specular Strength", 0),
    }
    for label, attribute_name, _, _, _ in CHARACTER_PARAMETER_SOCKETS:
        source = character_shader_attribute(material, label, attribute_name)
        if label == "Wetness":
            wetness = nodes.get("G4 Wetness")
            if wetness is not None:
                for link in tuple(wetness.outputs[0].links):
                    links.new(source, link.to_socket)
            continue
        target_spec = targets.get(label)
        if target_spec is None:
            continue
        node = nodes.get(target_spec[0])
        if node is None:
            continue
        socket = node.inputs.get(target_spec[1]) if isinstance(target_spec[1], str) else node.inputs[target_spec[1]]
        links.new(source, socket)


def configure_character_parameter_modifiers(imported_names: set[str]) -> int:
    group = character_parameter_node_group()
    created = 0
    input_sockets = {
        item.name: item
        for item in group.interface.items_tree
        if getattr(item, "in_out", None) == "INPUT" and item.name != "Geometry"
    }
    for object_name in imported_names:
        obj = bpy.data.objects.get(object_name)
        if obj is None or obj.type != "MESH" or not any(
            slot.material is not None and slot.material.get("g4_level5_toon")
            for slot in obj.material_slots
        ):
            continue
        modifier = obj.modifiers.get("Level-5 Character Parameters")
        if modifier is None:
            modifier = obj.modifiers.new("Level-5 Character Parameters", "NODES")
            created += 1
        modifier.node_group = group
        for label, _, default, _, _ in CHARACTER_PARAMETER_SOCKETS:
            socket = input_sockets.get(label)
            if socket is not None and socket.identifier not in modifier:
                modifier[socket.identifier] = default
        for slot in obj.material_slots:
            if slot.material is not None:
                connect_character_parameter_material(slot.material)
    return created


def configure_environment_cubemap(summary: dict, debug: list[str] | None = None) -> bool:
    candidates = [Path(path) for path in summary.get("textures", []) if texture_role(Path(path)) == "environment"]
    if not candidates:
        return False
    path = next((item for item in candidates if "global" in item.stem.lower()), candidates[0])
    image = load_image(path)
    if image is None:
        if debug is not None:
            debug.append(f"[cubemap] failed to load converted environment {path.name}")
        return False

    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new("Level-5 G4 Environment")
        bpy.context.scene.world = world
    world.use_nodes = True
    tree = world.node_tree
    if tree is None:
        return False
    nodes = tree.nodes
    links = tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputWorld")
    output.location = (520, 0)
    background = nodes.new("ShaderNodeBackground")
    background.location = (260, 0)
    background.inputs["Strength"].default_value = 0.35
    environment = nodes.new("ShaderNodeTexEnvironment")
    environment.name = "G4 Environment Cubemap"
    environment.label = path.name
    environment.image = image
    environment.projection = "EQUIRECTANGULAR"
    links.new(environment.outputs["Color"], background.inputs["Color"])
    links.new(background.outputs["Background"], output.inputs["Surface"])
    world["g4_environment_cubemap"] = str(path)
    if debug is not None:
        debug.append(f"[cubemap] world environment={path.name} strength=0.35")
    return True


def configure_character_color_management(debug: list[str] | None = None) -> None:
    view = bpy.context.scene.view_settings
    view.view_transform = "Standard"
    view.look = "None"
    view.exposure = 0.0
    view.gamma = 1.0
    if debug is not None:
        debug.append("[color] character display transform=Standard exposure=0 gamma=1")


def mark_level5_internal_edges(obj, angle: float = math.radians(48.0)) -> int:
    """Mark authored hard folds without enabling Freestyle on every triangle."""
    mesh = obj.data
    finger_group = re.compile(r"^[lr]_(?:thb|idx|mid|rng|pky)", re.IGNORECASE)

    def belongs_to_finger(vertex) -> bool:
        return any(
            item.group < len(obj.vertex_groups)
            and finger_group.match(obj.vertex_groups[item.group].name)
            for item in vertex.groups
        )

    polygons_by_edge: dict[tuple[int, int], list] = {}
    for polygon in mesh.polygons:
        for edge_key in polygon.edge_keys:
            polygons_by_edge.setdefault(tuple(sorted(edge_key)), []).append(polygon)

    marked = 0
    for edge in mesh.edges:
        edge.use_freestyle_mark = False
        polygons = polygons_by_edge.get(tuple(sorted(edge.key)), ())
        if len(polygons) == 1 and all(
            belongs_to_finger(mesh.vertices[index]) for index in edge.vertices
        ):
            # Fingernails are authored as open inset surfaces in several
            # character bodies. Their perimeter is a source detail line, not
            # a hull, so preserve it as an explicit non-destructive edge mark.
            edge.use_freestyle_mark = True
            marked += 1
            continue
        if len(polygons) != 2:
            continue
        first, second = polygons
        material_seam = first.material_index != second.material_index
        hard_fold = first.normal.angle(second.normal) >= angle
        start, end = (mesh.vertices[index].co for index in edge.vertices)
        direction = obj.matrix_world.to_3x3() @ (end - start)
        vertical_fold = direction.length > 1e-8 and abs(direction.normalized().z) >= 0.55
        authored_finger_fold = hard_fold and all(
            belongs_to_finger(mesh.vertices[index]) for index in edge.vertices
        )
        if authored_finger_fold or vertical_fold and (material_seam or hard_fold):
            edge.use_freestyle_mark = True
            marked += 1
    return marked


def configure_level5_outlines(
    imported_names: set[str],
    detailed: bool = False,
    debug: list[str] | None = None,
) -> bool:
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    try:
        scene.render.use_freestyle = True
        outline_thickness = max(0.25, float(getattr(addon_preferences(), "outline_thickness", 1.65)))
        settings = view_layer.freestyle_settings
        for stale in tuple(settings.linesets):
            if stale.linestyle is None or stale.name == "LineSet":
                settings.linesets.remove(stale)
        line_set = settings.linesets.get("Level-5 G4 Outline")
        if line_set is None or line_set.linestyle is None:
            if line_set is not None:
                settings.linesets.remove(line_set)
            bpy.ops.scene.freestyle_lineset_add()
            line_set = settings.linesets.active
            line_set.name = "Level-5 G4 Outline"
        line_style = line_set.linestyle
        line_style.name = "Level-5 G4 Outline"
        source_collection = bpy.data.collections.get("Level-5 G4 Outline Sources")
        if source_collection is None:
            source_collection = bpy.data.collections.new("Level-5 G4 Outline Sources")
            scene.collection.children.link(source_collection)
        thin_collection = bpy.data.collections.get("Level-5 G4 Thin Outline Sources")
        if thin_collection is None:
            thin_collection = bpy.data.collections.new("Level-5 G4 Thin Outline Sources")
            scene.collection.children.link(thin_collection)
        detail_collection = bpy.data.collections.get("Level-5 G4 Internal Detail Sources")
        if detail_collection is None:
            detail_collection = bpy.data.collections.new("Level-5 G4 Internal Detail Sources")
            scene.collection.children.link(detail_collection)
        excluded = re.compile(r"(?:^|_)(?:eye|mouth|teeth|tongue|pupil|eyelash)(?:_|$)", re.IGNORECASE)
        marked_edges = 0
        for name in imported_names:
            obj = bpy.data.objects.get(name)
            if obj is None or obj.type != "MESH" or excluded.search(obj.name):
                continue
            attribute = obj.data.color_attributes.get("G4 Outline Parameters")
            blue = (
                sum(value.color[2] for value in attribute.data) / len(attribute.data)
                if attribute is not None and attribute.data
                else 0.5
            )
            has_line_map = any(
                slot.material is not None and slot.material.get("g4_line_informative", False)
                for slot in obj.material_slots
            )
            collection = thin_collection if blue < 0.35 or has_line_map else source_collection
            if collection.objects.get(obj.name) is None:
                collection.objects.link(obj)
            if detail_collection.objects.get(obj.name) is None:
                detail_collection.objects.link(obj)
            if detailed:
                marked_edges += mark_level5_internal_edges(obj)
        line_set.select_by_collection = True
        line_set.select_by_edge_types = True
        line_set.collection = source_collection
        line_set.collection_negation = "INCLUSIVE"
        line_set.select_silhouette = False
        line_set.select_border = False
        line_set.select_crease = False
        line_set.select_material_boundary = False
        line_set.select_contour = True
        line_set.edge_type_combination = "OR"
        settings.crease_angle = math.radians(55.0)
        line_style.color = (0.018, 0.012, 0.018)
        line_style.thickness = outline_thickness
        line_style.caps = "ROUND"

        thin_set = settings.linesets.get("Level-5 G4 Thin Outline")
        if thin_collection.objects:
            if thin_set is None or thin_set.linestyle is None:
                if thin_set is not None:
                    settings.linesets.remove(thin_set)
                bpy.ops.scene.freestyle_lineset_add()
                thin_set = settings.linesets.active
                thin_set.name = "Level-5 G4 Thin Outline"
            thin_style = thin_set.linestyle
            thin_style.name = "Level-5 G4 Thin Outline"
            thin_set.select_by_collection = True
            thin_set.select_by_edge_types = True
            thin_set.collection = thin_collection
            thin_set.collection_negation = "INCLUSIVE"
            thin_set.select_silhouette = False
            thin_set.select_border = False
            thin_set.select_crease = False
            thin_set.select_material_boundary = False
            thin_set.select_contour = True
            thin_set.edge_type_combination = "OR"
            thin_style.color = (0.025, 0.016, 0.021)
            thin_style.thickness = outline_thickness * 0.70
            thin_style.caps = "ROUND"
        elif thin_set is not None:
            settings.linesets.remove(thin_set)

        detail_set = settings.linesets.get("Level-5 G4 Internal Detail")
        if marked_edges:
            if detail_set is None or detail_set.linestyle is None:
                if detail_set is not None:
                    settings.linesets.remove(detail_set)
                bpy.ops.scene.freestyle_lineset_add()
                detail_set = settings.linesets.active
                detail_set.name = "Level-5 G4 Internal Detail"
            detail_style = detail_set.linestyle
            detail_style.name = "Level-5 G4 Internal Detail"
            detail_set.select_by_collection = True
            detail_set.select_by_edge_types = True
            detail_set.collection = detail_collection
            detail_set.collection_negation = "INCLUSIVE"
            detail_set.select_silhouette = False
            detail_set.select_border = False
            detail_set.select_crease = False
            detail_set.select_material_boundary = False
            detail_set.select_contour = False
            detail_set.select_edge_mark = True
            detail_set.edge_type_combination = "OR"
            detail_style.color = (0.055, 0.025, 0.028)
            detail_style.thickness = outline_thickness * 0.44
            detail_style.caps = "ROUND"
        elif detail_set is not None:
            settings.linesets.remove(detail_set)
    except (AttributeError, RuntimeError, TypeError):
        if debug is not None:
            debug.append("[outline] Freestyle is unavailable for the active render configuration")
        return False
    if debug is not None:
        mode = "filtered silhouette with viewport depth/normal detail" if detailed else "filtered silhouette"
        debug.append(f"[outline] render lines use {mode}; marked internal edges={marked_edges}")
    return True


def refresh_existing_level5_outlines(mode: str | None = None) -> bool:
    view_layer = bpy.context.view_layer
    scene = bpy.context.scene
    if view_layer is None or scene is None:
        return False
    settings = getattr(view_layer, "freestyle_settings", None)
    if settings is None:
        return False
    managed = {
        "Level-5 G4 Outline",
        "Level-5 G4 Thin Outline",
        "Level-5 G4 Internal Detail",
    }
    if mode is None:
        mode = getattr(addon_preferences(), "outline_mode", "SIMPLE")
    has_level5_lines = any(line_set.name in managed for line_set in settings.linesets)
    if not has_level5_lines:
        if mode == "OFF":
            return False
        names = {
            obj.name for obj in bpy.data.objects
            if obj.type == "MESH" and obj.get("g4_viewport_outline") == "SCREEN_SPACE"
        }
        return bool(names) and configure_level5_outlines(names, detailed=mode == "HULL")
    if mode == "OFF":
        for line_set in tuple(settings.linesets):
            if line_set.name in managed:
                settings.linesets.remove(line_set)
        if not settings.linesets:
            scene.render.use_freestyle = False
        return True

    names: set[str] = set()
    for collection_name in (
        "Level-5 G4 Outline Sources",
        "Level-5 G4 Thin Outline Sources",
        "Level-5 G4 Internal Detail Sources",
    ):
        collection = bpy.data.collections.get(collection_name)
        if collection is not None:
            names.update(obj.name for obj in collection.objects if obj.type == "MESH")
    if not names:
        return False
    return configure_level5_outlines(names, detailed=mode == "HULL")


@persistent
def refresh_level5_outlines_on_load(_unused) -> None:
    refresh_existing_level5_outlines()


def configure_viewport_outlines(
    imported_names: set[str],
    detailed: bool = False,
    debug: list[str] | None = None,
) -> int:
    """Enable Blender's screen-space object outline without duplicating geometry."""
    for name in imported_names:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "MESH":
            obj["g4_viewport_outline"] = "SCREEN_SPACE"

    configured = 0
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                space.shading.show_object_outline = True
                space.shading.object_outline_color = (0.018, 0.012, 0.018)
                if detailed:
                    space.shading.show_cavity = True
                    space.shading.cavity_type = "BOTH"
                    space.shading.cavity_ridge_factor = 1.0
                    space.shading.cavity_valley_factor = 1.0
                configured += 1
    if debug is not None:
        detail = " with cavity detail" if detailed else ""
        debug.append(f"[outline] non-destructive viewport spaces={configured}{detail}")
    return configured


def preserve_outline_vertex_parameters(imported_names: set[str], debug: list[str] | None = None) -> int:
    preserved = 0
    for name in imported_names:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH" or not obj.data.color_attributes:
            continue
        attribute = obj.data.color_attributes.active_color or obj.data.color_attributes[0]
        attribute.name = "G4 Outline Parameters"
        obj["g4_outline_vertex_parameters"] = attribute.name
        preserved += 1
    if debug is not None:
        debug.append(f"[outline] preserved vertex parameter meshes={preserved}")
    return preserved


def pack_images(paths_before: set[str], texture_paths: set[str], enabled: bool) -> None:
    if not enabled:
        return
    for image in bpy.data.images:
        filepath = bpy.path.abspath(image.filepath) if image.filepath else ""
        filepath = str(Path(filepath).resolve()) if filepath else ""
        if filepath in paths_before and filepath not in texture_paths:
            continue
        if not filepath or not Path(filepath).exists():
            continue
        try:
            if not image.has_data:
                image.reload()
            image.pack()
        except RuntimeError:
            continue


def cleanup_generated_files(summary: dict, enabled: bool) -> None:
    if not enabled:
        return
    generated = [Path(value) for key in ("dae", "native_mesh", "report") if (value := summary.get(key))]
    for file_path in generated:
        if file_path.is_file():
            file_path.unlink()

    texture_dirs = {Path(path).parent for path in summary.get("textures", [])}
    for directory in sorted(texture_dirs, key=lambda item: len(item.parts), reverse=True):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)


def discard_secondary_lods(imported_names: set[str]) -> int:
    removed = 0
    for object_name in tuple(imported_names):
        obj = bpy.data.objects.get(object_name)
        if obj is None or obj.type != "MESH" or not re.search(r"_LOD[1-9](?:\.\d+)?$", obj.name, re.IGNORECASE):
            continue
        imported_names.discard(object_name)
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1
    return removed


def import_g4_model(
    path: Path,
    prefs: G4ImporterPreferences,
    create_report_text: bool,
    target_armature=None,
    apply_styling: bool | None = None,
) -> tuple[dict, set[str]]:
    if apply_styling is None:
        apply_styling = is_character_model(path)
    debug = [f"[import] input={path}"]
    if target_armature is not None and target_armature.type == "ARMATURE":
        debug.append(
            f"[target-armature] name={target_armature.name} "
            f"bones={len(target_armature.data.bones)} "
            f"first={list(target_armature.data.bones.keys())[:16]}"
        )
    images_before = image_paths()
    summary = run_exporter(str(path), prefs, target_armature)
    debug.append(f"[exporter] resolved={summary.get('resolved_model')}")
    debug.append(f"[exporter] native_mesh={summary.get('native_mesh')}")
    debug.append(f"[exporter] skeleton_source={summary.get('skeleton_source')}")
    debug.append(f"[exporter] textures={len(summary.get('textures', []))} materials={len(summary.get('materials', {}))}")
    debug.append(f"[exporter] texture_sources={summary.get('texture_sources', [])}")
    native_value = summary.get("native_mesh")
    native_path = Path(native_value).resolve() if native_value else None
    if native_path is not None and native_path.is_file():
        imported_names = import_native_g4_mesh(native_path)
        import_method = "native"
    else:
        dae_path = Path(summary.get("dae", "")).resolve()
        collada_import = getattr(bpy.ops.wm, "collada_import", None)
        if not dae_path.exists() or collada_import is None:
            raise RuntimeError("Native G4 mesh payload was not generated and Collada fallback is unavailable")
        imported_names = import_collada(dae_path)
        import_method = "collada_fallback"
    preserve_outline_vertex_parameters(imported_names, debug)
    removed_lods = discard_secondary_lods(imported_names)
    debug.append(f"[geometry] method={import_method} imported_objects={sorted(imported_names)}")
    debug.append(f"[geometry] secondary_lods_removed={removed_lods}")
    orientation = apply_g4_bone_orientation(
        imported_names,
        summary,
        getattr(prefs, "apply_bone_orientation", True),
    )
    summary["bone_orientation_import"] = orientation
    debug.append(f"[armature] bone_orientation={orientation}")
    compact_skeleton_summary(summary)
    collapse_duplicate_materials(imported_names)
    apply_material_texture_variants(summary, debug)
    outline_mode = getattr(prefs, "outline_mode", "SIMPLE") if apply_styling else "OFF"
    apply_auxiliary_textures_to_imported_materials(
        imported_names,
        summary,
        debug,
        apply_styling=apply_styling,
    )
    if apply_styling:
        modifier_count = configure_character_parameter_modifiers(imported_names)
        debug.append(f"[character-controls] geometry-node modifiers={modifier_count}")
    if apply_styling:
        configure_character_color_management(debug)
        if outline_mode != "OFF":
            configure_level5_outlines(imported_names, outline_mode == "HULL", debug)
            configure_viewport_outlines(imported_names, outline_mode == "HULL", debug)
        if outline_mode == "HULL":
            debug.append("[outline] detailed render and viewport cavity edges enabled")
        elif outline_mode == "SIMPLE":
            debug.append("[outline] simple viewport silhouette; geometry left unchanged")
        else:
            debug.append("[outline] disabled")
    else:
        debug.append("[styling] using classic material mapping without toon shader or outlines")
    configure_environment_cubemap(summary, debug)
    texture_paths = {str(Path(path).resolve()) for path in summary.get("textures", [])}
    pack_images(images_before, texture_paths, getattr(prefs, "pack_imported_textures", True))
    write_debug_log(summary, debug)
    if create_report_text:
        make_report_text(summary)
        make_debug_text(summary, debug)
    cleanup_generated_files(summary, getattr(prefs, "cleanup_import_cache", True))
    return summary, imported_names


def model_data_roots(path: Path, prefs: G4ImporterPreferences) -> list[Path]:
    roots = []
    configured = bpy.path.abspath(getattr(prefs, "raw_data_root", "") or "")
    if configured:
        roots.append(Path(configured))
    for parent in path.parents:
        if parent.name == "data" and parent.parent.name in {"raw", "readable"}:
            roots.append(parent)
            work_root = parent.parent.parent
            roots.extend((work_root / "raw" / "data", work_root / "readable" / "data"))
            break
    return list(dict.fromkeys(root.resolve() for root in roots if root.is_dir()))


def load_model_lookup(prefs: G4ImporterPreferences) -> dict:
    lookup_path = Path(bpy.path.abspath(getattr(prefs, "chara_model_lookup", "") or ""))
    if not lookup_path.is_file():
        return {}
    try:
        return json.loads(lookup_path.read_text(encoding="utf-8")).get("models") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def body_stem_from_row(row: dict | None) -> str | None:
    body_path = str((row or {}).get("body_path") or "").replace("\\", "/")
    match = re.search(r"(?:^|/)(c\d{6,8})(?:\.objbin)?$", body_path, re.IGNORECASE)
    return match.group(1).lower() if match else None


def lookup_body_stem_for_identifier(identifier: str, models: dict) -> str | None:
    identifier = identifier.lower()
    rows = (
        row
        for model_path, row in models.items()
        if Path(model_path).stem.lower() == identifier
    )
    return next((stem for row in rows if (stem := body_stem_from_row(row))), None)


def lookup_body_stem(path: Path, prefs: G4ImporterPreferences, models: dict | None = None) -> str | None:
    models = load_model_lookup(prefs) if models is None else models
    relative = None
    parts = path.parts
    for index in range(len(parts) - 2):
        if parts[index:index + 2] == ("common", "chr"):
            relative = Path(*parts[index + 2:]).as_posix()
            break
    row = None
    if relative:
        row = models.get(relative)
        if row is None:
            row = models.get(Path(relative).with_suffix(".objbin").as_posix())
    return body_stem_from_row(row)


def find_character_part(
    path: Path,
    prefix: str,
    prefs: G4ImporterPreferences,
    character_part_stem: str = "",
) -> Path | None:
    match = re.fullmatch(r"c(\d{6,8})", path.stem, re.IGNORECASE)
    if match is None:
        return None
    stems = []
    models = load_model_lookup(prefs)
    override = re.fullmatch(r"c(\d{6,8})", character_part_stem, re.IGNORECASE)
    if override:
        override_body = lookup_body_stem_for_identifier(override.group(0), models)
        if override_body:
            stems.append(f"{prefix}{override_body[1:]}")
        stems.append(f"{prefix}{override.group(1)}")
    stems.append(f"{prefix}{match.group(1)}")
    body_stem = lookup_body_stem(path, prefs, models)
    if body_stem:
        stems.append(f"{prefix}{body_stem[1:]}")
    for data_root in model_data_roots(path, prefs):
        uniform_root = data_root / "common" / "chr" / "_uniform"
        for stem in dict.fromkeys(stems):
            for extension in (".g4pkm", ".g4md"):
                candidate = uniform_root / stem / f"{stem}{extension}"
                if candidate.is_file():
                    return candidate
    return None


def attach_part_to_armature(
    path: Path,
    target_armature,
    prefs: G4ImporterPreferences,
    create_report_text: bool,
    preserve_part_armatures: bool = False,
) -> int:
    _, imported_names = import_g4_model(
        path,
        prefs,
        create_report_text,
        target_armature=target_armature,
    )
    imported_objects = [bpy.data.objects.get(name) for name in imported_names]
    imported_objects = [obj for obj in imported_objects if obj is not None]
    part_armatures = {obj for obj in imported_objects if obj.type == "ARMATURE" and obj != target_armature}

    attached = 0
    for obj in imported_objects:
        if obj.type != "MESH":
            continue
        armature_modifiers = [modifier for modifier in obj.modifiers if modifier.type == "ARMATURE"]
        if not armature_modifiers:
            raise RuntimeError(f"Character part mesh has no armature modifier: {path}::{obj.name}")
        # The Collada import may create a private skeleton for a part even
        # when a target rig was supplied.  Parts must never retain that
        # skeleton: its matching bone names otherwise make the Outliner look
        # plausible while the meshes ignore the actor's animated rig.
        for modifier in armature_modifiers:
            modifier.object = target_armature
        if obj.parent != target_armature:
            obj.parent = target_armature
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_basis = Matrix.Identity(4)
        obj["g4_character_part_source"] = str(path)
        obj["g4_character_part_group_remaps"] = 0
        obj["g4_character_part_actor"] = target_armature.name
        obj["g4_character_part_preserve_armature"] = False
        attached += 1

    for source_armature in part_armatures:
        bpy.data.objects.remove(source_armature, do_unlink=True)
    return attached


def find_accessory_part_for_body(body_path: Path) -> Path | None:
    match = re.fullmatch(r"u(\d{6,8})", body_path.stem, re.IGNORECASE)
    if match is None:
        return None
    stem = f"sk{match.group(1)}"
    uniform_root = body_path.parent.parent
    for extension in (".g4pkm", ".g4md"):
        candidate = uniform_root / stem / f"{stem}{extension}"
        if candidate.is_file():
            return candidate
    return None


def find_default_ball_model(model_path: Path, prefs: G4ImporterPreferences) -> Path | None:
    for data_root in model_data_roots(model_path, prefs):
        ball_root = data_root / "common" / "chr" / "b000001"
        for extension in (".g4pkm", ".g4md"):
            candidate = ball_root / f"b000001{extension}"
            if candidate.is_file():
                return candidate
    return None


def attach_ball_to_armature(
    path: Path,
    target_armature,
    prefs: G4ImporterPreferences,
    create_report_text: bool,
) -> int:
    ball_bone = next(
        (name for name in ("ball", "c_ball_1_0") if target_armature.pose.bones.get(name) is not None),
        None,
    )
    if ball_bone is None:
        raise RuntimeError(f"Target rig has no ball bone: {target_armature.name}")

    _, imported_names = import_g4_model(path, prefs, create_report_text, apply_styling=False)
    imported_objects = [bpy.data.objects.get(name) for name in imported_names]
    imported_objects = [obj for obj in imported_objects if obj is not None]
    source_armatures = {obj for obj in imported_objects if obj.type == "ARMATURE"}
    bone_matrix = target_armature.matrix_world @ target_armature.pose.bones[ball_bone].matrix
    attached = 0
    for obj in imported_objects:
        if obj.type != "MESH":
            continue
        world_matrix = obj.matrix_world.copy()
        for modifier in tuple(obj.modifiers):
            if modifier.type == "ARMATURE":
                obj.modifiers.remove(modifier)
        obj.parent = target_armature
        obj.parent_type = "BONE"
        obj.parent_bone = ball_bone
        obj.matrix_parent_inverse = bone_matrix.inverted_safe()
        obj.matrix_world = world_matrix
        obj["g4_ball_source"] = str(path)
        obj["g4_ball_bone"] = ball_bone
        attached += 1
    for source_armature in source_armatures:
        bpy.data.objects.remove(source_armature, do_unlink=True)
    return attached


def import_character_parts_for_armature(
    model_path: Path,
    target_armature,
    prefs: G4ImporterPreferences,
    automatic: bool,
    body_path: str,
    shoes_path: str,
    accessory_path: str,
    gloves_path: str,
    armband_path: str,
    nameplate_path: str,
    create_report_text: bool,
    character_part_stem: str = "",
    preserve_part_armatures: bool = False,
) -> tuple[int, list[Path]]:
    body = Path(bpy.path.abspath(body_path)) if body_path else find_character_part(
        model_path, "u", prefs, character_part_stem
    )
    shoes = Path(bpy.path.abspath(shoes_path)) if shoes_path else find_character_part(
        model_path, "s", prefs, character_part_stem
    )
    accessory = Path(bpy.path.abspath(accessory_path)) if accessory_path else None
    if accessory is None and body is not None:
        accessory = find_accessory_part_for_body(body)
    paths = [
        body,
        shoes,
        accessory,
        *(Path(bpy.path.abspath(value)) for value in (gloves_path, armband_path, nameplate_path) if value),
    ]
    selected = []
    for path in paths:
        if path is None:
            continue
        if not path.is_file() or path.suffix.lower() not in MODEL_EXTENSIONS:
            raise RuntimeError(f"Character part not found or unsupported: {path}")
        if path not in selected:
            selected.append(path)
    attached = sum(
        attach_part_to_armature(
            path,
            target_armature,
            prefs,
            create_report_text,
            preserve_part_armatures=preserve_part_armatures,
        )
        for path in selected
    )
    return attached, selected


def saved_character_import_parts(prefs) -> dict:
    try:
        value = json.loads(getattr(prefs, "character_import_parts", "{}") or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


class IMPORT_OT_level5_g4_character_setup(Operator):
    """Collect character cosmetics once, after the primary file picker."""

    bl_idname = "import_scene.level5_g4_character_setup"
    bl_label = "Character Parts"
    bl_options = {"REGISTER", "UNDO"}

    model_path: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    animation_path: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    animation_settings_json: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    create_report_text: BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    body_model: StringProperty(name="Body", subtype="FILE_PATH")
    shoes_model: StringProperty(name="Shoes", subtype="FILE_PATH")
    accessory_model: StringProperty(name="Sleeves / Collar", subtype="FILE_PATH")
    gloves_model: StringProperty(name="Gloves", subtype="FILE_PATH")
    armband_model: StringProperty(name="Captain Armband", subtype="FILE_PATH")
    nameplate_model: StringProperty(name="Nameplate", subtype="FILE_PATH")
    attach_ball: BoolProperty(name="Attach Ball")
    ball_model: StringProperty(name="Ball Model", subtype="FILE_PATH")

    def invoke(self, context, event):
        saved = saved_character_import_parts(addon_preferences())
        for key in (
            "body_model", "shoes_model", "accessory_model", "gloves_model",
            "armband_model", "nameplate_model", "ball_model",
        ):
            if not getattr(self, key):
                setattr(self, key, str(saved.get(key) or ""))
        self.attach_ball = bool(saved.get("attach_ball", self.attach_ball))
        return context.window_manager.invoke_props_dialog(self, width=660)

    def draw(self, context):
        layout = self.layout
        layout.label(text=f"Rig: {Path(self.model_path).name}", icon="ARMATURE_DATA")
        layout.label(text="Empty Body/Shoes fields use the matching parts automatically.")
        layout.prop(self, "body_model")
        layout.prop(self, "shoes_model")
        layout.prop(self, "accessory_model")
        layout.prop(self, "gloves_model")
        layout.prop(self, "armband_model")
        layout.prop(self, "nameplate_model")
        layout.prop(self, "attach_ball")
        if self.attach_ball:
            layout.prop(self, "ball_model")

    def execute(self, context):
        model_path = Path(bpy.path.abspath(self.model_path))
        if not model_path.is_file() or model_path.suffix.lower() not in MODEL_EXTENSIONS:
            self.report({"ERROR"}, f"Character model not found or unsupported: {model_path}")
            return {"CANCELLED"}

        values = {
            key: bpy.path.abspath(getattr(self, key)) if getattr(self, key) else ""
            for key in (
                "body_model", "shoes_model", "accessory_model", "gloves_model",
                "armband_model", "nameplate_model", "ball_model",
            )
        }
        values["attach_ball"] = self.attach_ball
        addon_preferences().character_import_parts = json.dumps(values, sort_keys=True)

        if self.animation_path:
            settings = json.loads(self.animation_settings_json or "{}")
            result = bpy.ops.import_scene.level5_g4mt(
                "EXEC_DEFAULT",
                filepath=self.animation_path,
                model_path=str(model_path),
                import_model=True,
                import_character_parts=True,
                prompt_for_models=False,
                **values,
                **settings,
            )
        else:
            result = bpy.ops.import_scene.level5_g4(
                "EXEC_DEFAULT",
                filepath=str(model_path),
                create_report_text=self.create_report_text,
                import_character_parts=True,
                character_setup_complete=True,
                **values,
            )
        return result


class IMPORT_OT_level5_g4(Operator, ImportHelper):
    bl_idname = "import_scene.level5_g4"
    bl_label = "Import Level-5 G4 Model"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".g4md"
    filter_glob: StringProperty(
        default="*.g4md;*.g4pkm",
        options={"HIDDEN"},
    )
    files: CollectionProperty(
        type=OperatorFileListElement,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    directory: StringProperty(
        subtype="DIR_PATH",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    create_report_text: BoolProperty(
        name="Create Report Text",
        default=True,
        description="Create a Blender text block with the exporter summary",
    )
    import_character_parts: BoolProperty(
        name="Import Character Parts",
        default=True,
        description="Attach selected uniform, shoes and optional character accessories to the character rig",
    )
    auto_character_parts: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    character_setup_complete: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    body_model: StringProperty(
        name="Body Model",
        description="Optional u*.g4md/.g4pkm override; empty detects the matching body automatically",
    )
    shoes_model: StringProperty(
        name="Shoes Model",
        description="Optional s*.g4md/.g4pkm override; empty detects matching shoes automatically",
    )
    accessory_model: StringProperty(
        name="Arms / Neck",
        description="Optional sk*.g4md/.g4pkm part; matching sk* is attached automatically for a selected u* body",
    )
    gloves_model: StringProperty(name="Gloves Model", description="Optional g*.g4md/.g4pkm gloves part")
    armband_model: StringProperty(name="Captain Armband Model", description="Optional m*.g4md/.g4pkm armband part")
    nameplate_model: StringProperty(name="Nameplate Model", description="Optional n*.g4md/.g4pkm nameplate part")
    attach_ball: BoolProperty(name="Attach Ball", default=False)
    ball_model: StringProperty(name="Ball Model", description="Optional b000001.g4pkm override")
    character_part_stem: StringProperty(
        options={"HIDDEN", "SKIP_SAVE"},
    )
    preserve_character_part_armatures: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    def invoke(self, context, event):
        self.auto_character_parts = False
        self.character_setup_complete = False
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "create_report_text")

    def execute(self, context):
        base_path = Path(self.filepath)
        if self.files:
            directory = Path(getattr(self, "directory", "") or base_path.parent)
            paths = [directory / item.name for item in self.files if Path(item.name).suffix.lower() in MODEL_EXTENSIONS]
        else:
            paths = [base_path]

        unsupported = [path for path in paths if path.suffix.lower() not in MODEL_EXTENSIONS]
        if unsupported:
            suffixes = ", ".join(sorted({path.suffix or "<none>" for path in unsupported}))
            self.report({"ERROR"}, f"Unsupported G4 model extension: {suffixes}")
            return {"CANCELLED"}
        if not paths:
            self.report({"ERROR"}, "No supported G4 model files selected")
            return {"CANCELLED"}

        if (
            not self.character_setup_complete
            and len(paths) == 1
            and re.fullmatch(r"c\d{6,8}", paths[0].stem, re.IGNORECASE)
        ):
            selected_path = str(paths[0])
            create_report_text = self.create_report_text
            bpy.app.timers.register(
                lambda: bpy.ops.import_scene.level5_g4_character_setup(
                    "INVOKE_DEFAULT",
                    model_path=selected_path,
                    create_report_text=create_report_text,
                ) and None,
                first_interval=0.01,
            )
            return {"FINISHED"}

        prefs = addon_preferences()
        imported_total = 0
        part_mesh_total = 0
        imported_parts = []
        summaries = []
        try:
            for path in paths:
                summary, imported_names = import_g4_model(path, prefs, self.create_report_text)
                summaries.append(summary)
                imported_total += len(imported_names)
                armatures = imported_armatures(imported_names)
                if self.import_character_parts and armatures and re.fullmatch(r"c\d{6,8}", path.stem, re.IGNORECASE):
                    attached, part_paths = import_character_parts_for_armature(
                        path,
                        armatures[0],
                        prefs,
                        self.auto_character_parts,
                        self.body_model,
                        self.shoes_model,
                        self.accessory_model,
                        self.gloves_model,
                        self.armband_model,
                        self.nameplate_model,
                        self.create_report_text,
                        self.character_part_stem,
                        self.preserve_character_part_armatures,
                    )
                    part_mesh_total += attached
                    imported_parts.extend(part_paths)
                    if self.attach_ball:
                        ball_path = Path(bpy.path.abspath(self.ball_model)) if self.ball_model else find_default_ball_model(path, prefs)
                        if ball_path is None:
                            raise RuntimeError("Default ball model b000001 was not found")
                        part_mesh_total += attach_ball_to_armature(ball_path, armatures[0], prefs, self.create_report_text)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        summary = summaries[-1]
        materials = summary.get("material_textures", {})
        hashes = summary.get("texture_hashes", {})
        missing = materials.get("missing") or []
        if len(summaries) == 1:
            message = (
                f"Imported {Path(summary['resolved_model']).name}: "
                f"{imported_total} objects, "
                f"materials {materials.get('resolved', 0)}/{materials.get('total', 0)}, "
                f"hashes {hashes.get('resolved', 0)}/{hashes.get('total', 0)}"
            )
        else:
            message = f"Imported {len(summaries)} G4 models: {imported_total} objects"
        if missing:
            message += f"; missing: {', '.join(missing[:4])}"
        if imported_parts:
            message += f"; parts {len(imported_parts)} ({part_mesh_total} meshes)"
        self.report({"INFO"}, message)
        return {"FINISHED"}


def collect_model_paths(directory: Path, recursive: bool) -> list[Path]:
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    candidates = sorted(
        path
        for path in iterator
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS
    )
    by_folder_stem: dict[tuple[Path, str], Path] = {}
    for path in candidates:
        key = (path.parent, path.stem)
        previous = by_folder_stem.get(key)
        if previous is None or (previous.suffix.lower() == ".g4md" and path.suffix.lower() == ".g4pkm"):
            by_folder_stem[key] = path
    return sorted(by_folder_stem.values(), key=lambda item: item.as_posix())


def collect_model_paths_auto(directory: Path, recursive: bool) -> list[Path]:
    paths = collect_model_paths(directory, recursive)
    if paths or recursive:
        return paths
    return collect_model_paths(directory, True)


def is_character_model(path: Path) -> bool:
    parents = {part.lower() for part in path.parent.parts}
    if "map" in parents:
        return False
    return "chr" in parents or path.stem.lower().startswith("ei")


def auto_hide_map_parts(imported_names: set[str]) -> int:
    hidden = 0
    for object_name in imported_names:
        obj = bpy.data.objects.get(object_name)
        if obj is None or not any(
            token in obj.name.lower()
            for token in ("sdw", "shadow", "culling", "lv1", "lv2")
        ):
            continue
        obj.hide_viewport = True
        obj.hide_render = True
        obj.hide_set(True)
        obj["g4_map_auto_hidden"] = True
        hidden += 1
    return hidden


G4_TO_BLENDER = Matrix(
    (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, -1.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
)


def placement_matrix(values: list[float]) -> Matrix:
    native = Matrix(tuple(tuple(values[row * 4 : row * 4 + 4]) for row in range(4)))
    return G4_TO_BLENDER @ native @ G4_TO_BLENDER.inverted()


def duplicate_imported_objects(objects: list[bpy.types.Object]) -> list[bpy.types.Object]:
    copies = {source: source.copy() for source in objects}
    for source, duplicate in copies.items():
        for collection in source.users_collection:
            collection.objects.link(duplicate)
        if source.parent in copies:
            duplicate.parent = copies[source.parent]
        for modifier in duplicate.modifiers:
            if hasattr(modifier, "object") and modifier.object in copies:
                modifier.object = copies[modifier.object]
        for constraint in duplicate.constraints:
            if hasattr(constraint, "target") and constraint.target in copies:
                constraint.target = copies[constraint.target]
    return list(copies.values())


def apply_map_placements(
    source_path: Path,
    imported_names: set[str],
    placements: list[dict],
) -> int:
    if not placements:
        return 0
    source_objects = [bpy.data.objects.get(name) for name in imported_names]
    source_objects = [obj for obj in source_objects if obj is not None]
    placed = 0
    for placement_index, placement in enumerate(placements):
        objects = source_objects if placement_index == 0 else duplicate_imported_objects(source_objects)
        object_set = set(objects)
        matrix = placement_matrix(placement["matrix"])
        for obj in objects:
            if obj.parent not in object_set:
                obj.matrix_world = matrix @ obj.matrix_world
            obj["g4_map_asset"] = source_path.stem
            obj["g4_map_node"] = placement["node_name"]
            obj["g4_map_node_index"] = placement["node_index"]
        placed += 1
    return placed


class IMPORT_OT_level5_g4_folder(Operator):
    bl_idname = "import_scene.level5_g4_folder"
    bl_label = "Import Level-5 G4 Model Folder"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(
        name="Folder",
        subtype="DIR_PATH",
        options={"SKIP_SAVE"},
    )
    recursive: BoolProperty(
        name="Recursive",
        default=True,
        description="Import models from subfolders too",
    )
    create_report_text: BoolProperty(
        name="Create Report Text",
        default=True,
        description="Create Blender text blocks with exporter summaries",
    )
    import_character_parts: BoolProperty(
        name="Import Character Parts",
        default=False,
    )
    auto_character_parts: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    body_model: StringProperty(name="Body Model")
    shoes_model: StringProperty(name="Shoes Model")
    accessory_model: StringProperty(name="Arms / Neck")
    gloves_model: StringProperty(name="Gloves Model")
    armband_model: StringProperty(name="Captain Armband Model")
    nameplate_model: StringProperty(name="Nameplate Model")
    attach_ball: BoolProperty(name="Attach Ball", default=False)
    ball_model: StringProperty(name="Ball Model")
    preserve_character_part_armatures: BoolProperty(
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    def invoke(self, context, event):
        self.auto_character_parts = False
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "recursive")
        layout.prop(self, "create_report_text")
        layout.prop(self, "import_character_parts")
        if self.import_character_parts:
            layout.prop(self, "body_model")
            layout.prop(self, "shoes_model")
            layout.prop(self, "accessory_model")
            layout.prop(self, "gloves_model")
            layout.prop(self, "armband_model")
            layout.prop(self, "nameplate_model")
            layout.prop(self, "attach_ball")
            if self.attach_ball:
                layout.prop(self, "ball_model")

    def execute(self, context):
        directory = Path(bpy.path.abspath(self.directory or ""))
        if not directory.exists() or not directory.is_dir():
            self.report({"ERROR"}, f"Folder not found: {directory}")
            return {"CANCELLED"}

        paths = collect_model_paths_auto(directory, self.recursive)
        if not paths:
            self.report({"ERROR"}, f"No .g4md/.g4pkm models found in {directory}")
            return {"CANCELLED"}

        prefs = addon_preferences()
        imported_total = 0
        stems = {path.stem.lower() for path in paths}
        placements_by_asset = map_scene_placements(directory, stems)
        placed_total = 0
        hidden_total = 0
        try:
            for path in paths:
                character_model = is_character_model(path)
                _, imported_names = import_g4_model(
                    path,
                    prefs,
                    self.create_report_text,
                    apply_styling=character_model,
                )
                imported_total += len(imported_names)
                if not character_model:
                    hidden_total += auto_hide_map_parts(imported_names)
                placed_total += apply_map_placements(
                    path,
                    imported_names,
                    placements_by_asset.get(path.stem.lower(), []),
                )
                armatures = imported_armatures(imported_names)
                if self.import_character_parts and armatures and re.fullmatch(r"c\d{6,8}", path.stem, re.IGNORECASE):
                    attached, _ = import_character_parts_for_armature(
                        path,
                        armatures[0],
                        prefs,
                        False,
                        self.body_model,
                        self.shoes_model,
                        self.accessory_model,
                        self.gloves_model,
                        self.armband_model,
                        self.nameplate_model,
                        self.create_report_text,
                        preserve_part_armatures=self.preserve_character_part_armatures,
                    )
                    imported_total += attached
                    if self.attach_ball:
                        ball_path = Path(bpy.path.abspath(self.ball_model)) if self.ball_model else find_default_ball_model(path, prefs)
                        if ball_path is None:
                            raise RuntimeError("Default ball model b000001 was not found")
                        imported_total += attach_ball_to_armature(ball_path, armatures[0], prefs, self.create_report_text)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        message = f"Imported {len(paths)} G4 models from folder: {imported_total} objects"
        if placements_by_asset:
            message += f"; reconstructed {placed_total} map placements"
        if hidden_total:
            message += f"; hidden {hidden_total} auxiliary map objects"
        self.report({"INFO"}, message)
        return {"FINISHED"}


class IMPORT_OT_level5_g4_character_parts(Operator):
    bl_idname = "import_scene.level5_g4_character_parts"
    bl_label = "Attach Level-5 G4 Character Parts"
    bl_options = {"REGISTER", "UNDO"}

    body_model: StringProperty(name="Body Model", subtype="FILE_PATH")
    shoes_model: StringProperty(name="Shoes Model", subtype="FILE_PATH")
    accessory_model: StringProperty(name="Arms / Neck", subtype="FILE_PATH")
    gloves_model: StringProperty(name="Gloves Model", subtype="FILE_PATH")
    armband_model: StringProperty(name="Captain Armband Model", subtype="FILE_PATH")
    nameplate_model: StringProperty(name="Nameplate Model", subtype="FILE_PATH")
    attach_ball: BoolProperty(name="Attach Ball", default=False)
    ball_model: StringProperty(name="Ball Model", subtype="FILE_PATH")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "ARMATURE"

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=620)

    def draw(self, context):
        layout = self.layout
        layout.label(text=f"Target rig: {context.active_object.name}")
        layout.prop(self, "body_model")
        layout.prop(self, "shoes_model")
        layout.prop(self, "accessory_model")
        layout.prop(self, "gloves_model")
        layout.prop(self, "armband_model")
        layout.prop(self, "nameplate_model")
        layout.prop(self, "attach_ball")
        if self.attach_ball:
            layout.prop(self, "ball_model")

    def execute(self, context):
        target = context.active_object
        paths = [
            Path(bpy.path.abspath(value))
            for value in (
                self.body_model, self.shoes_model, self.accessory_model,
                self.gloves_model, self.armband_model, self.nameplate_model,
            )
            if value
        ]
        if not paths and not self.attach_ball:
            self.report({"WARNING"}, "No character part selected")
            return {"CANCELLED"}
        invalid = [path for path in paths if not path.is_file() or path.suffix.lower() not in MODEL_EXTENSIONS]
        if invalid:
            self.report({"ERROR"}, f"Character part not found or unsupported: {invalid[0]}")
            return {"CANCELLED"}
        prefs = addon_preferences()
        try:
            attached = sum(attach_part_to_armature(path, target, prefs, False) for path in paths)
            if self.attach_ball:
                ball_path = Path(bpy.path.abspath(self.ball_model)) if self.ball_model else find_default_ball_model(Path(), prefs)
                if ball_path is None:
                    raise RuntimeError("Default ball model b000001 was not found")
                attached += attach_ball_to_armature(ball_path, target, prefs, False)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Attached {len(paths)} character parts: {attached} meshes")
        return {"FINISHED"}


class MATERIAL_PT_level5_character(bpy.types.Panel):
    bl_label = "Level-5 Character"
    bl_idname = "MATERIAL_PT_level5_character"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"

    @classmethod
    def poll(cls, context):
        material = context.material
        return material is not None and bool(material.get("g4_level5_toon")) and material.node_tree is not None

    def draw(self, context):
        layout = self.layout
        nodes = context.material.node_tree.nodes

        color = nodes.get("G4 Saturation")
        if color is not None:
            box = layout.box()
            box.label(text="Base Color", icon="COLOR")
            box.prop(color.inputs["Saturation"], "default_value", text="Saturation")
            box.prop(color.inputs["Value"], "default_value", text="Brightness")

        box = layout.box()
        box.label(text="Toon Lighting", icon="LIGHT_SUN")
        ambient = nodes.get("G4 Toon Ambient")
        if ambient is not None:
            box.prop(ambient.inputs[1], "default_value", text="Light Floor")
            box.prop(ambient.inputs[2], "default_value", text="Shadow Floor")
        dual_toon = nodes.get("G4 Dual Toon Ramp")
        if dual_toon is not None:
            box.prop(dual_toon.inputs[0], "default_value", text="Secondary Shadow")
        highlight = nodes.get("G4 Highlight")
        if highlight is not None:
            box.prop(highlight.inputs[2], "default_value", text="Highlight Color")
        underlight = nodes.get("G4 Under Light")
        if underlight is not None:
            box.prop(underlight.inputs[2], "default_value", text="Under-light Color")

        box = layout.box()
        box.label(text="Surface", icon="MATERIAL")
        normal = nodes.get("G4 Surface Normal")
        if normal is not None:
            box.prop(normal.inputs["Strength"], "default_value", text="Normal Strength")
        specular = nodes.get("G4 Specular Strength")
        if specular is not None:
            box.prop(specular.inputs[0], "default_value", text="Specular Strength")
        wetness = nodes.get("G4 Wetness")
        if wetness is not None:
            box.prop(wetness.outputs[0], "default_value", text="Wetness")

        outline = nodes.get("G4 UV Outline Width")
        if outline is not None:
            box = layout.box()
            box.label(text="Painted Line Detail", icon="MOD_LINEART")
            box.prop(outline.inputs[1], "default_value", text="Width Threshold")


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_level5_g4.bl_idname, text="Level-5 G4 Model (.g4md/.g4pkm)")
    self.layout.operator(IMPORT_OT_level5_g4_folder.bl_idname, text="Level-5 G4 Model Folder")
    self.layout.operator(
        IMPORT_OT_level5_g4_character_parts.bl_idname,
        text="Attach Level-5 G4 Character Parts",
    )


classes = [
    G4ImporterPreferences,
    IMPORT_OT_level5_g4_character_setup,
    IMPORT_OT_level5_g4,
    IMPORT_OT_level5_g4_folder,
    IMPORT_OT_level5_g4_character_parts,
    MATERIAL_PT_level5_character,
]


if hasattr(bpy.types, "FileHandler"):
    class G4_FH_import(bpy.types.FileHandler):
        bl_idname = "G4_FH_import"
        bl_label = "Level-5 G4 Model"
        bl_import_operator = IMPORT_OT_level5_g4.bl_idname
        bl_file_extensions = ".g4md;.g4pkm"

        @classmethod
        def poll_drop(cls, context):
            return context.area is not None and context.area.type in {"VIEW_3D", "OUTLINER", "FILE_BROWSER"}

    classes.append(G4_FH_import)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    g4_animation_addon.register()
    g4_port_addon.register()
    if refresh_level5_outlines_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(refresh_level5_outlines_on_load)


def unregister():
    if refresh_level5_outlines_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(refresh_level5_outlines_on_load)
    g4_port_addon.unregister()
    g4_animation_addon.unregister()
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
