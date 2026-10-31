"""A three-step wizard for authored model export."""
from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
import re
from pathlib import Path
import tempfile
import zlib

import bmesh
import bpy
import bpy.utils.previews
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from .g4_model_export import _material_source, export_model_package
from ..animation.humanoid_prefab import (prefab_joint_names, prefab_skeleton_bytes, PREFAB_ID,
                                          SOURCE_MODEL_STEM, CHR_FAMILY, EVENT_SKELETON)


_image_previews: bpy.utils.previews.ImagePreviewCollection | None = None
_texture_menu_items = {}
SHADERS = (("Chr_Toon", "Chr_Toon", "Base color, line and occlusion"),
           ("Chr_ToonNrm", "Chr_ToonNrm", "Adds a tangent normal map"))


def image_preview_icon(image: bpy.types.Image | None) -> int:
    if image is None or _image_previews is None:
        return 0
    preview = _image_previews.get(str(image.as_pointer()))
    return preview.icon_id if preview is not None else 0


def prepare_image_preview(image: bpy.types.Image | None) -> None:
    global _image_previews
    if image is None:
        return
    import numpy as np
    if _image_previews is None:
        _image_previews = bpy.utils.previews.new()
    key = str(image.as_pointer())
    if key in _image_previews:
        return
    width, height = image.size
    if not width or not height:
        return
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(height, width, 4)
    preview = _image_previews.new(key)
    for size, prefix in ((32, 'icon'), (64, 'image')):
        xs = np.linspace(0, width - 1, size, dtype=int)
        ys = np.linspace(0, height - 1, size, dtype=int)
        setattr(preview, prefix + '_size', (size, size))
        setattr(preview, prefix + '_pixels_float', pixels[ys[:, None], xs].ravel())


def assigned_image_changed(self, context) -> None:
    for image in (self.base_image, self.normal_image, self.line_image, self.occlusion_image):
        prepare_image_preview(image)


def texture_preview_property(prop: str):
    def items(self, context):
        image = getattr(self, prop)
        key = (image.as_pointer() if image else 0, prop)
        if key not in _texture_menu_items:
            _texture_menu_items[key] = (
                ('CURRENT', image.name if image else 'Empty', '', image_preview_icon(image) or 'IMAGE_DATA', 0),
                ('CHOOSE', 'Choose image', '', 'FILE_FOLDER', 1),
            )
        return _texture_menu_items[key]
    def get(self):
        return 0
    def set(self, value):
        if value != 1:
            return
        from ..animation.g4_animation_addon import defer_blender_call
        owner_path = self.path_from_id()
        defer_blender_call(lambda: bpy.ops.level5_g4_export.choose_image(
            'INVOKE_DEFAULT', owner_path=owner_path, slot=prop))
    return EnumProperty(items=items, get=get, set=set, options={'SKIP_SAVE'})


def cell_image_changed(self, context) -> None:
    prepare_image_preview(self.image)


def expression_group(surface, context):
    settings = state(context)
    return settings.face_atlas if surface.expression_kind == 'FACE' else settings.mouth_atlas


def ensure_expression_group(surface, context):
    group = expression_group(surface, context)
    if not group.initialized:
        for prop in ('shader', 'base_image', 'normal_image', 'line_image', 'occlusion_image', 'use_outline'):
            setattr(group, prop, getattr(surface, prop))
        group.alpha_blend = True
        group.initialized = True
    while len(group.cells) < 8:
        cell = group.cells.add()
        cell.name = str(len(group.cells))
    return group


def expression_mode_changed(self, context) -> None:
    if self.expression_atlas:
        ensure_expression_group(self, context)


def build_atlas_changed(self, context) -> None:
    if self.build_atlas:
        self.saved_base_image = self.base_image
        self.base_image = None
    else:
        self.base_image = self.saved_base_image


class G4ExportAtlasCell(PropertyGroup):
    image: PointerProperty(type=bpy.types.Image, update=cell_image_changed)
    preview: texture_preview_property('image')


class G4ExportExpressionGroup(PropertyGroup):
    initialized: BoolProperty()
    build_atlas: BoolProperty(name='Build Expression Atlas', update=build_atlas_changed)
    saved_base_image: PointerProperty(type=bpy.types.Image)
    cells: CollectionProperty(type=G4ExportAtlasCell)
    shader: EnumProperty(items=SHADERS)
    base_image: PointerProperty(type=bpy.types.Image, update=assigned_image_changed)
    normal_image: PointerProperty(type=bpy.types.Image, update=assigned_image_changed)
    line_image: PointerProperty(type=bpy.types.Image, update=assigned_image_changed)
    occlusion_image: PointerProperty(type=bpy.types.Image, update=assigned_image_changed)
    base_preview: texture_preview_property('base_image')
    normal_preview: texture_preview_property('normal_image')
    line_preview: texture_preview_property('line_image')
    occlusion_preview: texture_preview_property('occlusion_image')
    alpha_blend: BoolProperty()
    use_outline: BoolProperty(name='Enable Outline', default=True)


