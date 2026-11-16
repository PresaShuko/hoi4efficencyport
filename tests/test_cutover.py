"""cutover: mapping, dedup, guards, run() apply, and BUG-01 regression.

BUG-01 (critical, expectedFailure): `conv_ok` contains the .dds produced by
'reencode' actions, so cutover deletes freshly reencoded (and still
referenced) .dds files.
"""
from __future__ import annotations

import json
import unittest

from _harness import BaseTest, crafted_dds, make_scan
from pipeline import analyze, cutover
from pipeline.cutover import _redirect


class RedirectTest(unittest.TestCase):
    def test_chain(self):
        m = {"a.png": "b.png", "b.png": "b.png", "c.png": "c.png"}
        _redirect(m, "b.png", "d.dds")
        self.assertEqual(m["a.png"], "d.dds")
        self.assertEqual(m["b.png"], "d.dds")
        self.assertEqual(m["c.png"], "c.png")

    def test_self_noop(self):
        m = {"a": "b"}
        _redirect(m, "b", "b")
        self.assertEqual(m, {"a": "b"})


def make_plan(ws):
    sr = make_scan(ws)
    return analyze.plan(ws.modsrc, sr, None), sr


class PlanCutoverTest(BaseTest):
    def test_extension_rewrite_when_twin_exists(self):
        ws = self.ws
        ws.write("interface/a.gfx",
                 'spriteType = {\n name = "GFX_a"\n'
                 ' texturefile = "gfx/interface/a.png"\n}\n')
        ws.make_png("gfx/interface/a.png")
        ws.make_raw_dds("gfx/interface/a.dds")
        plan, sr = make_plan(ws)
        changes, deletions, dedup, guard = cutover.plan_cutover(
            ws.modsrc, sr, plan, set())
        self.assertEqual([(c["src"], c["dst"]) for c in changes],
                         [("gfx/interface/a.png", "gfx/interface/a.dds")])
        self.assertEqual(deletions, [])

    def test_fonts_excluded_from_rewrite(self):
        ws = self.ws
        ws.write("interface/fonts.gfx",
                 'bitmapfont = {\n name = "X"\n fontfile = "gfx/fonts/f.tga"\n}\n')
        (ws.modsrc / "gfx" / "fonts").mkdir(parents=True)
        (ws.modsrc / "gfx" / "fonts" / "f.tga").write_bytes(b"x")
        (ws.modsrc / "gfx" / "fonts" / "f.dds").write_bytes(b"x")
        plan, sr = make_plan(ws)
        changes, *_ = cutover.plan_cutover(ws.modsrc, sr, plan, set())
        self.assertEqual(changes, [])

    def test_dedup_prefers_dds_then_refs_then_name(self):
        ws = self.ws
        ws.write("interface/a.gfx",
                 'spriteType = {\n name = "GFX_x"\n'
                 ' texturefile = "gfx/interface/x.png"\n}\n'
                 'spriteType = {\n name = "GFX_y"\n'
                 ' texturefile = "gfx/interface/y.png"\n}\n')
        ws.make_png("gfx/interface/x.png", w=8, h=8, color=(7, 7, 7, 7))
        ws.make_png("gfx/interface/y.png", w=8, h=8, color=(7, 7, 7, 7))
        plan, sr = make_plan(ws)
        self.assertEqual(len(plan["duplicates"]), 1)
        changes, deletions, dedup, guard = cutover.plan_cutover(
            ws.modsrc, sr, plan, set())
        # both are png; equal refs; lexicographic max wins -> y.png canonical
        self.assertEqual(deletions, ["gfx/interface/x.png"])
        self.assertEqual(dedup, 1)
        self.assertIn(("gfx/interface/x.png", "gfx/interface/y.png"),
                      [(c["src"], c["dst"]) for c in changes])

    def test_convention_dirs_not_deduped(self):
        ws = self.ws
        ws.make_png("gfx/flags/GER.png", color=(3, 3, 3, 3))
        ws.make_png("gfx/flags_copy/GER.png", color=(3, 3, 3, 3))
        # reference the copy so it enters the scan; flags dir itself is skipped
        ws.write("common/x.txt", 'f = "gfx/flags_copy/GER.png"\n')
        plan, sr = make_plan(ws)
        self.assertEqual(len(plan["duplicates"]), 1)
        changes, deletions, dedup, guard = cutover.plan_cutover(
            ws.modsrc, sr, plan, set())
        # the flags member is filtered out -> nothing to dedup, nothing deleted
        self.assertEqual(changes, [])
        self.assertEqual(deletions, [])
        self.assertEqual(dedup, 0)

    def test_vanilla_identical_deleted_only_opt_in(self):
        ws = self.ws
        ws.make_png("gfx/interface/goals/g.png", color=(5, 5, 5, 5))
        src = ws.modsrc / "gfx/interface/goals/g.png"
        vanilla_goals = ws.root / "vanilla" / "gfx" / "interface" / "goals"
        vanilla_goals.mkdir(parents=True)
        (vanilla_goals / "g.png").write_bytes(src.read_bytes())
        plan, sr = make_plan(ws)
        plan["vanilla_identical"] = ["gfx/interface/goals/g.png"]
        _, deletions_off, *_ = cutover.plan_cutover(ws.modsrc, sr, plan, set(),
                                                    do_vanilla_dedup=False)
        self.assertEqual(deletions_off, [])
        _, deletions_on, *_ = cutover.plan_cutover(ws.modsrc, sr, plan, set(),
                                                   do_vanilla_dedup=True)
        self.assertIn("gfx/interface/goals/g.png", deletions_on)

    def test_converted_source_without_twin_kept(self):
        ws = self.ws
        # conv_ok lists x.png but no .dds twin was produced (stale manifest):
        # the source must survive (BUG-07)
        ws.write("common/x.txt", 'i = "gfx/interface/x.dds"\n')
        ws.make_png("gfx/interface/x.png")
        plan, sr = make_plan(ws)
        changes, deletions, dedup, guard = cutover.plan_cutover(
            ws.modsrc, sr, plan, conv_ok={"gfx/interface/x.png"})
        self.assertEqual(deletions, [])
        self.assertTrue((ws.modsrc / "gfx/interface/x.png").is_file())

    def test_rewrite_destination_never_deleted(self):
        ws = self.ws
        ws.write("interface/a.gfx",
                 'spriteType = {\n name = "GFX_a"\n'
                 ' texturefile = "gfx/interface/a.png"\n}\n')
        ws.make_png("gfx/interface/a.png", color=(1, 2, 3, 4))
        ws.make_png("gfx/interface/a.dds", color=(1, 2, 3, 4))
        plan, sr = make_plan(ws)
        # a.dds is both a rewrite destination and a duplicate loser
        changes, deletions, dedup, guard = cutover.plan_cutover(
            ws.modsrc, sr, plan, set())
        self.assertNotIn("gfx/interface/a.dds", deletions)


