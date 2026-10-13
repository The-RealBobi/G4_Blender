"""Write a single absolute-pose G4MT clip from target-local transforms."""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Sequence

from .g4mt import crc32b, parse_g4mt

PRECISION_FACE_MARKERS = ("eye", "eld", "ebw", "lgt")


def _clip_hash_order(clip_hashes: Sequence[int]) -> tuple[int, ...]:
    return tuple(sorted(range(len(clip_hashes)), key=lambda index: (clip_hashes[index], index)))


def _has_clip_hash_order(data: bytes, clip_hashes: Sequence[int]) -> bool:
    header_words = struct.unpack_from("<H", data, 0x0A)[0]
    name_offset = (header_words + struct.unpack_from("<H", data, 0x2E)[0]) * 4
    order = struct.unpack_from(f"<{len(clip_hashes)}H", data, name_offset)
    return order == _clip_hash_order(clip_hashes)


def simplify_samples(frames: Sequence[int], samples: Sequence[Sequence[float]], tolerance: float) -> tuple[list[int], list[Sequence[float]]]:
    if samples and all(sample == samples[0] for sample in samples[1:]):
        return [frames[0]], [samples[0]]
    if len(samples) <= 2:
        return list(frames), list(samples)
    keep = {0, len(samples) - 1}
    stack = [(0, len(samples) - 1)]
    tolerance_squared = tolerance * tolerance
    while stack:
        first, last = stack.pop()
        frame_span = frames[last] - frames[first]
        if last - first <= 1 or frame_span == 0:
            continue
        worst_index = None
        worst_error = tolerance_squared
        for index in range(first + 1, last):
            amount = (frames[index] - frames[first]) / frame_span
            if len(samples[index]) == 1:
                delta = samples[index][0] - (
                    samples[first][0] + (samples[last][0] - samples[first][0]) * amount
                )
                error = delta * delta
            else:
                error = sum(
                    (value - (start + (end - start) * amount)) ** 2
                    for value, start, end in zip(samples[index], samples[first], samples[last])
                )
            if error > worst_error:
                worst_error = error
                worst_index = index
        if worst_index is not None:
            keep.add(worst_index)
            stack.extend(((first, worst_index), (worst_index, last)))
    indices = sorted(keep)
    return [frames[index] for index in indices], [samples[index] for index in indices]


@dataclass(frozen=True)
class G4MTLocalTransform:
    translation: tuple[float, float, float]
    rotation_xyzw: tuple[float, float, float, float]
    scale: tuple[float, float, float]


@dataclass(frozen=True)
class G4MTTargetSamples:
    name: str
    parent_index: int | None
    samples: tuple[G4MTLocalTransform, ...]


@dataclass(frozen=True)
class G4MTClipSamples:
    template_clip_index: int
    name: str
    targets: tuple[G4MTTargetSamples, ...]


def _targets_parent_first(targets: Sequence[G4MTTargetSamples]) -> tuple[G4MTTargetSamples, ...]:
    order: list[int] = []
    state = [0] * len(targets)

    def visit(index: int) -> None:
        if state[index] == 2:
            return
        if state[index] == 1:
            raise ValueError("G4MT target hierarchy contains a cycle")
        state[index] = 1
        parent = targets[index].parent_index
        if parent is not None:
            if not 0 <= parent < len(targets) or parent == index:
                raise ValueError(f"G4MT target {targets[index].name!r} has an invalid parent index")
            visit(parent)
        state[index] = 2
        order.append(index)

    for index in range(len(targets)):
        visit(index)

    new_indices = {old_index: new_index for new_index, old_index in enumerate(order)}
    return tuple(
        G4MTTargetSamples(
            targets[old_index].name,
            new_indices.get(targets[old_index].parent_index),
            targets[old_index].samples,
        )
        for old_index in order
    )


EncodedG4MTChannel = tuple[bytes, bytes, bytes]
EncodedG4MTTarget = tuple[EncodedG4MTChannel, ...]


