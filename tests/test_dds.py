"""dds: header parsing and the exact, decode-free alpha predicate."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from _harness import BaseTest, crafted_dds
from pipeline import convert, dds, images


class AlphaOpaqueTest(unittest.TestCase):
    """`dds.alpha_opaque` must equal `alpha_kind(load_rgba()) == "opaque"`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dds_alpha_"))

    def _raw(self, name: str, size=(64, 64), color=(1, 2, 3, 255),
             mode="RGBA") -> Path:
        p = self.tmp / name
        Image.new(mode, size, color).save(p, format="DDS")
        return p

    def _assert_equivalent(self, p: Path):
        exact = dds.alpha_opaque(p)
        self.assertIsNotNone(exact, p.name)
        pillow = images.alpha_kind(images.load_rgba(p)) == "opaque"
        self.assertEqual(exact, pillow, p.name)

    def test_all_alpha_255_is_opaque(self):
        p = self._raw("a.dds")
        self.assertTrue(dds.alpha_opaque(p))
        self._assert_equivalent(p)

    def test_gradient_alpha_is_not_opaque(self):
        p = self._raw("g.dds", color=(1, 2, 3, 128))
        self.assertFalse(dds.alpha_opaque(p))
        self._assert_equivalent(p)

    def test_single_non_255_sample_is_detected(self):
        p = self._raw("one.dds")
        with Image.open(p) as img:
            img.load()
        img.putpixel((13, 7), (1, 2, 3, 254))
        img.save(p, format="DDS")
        self.assertFalse(dds.alpha_opaque(p))
        self._assert_equivalent(p)

    def test_rgb_without_alpha_channel_is_opaque(self):
        p = self._raw("rgb.dds", color=(1, 2, 3), mode="RGB")
        self.assertTrue(dds.alpha_opaque(p))
        self._assert_equivalent(p)

    def test_compressed_dds_defers_to_pillow(self):
        p = self.tmp / "c.dds"
        p.write_bytes(crafted_dds(8, 8, b"DXT5", 1))
        self.assertIsNone(dds.alpha_opaque(p))

    def test_truncated_payload_defers(self):
        p = self.tmp / "t.dds"
        p.write_bytes(crafted_dds(64, 64, b"", 1)[:200])
        self.assertIsNone(dds.alpha_opaque(p))

    def test_padded_pitch_defers(self):
        p = self._raw("p.dds")
        data = bytearray(p.read_bytes())
        data[20:24] = (64 * 4 + 64).to_bytes(4, "little")   # pitch != w*4
        p.write_bytes(bytes(data))
        self.assertIsNone(dds.alpha_opaque(p))

    def test_non_byte_alpha_mask_defers(self):
        p = self._raw("m.dds")
        data = bytearray(p.read_bytes())
        data[88:92] = (16).to_bytes(4, "little")            # 16-bit pixels
        data[104:108] = (0x8000).to_bytes(4, "little")      # 1-bit alpha
        p.write_bytes(bytes(data))
        self.assertIsNone(dds.alpha_opaque(p))


class PngDirectTest(BaseTest):
    def test_rgb_and_rgba_8bit_are_direct(self):
        for mode, color in (("RGB", (1, 2, 3)), ("RGBA", (1, 2, 3, 255))):
            p = self.ws.make_png(f"{mode}.png", color=color, mode=mode)
            self.assertTrue(convert._png_direct(p), mode)

    def test_palette_png_is_not_direct(self):
        p = self.ws.modsrc / "pal.png"
        Image.new("RGB", (8, 8), (1, 2, 3)).convert("P").save(p)
        self.assertFalse(convert._png_direct(p))

    def test_non_png_is_not_direct(self):
        p = self.ws.modsrc / "x.tga"
        Image.new("RGBA", (8, 8)).save(p)
        self.assertFalse(convert._png_direct(p))


if __name__ == "__main__":
    unittest.main()
