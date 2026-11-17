"""config: CLI > env > toml precedence, defaults, Steam resolution error paths."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from _harness import BaseTest, clean_env
from pipeline import config


class PrecedenceTest(BaseTest):
    def setUp(self):
        super().setUp()
        # hermetic: no real Steam libraries/candidates, no real APPDATA toml
        self._patches = [
            mock.patch.object(config, "workshop_candidates", lambda: []),
            mock.patch.object(config, "_steam_libraries", lambda: []),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _load(self, **kw):
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)):
            os.chdir(self.ws.root)
            try:
                return config.load_config(**kw)
            finally:
                os.chdir(Path(__file__).parent.parent)

    def test_defaults_are_relative_to_cwd(self):
        cfg = self._load()
        self.assertEqual(cfg.staging, self.ws.root / "mod_tfr")
        self.assertEqual(cfg.out, self.ws.root / "build")
        self.assertIsNone(cfg.workshop_mod)
        self.assertIsNone(cfg.vanilla)

    def test_env_overrides_defaults(self):
        env = clean_env({
            "HOI4PORT_STAGING": str(self.ws.root / "s"),
            "HOI4PORT_OUT": str(self.ws.root / "o"),
            "HOI4PORT_MOD": str(self.ws.modsrc),
        }, appdata=self.ws.root)
        self.ws.mkdirs()
        with mock.patch.dict(os.environ, env):
            os.chdir(self.ws.root)
            try:
                cfg = config.load_config()
            finally:
                os.chdir(Path(__file__).parent.parent)
            self.assertEqual(cfg.staging, Path(self.ws.root / "s").resolve())
            self.assertEqual(cfg.out, Path(self.ws.root / "o").resolve())
            self.assertEqual(cfg.workshop_mod, self.ws.modsrc.resolve())

    def test_empty_env_var_falls_through(self):
        env = clean_env({"HOI4PORT_STAGING": ""}, appdata=self.ws.root)
        with mock.patch.dict(os.environ, env):
            os.chdir(self.ws.root)
            try:
                cfg = config.load_config(staging=str(self.ws.root / "cli_s"))
            finally:
                os.chdir(Path(__file__).parent.parent)
            self.assertEqual(cfg.staging, Path(self.ws.root / "cli_s").resolve())

    def test_cli_beats_env(self):
        env = clean_env({"HOI4PORT_OUT": str(self.ws.root / "env_out")},
                          appdata=self.ws.root)
        with mock.patch.dict(os.environ, env):
            os.chdir(self.ws.root)
            try:
                cfg = config.load_config(out=str(self.ws.root / "cli_out"))
            finally:
                os.chdir(Path(__file__).parent.parent)
            self.assertEqual(cfg.out, Path(self.ws.root / "cli_out").resolve())

    def test_toml_used_when_no_cli_env(self):
        (self.ws.root / "hoi4port.toml").write_text(
            'staging = "toml_s"\nout = "toml_o"\nmod = 12345\n', encoding="utf-8")
        cand = self.ws.root / "cand" / "12345"
        cand.mkdir(parents=True)
        with mock.patch.object(config, "workshop_candidates",
                               lambda: [cand]):
            cfg = self._load()
        self.assertEqual(cfg.staging, self.ws.root / "toml_s")
        self.assertEqual(cfg.out, self.ws.root / "toml_o")
        # int values are stringified and matched against workshop candidates
        self.assertEqual(cfg.workshop_mod, cand.resolve())

    def test_invalid_toml_exits(self):
        (self.ws.root / "hoi4port.toml").write_text("not [valid toml ===",
                                                    encoding="utf-8")
        with self.assertRaises(SystemExit):
            self._load()
    def test_bool_toml_value_ignored(self):
        # BUG-08: booleans pass the isinstance(int) filter and become "True",
        # so `staging = true` silently resolves to a folder named "True".
        (self.ws.root / "hoi4port.toml").write_text('staging = true\n',
                                                    encoding="utf-8")
        cfg = self._load()
        self.assertEqual(cfg.staging, self.ws.root / "mod_tfr",
                         "BUG-08: boolean toml value must be ignored")


class ResolveWorkshopTest(BaseTest):
    def test_direct_path(self):
        self.ws.mkdirs()
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)):
            p = config.resolve_workshop(str(self.ws.modsrc))
            self.assertEqual(p, self.ws.modsrc.resolve())

    def test_numeric_missing_id_exits_listing_libraries(self):
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)), \
                mock.patch.object(config, "workshop_candidates", lambda: []), \
                mock.patch.object(config, "_steam_libraries", lambda: [Path("C:/steam")]):
            with self.assertRaises(SystemExit) as cm:
                config.resolve_workshop("000000000")
            self.assertIn("000000000", str(cm.exception))

    def test_garbage_value_exits(self):
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)):
            with self.assertRaises(SystemExit) as cm:
                config.resolve_workshop("not-a-path-or-id")
            self.assertIn("unrecognized", str(cm.exception))

    def test_ambiguous_autodetect_returns_none(self):
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)), \
                mock.patch.object(config, "workshop_candidates",
                                  lambda: [Path("C:/m/1"), Path("C:/m/2")]):
            self.assertIsNone(config.resolve_workshop(None))

    def test_single_autodetect_picked(self):
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)), \
                mock.patch.object(config, "workshop_candidates",
                                  lambda: [Path("C:/m/111")]):
            self.assertEqual(config.resolve_workshop(None), Path("C:/m/111").resolve())


class ResolveModsTest(BaseTest):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(config, "workshop_candidates", lambda: []),
                  mock.patch.object(config, "_steam_libraries", lambda: [])):
            p.start()
            self.addCleanup(p.stop)

    def dirs(self, *names):
        out = []
        for n in names:
            d = self.ws.root / n
            d.mkdir(parents=True, exist_ok=True)
            out.append(d.resolve())
        return out

    def test_cli_list_keeps_load_order(self):
        a, b = self.dirs("a", "b")
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)):
            self.assertEqual(config.resolve_mods([str(a), str(b)]), (a, b))

    def test_env_list_is_split_on_pathsep(self):
        a, b = self.dirs("a", "b")
        env = clean_env({"HOI4PORT_MOD": f"{a}{os.pathsep}{b}"},
                        appdata=self.ws.root)
        with mock.patch.dict(os.environ, env):
            self.assertEqual(config.resolve_mods(None), (a, b))

    def test_cli_wins_over_env(self):
        a, b = self.dirs("a", "b")
        env = clean_env({"HOI4PORT_MOD": str(b)}, appdata=self.ws.root)
        with mock.patch.dict(os.environ, env):
            self.assertEqual(config.resolve_mods([str(a)]), (a,))

    def test_toml_array_of_mods(self):
        a, b = self.dirs("a", "b")
        (self.ws.root / "hoi4port.toml").write_text(
            f'mods = ["{a.as_posix()}", "{b.as_posix()}"]\n', encoding="utf-8")
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)):
            os.chdir(self.ws.root)
            try:
                self.assertEqual(config.resolve_mods(None), (a, b))
            finally:
                os.chdir(Path(__file__).parent.parent)

    def test_nothing_configured_and_ambiguous_is_empty(self):
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)), \
                mock.patch.object(config, "workshop_candidates",
                                  lambda: [Path("C:/m/1"), Path("C:/m/2")]):
            self.assertEqual(config.resolve_mods(None), ())


class ResolveVanillaTest(BaseTest):
    def test_missing_explicit_vanilla_warns_and_returns_none(self):
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)):
            self.assertIsNone(config.resolve_vanilla(str(self.ws.root / "novanilla")))

    def test_explicit_vanilla_dir(self):
        v = self.ws.mkdirs("vanilla_install")
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)):
            self.assertEqual(config.resolve_vanilla(str(v)), v.resolve())

    def test_autodetect_from_steam_library(self):
        libroot = self.ws.root / "steamlib"
        cand = libroot / "steamapps" / "common" / config.VANILLA_DIRNAME
        cand.mkdir(parents=True)
        with mock.patch.dict(os.environ, clean_env(appdata=self.ws.root)), \
                mock.patch.object(config, "_steam_libraries", lambda: [libroot]):
            self.assertEqual(config.resolve_vanilla(None), cand.resolve())


if __name__ == "__main__":
    unittest.main()
