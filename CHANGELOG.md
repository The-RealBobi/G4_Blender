# G4_Blender changelog

## v2.1.1

- Fixed model exports on Windows when DirectXTex is installed as `texconv.exe`. PNG texture conversion now uses the correct DirectXTex options and avoids the `Unknown option: compression_format` error.
- Keep temporary DDS output separate from source textures during export. An existing DDS with the same name as the source PNG is preserved.
- Accept DDS output with either a lowercase or uppercase extension from the converter.
