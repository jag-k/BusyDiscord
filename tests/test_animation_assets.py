"""Validate packaged animations against the firmware 1.3.1 binary layout."""

import struct
import unittest

from importlib.resources import files

from busydiscord.bar_display import GLYPH_FILES, STRIKE_FILES


class AnimationAssetTests(unittest.TestCase):
    def test_packaged_animations_have_valid_sections_and_raw_frames(self):
        for section, filename in {**GLYPH_FILES, **STRIKE_FILES}.items():
            with self.subTest(filename=filename):
                data = files("busydiscord").joinpath("assets", filename).read_bytes()
                header_format = "<8sBBBBHHBBIIII"
                header_size = struct.calcsize(header_format)
                (
                    signature,
                    flags,
                    width,
                    height,
                    color,
                    max_mask,
                    max_pixels,
                    fps,
                    reserved,
                    section_bytes,
                    frame_bytes,
                    section_count,
                    frame_count,
                ) = struct.unpack_from(header_format, data)
                self.assertEqual(signature, b"bicycle1")
                self.assertEqual((flags, width, height, color, max_mask, fps, reserved), (0, 9, 9, 2, 0, 48, 0))
                self.assertEqual(max_pixels, width * height * 4)
                self.assertEqual(len(data), header_size + section_bytes + frame_bytes)
                self.assertGreater(frame_count, 0)

                frame_offsets = []
                offset = header_size + section_bytes
                for _ in range(frame_count):
                    frame_offsets.append(offset)
                    encoding, mask_length, pixel_length = struct.unpack_from("<BHH", data, offset)
                    self.assertEqual((encoding, mask_length, pixel_length), (0x10, 0, max_pixels))
                    offset += 5 + pixel_length
                self.assertEqual(offset, len(data))

                offset = header_size
                names = []
                for _ in range(section_count):
                    start, end, frame_offset = struct.unpack_from("<III", data, offset)
                    name_end = data.index(b"\0", offset + 12)
                    names.append(data[offset + 12 : name_end].decode("ascii"))
                    self.assertLessEqual(start, end)
                    self.assertLess(end, frame_count)
                    self.assertEqual(frame_offset, frame_offsets[start])
                    if names[-1] == "default":
                        self.assertEqual((start, end), (0, frame_count - 1))
                    offset = name_end + 1
                self.assertEqual(offset, header_size + section_bytes)
                self.assertEqual(names, ["default", section])
