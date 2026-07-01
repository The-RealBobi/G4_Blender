#!/usr/bin/env python3
"""Decode G4MT transform curves and export one clip as JSON or glTF 2.0."""

from __future__ import annotations

import argparse
import base64
import bisect
import json
import math
import struct
import sys
from html import escape
from pathlib import Path

try:
    from .g4mt_probe import companion_g4sk, crc32b, g4sk_name_table_offset, parse_g4mt, read_g4sk_data, u16
except ImportError:
    from g4mt_probe import companion_g4sk, crc32b, g4sk_name_table_offset, parse_g4mt, read_g4sk_data, u16


CHANNEL_PATHS = {
    1: ("scale", 0),
    2: ("scale", 1),
    3: ("scale", 2),
    9: ("rotation", None),
    10: ("translation", 0),
    11: ("translation", 1),
    12: ("translation", 2),
}

PRECISION_FACE_MARKERS = ("eye", "eld", "ebw", "lgt")


def matrix_mul(a: list[float], b: list[float]) -> list[float]:
    return [
        sum(a[row * 4 + k] * b[k * 4 + col] for k in range(4))
        for row in range(4)
        for col in range(4)
    ]


def rigid_inverse(matrix: list[float]) -> list[float]:
    r00, r01, r02, tx = matrix[0:4]
    r10, r11, r12, ty = matrix[4:8]
    r20, r21, r22, tz = matrix[8:12]
    return [
        r00, r10, r20, -(r00 * tx + r10 * ty + r20 * tz),
        r01, r11, r21, -(r01 * tx + r11 * ty + r21 * tz),
        r02, r12, r22, -(r02 * tx + r12 * ty + r22 * tz),
        0.0, 0.0, 0.0, 1.0,
    ]


