# Level-5 G4 Blender Tools

Import models, characters, maps and textures from Level-5 G4-based games directly into Blender, and port edited Blender/DAE geometry back to conservative `G4MD/G4MG/G4TX` packages.

The repository ships one unified Blender add-on:

* `Level-5 G4 Blender Tools`: models, animation, cameras and port/export tools in one flat add-on package.

The importer automatically converts G4 assets into Blender-compatible data, recreating materials, textures and skeletons with minimal user interaction. The port exporter wraps the same parser/validator stack and patches a native model base conservatively instead of rebuilding unknown format tables from scratch.

<img src="img/img_02.png" alt="Stadium" width="750" height="auto" />

## Features

### Importer

* Direct import from Blender.
* Drag & drop support.
* Automatic texture extraction.
* Automatic material generation.
* Automatic texture assignment.
* Character rigging support.
* Manual body and shoes composition for character rigs.
* Shared skeleton resolution.
* G4SK rest bone orientation reconstruction.
* Full map importing.
* Batch processing support.
* Automatic scene reconstruction.
* Direct G4MT and animation G4PK import.
* G4CM camera import synchronized with character animation.
* Full event-folder reconstruction with one rig per actor and editable NLA cuts.

### Port Exporter

* Export edited Blender/DAE geometry into a native `G4MD/G4MG` pair.
* Preserve native layouts, materials, hashes, texture references and record structure where possible.
* Copy or rebuild `G4TX` texture archives from a native base.
* Resolve Collada skin controllers and Blender-exported weight sidecars.
* Validate generated records, palettes, indices and packed weight sums before writing packages.
* Build port settings from the selected original model instead of shipping model-specific bone presets.

## Supported Formats

| Format | Description             |
| ------ | ----------------------- |
| G4MD   | Model files             |
| G4PKM  | Packed model containers |
| G4SK   | Skeleton files          |
| G4TX   | Texture archives        |
| G4MT   | Character animation     |
| G4CM   | Camera animation        |
| G4PK   | Animation containers    |

## Installation

Install this repository as a single Blender add-on package. The root `__init__.py` is the only add-on entry point; helper scripts and lookup data live next to it at the same level.

The add-on package includes:

```text
|-- __init__.py
|-- g4_port_addon.py
|-- g4_port.py
|-- g4_model_probe.py
|-- g4_animation_addon.py
|-- g4mt_motion.py
|-- g4mt_probe.py
|-- g4cm_camera.py
|-- g4pk_extract_g4mt.py
`-- chara_model_lookup.json
```

In Blender, enable `Level-5 G4 Blender Tools` and use:

```text
File > Import > Level-5 G4 Model
File > Import > Level-5 G4 Model Folder
File > Import > Attach Level-5 G4 Body and Shoes
File > Import > Level-5 G4 Animation
File > Import > Level-5 G4 Camera
File > Import > Level-5 G4 Event Folder
File > Export > Level-5 G4 Port
View3D > Sidebar > Level-5 > G4 Port
```

The exporter needs a legally obtained native model base from a complete game dump. The original model defines the compatible record structure, materials, palettes and texture archive that the port operation patches.

## Character Rigging

<img src="img/img_01.png" alt="Stadium" width="500" height="auto" />
<img src="img/img_03.png" alt="Stadium" width="500" height="auto" />


The importer supports character skeletons and skinning data.

Character heads named `cXXXXXXXX` can be completed with their separate
`uXXXXXXXX` body and `sXXXXXXXX` shoes. After selecting a single animation,
Blender opens three explicit selectors in sequence: character model, body and
shoes. Cancelling the body or shoes selector skips only that part; no uniform
is guessed from an ID. Direct model imports expose equivalent manual fields.
Existing rigs can also use `Attach Level-5 G4 Body and Shoes` from the Import
menu. Secondary LOD meshes are discarded during import so LOD0, LOD1 and LOD2
do not deform visibly at the same time.

Event-folder import opens one assignment list containing every character actor
before building the batch scene. Head, body and shoes paths can be populated
from that dialog; an empty head uses the model encoded by the event and empty
body/shoes fields skip those parts. Assignments are retained in the addon
preferences for later events. Head substitution is intentionally restricted to
event-folder import. Repeated instances of one model use separate slot entries
such as `c000101_s00` and `c000101_s01`.

Many character models do not store their skeleton locally and instead reference shared skeletons located elsewhere in the game's data.

The addon includes a preprocessed `chara_model_lookup.json` for shared skeleton lookup. The source files still need to come from a complete game dump containing the shared skeleton files located under:

```text
data/common/chr/
├── c000101/
├── c000102/
├── c002001/
├── c002202/
├── c003001/
├── c004001/
└── c004202/
```

If these directories are present, the importer will automatically locate and load the required skeletons during import.

After Collada import, Blender may display imported bones with vertical default
tails. `Apply Bone Orientation` can improve that visual presentation, but it
changes Blender's local bone axes and is therefore disabled by default. G4MT
imports always preserve the original G4SK axes; a previously reoriented
selected rig is replaced with a fresh animation-safe rig. When visual
orientation is enabled, the original rest quaternion is stored on each bone as
`g4_rest_rotation_xyzw`.

Bodies and shoes keep their native G4SK armature, bind pose, vertex groups and
weight palettes. G4MT tracks are applied independently to every character part
whose bone CRC/name matches a motion target. Uniform-only helpers such as
`_wgt_1_0`, sleeves and accessories remain unkeyed and inherit motion through
their native hierarchy. No vertex weights or part-specific bones are remapped.

Imported materials use a Level-5-style Eevee node graph with one hard light
transition instead of conventional smooth shading. The base texture receives
the stronger saturation used by the game; `oc` supplies a second baked shadow
region, `sp` shapes the directional highlight, and `spm` masks its affected
regions. RGB channels from `msk` are exposed as neutral `G4 Mask ... Tint`
nodes so recolour and skin parameters can be applied without using the mask as
alpha. Source-painted line work remains in the base texture.

The add-on also enables one Freestyle line set using silhouette, crease,
contour and material-boundary edges. This approximates the engine's global
depth/normal edge pass. Executable analysis identifies that pass through the
global parameters `edge2OutlineScale`, `edge2OutlineDepthScaleOffset`,
`edge2OutlineDepthScaleMax` and `charaRimOutlineMax`; consequently the `line`
texture is retained as a material parameter rather than misused as a UV-space
outline mask.

## Event Animation

`Level-5 G4 Event Folder` reads all character animation G4PK files and the
event G4CM in a directory. Models are imported once per character ID. Each cut
becomes a named Action and an NLA strip; camera and lens cuts use the same
timeline, and Blender markers identify cut boundaries. Events with disjoint
source ranges preserve their original timing. Alternative cuts whose source
ranges overlap are concatenated by cut number so Blender can represent them in
a single non-overlapping NLA scene.

When the extracted `event_cfg/evt/<event>.cfg.bin.json` or XML is available,
the importer reads actor placement links such as `c11010019 -> evp01`. Each
cut's `point_s00` G4MT is composed through `Ex/all/evpXX`, converted to
Blender's axes and composed with the model's imported base orientation before
being written into the actor Action. Actors absent from a cut stay hidden
instead of appearing in their rest pose at the origin.

Animation rigs are aligned to the exact G4SK rest axes before Actions are
created. This avoids the TRS approximation that Blender's Collada bone-axis
conversion can otherwise introduce, especially on non-uniform facial scaling.
G4MT scale channels store cumulative scale at each node; the importer converts
them to Blender-local child/parent ratios while retaining normal full scale
inheritance. This prevents repeated scale multiplication without detaching the
eyes, eyelids, tongue or lips from legitimate head scaling. Eye, eyelid and
eyebrow curves use lossless reduction.

Large events intentionally produce large Blender files because transforms are
sampled every source frame to preserve G4 quaternion interpolation. The
importer writes F-curves in bulk, omits unanimated channels and simplifies
constant or near-linear samples within a `1e-5` vector tolerance. Memory and
disk use still scale with the number of actors, bones and frames.

Use `File > Export > Level-5 G4 Scene (.fbx)` for completed event scenes. It
exports one binary scene animation without embedding textures, duplicating NLA
strips as takes, exporting every Action or adding leaf bones. The animation
simplification default is zero so subtle face and finger curves survive a
round trip. `Include Meshes` can be disabled when the destination already has
the models. Do not use Blender's standard FBX preset for these event scenes:
its all-Actions behavior can duplicate the same animation across every rig.
The supplied 955 MB sample contained 1,695,710 animation curves; the lossless
Level-5 export of the tested 2054-frame event, including meshes, was 108.8 MB.

G4 channels whose first key occurs after the clip start hold that first value.
This prevents invalid backward extrapolation that previously caused repeated
180-degree bone rotations and extreme translation or scale values.

## Requirements

* Blender 4.0 or newer
* Python 3.10 or newer
* Pillow available to Blender/Python for custom texture rebuilds

## Disclaimer

This project is intended for interoperability, research and modding purposes.

The repository does not contain original game models, textures, animations, audio files or other playable assets.

A generated lookup database derived from game metadata is included solely to allow automatic skeleton resolution and character rigging during import.

Users must provide their own legally obtained game files.

This project is an independent community-made tool and is not affiliated with, endorsed by or associated with Level-5.