class G4ExportSurface(PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)
    slot: IntProperty()
    included: BoolProperty(default=True)
    expanded: BoolProperty(default=False)
    shader: EnumProperty(items=SHADERS)
    base_image: PointerProperty(type=bpy.types.Image, update=assigned_image_changed)
    normal_image: PointerProperty(type=bpy.types.Image, update=assigned_image_changed)
    line_image: PointerProperty(type=bpy.types.Image, update=assigned_image_changed)
    occlusion_image: PointerProperty(type=bpy.types.Image, update=assigned_image_changed)
    alpha_blend: BoolProperty()
    base_preview: texture_preview_property('base_image')
    normal_preview: texture_preview_property('normal_image')
    line_preview: texture_preview_property('line_image')
    occlusion_preview: texture_preview_property('occlusion_image')
    use_outline: BoolProperty(name='Enable Outline', default=True)
    expression_atlas: BoolProperty(name='Use Expression Atlas', update=expression_mode_changed)
    expression_kind: EnumProperty(items=[('FACE', 'Face', ''), ('MOUTH', 'Mouth', '')],
                                  update=expression_mode_changed, translation_context='G4_EXPORT')
    uv_map: StringProperty()
    normal_uv_map: StringProperty()
    tangent_uv_map: StringProperty()


class G4ExportBone(PropertyGroup):
    parent: StringProperty()
    depth: IntProperty()
    expanded: BoolProperty(default=True)
    reference: StringProperty(name='Reference bone')


class G4ExportReference(PropertyGroup):
    pass


class G4ExportState(PropertyGroup):
    step: EnumProperty(items=[("materials", "Materials", ""), ("rig", "Rig", ""), ("confirm", "Confirm", "")])
    error: StringProperty()
    show_expressions: BoolProperty(name="Expressions")
    face_atlas: PointerProperty(type=G4ExportExpressionGroup)
    mouth_atlas: PointerProperty(type=G4ExportExpressionGroup)
    surfaces: CollectionProperty(type=G4ExportSurface)
    bones: CollectionProperty(type=G4ExportBone)
    references: CollectionProperty(type=G4ExportReference)
    armature: PointerProperty(type=bpy.types.Object)
    model_name: StringProperty(name='Model name')
    prepare_victorytool: BoolProperty(name='Prepare model for VictoryTool', default=True)
    platform: EnumProperty(name='Platform', items=[('dx11', 'DX11', 'DDS textures under data/dx11'),
                         ('nx', 'NX', 'NXTCH textures under data/nx'),
                         ('both', 'Both', 'Generate both texture archives')])
    reference_skeleton: StringProperty(options={'HIDDEN'})
    reference_error: StringProperty()


def state(context) -> G4ExportState:
    return context.scene.level5_g4_export


def refresh_reference_bones(context) -> set[str]:
    settings = state(context)
    names = prefab_joint_names()
    ordered = sorted(names)
    if [item.name for item in settings.references] != ordered:
        settings.references.clear()
        for name in ordered:
            settings.references.add().name = name
    settings.reference_error = ''
    return set(names)


def prefill_assigned_images(row, material) -> None:
    from ..shading.character_textures import character_texture_role
    if material is None or material.node_tree is None:
        return
    candidates = {}
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            color = node.inputs['Base Color']
            if color.is_linked and color.links[0].from_node.type == 'TEX_IMAGE':
                image_node = color.links[0].from_node
                row.base_image = image_node.image
                vector = image_node.inputs['Vector']
                row.uv_map = vector.links[0].from_node.uv_map if vector.is_linked and vector.links[0].from_node.type == 'UVMAP' else ''
            normal = node.inputs['Normal']
            if normal.is_linked and normal.links[0].from_node.type == 'NORMAL_MAP':
                normal_color = normal.links[0].from_node.inputs['Color']
                if normal_color.is_linked and normal_color.links[0].from_node.type == 'TEX_IMAGE':
                    image_node = normal_color.links[0].from_node
                    row.normal_image = image_node.image
                    vector = image_node.inputs['Vector']
                    row.normal_uv_map = vector.links[0].from_node.uv_map if vector.is_linked and vector.links[0].from_node.type == 'UVMAP' else ''
                    row.tangent_uv_map = normal.links[0].from_node.uv_map
            row.alpha_blend = node.inputs['Alpha'].is_linked
        if node.type == 'TEX_IMAGE' and node.image is not None and any(socket.is_linked for socket in node.outputs):
            role = character_texture_role(node.image.filepath or node.image.name)
            candidates.setdefault(role, set()).add(node.image)
    for role, prop in [('base', 'base_image'), ('normal', 'normal_image'), ('line', 'line_image'), ('oc', 'occlusion_image')]:
        images = candidates.get(role, set())
        if getattr(row, prop) is None and len(images) == 1:
            setattr(row, prop, next(iter(images)))
    if row.normal_image is not None:
        row.shader = 'Chr_ToonNrm'
        if row.base_image is None:
            row.uv_map = row.normal_uv_map or row.tangent_uv_map


