"""analyze: classification, gui size heuristics, alpha kinds, caps, dedup, unused."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from _harness import BaseTest, crafted_dds, make_scan
from pipeline import analyze, dds, images


class ClassifyTest(unittest.TestCase):
    def test_all_classes(self):
        cases = {
            "gfx/interface/goals/x.png": "goals",
            "gfx/interface/ideas/x.png": "ideas",
            "gfx/interface/decisions/x.png": "decisions",
            "gfx/event_pictures/x.png": "events",
            "gfx/leaders/GER/x.png": "portraits",
            "gfx/loading_screens/x.png": "loading",
            "gfx/interface/x.png": "ui",
            "gfx/banner/x.png": "ui",
            "gfx/models/units/x.png": "skip",
            "gfx/entities/x.png": "skip",
            "gfx/flags/GER.png": "skip",
        }
        for rel, want in cases.items():
            self.assertEqual(analyze.classify(rel), want, rel)


class AlphaKindTest(unittest.TestCase):
    def test_opaque_rgb(self):
        self.assertEqual(images.alpha_kind(Image.new("RGB", (4, 4))), "opaque")

    def test_opaque_full_alpha(self):
        img = Image.new("RGBA", (4, 4), (0, 0, 0, 255))
        self.assertEqual(images.alpha_kind(img), "opaque")

    def test_binary(self):
        img = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        img.putpixel((0, 0), (255, 255, 255, 255))
        self.assertEqual(images.alpha_kind(img), "binary")

    def test_gradient(self):
        img = Image.new("RGBA", (4, 4), (0, 0, 0, 128))
        self.assertEqual(images.alpha_kind(img), "gradient")

    def test_la_mode_gradient(self):
        img = Image.new("LA", (4, 4), (10, 64))
        self.assertEqual(images.alpha_kind(img), "gradient")


class DdsFourccTest(unittest.TestCase):
    def test_crafted(self):
        p = Path(tempfile.mkdtemp()) / "x.dds"
        p.write_bytes(crafted_dds(8, 8, b"DXT5", 1))
        self.assertEqual(dds.fourcc_file(p), "DXT5")

    def test_not_dds(self):
        p = Path(tempfile.mkdtemp()) / "x.png"
        p.write_bytes(b"not a dds at all" * 10)
        self.assertEqual(dds.fourcc_file(p), "?")

    def test_pillow_raw_dds_fourcc_zero(self):
        p = Path(tempfile.mkdtemp()) / "x.dds"
        Image.new("RGBA", (8, 8)).save(p, format="DDS")
        self.assertEqual(dds.fourcc_file(p), "\x00\x00\x00\x00")


class GuiSpriteSizesTest(BaseTest):
    def test_size_after_sprite(self):
        self.ws.write("interface/w.gui",
                      'windowType = {\n name = "x"\n'
                      ' sprite = "GFX_a"\n'
                      ' size = { x=20 y=10 }\n}\n')
        sizes = analyze.gui_sprite_sizes(self.ws.modsrc)
        self.assertEqual(sizes, {"GFX_a": (20, 10)})

    def test_size_before_sprite_and_lookahead(self):
        self.ws.write("interface/w.gui",
                      'windowType = {\n name = "x"\n'
                      ' size = { x = 30 y = 5 }\n'
                      ' sprite = "GFX_b"\n}\n')
        self.assertEqual(analyze.gui_sprite_sizes(self.ws.modsrc),
                         {"GFX_b": (30, 5)})

    def test_largest_use_wins(self):
        self.ws.write("interface/w.gui",
                      'size = { x=10 y=10 }\nsprite = "GFX_c"\n'
                      'size = { x=40 y=8 }\nsprite = "GFX_c"\n')
        self.assertEqual(analyze.gui_sprite_sizes(self.ws.modsrc),
                         {"GFX_c": (40, 8)})

    def test_bordersize_not_matched(self):
        self.ws.write("interface/w.gui",
                      'borderSize = { x=99 y=99 }\nsprite = "GFX_d"\n')
        self.assertEqual(analyze.gui_sprite_sizes(self.ws.modsrc), {})


class PlanTest(BaseTest):
    def _gfx(self):
        self.ws.write("interface/a.gfx",
                      'spriteType = {\n name = "GFX_goal"\n'
                      ' texturefile = "gfx/interface/goals/goal.png"\n}\n'
                      'spriteType = {\n name = "GFX_ui"\n'
                      ' texturefile = "gfx/interface/ui.png"\n}\n')
        self.ws.write("interface/w.gui",
                      'sprite = "GFX_ui"\nsize = { x=16 y=16 }\n')
        self.ws.make_png("gfx/interface/goals/goal.png", w=512, h=256)
        self.ws.make_png("gfx/interface/ui.png", w=40, h=20)
        self.ws.make_png("gfx/models/units/tank.png", w=64, h=64)
        self.ws.make_png("gfx/event_pictures/big.png", w=2000, h=100)
        # duplicate of goal.png
        self.ws.make_png("gfx/interface/goals/goal.png", w=512, h=256,
                         color=(0, 128, 255, 255))
        self.ws.make_png("gfx/interface/ideas/twin.png", w=512, h=256,
                         color=(0, 128, 255, 255))
        self.ws.write("common/ideas/i.txt", 'i = "gfx/interface/ideas/twin.png"\n')

    def test_actions_and_caps(self):
        self._gfx()
        res = analyze.plan(self.ws.modsrc, make_scan(self.ws), None)
        by = {f["path"]: f for f in res["files"]}
        goal = by["gfx/interface/goals/goal.png"]
        self.assertEqual(goal["class"], "goals")
        self.assertEqual(goal["cap"], 256)          # class cap, no gui size
        self.assertEqual(goal["action"], "convert")
        ui = by["gfx/interface/ui.png"]
        # gui size 16x16 -> cap = 2*16 = 32 (gui cap beats the class default)
        self.assertEqual(ui["cap"], 32)
        tank = by["gfx/models/units/tank.png"]
        self.assertEqual(tank["class"], "skip")
        self.assertEqual(tank["action"], "skip")
        ev = by["gfx/event_pictures/big.png"]
        self.assertEqual(ev["cap"], 1024)
        self.assertEqual(ev["w"], 2000)

    def test_dds_keep_and_reencode(self):
        ws = self.ws
        ws.make_raw_dds("gfx/interface/raw.dds")
        dxt = ws.modsrc / "gfx" / "interface" / "comp.dds"
        dxt.write_bytes(crafted_dds(8, 8, b"DXT1", 1))
        ws.write("interface/a.gfx",
                 'spriteType = {\n name = "GFX_r"\n texturefile = "gfx/interface/raw.dds"\n}\n')
        res = analyze.plan(ws.modsrc, make_scan(ws), None)
        by = {Path(f["path"]).name: f for f in res["files"]}
        self.assertEqual(by["raw.dds"]["action"], "reencode")
        self.assertEqual(by["comp.dds"]["action"], "keep")
        self.assertEqual(by["comp.dds"]["fourcc"], "DXT1")

    def test_gui_cap_overrides_class_cap(self):
        ws = self.ws
        ws.write("interface/a.gfx",
                 'spriteType = {\n name = "GFX_g"\n'
                 ' texturefile = "gfx/interface/goals/g.png"\n}\n')
        ws.write("interface/w.gui",
                 'sprite = "GFX_g"\nsize = { x=10 y=10 }\n')
        ws.make_png("gfx/interface/goals/g.png", w=64, h=64)
        res = analyze.plan(ws.modsrc, make_scan(ws), None)
        f = res["files"][0]
        self.assertEqual(f["cap"], 20)   # 2x gui size

    def test_duplicates_grouped(self):
        self._gfx()
        res = analyze.plan(self.ws.modsrc, make_scan(self.ws), None)
        groups = list(res["duplicates"].values())
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0]),
                         ["gfx/interface/goals/goal.png",
                          "gfx/interface/ideas/twin.png"])

    def test_unused(self):
        self._gfx()
        res = analyze.plan(self.ws.modsrc, make_scan(self.ws), None)
        self.assertIn("gfx/models/units/tank.png", res["unused"])

    def test_vanilla_identical(self):
        ws = self.ws
        v = ws.root / "vanilla"
        (v / "gfx" / "interface" / "goals").mkdir(parents=True)
        Image.new("RGBA", (8, 8), (1, 2, 3, 4)).save(v / "gfx" / "interface" / "goals" / "same.png")
        ws.make_png("gfx/interface/goals/same.png", color=(1, 2, 3, 4))
        ws.make_png("gfx/interface/goals/other.png", color=(9, 9, 9, 9))
        vhash = {"gfx/interface/goals/same.png":
                 hashlib.sha256(
                     (v / "gfx/interface/goals/same.png").read_bytes()).hexdigest()}
        res = analyze.plan(ws.modsrc, make_scan(ws), vhash)
        self.assertEqual(res["vanilla_identical"], ["gfx/interface/goals/same.png"])

    def test_report_written(self):
        self._gfx()
        res = analyze.plan(self.ws.modsrc, make_scan(self.ws), None)
        out = self.ws.root / "asset_report.txt"
        analyze.write_report(res, out)
        text = out.read_text(encoding="utf-8")
        self.assertIn("Files in gfx/:", text)
        self.assertIn("Above resolution cap", text)


if __name__ == "__main__":
    unittest.main()
