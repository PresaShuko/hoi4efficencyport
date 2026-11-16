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

    def test_bad_dimensions_reported_and_rc1(self):
        self._minimal_gfx()
        dds = self.ws.modsrc / "gfx" / "a.dds"
        dds.write_bytes(crafted_dds(6, 6, b"DXT1", 1))   # not %4
        write_convert_manifest(self.ws.build, ["gfx/a.png"])
        rc = validate.run(self.ws.modsrc, None, self.ws.build, None)
        self.assertEqual(rc, 1)
        data = json.loads((self.ws.build / "validate.json").read_text("utf-8"))
        self.assertTrue(any("multiple of 4" in p for p in data["problems"]))

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
