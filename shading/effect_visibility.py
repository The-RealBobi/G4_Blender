"""Skip rasterizing native effect faces whose material opacity is exactly zero."""
import bpy


MODIFIER_NAME = "Effect Opacity Culling"
OPACITY_PATH = 'nodes["Effect Diffuse A"].outputs[0].default_value'
FAMILIES = {"T1_STATIC", "T1M1_STATIC", "THRESHOLD_STATIC", "T3_THRESHOLD"}


def _opacity_source(material, scene):
    if material is None or material.get("g4_effect_preview") not in FAMILIES:
        return None
    tree = material.node_tree
    if tree is None:
        return None
    for node in tree.nodes:
        if node.get("g4_scene_animation_socket") == OPACITY_PATH:
            key = node.attribute_name
            if node.attribute_type == 'VIEW_LAYER' and key in scene:
                return scene, f'["{key}"]'
            return None
    node = tree.nodes.get("Effect Diffuse A")
    if node is not None and node.bl_idname == 'ShaderNodeValue':
        return material, 'node_tree.' + OPACITY_PATH
    return None


def _visibility_group(count: int):
    for group in bpy.data.node_groups:
        if group.get('g4_effect_opacity_schema') == 1 and group.get('g4_effect_opacity_slots') == count:
            return group
    group = bpy.data.node_groups.new(f"Effect Opacity Culling {count}", 'GeometryNodeTree')
    group.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    group.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    for index in range(count):
        socket = group.interface.new_socket(name=f'Opacity {index}', in_out='INPUT', socket_type='NodeSocketFloat')
        socket.default_value = 1.0
        socket.hide_in_modifier = True
    nodes, links = group.nodes, group.links
    source = nodes.new('NodeGroupInput')
    output = nodes.new('NodeGroupOutput')
    index = nodes.new('GeometryNodeInputMaterialIndex')
    select = nodes.new('GeometryNodeIndexSwitch')
    select.data_type = 'FLOAT'
    select.index_switch_items.clear()
    for _ in range(count):
        select.index_switch_items.new()
    links.new(index.outputs[0], select.inputs['Index'])
    for slot in range(count):
        links.new(source.outputs[f'Opacity {slot}'], select.inputs[slot + 1])
    zero = nodes.new('FunctionNodeCompare')
    zero.data_type = 'FLOAT'
    zero.operation = 'LESS_EQUAL'
    zero.inputs[1].default_value = 0.0
    # No epsilon: even a small positive opacity must remain visible.
    links.new(select.outputs[0], zero.inputs[0])
    delete = nodes.new('GeometryNodeDeleteGeometry')
    delete.domain = 'FACE'
    delete.mode = 'ALL'
    links.new(source.outputs['Geometry'], delete.inputs['Geometry'])
    links.new(zero.outputs[0], delete.inputs['Selection'])
    links.new(delete.outputs['Geometry'], output.inputs['Geometry'])
    group['g4_effect_opacity_schema'] = 1
    group['g4_effect_opacity_slots'] = count
    return group


def _remove_culling(obj, modifier) -> None:
    prefix = modifier.path_from_id()
    if obj.animation_data:
        for curve in tuple(obj.animation_data.drivers):
            if curve.data_path.startswith((prefix + '.', prefix + '[')):
                obj.driver_remove(curve.data_path)
    obj.modifiers.remove(modifier)


def configure_effect_opacity_culling(scene: bpy.types.Scene) -> int:
    # Index Switch is unavailable in Blender 4.0; retain its existing rendering.
    if not hasattr(bpy.types, 'GeometryNodeIndexSwitch') or scene.library:
        return 0
    configured = 0
    for obj in scene.objects:
        if obj.library:
            continue
        modifier = next((item for item in obj.modifiers if item.type == 'NODES'
                         and item.node_group and item.node_group.get('g4_effect_opacity_schema')), None)
        # Native split normals survive face removal without changing the shading
        # of adjacent visible surfaces. Leave generated particles and user meshes alone.
        if obj.type != 'MESH' or len(obj.users_scene) != 1 or not obj.data.has_custom_normals:
            if modifier is not None:
                _remove_culling(obj, modifier)
            continue
        if any(modifier.type == 'NODES' and modifier.node_group
               and modifier.node_group.get('g4_particle_geometry') for modifier in obj.modifiers):
            if modifier is not None:
                _remove_culling(obj, modifier)
            continue
        sources = [_opacity_source(slot.material, scene) for slot in obj.material_slots]
        if not any(sources) or any(face.material_index >= len(sources) for face in obj.data.polygons):
            if modifier is not None:
                _remove_culling(obj, modifier)
            continue
        group = _visibility_group(len(sources))
        if modifier is not None and modifier.node_group != group:
            _remove_culling(obj, modifier)
            modifier = None
        if modifier is None:
            modifier = obj.modifiers.new(MODIFIER_NAME, 'NODES')
            configured += 1
        modifier.node_group = group
        inputs = {item.name: item.identifier for item in group.interface.items_tree
                  if item.item_type == 'SOCKET' and item.in_out == 'INPUT'}
        for index, source in enumerate(sources):
            identifier = inputs[f'Opacity {index}']
            path = f'["{identifier}"]'
            if bpy.app.version >= (5, 0, 0):
                control = getattr(modifier.properties.inputs, identifier)
                path = 'value'
                control.value = 1.0
            else:
                control = modifier
                modifier[identifier] = 1.0
            control.driver_remove(path)
            if source is None:
                continue
            owner, data_path = source
            driver = control.driver_add(path).driver
            # Drive a boolean so tiny positive opacity is never rounded to zero
            # by the modifier's floating-point change detection.
            driver.type = 'SCRIPTED'
            driver.expression = 'opacity != 0.0'
            variable = driver.variables.new()
            variable.name = 'opacity'
            variable.type = 'SINGLE_PROP'
            target = variable.targets[0]
            target.id_type = 'SCENE' if isinstance(owner, bpy.types.Scene) else 'MATERIAL'
            target.id = owner
            target.data_path = data_path
    return configured