def initialize_workflow(context) -> None:
    settings = state(context)
    objects = sorted((o for o in context.scene.objects if o.type == 'MESH' and o.visible_get()), key=lambda o: o.name)
    if not objects:
        raise ValueError('Load a mesh model in Blender before exporting')
    settings.step = "materials"
    settings.error = ""
    settings.surfaces.clear()
    settings.bones.clear()
    for group in (settings.face_atlas, settings.mouth_atlas):
        group.build_atlas = False
        group.initialized = False
        group.cells.clear()
        group.saved_base_image = None
        for prop in ('base_image', 'normal_image', 'line_image', 'occlusion_image'):
            setattr(group, prop, None)
    active = context.active_object
    name = active.name if active is not None and active.type in {'MESH', 'ARMATURE'} else objects[0].name
    settings.model_name = re.sub(r'[^A-Za-z0-9_-]', '_', name).strip('_') or 'model'
    selected = {o for o in context.selected_objects if o.type == 'MESH'}
    settings.armature = None
    settings.prepare_victorytool = True
    for obj in objects:
        slots = sorted({p.material_index for p in obj.data.polygons})
        for slot in slots:
            row = settings.surfaces.add()
            row.object, row.slot = obj, slot
            row.name = obj.name if len(slots) == 1 else f'{obj.name} / {obj.data.materials[slot].name if slot < len(obj.data.materials) and obj.data.materials[slot] else slot}'
            row.included = not selected or obj in selected
            material = obj.data.materials[slot] if slot < len(obj.data.materials) else None
            try:
                source = _material_source(material)
            except ValueError:
                prefill_assigned_images(row, material)
                continue
            row.shader = 'Chr_ToonNrm' if source.normal_image else 'Chr_Toon'
            row.base_image = source.base_image.image if source.base_image else None
            row.normal_image = source.normal_image.image if source.normal_image else None
            row.line_image, row.occlusion_image = source.line_image, source.occlusion_image
            row.alpha_blend = source.alpha_blend
            row.normal_uv_map = source.normal_image.uv_map if source.normal_image else ''
            row.tangent_uv_map = source.tangent_uv_map
            row.uv_map = source.base_image.uv_map if source.base_image else (row.normal_uv_map or row.tangent_uv_map)
    refresh_reference_bones(context)
    synchronize_rig(context)


def synchronize_rig(context) -> None:
    from ..animation.humanoid_avatar_addon import mapping_from_profile, bone_nodes
    from ..animation.humanoid_avatar import incompatible_body_assignments, suggest_native_mapping
    settings = state(context)
    objects = {row.object for row in settings.surfaces if row.included and row.object is not None}
    rigs = {modifier.object for obj in objects for modifier in obj.modifiers
            if modifier.type == 'ARMATURE' and modifier.show_viewport and modifier.object is not None}
    if len(rigs) > 1:
        raise ValueError('The included submeshes use different armatures; export one model rig at a time')
    rig = next(iter(rigs), None)
    if rig == settings.armature and settings.bones:
        return
    settings.armature = rig
    settings.bones.clear()
    if rig is None:
        return
    existing = mapping_from_profile(settings.armature.level5_humanoid_avatar)
    nodes = bone_nodes(settings.armature)
    invalid = incompatible_body_assignments(nodes, existing)
    if invalid:
        suggestions = suggest_native_mapping(nodes, refresh_reference_bones(context)).assignments
        existing = {joint: suggestions[joint] if joint in invalid else name
                    for joint, name in existing.items() if joint not in invalid or joint in suggestions}
        settings.error = 'Updated incompatible previous body assignments; review Rig.'
    by_bone = {bone: joint for joint, bone in existing.items()}
    def walk(bone):
        yield bone
        for child in sorted(bone.children, key=lambda b: b.name):
            yield from walk(child)
    ordered = [bone for root in settings.armature.data.bones if root.parent is None for bone in walk(root)]
    for bone in ordered:
        item = settings.bones.add()
        item.name, item.parent = bone.name, bone.parent.name if bone.parent else ''
        item.depth = len(bone.parent_recursive)
        item.expanded = item.depth < 2
        item.reference = by_bone.get(bone.name, '')


def validate_mapping(context) -> dict[str, str]:
    settings = state(context)
    known = refresh_reference_bones(context)
    mapping = {}
    for bone in settings.bones:
        if not bone.reference:
            continue
        if bone.reference not in known:
            raise ValueError(f'{bone.name}: choose a valid reference bone')
        if bone.reference in mapping.values():
            raise ValueError(f'{bone.reference} is assigned more than once')
        mapping[bone.name] = bone.reference
    if settings.armature is not None:
        from ..animation.humanoid_avatar import incompatible_body_assignments
        from ..animation.humanoid_avatar_addon import bone_nodes
        invalid = incompatible_body_assignments(bone_nodes(settings.armature), {joint: name for name, joint in mapping.items()})
        if invalid:
            raise ValueError(next(iter(invalid.values())))
    return mapping


