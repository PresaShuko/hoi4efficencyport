"""fsutil + tools: encoding roundtrips, iterators, hashing, staging sync, texconv resolution."""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import unittest
from unittest import mock

from _harness import BaseTest, REPO
from pipeline import fsutil
from pipeline import tools


class ReadTextTest(BaseTest):
    def test_utf8_roundtrip(self):
        p = self.ws.root / "a.txt"
        p.write_text("hello àèì", encoding="utf-8")
        text, enc = fsutil.read_text(p)
        self.assertEqual(text, "hello àèì")
        self.assertEqual(enc, "utf-8")

    def test_bom_detected_and_preserved(self):
        p = self.ws.root / "b.txt"
        p.write_bytes(b"\xef\xbb\xbfname = \"x\"\n")
        text, enc = fsutil.read_text(p)
        self.assertEqual(enc, "utf-8-sig")
        self.assertTrue(text.startswith('name = "x"'))  # BOM consumed on read
        fsutil.write_text(p, text, enc)
        self.assertTrue(p.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_bomless_file_stays_bomless(self):
        p = self.ws.root / "c.txt"
        p.write_bytes(b'name = "x"\n')
        text, enc = fsutil.read_text(p)
        self.assertEqual(enc, "utf-8")
        fsutil.write_text(p, text, enc)
        self.assertFalse(p.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_cp1252(self):
        p = self.ws.root / "d.txt"
        p.write_bytes(b"caf\xe9")          # é in cp1252, invalid utf-8
        text, enc = fsutil.read_text(p)
        self.assertEqual(text, "café")
        self.assertEqual(enc, "cp1252")
        fsutil.write_text(p, text, enc)
        self.assertEqual(p.read_bytes(), b"caf\xe9")

    def test_latin1_fallback_for_undefined_cp1252_bytes(self):
        p = self.ws.root / "e.txt"
        p.write_bytes(b"\x81\x8f")         # undefined in cp1252, valid latin-1
        text, enc = fsutil.read_text(p)
        self.assertEqual(enc, "latin-1")
        self.assertEqual(text, "\x81\x8f")

    def test_write_text_preserves_lf(self):
        p = self.ws.root / "f.txt"
        fsutil.write_text(p, "a\nb\n", "utf-8")
        self.assertEqual(p.read_bytes(), b"a\nb\n")


class IteratorsTest(BaseTest):
    def test_iter_text_files_case_insensitive(self):
        for name in ("a.TXT", "b.Gfx", "c.Lua", "d.txt", "e.png", "f.gfx2"):
            self.ws.write(name, "x")
        names = sorted(p.name for p in fsutil.iter_text_files(self.ws.root))
        self.assertEqual(names, ["a.TXT", "b.Gfx", "c.Lua", "d.txt"])

    def test_iter_asset_files(self):
        (self.ws.root / "gfx").mkdir()
        self.ws.make_png("gfx/a.png")
        (self.ws.root / "gfx/b.tga").write_bytes(b"\0")
        (self.ws.root / "gfx/c.txt").write_text("x", encoding="utf-8")
        names = sorted(p.name for p in fsutil.iter_asset_files(self.ws.root))
        self.assertEqual(names, ["a.png", "b.tga"])


class ShaTest(BaseTest):
    def test_sha256_known_vector(self):
        p = self.ws.root / "h.bin"
        p.write_bytes(b"abc")
        self.assertEqual(
            fsutil.sha256_file(p),
            hashlib.sha256(b"abc").hexdigest())


class TreeSizeTest(BaseTest):
    def test_matches_rglob_sum(self):
        self.ws.write("a.txt", "x")
        self.ws.make_png("gfx/b.png")
        self.ws.make_raw_dds("gfx/c.dds")
        want = sum(p.stat().st_size for p in self.ws.modsrc.rglob("*")
                   if p.is_file())
        self.assertEqual(fsutil.tree_size(self.ws.modsrc), want)
        self.assertEqual(fsutil.tree_size(self.ws.root / "nope"), 0)


class SyncStagingTest(BaseTest):
    def test_missing_source_exits(self):
        with self.assertRaises(SystemExit):
            fsutil.sync_staging(self.ws.root / "nope", self.ws.staging)

    def test_mirrors_and_prunes(self):
        src = self.ws.root / "src"
        (src / "gfx").mkdir(parents=True)
        (src / "gfx" / "a.png").write_bytes(b"x")
        dst = self.ws.root / "dst"
        fsutil.sync_staging(src, dst)
        self.assertTrue((dst / "gfx" / "a.png").is_file())
        # extra file in dst must be removed by the mirror
        (dst / "stale.txt").write_text("x", encoding="utf-8")
        fsutil.sync_staging(src, dst)
        self.assertFalse((dst / "stale.txt").exists())
        self.assertTrue((dst / "gfx" / "a.png").is_file())

    def test_robocopy_summary_decodes_without_replacement_chars(self):
        """robocopy speaks the OEM code page; decoding as UTF-8 turned the
        localized summary into 'Velocit\uFFFD'. Any U+FFFD here is a regression."""
        if not shutil.which("robocopy"):
            self.skipTest("robocopy is Windows-only")
        src = self.ws.root / "src_enc"
        (src / "citt\u00e0").mkdir(parents=True)
        (src / "citt\u00e0" / "a.png").write_bytes(b"x")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fsutil.sync_staging(src, self.ws.root / "dst_enc")
        text = out.getvalue()
        self.assertTrue(text.strip())
        self.assertNotIn("\uFFFD", text)


class MergeStagingTest(BaseTest):
    def mods(self, *specs):
        roots = []
        for name, files in specs:
            root = self.ws.root / name
            for rel, data in files.items():
                f = root / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data if isinstance(data, bytes)
                              else data.encode("utf-8"))
            roots.append(root)
        return roots

    def test_later_mod_overrides_and_nothing_is_lost(self):
        a, b = self.mods(("a", {"gfx/a.png": b"1", "shared.txt": "old"}),
                         ("b", {"gfx/b.png": b"2", "shared.txt": "new"}))
        report = fsutil.merge_staging([a, b], self.ws.staging)
        self.assertEqual((self.ws.staging / "shared.txt").read_text(encoding="utf-8"),
                         "new")
        self.assertTrue((self.ws.staging / "gfx" / "a.png").is_file())
        self.assertTrue((self.ws.staging / "gfx" / "b.png").is_file())
        self.assertEqual(report["overridden"][str(b)], ["shared.txt"])
        self.assertNotIn(str(a), report["overridden"], "first mod overrides nothing")
        self.assertEqual(report["files"], 3)

    def test_single_source_prunes_stale_files(self):
        a = self.mods(("a", {"keep.txt": "x"}))[0]
        self.ws.staging.mkdir(parents=True, exist_ok=True)
        (self.ws.staging / "stale.txt").write_text("gone", encoding="utf-8")
        fsutil.merge_staging([a], self.ws.staging)
        self.assertTrue((self.ws.staging / "keep.txt").is_file())
        self.assertFalse((self.ws.staging / "stale.txt").exists())

    def test_later_mod_never_deletes(self):
        a, b = self.mods(("a", {"only_in_a.txt": "x", "gfx/s.png": b"1"}),
                         ("b", {"gfx/s.png": b"2"}))
        fsutil.merge_staging([a, b], self.ws.staging)
        self.assertTrue((self.ws.staging / "only_in_a.txt").is_file(),
                        "a later mod must not purge earlier files")
        self.assertEqual((self.ws.staging / "gfx" / "s.png").read_bytes(), b"2")

    def test_missing_source_exits(self):
        with self.assertRaises(SystemExit):
            fsutil.merge_staging([self.ws.root / "nope"], self.ws.staging)
        with self.assertRaises(SystemExit):
            fsutil.merge_staging([], self.ws.staging)

    def test_sync_staging_accepts_one_path_or_a_list(self):
        a, b = self.mods(("a", {"x.txt": "1"}), ("b", {"y.txt": "2"}))
        fsutil.sync_staging(a, self.ws.staging)
        self.assertTrue((self.ws.staging / "x.txt").is_file())
        fsutil.sync_staging([a, b], self.ws.root / "stg2")
        self.assertTrue((self.ws.root / "stg2" / "y.txt").is_file())

    def test_files_without_extension_are_merged(self):
        a = self.mods(("a", {"LICENSE": "text", "descriptor.mod": 'name="m"'}))[0]
        report = fsutil.merge_staging([a], self.ws.staging)
        self.assertEqual(report["files"], 2)
        self.assertTrue((self.ws.staging / "LICENSE").is_file())


class TexconvTest(unittest.TestCase):
    def test_bundled_binary_exists(self):
        p = tools._bundled()
        self.assertEqual(p.name, "texconv.exe")

    def test_env_missing_file_exits(self):
        with mock.patch.dict(os.environ, {tools.ENV_TEXCONV: "Z:/definitely/missing.exe"}):
            with self.assertRaises(SystemExit):
                tools.texconv_path()

    def test_env_valid_file_wins(self):
        cand = REPO / "pipeline" / "bin" / "texconv.exe"
        if not cand.is_file():
            self.skipTest("bundled texconv not present")
        with mock.patch.dict(os.environ, {tools.ENV_TEXCONV: str(cand)}):
            self.assertEqual(tools.texconv_path(), cand.resolve())

    def test_wine_skipped_with_no_wine_flag(self):
        cmd = tools.texconv_command(no_wine=True)
        self.assertEqual(len(cmd), 1)
        self.assertTrue(cmd[0].endswith("texconv.exe"))

    def test_wine_skipped_with_env(self):
        with mock.patch.dict(os.environ, {tools.ENV_NO_WINE: "1"}):
            cmd = tools.texconv_command()
            self.assertEqual(len(cmd), 1)


if __name__ == "__main__":
    unittest.main()