def _encode_clip_samples(
    template_clip, clip_name: str, targets: Sequence[G4MTTargetSamples],
) -> tuple[list[int], tuple[EncodedG4MTTarget, ...]]:
    if not clip_name or "\0" in clip_name:
        raise ValueError("G4MT clip name must be non-empty and contain no null characters")
    frame_count = template_clip.end_frame - template_clip.start_frame + 1
    if not targets or len(targets) > 0xFFFF:
        raise ValueError("G4MT clip requires between 1 and 65535 target tracks")

    target_hashes: list[int] = []
    seen_names: set[str] = set()
    cumulative_scales: list[tuple[tuple[float, float, float], ...]] = []
    for target_index, target in enumerate(targets):
        try:
            target.name.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError(f"G4SK target name is not ASCII: {target.name!r}") from error
        if not target.name or target.name in seen_names:
            raise ValueError(f"G4MT target name is empty or duplicated: {target.name!r}")
        seen_names.add(target.name)
        if len(target.samples) != frame_count:
            raise ValueError(f"G4MT target {target.name!r} has {len(target.samples)} samples; expected {frame_count}")
        if target.parent_index is not None and not 0 <= target.parent_index < target_index:
            raise ValueError(f"G4MT target {target.name!r} parent must precede it in target order")
        target_hashes.append(crc32b(target.name))
        parent_scales = cumulative_scales[target.parent_index] if target.parent_index is not None else (
            ((1.0, 1.0, 1.0),) * frame_count
        )
        cumulative_scales.append(tuple(
            tuple(local * parent for local, parent in zip(sample.scale, parent_scale))
            for sample, parent_scale in zip(target.samples, parent_scales)
        ))
    if len(set(target_hashes)) != len(target_hashes):
        raise ValueError("G4SK target names contain a CRC32 collision")

    frames = tuple(range(template_clip.start_frame, template_clip.end_frame + 1))
    encoded_targets: list[EncodedG4MTTarget] = []
    for target_index, target in enumerate(targets):
        values_by_type = (
            (1, tuple(scale[0] for scale in cumulative_scales[target_index])),
            (2, tuple(scale[1] for scale in cumulative_scales[target_index])),
            (3, tuple(scale[2] for scale in cumulative_scales[target_index])),
            (9, tuple(value for sample in target.samples for value in sample.rotation_xyzw)),
            (10, tuple(sample.translation[0] for sample in target.samples)),
            (11, tuple(sample.translation[1] for sample in target.samples)),
            (12, tuple(sample.translation[2] for sample in target.samples)),
        )
        encoded_channels: list[EncodedG4MTChannel] = []
        for channel_type, values in values_by_type:
            component_count = 4 if channel_type == 9 else 1
            bytes_per_key = component_count * 4
            samples = [tuple(values[start:start + component_count])
                       for start in range(0, len(values), component_count)]
            if channel_type == 9:
                normalized = []
                for sample in samples:
                    length = math.sqrt(sum(value * value for value in sample))
                    if length < 1e-8:
                        raise ValueError(f"G4MT target {target.name!r} has a zero-length rotation")
                    sample = tuple(value / length for value in sample)
                    if normalized and sum(left * right for left, right in zip(normalized[-1], sample)) < 0:
                        sample = tuple(-value for value in sample)
                    normalized.append(sample)
                samples = normalized
            tolerance = 0.0 if any(marker in target.name.lower() for marker in PRECISION_FACE_MARKERS) else 1e-5
            key_frames, key_samples = simplify_samples(frames, samples, tolerance)
            key_bytes = bytearray()
            data_bytes = bytearray()
            for frame, components in zip(key_frames, key_samples):
                if frame > 0xFFFF:
                    raise ValueError("G4MT frame number exceeds the 16-bit key limit")
                if not all(math.isfinite(value) for value in components):
                    raise ValueError(f"G4MT target {target.name!r} has a non-finite channel value")
                key_bytes.extend(struct.pack("<H", frame))
                data_bytes.extend(struct.pack(f"<{component_count}f", *components))
            header = struct.pack("<8B", channel_type, 1, 1, 4, component_count,
                                 bytes_per_key, 0, 0)
            encoded_channels.append((header, bytes(key_bytes), bytes(data_bytes)))
        encoded_targets.append(tuple(encoded_channels))
    return target_hashes, tuple(encoded_targets)