def write_external_profile(context, model: Path) -> None:
    from ..animation.victorytool_animation_recipe import build_avatar_recipe
    from ..animation.g4mt_motion import parse_skeleton
    settings = state(context)
    if settings.armature is None:
        raise ValueError('This model has no armature; disable VictoryTool preparation for a static export')
    mapping = validate_mapping(context)
    mapping['c_global_0_0'] = 'c_global_0_0'
    target_path = model.with_suffix('.g4sk')
    source_bytes = prefab_skeleton_bytes()
    with tempfile.TemporaryDirectory(prefix='g4-prefab-') as folder:
        source = Path(folder) / f'{PREFAB_ID}.g4sk'
        source.write_bytes(source_bytes)
        recipe = build_avatar_recipe(source, target_path, model, mapping, CHR_FAMILY, EVENT_SKELETON, True, False)
        source_rig = parse_skeleton(source)
    target = parse_skeleton(target_path)
    indices = {n: i for i, n in enumerate(source_rig['names'])}
    joints = []
    for i, name in enumerate(target['names']):
        source_name = mapping.get(name)
        parent = target['parents'][i]
        source_index = indices.get(source_name)
        source_parent = source_rig['parents'][source_index] if source_index is not None else len(indices)
        joints.append({'name': name, 'nameCrc32': zlib.crc32(name.encode()) & 0xffffffff,
                       'parent': target['names'][parent] if parent < len(target['names']) else None,
                       'sourceRest': source_rig['local_trs'][source_index] if source_index is not None else target['local_trs'][i],
                       'targetRest': target['local_trs'][i], 'modelRest': None,
                       'sourceParent': source_rig['names'][source_parent] if source_parent < len(indices) else None,
                       'sourceName': source_name, 'sourceNameCrc32': zlib.crc32(source_name.encode()) & 0xffffffff if source_name else None})
    profile = {'schemaVersion': 7, 'producer': 'G4_Blender', 'modelStem': model.stem,
               'sourceModelStem': SOURCE_MODEL_STEM, 'sourceRigBinding': 'dump', 'modelPath': f'chr/{model.stem}/{model.name}',
               'sourceRig': {'path': f'prefab/{PREFAB_ID}.g4sk', 'sha256': hashlib.sha256(source_bytes).hexdigest()},
               'targetRig': {'path': f'common/chr/{model.stem}/{target_path.name}', 'sha256': hashlib.sha256(target_path.read_bytes()).hexdigest()},
               'joints': joints, 'avatarRecipe': recipe, 'generatedBanks': [], 'generatedPackages': []}
    model.with_suffix('.g4anim.json').write_text(json.dumps(profile, indent=2) + '\n', encoding='utf-8')


