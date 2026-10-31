"""Public Action-to-G4PK export using the existing animation sampler."""
import os
from pathlib import Path
import re
import tempfile

import bpy
from bpy.props import BoolProperty, CollectionProperty, IntProperty, StringProperty
from bpy.types import Operator, PropertyGroup, UIList
from bpy_extras.io_utils import ExportHelper

try:
    from ..formats.g4mt_writer import G4MTClipSamples, write_authored_g4mt
except ImportError:
    from formats.g4mt_writer import G4MTClipSamples, write_authored_g4mt
from .g4pk_extract_g4mt import pack_g4mt


def rig_actions(rig) -> list:
    from .g4_animation_addon import action_fcurves
    names = set(rig.data.bones.keys())
    result = []
    for action in bpy.data.actions:
        owner = action.get('g4mt_rig_id')
        if owner and rig.get('g4_animation_rig_id') and owner != rig['g4_animation_rig_id']:
            continue
        joints = {match.group(1) for curve in action_fcurves(action)
                  if (match := re.match(r'pose\.bones\["((?:\\.|[^"\\])*)"\]', curve.data_path))}
        joints = {name.replace('\\"', '"').replace('\\\\', '\\') for name in joints}
        if joints and joints <= names:
            result.append(action)
    return sorted(result, key=lambda action: action.name.casefold())


def export_actions(context, rig, actions: list, path: Path, keep_g4mt: bool) -> None:
    from .g4_animation_addon import sample_action_for_g4mt
    if rig is None or rig.type != 'ARMATURE' or not actions:
        raise ValueError('Select an armature and at least one compatible Action')
    compatible = set(rig_actions(rig))
    if any(action not in compatible for action in actions):
        raise ValueError('An Action is no longer compatible with the selected rig')
    if path.suffix.lower() != '.g4pk' or not path.parent.is_dir():
        raise ValueError('Choose a G4PK file in an existing folder')
    motion_path = path.with_suffix('.g4mt')
    for action in actions:
        source = action.get('g4mt_source')
        if source and Path(bpy.path.abspath(source)).resolve() in ({path.resolve(), motion_path.resolve()} if keep_g4mt else {path.resolve()}):
            raise ValueError('Choose an output filename different from the source animation')
    if keep_g4mt and motion_path.exists():
        raise FileExistsError(f'The companion G4MT already exists: {motion_path.name}')
    fps = context.scene.render.fps / context.scene.render.fps_base
    if abs(fps - round(fps)) > 1e-4:
        raise ValueError('G4MT requires an integer scene frame rate')
    copy = rig.copy()
    copy.data = rig.data.copy()
    context.collection.objects.link(copy)
    data = copy.data
    context.window_manager.progress_begin(0, len(actions))
    try:
        copy.animation_data_create()
        clips = []
        for index, action in enumerate(actions):
            context.window_manager.progress_update(index)
            context.workspace.status_text_set(f'Exporting Action {index + 1}/{len(actions)}: {action.name}')
            samples = sample_action_for_g4mt(copy, action, context.scene, context.view_layer)
            clips.append(G4MTClipSamples(index, action.name, samples))
        motion = write_authored_g4mt(clips, round(fps))
        package = pack_g4mt(motion_path.name, motion)
        with tempfile.TemporaryDirectory(prefix='.g4-motion-', dir=path.parent) as directory:
            staged = Path(directory)
            (staged / path.name).write_bytes(package)
            if keep_g4mt:
                (staged / motion_path.name).write_bytes(motion)
                os.replace(staged / motion_path.name, motion_path)
            os.replace(staged / path.name, path)
    finally:
        context.window_manager.progress_end()
        context.workspace.status_text_set(None)
        bpy.data.objects.remove(copy, do_unlink=True)
        bpy.data.armatures.remove(data)


class G4MotionAction(PropertyGroup):
    action_name: StringProperty()
    included: BoolProperty(default=True)


class G4_UL_motion_actions(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index=0, flt_flag=0):
        layout.prop(item, 'included', text=item.action_name)


class G4MotionChooseActions(Operator):
    bl_idname = 'export_scene.level5_g4pk_actions'
    bl_label = 'Choose which animations to pack'
    filepath: StringProperty(subtype='FILE_PATH')
    rig_name: StringProperty()
    keep_g4mt: BoolProperty(default=False)
    actions: CollectionProperty(type=G4MotionAction)
    active_action_index: IntProperty(default=0)

    def invoke(self, context, event):
        rig = bpy.data.objects.get(self.rig_name)
        if rig is None or rig.type != 'ARMATURE':
            self.report({'ERROR'}, 'The selected armature is no longer available')
            return {'CANCELLED'}
        self.actions.clear()
        for action in rig_actions(rig):
            item = self.actions.add()
            item.name = item.action_name = action.name
        return context.window_manager.invoke_props_dialog(self, width=480)

    def draw(self, context):
        self.layout.label(text='Choose which animations to pack:')
        self.layout.template_list('G4_UL_motion_actions', '', self, 'actions',
                                  self, 'active_action_index', rows=10)

    def execute(self, context):
        try:
            rig = bpy.data.objects.get(self.rig_name)
            actions = [bpy.data.actions[item.action_name] for item in self.actions if item.included]
            export_actions(context, rig, actions, Path(bpy.path.abspath(self.filepath)), self.keep_g4mt)
        except (KeyError, OSError, ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Packed {len(actions)} Actions')
        return {'FINISHED'}


class G4MotionExport(Operator, ExportHelper):
    bl_idname = 'export_scene.level5_g4pk'
    bl_label = 'Export G4PK animation'
    filename_ext = '.g4pk'
    filter_glob: StringProperty(default='*.g4pk', options={'HIDDEN'})
    keep_g4mt: BoolProperty(name='Maintain G4MT file', default=False)
    export_all_actions: BoolProperty(name='Export all Actions', default=True)
    rig_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'ARMATURE'

    def invoke(self, context, event):
        self.rig_name = context.active_object.name
        self.filepath = self.rig_name + '.g4pk'
        return ExportHelper.invoke(self, context, event)

    def draw(self, context):
        self.layout.prop(self, 'keep_g4mt')
        self.layout.prop(self, 'export_all_actions')

    def execute(self, context):
        rig = bpy.data.objects.get(self.rig_name) if self.rig_name else context.active_object
        if rig is None or rig.type != 'ARMATURE':
            self.report({'ERROR'}, 'Select an armature')
            return {'CANCELLED'}
        if not self.export_all_actions:
            from .g4_animation_addon import defer_blender_call
            path, name, keep = self.filepath, rig.name, self.keep_g4mt
            defer_blender_call(lambda: bpy.ops.export_scene.level5_g4pk_actions(
                'INVOKE_DEFAULT', filepath=path, rig_name=name, keep_g4mt=keep))
            return {'FINISHED'}
        try:
            actions = rig_actions(rig)
            export_actions(context, rig, actions, Path(bpy.path.abspath(self.filepath)), self.keep_g4mt)
        except (OSError, ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Packed {len(actions)} Actions')
        return {'FINISHED'}


classes = [G4MotionAction, G4_UL_motion_actions, G4MotionChooseActions, G4MotionExport]