def write_g4mt_clip(
    template: bytes,
    template_clip_index: int,
    clip_name: str,
    targets: Sequence[G4MTTargetSamples],
) -> bytes:
    """Write one clip using the frame range, flags, and FPS of a template clip.

    Target samples must already use the local coordinate space and bone axes
    that the destination G4SK will store.
    """
    source = parse_g4mt(template, "G4MT template")
    if source.header.file_type != 0x68:
        raise ValueError("G4MT template has an unsupported file type")
    if not 0 <= template_clip_index < len(source.clips):
        raise IndexError("G4MT template clip index is out of range")
    name_bytes = clip_name.encode("utf-8")
    template_clip = source.clips[template_clip_index]
    targets = _targets_parent_first(targets)
    target_hashes, encoded_targets = _encode_clip_samples(template_clip, clip_name, targets)
    clip_hash = crc32b(clip_name)
    channel_records = bytearray()
    target_info_records = bytearray()
    key_records = bytearray()
    data_records = bytearray()
    for target_index, encoded_channels in enumerate(encoded_targets):
        if len(channel_records) // 20 > 0xFFFFFF:
            raise ValueError("G4MT channel table exceeds its 24-bit index limit")
        channel_start = len(channel_records) // 20
        for header, key_bytes, data_bytes in encoded_channels:
            key_start = len(key_records) // 2
            data_start = len(data_records)
            key_count = len(key_bytes) // 2
            channel_records.extend(header)
            channel_records.extend(struct.pack("<III", key_start, data_start, key_count))
            key_records.extend(key_bytes)
            data_records.extend(data_bytes)
        target_info_records.extend(struct.pack(
            "<HHBBH", target_index, channel_start & 0xFFFF,
            len(encoded_channels), channel_start >> 16, 0
        ))

    clip_row = bytearray(template[0x40 + template_clip_index * 0x10:0x50 + template_clip_index * 0x10])
    if len(clip_row) != 0x10:
        raise ValueError("G4MT template clip record is truncated")
    struct.pack_into("<HHHH", clip_row, 0, template_clip.start_frame, template_clip.end_frame, 0, len(targets))
    clip_row[12] = 0
    clip_name_block = bytearray(struct.pack("<H", 0))
    clip_name_block.extend(b"\0\0")
    clip_name_block.extend(struct.pack("<H", 2))
    clip_name_block.extend(name_bytes + b"\0")

    output = bytearray(template[:0x40])
    output.extend(clip_row)
    header_words = struct.unpack_from("<H", template, 0x0A)[0]
    offset_shift = 2
    section_offsets: dict[str, int] = {}

    def append_section(name: str, data: bytes | bytearray, alignment: int) -> None:
        padding = (-len(output)) % alignment
        output.extend(b"\0" * padding)
        section_offsets[name] = len(output)
        output.extend(data)

    append_section("scales", struct.pack("<f", 1.0), 4)
    append_section("clip_hashes", struct.pack("<I", clip_hash), 4)
    append_section("target_hashes", struct.pack(f"<{len(target_hashes)}I", *target_hashes), 4)
    append_section("names", clip_name_block, 4)
    append_section("target_infos", target_info_records, 4 << offset_shift)
    append_section("channels", channel_records, 4 << offset_shift)
    append_section("keys", key_records, 4 << (offset_shift * 2))
    append_section("data", data_records, 4 << (offset_shift * 2))

    def write_offset(field: int, section: str, shift: int) -> None:
        units = (section_offsets[section] // 4 - header_words) >> shift
        if ((header_words + (units << shift)) * 4) != section_offsets[section] or units > 0xFFFF:
            raise ValueError(f"G4MT {section} section offset is not representable")
        struct.pack_into("<H", output, field, units)

    struct.pack_into("<H", output, 0x20, 1)
    struct.pack_into("<H", output, 0x22, len(target_hashes))
    output[0x36] = offset_shift
    for field, section in ((0x28, "scales"), (0x2A, "clip_hashes"),
                           (0x2C, "target_hashes"), (0x2E, "names")):
        write_offset(field, section, 0)
    write_offset(0x24, "target_infos", offset_shift)
    write_offset(0x26, "channels", offset_shift)
    write_offset(0x30, "keys", offset_shift * 2)
    write_offset(0x32, "data", offset_shift * 2)
    struct.pack_into("<I", output, 0x0C, len(output) - 0x40)

    result = bytes(output)
    readback = parse_g4mt(result, f"generated:{clip_name}")
    if len(readback.clips) != 1 or readback.clips[0].name != clip_name:
        raise ValueError("Generated G4MT clip failed its structural read-back")
    if len(readback.targets) != len(targets) or len(readback.channels) != len(channel_records) // 20:
        raise ValueError("Generated G4MT target/channel tables failed their structural read-back")
    if [target.name_hash for target in readback.targets] != target_hashes:
        raise ValueError("Generated G4MT target hashes failed their structural read-back")
    return result


def write_g4mt_family(template: bytes, clips: Sequence[G4MTClipSamples]) -> bytes:
    """Write several clips for one target rig as a single G4MT bank."""
    if not clips:
        raise ValueError("G4MT family requires at least one clip")
    if len(clips) > 0xFFFF:
        raise ValueError("G4MT family exceeds the clip table limit")

    source = parse_g4mt(template, "G4MT family template")
    if source.header.file_type != 0x68:
        raise ValueError("G4MT template has an unsupported file type")
    clips = tuple(
        G4MTClipSamples(
            clip.template_clip_index,
            clip.name,
            _targets_parent_first(clip.targets),
        )
        for clip in clips
    )

    target_names_by_first_appearance = tuple(dict.fromkeys(
        target.name for clip in clips for target in clip.targets
    ))
    target_parents: dict[str, str] = {}
    for clip in clips:
        for target in clip.targets:
            if target.parent_index is None:
                continue
            parent_name = clip.targets[target.parent_index].name
            previous_parent = target_parents.setdefault(target.name, parent_name)
            if previous_parent != parent_name:
                raise ValueError(f"G4MT target {target.name!r} has conflicting parents across clips")

    target_names_list: list[str] = []
    target_states: dict[str, int] = {}

    def visit_target(name: str) -> None:
        state = target_states.get(name, 0)
        if state == 2:
            return
        if state == 1:
            raise ValueError("G4MT family target hierarchy contains a cycle")
        target_states[name] = 1
        parent_name = target_parents.get(name)
        if parent_name is not None:
            visit_target(parent_name)
        target_states[name] = 2
        target_names_list.append(name)

    for name in target_names_by_first_appearance:
        visit_target(name)
    target_names = tuple(target_names_list)
    if not target_names:
        raise ValueError("G4MT family requires at least one target track")
    if len(target_names) > 0xFFFF:
        raise ValueError("G4MT family exceeds the target table limit")
    target_index_by_name = {name: index for index, name in enumerate(target_names)}
    clip_names = [clip.name for clip in clips]
    clip_hashes = [crc32b(name) for name in clip_names]
    target_hashes = [crc32b(name) for name in target_names]
    if len(set(target_hashes)) != len(target_hashes):
        raise ValueError("G4SK target names contain a CRC32 collision")

    header_words = source.header.header_size // 4
    minimum_offset_shift = max(2, source.header.offset_shift)
    output = bytearray(template[:0x40])
    target_infos = bytearray()
    channels = bytearray()
    keys = bytearray()
    data = bytearray()
    shared_channel_blocks: dict[EncodedG4MTTarget, int] = {}
    for clip in clips:
        if not 0 <= clip.template_clip_index < len(source.clips):
            raise IndexError("G4MT template clip index is out of range")
        template_clip = source.clips[clip.template_clip_index]
        _clip_target_hashes, encoded_targets = _encode_clip_samples(template_clip, clip.name, clip.targets)

        row = bytearray(template[0x40 + clip.template_clip_index * 0x10:
                                 0x50 + clip.template_clip_index * 0x10])
        if len(row) != 0x10:
            raise ValueError("G4MT template clip record is truncated")
        target_info_start = len(target_infos) // 8
        if target_info_start > 0xFFFFFF:
            raise ValueError("G4MT family target-info table exceeds its 24-bit index limit")
        struct.pack_into("<HHHH", row, 0, template_clip.start_frame, template_clip.end_frame,
                         target_info_start & 0xFFFF, len(encoded_targets))
        row[10] = target_info_start >> 16
        struct.pack_into("<I", row, 12, 0)
        output.extend(row)

        for target, encoded_channels in zip(clip.targets, encoded_targets):
            if len(encoded_channels) > 0xFF:
                raise ValueError("G4MT target has more than 255 animation channels")
            signature = encoded_channels
            channel_start = shared_channel_blocks.get(signature)
            if channel_start is None:
                channel_start = len(channels) // 20
                if channel_start > 0xFFFFFF:
                    raise ValueError("G4MT family channel table exceeds its 24-bit index limit")
                shared_channel_blocks[signature] = channel_start
                for header, key_bytes, data_bytes in encoded_channels:
                    key_start = len(keys) // 2
                    data_start = len(data)
                    key_count = len(key_bytes) // 2
                    channels.extend(header)
                    channels.extend(struct.pack("<III", key_start, data_start, key_count))
                    keys.extend(key_bytes)
                    data.extend(data_bytes)
            target_infos.extend(struct.pack(
                "<HHBBH", target_index_by_name[target.name], channel_start & 0xFFFF,
                len(encoded_channels), channel_start >> 16, 0
            ))

    scales = struct.pack("<f", 1.0)
    clip_hash_table = struct.pack(f"<{len(clip_hashes)}I", *clip_hashes)
    target_hash_table = struct.pack(f"<{len(target_hashes)}I", *target_hashes)
    name_table = bytearray(struct.pack(f"<{len(clips)}H", *_clip_hash_order(clip_hashes)))
    name_table.extend(b"\0" * (-len(name_table) % 4))
    name_offsets_table = len(name_table)
    name_table.extend(b"\0" * (len(clips) * 2))
    for index, name in enumerate(clip_names):
        relative = len(name_table) - name_offsets_table
        if relative > 0xFFFF:
            raise ValueError("G4MT clip-name table exceeds its 16-bit offset limit")
        struct.pack_into("<H", name_table, name_offsets_table + index * 2, relative)
        name_table.extend(name.encode("utf-8") + b"\0")

    offsets: dict[str, int] = {}

    def append_section(name: str, section: bytes | bytearray, alignment: int) -> None:
        output.extend(b"\0" * (-(len(output) - header_words * 4) % alignment))
        offsets[name] = len(output)
        output.extend(section)

    sections = (
        ("scales", scales, 4),
        ("clip_hashes", clip_hash_table, 4),
        ("target_hashes", target_hash_table, 4),
        ("names", name_table, 4),
    )

    def can_represent_offsets(shift: int) -> bool:
        offset = 0x40 + len(clips) * 0x10
        for name, section, _alignment in sections + (
            ("target_infos", target_infos, 4 << shift),
            ("channels", channels, 4 << shift),
            ("keys", keys, 4 << (shift * 2)),
            ("data", data, 4 << (shift * 2)),
        ):
            alignment = _alignment
            offset += -((offset - header_words * 4) % alignment)
            section_shift = 0 if name in {"scales", "clip_hashes", "target_hashes", "names"} else (
                shift * 2 if name in {"keys", "data"} else shift
            )
            units = (offset // 4 - header_words) >> section_shift
            if (header_words + (units << section_shift)) * 4 != offset or units > 0xFFFF:
                return False
            offset += len(section)
        return True

    offset_shift = next(
        (shift for shift in range(minimum_offset_shift, 5) if can_represent_offsets(shift)),
        None,
    )
    if offset_shift is None:
        raise ValueError("G4MT family exceeds the representable section-offset range")

    for name, section, alignment in sections + (
        ("target_infos", target_infos, 4 << offset_shift),
        ("channels", channels, 4 << offset_shift),
        ("keys", keys, 4 << (offset_shift * 2)),
        ("data", data, 4 << (offset_shift * 2)),
    ):
        append_section(name, section, alignment)

    def write_offset(field: int, name: str, shift: int) -> None:
        units = (offsets[name] // 4 - header_words) >> shift
        if (header_words + (units << shift)) * 4 != offsets[name] or units > 0xFFFF:
            raise ValueError(f"G4MT {name} section offset is not representable")
        struct.pack_into("<H", output, field, units)

    struct.pack_into("<HH", output, 0x20, len(clips), len(target_hashes))
    output[0x36] = offset_shift
    for field, name in ((0x28, "scales"), (0x2A, "clip_hashes"),
                        (0x2C, "target_hashes"), (0x2E, "names")):
        write_offset(field, name, 0)
    write_offset(0x24, "target_infos", offset_shift)
    write_offset(0x26, "channels", offset_shift)
    write_offset(0x30, "keys", offset_shift * 2)
    write_offset(0x32, "data", offset_shift * 2)
    struct.pack_into("<I", output, 0x0C, len(output) - 0x40)

    result = bytes(output)
    readback = parse_g4mt(result, "generated G4MT family")
    if not _has_clip_hash_order(result, clip_hashes):
        raise ValueError("Generated G4MT family has an invalid clip-hash search order")
    if tuple(clip.name for clip in readback.clips) != tuple(clip_names):
        raise ValueError("Generated G4MT family failed clip-name read-back")
    if len(readback.targets) != len(target_hashes) or len(readback.channels) != len(channels) // 20:
        raise ValueError(
            f"Generated G4MT family has {len(readback.targets)} targets and "
            f"{len(readback.channels)} channels; expected {len(target_hashes)} and "
            f"{len(channels) // 20}"
        )
    return result


def write_g4mt_family_preserving_additive(
    template: bytes, clips: Sequence[G4MTClipSamples],
) -> bytes:
    source = parse_g4mt(template, "G4MT family template")
    additive = {clip.index for clip in source.clips if clip.flags & 1}
    expected_generated = {clip.index for clip in source.clips} - additive
    generated_source_indices = [clip.template_clip_index for clip in clips]
    if len(generated_source_indices) != len(expected_generated) or set(generated_source_indices) != expected_generated:
        raise ValueError("G4MT family export must provide every non-additive source clip exactly once")
    generated_bytes = write_g4mt_family(template, clips)
    generated = parse_g4mt(generated_bytes, "generated G4MT family")
    generated_by_source_index = dict(zip(generated_source_indices, generated.clips))

    target_hashes = list(target.name_hash for target in generated.targets)
    target_index = {value: index for index, value in enumerate(target_hashes)}
    for target in source.targets:
        if target.name_hash not in target_index:
            target_index[target.name_hash] = len(target_hashes)
            target_hashes.append(target.name_hash)
    if len(target_hashes) > 0xFFFF:
        raise ValueError("G4MT family exceeds the target table limit")

    scales = list(source.scales)
    scale_indices = {value: index for index, value in enumerate(scales)}
    scale_maps = []
    for bank in (source, generated):
        mapping = []
        for value in bank.scales:
            if value not in scale_indices:
                scale_indices[value] = len(scales)
                scales.append(value)
            mapping.append(scale_indices[value])
        scale_maps.append(mapping)
    if len(scales) > 0x100:
        raise ValueError("G4MT family exceeds the scale table limit")

    target_infos = bytearray()
    channels = bytearray()
    keys = bytearray()
    data = bytearray()
    rows = bytearray()
    clip_hashes = []
    shared_channel_blocks = {}
    for source_clip in source.clips:
        passthrough = source_clip.index in additive
        bank = source if passthrough else generated
        bank_bytes = template if passthrough else generated_bytes
        bank_clip = source_clip if passthrough else generated_by_source_index.get(source_clip.index)
        if bank_clip is None or bank_clip.name != source_clip.name:
            raise ValueError(f"G4MT family is missing source clip {source_clip.name!r}")
        bank_index = 0 if passthrough else 1
        target_info_start = len(target_infos) // 8
        if target_info_start > 0xFFFFFF:
            raise ValueError("G4MT family target-info table exceeds its 24-bit index limit")
        info_slice = bank.target_infos[
            bank_clip.target_info_start:bank_clip.target_info_start + bank_clip.target_info_count
        ]
        for info in info_slice:
            target_hash = bank.targets[info.target_index].name_hash
            if info.channel_count > 0xFF:
                raise ValueError("G4MT target has more than 255 animation channels")
            block_key = (bank_index, info.channel_start, info.channel_count)
            channel_start = shared_channel_blocks.get(block_key)
            if channel_start is None:
                channel_start = len(channels) // 20
                if channel_start > 0xFFFFFF:
                    raise ValueError("G4MT family channel table exceeds its 24-bit index limit")
                shared_channel_blocks[block_key] = channel_start
                for channel_index in range(info.channel_start, info.channel_start + info.channel_count):
                    channel = bank.channels[channel_index]
                    key_start = len(keys) // 2
                    data_start = len(data)
                    keys.extend(struct.pack(f"<{channel.key_count}H", *channel.keys))
                    data.extend(bank_bytes[
                        bank.header.data_offset + channel.data_offset:
                        bank.header.data_offset + channel.data_offset + channel.key_count * channel.bytes_per_key
                    ])
                    record_offset = bank.header.channel_offset + channel.index * 20
                    record = bytearray(bank_bytes[record_offset:record_offset + 20])
                    record[6] = scale_maps[bank_index][channel.scale_index]
                    struct.pack_into("<III", record, 8, key_start, data_start, channel.key_count)
                    channels.extend(record)
            target_infos.extend(struct.pack(
                "<HHBBH", target_index[target_hash], channel_start & 0xFFFF,
                info.channel_count, channel_start >> 16, info.reserved
            ))

        row = bytearray(template[0x40 + source_clip.index * 0x10:0x50 + source_clip.index * 0x10])
        struct.pack_into("<HHHH", row, 0, source_clip.start_frame, source_clip.end_frame,
                         target_info_start & 0xFFFF, len(info_slice))
        row[10] = target_info_start >> 16
        rows.extend(row)
        clip_hashes.append(source_clip.name_hash)

    names = bytearray(struct.pack(f"<{len(source.clips)}H", *_clip_hash_order(clip_hashes)))
    names.extend(b"\0" * (-len(names) % 4))
    name_offsets = len(names)
    names.extend(b"\0" * (len(source.clips) * 2))
    for index, clip in enumerate(source.clips):
        relative = len(names) - name_offsets
        if relative > 0xFFFF:
            raise ValueError("G4MT clip-name table exceeds its 16-bit offset limit")
        struct.pack_into("<H", names, name_offsets + index * 2, relative)
        names.extend(clip.name.encode("utf-8") + b"\0")

    header_words = struct.unpack_from("<H", template, 0x0A)[0]
    sections = (
        ("scales", struct.pack(f"<{len(scales)}f", *scales), 4),
        ("clip_hashes", struct.pack(f"<{len(clip_hashes)}I", *clip_hashes), 4),
        ("target_hashes", struct.pack(f"<{len(target_hashes)}I", *target_hashes), 4),
        ("names", names, 4),
    )
    output = bytearray(template[:0x40])
    output.extend(rows)
    offsets = {}

    def append_section(name: str, section: bytes | bytearray, alignment: int) -> None:
        output.extend(b"\0" * (-(len(output) - header_words * 4) % alignment))
        offsets[name] = len(output)
        output.extend(section)

    def can_represent(shift: int) -> bool:
        offset = 0x40 + len(source.clips) * 0x10
        for name, section, alignment in sections + (
            ("target_infos", target_infos, 4 << shift),
            ("channels", channels, 4 << shift),
            ("keys", keys, 4 << (shift * 2)),
            ("data", data, 4 << (shift * 2)),
        ):
            offset += -((offset - header_words * 4) % alignment)
            section_shift = 0 if name in {"scales", "clip_hashes", "target_hashes", "names"} else (
                shift * 2 if name in {"keys", "data"} else shift
            )
            units = (offset // 4 - header_words) >> section_shift
            if (header_words + (units << section_shift)) * 4 != offset or units > 0xFFFF:
                return False
            offset += len(section)
        return True

    minimum_shift = max(2, template[0x36])
    offset_shift = next((shift for shift in range(minimum_shift, 5) if can_represent(shift)), None)
    if offset_shift is None:
        raise ValueError("G4MT family exceeds the representable section-offset range")
    for name, section, alignment in sections + (
        ("target_infos", target_infos, 4 << offset_shift),
        ("channels", channels, 4 << offset_shift),
        ("keys", keys, 4 << (offset_shift * 2)),
        ("data", data, 4 << (offset_shift * 2)),
    ):
        append_section(name, section, alignment)

    struct.pack_into("<HH", output, 0x20, len(source.clips), len(target_hashes))
    output[0x36] = offset_shift
    for field, name in ((0x28, "scales"), (0x2A, "clip_hashes"),
                        (0x2C, "target_hashes"), (0x2E, "names")):
        units = offsets[name] // 4 - header_words
        if units > 0xFFFF:
            raise ValueError(f"G4MT {name} section offset is not representable")
        struct.pack_into("<H", output, field, units)
    for field, name, shift in (
        (0x24, "target_infos", offset_shift), (0x26, "channels", offset_shift),
        (0x30, "keys", offset_shift * 2), (0x32, "data", offset_shift * 2),
    ):
        units = (offsets[name] // 4 - header_words) >> shift
        if (header_words + (units << shift)) * 4 != offsets[name] or units > 0xFFFF:
            raise ValueError(f"G4MT {name} section offset is not representable")
        struct.pack_into("<H", output, field, units)
    struct.pack_into("<I", output, 0x0C, len(output) - 0x40)
    result = bytes(output)
    readback = parse_g4mt(result, "G4MT family with additive passthrough")
    if not _has_clip_hash_order(result, clip_hashes):
        raise ValueError("G4MT family with additive passthrough has an invalid clip-hash search order")
    if tuple(clip.name for clip in readback.clips) != tuple(clip.name for clip in source.clips):
        raise ValueError("G4MT family clip order changed during additive passthrough")
    for index in additive:
        original = source.clips[index]
        actual = readback.clips[index]
        if actual.flags != original.flags:
            raise ValueError(f"G4MT additive clip {original.name!r} changed its flags")
        for original_info, actual_info in zip(
            source.target_infos[original.target_info_start:original.target_info_start + original.target_info_count],
            readback.target_infos[actual.target_info_start:actual.target_info_start + actual.target_info_count],
        ):
            if source.targets[original_info.target_index].name_hash != readback.targets[actual_info.target_index].name_hash:
                raise ValueError(f"G4MT additive clip {original.name!r} changed its targets")
    return result
