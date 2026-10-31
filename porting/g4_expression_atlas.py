"""Expression atlas preparation on disposable export images and meshes."""
from array import array
from collections.abc import Sequence
from math import isfinite

import bpy

from .g4_port_addon import blit_image_fit, image_pixels


def build_expression_atlas(
    images: Sequence[bpy.types.Image | None], name: str, data_texture: bool,
) -> bpy.types.Image:
    """Place eight images in reading order in a 4 by 2 atlas."""
    if len(images) != 8:
        raise ValueError('An expression atlas requires exactly eight image slots.')
    first = next((image for image in images if image is not None), None)
    width, height = (int(value) for value in first.size) if first else (32, 32)
    if width <= 0 or height <= 0:
        raise ValueError(f'Expression image {first.name!r} has no readable pixels.')
    scale = min(1.0, 8192 / (width * 4), 8192 / (height * 2))
    cell_width, cell_height = max(1, int(width * scale)), max(1, int(height * scale))
    atlas_width, atlas_height = cell_width * 4, cell_height * 2
    pixels = image_pixels(atlas_width, atlas_height, (0.0, 0.0, 0.0, 0.0))
    sources: dict[int, tuple[int, int, array]] = {}
    for index, image in enumerate(images):
        image = image if image is not None else first
        if image is None:
            source = (1, 1, image_pixels(1, 1, (1.0, 1.0, 1.0, 1.0)))
        else:
            key = image.as_pointer()
            if key not in sources:
                source_width, source_height = (int(value) for value in image.size)
                if source_width <= 0 or source_height <= 0 or len(image.pixels) != source_width * source_height * 4:
                    raise ValueError(f'Expression image {image.name!r} has no readable RGBA pixels.')
                source_pixels = array('f', [0.0]) * (source_width * source_height * 4)
                image.pixels.foreach_get(source_pixels)
                if not data_texture and not image.is_float:
                    import PyOpenColorIO as ocio
                    processor = ocio.GetCurrentConfig().getProcessor(
                        image.colorspace_settings.name, ocio.ROLE_SCENE_LINEAR,
                    ).getDefaultCPUProcessor()
                    processor.applyRGBA(source_pixels)
                sources[key] = source_width, source_height, source_pixels
            source = sources[key]
        blit_image_fit(pixels, atlas_width, atlas_height, source,
                       (index % 4) * cell_width, (1 - index // 4) * cell_height,
                       cell_width, cell_height)
    atlas = bpy.data.images.new(name, width=atlas_width, height=atlas_height,
                                alpha=True, float_buffer=True)
    try:
        if data_texture:
            atlas.colorspace_settings.name = 'Non-Color'
        atlas.pixels.foreach_set(pixels)
        atlas.update()
    except Exception:
        bpy.data.images.remove(atlas)
        raise
    return atlas


def fit_expression_uv(mesh: bpy.types.Mesh, source_uv: str, output_uv: str, build: bool) -> str:
    """Copy UVs, optionally fitting them into the first top atlas cell."""
    source = mesh.uv_layers.get(source_uv) if source_uv else next(
        (layer for layer in mesh.uv_layers if layer.active_render), mesh.uv_layers.active,
    )
    if source is None:
        raise ValueError(f'Mesh {mesh.name!r} has no expression UV map {source_uv!r}.')
    coordinates = [tuple(loop.uv) for loop in source.data]
    if any(not isfinite(value) or value < 0.0 or value > 1.0
           for coordinate in coordinates for value in coordinate):
        raise ValueError(f'Expression UV map {source.name!r} must contain finite coordinates within 0 to 1.')
    output = mesh.uv_layers.get(output_uv)
    if output is None:
        output = mesh.uv_layers.new(name=output_uv)
    for loop, (u, v) in zip(output.data, coordinates):
        loop.uv = (u * .25, v * .5 + .5) if build else (u, v)
    return output.name


def merge_expression_surfaces(
    context: bpy.types.Context, objects: list[bpy.types.Object], name: str,
    material: bpy.types.Material,
) -> bpy.types.Object:
    """Consume temporary meshes, retaining world geometry and named bone weights."""
    if not objects or any(obj.type != 'MESH' for obj in objects):
        raise ValueError('Expression surfaces must contain at least one mesh object.')
    if context.mode != 'OBJECT':
        raise ValueError('Expression surfaces must be merged in Object Mode.')
    armatures = [[modifier for modifier in obj.modifiers if modifier.type == 'ARMATURE']
                 for obj in objects]
    signatures = [tuple((modifier.object, modifier.use_vertex_groups, modifier.use_bone_envelopes,
                         modifier.use_deform_preserve_volume, modifier.vertex_group,
                         modifier.invert_vertex_group, modifier.use_multi_modifier,
                         modifier.show_viewport, modifier.show_render)
                        for modifier in modifiers) for modifiers in armatures]
    if any(signature != signatures[0] for signature in signatures[1:]):
        raise ValueError('Expression surfaces must use matching armature modifiers and settings.')
    for obj, rig_modifiers in zip(objects, armatures):
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        visibility = [modifier.show_viewport for modifier in rig_modifiers]
        try:
            for modifier in rig_modifiers:
                modifier.show_viewport = False
            with context.temp_override(object=obj, active_object=obj,
                                       selected_objects=[obj], selected_editable_objects=[obj]):
                for modifier in list(obj.modifiers):
                    if modifier.type == 'ARMATURE':
                        continue
                    if modifier.show_viewport:
                        bpy.ops.object.modifier_apply(modifier=modifier.name)
                    else:
                        obj.modifiers.remove(modifier)
        finally:
            for modifier, visible in zip(rig_modifiers, visibility):
                modifier.show_viewport = visible
        obj.data.materials.clear()
        obj.data.materials.append(material)
        for polygon in obj.data.polygons:
            polygon.material_index = 0
    active = objects[0]
    if len(objects) > 1:
        with context.temp_override(object=active, active_object=active,
                                   selected_objects=objects, selected_editable_objects=objects):
            bpy.ops.object.join()
    active.name = name
    active['g4_export_mesh_name'] = name
    return active