@contextmanager
def export_surfaces(context) -> Iterator[None]:
    from .g4_port_addon import (special_texture_default_color, EXPRESSION_PREVIEW_BACKUP_UV,
                                EXPRESSION_PREVIEW_SOURCE_UV_KEY)
    from .g4_expression_atlas import build_expression_atlas, fit_expression_uv, merge_expression_surfaces
    settings = state(context)
    rows = [row for row in settings.surfaces if row.included]
    if not rows:
        raise ValueError('Include at least one submesh')
    for row in rows:
        if row.object is not None and row.object.parent_type == 'BONE':
            raise ValueError(f'{row.object.name}: bone-parented meshes are not supported; use an armature modifier')
    active, selected = context.view_layer.objects.active, tuple(context.selected_objects)
    before_objects, before_meshes = set(bpy.data.objects), set(bpy.data.meshes)
    before_materials, before_images = set(bpy.data.materials), set(bpy.data.images)
    grouped_objects, grouped_materials = {}, {}
    def image_or_default(image, label, color, noncolor):
        if image is not None:
            return image
        generated = bpy.data.images.new(label, width=8, height=8)
        generated.generated_color = color
        if noncolor:
            generated.colorspace_settings.name = 'Non-Color'
        return generated
    def material_for(row, owner, uv_name):
        role = row.expression_kind if row.expression_atlas else None
        if role in grouped_materials:
            return grouped_materials[role]
        material = bpy.data.materials.new(row.name + '_export')
        material.use_nodes = True
        tree = material.node_tree
        tree.nodes.clear()
        shader = tree.nodes.new('ShaderNodeBsdfPrincipled')
        output = tree.nodes.new('ShaderNodeOutputMaterial')
        tree.links.new(shader.outputs['BSDF'], output.inputs['Surface'])
        building = row.expression_atlas and owner.build_atlas
        base = (build_expression_atlas([cell.image for cell in owner.cells], role.lower() + '_expressions', False)
                if building else image_or_default(owner.base_image, 'base_default', (1.,1.,1.,1.), False))
        node = tree.nodes.new('ShaderNodeTexImage')
        node.image = base
        if uv_name:
            uv = tree.nodes.new('ShaderNodeUVMap')
            uv.uv_map = uv_name
            tree.links.new(uv.outputs['UV'], node.inputs['Vector'])
        tree.links.new(node.outputs['Color'], shader.inputs['Base Color'])
        if owner.alpha_blend:
            tree.links.new(node.outputs['Alpha'], shader.inputs['Alpha'])
        if owner.shader == 'Chr_ToonNrm':
            normal = image_or_default(owner.normal_image, 'normal_default', (.5,.5,1.,1.), True)
            if building and owner.normal_image is not None:
                normal = build_expression_atlas([normal]*8, role.lower() + '_normals', True)
            node = tree.nodes.new('ShaderNodeTexImage')
            node.image = normal
            normal_uv_name = uv_name if row.expression_atlas else row.normal_uv_map
            if normal_uv_name:
                uv = tree.nodes.new('ShaderNodeUVMap')
                uv.uv_map = normal_uv_name
                tree.links.new(uv.outputs['UV'], node.inputs['Vector'])
            normal_node = tree.nodes.new('ShaderNodeNormalMap')
            normal_node.uv_map = uv_name if row.expression_atlas else row.tangent_uv_map
            tree.links.new(node.outputs['Color'], normal_node.inputs['Color'])
            tree.links.new(normal_node.outputs['Normal'], shader.inputs['Normal'])
        for prop, label, default in (('line_image','line','default_line'), ('occlusion_image','occlusion','default_oc')):
            image = image_or_default(getattr(owner, prop), label + '_default', special_texture_default_color(default), True)
            if building and getattr(owner, prop) is not None:
                image = build_expression_atlas([image]*8, role.lower() + '_' + label, True)
            material['g4_export_line_image' if prop=='line_image' else 'g4_export_occlusion_image'] = image.name
        material['g4_export_outline'] = owner.use_outline
        if role is not None:
            grouped_materials[role] = material
        return material
    try:
        for obj in context.selected_objects:
            obj.select_set(False)
        for row in rows:
            if row.object is None or row.object.name not in context.scene.objects:
                raise ValueError(f'{row.name}: source mesh is no longer in the scene')
            obj = row.object.copy()
            obj.data = row.object.data.copy()
            context.scene.collection.objects.link(obj)
            obj.name = re.sub(r'[^A-Za-z0-9_-]', '_', row.name)
            mesh = bmesh.new()
            try:
                mesh.from_mesh(obj.data)
                bmesh.ops.delete(mesh, geom=[face for face in mesh.faces if face.material_index != row.slot], context='FACES')
                mesh.to_mesh(obj.data)
            finally:
                mesh.free()
            source_uv = obj.data.uv_layers.get(str(obj.get(EXPRESSION_PREVIEW_SOURCE_UV_KEY, '')))
            backup_uv = obj.data.uv_layers.get(EXPRESSION_PREVIEW_BACKUP_UV)
            if source_uv is not None and backup_uv is not None:
                for target, source in zip(source_uv.data, backup_uv.data):
                    target.uv = source.uv
            owner = ensure_expression_group(row, context) if row.expression_atlas else row
            uv_name = row.uv_map
            if row.expression_atlas:
                render_uv = next((layer for layer in obj.data.uv_layers if layer.active_render), obj.data.uv_layers.active)
                source_name = row.uv_map or (render_uv.name if render_uv else '')
                if owner.shader == 'Chr_ToonNrm' and owner.normal_image is not None:
                    normal_name = row.normal_uv_map or (render_uv.name if render_uv else '')
                    tangent_name = row.tangent_uv_map or (render_uv.name if render_uv else '')
                    if source_name != normal_name or source_name != tangent_name:
                        raise ValueError(f'{row.name}: base, normal and tangent UV maps must agree')
                uv_name = fit_expression_uv(obj.data, source_name, '_ExpressionUV', owner.build_atlas)
                obj.data.uv_layers[uv_name].active_render = True
            material = material_for(row, owner, uv_name)
            obj.data.materials.clear()
            obj.data.materials.append(material)
            for polygon in obj.data.polygons:
                polygon.material_index = 0
            if row.expression_atlas:
                grouped_objects.setdefault(row.expression_kind, []).append(obj)
        for role, objects in grouped_objects.items():
            merge_expression_surfaces(context, objects, 'eye_10' if role=='FACE' else 'mouth_10', grouped_materials[role])
        exports = [obj for obj in bpy.data.objects if obj not in before_objects and obj.type=='MESH']
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in exports:
            obj.select_set(True)
        context.view_layer.objects.active = exports[0]
        context.view_layer.update()
        yield
    finally:
        for obj in set(bpy.data.objects) - before_objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in set(bpy.data.meshes) - before_meshes:
            bpy.data.meshes.remove(mesh)
        for material in set(bpy.data.materials) - before_materials:
            bpy.data.materials.remove(material)
        for image in set(bpy.data.images) - before_images:
            bpy.data.images.remove(image)
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in selected:
            if obj.name in context.scene.objects:
                obj.select_set(True)
        context.view_layer.objects.active = active
        context.view_layer.update()


def validate_export_setup(context) -> None:
    settings = state(context)
    synchronize_rig(context)
    if settings.prepare_victorytool:
        if settings.armature is None:
            raise ValueError('Disable VictoryTool preparation for a static model without an armature')
        mapping = validate_mapping(context)
        required = {'c_c_1_0', 'c_head_1_0', 'l_l_1_0', 'r_l_1_0', 'l_foot_1_0', 'r_foot_1_0'}
        missing = required - set(mapping.values())
        if missing:
            raise ValueError('Assign the required reference bones in Rig Mapping: ' + ', '.join(sorted(missing)))


def export_workflow(context, directory: Path) -> Path:
    validate_export_setup(context)
    settings = state(context)
    prepare = (lambda model: write_external_profile(context, model)) if settings.prepare_victorytool else None
    with export_surfaces(context):
        return export_model_package(context, directory, settings.model_name, True, settings.platform, prepare)


WIZARD_STEPS = ('materials', 'rig', 'confirm')


