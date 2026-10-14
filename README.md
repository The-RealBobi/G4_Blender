# Level-5 G4 Blender Tools

Blender add-on for importing Level-5 G4 models, characters, maps, animations and textures, then exporting edited assets against a compatible native base.

The exporter patches a legally obtained native base instead of rebuilding unknown format tables from scratch. This preserves the original layout, material references, hashes, palettes and texture structure whenever possible.

## Current release (2.0.0)

* Grouped runtime modules by function under `animation/`, `porting/` and `shared/`; only the Blender entry point stays at the project root.
* Model, folder and event imports show one centered progress window with the current file or event phase and a Cancel button.
* Import every animation in a G4MT/G4PK bank as a separate Blender Action, then choose the active clip from the 3D View sidebar.
* Retarget an original animation-bank family to a custom rig. The search includes matching `common/chr` and `common/event` resources, and the bulk operation reports progress and can be cancelled.
* Map joints with common-name suggestions or manual overrides, review native mesh records, and prepare textures and exports from the **G4 Port** panel.
* Export recursively matched G4PK/G4MT resources immediately after the model when **Export Animation Family for VictoryTool** is enabled. `common/chr` uses the model's `CHARA_MOTION_INFO` animation family; `common/event` uses its generic G4SK ID and includes sibling resources from matching event cuts. Animation workers retarget against the exact G4SK produced with the model. Object scale is applied to exported geometry and joint positions, while skeleton and animation rest scales remain normalized. Explicit joint aliases are written to the VictoryTool profile and applied while the animation workers retarget; unmatched rig bones are kept in the G4SK but remain unmapped.
* Event placement and root motion keep the same game-to-Blender orientation used by the imported character rig, including when sampling or exporting animation.
* Event effects prefer the native one-cut offset between the effect asset ID and the event cut when both neighboring cuts are present.

Animation preparation for **VictoryTool** writes a `.g4anim.json` profile beside the exported model, containing the rig and retargeting details needed to generate animations during incorporation. VictoryTool lets users select CHR animations, EVENT animations, or both. Animation banks do not need to be included in the portable character package.

The legacy Blender animation exporter remains available through **Edit > Preferences > Add-ons > Level-5 G4 Blender Tools > Enable legacy animation exporter**. Enabling it exposes the CHR and EVENT export options in G4 Port. It uses the character animation family for CHR and the generic skeleton ID for EVENT, exports against the model's custom G4SK, and reports cancellable progress.

An untouched model imported from its original G4MD is preserved byte-for-byte on export by default. **Preserve Untouched Native Import** copies its original G4MD, G4MG and G4TX files only when every assigned imported mesh still matches its import snapshot. Moving geometry, changing UVs or assigning a different source mesh automatically falls back to the normal port process.

<img src="img/img_02.png" alt="Imported stadium scene" width="750" />

## Installation

Install the release ZIP, or package the `G4_Blender` directory as a ZIP while keeping its folder name and `__init__.py` at the add-on root.

1. In Blender, open **Edit > Preferences > Add-ons**.
2. Choose **Install from Disk** and select the ZIP.
3. Enable **Level-5 G4 Blender Tools**.

Install the versioned release ZIP directly in Blender; it contains the add-on
folder and the guide images.

### Menu entries

```text
File > Import > Level-5 G4 Model
File > Import > Level-5 G4 Model Folder
File > Import > Attach Level-5 G4 Character Parts
File > Import > Level-5 G4 Animation (.g4mt/.g4pk)
File > Import > Level-5 G4 Event Folder
File > Import > Level-5 G4 Camera (.g4cm)
File > Export > Level-5 G4 Port
File > Export > Retarget Original Bank Family
File > Export > Export Animation Family for VictoryTool
File > Export > Level-5 G4 Animation Candidate (.g4mt)
File > Export > Level-5 G4 Skeleton Candidate (.g4sk)
File > Export > Level-5 G4 Scene (.fbx)
View3D > Sidebar > Level-5 > G4 Port
View3D > Sidebar > G4 > Imported G4MT Bank
```

### Quick character-port workflow

1. Import the character model you want to port and open **View3D > Sidebar > Level-5 > G4 Port**.
2. Choose its original model as the native template. The exporter uses that file to identify mesh records, materials and texture entries.
3. Assign each mesh to a record. **Auto-map Common Joints** fills recognized bone mappings; use the searchable target picker or per-bone source-joint override for the remaining rig-specific assignments.
4. Review texture replacements and run **Analyze** before **Export**. To prepare the matching animation resources, enable **Export Animation Family for VictoryTool**; the resource-tree export starts automatically after the model export finishes.

