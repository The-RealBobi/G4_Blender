"""Cached GPU surface-data pass for Game outlines in shaded viewports."""
import bpy
import bmesh
import gpu
import numpy as np
from bpy.app.handlers import persistent
from gpu_extras.batch import batch_for_shader

_handler = None
_surfaces = {}
_topology = {}
_targets = {}
_shaders = None
_enabled = set()
_authored_enabled = set()


def _programs():
    global _shaders
    if _shaders is not None:
        return _shaders
    vary = gpu.types.GPUStageInterfaceInfo('g4_outline_surface')
    vary.smooth('VEC3', 'viewPosition')
    vary.smooth('VEC3', 'viewNormal')
    vary.smooth('VEC4', 'parameters')
    info = gpu.types.GPUShaderCreateInfo()
    info.vertex_in(0, 'VEC3', 'position')
    info.vertex_in(1, 'VEC3', 'normal')
    info.vertex_in(2, 'VEC4', 'color')
    info.push_constant('MAT4', 'modelView')
    info.push_constant('MAT4', 'projection')
    info.vertex_out(vary)
    info.fragment_out(0, 'VEC4', 'surface')
    info.vertex_source('''
void main() {
    vec4 p = modelView * vec4(position, 1.0);
    viewPosition = p.xyz;
    viewNormal = transpose(inverse(mat3(modelView))) * normal;
    parameters = color;
    gl_Position = projection * p;
}''')
    info.fragment_source('''
void main() {
    vec3 incoming = abs(projection[3][3]) > 0.5 ? vec3(0.0, 0.0, 1.0) : normalize(-viewPosition);
    float facing = dot(normalize(viewNormal), incoming);
    surface = vec4(parameters.g, parameters.r, facing, parameters.b);
}''')
    surface = gpu.shader.create_from_info(info)
    info = gpu.types.GPUShaderCreateInfo()
    info.vertex_in(0, 'VEC2', 'position')
    info.sampler(0, 'FLOAT_2D', 'surfaceData')
    info.sampler(1, 'FLOAT_2D', 'surfaceDepth')
    info.push_constant('VEC2', 'imageSize')
    info.push_constant('FLOAT', 'width')
    info.push_constant('VEC2', 'viewportOrigin')
    info.push_constant('MAT4', 'inverseProjection')
    info.fragment_out(0, 'VEC4', 'ink')
    info.vertex_source('void main() { gl_Position = vec4(position, 0.0, 1.0); }')
    info.fragment_source('''
float viewDepth(vec2 uv, float depth) {
    vec4 p = inverseProjection * vec4(uv * 2.0 - 1.0, depth * 2.0 - 1.0, 1.0);
    return log(max(abs(p.z / p.w), 0.001));
}
void main() {
    vec2 uv = (gl_FragCoord.xy - viewportOrigin) / imageSize;
    vec4 center = texture(surfaceData, uv);
    if (center.a <= 0.0039) { ink = vec4(0.0); return; }
    float z = viewDepth(uv, texture(surfaceDepth, uv).r);
    float seam = 0.0, normalEdge = 0.0, silhouette = 0.0, mask = 0.0;
    vec2 offsets[4] = vec2[4](vec2(1,0), vec2(-1,0), vec2(0,1), vec2(0,-1));
    for (int i = 0; i < 4; ++i) {
        vec2 sampleUV = uv + offsets[i] * width / imageSize;
        vec4 other = texture(surfaceData, sampleUV);
        seam = max(seam, abs(center.r - other.r));
        mask += abs(center.g - other.g);
        normalEdge = max(normalEdge, abs(center.b - other.b));
        silhouette = max(silhouette, abs(z-viewDepth(sampleUV, texture(surfaceDepth, sampleUV).r)));
    }
    seam = clamp(seam * 5.0, 0.0, 1.0);
    normalEdge = clamp((normalEdge-0.30)*8.0, 0.0, 1.0) * clamp((0.25-mask*0.5)*256.0, 0.0, 1.0);
    silhouette = clamp((silhouette-0.015)*40.0, 0.0, 1.0);
    float edge = max(seam, max(normalEdge, silhouette)) * clamp(center.a*0.616, 0.0, 1.0);
    ink = vec4(0.0, 0.0, 0.0, 1.0-pow(1.0-edge, 2.2));
}''')
    screen = gpu.shader.create_from_info(info)
    quad = batch_for_shader(screen, 'TRIS', {'position': [(-1,-1),(3,-1),(-1,3)]})
    _shaders = surface, screen, quad
    return _shaders