def capture_rig_mapping(context) -> None:
    from ..animation.humanoid_avatar_addon import (
        capture_avatar_representation, update_validated_representation,
        update_pose_snapshot, bone_nodes, mesh_nodes,
    )
    settings = state(context)
    if settings.armature is None:
        return
    mapping = validate_mapping(context)
    rig = settings.armature
    profile = rig.level5_humanoid_avatar
    if not profile.has_representation:
        capture_avatar_representation(context, rig, refresh_reference_bones(context))
    update_pose_snapshot(profile, rig, bone_nodes(rig), mesh_nodes(context, rig))
    inverse = {joint: bone for bone, joint in mapping.items()}
    for role in profile.roles:
        role.source_bone = inverse.get(role.native_joint, '')
        role.ambiguous_candidates = ''
    role_bones = {role.source_bone for role in profile.roles if role.source_bone}
    auxiliary_bones = {auxiliary.source_bone for auxiliary in profile.auxiliaries}
    for name in mapping.keys() - role_bones - auxiliary_bones:
        bone = rig.data.bones[name]
        auxiliary = profile.auxiliaries.add()
        auxiliary.source_bone = name
        auxiliary.parent_bone = bone.parent.name if bone.parent else ''
        auxiliary.category = 'other'
        auxiliary.chain_names = '[]'
    for auxiliary in profile.auxiliaries:
        auxiliary.native_joint = mapping.get(auxiliary.source_bone, '') if auxiliary.source_bone not in role_bones else ''
    profile.native_joints.clear()
    for name in sorted(refresh_reference_bones(context)):
        profile.native_joints.add().name = name
    update_validated_representation(context, rig, profile)


def advance_wizard(context, direction: int) -> None:
    settings = state(context)
    index = WIZARD_STEPS.index(settings.step)
    if direction not in {-1, 1} or not 0 <= index + direction < len(WIZARD_STEPS):
        raise ValueError('This wizard step is not available')
    if direction > 0:
        if settings.step == 'materials':
            if not any(row.included for row in settings.surfaces):
                raise ValueError('Include at least one submesh')
            synchronize_rig(context)
        elif settings.step == 'rig':
            validate_mapping(context)
            capture_rig_mapping(context)
    settings.step = WIZARD_STEPS[index + direction]
    settings.error = ''


def wizard_back(self, context) -> None:
    if self.previous:
        self.previous = False
        advance_wizard(context, -1)


def wizard_auto_assign(self, context) -> None:
    if self.auto_assign:
        self.auto_assign = False
        bpy.ops.level5_g4_export.auto_assign('EXEC_DEFAULT')


def wizard_validate_rig(self, context) -> None:
    if self.validate_rig:
        self.validate_rig = False
        bpy.ops.level5_g4_export.validate_rig('EXEC_DEFAULT')


def preview_expression(context, cell: int) -> None:
    from .g4_port_addon import apply_expression_preview
    from .g4_port_addon import EXPRESSION_PREVIEW_SOURCE_UV_KEY
    targets = {}
    uv_names = {}
    for surface in state(context).surfaces:
        if not surface.included or not surface.expression_atlas or surface.object is None:
            continue
        obj = surface.object
        uv = obj.data.uv_layers.get(surface.uv_map) if surface.uv_map else next(
            (layer for layer in obj.data.uv_layers if layer.active_render), obj.data.uv_layers.active)
        if uv is None:
            raise ValueError(f'{surface.name}: select an available base texture UV map')
        previous = uv_names.setdefault(obj, uv.name)
        if previous != uv.name:
            raise ValueError('Expression surfaces on one object must use the same UV map')
        backup_source = obj.get(EXPRESSION_PREVIEW_SOURCE_UV_KEY, '')
        if backup_source and backup_source != uv.name:
            raise ValueError('Restore Original UVs before changing the expression UV map')
        targets.setdefault((obj, uv.name), set()).add(surface.slot)
    if not targets:
        raise ValueError('Select a mesh surface using a 4×2 expression atlas')
    if not apply_expression_preview(cell, [(obj, slots, uv) for (obj, uv), slots in targets.items()]):
        raise ValueError('The selected expression surfaces require a UV map')


def wizard_expression(self, context) -> None:
    try:
        preview_expression(context, int(self.expression))
    except ValueError as error:
        state(context).error = str(error)
    else:
        state(context).error = ''


def wizard_restore_expression(self, context) -> None:
    if self.restore_expression:
        self.restore_expression = False
        from .g4_port_addon import restore_expression_preview
        restore_expression_preview()


def wizard_refresh(self, context) -> None:
    if self.refresh_model:
        self.refresh_model = False
        try:
            initialize_workflow(context)
        except (OSError, ValueError) as error:
            state(context).error = str(error)


def open_wizard() -> None:
    from ..animation.g4_animation_addon import defer_blender_call
    defer_blender_call(lambda: bpy.ops.level5_g4_export.wizard('INVOKE_DEFAULT'))


