"""meshscan: binary mesh parsing, medians, baseline cache, run()."""
from __future__ import annotations

import json
import unittest

from _harness import BaseTest, MeshBuilder, simple_mesh
from pipeline import meshscan


class ExpectedMipsTest(unittest.TestCase):
    def test_vectors(self):
        cases = {(1, 1): 1, (2, 2): 2, (4, 2): 3, (64, 32): 7,
                 (2048, 8): 12, (100, 64): 1, (0, 0): 1, (3, 3): 1}
        for (w, h), want in cases.items():
            self.assertEqual(meshscan.expected_mips(w, h), want, (w, h))


class ParseMeshTest(BaseTest):
    def test_basic_geometry_and_material(self):
        p = self.ws.root / "tank.mesh"
        p.write_bytes(simple_mesh(verts=9, tris=4, shader="Standard",
                                  diff="models/tank_diff.dds", bones=0))
        rec = meshscan._parse_mesh(p)
        self.assertEqual(rec["verts"], 9)
        self.assertEqual(rec["tris"], 4)
        self.assertEqual(rec["submeshes"], 1)
        self.assertEqual(rec["materials"], 1)
        self.assertEqual(rec["bones"], 0)
        self.assertEqual(rec["collision_verts"], 0)

    def test_collision_shader_flagged(self):
        p = self.ws.root / "col.mesh"
        p.write_bytes(simple_mesh(verts=3, shader="collision", bones=0))
        rec = meshscan._parse_mesh(p)
        self.assertEqual(rec["collision_verts"], 3)

    def test_multi_submesh_and_bones(self):
        b = MeshBuilder()
        b.obj("mesh").floats("p", [0.0] * 6).ints("tri", [0, 1, 2])
        b.obj("material").string("shader", "Standard").string("diff", "a.dds")
        b.obj("skin").ints("bones", [23, 0, 0, 0])
        b.obj("mesh").floats("p", [0.0] * 3).ints("tri", [0, 1, 2])
        b.obj("material").string("shader", "Standard").string("diff", "b.dds")
        p = self.ws.root / "two.mesh"
        p.write_bytes(b.bytes_())
        rec = meshscan._parse_mesh(p)
        self.assertEqual(rec["submeshes"], 2)
        self.assertEqual(rec["verts"], 3)     # 2 + 1
        self.assertEqual(rec["tris"], 2)
        self.assertEqual(rec["materials"], 2)
        self.assertEqual(rec["bones"], 23)

    def test_bad_header_raises(self):
        p = self.ws.root / "bad.mesh"
        p.write_bytes(b"JUNKJUNK")
        with self.assertRaises(ValueError):
            meshscan._parse_mesh(p)

    def test_unknown_property_type_raises(self):
        b = MeshBuilder()
        b.buf.write(b"[mesh\0")
        b.buf.write(b"!")
        b.buf.write(bytes([4]) + b"name" + b"z" + b"\0\0\0\0")
        p = self.ws.root / "weird.mesh"
        p.write_bytes(b.bytes_())
        with self.assertRaises(ValueError):
            meshscan._parse_mesh(p)


class ScanDirTest(BaseTest):
    def test_records_errors_without_stopping(self):
        good = self.ws.mkdirs("gfx", "models") / "good.mesh"
        good.write_bytes(simple_mesh(verts=3))
        bad = self.ws.mkdirs("gfx", "units") / "bad.mesh"
        bad.write_bytes(b"nonsense")
        stats = meshscan.scan_dir(self.ws.modsrc)
        self.assertEqual(len(stats), 2)
        ok = [s for s in stats if s["ok"]]
        ko = [s for s in stats if not s["ok"]]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(ko), 1)
        self.assertIn("class", ok[0])

    def test_no_gfx_returns_empty(self):
        self.assertEqual(meshscan.scan_dir(self.ws.root), [])


class MediansTest(unittest.TestCase):
    def test_odd_and_even(self):
        stats = [
            {"ok": True, "class": "infantry", "verts": 100},
            {"ok": True, "class": "infantry", "verts": 200},
            {"ok": True, "class": "infantry", "verts": 300},
            {"ok": False, "class": "infantry", "verts": 9999},
        ]
        self.assertEqual(meshscan.class_medians(stats), {"infantry": 200})
        stats.append({"ok": True, "class": "infantry", "verts": 400})
        self.assertEqual(meshscan.class_medians(stats),
                         {"infantry": (200 + 300) // 2})


class VanillaBaselineTest(BaseTest):
    def test_cache_roundtrip(self):
        vanilla = self.ws.root / "van"
        (vanilla / "gfx").mkdir(parents=True)
        (vanilla / "gfx" / "m.mesh").write_bytes(simple_mesh(verts=6))
        cache = self.ws.root / "cache" / "v.json"
        res = meshscan.vanilla_baseline(vanilla, cache)
        self.assertEqual(res["files"], 1)
        again = meshscan.vanilla_baseline(vanilla, cache)
        self.assertEqual(again, res)

    def test_cache_invalidated_by_game_update(self):
        vanilla = self.ws.root / "van"
        (vanilla / "gfx").mkdir(parents=True)
        (vanilla / "gfx" / "m.mesh").write_bytes(simple_mesh(verts=6))
        cache = self.ws.root / "cache" / "v.json"
        first = meshscan.vanilla_baseline(vanilla, cache)
        (vanilla / "launcher-settings.json").write_text(
            '{"version": "9.9.9"}', encoding="utf-8")
        again = meshscan.vanilla_baseline(vanilla, cache)
        self.assertNotEqual(again["stamp"], first["stamp"])


class RunTest(BaseTest):
    def test_no_gfx_returns_2(self):
        self.assertEqual(meshscan.run(self.ws.modsrc, self.ws.build, None, True), 2)

    def test_run_writes_stats_and_report(self):
        (self.ws.mkdirs("gfx", "models") / "tank.mesh").write_bytes(
            simple_mesh(verts=12, tris=2, diff="models/tank_diff.dds"))
        rc = meshscan.run(self.ws.modsrc, self.ws.build, None, skip_vanilla=True)
        self.assertEqual(rc, 0)
        stats = json.loads((self.ws.build / "mesh_stats.json").read_text("utf-8"))
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["verts"], 12)
        self.assertEqual(stats[0]["detail"][0]["diff"], "models/tank_diff.dds")
        report = (self.ws.build / "mesh_report.txt").read_text("utf-8")
        self.assertIn("Meshes: 1 ok, 0 errors", report)
        self.assertIn("Top 25 by vertices", report)

    def test_run_reports_corrupt_mesh(self):
        (self.ws.mkdirs("gfx") / "bad.mesh").write_bytes(b"xxx")
        rc = meshscan.run(self.ws.modsrc, self.ws.build, None, skip_vanilla=True)
        self.assertEqual(rc, 0)
        report = (self.ws.build / "mesh_report.txt").read_text("utf-8")
        self.assertIn("Parse errors", report)


if __name__ == "__main__":
    unittest.main()