def _surface(obj, depsgraph, shader, outlined):
    key = obj.as_pointer()
    cached = _surfaces.get(key)
    if cached is not None and cached[0] == outlined:
        return cached[1]
    evaluated = obj.evaluated_get(depsgraph)
    temporary = obj.mode == 'EDIT'
    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph) if temporary else evaluated.data
    try:
        if not mesh.loops:
            return None
        counts = (len(mesh.vertices), len(mesh.loops), len(mesh.polygons), outlined)
        stable = all(m.type == 'ARMATURE' or
                     (m.type == 'NODES' and m.node_group and m.node_group.get('g4_parameter_schema') in {3, 4})
                     for m in obj.modifiers if m.show_viewport)
        topology = _topology.get(key) if stable and obj.mode == 'OBJECT' else None
        if topology is None or topology[0] != counts:
            mesh.calc_loop_triangles()
            indices = np.empty(len(mesh.loops), dtype=np.int32)
            mesh.loops.foreach_get('vertex_index', indices)
            color = np.zeros((len(mesh.loops), 4), dtype=np.float32)
            attribute = mesh.color_attributes.get('G4 Outline Parameters')
            if outlined and attribute is not None:
                values = np.empty((len(attribute.data), 4), dtype=np.float32)
                attribute.data.foreach_get('color', values.ravel())
                color[:] = values[indices] if attribute.domain == 'POINT' else values
            triangles = np.empty((len(mesh.loop_triangles), 3), dtype=np.int32)
            mesh.loop_triangles.foreach_get('loops', triangles.ravel())
            color_format = gpu.types.GPUVertFormat()
            color_format.attr_add(id='color', comp_type='F32', len=4, fetch_mode='FLOAT')
            colors = gpu.types.GPUVertBuf(color_format, len(color))
            colors.attr_fill('color', color)
            elements = gpu.types.GPUIndexBuf(type='TRIS', seq=triangles)
            topology = counts, indices, colors, elements
            if stable:
                _topology[key] = topology
        _, indices, colors, elements = topology
        vertices = np.empty((len(mesh.vertices), 3), dtype=np.float32)
        mesh.vertices.foreach_get('co', vertices.ravel())
        normals = np.empty((len(mesh.loops), 3), dtype=np.float32)
        mesh.corner_normals.foreach_get('vector', normals.ravel())
        vertex_format = gpu.types.GPUVertFormat()
        vertex_format.attr_add(id='position', comp_type='F32', len=3, fetch_mode='FLOAT')
        vertex_format.attr_add(id='normal', comp_type='F32', len=3, fetch_mode='FLOAT')
        geometry = gpu.types.GPUVertBuf(vertex_format, len(indices))
        geometry.attr_fill('position', vertices[indices])
        geometry.attr_fill('normal', normals)
        batch = gpu.types.GPUBatch(type='TRIS', buf=geometry, elem=elements)
        batch.vertbuf_add(colors)
        cached = batch
        _surfaces[key] = outlined, cached
        return cached
    finally:
        if temporary:
            evaluated.to_mesh_clear()


