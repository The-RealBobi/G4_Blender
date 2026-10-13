"""Import a game animation onto a character's original Avatar rig."""

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Context, Object, Operator
from bpy_extras.io_utils import ImportHelper

from .humanoid_avatar_pose import bake_avatar_action


def original_skeleton(context: Context, target: Object) -> Path:
    from .g4_animation_addon import materialize_skeleton_source
    from ..porting.g4_model_probe import configure_raw_data_root_from_path, find_skeleton_for_model

    props = getattr(context.scene, "level5_g4_port", None)
    if props is not None and props.original_model:
        original = Path(bpy.path.abspath(props.original_model))
        configure_raw_data_root_from_path(original)
        _data, source = find_skeleton_for_model(original)
        if source:
            path = materialize_skeleton_source(source.split(" via ", 1)[0])
            if path is not None:
                return path
        raise ValueError("The selected original character model has no readable skeleton")
    source = str(target.get("g4_character_skeleton_source", ""))
    if source:
        path = materialize_skeleton_source(source)
        if path is not None:
            return path
    raise ValueError("Choose the original character model in the Port panel before importing an animation")


def read_motion(path: Path, entry: str, clip: str, skeleton: Path) -> dict:
    from .g4_animation_addon import materialize_g4mt
    from .g4mt_motion import decode_motion

    motion_path, temporary, _entry = materialize_g4mt(path, entry)
    try:
        return decode_motion(motion_path, clip, skeleton)
    finally:
        if temporary is not None:
            temporary.unlink()
            temporary.parent.rmdir()


def preview_animation(context: Context, target: Object, path: Path, entry: str, clip: str) -> Generator[tuple[int, int, str], None, str]:
    from .g4_animation_addon import create_action
    from .. import addon_preferences, apply_g4_bone_orientation, create_native_g4_armature
    from ..porting.g4_model_probe import parse_g4sk, skeleton_bone_orientation
    from .humanoid_avatar_addon import mapping_from_profile, update_validated_representation

    validated = update_validated_representation(context, target, target.level5_humanoid_avatar)
    if not validated["valid"] or target.level5_humanoid_avatar.pose_stale:
        raise ValueError("Validate the Avatar mapping and capture its current rest pose before importing an animation")
    if not path.is_file() or path.suffix.lower() not in {".g4mt", ".g4pk"}:
        raise ValueError("Choose a readable G4MT animation or G4PK animation bank")
    skeleton = original_skeleton(context, target)
    yield 0, 1, f"Reading animation: {path.name}"
    executor = ThreadPoolExecutor(max_workers=1)
    source = None
    source_action = None
    action = None
    committed = False
    try:
        pending = executor.submit(read_motion, path, entry, clip, skeleton)
        if not bpy.app.background:
            while not pending.done():
                yield 0, 1, f"Reading animation: {path.name}"
        motion = pending.result()
        fps = max(1, round(motion["clip"]["fps"]))
        if fps > context.scene.render.bl_rna.properties["fps"].hard_max:
            raise ValueError("The animation frame rate exceeds Blender's supported range")
        yield 0, 1, f"Preparing animation: {path.name}"
        previous_active = context.view_layer.objects.active
        previous_selected = tuple(context.selected_objects)
        try:
            for obj in previous_selected:
                obj.select_set(False)
            skeleton_info = parse_g4sk(skeleton.read_bytes())
            source = create_native_g4_armature(skeleton_info)
            apply_g4_bone_orientation(
                {source.name}, {"bone_orientation": skeleton_bone_orientation(skeleton_info)},
                addon_preferences().apply_bone_orientation,
            )
            source.select_set(False)
        finally:
            for obj in previous_selected:
                obj.select_set(True)
            context.view_layer.objects.active = previous_active
        source.hide_render = True
        source.hide_set(True)
        source_action, keyed = create_action(source, motion)
        if not keyed:
            raise ValueError("The selected animation has no tracks for the original character")
        source_mapping = {bone.name: bone.name for bone in source.data.bones}
        target_mapping = mapping_from_profile(target.level5_humanoid_avatar)
        frames = tuple(range(1, len(motion["frames"]) + 1))
        action_name = f"{target.name} · {motion['clip']['name']}"
        action = yield from bake_avatar_action(context, source, target, source_mapping, target_mapping, frames, action_name)
        target.animation_data_create()
        target.animation_data.action = action
        slots = getattr(action, "slots", ())
        if slots:
            target.animation_data.action_slot = slots[0]
        target.data.pose_position = "POSE"
        committed = True
        context.scene.frame_start = frames[0]
        context.scene.frame_end = frames[-1]
        context.scene.render.fps = fps
        context.scene.frame_set(frames[0])
        return f"Animation imported onto {target.name}: {len(frames)} frames"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        if source is not None:
            if source_action is None and source.animation_data is not None:
                source_action = source.animation_data.action
            source_data = source.data
            bpy.data.objects.remove(source, do_unlink=True)
            bpy.data.armatures.remove(source_data)
        if source_action is not None:
            bpy.data.actions.remove(source_action)
        if action is not None and not committed:
            bpy.data.actions.remove(action)


class IMPORT_SCENE_OT_level5_humanoid_animation(Operator, ImportHelper):
    bl_idname = "import_scene.level5_humanoid_animation"
    bl_label = "Import Avatar Animation"
    bl_description = "Apply a game animation to this character's original bones"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".g4mt"
    filter_glob: StringProperty(default="*.g4mt;*.g4pk", options={"HIDDEN"})
    clip_index: IntProperty(name="Animation", min=0)
    entry_index: IntProperty(name="Bank Entry", min=0)
    progress_text: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    progress_value: FloatProperty(options={"HIDDEN", "SKIP_SAVE"})
    progress_window_open: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        target = context.active_object
        return target is not None and target.type == "ARMATURE" and target.mode == "OBJECT" and target.level5_humanoid_avatar.has_representation

    def execute(self, context):
        from .. import start_import_progress
        task = preview_animation(context, context.active_object, Path(self.filepath), str(self.entry_index), str(self.clip_index))
        if bpy.app.background:
            try:
                while True:
                    next(task)
            except StopIteration as result:
                self.report({"INFO"}, result.value)
                return {"FINISHED"}
            except Exception as error:
                task.close()
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}
        return start_import_progress(self, context, 1, "Preparing animation", task)

    def modal(self, context, event):
        from .. import finish_import_progress, modal_import_progress, progress_cancel_clicked
        if event.type == "ESC" or progress_cancel_clicked(self, context, event):
            self._import_progress_task.close()
            self._import_progress_task = None
            finish_import_progress(self, context)
            self.report({"INFO"}, "Animation import cancelled")
            return {"CANCELLED"}
        return modal_import_progress(self, context, event)