class G4ExportBegin(Operator):
    bl_idname = 'level5_g4_export.begin'
    bl_label = 'Export model'
    def invoke(self, context, event):
        try:
            visible = {obj for obj in context.scene.objects if obj.type == 'MESH' and obj.visible_get()}
            listed = {surface.object for surface in state(context).surfaces}
            if not listed or listed != visible:
                initialize_workflow(context)
        except (OSError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        refresh_reference_bones(context)
        for surface in state(context).surfaces:
            assigned_image_changed(surface, context)
            if surface.expression_atlas:
                ensure_expression_group(surface, context)
        open_wizard()
        return {'FINISHED'}


def draw_image_tile(layout, owner, prop: str, label: str) -> None:
    tile = layout.column(align=True)
    tile.alignment = 'LEFT'
    preview = 'preview' if prop=='image' else prop.replace('_image', '_preview')
    button = tile.row()
    button.alignment = 'LEFT'
    button.template_icon_view(owner, preview, show_labels=False, scale=5, scale_popup=4)
    tile.label(text=label if getattr(owner, prop) else f'{label} (Empty)', translate=False)


def draw_materials(layout, context) -> None:
    settings = state(context)
    layout.label(text='Select which meshes to include in this model', translate=False)
    for surface in settings.surfaces:
        box = layout.box()
        row = box.row(align=True)
        row.prop(surface, 'included', text='')
        row.label(text=surface.name)
        row.prop(surface, 'expanded', text='', icon='TRIA_DOWN' if surface.expanded else 'TRIA_RIGHT', emboss=False)
        if not surface.expanded:
            continue
        column = box.column()
        column.enabled = surface.included
        owner = expression_group(surface, context) if surface.expression_atlas else surface
        column.prop(owner, 'shader', text='Shader')
        slots = [('base_image','Base Texture'), ('occlusion_image','Occlusion Texture'), ('line_image','Line Texture')]
        if owner.shader=='Chr_ToonNrm':
            slots.insert(1, ('normal_image','Normal Texture'))
        grid = column.grid_flow(row_major=True, columns=2, even_columns=True, even_rows=True)
        for prop, label in slots:
            cell = grid.column()
            cell.enabled = not (surface.expression_atlas and owner.build_atlas and prop=='base_image')
            draw_image_tile(cell, owner, prop, label)
        column.prop(owner, 'use_outline')
        row = column.row(align=True)
        row.prop(surface, 'expression_atlas')
        choice = row.row()
        choice.enabled = surface.expression_atlas
        choice.prop(surface, 'expression_kind', text='')
        if surface.expression_atlas:
            column.prop(owner, 'build_atlas')
            if owner.build_atlas:
                grid = column.grid_flow(row_major=True, columns=4, even_columns=True, even_rows=True)
                for index, cell in enumerate(owner.cells):
                    draw_image_tile(grid, cell, 'image', str(index+1))


class G4ExportChooseImage(Operator):
    bl_idname = 'level5_g4_export.choose_image'
    bl_label = 'Choose texture'
    surface_index: IntProperty()
    owner_path: StringProperty()
    slot: EnumProperty(items=[('base_image', 'Base Texture', ''), ('normal_image', 'Normal Texture', ''),
                              ('line_image', 'Line Texture', ''), ('occlusion_image', 'Occlusion Texture', ''), ('image', 'Expression', '')])
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420, confirm_text='Use image')
    def draw(self, context):
        owner = context.scene.path_resolve(self.owner_path) if self.owner_path else state(context).surfaces[self.surface_index]
        self.layout.template_ID(owner, self.slot, open='image.open')
    def execute(self, context):
        return {'FINISHED'}


class G4ExportAutoAssign(Operator):
    bl_idname = 'level5_g4_export.auto_assign'
    bl_label = 'Auto assign'
    def execute(self, context):
        from ..animation.humanoid_avatar import suggest_native_mapping, incompatible_body_assignments
        from ..animation.humanoid_avatar_addon import bone_nodes
        settings = state(context)
        try:
            names = refresh_reference_bones(context)
            suggestions = suggest_native_mapping(bone_nodes(settings.armature), names)
            inverse = {bone: joint for joint, bone in suggestions.assignments.items()}
            current = {bone.reference: bone.name for bone in settings.bones if bone.reference}
            invalid = incompatible_body_assignments(bone_nodes(settings.armature), current)
            for bone in settings.bones:
                if bone.reference in invalid:
                    bone.reference = ''
            used = {b.reference for b in settings.bones if b.reference}
            for bone in settings.bones:
                suggested = inverse.get(bone.name)
                if not bone.reference and suggested and suggested not in used:
                    bone.reference = suggested
                    used.add(suggested)
        except (OSError, ValueError) as error:
            settings.error = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        settings.error = ''
        return {'FINISHED'}


class G4ExportValidateRig(Operator):
    bl_idname = 'level5_g4_export.validate_rig'
    bl_label = 'Validate Rig'
    def execute(self, context):
        try:
            capture_rig_mapping(context)
        except (OSError, ValueError) as error:
            state(context).error = str(error)
            return {'CANCELLED'}
        state(context).error = ''
        return {'FINISHED'}


