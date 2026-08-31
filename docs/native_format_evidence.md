# Native format evidence

Inventory captured on 2026-08-31 from
`/Volumes/BOBI/Proyectos Personales/VictoryRoad/DUMP_712/._work/raw`, including
hidden work directories.

| Extension | Files found | Status | Sample |
| --- | ---: | --- | --- |
| `.g4ma` | 35 | confirmed-file | `data/common/event/ev60/ev60_00150/ev60_00150_c000301_s00_p00_c0100.g4ma` |
| `.g4mt` | 71 | confirmed-file | `data/common/chr/_face/11_VICTORY/c11806100/c11806100_p250.g4mt` |
| `.g4cm` | 1217 | confirmed-file | `data/common/effect/event/ev62/ev62013800/ev62013800.g4cm` |
| `.cfg.bin` | 71102 | confirmed-file | `data/common/action/base_act.cfg.bin` |

Additional dump inventory confirms models, textures, effects, lights and
shader families in `DUMP_712` and `YK4`. Counts are evidence of existence only;
they do not prove that two same-named resources are binary-compatible.

## Parser smoke evidence

- `chara_model_1.03.49.00.cfg.bin`: T2B, 7,773 entries, 4-byte values.
- `comic_marks_config_0.08.55.cfg.bin`: RDBNP, 2 types, 2 lists, 1,881 bytes.
- First real G4MA: 1 clip, 2 targets, 1 channel.
- First real G4MT: 2 clips, 27 targets, 371 channels.

The Ryujinx `.toc/.data` cache is intentionally excluded from this parser
inventory because it has no native resource names or material associations.
