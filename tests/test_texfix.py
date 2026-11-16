"""texfix: DDS header parsing, mip expectations, pow2, actions, real fixes."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from _harness import CMD, TEXCONV, BaseTest, crafted_dds
from pipeline import dds, texfix


class DdsHeaderTest(unittest.TestCase):
    def test_offsets(self):
        p = Path(tempfile.mkdtemp()) / "a.dds"
        p.write_bytes(crafted_dds(20, 10, b"DXT1", 6))
        hdr = dds.read_header(p)
        self.assertEqual(hdr, {"w": 20, "h": 10, "mips": 6, "fourcc": "DXT1"})

    def test_garbage_returns_none(self):
        p = Path(tempfile.mkdtemp()) / "a.dds"
        p.write_bytes(b"tiny")
        self.assertIsNone(dds.read_header(p))


class NeededActionTest(BaseTest):
    def test_png_without_twin_converts(self):
        p = self.ws.make_png("gfx/models/x.png")
        self.assertEqual(texfix.needed_action(p), {"action": "convert"})

    def test_png_with_twin_skips(self):
        self.ws.make_png("gfx/models/x.png")
        twin = self.ws.modsrc / "gfx" / "models" / "x.dds"
        twin.write_bytes(crafted_dds(8, 8, b"DXT5", 4))
        rec = texfix.needed_action(self.ws.modsrc / "gfx/models/x.png")
        self.assertEqual(rec["action"], "skip")

    def test_compliant_dds_skips(self):
        p = self.ws.modsrc / "gfx" / "models" / "ok.dds"
        p.parent.mkdir(parents=True)
        p.write_bytes(crafted_dds(16, 16, b"DXT5", 5))   # expected_mips(16,16)=5
        self.assertEqual(texfix.needed_action(p),
                         {"action": "skip", "note": "compliant"})

    def test_wrong_fourcc_fixes(self):
        p = self.ws.modsrc / "gfx" / "models" / "dxt3.dds"
        p.parent.mkdir(parents=True)
        p.write_bytes(crafted_dds(16, 16, b"DXT3", 5))
        rec = texfix.needed_action(p)
        self.assertEqual(rec["action"], "fix")
        self.assertIn("fourcc", rec["why"])

    def test_wrong_mips_fixes(self):
        p = self.ws.modsrc / "gfx" / "models" / "few.dds"
        p.parent.mkdir(parents=True)
        p.write_bytes(crafted_dds(16, 16, b"DXT1", 1))
        rec = texfix.needed_action(p)
        self.assertEqual(rec["action"], "fix")
        self.assertIn("mips", rec["why"])

    def test_raw_dds_fixes(self):
        self.ws.make_raw_dds("gfx/models/raw.dds")
        rec = texfix.needed_action(self.ws.modsrc / "gfx/models/raw.dds")
        self.assertEqual(rec["action"], "fix")


class ToPow2Test(unittest.TestCase):
    def test_floor_power_of_two(self):
        img = _img((40, 20))
        out, changed = texfix._to_pow2(img)
        self.assertEqual(out.size, (32, 16))
        self.assertTrue(changed)

    def test_pow2_untouched(self):
        img = _img((64, 32))
        out, changed = texfix._to_pow2(img)
        self.assertIs(out, img)
        self.assertFalse(changed)

    def test_one_pixel(self):
        out, changed = texfix._to_pow2(_img((1, 1)))
        self.assertEqual(out.size, (1, 1))
        self.assertFalse(changed)


def _img(size):
    return Image.new("RGBA", size, (1, 2, 3, 255))


@unittest.skipUnless(TEXCONV, "bundled texconv.exe required (Windows)")
class FixOneTest(BaseTest):
    def test_raw_dds_gets_format_and_full_mips(self):
        self.ws.make_raw_dds("gfx/models/raw.dds", w=8, h=8)
        rec = texfix._fix_one(self.ws.modsrc / "gfx/models/raw.dds", None, CMD)
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["action"], "fix")
        self.assertIn(rec["fmt"], ("DXT1", "DXT5"))
        self.assertEqual(rec["mips"], 4)          # expected_mips(8,8)
        hdr = dds.read_header(self.ws.modsrc / "gfx/models/raw.dds")
        self.assertEqual(hdr["mips"], 4)

    def test_png_converted_with_mips_and_nonpow2_resized(self):
        self.ws.make_png("gfx/models/dec.png", w=40, h=20)
        rec = texfix._fix_one(self.ws.modsrc / "gfx/models/dec.png", None, CMD)
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["action"], "convert")
        self.assertTrue(rec["non_pow2_resized"])
        hdr = dds.read_header(self.ws.modsrc / "gfx/models/dec.dds")
        self.assertEqual((hdr["w"], hdr["h"]), (32, 16))
        self.assertEqual(hdr["mips"], texfix.expected_mips(32, 16))

    def test_cap_resize(self):
        self.ws.make_png("gfx/models/big.png", w=64, h=64)
        rec = texfix._fix_one(self.ws.modsrc / "gfx/models/big.png", 32, CMD)
        self.assertTrue(rec["ok"], rec)
        self.assertTrue(rec["resized_cap"])
        hdr = dds.read_header(self.ws.modsrc / "gfx/models/big.dds")
        self.assertEqual((hdr["w"], hdr["h"]), (32, 32))


class CollectTargetsTest(BaseTest):
    def test_from_dirs_and_mesh_stats(self):
        self.ws.make_png("gfx/models/a.png")
        self.ws.make_png("gfx/elsewhere/b.png")
        self.ws.build.mkdir(parents=True)
        (self.ws.build / "mesh_stats.json").write_text(json.dumps([
            {"path": "gfx/m.mesh", "ok": True, "detail": [
                {"diff": "elsewhere/b.png", "n": "", "spec": ""}]}]),
            encoding="utf-8")
        targets = texfix.collect_targets(self.ws.modsrc, self.ws.build)
        names = {t.name for t in targets}
        self.assertEqual(names, {"a.png", "b.png"})


class RunTest(BaseTest):
    def _model_tree(self):
        self.ws.build.mkdir(parents=True, exist_ok=True)  # BUG-10: texfix won't
        self.ws.make_png("gfx/models/tank_diff.png", w=8, h=8)
        self.ws.make_raw_dds("gfx/models/tank_nm.dds", w=8, h=8)

    def test_no_targets_returns_2(self):
        self.assertEqual(texfix.run(self.ws.modsrc, self.ws.build, apply=False), 2)

    def test_dry_run_writes_nothing(self):
        self._model_tree()
        rc = texfix.run(self.ws.modsrc, self.ws.build, apply=False)
        self.assertEqual(rc, 0)
        self.assertFalse((self.ws.modsrc / "gfx/models/tank_diff.dds").exists())
    @unittest.skipUnless(TEXCONV, "texconv required")
    def test_apply_creates_missing_out_dir(self):
        self.ws.make_png("gfx/models/x.png")
        rc = texfix.run(self.ws.modsrc, self.ws.build, apply=True)
        self.assertEqual(rc, 0)
        self.assertTrue((self.ws.build / "texfix_manifest.json").is_file(),
                        "BUG-10: texfix crashes when build/ does not exist")

    @unittest.skipUnless(TEXCONV, "texconv required")
    def test_apply_and_idempotent(self):
        self._model_tree()
        rc = texfix.run(self.ws.modsrc, self.ws.build, apply=True)
        self.assertEqual(rc, 0)
        for name in ("tank_diff.dds", "tank_nm.dds"):
            hdr = dds.read_header(self.ws.modsrc / f"gfx/models/{name}")
            self.assertEqual(hdr["mips"], 4, name)
            self.assertIn(hdr["fourcc"], ("DXT1", "DXT5"), name)
        # second run: everything compliant -> skipped, rc 0
        rc2 = texfix.run(self.ws.modsrc, self.ws.build, apply=True)
        self.assertEqual(rc2, 0)
        mf = json.loads((self.ws.build / "texfix_manifest.json").read_text("utf-8"))
        self.assertTrue(all(m.get("skipped") for m in mf["ok"]))
        self.assertEqual(mf["failed"], [])


@unittest.skipUnless(TEXCONV, "texconv required")
class DirectPathTest(BaseTest):
    """The decode-free path must produce the same bytes as the old one."""

    def _pair(self, color):
        direct = self.ws.make_raw_dds("gfx/models/direct.dds", w=8, h=8, color=color)
        old = self.ws.modsrc / "gfx/models/old.dds"
        old.write_bytes(direct.read_bytes())
        return direct, old

    def test_opaque_matches_pillow_roundtrip(self):
        direct, old = self._pair((1, 2, 3, 255))
        rec = texfix._fix_one(direct, None, CMD)
        self.assertTrue(rec["ok"], rec)
        self.assertFalse(rec["resized_cap"] or rec["non_pow2_resized"])
        with mock.patch.object(texfix.dds, "alpha_opaque", lambda _: None):
            rec_old = texfix._fix_one(old, None, CMD)
        self.assertTrue(rec_old["ok"], rec_old)
        self.assertEqual(rec["fmt"], rec_old["fmt"])
        self.assertEqual(direct.read_bytes(), old.read_bytes())

    def test_gradient_matches_pillow_roundtrip(self):
        direct, old = self._pair((1, 2, 3, 128))
        rec = texfix._fix_one(direct, None, CMD)
        self.assertTrue(rec["ok"], rec)
        with mock.patch.object(texfix.dds, "alpha_opaque", lambda _: None):
            rec_old = texfix._fix_one(old, None, CMD)
        self.assertEqual(rec["fmt"], "DXT5")
        self.assertEqual(rec["fmt"], rec_old["fmt"])
        self.assertEqual(direct.read_bytes(), old.read_bytes())


if __name__ == "__main__":
    unittest.main()