## Importing assets

### Models and textures

Import a model directly from Blender, drag it into the viewport, or import a folder in batch. The add-on extracts compatible textures, builds materials and assigns them to the mesh. It also imports character and map assets with the appropriate material treatment instead of applying the character shader indiscriminately.

Map assets use separate terrain, water, grass, cutout and map-PBR profiles.
Water and grass previews add conservative detail/normal treatment while
preserving authored texture links. Character assets retain the existing
Level-5-style Eevee material with hard shadow bands, native normal-map
decoding, recolour-mask controls, wetness and outline parameters. Source
`COLOR` data is kept as **G4 Outline Parameters**, and the original line
texture remains available as **G4 Line Parameter**.

### Character rigging

<img src="img/img_01.png" alt="Imported character rig" width="500" />
<img src="img/img_03.png" alt="Character material and outline result" width="500" />

Character heads named `cXXXXXXXX` can be combined with separate components:

| Prefix | Part |
| --- | --- |
| `uXXXXXXXX` | Body |
| `sXXXXXXXX` | Shoes |
| `skXXXXXXXX` | Arms and neck, where available |
| `g`, `m`, `n` | Gloves, captain armband and nameplate |

Use the character-parts dialog during model import, or select **Attach Level-5 G4 Character Parts** to add components to an existing rig. The importer never guesses a uniform from an ID: cancelling a body or shoes selection simply skips that part. Secondary LOD meshes are discarded so multiple LODs do not deform together.

Many character models reference a shared skeleton rather than embedding one locally. `data/chara_model_lookup.json` helps locate it, but the add-on still requires a complete legal game dump with the shared skeletons, typically under `data/common/chr/`. Joint names are resolved through the model's CRC32 palette, not by palette order or mesh shape, so modular parts can target the correct bones reliably.

`Apply Bone Orientation` is optional and improves the visual orientation of imported bones in the viewport.

### Outlines and character parameters

The **Character Outline** preference has three modes:

| Mode | Result |
| --- | --- |
| **Detailed** | Default. Filtered silhouette plus selected authored seam details and viewport cavity lines. |
| **Simple** | Filtered silhouette and viewport outline only. |
| **Off** | Disables both outline paths. |

**Outline Thickness** controls the main silhouette in pixels; its default of `1.65` matches the game reference. Eye and mouth helper planes are excluded from contours. Authored line textures and low `COLOR.B` weights select the restrained secondary silhouette where appropriate.

Character meshes also receive a **Level-5 Character Parameters** Geometry Nodes modifier. It is added for character imports from `chr` even when a shared uniform part has no texture of its own, so externally controlled character shading remains available on modular bodies. Shared `_uniform` models also fall back to their family G4TX directory when no converted `chara_parts` lookup is present, letting standalone imports such as `u000101` resolve `u000101_20`/`u000101_30` texture sets and receive the Character shader. The modifier exposes saturation, brightness, light and shadow floors, normal strength, specular strength and wetness without requiring shader-graph edits. When the selected mesh uses the Character shader, the modifier context also shows labelled texture slots for the material's base, mask, normal, occlusion, line, specular and alpha image nodes. **Load Character G4TX** in the same modifier panel extracts a selected `.g4tx` into the import cache and loads its images into Blender; matching texture roles are assigned automatically when possible, otherwise the loaded images remain available for manual selection in the texture fields.

## Map reconstruction

For a complete map, select the world directory itself, such as `w10`, `w11` or `w12`, and enable recursive folder import. The importer reads the world-level `.g4pk` or `.g4pkm` hierarchy, matches scene nodes to model assets, composes transforms, and uses linked object data for repeated assets.

Auxiliary shadow and culling objects are hidden automatically. Models absent from the render hierarchy still import unchanged; the add-on does not invent placements for them. When present, a matching native half-float DDS cubemap is converted to equirectangular Radiance HDR and used as a restrained world environment.

## Porting edited models

The add-on imports game models from a native mesh payload and no longer exports and reimports an intermediate DAE. DAE remains an optional probe/export format, and Blender can still open DAE files directly in builds that include the Collada importer. The port exporter writes edited Blender geometry directly into a native `G4MD`/`G4MG` pair. Start from a compatible original model from a complete legal game dump; that base defines the record structure, materials, palettes and texture archive the exporter can safely patch.

The exporter:

