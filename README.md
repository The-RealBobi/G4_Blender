# Level-5 G4 Importer for Blender

Import models, characters, maps and textures from Level-5 G4-based games directly into Blender.

The addon automatically converts G4 assets into Blender-compatible data, recreating materials, textures and skeletons with minimal user interaction.

<img src="img/img_02.png" alt="Stadium" width="750" height="auto" />

## Features

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

## Supported Formats

| Format | Description             |
| ------ | ----------------------- |
| G4MD   | Model files             |
| G4PKM  | Packed model containers |
| G4SK   | Skeleton files          |
| G4TX   | Texture archives        |

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

## Disclaimer

This project is intended for interoperability, research and modding purposes.

The repository does not contain original game models, textures, animations, audio files or other playable assets.

A generated lookup database derived from game metadata is included solely to allow automatic skeleton resolution and character rigging during import.

Users must provide their own legally obtained game files.

This project is an independent community-made tool and is not affiliated with, endorsed by or associated with Level-5.