class RunTest(BaseTest):
    def _stage(self):
        ws = self.ws
        ws.write("interface/a.gfx",
                 'spriteType = {\n name = "GFX_a"\n'
                 ' texturefile = "gfx/interface/a.png"\n}\n')
        ws.make_png("gfx/interface/a.png", w=8, h=8, mode="RGB")
        # the conversion actually produced the .dds twin
        (ws.modsrc / "gfx" / "interface" / "a.dds").write_bytes(
            crafted_dds(8, 8, b"DXT1", 1))
        sr = make_scan(ws)
        plan = analyze.plan(ws.modsrc, sr, None)
        ws.build.mkdir(parents=True, exist_ok=True)
        (ws.build / "asset_plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (ws.build / "convert_manifest.json").write_text(
            json.dumps({"ok": [{"path": "gfx/interface/a.png", "ok": True,
                                "bytes": 1, "orig_bytes": 2}],
                        "failed": []}), encoding="utf-8")

    def test_missing_manifests_returns_2(self):
        self.assertEqual(cutover.run(self.ws.modsrc, self.ws.build,
                                     apply=False, vanilla=None), 2)

    def test_dry_run_changes_nothing(self):
        self._stage()
        rc = cutover.run(self.ws.modsrc, self.ws.build, apply=False,
                         vanilla=None)
        self.assertEqual(rc, 0)
        self.assertTrue((self.ws.modsrc / "gfx/interface/a.png").is_file())
        mf = json.loads((self.ws.build / "cutover_manifest.json").read_text("utf-8"))
        self.assertTrue(mf["dry_run"])

    def test_apply_rewrites_and_deletes_converted_source(self):
        self._stage()
        rc = cutover.run(self.ws.modsrc, self.ws.build, apply=True,
                         vanilla=None)
        self.assertEqual(rc, 0)
        gfx = (self.ws.modsrc / "interface" / "a.gfx").read_text("utf-8")
        self.assertIn("gfx/interface/a.dds", gfx)
        self.assertFalse((self.ws.modsrc / "gfx/interface/a.png").exists())
        self.assertTrue((self.ws.modsrc / "gfx/interface/a.dds").is_file())
        mf = json.loads((self.ws.build / "cutover_manifest.json").read_text("utf-8"))
        self.assertEqual(mf["removed"], 1)
        self.assertEqual(mf["verify"]["deleted_still_referenced"], [])
    def test_source_not_deleted_when_twin_dds_missing(self):
        # stale/failed convert manifest: conv_ok lists a.png but no .dds exists
        ws = self.ws
        ws.write("interface/a.gfx",
                 'spriteType = {\n name = "GFX_a"\n'
                 ' texturefile = "gfx/interface/a.png"\n}\n')
        ws.make_png("gfx/interface/a.png", w=8, h=8, mode="RGB")
        sr = make_scan(ws)
        plan = analyze.plan(ws.modsrc, sr, None)
        ws.build.mkdir(parents=True)
        (ws.build / "asset_plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (ws.build / "convert_manifest.json").write_text(
            json.dumps({"ok": [{"path": "gfx/interface/a.png", "ok": True}],
                        "failed": []}), encoding="utf-8")
        rc = cutover.run(ws.modsrc, ws.build, apply=True, vanilla=None)
        self.assertEqual(rc, 0)
        self.assertTrue((ws.modsrc / "gfx/interface/a.png").is_file(),
                        "BUG-07: source deleted although the .dds twin is missing")
    def test_reencoded_dds_is_not_deleted(self):
        ws = self.ws
        ws.write("interface/r.gfx",
                 'spriteType = {\n name = "GFX_r"\n'
                 ' texturefile = "gfx/interface/raw.dds"\n}\n')
        ws.make_raw_dds("gfx/interface/raw.dds")
        sr = make_scan(ws)
        plan = analyze.plan(ws.modsrc, sr, None)
        ws.build.mkdir(parents=True)
        (ws.build / "asset_plan.json").write_text(json.dumps(plan), encoding="utf-8")
        # a real reencode: conv_ok lists the .dds itself
        (ws.build / "convert_manifest.json").write_text(
            json.dumps({"ok": [{"path": "gfx/interface/raw.dds", "ok": True}],
                        "failed": []}), encoding="utf-8")
        rc = cutover.run(ws.modsrc, ws.build, apply=True, vanilla=None)
        self.assertEqual(rc, 0)
        self.assertTrue((ws.modsrc / "gfx/interface/raw.dds").is_file(),
                        "BUG-01: cutover deleted the reencoded .dds it just made")
        mf = json.loads((ws.build / "cutover_manifest.json").read_text("utf-8"))
        self.assertEqual(mf["verify"]["deleted_still_referenced"], [])

    def test_no_dedup_flag(self):
        ws = self.ws
        ws.write("interface/a.gfx",
                 'spriteType = {\n name = "GFX_x"\n texturefile = "gfx/x.png"\n}\n'
                 'spriteType = {\n name = "GFX_y"\n texturefile = "gfx/y.png"\n}\n')
        ws.make_png("gfx/x.png", color=(7, 7, 7, 7))
        ws.make_png("gfx/y.png", color=(7, 7, 7, 7))
        sr = make_scan(ws)
        plan = analyze.plan(ws.modsrc, sr, None)
        ws.build.mkdir(parents=True)
        (ws.build / "asset_plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (ws.build / "convert_manifest.json").write_text(
            json.dumps({"ok": [], "failed": []}), encoding="utf-8")
        rc = cutover.run(ws.modsrc, ws.build, apply=False, vanilla=None,
                         do_dedup=False)
        self.assertEqual(rc, 0)
        mf = json.loads((ws.build / "cutover_manifest.json").read_text("utf-8"))
        self.assertEqual(mf["dedup"], 0)
        self.assertEqual(mf["deletions"], [])


if __name__ == "__main__":
    unittest.main()