* Preserves native layouts, material references, hashes and record structure where possible.
* Reads weights from Blender vertex groups and from supported Collada skin controllers.
* Validates generated records, palettes, indices and packed-weight sums before writing packages.
* Copies or rebuilds `G4TX` archives from the native base.
* Handles Nintendo Switch `NXTCH` texture payloads, with automatic `dx11` to `nx` fallback.
* Builds port settings from the selected original model rather than using model-specific bone presets.

### Custom outline edges

The **Level-5** sidebar contains separate **PORT**, **OUTLINE** and **EXPRESSION**
panels. Expression atlas preparation and facial previews are in **EXPRESSION**.

In **OUTLINE**, select edges on one mesh in Edit Mode and choose **Mark Outline**.
Marked edges immediately appear in red, even after deselection. Parameters
refresh automatically on Mark/Clear and when loading authored meshes; export
always rebuilds the bands. **Stroke Width** changes the custom band width and
is saved with the scene and port configuration. **Clear Outline**
removes marks; **Select Outlines** selects them again. UV seams remain independent.
**Preview Outlines** toggles the red guide. It is an editing guide, not an exact
preview of the game's line width or visibility.

Open strokes and individual surface edges are supported without enclosing a
region. Marking enables **Authored Outlines** for export. Export creates narrow
bands along marked edges with constant COLOR per triangle, and matches the new
cuts on adjacent faces. UVs and skin weights are interpolated in the exported
copy; the editable mesh remains unchanged. The red guide shows the marked edge;
the exported band has a small width relative to its incident triangles. Loose
edges without faces cannot produce surface outlines. Other meshes retain the
constant custom fallback.

The authored channel values remain experimental: Blender preview and parameter
transport are validated, but native-game appearance still needs testing. The red
guide is a viewport overlay and does not appear in F12 renders.

Native imports recover detectable green-COLOR discontinuities, including seams
between duplicated vertices. These inferred guides appear in Edit Mode without
changing native colors or enabling authored export. They are not a complete list
of view-dependent outlines: the native edge2 silhouette remains shader-driven.
Marking or clearing an original mesh opts that mesh into custom authoring.

### Texture replacement

Texture replacement is deliberate and non-destructive:

1. Assign each Blender mesh to its original G4MD record.
2. Open **Prepare and review atlas**, then choose **Prepare Atlas**. Review the destination G4TX texture, source state and whether the atlas is ready, stale or native.
3. Export. Optionally enable **Regenerate Atlas On Export** to refresh it automatically.

The default atlas source is the first diffuse image used by the mesh. **Atlas Source** lets you override it. Empty, stale or unreadable entries never write blank textures or UV-guide PNGs: the exporter removes only its failed generated atlas, logs a warning and keeps the native G4TX payload. Atlas cells are deterministic (record order, then object name), laid out left-to-right and top-to-bottom. Generated cells include a small edge gutter and transparent-pixel colour bleed so filtering does not create black outlines or sample a neighbour. Meshes that share the same normal source image share the exact same atlas-cell transform, preserving their authored relative UV placement; only meshes with repeated or out-of-range UVs receive an isolated projected cell and a fitted transform. Object UV tiles are exported only for a base texture that is actually replaced; native textures keep their original UVs. Enabling **Regenerate Atlas On Export**, **Use Object UV Tiles** or **Auto Pack Object UVs** is treated as an explicit atlas export and never falls back to a byte-for-byte native copy. `line`, `oc`, `sp` and `spm` maps are preserved unless **Replace Special Maps** is enabled *and* a valid source map exists; unavailable special maps never receive implicit solid-colour replacements.


`eye_10` and `mouth_10` share the native facial texture (`*_10`) and therefore never create a new base atlas. Generic replacement paths for that entry are ignored deliberately: they would invalidate its authored UV windows. A prepared atlas from another model can be accepted explicitly through **Existing 4x2 Atlas** and **Use Existing 4x2 Atlas**. Alternatively, initialize **Expression pool**, provide eight images in row-major order (Cell 1–4 are the top row; 5–8 the bottom row), then choose **Build 4x2 Expression Atlas**. A pool maps the source face and mouth into Cell 1 (top-left); an accepted existing atlas leaves their authored UV windows intact. Both routes replace only the shared facial G4TX entry; until then, its native payload is preserved.

Atlas diagnostics are written to `atlas_uv_trace.log` in the configured export cache. It lists each source image, UV bounds, atlas cell, scale and offset, then appends the exact converter command and its output. Analysis/export reports also include `uv_transform_trace` for every generated native record.