def surface_pass(objects, depsgraph, view, projection, size, key, outlined_objects=None):
    surface, _, _ = _programs()
    target = _targets.get(key)
    if target is None or target[0] != size:
        data = gpu.types.GPUTexture(size, format='RGBA16F')
        depth = gpu.types.GPUTexture(size, format='DEPTH_COMPONENT32F')
        target = size, data, depth, gpu.types.GPUFrameBuffer(color_slots=data, depth_slot=depth)
        _targets[key] = target
    viewport = gpu.state.viewport_get()
    old_depth, old_write, old_blend = gpu.state.depth_test_get(), gpu.state.depth_mask_get(), gpu.state.blend_get()
    try:
        with target[3].bind():
            gpu.state.viewport_set(0, 0, *size)
            target[3].clear(color=(0,0,0,0), depth=1.0)
            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.depth_mask_set(True)
            gpu.state.blend_set('NONE')
            surface.bind()
            surface.uniform_float('projection', projection)
            for obj in objects:
                outlined = (obj.as_pointer() in outlined_objects if outlined_objects is not None else
                            any(m and m.get('g4_level5_toon') for m in obj.data.materials))
                cached = _surface(obj, depsgraph, surface, outlined)
                if cached is None:
                    continue
                matrix = view @ obj.evaluated_get(depsgraph).matrix_world
                surface.uniform_float('modelView', matrix)
                cached.draw(surface)
    finally:
        gpu.state.depth_test_set(old_depth)
        gpu.state.depth_mask_set(old_write)
        gpu.state.blend_set(old_blend)
        gpu.state.viewport_set(*viewport)
    return target[1], target[2]


def _draw_marks(context):
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    old_depth = gpu.state.depth_test_get()
    old_write = gpu.state.depth_mask_get()
    old_blend = gpu.state.blend_get()
    projection = context.region_data.window_matrix.copy()
    for column in range(4):
        projection[2][column] -= 0.0001 * projection[3][column]
    try:
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(False)
        gpu.state.blend_set('ALPHA')
        for obj in context.visible_objects:
            if obj.type != 'MESH':
                continue
            if obj.data.get('g4_outline_native') and not obj.data.get('g4_outline_authored') and obj.mode != 'EDIT':
                continue
            coordinates = []
            if obj.mode == 'EDIT':
                bm = bmesh.from_edit_mesh(obj.data)
                layer = bm.edges.layers.int.get('g4_outline_seam')
                if layer is not None:
                    coordinates = [tuple(vertex.co) for edge in bm.edges
                                   if edge[layer] and not edge.hide for vertex in edge.verts]
            else:
                evaluated = obj.evaluated_get(context.evaluated_depsgraph_get())
                mesh = evaluated.data
                marks = mesh.attributes.get('g4_outline_seam')
                if marks is not None and marks.domain == 'EDGE' and marks.data_type == 'INT':
                    coordinates = [tuple(mesh.vertices[index].co) for edge in mesh.edges
                                   if marks.data[edge.index].value and not edge.hide for index in edge.vertices]
            if not coordinates:
                continue
            with gpu.matrix.push_pop(), gpu.matrix.push_pop_projection():
                gpu.matrix.load_matrix(context.region_data.view_matrix @ obj.matrix_world)
                gpu.matrix.load_projection_matrix(projection)
                shader.bind()
                shader.uniform_float('viewportSize', (context.region.width, context.region.height))
                shader.uniform_float('lineWidth', 3.0 * context.scene.level5_g4_port.outline_width)
                shader.uniform_float('color', (1.0, 0.015, 0.015, 1.0))
                batch_for_shader(shader, 'LINES', {'pos': coordinates}).draw(shader)
    finally:
        gpu.state.depth_test_set(old_depth)
        gpu.state.depth_mask_set(old_write)
        gpu.state.blend_set(old_blend)


