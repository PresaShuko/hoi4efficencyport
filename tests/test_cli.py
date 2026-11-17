"""CLI integration: real subprocess runs of `python -m hoi4port` (offline).

Covers: --version, sync (real mirror), scan, normalize, analyze, meshscan,
convert --apply (bundled texconv on Windows), entityfix, cutover --apply,
validate, env-var resolution, error paths. Never touches real Steam installs:
--mod is always an explicit fixture/tmp path.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from _harness import FIXTURE, BaseTest, clean_env, run_cli, simple_mesh


class VersionTest(unittest.TestCase):
    def test_version_flag(self):
        from pipeline import __version__
        rc, out, _ = run_cli("--version")
        self.assertEqual(rc, 0)
        self.assertIn(f"hoi4port {__version__}", out)

    def test_unknown_command_rc2(self):
        rc, _, _ = run_cli("does-not-exist")
        self.assertNotEqual(rc, 0)


class SyncCliTest(BaseTest):
    def test_sync_from_fixture_path(self):
        staging = self.ws.staging
        rc, _, err = run_cli("sync", "--mod", str(FIXTURE),
                             "--staging", str(staging))
        self.assertEqual(rc, 0, err)
        self.assertTrue((staging / "descriptor.mod").is_file())
        self.assertTrue((staging / "gfx" / "fixture" / "test.dds").is_file())

    def test_sync_merges_several_mods_in_load_order(self):
        first = self.ws.root / "first"
        second = self.ws.root / "second"
        for root, name in ((first, "one"), (second, "two")):
            (root / "gfx").mkdir(parents=True)
            (root / "gfx" / f"{name}.png").write_bytes(b"x")
        (first / "shared.txt").write_text("from first", encoding="utf-8")
        (second / "shared.txt").write_text("from second", encoding="utf-8")
        rc, out, err = run_cli("sync", "--mod", str(first), "--mod", str(second),
                               "--staging", str(self.ws.staging),
                               "--out", str(self.ws.build))
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.ws.staging / "gfx" / "one.png").is_file())
        self.assertTrue((self.ws.staging / "gfx" / "two.png").is_file())
        self.assertEqual((self.ws.staging / "shared.txt").read_text(encoding="utf-8"),
                         "from second", "the later mod must win")
        report = self.ws.build / "merge_report.json"
        self.assertTrue(report.is_file(), "merge report missing")
        self.assertIn("shared.txt", report.read_text(encoding="utf-8"))
        self.assertIn("later", out.lower() + err.lower())

    def test_sync_with_nonexistent_id_errors(self):
        rc, _, err = run_cli("sync", "--mod", "000000000001")
        self.assertNotEqual(rc, 0)
        self.assertIn("000000000001", err)

    def test_sync_with_garbage_mod_errors(self):
        rc, _, err = run_cli("sync", "--mod", "Z:/definitely/not/a/mod")
        self.assertNotEqual(rc, 0)
        self.assertIn("unrecognized", err)


class ScanCliTest(BaseTest):
    def test_missing_staging_rc2(self):
        rc, _, err = run_cli("scan", "--mod", str(FIXTURE),
                             "--staging", str(self.ws.root / "nope"),
                             "--out", str(self.ws.build), "--skip-vanilla")
        self.assertEqual(rc, 2)
        self.assertIn("Staging not found", err)

    def test_scan_outputs(self):
        self.ws.sync()
        rc, out, err = run_cli("scan", "--mod", str(FIXTURE),
                               "--staging", str(self.ws.staging),
                               "--out", str(self.ws.build), "--skip-vanilla")
        self.assertEqual(rc, 0, err)
        self.assertIn("true: 0", out)
        self.assertTrue((self.ws.build / "scan.json").is_file())
        self.assertTrue((self.ws.build / "scan_report.txt").is_file())

    def test_env_var_resolution(self):
        self.ws.sync()
        env = clean_env({
            "HOI4PORT_MOD": str(FIXTURE),
            "HOI4PORT_STAGING": str(self.ws.staging),
            "HOI4PORT_OUT": str(self.ws.build),
        })
        rc, out, err = run_cli("scan", "--skip-vanilla", env=env,
                               cwd=self.ws.root)
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.ws.build / "scan.json").is_file())


class MeshscanCliTest(BaseTest):
    def test_mesh_report_from_staging(self):
        self.ws.sync()
        (self.ws.staging / "gfx" / "models").mkdir(parents=True)
        (self.ws.staging / "gfx" / "models" / "tank.mesh").write_bytes(
            simple_mesh(verts=15, tris=3))
        rc, out, err = run_cli("meshscan", "--mod", str(FIXTURE),
                               "--staging", str(self.ws.staging),
                               "--out", str(self.ws.build), "--skip-vanilla")
        self.assertEqual(rc, 0, err)
        self.assertIn("15", out)
        self.assertTrue((self.ws.build / "mesh_stats.json").is_file())


@unittest.skipUnless(os.name == "nt", "bundled texconv.exe required")
class EndToEndTest(BaseTest):
    """Full pipeline on a synthetic mod: ui png conversion path."""

    def _build_mod(self):
        ws = self.ws
        ws.write("descriptor.mod",
                 'name = "e2e"\npath = "mod/e2e"\ntags = { "Graphics" }\n'
                 'picture = "gfx/interface/ico.png"\n')
        ws.write("interface/sprites.gfx",
                 'spriteType = {\n name = "GFX_ico"\n'
                 ' texturefile = "gfx/interface/ico.png"\n}\n')
        ws.make_png("gfx/interface/ico.png", w=32, h=16, mode="RGB")
        ws.write("interface/w.gui",
                 'guiTypes = {\n windowType = {\n name = "w"\n'
                 ' sprite = "GFX_ico"\n size = { x = 8 y = 8 }\n}\n}\n')
        ws.write("common/national_focus/f.txt",
                 'focus = {\n id = E2E\n icon = "art/goal.png"\n}\n')
        ws.make_png("art/goal.png", w=64, h=64, mode="RGB")
        ws.write("map/ambient_object.txt",
                 'ambient_object = {\n type = "amb_tree"\n}\n')
        ws.write("gfx/entities/trees.asset",
                 'entity = {\n name = "amb_tree"\n pdxmesh = "tree"\n}\n')

    def test_full_pipeline(self):
        ws = self.ws
        self._build_mod()
        common = ["--mod", str(ws.modsrc), "--staging", str(ws.staging),
                  "--out", str(ws.build)]
        env = clean_env()

        self.assertEqual(run_cli("sync", "--mod", str(ws.modsrc),
                                 "--staging", str(ws.staging), env=env)[0], 0)
        rc, out, _ = run_cli("scan", *common, "--skip-vanilla", env=env)
        self.assertEqual(rc, 0)
        self.assertIn("true: 0", out)

        # normalize: art/goal.png -> gfx/interface/goals/goal.png
        self.assertEqual(run_cli("normalize", *common, "--apply", env=env)[0], 0)
        self.assertTrue(ws.sp("gfx/interface/goals/goal.png").is_file())
        f = (ws.staging / "common/national_focus/f.txt").read_text("utf-8")
        self.assertIn("gfx/interface/goals/goal.png", f)

        self.assertEqual(run_cli("analyze", *common, "--skip-vanilla", env=env)[0], 0)
        self.assertEqual(run_cli("meshscan", *common, "--skip-vanilla", env=env)[0], 0)

        # entityfix: ambient entity gets cull_radius, idempotent
        self.assertEqual(run_cli("entityfix", *common, "--apply", env=env)[0], 0)
        asset = (ws.staging / "gfx/entities/trees.asset").read_text("utf-8")
        self.assertIn("cull_radius = 100", asset)
        self.assertEqual(run_cli("entityfix", *common, "--apply", env=env)[0], 0)
        mf = json.loads((ws.build / "entityfix_manifest.json").read_text("utf-8"))
        self.assertEqual(mf["edits"], [])

        # convert: ico.png (ui, cap 16) -> dds
        rc, out, _ = run_cli("convert", *common, "--apply", env=env)
        self.assertEqual(rc, 0)
        self.assertTrue(ws.sp("gfx/interface/ico.dds").is_file())

        # cutover: rewrite texturefile to .dds, delete png source
        self.assertEqual(run_cli("cutover", *common, "--apply", env=env)[0], 0)
        gfx = (ws.staging / "interface/sprites.gfx").read_text("utf-8")
        self.assertIn("gfx/interface/ico.dds", gfx)
        self.assertFalse(ws.sp("gfx/interface/ico.png").exists())

        # texfix: nothing under models/entities -> rc 2
        rc, out, _ = run_cli("texfix", *common, env=env)
        self.assertEqual(rc, 2)
        self.assertIn("No model textures", out)

        rc, out, _ = run_cli("validate", *common, env=env)
        self.assertEqual(rc, 0, out)
        self.assertIn("Header problems: 0", out)
        self.assertIn("true missing: 0", out)

        # idempotency: normalize finds nothing new after cutover
        rc, out, _ = run_cli("normalize", *common, "--apply", env=env)
        self.assertIn("No changes needed.", out)


class CliErrorPathsTest(BaseTest):
    def test_analyze_missing_staging(self):
        rc, _, err = run_cli("analyze", "--mod", str(FIXTURE),
                             "--staging", str(self.ws.root / "gone"),
                             "--out", str(self.ws.build))
        self.assertEqual(rc, 2)

    def test_cutover_missing_manifests(self):
        self.ws.sync()
        rc, out, _ = run_cli("cutover", "--mod", str(FIXTURE),
                             "--staging", str(self.ws.staging),
                             "--out", str(self.ws.build))
        self.assertEqual(rc, 2)
        self.assertIn("missing", out)

    def test_check_log_explicit_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log",
                                         delete=False) as fh:
            fh.write("[texture.cpp:1]: Failed to load tex x.dds\n")
            p = fh.name
        try:
            import io
            from contextlib import redirect_stdout
            from pipeline import validate
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = validate.check_log(path=Path(p))
            self.assertEqual(rc, 0)
            self.assertIn("1 lines analyzed", buf.getvalue())
        finally:
            os.unlink(p)


if __name__ == "__main__":
    unittest.main()
