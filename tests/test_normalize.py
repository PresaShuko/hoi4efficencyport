"""normalize: classification, move planning, repairs, rewriting, apply, idempotency.

BUG-02 (expectedFailure): a planned move overwrites a pre-existing file at the
destination (_unique_dest only knows about planned destinations, not the disk).
"""
from __future__ import annotations

import json
import unittest

from PIL import Image

from _harness import BaseTest, make_scan
from pipeline import normalize, scanner


class ClassifyTokenTest(BaseTest):
    def test_direct_focus_rule(self):
        ws = self.ws
        ws.write("common/national_focus/f.txt", 'i = "gfx/x.png"\n')
        sr = make_scan(ws)
        ref = sr["references"][0]
        cls = normalize.classify_token("gfx/x.png", sr, {}, ref)
        self.assertEqual(cls, "gfx/interface/goals/")

    def test_direct_event_rule(self):
        ws = self.ws
        ws.write("events/e.txt", 'p = "gfx/ev.png"\n')
        sr = make_scan(ws)
        self.assertEqual(
            normalize.classify_token("gfx/ev.png", sr, {}, sr["references"][0]),
            "gfx/event_pictures/")

    def test_sprite_chain_to_focus(self):
        ws = self.ws
        ws.write("interface/s.gfx",
                 'spriteType = {\n name = "GFX_focus_a"\n'
                 ' texturefile = "art/a.png"\n}\n')
        ws.write("common/national_focus/f.txt", 'sprite = GFX_focus_a\n')
        ws.make_png("art/a.png")
        sr = make_scan(ws)
        sprite_by_tex = {"art/a.png": ["GFX_focus_a"]}
        ref = {r["token"]: r for r in sr["references"]}["art/a.png"]
        self.assertEqual(
            normalize.classify_token("art/a.png", sr, sprite_by_tex, ref),
            "gfx/interface/goals/")
    def test_conflicting_uses_return_none(self):
        ws = self.ws
        # one texture, two sprites with DIFFERENT consumer classes -> ambiguous
        ws.write("interface/s1.gfx",
                 'spriteType = {\n name = "GFX_focus_one"\n'
                 ' texturefile = "art/a.png"\n}\n')
        ws.write("interface/s2.gfx",
                 'spriteType = {\n name = "GFX_idea_one"\n'
                 ' texturefile = "art/a.png"\n}\n')
        ws.write("common/national_focus/f.txt", 'sprite = GFX_focus_one\n')
        ws.write("common/ideas/i.txt", 'sprite = GFX_idea_one\n')
        ws.make_png("art/a.png")
        sr = make_scan(ws)
        sprite_by_tex = {"art/a.png": ["GFX_focus_one", "GFX_idea_one"]}
        ref = {r["token"]: r for r in sr["references"]}["art/a.png"]
        self.assertIsNone(
            normalize.classify_token("art/a.png", sr, sprite_by_tex, ref))

    def test_single_sprite_multi_use_uses_rule_priority(self):
        # one sprite used both as focus and idea: rule order decides (goals),
        # by design "first match wins"
        ws = self.ws
        ws.write("interface/s.gfx",
                 'spriteType = {\n name = "GFX_both"\n'
                 ' texturefile = "art/a.png"\n}\n')
        ws.write("common/national_focus/f.txt", 'sprite = GFX_both\n')
        ws.write("common/ideas/i.txt", 'sprite = GFX_both\n')
        ws.make_png("art/a.png")
        sr = make_scan(ws)
        sprite_by_tex = {"art/a.png": ["GFX_both"]}
        ref = {r["token"]: r for r in sr["references"]}["art/a.png"]
        self.assertEqual(
            normalize.classify_token("art/a.png", sr, sprite_by_tex, ref),
            "gfx/interface/goals/")

    def test_interface_fallback(self):
        ws = self.ws
        ws.write("interface/gui.gui", 't = "art/ui.png"\n')
        ws.make_png("art/ui.png")
        sr = make_scan(ws)
        self.assertEqual(
            normalize.classify_token("art/ui.png", sr, {}, sr["references"][0]),
            "ui")


