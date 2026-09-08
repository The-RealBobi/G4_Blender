"""Event-triggered custom parameter samples and their native UV bindings."""
from dataclasses import dataclass
import math
import random
import struct
import zlib
from pathlib import Path



@dataclass(frozen=True)
class RandomParameter:
    animation_crc: int
    parameter_crc: int
    mask: int
    minimum: float
    maximum: float
    step: float


@dataclass(frozen=True)
class UVParameter:
    material_crc: int
    texture_index: int
    parameter_crc: int
    mask: int


@dataclass(frozen=True)
class RandomAnimation:
    initial: dict[int, tuple[float, ...]]
    samples: tuple[RandomParameter, ...]
    bindings: tuple[UVParameter, ...]


def float32(value: float) -> float:
    return struct.unpack('<f', struct.pack('<f', value))[0]


def quantized_sample(rule: RandomParameter, generator: random.Random) -> float:
    if not all(math.isfinite(v) for v in (rule.minimum, rule.maximum, rule.step)) or rule.step <= 0 or rule.maximum < rule.minimum:
        raise ValueError('Invalid effect random parameter range')
    # Native SSE rounds the quotient before truncation; double division can lose the last bin.
    count = int(float32(float32(rule.maximum-rule.minimum)/rule.step))
    return float32(rule.minimum + float32(generator.randrange(count+1)*rule.step))


def sample_parameters(animation: RandomAnimation, animation_crc: int, state: dict[int, tuple[float, ...]], generator: random.Random) -> dict[int, tuple[float, ...]]:
    result = dict(state)
    for rule in animation.samples:
        if rule.animation_crc != animation_crc or rule.parameter_crc not in result:
            continue
        value = quantized_sample(rule, generator)
        result[rule.parameter_crc] = tuple(value if rule.mask & (1 << axis) else component
                                           for axis, component in enumerate(result[rule.parameter_crc]))
    return result


def read_random_animation(path: Path) -> RandomAnimation:
    from ..formats.cfgbin import CfgBinDocument, parse_cfgbin_file

    document = parse_cfgbin_file(path)
    initial, samples, bindings = {}, [], []
    if not isinstance(document, CfgBinDocument):
        return RandomAnimation(initial, (), ())
    section, fields = '', None
    for entry in document.entries:
        values = [v.value for v in entry.values]
        if entry.name_crc == 0x689952A2:
            section = values[0]
        elif entry.name_crc == 0x5CC11670:
            section = ''
        elif section in ('CCustomParamProperty', 'CCustomAnimeRandom', 'CCustomAddMaterial'):
            if entry.name_crc == 0x6454D5A5 and values == ['Param']:
                fields = {}
            elif entry.name_crc == 0xBD064D3E and fields is not None:
                fields[values[0]] = values[1:]
            elif entry.name_crc == 0x500C9177 and fields is not None:
                try:
                    if section == 'CCustomParamProperty':
                        vector = tuple(float(v) for v in fields['param'])
                        if len(vector) != 4:
                            raise ValueError('Expected four custom parameter components')
                        initial[zlib.crc32(fields['name'][0].encode('utf-8'))] = vector
                    elif section == 'CCustomAnimeRandom':
                        minimum, maximum, step = map(float, fields['random'])
                        samples.append(RandomParameter(int(fields['animeNameCrc'][0]) & 0xFFFFFFFF,
                            int(fields['nameCrc'][0]) & 0xFFFFFFFF, int(fields['setType'][0]), minimum, maximum, step))
                    elif fields['setMatInfo'] == [0] and fields['setType'] == [1]:
                        bindings.append(UVParameter(int(fields['nameCrc'][0]) & 0xFFFFFFFF,
                            int(fields['setParamIdx'][0]), int(fields['refParamNameCrc'][0]) & 0xFFFFFFFF,
                            int(fields['refParamType'][0])))
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError(f'Invalid {section} effect parameter') from error
                fields = None
    return RandomAnimation(initial, tuple(samples), tuple(bindings))
