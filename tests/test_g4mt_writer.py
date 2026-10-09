import struct
import unittest

from formats.g4mt import crc32b, parse_g4mt
from formats.g4mt_writer import (
    G4MTClipSamples,
    G4MTLocalTransform,
    G4MTTargetSamples,
    simplify_samples,
    write_g4mt_clip,
    write_g4mt_family,
    write_g4mt_family_preserving_additive,
)


def make_template():
    data = bytearray(160)
    data[:4] = b"G4MT"
    struct.pack_into("<HHI", data, 4, 0x40, 0x68, 0x7D)
    struct.pack_into("<I", data, 0x0C, len(data) - 0x40)
    struct.pack_into("<H", data, 0x0A, 0x10)
    struct.pack_into("<HH", data, 0x20, 1, 1)
    struct.pack_into("<HHHHHHHH", data, 0x24, 12, 14, 4, 5, 6, 7, 19, 20)
    struct.pack_into("<H", data, 0x36, 0)
    struct.pack_into("<HHHHBBBBI", data, 0x40, 0, 1, 0, 1, 0, 60, 0, 0, 0)
    struct.pack_into("<fII", data, 0x50, 1.0, crc32b("idle"), crc32b("root"))
    struct.pack_into("<HHH", data, 0x5C, 0, 0, 2)
    data[0x62:0x67] = b"idle\0"
    struct.pack_into("<HHBBH", data, 0x70, 0, 0, 1, 0, 0)
    data[0x78:0x80] = bytes((10, 1, 1, 4, 1, 4, 0, 0))
    struct.pack_into("<III", data, 0x80, 0, 0, 2)
    struct.pack_into("<HHff", data, 0x8C, 0, 1, 0.0, 1.0)
    return bytes(data)


class G4MTWriterTests(unittest.TestCase):
    def test_simplifies_linear_channels_with_bounded_error(self):
        frames = list(range(101))
        samples = [(frame * 0.25,) for frame in frames]

        reduced_frames, reduced_samples = simplify_samples(frames, samples, 1e-5)

        self.assertEqual([0, 100], reduced_frames)
        self.assertEqual([samples[0], samples[-1]], reduced_samples)
        self.assertIn(50, simplify_samples(frames, [(frame * frame,) for frame in frames], 0.0)[0])

    def test_writes_and_reads_back_absolute_local_srt_channels(self):
        targets = (
            G4MTTargetSamples("root", None, (
                G4MTLocalTransform((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
                G4MTLocalTransform((1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0), (2.0, 3.0, 4.0)),
            )),
            G4MTTargetSamples("child", 0, (
                G4MTLocalTransform((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
                G4MTLocalTransform((4.0, 5.0, 6.0), (0.0, 0.0, 0.0, 1.0), (3.0, 2.0, 1.0)),
            )),
        )

        output = write_g4mt_clip(make_template(), 0, "walk", targets)

        bank = parse_g4mt(output, "writer test")
        self.assertEqual(("walk", 0, 1, 60),
                         (bank.clips[0].name, bank.clips[0].start_frame,
                          bank.clips[0].end_frame, bank.clips[0].fps))
        self.assertEqual((crc32b("root"), crc32b("child")),
                         tuple(target.name_hash for target in bank.targets))
        self.assertEqual((1, 2, 3, 9, 10, 11, 12) * 2,
                         tuple(channel.channel_type for channel in bank.channels))
        for channel_index, channel in enumerate(bank.channels):
            offset = bank.header.data_offset + channel.data_offset
            child = channel_index >= 7
            if channel.channel_type == 9:
                self.assertEqual((0.0, 0.0, 0.0, 1.0), struct.unpack_from("<4f", output, offset))
                continue
            values = struct.unpack_from("<2f", output, offset)
            expected = {
                1: (1.0, 2.0 if not child else 6.0),
                2: (1.0, 3.0 if not child else 6.0),
                3: (1.0, 4.0),
                10: (0.0, 4.0 if child else 1.0),
                11: (0.0, 5.0 if child else 2.0),
                12: (0.0, 6.0 if child else 3.0),
            }[channel.channel_type]
            self.assertEqual(expected, values)

    def test_writes_multiple_clips_for_one_target_rig(self):
        first_targets = (
            G4MTTargetSamples("root", None, (
                G4MTLocalTransform((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
                G4MTLocalTransform((1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            )),
        )
        second_targets = (
            G4MTTargetSamples("root", None, (
                G4MTLocalTransform((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
                G4MTLocalTransform((8.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            )),
        )

        output = write_g4mt_family(make_template(), (
            G4MTClipSamples(0, "walk", first_targets),
            G4MTClipSamples(0, "run", second_targets),
        ))

        bank = parse_g4mt(output, "family writer test")
        self.assertEqual(("walk", "run"), tuple(clip.name for clip in bank.clips))
        self.assertEqual((0, 1), tuple(clip.target_info_start for clip in bank.clips))
        self.assertEqual((1, 1), tuple(clip.target_info_count for clip in bank.clips))
        self.assertEqual(14, len(bank.channels))
        second_x = bank.channels[11]
        value_offset = bank.header.data_offset + second_x.data_offset
        self.assertEqual((0.0, 8.0), struct.unpack_from("<2f", output, value_offset))

    def test_allows_duplicate_clip_names(self):
        target = G4MTTargetSamples("root", None, (
            G4MTLocalTransform((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            G4MTLocalTransform((1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
        ))

        output = write_g4mt_family(make_template(), (
            G4MTClipSamples(0, "repeat", (target,)),
            G4MTClipSamples(0, "repeat", (target,)),
        ))

        bank = parse_g4mt(output, "duplicate clip names")
        self.assertEqual(("repeat", "repeat"), tuple(clip.name for clip in bank.clips))
        self.assertEqual(bank.clips[0].name_hash, bank.clips[1].name_hash)

    def test_preserves_additive_clips_while_replacing_other_clips(self):
        first_targets = (
            G4MTTargetSamples("root", None, (
                G4MTLocalTransform((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
                G4MTLocalTransform((1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            )),
        )
        second_targets = (
            G4MTTargetSamples("root", None, (
                G4MTLocalTransform((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
                G4MTLocalTransform((8.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)),
            )),
        )
        template = bytearray(write_g4mt_family(make_template(), (
            G4MTClipSamples(0, "repeat", first_targets),
            G4MTClipSamples(0, "repeat", second_targets),
        )))
        template[0x40 + 0x10 + 8] |= 1
        source = parse_g4mt(template, "additive passthrough test source")

        output = write_g4mt_family_preserving_additive(bytes(template), (
            G4MTClipSamples(0, "repeat", first_targets),
        ))

        result = parse_g4mt(output, "additive passthrough test output")
        self.assertEqual(("repeat", "repeat"), tuple(clip.name for clip in result.clips))
        self.assertEqual(source.clips[1].flags, result.clips[1].flags)
        source_info = source.target_infos[source.clips[1].target_info_start]
        result_info = result.target_infos[result.clips[1].target_info_start]
        self.assertEqual(source.targets[source_info.target_index].name_hash,
                         result.targets[result_info.target_index].name_hash)
        source_channel = source.channels[source_info.channel_start + 5]
        result_channel = result.channels[result_info.channel_start + 5]
        self.assertEqual(source_channel.keys, result_channel.keys)
        self.assertEqual(source_channel.key_count, result_channel.key_count)


if __name__ == "__main__":
    unittest.main()