Atlas offsets are authored in Blender image space and converted automatically when the G4 V flip is enabled, so the visible top row remains the top row in the exported model.

Native normal, occlusion and specular textures are preserved when available;
valid same-role replacements can also be supplied. Automatic atlas generation
focuses on the base texture. It does not create a new normal map or reflection
cubemap.

## Requirements

* Blender 4.0 or newer
* Python 3.10 or newer
* Pillow available to Blender/Python when rebuilding custom textures

## Disclaimer

This independent community tool is intended for interoperability, research and modding. It contains no original game models, textures, audio or other playable assets. The included lookup database is derived from game metadata solely to support automatic skeleton resolution and rigging.

Provide your own legally obtained game files. This project is not affiliated with, endorsed by or associated with Level-5.

## Special thanks

* **TheWonderVal** — outline logic, rigging testing and bug reporting.
* **KatamariEnjoyer** — testing and bug reporting.
* **daniguay87** — rigging testing and bug reporting.
* **DaRk_Proaso** — porting testing.
* **DaniKH** — batch-importing and shading support, rigging testing and bug reporting.
* **Victory Road España**.

## Avatar animation preview

Import the character normally and keep its original armature. In **Level-5 > AVATAR**, use **Build Avatar Representation**, review the bone assignments, and validate the mapping. Select the original game character model in **PORT**, then use **Import Avatar Animation** to choose a G4MT or G4PK file. The file selector's **Animation** and **Bank Entry** fields use indices starting at zero.

The preview creates a new Action on the character's existing bone names. It preserves the original hierarchy, rest matrices, mesh geometry, skin weights, and bone rotation modes. The original character model supplies the source skeleton automatically. Animation preparation runs on a temporary armature; the completed Action is assigned only after its evaluation is checked. Cancel or an error removes the incomplete Action and temporary rigs. The progress display closes automatically when the operation ends.

This preview is the representation phase of the new Avatar system. Finger and auxiliary assignments can be reviewed in the Avatar panel. It does not establish game-export compatibility or replace the existing animation-family exporter.

## Animation import (1.5.0)

The G4MT/G4PK importer creates separate Actions for all independent clips in
its selected bank. Leave **Active Animation** empty to start with the first
clip longer than two frames, or enter an exact name/index. Disable **Import All
Animations** to import only that selection. Additive clips still require a
base animation and are reported as skipped.

When importing all clips, select the character armature and use **View3D >
Sidebar > G4 > Imported G4MT Bank** to switch the active Action. That panel also
opens **Retarget Original Bank Family**. Enter the source bank ID, the new
`c00…` destination ID and a new output folder; the add-on searches matching
character and event resources beneath the configured game data root. It writes
to the new folder and leaves the source resources intact. For a single clip,
use **File > Export > Level-5 G4 Animation Candidate**. **Export All Clips
from Imported Bank** remains off unless enabled for that export.

In **G4 Port > Export**, **Export Animation Family for VictoryTool** is
optional and remains off by default. When enabled, the exported armature is
serialized as the target G4SK and gets a `.g4anim.json` mapping profile. The
original model resolves the `common/chr` animation family from its skeleton
CRC and the `common/event` family from its generic G4SK ID. The export begins
after the regular model export and merges both branches into the package's
`data/common` overlay. The UI does not require an imported Action or a
separate source G4SK selection for this workflow.

Normal imports retain animated root bones. Event root extraction now uses
the same bone delta and rest basis, preserving the resulting world pose
instead of converting the coordinate system twice. Existing Actions need to
be reimported to receive the fix.

Validation covers both banks of the reported Yo-Kai Watch 4 y03150000 model
(8 and 24 clips) in Blender 4.5.10 and 5.2, a Gakuen Y bank, and six Victory
Road event cuts with world-matrix and skinned-vertex comparisons. This does
not establish native-game equivalence or support for additive blending.

### Event placement and animation skeletons (1.6.4)

Event imports now apply per-cut actor attachments from `event_cfg/evt` or
`event_cfg/vis` to the corresponding `point_sXX` animation and `evpXX` joint.
The placement is composed with the actor's animated root, preserving both
layers. Missing referenced points are reported before creating the actor.

This fixes the clustered actors and distant-camera mismatch in `ev20_03500`.
Its complete import was checked with 13 actors, 37 actor cuts and 14 camera
cuts, including renders through the original cameras. If an event contains
point packages but its event_cfg or point motions cannot be resolved, the
import now records the placement counts in scene custom properties and shows
a warning instead of silently leaving actors at their local animation
positions. All companion resources are resolved from the selected data root
that contains `common`; keep the event_cfg and point assets under that same
data root. Reimport existing events to apply the correction.