def _draw():
    context = bpy.context
    scene_key = context.scene.as_pointer()
    authored = scene_key in _authored_enabled
    game = scene_key in _enabled
    if (not game and not authored) or context.region_data is None:
        return
    if authored:
        _draw_marks(context)
    shading = context.space_data.shading.type
    if shading not in {'MATERIAL','RENDERED'} and not (authored and shading == 'SOLID'):
        return
    objects = [o for o in context.visible_objects if o.type == 'MESH' and
               not any(m and m.get('g4_effect_preview') for m in o.data.materials)]
    outlined_objects = {
        o.as_pointer() for o in objects
        if game and shading in {'MATERIAL', 'RENDERED'} and any(m and m.get('g4_level5_toon') for m in o.data.materials)
    }
    if not outlined_objects:
        return
    regions = {region.as_pointer() for window in context.window_manager.windows
               for area in window.screen.areas if area.type == 'VIEW_3D'
               for region in area.regions if region.type == 'WINDOW'}
    for key in tuple(_targets):
        if key not in regions:
            del _targets[key]
    size = (context.region.width, context.region.height)
    viewport = gpu.state.viewport_get()
    try:
        data, depth = surface_pass(objects, context.evaluated_depsgraph_get(), context.region_data.view_matrix,
                                   context.region_data.window_matrix, size, context.region.as_pointer(), outlined_objects)
        gpu.state.viewport_set(*viewport)
        _, shader, quad = _programs()
        old_blend, old_depth, old_write = gpu.state.blend_get(), gpu.state.depth_test_get(), gpu.state.depth_mask_get()
        try:
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            shader.bind()
            shader.uniform_sampler('surfaceData', data)
            shader.uniform_sampler('surfaceDepth', depth)
            shader.uniform_float('imageSize', size)
            shader.uniform_float('viewportOrigin', viewport[:2])
            shader.uniform_float('inverseProjection', context.region_data.window_matrix.inverted())
            shader.uniform_float('width', context.scene.get('g4_viewport_outline_width', 1.0))
            quad.draw(shader)
        finally:
            gpu.state.blend_set(old_blend)
            gpu.state.depth_test_set(old_depth)
            gpu.state.depth_mask_set(old_write)
    finally:
        gpu.state.viewport_set(*viewport)


@persistent
def invalidate_surfaces(_scene, depsgraph):
    for update in depsgraph.updates:
        if update.is_updated_shading and isinstance(update.id, bpy.types.Object):
            key = update.id.original.as_pointer()
            _surfaces.pop(key, None)
            _topology.pop(key, None)
        if update.is_updated_geometry:
            if isinstance(update.id, bpy.types.Object):
                _surfaces.pop(update.id.original.as_pointer(), None)
            elif isinstance(update.id, bpy.types.Mesh):
                _surfaces.clear()
                _topology.clear()
                break


def configure(scene, enabled, width):
    global _handler
    objects = {obj.as_pointer() for obj in bpy.data.objects}
    for cache in (_surfaces, _topology):
        for key in tuple(cache):
            if key not in objects:
                del cache[key]
    scene['g4_viewport_outline_width'] = width
    if enabled:
        _enabled.add(scene.as_pointer())
    else:
        _enabled.discard(scene.as_pointer())
    if _enabled or _authored_enabled:
        if not bpy.app.background and _handler is None:
            _handler = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_VIEW')
        if invalidate_surfaces not in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.append(invalidate_surfaces)
    else:
        unregister()


def configure_authored(scene, enabled):
    if enabled:
        _authored_enabled.add(scene.as_pointer())
    else:
        _authored_enabled.discard(scene.as_pointer())
    _surfaces.clear()
    _topology.clear()
    configure(scene, scene.as_pointer() in _enabled, scene.get('g4_viewport_outline_width', 1.0))


def unregister():
    global _handler, _shaders
    if _handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handler, 'WINDOW')
        _handler = None
    if invalidate_surfaces in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(invalidate_surfaces)
    _surfaces.clear()
    _topology.clear()
    _targets.clear()
    _enabled.clear()
    _authored_enabled.clear()
    _shaders = None