class PlanMovesTest(BaseTest):
    def test_stray_focus_icon_moves(self):
        ws = self.ws
        ws.write("common/national_focus/f.txt", 'i = "art/goal.png"\n')
        ws.make_png("art/goal.png")
        moves = normalize.plan_moves(ws.modsrc, make_scan(ws))
        self.assertEqual([(m["src"], m["dst"]) for m in moves],
                         [("art/goal.png", "gfx/interface/goals/goal.png")])

    def test_portrait_tag_from_sprite_name(self):
        ws = self.ws
        ws.write("interface/p.gfx",
                 'spriteType = {\n name = "GFX_GER_portrait_minister"\n'
                 ' texturefile = "art/ger_lead.png"\n}\n')
        ws.write("portraits/lead.txt", 'name = "X" portrait = GFX_GER_portrait_minister\n')
        ws.make_png("art/ger_lead.png")
        moves = normalize.plan_moves(ws.modsrc, make_scan(ws))
        self.assertEqual([m["dst"] for m in moves],
                         ["gfx/leaders/GER/ger_lead.png"])

    def test_model_and_flag_assets_skipped(self):
        ws = self.ws
        ws.write("common/ideas/i.txt", 'i = "gfx/models/m.png"\n')
        ws.make_png("gfx/models/m.png")
        ws.write("common/ideas/j.txt", 'i = "gfx/flags/fl.png"\n')
        ws.make_png("gfx/flags/fl.png")
        self.assertEqual(normalize.plan_moves(ws.modsrc, make_scan(ws)), [])

    def test_canonical_position_untouched(self):
        ws = self.ws
        ws.write("common/national_focus/f.txt",
                 'i = "gfx/interface/goals/goal.png"\n')
        ws.make_png("gfx/interface/goals/goal.png")
        self.assertEqual(normalize.plan_moves(ws.modsrc, make_scan(ws)), [])

    def test_ui_structure_preserved(self):
        ws = self.ws
        ws.write("interface/gui.gui", 't = "gfx/banner/decals/d.png"\n')
        ws.make_png("gfx/banner/decals/d.png")
        moves = normalize.plan_moves(ws.modsrc, make_scan(ws))
        self.assertEqual([m["dst"] for m in moves],
                         ["gfx/interface/banner/decals/d.png"])

    def test_non_texture_never_moved(self):
        ws = self.ws
        ws.write("common/national_focus/f.txt", 's = "script/focus_effect.txt"\n')
        ws.write("script/focus_effect.txt", "x = 1\n")
        self.assertEqual(normalize.plan_moves(ws.modsrc, make_scan(ws)), [])

    def test_doubled_separator_canonical_rewrites_in_text_only(self):
        # scan already normalized backslash tokens; '//' collapses via as_posix
        ws = self.ws
        ws.write("common/national_focus/f.txt",
                 'i = "gfx//interface//goals//goal.png"\n')
        ws.make_png("gfx/interface/goals/goal.png")
        moves = normalize.plan_moves(ws.modsrc, make_scan(ws))
        self.assertEqual([(m["src"], m["dst"], m["cls"]) for m in moves],
                         [("gfx//interface//goals//goal.png",
                           "gfx/interface/goals/goal.png", "norm")])

    def test_same_basename_collision_planned_to_same_dest(self):
        ws = self.ws
        ws.write("common/national_focus/f.txt", 'i = "art/goal_a.png"\n')
        ws.make_png("art/goal_a.png")
        moves = normalize.plan_moves(ws.modsrc, make_scan(ws))
        self.assertEqual([m["dst"] for m in moves],
                         ["gfx/interface/goals/goal_a.png"])


