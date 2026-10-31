# Level-5 G4 Blender Tools

<p>
  <a href="LICENSE.MD"><img alt="Licence GPL-3.0-or-later" src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue"></a>
  <img alt="Blender 4.1 or newer" src="https://img.shields.io/badge/Blender-4.1%2B-orange">
</p>

**G4_Blender** is a Blender add-on for working with files from Level-5's proprietary **lives** engine, which uses a range of formats beginning with `G4`.

The add-on lets you import complete models and cutscene animations, recreate the games' shaders in Blender, and export custom models. Whether you're exploring game assets or making your own, the goal is to make working with these formats a little easier.

## Blender compatibility

- **Minimum:** Blender 4.1
- **Recommended:** Blender 4.5 or newer

## Supported formats

| Format | Support |
| --- | --- |
| `G4MD` | Model import and export |
| `G4PKM` | Model import only; (export is not currently supported) |
| `G4PK` and `G4MT` | Animation import, including cutscenes |
| `G4SK` | Rig data used during import (cannot be imported on its own) |
| `G4TX` | Texture packs from Nintendo Switch and Windows versions |

## Game compatibility

G4_Blender is intended to work with modern Level-5 games built on the lives engine. This includes games such as:

- **INAZUMA ELEVEN: Victory Road**
- **Yo-Kai Watch 4**
- **Yo-Kai Gakuen Y**
- **Ni no Kuni II: Revenant Kingdom**

Compatibility can vary between games and assets, so support for every file isn't guaranteed.

## Special thanks

- **TheWonderVal** — outline logic, rigging tests, and bug reports.
- **KatamariEnjoyer** — testing and bug reports.
- **daniguay87** — rigging tests and bug reports.
- **DaRk_Proaso** — porting tests.
- **DaniKH** — batch-importing and shading support, rigging tests, and bug reports.
- **Victory Road España**.

## License

G4_Blender is licensed under the **GNU General Public License v3.0 or later**. See [LICENSE.MD](LICENSE.MD) for details.

## v2.1.0

The G4 Port exporter now preserves the original model's vertex stride, secondary vertex colors and UV placement. This fixes armor exports that failed with a vertex-buffer range or skin-weight validation error. The release also includes the current model, expression-atlas and animation workflows, including G4PK animation export.

Download `G4_Blender_v2.1.0.zip` from [Releases](https://github.com/The-RealBobi/G4_Blender/releases/tag/v2.1.0), then use **Edit > Preferences > Add-ons > Install from Disk** in Blender and select the ZIP. Enable **Level-5 G4 Blender Tools** and restart Blender after replacing an older version.