def draw_rig(layout, context, wizard) -> None:
    settings = state(context)
    if settings.armature is None:
        layout.label(text='No armature detected. Static models do not need mapping.', icon='INFO')
    else:
        if settings.reference_error:
            layout.label(text=settings.reference_error, icon='ERROR')
        row = layout.row(align=True)
        row.prop(wizard, 'auto_assign', text='Auto assign', toggle=True)
        row.prop(wizard, 'validate_rig', text='Validate Rig', icon='CHECKMARK', toggle=True)
        hidden = set()
        for bone in settings.bones:
            if bone.parent in hidden:
                hidden.add(bone.name)
                continue
            split = layout.split(factor=.5)
            row = split.row(align=True)
            row.prop(bone, 'expanded', text='', icon='TRIA_DOWN' if bone.expanded else 'TRIA_RIGHT', emboss=False)
            row.label(text='  ' * bone.depth + bone.name)
            split.prop_search(bone, 'reference', settings, 'references', text='')
            if not bone.expanded:
                hidden.add(bone.name)

def draw_confirmation(layout, context) -> None:
    settings = state(context)
    layout.label(text=f'{sum(s.included for s in settings.surfaces)} submeshes')
    layout.prop(settings, 'model_name')
    layout.prop(settings, 'prepare_victorytool')
    if settings.prepare_victorytool:
        layout.label(text='Animation banks are exported in VictoryTool.', icon='INFO')
    layout.prop(settings, 'platform')


class G4ExportWizard(Operator):
    bl_idname = 'level5_g4_export.wizard'
    bl_label = 'Export model'
    refresh_model: BoolProperty(name='Refresh model', update=wizard_refresh, options={'SKIP_SAVE'})
    previous: BoolProperty(name='Back', update=wizard_back, options={'SKIP_SAVE'})
    auto_assign: BoolProperty(update=wizard_auto_assign, options={'SKIP_SAVE'})
    validate_rig: BoolProperty(update=wizard_validate_rig, options={'SKIP_SAVE'})
    expression: EnumProperty(items=[(str(i), str(i), '') for i in range(1, 9)], update=wizard_expression, options={'SKIP_SAVE'})
    restore_expression: BoolProperty(update=wizard_restore_expression, options={'SKIP_SAVE'})
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=760, confirm_text='Next')
    def check(self, context):
        return True
    def draw(self, context):
        settings = state(context)
        index = WIZARD_STEPS.index(settings.step)
        title = ('Meshes & Materials', 'Rig', 'Confirm Export')[index]
        header = self.layout.row()
        header.label(text=f'{index + 1} / 3 · {title}')
        if index > 0:
            header.prop(self, 'previous', text='Back', icon='BACK', toggle=True)
        if index == 0:
            header.prop(self, 'refresh_model', text='', icon='FILE_REFRESH', toggle=True)
        self.layout.separator()
        if settings.step == 'rig':
            draw_rig(self.layout, context, self)
        else:
            (draw_materials if index == 0 else draw_confirmation)(self.layout, context)
        if settings.error:
            self.layout.label(text=settings.error, icon='ERROR')
    def execute(self, context):
        try:
            if state(context).step != 'confirm':
                advance_wizard(context, 1)
                open_wizard()
            else:
                validate_export_setup(context)
                from ..animation.g4_animation_addon import defer_blender_call
                defer_blender_call(lambda: bpy.ops.level5_g4_export.folder('INVOKE_DEFAULT'))
        except (OSError, ValueError) as error:
            state(context).error = str(error)
            open_wizard()
            return {'CANCELLED'}
        return {'FINISHED'}


class G4ExportFolder(Operator):
    bl_idname = 'level5_g4_export.folder'
    bl_label = 'Export model'
    directory: StringProperty(subtype='DIR_PATH')
    filter_folder: BoolProperty(default=True, options={'HIDDEN'})
    def invoke(self, context, event):
        try:
            validate_export_setup(context)
        except (OSError, ValueError) as error:
            state(context).error = str(error)
            self.report({'ERROR'}, str(error))
            open_wizard()
            return {'CANCELLED'}
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    def cancel(self, context):
        open_wizard()
    def execute(self, context):
        try:
            if not self.directory:
                raise ValueError('Choose an output folder')
            output = export_workflow(context, Path(bpy.path.abspath(self.directory)))
        except (OSError, ValueError, RuntimeError) as error:
            self.report({'ERROR'}, str(error))
            open_wizard()
            return {'CANCELLED'}
        self.report({'INFO'}, f'Model exported to {output}')
        return {'FINISHED'}


class G4ExportPanel(Panel):
    bl_idname = 'LEVEL5_G4_EXPORT_PT_panel'
    bl_label = 'Export model'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Level-5'
    bl_order = -1
    def draw(self, context):
        self.layout.operator(G4ExportBegin.bl_idname, text='Export model', icon='EXPORT')


CLASSES = (G4ExportAtlasCell, G4ExportExpressionGroup, G4ExportSurface, G4ExportBone, G4ExportReference, G4ExportState,
           G4ExportBegin, G4ExportChooseImage, G4ExportAutoAssign, G4ExportValidateRig, G4ExportWizard, G4ExportFolder, G4ExportPanel)


def register() -> None:
    from .g4_port_addon import register_class_replacing_stale
    for cls in CLASSES:
        register_class_replacing_stale(cls)
    bpy.types.Scene.level5_g4_export = PointerProperty(type=G4ExportState)


def unregister() -> None:
    global _image_previews
    _texture_menu_items.clear()
    if _image_previews is not None:
        bpy.utils.previews.remove(_image_previews)
        _image_previews = None
    del bpy.types.Scene.level5_g4_export
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
