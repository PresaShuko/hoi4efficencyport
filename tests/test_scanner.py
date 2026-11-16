"""scanner: comment stripping, sprite defs, reference classification, reports."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from _harness import BaseTest
from pipeline import scanner


class StripCommentsTest(unittest.TestCase):
    def test_simple_comment(self):
        self.assertEqual(scanner.strip_comments("a = 1 # note\nb = 2"),
                         "a = 1 \nb = 2")

    def test_hash_inside_quotes_kept(self):
        text = 'texture = "file#1.dds" # real comment'
        out = scanner.strip_comments(text)
        self.assertIn("file#1.dds", out)
        self.assertNotIn("real comment", out)

    def test_full_line_comment(self):
        self.assertEqual(scanner.strip_comments("# whole line\nx = 1"), "\nx = 1")


class SpriteDefsTest(BaseTest):
    def test_collects_sprite_and_cornered_names(self):
        self.ws.write("interface/a.gfx",
                      'spriteType = {\n name = "GFX_a"\n}\n'
                      'corneredTileSpriteType = {\n name = "GFX_c"\n}\n'
                      '# spriteType = { name = "GFX_dead" }\n')
        self.ws.write("gfx/sub/b.gfx", 'spriteType = {\n name = "GFX_b"\n}\n')
        names = scanner.sprite_defs(self.ws.modsrc)
        self.assertEqual(names, {"GFX_a", "GFX_b", "GFX_c"})


class ScanTest(BaseTest):
    def setUp(self):
        super().setUp()
        ws = self.ws
        ws.write("interface/ui.gfx",
                 'spriteType = {\n'
                 '  name = "GFX_goal"\n'
                 '  texturefile = "gfx/interface/goals/goal.dds"\n'
                 '  noOfFrames = 4\n'
                 '}\n')
        ws.make_png("gfx/interface/goals/goal.dds".replace(".dds", ".png"))
        ws.write("interface/gui.gui",
                 'windowType = {\n name = "w"\n'
                 ' sprite = "GFX_goal"\n'
                 ' iconTexture = "gfx/interface/goals/goal.png"\n}\n')
        ws.write("common/national_focus/focus.txt",
                 'focus = {\n id = FOO\n sprite = GFX_goal\n'
                 ' picture = "gfx/event_pictures/ev.dds"\n'
                 ' missing_icon = "gfx/interface/goals/gone.dds"\n'
                 ' external = "gfx/vanilla_only.png"\n'
                 ' weird = "models/infantry"\n'
                 ' url = "https://example.com/[GetTag]"\n}\n')
        ws.make_png("gfx/event_pictures/ev.png")
        ws.write("descriptor.mod", 'name = "t"\npicture = "thumb.png"\n')
        ws.make_png("thumb.png")

        self.vanilla = ws.root / "vanilla"
        (self.vanilla / "gfx").mkdir(parents=True)
        (self.vanilla / "gfx" / "vanilla_only.png").write_bytes(b"v")
        (self.vanilla / "interface").mkdir()
        (self.vanilla / "interface" / "v.gfx").write_text(
            'spriteType = { name = "GFX_vanilla_sprite" }', encoding="utf-8")

        self.result = scanner.scan(ws.modsrc, self.vanilla)

    def test_encoding_stats(self):
        # descriptor.mod is NOT in TEXT_EXTS: 4 text files on disk, 3 scanned
        self.assertEqual(sum(self.result["files_by_encoding"].values()), 3)

    def test_existing_reference(self):
        ref = {r["token"]: r for r in self.result["references"]}
        self.assertTrue(ref["gfx/interface/goals/goal.png"]["exists"])

    def test_true_missing(self):
        self.assertIn("gfx/interface/goals/gone.dds", self.result["missing_true"])

    def test_vanilla_fallback(self):
        self.assertIn("gfx/vanilla_only.png",
                      self.result["missing_fallback_vanilla"])

    def test_noise_and_suspect(self):
        self.assertIn("models/infantry", self.result["missing_noise"])
        self.assertIn("https://example.com/[GetTag]",
                      self.result["missing_suspect"])

    def test_sprite_types_parsed(self):
        st = self.result["sprite_types"]["GFX_goal"]
        self.assertEqual(st["texturefile"], "gfx/interface/goals/goal.dds")
        self.assertEqual(st["noOfFrames"], 4)

    def test_gfx_refs_quoted_and_bare(self):
        # the sprite DEFINITION (interface/ui.gfx:2) is not a usage (BUG-06)
        rec = self.result["gfx_refs"]["GFX_goal"]
        self.assertEqual(rec["quoted"], ["interface/gui.gui:3"])
        self.assertEqual(rec["bare"], ["common/national_focus/focus.txt:3"])

    def test_gfx_missing_and_covered(self):
        # GFX_vanilla_sprite is referenced nowhere: only defined count matters
        self.assertEqual(self.result["gfx_missing"], [])
        self.assertEqual(self.result["gfx_covered_by_vanilla"], [])
        self.assertEqual(self.result["gfx_vanilla_defs_count"], 1)
    def test_gfx_unused_defs(self):
        self.ws.write("interface/unused.gfx",
                      'spriteType = {\n name = "GFX_unused"\n}\n')
        result = scanner.scan(self.ws.modsrc)
        self.assertIn("GFX_unused", result["gfx_unused_defs"])
        # a defined-and-used sprite is not "unused"
        self.assertNotIn("GFX_goal", result["gfx_unused_defs"])

    def test_missing_gfx_static(self):
        self.ws.write("interface/broken.gui", 'sprite = "GFX_not_defined_anywhere"\n')
        result = scanner.scan(self.ws.modsrc)
        self.assertIn("GFX_not_defined_anywhere", result["gfx_missing_static"])

    def test_backslash_token_normalized(self):
        self.ws.write("common/x.txt", 'icon = "gfx\\interface\\goals\\goal.png"\n')
        result = scanner.scan(self.ws.modsrc)
        tokens = {r["token"] for r in result["references"]}
        self.assertIn("gfx/interface/goals/goal.png", tokens)

    def test_entity_relative_resolution(self):
        # texture referenced relative to its own folder (entities .gfx style)
        self.ws.make_png("gfx/entities/sub/tex.png")
        self.ws.write("gfx/entities/sub/e.gfx",
                      'spriteType = {\n name = "GFX_e"\n'
                      ' texturefile = "tex.png"\n}\n')
        result = scanner.scan(self.ws.modsrc)
        ref = {r["token"]: r for r in result["references"]}["tex.png"]
        self.assertTrue(ref["exists"])

    def test_vanilla_baseline_and_cache(self):
        out = self.ws.root / "vanilla_hash.json"
        status = scanner.vanilla_baseline(self.vanilla, out, skip=False)
        self.assertEqual(status, "1 file")
        self.assertEqual(scanner.vanilla_baseline(self.vanilla, out, skip=False),
                         "cached")
        hashes = json.loads(out.read_text(encoding="utf-8"))["hashes"]
        self.assertIn("gfx/vanilla_only.png", hashes)

    def test_vanilla_baseline_invalidated_by_game_update(self):
        out = self.ws.root / "vanilla_hash.json"
        self.assertEqual(scanner.vanilla_baseline(self.vanilla, out, skip=False),
                         "1 file")
        first = out.read_text(encoding="utf-8")
        self.assertEqual(scanner.vanilla_baseline(self.vanilla, out, skip=False),
                         "cached")
        # Steam rewrites launcher-settings.json on update: stale cache dropped
        (self.vanilla / "launcher-settings.json").write_text(
            '{"version": "9.9.9"}', encoding="utf-8")
        self.assertEqual(scanner.vanilla_baseline(self.vanilla, out, skip=False),
                         "1 file")
        self.assertNotEqual(out.read_text(encoding="utf-8"), first)

    def test_vanilla_baseline_skipped(self):
        out = self.ws.root / "vanilla_hash.json"
        self.assertEqual(scanner.vanilla_baseline(None, out, skip=False), "skipped")
        self.assertFalse(out.exists())


class WriteReportTest(BaseTest):
    def test_report_mentions_counts(self):
        self.ws.write("interface/a.gfx",
                      'spriteType = {\n name = "GFX_a"\n'
                      ' texturefile = "gfx/a.png"\n}\n')
        self.ws.make_png("gfx/a.png")
        res = scanner.scan(self.ws.modsrc)
        out = self.ws.root / "scan_report.txt"
        scanner.write_report(res, out)
        text = out.read_text(encoding="utf-8")
        self.assertIn("TRUE missing: 0", text)
        self.assertIn("spriteType defined (mod): 1", text)


class VanillaSpriteCacheTest(BaseTest):
    def setUp(self):
        super().setUp()
        cache = self.ws.root / "cache"
        patcher = mock.patch.object(scanner.fsutil, "cache_dir", lambda: cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.van = self.ws.root / "van"
        (self.van / "interface").mkdir(parents=True)
        (self.van / "interface" / "v.gfx").write_text(
            'spriteType = {\n name = "GFX_v"\n}\n', encoding="utf-8")
        (self.van / "launcher-settings.json").write_text(
            '{"version": "1.0"}', encoding="utf-8")

    def test_second_call_reads_the_cache(self):
        self.assertEqual(scanner.vanilla_sprite_defs(self.van), {"GFX_v"})
        with mock.patch.object(scanner, "sprite_defs",
                               side_effect=AssertionError("re-parsed")):
            self.assertEqual(scanner.vanilla_sprite_defs(self.van), {"GFX_v"})

    def test_unknown_install_stamp_bypasses_the_cache(self):
        with mock.patch.object(scanner.fsutil, "install_stamp",
                               lambda _: "unknown"), \
                mock.patch.object(scanner, "sprite_defs",
                                  return_value={"GFX_x"}) as fake:
            scanner.vanilla_sprite_defs(self.van)
            scanner.vanilla_sprite_defs(self.van)
        self.assertEqual(fake.call_count, 2)


class ExistenceIndexTest(BaseTest):
    def test_directory_ref_exists(self):
        self.ws.make_png("gfx/interface/goals/g.png")
        self.ws.write("common/a.txt", 'd = "gfx/interface/goals/"\n')
        ref = {r["token"]: r for r in scanner.scan(self.ws.modsrc)["references"]}
        self.assertTrue(ref["gfx/interface/goals/"]["exists"])

    def test_case_variant_matches_platform_exists(self):
        self.ws.make_png("gfx/interface/goals/g.png")
        self.ws.write("common/a.txt", 'c = "GFX/Interface/goals/g.png"\n')
        ref = {r["token"]: r for r in scanner.scan(self.ws.modsrc)["references"]}
        want = (self.ws.modsrc / "GFX/Interface/goals/g.png").exists()
        self.assertEqual(ref["GFX/Interface/goals/g.png"]["exists"], want)

    def test_parent_escape_falls_back_to_stat(self):
        self.ws.make_png("gfx/interface/g.png")
        self.ws.write("gfx/sub/a.txt", 'r = "../interface/g.png"\n')
        ref = {r["token"]: r for r in scanner.scan(self.ws.modsrc)["references"]}
        self.assertTrue(ref["../interface/g.png"]["exists"])


if __name__ == "__main__":
    unittest.main()
