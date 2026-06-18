# Level-5 G4 Blender Tools

Import models, characters, maps and textures from Level-5 G4-based games directly into Blender, and port edited Blender/DAE geometry back to conservative `G4MD/G4MG/G4TX` packages.

The repository ships one unified Blender add-on:

* `Level-5 G4 Blender Tools`: import and port/export tools in one flat add-on package.

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
* Shared skeleton resolution.
* G4SK rest bone orientation reconstruction.
* Full map importing.
* Batch processing support.
* Automatic scene reconstruction.

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

## Installation

Install this repository as a single Blender add-on package. The root `__init__.py` is the only add-on entry point; helper scripts and lookup data live next to it at the same level.

The add-on package includes:

```text
|-- __init__.py
|-- g4_port_addon.py
|-- g4_port.py
|-- g4_model_probe.py
`-- chara_model_lookup.json
```

In Blender, enable `Level-5 G4 Blender Tools` and use:

```text
File > Import > Level-5 G4 Model
File > Import > Level-5 G4 Model Folder
File > Export > Level-5 G4 Port
View3D > Sidebar > Level-5 > G4 Port
```

The exporter needs a legally obtained native model base from a complete game dump. The original model defines the compatible record structure, materials, palettes and texture archive that the port operation patches.

## Character Rigging

<img src="img/img_01.png" alt="Stadium" width="500" height="auto" />
<img src="img/img_03.png" alt="Stadium" width="500" height="auto" />


The importer supports character skeletons and skinning data.

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

After Collada import, Blender may display imported bones with vertical default tails. The addon fixes the visual armature by using the real parent-child joint direction for each bone and the G4SK section-1 SRT quaternion for roll. The original rest quaternion is stored on each bone as `g4_rest_rotation_xyzw`.

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
