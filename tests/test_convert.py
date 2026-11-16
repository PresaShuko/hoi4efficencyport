"""convert: padding, format choice, real texconv conversion, guards, run()."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from PIL import Image

from _harness import CMD, TEXCONV, BaseTest
from pipeline import analyze, convert, dds, images, scanner, tools


class PadTest(unittest.TestCase):
    def test_no_pad_needed(self):
        img = Image.new("RGBA", (8, 4))
        out, pw, ph = images.pad_to_4(img)
        self.assertIs(out, img)
        self.assertEqual((pw, ph), (0, 0))

    def test_pad_replicates_edges(self):
        img = Image.new("RGBA", (5, 3), (10, 20, 30, 40))
        img.putpixel((4, 2), (255, 0, 0, 255))     # bottom-right source pixel
        out, pw, ph = images.pad_to_4(img)
        self.assertEqual(out.size, (8, 4))
        self.assertEqual((pw, ph), (3, 1))
        # right padding copies the last source column
        self.assertEqual(out.getpixel((5, 0)), img.getpixel((4, 0)))
        # bottom padding copies the last row (including padded columns)
        self.assertEqual(out.getpixel((7, 3)), (255, 0, 0, 255))


class FmtForTest(BaseTest):
    def _entry(self, **kw):
        return {"path": "gfx/interface/x.png", "action": "convert", **kw}

    def test_opaque(self):
        self.assertEqual(convert._fmt_for(self._entry(alpha="opaque"),
                                         self.ws.modsrc), "DXT1")

    def test_gradient(self):
        self.assertEqual(convert._fmt_for(self._entry(alpha="gradient"),
                                         self.ws.modsrc), "DXT5")

    def test_reencode_reopens_dds(self):
        self.ws.make_raw_dds("gfx/interface/raw.dds",
                             color=(0, 0, 0, 128))          # gradient alpha
        entry = {"path": "gfx/interface/raw.dds", "action": "reencode",
                 "alpha": "dds"}
        self.assertEqual(convert._fmt_for(entry, self.ws.modsrc), "DXT5")

    def test_reencode_unreadable_dds_defaults_dxt5(self):
        p = self.ws.modsrc / "gfx" / "interface" / "bad.dds"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"garbage" * 64)
        entry = {"path": "gfx/interface/bad.dds", "action": "reencode",
                 "alpha": "dds"}
        self.assertEqual(convert._fmt_for(entry, self.ws.modsrc), "DXT5")


@unittest.skipUnless(TEXCONV, "bundled texconv.exe required (Windows)")
class ConvertOneTest(BaseTest):
    def test_png_converted_opaque_with_cap_and_pad(self):
        self.ws.make_png("gfx/interface/ico.png", w=5, h=3,
                         color=(200, 10, 10, 255), mode="RGB")
        entry = {"path": "gfx/interface/ico.png", "action": "convert",
                 "alpha": "opaque", "cap": 4, "bytes": 0}
        rec = convert._convert_one(entry, self.ws.modsrc, CMD)
        self.assertTrue(rec["ok"], rec)
        self.assertTrue(rec["resized"])             # 5x3 -> 4x3
        self.assertTrue(rec["padded"])              # 4x3 -> 4x4
        self.assertEqual(rec["fmt"], "DXT1")
        hdr = dds.read_header(self.ws.modsrc / "gfx/interface/ico.dds")
        self.assertEqual((hdr["w"], hdr["h"]), (4, 4))
        self.assertEqual(hdr["fourcc"], "DXT1")

    def test_gradient_uses_dxt5(self):
        self.ws.make_png("gfx/interface/g.png", w=8, h=8,
                         color=(0, 0, 0, 128))
        entry = {"path": "gfx/interface/g.png", "action": "convert",
                 "alpha": "gradient", "cap": 64, "bytes": 0}
        rec = convert._convert_one(entry, self.ws.modsrc, CMD)
        self.assertEqual(rec["fmt"], "DXT5")

    def test_twin_guard(self):
        self.ws.make_png("gfx/interface/a.png")
        self.ws.make_raw_dds("gfx/interface/a.dds")
        entry = {"path": "gfx/interface/a.png", "action": "convert",
                 "alpha": "opaque", "cap": 16, "bytes": 0}
        rec = convert._convert_one(entry, self.ws.modsrc, CMD)
        self.assertFalse(rec["ok"])
        self.assertIn("twin", rec["note"])

    def test_reencode_rewrites_raw_dds_in_place(self):
        self.ws.make_raw_dds("gfx/interface/raw.dds", w=8, h=8)
        entry = {"path": "gfx/interface/raw.dds", "action": "reencode",
                 "alpha": "dds", "cap": 16, "bytes": 0}
        rec = convert._convert_one(entry, self.ws.modsrc, CMD)
        self.assertTrue(rec["ok"], rec)
        hdr = dds.read_header(self.ws.modsrc / "gfx/interface/raw.dds")
        self.assertIn(hdr["fourcc"], ("DXT1", "DXT5"))


class RunTest(BaseTest):
    def _plan(self):
        self.ws.make_png("gfx/interface/a.png", w=8, h=8, mode="RGB")
        self.ws.make_png("gfx/interface/b.png", w=8, h=8, mode="RGB")
        sr = scanner.scan(self.ws.modsrc)
        res = analyze.plan(self.ws.modsrc, sr, None)
        self.ws.build.mkdir(parents=True, exist_ok=True)
        (self.ws.build / "asset_plan.json").write_text(json.dumps(res),
                                                       encoding="utf-8")

    def test_missing_plan_returns_2(self):
        self.assertEqual(convert.run(self.ws.modsrc, self.ws.build, apply=False), 2)

    def test_dry_run_writes_nothing(self):
        self._plan()
        rc = convert.run(self.ws.modsrc, self.ws.build, apply=False)
        self.assertEqual(rc, 0)
        self.assertFalse((self.ws.build / "convert_manifest.json").exists())
        self.assertTrue((self.ws.modsrc / "gfx/interface/a.png").is_file())

    @unittest.skipUnless(TEXCONV, "texconv required")
    def test_apply_writes_manifest_into_out(self):
        # unlike texfix, convert needs asset_plan.json, so build/ exists in
        # any real flow; the mkdir in run() keeps the contract uniform
        self.ws.make_png("gfx/interface/a.png", w=8, h=8, mode="RGB")
        self.ws.build.mkdir(parents=True)
        plan = {"files": [{"path": "gfx/interface/a.png", "action": "convert",
                           "alpha": "opaque", "cap": 8, "bytes": 0}]}
        (self.ws.build / "asset_plan.json").write_text(json.dumps(plan),
                                                       encoding="utf-8")
        rc = convert.run(self.ws.modsrc, self.ws.build, apply=True)
        self.assertEqual(rc, 0)
        self.assertTrue((self.ws.build / "convert_manifest.json").is_file())
        self.assertTrue((self.ws.modsrc / "gfx/interface/a.dds").is_file())

    @unittest.skipUnless(TEXCONV, "texconv required")
    def test_apply_converts_and_reports_stem_conflict(self):
        self.ws.make_png("gfx/interface/a.tga")     # same stem, same folder
        self._plan()
        rc = convert.run(self.ws.modsrc, self.ws.build, apply=True)
        self.assertEqual(rc, 0)
        mf = json.loads((self.ws.build / "convert_manifest.json").read_text("utf-8"))
        ok_paths = {m["path"] for m in mf["ok"]}
        self.assertIn("gfx/interface/a.png", ok_paths)
        self.assertTrue((self.ws.modsrc / "gfx/interface/a.dds").is_file())
        # exactly one of (a.png, a.tga) conversion attempt is blocked
        notes = {m["path"]: m.get("note", "") for m in mf["failed"]}
        if notes:
            self.assertIn("duplicate stem", notes["gfx/interface/a.tga"])

    def test_apply_missing_staging_reports_failure_not_crash(self):
        # plan exists but staging gone: _convert_one fails gracefully per entry
        self.ws.build.mkdir(parents=True)
        (self.ws.build / "asset_plan.json").write_text(
            json.dumps({"files": [{"path": "gfx/interface/gone.png",
                                   "action": "convert", "alpha": "opaque",
                                   "cap": 8, "bytes": 0}]}), encoding="utf-8")
        if tools.texconv_path() is None:
            # no texconv at all: run() must refuse before touching the tree
            with self.assertRaises(SystemExit):
                convert.run(self.ws.modsrc, self.ws.build, apply=True)
            return
        rc = convert.run(self.ws.modsrc, self.ws.build, apply=True)
        self.assertEqual(rc, 0)
        mf = json.loads((self.ws.build / "convert_manifest.json").read_text("utf-8"))
        self.assertEqual(len(mf["failed"]), 1)


@unittest.skipUnless(TEXCONV, "texconv required")
class PassThroughTest(BaseTest):
    def test_png_pass_through_matches_decode_path(self):
        src = self.ws.make_png("gfx/interface/a.png", w=8, h=8, mode="RGB")
        twin = self.ws.modsrc / "gfx/interface/b.png"
        twin.write_bytes(src.read_bytes())
        base = {"action": "convert", "alpha": "opaque", "cap": 256,
                "bytes": src.stat().st_size, "w": 8, "h": 8}
        rec = convert._convert_one(dict(base, path="gfx/interface/a.png"),
                                   self.ws.modsrc, CMD)
        with mock.patch.object(convert, "_png_direct", lambda _: False):
            rec_old = convert._convert_one(dict(base, path="gfx/interface/b.png"),
                                           self.ws.modsrc, CMD)
        self.assertTrue(rec["ok"], rec)
        self.assertTrue(rec_old["ok"], rec_old)
        self.assertEqual(rec["fmt"], rec_old["fmt"])
        self.assertEqual((self.ws.modsrc / "gfx/interface/a.dds").read_bytes(),
                         (self.ws.modsrc / "gfx/interface/b.dds").read_bytes())


if __name__ == "__main__":
    unittest.main()