def quaternion_from_matrix(matrix: list[float]) -> list[float]:
    m00, m01, m02 = matrix[0], matrix[1], matrix[2]
    m10, m11, m12 = matrix[4], matrix[5], matrix[6]
    m20, m21, m22 = matrix[8], matrix[9], matrix[10]
    trace = m00 + m11 + m22
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        quat = [(m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s, 0.25 * s]
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        quat = [0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s]
    elif m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        quat = [(m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s]
    else:
        s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
        quat = [(m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s]
    return normalize_quaternion(quat)


def matrix_trs(matrix: list[float]) -> dict[str, list[float]]:
    translation = [matrix[3], matrix[7], matrix[11]]
    scale = [
        math.sqrt(matrix[0] ** 2 + matrix[4] ** 2 + matrix[8] ** 2),
        math.sqrt(matrix[1] ** 2 + matrix[5] ** 2 + matrix[9] ** 2),
        math.sqrt(matrix[2] ** 2 + matrix[6] ** 2 + matrix[10] ** 2),
    ]
    rotation_matrix = matrix[:]
    for col in range(3):
        divisor = scale[col] or 1.0
        for row in range(3):
            rotation_matrix[row * 4 + col] /= divisor
    return {"translation": translation, "rotation": quaternion_from_matrix(rotation_matrix), "scale": scale}


def trs_matrix(translation: list[float], rotation: list[float], scale: list[float]) -> list[float]:
    x, y, z, w = normalize_quaternion(rotation)
    sx, sy, sz = scale
    return [
        (1.0 - 2.0 * (y * y + z * z)) * sx,
        (2.0 * (x * y - z * w)) * sy,
        (2.0 * (x * z + y * w)) * sz,
        translation[0],
        (2.0 * (x * y + z * w)) * sx,
        (1.0 - 2.0 * (x * x + z * z)) * sy,
        (2.0 * (y * z - x * w)) * sz,
        translation[1],
        (2.0 * (x * z - y * w)) * sx,
        (2.0 * (y * z + x * w)) * sy,
        (1.0 - 2.0 * (x * x + y * y)) * sz,
        translation[2],
        0.0, 0.0, 0.0, 1.0,
    ]


def parse_skeleton(path: Path) -> dict:
    data = read_g4sk_data(path)
    if data[:4] != b"G4SK":
        raise ValueError(f"{path} is not a G4SK file")
    count = u16(data, 0x20)
    sections = [0x40 + u16(data, 0x24 + index * 2) * 4 for index in range(8)]
    global_matrices = []
    for index in range(count):
        values = struct.unpack_from("<12f", data, 0x40 + index * 0x30)
        global_matrices.append([
            values[0], values[1], values[2], values[3],
            values[4], values[5], values[6], values[7],
            values[8], values[9], values[10], values[11],
            0.0, 0.0, 0.0, 1.0,
        ])
    parents = [u16(data, sections[3] + index * 2) for index in range(count)]
    name_table = g4sk_name_table_offset(data, count)
    names = []
    for index in range(count):
        offset = u16(data, name_table + index * 2)
        end = data.find(b"\0", name_table + offset)
        names.append(data[name_table + offset:end].decode("utf-8", errors="replace"))
    local_matrices = []
    for index, matrix in enumerate(global_matrices):
        parent = parents[index]
        local_matrices.append(
            matrix_mul(rigid_inverse(global_matrices[parent]), matrix)
            if parent < count and parent != index else matrix
        )
    return {
        "path": str(path),
        "count": count,
        "names": names,
        "parents": parents,
        "local_trs": [matrix_trs(matrix) for matrix in local_matrices],
        "hash_to_index": {crc32b(name): index for index, name in enumerate(names)},
    }


def normalize_quaternion(value: list[float]) -> list[float]:
    length = math.sqrt(sum(component * component for component in value))
    return [component / length for component in value] if length else [0.0, 0.0, 0.0, 1.0]


def slerp(a: list[float], b: list[float], amount: float) -> list[float]:
    a = normalize_quaternion(a)
    b = normalize_quaternion(b)
    dot = sum(x * y for x, y in zip(a, b))
    if dot < 0.0:
        b = [-value for value in b]
        dot = -dot
    if dot > 0.9995:
        return normalize_quaternion([x + (y - x) * amount for x, y in zip(a, b)])
    theta = math.acos(max(-1.0, min(1.0, dot)))
    scale = math.sin(theta)
    left = math.sin((1.0 - amount) * theta) / scale
    right = math.sin(amount * theta) / scale
    return [x * left + y * right for x, y in zip(a, b)]


def decode_key(data: bytes, data_base: int, channel: dict, scale: float, key_index: int) -> list[float]:
    encoding = channel["encoding"]
    component_count = encoding[4]
    offset = data_base + channel["data_offset"] + key_index * encoding[5]
    codec, variant = encoding[1], encoding[3]
    if codec == 1 and variant == 1:
        return list(struct.unpack_from(f"<{component_count}b", data, offset))
    if codec == 1 and variant == 2:
        return list(struct.unpack_from(f"<{component_count}h", data, offset))
    if codec == 1 and variant == 4:
        return list(struct.unpack_from(f"<{component_count}f", data, offset))
    if codec == 2 and variant == 2:
        values = struct.unpack_from(f"<{component_count}H", data, offset)
        return [value * scale / 65536.0 for value in values]
    if codec == 3 and variant == 2:
        values = struct.unpack_from(f"<{component_count}h", data, offset)
        return [value * scale / 32768.0 for value in values]
    raise ValueError(f"unsupported G4MT codec {codec}/{variant}: {encoding}")


def sample_channel(data: bytes, data_base: int, channel: dict, scale: float, frame: float) -> list[float]:
    keys = channel["keys"]
    if not keys:
        return []
    right = bisect.bisect_right(keys, frame)
    if right == 0:
        return decode_key(data, data_base, channel, scale, 0)
    left = max(0, min(len(keys) - 1, right - 1))
    if left == len(keys) - 1 or encoding_step(channel):
        return decode_key(data, data_base, channel, scale, left)
    span = keys[left + 1] - keys[left]
    amount = (frame - keys[left]) / span if span else 0.0
    first = decode_key(data, data_base, channel, scale, left)
    second = decode_key(data, data_base, channel, scale, left + 1)
    if channel["channel_type"] == 9:
        return slerp(first, second, amount)
    return [a + (b - a) * amount for a, b in zip(first, second)]


def simplify_samples(frames, samples, tolerance: float = 1e-5):
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
            error = sum(
                (value - (start + (end - start) * amount)) ** 2
                for value, start, end in zip(samples[index], samples[first], samples[last])
            )
            if error > worst_error:
                worst_error = error
                worst_index = index
        if worst_index is not None:
            keep.add(worst_index)
            stack.append((first, worst_index))
            stack.append((worst_index, last))
    indices = sorted(keep)
    return [frames[index] for index in indices], [samples[index] for index in indices]


def simplify_motion_samples(frames, samples, target_name: str | None):
    name = (target_name or "").lower()
    tolerance = 0.0 if any(marker in name for marker in PRECISION_FACE_MARKERS) else 1e-5
    return simplify_samples(frames, samples, tolerance)


def encoding_step(channel: dict) -> bool:
    return channel["encoding"][2] == 0


def select_clip(clips: list[dict], selector: str) -> dict:
    if selector.isdigit():
        index = int(selector)
        if index < len(clips):
            return clips[index]
    for clip in clips:
        if clip["name"] == selector:
            return clip
    raise ValueError(f"clip not found: {selector}")


def decode_motion(path: Path, clip_selector: str, skeleton_path: Path | None) -> dict:
    parsed = parse_g4mt(path, skeleton_path)
    clip = select_clip(parsed["clips"], clip_selector)
    if clip["flags"] & 1:
        raise ValueError("additive G4MT clips need a base pose and are not exported yet")
    data = path.read_bytes()
    target_hashes = {int(target["crc32b"], 16) for target in parsed["targets"]}
    skeleton_file = skeleton_path or (Path(parsed["skeleton"]) if parsed["skeleton"] else companion_g4sk(path, target_hashes))
    skeleton = parse_skeleton(skeleton_file) if skeleton_file and skeleton_file.is_file() else None
    target_infos = parsed["target_infos"][
        clip["target_info_start"]:clip["target_info_start"] + clip["target_info_count"]
    ]
    fps = clip["fps"] or 60
    frames = list(range(clip["start_frame"], clip["end_frame"] + 1))
    times = [(frame - clip["start_frame"]) / fps for frame in frames]
    tracks = []
    for info in target_infos:
        target = parsed["targets"][info["target_index"]]
        skeleton_index = None
        rest = {"scale": [1.0, 1.0, 1.0], "rotation": [0.0, 0.0, 0.0, 1.0], "translation": [0.0, 0.0, 0.0]}
        if skeleton:
            skeleton_index = skeleton["hash_to_index"].get(int(target["crc32b"], 16))
            if skeleton_index is not None:
                rest = skeleton["local_trs"][skeleton_index]
        channels = parsed["channels"][info["channel_start"]:info["channel_start"] + info["channel_count"]]
        values = {path_name: [] for path_name in ("scale", "rotation", "translation")}
        animated_paths = set()
        for frame in frames:
            pose = {name: components[:] for name, components in rest.items()}
            for channel in channels:
                mapping = CHANNEL_PATHS.get(channel["channel_type"])
                if mapping is None:
                    continue
                path_name, component = mapping
                scale = parsed["scales"][channel["encoding"][6]]
                decoded = sample_channel(data, parsed["section_offsets"]["data"], channel, scale, frame)
                if component is None:
                    pose[path_name] = normalize_quaternion(decoded)
                else:
                    pose[path_name][component] = decoded[0]
                animated_paths.add(path_name)
            for path_name in values:
                value = pose[path_name]
                if path_name == "rotation" and values[path_name]:
                    previous = values[path_name][-1]
                    if sum(a * b for a, b in zip(previous, value)) < 0.0:
                        value = [-component for component in value]
                values[path_name].append(value)
        tracks.append({
            "target_index": info["target_index"],
            "target_hash": target["crc32b"],
            "target_name": target["name"],
            "skeleton_index": skeleton_index,
            "animated_paths": sorted(animated_paths),
            "values": values,
        })
    return {
        "source": str(path),
        "skeleton": skeleton,
        "clip": clip,
        "frames": frames,
        "times": times,
        "tracks": tracks,
    }


def write_json(motion: dict, output: Path) -> None:
    payload = dict(motion)
    if payload["skeleton"]:
        payload["skeleton"] = {key: value for key, value in payload["skeleton"].items() if key != "hash_to_index"}
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class GltfBuffer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.views = []
        self.accessors = []

    def accessor(self, values: list[list[float]] | list[float], components: int) -> int:
        while len(self.data) % 4:
            self.data.append(0)
        offset = len(self.data)
        flat = values if components == 1 else [component for value in values for component in value]
        self.data.extend(struct.pack(f"<{len(flat)}f", *flat))
        view = len(self.views)
        self.views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(flat) * 4})
        accessor = {"bufferView": view, "componentType": 5126, "count": len(flat) // components, "type": {1: "SCALAR", 3: "VEC3", 4: "VEC4"}[components]}
        if components == 1 and flat:
            accessor["min"] = [min(flat)]
            accessor["max"] = [max(flat)]
        self.accessors.append(accessor)
        return len(self.accessors) - 1


def write_gltf(motion: dict, output: Path) -> None:
    skeleton = motion["skeleton"]
    if not skeleton:
        raise ValueError("glTF export requires a companion G4SK skeleton")
    children = [[] for _ in range(skeleton["count"])]
    roots = []
    nodes = []
    for index, name in enumerate(skeleton["names"]):
        parent = skeleton["parents"][index]
        if parent < skeleton["count"] and parent != index:
            children[parent].append(index)
        else:
            roots.append(index)
        node = {"name": name, **skeleton["local_trs"][index]}
        nodes.append(node)
    for index, node_children in enumerate(children):
        if node_children:
            nodes[index]["children"] = node_children

    binary = GltfBuffer()
    time_accessor = binary.accessor(motion["times"], 1)
    samplers = []
    animation_channels = []
    for track in motion["tracks"]:
        node_index = track["skeleton_index"]
        if node_index is None:
            continue
        for path_name in track["animated_paths"]:
            components = 4 if path_name == "rotation" else 3
            output_accessor = binary.accessor(track["values"][path_name], components)
            sampler_index = len(samplers)
            samplers.append({"input": time_accessor, "output": output_accessor, "interpolation": "LINEAR"})
            animation_channels.append({"sampler": sampler_index, "target": {"node": node_index, "path": path_name}})

    document = {
        "asset": {"version": "2.0", "generator": "LEVEL 5 ENGINE G4MT research tools"},
        "scene": 0,
        "scenes": [{"nodes": roots}],
        "nodes": nodes,
        "animations": [{"name": motion["clip"]["name"], "samplers": samplers, "channels": animation_channels}],
        "buffers": [{"byteLength": len(binary.data), "uri": "data:application/octet-stream;base64," + base64.b64encode(binary.data).decode("ascii")}],
        "bufferViews": binary.views,
        "accessors": binary.accessors,
        "extras": {"g4mtSource": motion["source"], "g4skSource": skeleton["path"]},
    }
    output.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def dae_floats(values: list[float] | list[list[float]]) -> str:
    if values and isinstance(values[0], list):
        values = [component for row in values for component in row]
    return " ".join(format(float(value), ".9g") for value in values)


def write_dae(motion: dict, output: Path) -> None:
    skeleton = motion["skeleton"]
    if not skeleton:
        raise ValueError("DAE export requires a companion or model-resolved G4SK skeleton")

    track_by_joint = {
        track["skeleton_index"]: track
        for track in motion["tracks"]
        if track["skeleton_index"] is not None
    }
    children = [[] for _ in range(skeleton["count"])]
    roots = []
    for index, parent in enumerate(skeleton["parents"]):
        if parent < skeleton["count"] and parent != index:
            children[parent].append(index)
        else:
            roots.append(index)

    def joint_xml(index: int) -> str:
        name = skeleton["names"][index] or f"joint_{index:03d}"
        rest = skeleton["local_trs"][index]
        matrix = trs_matrix(rest["translation"], rest["rotation"], rest["scale"])
        return (
            f'<node id="joint_{index:03d}" sid="{escape(name)}" name="{escape(name)}" type="JOINT">'
            f'<matrix sid="transform">{dae_floats(matrix)}</matrix>'
            f'{"".join(joint_xml(child) for child in children[index])}</node>'
        )

    animations = []
    for joint_index, track in sorted(track_by_joint.items()):
        matrices = [
            trs_matrix(translation, rotation, scale)
            for translation, rotation, scale in zip(
                track["values"]["translation"],
                track["values"]["rotation"],
                track["values"]["scale"],
            )
        ]
        animation_id = f"joint_{joint_index:03d}_animation"
        count = len(matrices)
        animations.append(
            f'<animation id="{animation_id}" name="{animation_id}">'
            f'<source id="{animation_id}_input"><float_array id="{animation_id}_input_array" count="{count}">'
            f'{dae_floats(motion["times"])}</float_array><technique_common>'
            f'<accessor source="#{animation_id}_input_array" count="{count}" stride="1">'
            f'<param name="TIME" type="float"/></accessor></technique_common></source>'
            f'<source id="{animation_id}_output"><float_array id="{animation_id}_output_array" count="{count * 16}">'
            f'{dae_floats(matrices)}</float_array><technique_common>'
            f'<accessor source="#{animation_id}_output_array" count="{count}" stride="16">'
            f'<param name="TRANSFORM" type="float4x4"/></accessor></technique_common></source>'
            f'<source id="{animation_id}_interpolation"><Name_array id="{animation_id}_interpolation_array" count="{count}">'
            f'{" ".join(["LINEAR"] * count)}</Name_array><technique_common>'
            f'<accessor source="#{animation_id}_interpolation_array" count="{count}" stride="1">'
            f'<param name="INTERPOLATION" type="name"/></accessor></technique_common></source>'
            f'<sampler id="{animation_id}_sampler">'
            f'<input semantic="INPUT" source="#{animation_id}_input"/>'
            f'<input semantic="OUTPUT" source="#{animation_id}_output"/>'
            f'<input semantic="INTERPOLATION" source="#{animation_id}_interpolation"/>'
            f'</sampler><channel source="#{animation_id}_sampler" target="joint_{joint_index:03d}/transform"/>'
            f'</animation>'
        )

    clip_name = escape(motion["clip"]["name"])
    start = motion["times"][0] if motion["times"] else 0.0
    end = motion["times"][-1] if motion["times"] else 0.0
    animation_instances = "".join(
        f'<instance_animation url="#{animation_id}"/>'
        for animation_id in (f"joint_{index:03d}_animation" for index in sorted(track_by_joint))
    )
    document = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">'
        '<asset><contributor><authoring_tool>LEVEL 5 ENGINE G4MT research tools</authoring_tool></contributor>'
        '<unit name="meter" meter="1"/><up_axis>Y_UP</up_axis></asset>'
        f'<library_animations>{"".join(animations)}</library_animations>'
        f'<library_animation_clips><animation_clip id="clip_000" name="{clip_name}" start="{start:.9g}" end="{end:.9g}">'
        f'{animation_instances}</animation_clip></library_animation_clips>'
        f'<library_visual_scenes><visual_scene id="Scene" name="Scene">'
        f'<node id="skeleton_root" name="skeleton_root">{"".join(joint_xml(root) for root in roots)}</node>'
        f'</visual_scene></library_visual_scenes><scene><instance_visual_scene url="#Scene"/></scene></COLLADA>\n'
    )
    output.write_text(document, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("g4mt", type=Path)
    parser.add_argument("--skeleton", type=Path)
    parser.add_argument("--clip", default="0", help="Clip index or exact name")
    parser.add_argument("--list", action="store_true", help="List clips without exporting")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("json", "gltf", "dae"))
    args = parser.parse_args()

    if args.list:
        for clip in parse_g4mt(args.g4mt, args.skeleton)["clips"]:
            print(f"{clip['index']:4d}  {clip['start_frame']:5d}-{clip['end_frame']:<5d}  {clip['fps']:3d} fps  {clip['name']}")
        return
    output_format = args.format or (args.output.suffix.lstrip(".").lower() if args.output else "json")
    if output_format not in {"json", "gltf", "dae"}:
        raise ValueError(f"cannot infer output format from .{output_format}; use --format")
    output = args.output or args.g4mt.with_name(f"{args.g4mt.stem}_clip_{args.clip}.{output_format}")
    output.parent.mkdir(parents=True, exist_ok=True)
    motion = decode_motion(args.g4mt, args.clip, args.skeleton)
    if output_format == "json":
        write_json(motion, output)
    elif output_format == "gltf":
        write_gltf(motion, output)
    else:
        write_dae(motion, output)
    print(output)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, struct.error) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