class MoveOverwriteRegression(BaseTest):
    def test_existing_destination_file_is_not_overwritten(self):
        ws = self.ws
        ws.write("common/national_focus/f.txt",
                 'i = "gfx/interface/goals/goal_a.png"\n')
        ws.make_png("gfx/interface/goals/goal_a.png", color=(0, 255, 0, 255))
        ws.write("common/national_focus/g.txt", 'i = "art/goal_a.png"\n')
        ws.make_png("art/goal_a.png", color=(255, 0, 0, 255))

        normalize.run(ws.modsrc, ws.build, apply=True)

        px = Image.open(ws.modsrc / "gfx/interface/goals/goal_a.png").getpixel((0, 0))
        self.assertEqual(px, (0, 255, 0, 255),
                         "BUG-02: canonical file was silently overwritten")
        # the stray copy must land under a NEW name, with the reference rewritten
        self.assertTrue((ws.modsrc / "gfx/interface/goals/goal_a_2.png").is_file())
        g = (ws.modsrc / "common/national_focus/g.txt").read_text(encoding="utf-8")
        self.assertIn("goal_a_2.png", g)
        # the moved duplicate content is identical to the stray source
        px2 = Image.open(ws.modsrc / "gfx/interface/goals/goal_a_2.png").getpixel((0, 0))
        self.assertEqual(px2, (255, 0, 0, 255))


class PlanRepairsTest(BaseTest):
    def test_leading_slash_repair(self):
        ws = self.ws
        ws.write("common/ideas/i.txt", 'i = "/gfx/interface/ideas/idea.png"\n')
        ws.make_png("gfx/interface/ideas/idea.png")
        repairs = normalize.plan_repairs(ws.modsrc, make_scan(ws))
        self.assertEqual([(r["src"], r["dst"]) for r in repairs],
                         [("/gfx/interface/ideas/idea.png",
                           "gfx/interface/ideas/idea.png")])

    def test_doubled_separator_resolves_without_repair(self):
        # pathlib/the OS collapse '//' so the token already exists: no repair
        ws = self.ws
        ws.write("common/ideas/i.txt", 'i = "gfx//interface//ideas//idea.png"\n')
        ws.make_png("gfx/interface/ideas/idea.png")
        repairs = normalize.plan_repairs(ws.modsrc, make_scan(ws))
        self.assertEqual(repairs, [])

    def test_extension_twin_repair(self):
        ws = self.ws
        ws.write("common/ideas/i.txt", 'i = "gfx/interface/ideas/idea.dds"\n')
        ws.make_png("gfx/interface/ideas/idea.png")
        repairs = normalize.plan_repairs(ws.modsrc, make_scan(ws))
        self.assertEqual([(r["src"], r["dst"]) for r in repairs],
                         [("gfx/interface/ideas/idea.dds",
                           "gfx/interface/ideas/idea.png")])

    def test_no_repair_when_truly_missing(self):
        ws = self.ws
        ws.write("common/ideas/i.txt", 'i = "gfx/interface/ideas/nope.png"\n')
        self.assertEqual(normalize.plan_repairs(ws.modsrc, make_scan(ws)), [])

    def test_vanilla_fallback_not_repaired(self):
        ws = self.ws
        vanilla = ws.root / "vanilla"
        (vanilla / "gfx").mkdir(parents=True)
        (vanilla / "gfx" / "v.png").write_bytes(b"v")
        ws.write("common/ideas/i.txt", 'i = "gfx/v.png"\n')
        sr = scanner.scan(ws.modsrc, vanilla)
        self.assertEqual(normalize.plan_repairs(ws.modsrc, sr), [])


