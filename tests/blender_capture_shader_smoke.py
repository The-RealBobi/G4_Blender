"""Run with Blender --factory-startup --background --python this_file."""
import sys
from pathlib import Path
import tempfile

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import G4_Blender as addon


with tempfile.TemporaryDirectory() as directory:
    image = bpy.data.images.new("Capture test base", width=2, height=2)
    image.generated_color = (0.5, 0.25, 0.1, 1.0)
    path = Path(directory) / "test_10.png"
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    material = bpy.data.materials.new("Capture test")
    material.use_nodes = True
    assert addon.apply_level5_toon_shader(material, path, {})
    nodes = material.node_tree.nodes
    assert nodes["G4 Saturation"].inputs["Saturation"].default_value == 1.0
    assert nodes["G4 Saturation"].inputs["Value"].default_value == 1.0
    assert nodes.get("G4 Painted Occlusion Composite") is None
    assert nodes["G4 Shadow Threshold 0"].outputs[0].default_value == 0.5
    assert abs(nodes["G4 Shadow Threshold 1"].outputs[0].default_value - 0.54) < 1e-5
    assert nodes["G4 Shadow Color 0"].type == "RGB"
    assert nodes["G4 Shadow Color 1"].type == "RGB"
    assert nodes.get("G4 Edge Data") is not None

from G4_Blender.shading.character_outline import configure_screen_outline, remove_screen_outline, _compositor_tree

scene = bpy.context.scene
tree = _compositor_tree(scene)
source = next(n for n in tree.nodes if n.type == "R_LAYERS")
original = source.outputs["Image"]
destinations = [link.to_socket for link in original.links]
assert configure_screen_outline(scene, bpy.context.view_layer, 1.0)
assert configure_screen_outline(scene, bpy.context.view_layer, 1.0)
assert len([n for n in tree.nodes if n.get("g4_screen_outline")]) == 1
assert all(socket.links[0].from_node.get("g4_screen_outline") for socket in destinations)
assert len([a for a in bpy.context.view_layer.aovs if a.name == "G4 Edge Data"]) == 1
remove_screen_outline(scene)
assert all(socket.links[0].from_socket == source.outputs["Image"] for socket in destinations)
assert not any(n.get("g4_screen_outline") for n in tree.nodes)
print("CAPTURE_SHADER_SMOKE_OK")

# Exercise the real dialog validation with skin_color preceding an invalid path.
from types import SimpleNamespace
from G4_Blender import g4_animation_addon as animation
item = SimpleNamespace(head_model="", body_model="", shoes_model="", accessory_model="",
                       skin_color=[1.0, .6, .4], gloves_model="missing-gloves.g4pkm",
                       armband_model="", nameplate_model="", attach_ball=False, ball_model="")
reports = []
operator = SimpleNamespace(parts=[item], report=lambda level, message: reports.append(message))
assert animation.IMPORT_OT_level5_g4_event_parts.execute(operator, bpy.context) == {"CANCELLED"}
assert len(reports) == 1 and "missing-gloves.g4pkm" in reports[0]
print("EVENT_PART_COLOR_VALIDATION_OK")
