"""validate: DDS checks, moved-file resolution, orphans, meshes, check_log."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from _harness import BaseTest, crafted_dds
from pipeline import validate


def write_convert_manifest(build: Path, entries: list[str]):
    build.mkdir(parents=True, exist_ok=True)
    (build / "convert_manifest.json").write_text(
        json.dumps({"ok": [{"path": p, "ok": True} for p in entries],
                    "failed": []}), encoding="utf-8")


class ValidateRunTest(BaseTest):
    def _minimal_gfx(self):
        self.ws.mkdirs("gfx")

    def test_odd_dimensions_are_counted_not_a_problem(self):
        # Vanilla HoI4 ships 11 745 of 21 898 DDS with non-%4 dimensions and
        # loads them: reencoded sources keep their size, so this cannot be an
        # error without telling users to "fix" vanilla.
        self._minimal_gfx()
        (self.ws.modsrc / "gfx" / "a.dds").write_bytes(crafted_dds(6, 6, b"DXT1", 1))
        write_convert_manifest(self.ws.build, ["gfx/a.png"])
        rc = validate.run(self.ws.modsrc, None, self.ws.build, None)
        self.assertEqual(rc, 0)
        data = json.loads((self.ws.build / "validate.json").read_text("utf-8"))
        self.assertEqual(data["odd_dimensions"], 1)
        self.assertFalse(any("multiple of 4" in p for p in data["problems"]))

    def test_png_disguised_as_dds_is_a_problem(self):
        self._minimal_gfx()
        (self.ws.modsrc / "gfx" / "a.dds").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        self.ws.build.mkdir(parents=True, exist_ok=True)
        (self.ws.build / "convert_manifest.json").write_text(json.dumps(
            {"ok": [], "failed": [{"path": "gfx/a.dds", "ok": False,
                                   "note": "not a dds (PNG data)"}]}), encoding="utf-8")
        rc = validate.run(self.ws.modsrc, None, self.ws.build, None)
        self.assertEqual(rc, 1)
        data = json.loads((self.ws.build / "validate.json").read_text("utf-8"))
        self.assertEqual(data["mislabeled"], 1)
        self.assertTrue(any("not a dds (PNG data)" in p for p in data["problems"]))

    def test_skipped_conversion_keeps_the_original_and_is_counted(self):
        self._minimal_gfx()
        self.ws.make_png("gfx/b.png")
        self.ws.build.mkdir(parents=True, exist_ok=True)
        (self.ws.build / "convert_manifest.json").write_text(json.dumps(
            {"ok": [], "failed": [{"path": "gfx/b.png", "ok": False,
                                   "note": "duplicate stem in the same folder"}]}),
            encoding="utf-8")
        rc = validate.run(self.ws.modsrc, None, self.ws.build, None)
        self.assertEqual(rc, 0)
        data = json.loads((self.ws.build / "validate.json").read_text("utf-8"))
        self.assertEqual(data["conversion_skipped"], 1)
        self.assertEqual(data["problems"], [])

    def test_bad_fourcc_reported(self):
        self._minimal_gfx()
        (self.ws.modsrc / "gfx" / "a.dds").write_bytes(
            crafted_dds(8, 8, b"", 1))          # uncompressed: no DXT fourcc
        write_convert_manifest(self.ws.build, ["gfx/a.png"])
        validate.run(self.ws.modsrc, None, self.ws.build, None)
        data = json.loads((self.ws.build / "validate.json").read_text("utf-8"))
        self.assertTrue(any("fourcc" in p for p in data["problems"]))

    def test_good_dds_passes(self):
        self._minimal_gfx()
        (self.ws.modsrc / "gfx" / "a.dds").write_bytes(crafted_dds(8, 8, b"DXT1", 1))
        write_convert_manifest(self.ws.build, ["gfx/a.png"])
        rc = validate.run(self.ws.modsrc, None, self.ws.build, None)
        self.assertEqual(rc, 0)
        data = json.loads((self.ws.build / "validate.json").read_text("utf-8"))
        self.assertEqual(data["dds_checked"], 1)
        self.assertEqual(data["problems"], [])

    def test_missing_converted_dds_is_unverifiable_not_problem(self):
        self._minimal_gfx()
        write_convert_manifest(self.ws.build, ["gfx/gone.png"])
        rc = validate.run(self.ws.modsrc, None, self.ws.build, None)
        self.assertEqual(rc, 0)
        data = json.loads((self.ws.build / "validate.json").read_text("utf-8"))
        self.assertEqual(data["unverifiable"], 1)

    def test_moved_after_conversion_resolved_by_ledger_and_size(self):
        self._minimal_gfx()
        dds = self.ws.modsrc / "gfx" / "moved.dds"
        dds.write_bytes(crafted_dds(8, 8, b"DXT5", 1))
        size = dds.stat().st_size
        self.ws.build.mkdir(parents=True)
        (self.ws.build / "convert_manifest.json").write_text(
            json.dumps({"ok": [{"path": "gfx/orig.png", "ok": True,
                                "bytes": size}],
                        "failed": []}), encoding="utf-8")
        (self.ws.build / "moves_ledger.json").write_text(
            json.dumps({"gfx/orig.png": "gfx/moved.dds"}), encoding="utf-8")
        rc = validate.run(self.ws.modsrc, None, self.ws.build, None)
        self.assertEqual(rc, 0)
        data = json.loads((self.ws.build / "validate.json").read_text("utf-8"))
        self.assertEqual(data["unverifiable"], 0)
        self.assertEqual(data["dds_checked"], 1)
        self.assertEqual(data["fourccs"], {"DXT5": 1})

    def test_corrupt_mesh_is_a_problem(self):
        self._minimal_gfx()
        (self.ws.modsrc / "gfx" / "bad.mesh").write_bytes(b"junk")
        rc = validate.run(self.ws.modsrc, None, self.ws.build, None)
        self.assertEqual(rc, 1)
        data = json.loads((self.ws.build / "validate.json").read_text("utf-8"))
        self.assertTrue(any("unreadable mesh" in p for p in data["problems"]))

    def test_orphans_counted(self):
        self._minimal_gfx()
        from PIL import Image
        Image.new("RGBA", (4, 4)).save(self.ws.modsrc / "gfx" / "orphan.png")
        write_convert_manifest(self.ws.build, [])
        validate.run(self.ws.modsrc, None, self.ws.build, None)
        data = json.loads((self.ws.build / "validate.json").read_text("utf-8"))
        self.assertEqual(data["orphans"], 1)


class DescriptorCheckTest(BaseTest):
    """The drift that makes the game load vanilla and the mod together."""

    def tree(self, *, outer: str | None, inner: str, vanilla_states: bool = False):
        root = self.ws.root / "mod" / "merged"
        (root / "history" / "states").mkdir(parents=True)
        (root / "descriptor.mod").write_text(inner, encoding="utf-8")
        if outer is not None:
            (root.parent / "merged.mod").write_text(outer, encoding="utf-8")
        (root / "history" / "states" / "1002-Owensboro.txt").write_text(
            "state={\n\tid=1002\n\tname=\"STATE_1002\"\n}\n", encoding="utf-8")
        vanilla = None
        if vanilla_states:
            vanilla = self.ws.root / "vanilla"
            (vanilla / "history" / "states").mkdir(parents=True)
            (vanilla / "history" / "states" / "1002-Yazd.txt").write_text(
                "state={\n\tid=1002\n\tname=\"STATE_1002\"\n}\n",
                encoding="utf-8")
        return root, vanilla

    def test_consistent_pair_is_clean(self):
        inner = 'name="merged"\nversion="1"\nreplace_path="history/states"\n'
        outer = (inner + f'path="{self.ws.root.as_posix()}/mod/merged"\n')
        root, vanilla = self.tree(outer=outer, inner=inner, vanilla_states=True)
        self.assertEqual(validate.descriptor_problems(root, vanilla), [])

    def test_missing_launcher_file(self):
        root, vanilla = self.tree(outer=None, inner='name="merged"\n')
        problems = validate.descriptor_problems(root, vanilla)
        self.assertTrue(any("launcher file" in p for p in problems), problems)

    def test_launcher_without_replace_path(self):
        inner = 'name="merged"\nreplace_path="history/states"\n'
        outer = f'name="merged"\npath="{self.ws.root.as_posix()}/x"\n'
        root, vanilla = self.tree(outer=outer, inner=inner)
        problems = validate.descriptor_problems(root, vanilla)
        self.assertTrue(any("replace_path differs" in p for p in problems) or
                        any("replace_path missing" in p for p in problems),
                        problems)

    def test_launcher_without_path(self):
        inner = 'name="merged"\n'
        root, vanilla = self.tree(outer='name="merged"\n', inner=inner)
        problems = validate.descriptor_problems(root, vanilla)
        self.assertTrue(any('no path="..."' in p for p in problems), problems)

    def test_vanilla_state_overlap_without_replace_path(self):
        inner = 'name="merged"\n'
        outer = f'name="merged"\npath="{self.ws.root.as_posix()}/x"\n'
        root, vanilla = self.tree(outer=outer, inner=inner, vanilla_states=True)
        problems = validate.descriptor_problems(root, vanilla)
        self.assertTrue(any("state ids exist in vanilla" in p for p in problems),
                        problems)

    def test_state_overlap_is_fine_when_replaced(self):
        inner = 'name="merged"\nreplace_path="history/states"\n'
        outer = (inner + f'path="{self.ws.root.as_posix()}/x"\n')
        root, vanilla = self.tree(outer=outer, inner=inner, vanilla_states=True)
        self.assertEqual(validate.descriptor_problems(root, vanilla), [])

    def test_bare_tree_is_not_reported(self):
        root = self.ws.root / "bare"
        root.mkdir()
        self.assertEqual(validate.descriptor_problems(root, None), [])


class CheckLogTest(unittest.TestCase):
    def test_filters_and_rc(self):
        import tempfile
        log = Path(tempfile.mkdtemp()) / "error.log"
        log.write_text(
            "[texture.cpp:99]: Failed to load texture bad.dds\n"
            "random gameplay line\n"
            "[pdx_sprite types]: sprite GFX_x missing\n", encoding="utf-8")
        rc = validate.check_log(path=log)
        self.assertEqual(rc, 0)

    def test_fatal_classes_are_rc1(self):
        import tempfile
        log = Path(tempfile.mkdtemp()) / "error.log"
        log.write_text(
            "[x]: State ID conflict: Both \"Louisville\" ( a.txt) and "
            "\"Louisville\" ( b.txt).\n"
            "[x]: Failed to create id 12500001 50. Already exists in game. "
            "This might crash the game.\n"
            "[x]: Invalid trigger 'is_x' in common/decisions/X.txt\n",
            encoding="utf-8")
        self.assertEqual(validate.check_log(path=log), 1)

    def test_crash_report_is_shown(self):
        import tempfile
        docs = Path(tempfile.mkdtemp()) / "Paradox Interactive" / "HOI4"
        (docs / "logs").mkdir(parents=True)
        log = docs / "logs" / "error.log"
        log.write_text("[x]: random\n", encoding="utf-8")
        crash = docs / "crashes" / "hoi4_20260101_120000"
        crash.mkdir(parents=True)
        (crash / "exception.txt").write_text(
            "Unhandled Exception C0000005 at address 0x1\n", encoding="utf-8")
        self.assertEqual(validate.check_log(path=log), 0)
        self.assertEqual(validate._crash_report(log)[1], "Latest crash: hoi4_20260101_120000")

    def test_missing_default_log_rc2(self):
        # default search only: pretend none of the candidates exist
        from unittest import mock
        with mock.patch.object(validate, "GAME_LOG_CANDIDATES", ()):
            self.assertEqual(validate.check_log(), 2)
    def test_explicit_missing_path_is_graceful(self):
        rc = validate.check_log(path=Path("Z:/definitely/missing/error.log"))
        self.assertEqual(rc, 2, "BUG-09: FileNotFoundError traceback instead")


if __name__ == "__main__":
    unittest.main()