class RewriteRefsTest(BaseTest):
    def test_replaces_slash_and_backslash_variants(self):
        ws = self.ws
        fp = ws.write("common/ideas/i.txt",
                      'a = "gfx/a.png"\nb = "gfx\\a.png"\n')
        normalize._rewrite_refs(ws.modsrc,
                                [{"kind": "repair", "src": "gfx/a.png",
                                  "dst": "gfx/b.png",
                                  "locs": ["common/ideas/i.txt:1"]}],
                                apply=True)
        text = fp.read_text(encoding="utf-8")
        # both the slash and the backslash variant become the dst text verbatim
        self.assertEqual(text.count('"gfx/b.png"'), 2)
        self.assertNotIn("a.png", text)

    def test_longest_token_first(self):
        ws = self.ws
        fp = ws.write("common/ideas/i.txt",
                      'a = "gfx/aa.png"\nb = "gfx/a.png"\n')
        normalize._rewrite_refs(ws.modsrc, [
            {"kind": "repair", "src": "gfx/a.png", "dst": "X.png",
             "locs": ["common/ideas/i.txt:1"]},
            {"kind": "repair", "src": "gfx/aa.png", "dst": "Y.png",
             "locs": ["common/ideas/i.txt:1"]},
        ], apply=True)
        text = fp.read_text(encoding="utf-8")
        self.assertIn('"Y.png"', text)
        self.assertIn('"X.png"', text)

    def test_dry_run_touches_nothing(self):
        ws = self.ws
        fp = ws.write("common/ideas/i.txt", 'a = "gfx/a.png"\n')
        stats = normalize._rewrite_refs(ws.modsrc,
                                        [{"kind": "repair", "src": "gfx/a.png",
                                          "dst": "gfx/b.png",
                                          "locs": ["common/ideas/i.txt:1"]}],
                                        apply=False)
        self.assertEqual(stats["substitutions"], 1)
        self.assertIn("gfx/a.png", fp.read_text(encoding="utf-8"))


class RunApplyTest(BaseTest):
    def _mk(self):
        ws = self.ws
        ws.write("common/national_focus/f.txt", 'i = "art/goal.png"\n')
        ws.make_png("art/goal.png")
        ws.write("common/ideas/i.txt", 'i = "/gfx/interface/ideas/idea.png"\n')
        ws.make_png("gfx/interface/ideas/idea.png")

    def test_dry_run_moves_nothing(self):
        self._mk()
        res = normalize.run(self.ws.modsrc, self.ws.build, apply=False)
        self.assertTrue(res["dry_run"])
        self.assertTrue((self.ws.modsrc / "art/goal.png").is_file())
        self.assertFalse((self.ws.modsrc / "gfx/interface/goals/goal.png").exists())

    def test_apply_moves_and_repairs_and_writes_ledger(self):
        self._mk()
        res = normalize.run(self.ws.modsrc, self.ws.build, apply=True)
        self.assertTrue((self.ws.modsrc / "gfx/interface/goals/goal.png").is_file())
        self.assertFalse((self.ws.modsrc / "art/goal.png").exists())
        f = (self.ws.modsrc / "common/national_focus/f.txt").read_text("utf-8")
        self.assertIn("gfx/interface/goals/goal.png", f)
        i = (self.ws.modsrc / "common/ideas/i.txt").read_text("utf-8")
        self.assertIn('"gfx/interface/ideas/idea.png"', i)
        ledger = json.loads((self.ws.build / "moves_ledger.json").read_text("utf-8"))
        self.assertEqual(ledger, {"art/goal.png": "gfx/interface/goals/goal.png"})
        self.assertEqual(res["verify"]["src_still_present"], [])
        self.assertEqual(res["verify"]["dst_unresolved"], [])

    def test_second_apply_is_noop(self):
        self._mk()
        normalize.run(self.ws.modsrc, self.ws.build, apply=True)
        res = normalize.run(self.ws.modsrc, self.ws.build, apply=True)
        self.assertEqual(res["moves"], [])
        self.assertEqual(res["repairs"], [])

    def test_no_moves_flag_repairs_only(self):
        self._mk()
        res = normalize.run(self.ws.modsrc, self.ws.build, apply=True,
                            do_moves=False)
        self.assertEqual(res["moves"], [])
        # repair still applied
        i = (self.ws.modsrc / "common/ideas/i.txt").read_text("utf-8")
        self.assertNotIn('"/gfx', i)
        # stray file not moved
        self.assertTrue((self.ws.modsrc / "art/goal.png").is_file())

    def test_missing_staging_exits(self):
        with self.assertRaises(SystemExit):
            normalize.run(self.ws.root / "no_staging", self.ws.build, apply=False)


if __name__ == "__main__":
    unittest.main()
