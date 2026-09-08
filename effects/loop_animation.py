"""Native shared-loop references used by effect material animation."""
from dataclasses import dataclass
from pathlib import Path

from ..formats.cfgbin import CfgBinDocument, parse_cfgbin_file


@dataclass(frozen=True)
class LoopBinding:
    source_crc: int
    material_crc: int
    source_type: int
    target_type: int
    texture_index: int
    speed: float


@dataclass(frozen=True)
class LoopAnimation:
    motion_path: str
    motion_crc: int
    bindings: tuple[LoopBinding, ...]


def read_loop_animation(path: Path) -> LoopAnimation | None:
    document = parse_cfgbin_file(path)
    if not isinstance(document, CfgBinDocument):
        return None
    active = False
    stack = []
    header = {}
    bindings = []
    for entry in document.entries:
        values = [value.value for value in entry.values]
        if entry.name_crc == 0x689952A2:
            active = values == ['CReferenceLoopAnimeComponent']
        elif active and entry.name_crc == 0x5CC11670:
            break
        elif active and entry.name_crc == 0x6454D5A5:
            stack.append((values[0], {}))
        elif active and entry.name_crc == 0xBD064D3E:
            if len(values) == 2:
                (stack[-1][1] if stack else header)[values[0]] = values[1]
        elif active and entry.name_crc == 0x500C9177:
            name, fields = stack.pop()
            if name == 'REF_PARAM' and stack and stack[-1][0] == 'REF_CONFIG':
                config = stack[-1][1]
                try:
                    bindings.append(LoopBinding(
                        int(config['SRC_ANIME_BONE_CRC']) & 0xFFFFFFFF,
                        int(config['TRG_MODEL_MATERIAL_CRC']) & 0xFFFFFFFF,
                        int(fields['SRC_TYPE']), int(fields['TRG_TYPE']),
                        int(fields['TEX_IDX']), float(fields['MOTION_SPEED'])))
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError('Invalid effect loop reference') from error
    if not bindings:
        return None
    return LoopAnimation(str(header['MOT_FILE_PATH']), int(header['MOTION_NAME_CRC']) & 0xFFFFFFFF,
                         tuple(bindings))