The same direct data-root rule is used by the G4MT skeleton lookup, the model
importer, animation companions, event resources and character-part metadata.
No neighboring `raw/data` or `readable/data` directory is added implicitly.

Event character defaults also attach the matching `sk000xxx` arms mesh beside
the selected `u000xxx` body, including direct event-operator imports that skip
the character-parts dialog. The mesh is rebound to the actor's animated
armature and follows the same shared skeleton.

Animation imports now establish the selected model's data root before looking
up its external G4SK. This keeps models whose skeleton is shared through the
character catalog on the same bind skeleton as their animation and prevents
localized bone deformation when the importer starts in a fresh Blender
session.

Event character assembly also reads the native `chara_parts*.cfg.bin` tables
when they are available. The declared body profile selects the matching
`u000xxx` mesh and its paired `sk000xxx` accessory, so modular characters no
longer fall back to the profile-0 arms. Event `event_cfg` model substitutions
are applied before this lookup, and the animation importer passes every part
slot by name so a body cannot be attached as shoes by argument position.

The joint palette resolver prefers a complete named source-skeleton mapping
when a separated character part exposes more named joints than the partial
CRC32 palette. This preserves the CRC32 path while avoiding unresolved arm
weights on compact character rigs.

The event character-parts dialog now starts each actor from the model declared
by `event_cfg`, including its body, shoes and matching arms. Existing saved
choices remain available, and changing the head continues to refill the
modular parts for that head. Legacy saved paths that point directly to a base
folder such as `_uniform/u000101/u000101.g4md` are treated as old fallbacks and
are replaced by the event profile in both the dialog and direct batch imports.
Profile-specific paths such as `_uniform/u000101/u000102.g4md` remain explicit
choices. Character event packages that embed their `G4MA` material animation
now receive the same facial-atlas UV animation even when **Import Effects** is
disabled; effect meshes remain controlled by that option.

The event character-parts dialog also reads the native character model tables
used by VictoryTool. It follows `CHARA_BASE_INFO[6]` to
`CHARA_MODEL_INFO[0]`, reads the packed RGBA skin value from
`CHARA_MODEL_INFO[16]`, and pre-fills **Skin Mask** for the selected head.
Event and direct character imports apply that value to the red recolour-mask
channel on every imported character mesh, while saved manual choices remain
available. This lookup uses only the selected `data` root and accepts both
native signed and unsigned integer representations from the CFGBIN reader.

Embedded face `G4MA` tracks are evaluated through the material's facial atlas
mapping, so the eye and mouth UVs continue to move when effect import is off.
For example, the `ev20_03500` c116 actor changes its mapping at frames 679 and
1217, and the resulting facial pose is visible in a Blender render.

The event-parts dialog now resolves its `event_cfg` model lookup through the
model importer module in every event batch path. This fixes events such as
`ev60_00210`, whose generic `c000xxx` actors previously stopped after folder
selection because the deferred dialog callback referenced an unavailable
function. Generic actor variants now open the assignment dialog and continue
through the same data-root and character-part resolution used by named actors.

### Character rendering (v1.8.0)

Game is the default outline mode; Simple and Off remain available. Game uses
screen-space vertex parameters, normals and depth to darken the character's
own colors. Render outlines require EEVEE and the compositor. The viewport
uses Blender's object silhouette preview. Detailed has been retired.

The character shader uses capture-derived shadow coverage and UNORM color
processing with neutral saturation and brightness. This is a Blender preview
approximation of the captured shader variant, not a complete reproduction of
the game's lighting, post-processing or every gradient-table variant.

Existing Game compositor groups update on load. Facial mask gradients no
longer become triangular dark patches. Block animation imports validate model
paths separately from skin color.

## Event effects (v1.9.0)

Import the complete event with the batch event importer. **Import Effects** is
enabled by default there; individual model imports do not start effect animation.
Effects are assigned to existing event cuts, including both native cut naming
conventions. T1, T1M1 and ThresholdGrd materials support the reconstructed shader
paths. Base FakeParticle meshes generate camera-facing particles with embedded
G4TP stand animation where available. Other particle variants and PTLB simulation
remain incomplete.

The installation ZIP includes this guide. To build it from a source checkout,
run `python3 tools/build_release.py`; the versioned ZIP is written to `dist/`.
