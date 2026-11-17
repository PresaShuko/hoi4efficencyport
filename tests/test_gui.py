"""gui: CLI argv mapping, log severity, persisted settings, widget smoke test.

The window itself is only orchestration; every decision worth a regression
test lives in a pure function so it runs without a display.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import asdict
from pathlib import Path

from _harness import BaseTest


def _gui():
    """Import the GUI module or skip the test (tkinter is optional on Linux)."""
    try:
        from pipeline import fsutil, gui
    except ImportError as exc:
        raise unittest.SkipTest(f"tkinter unavailable: {exc}") from exc
    return fsutil, gui


def _tk_available() -> bool:
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:  # noqa: BLE001 - no display / no Tk build
        return False
    root.destroy()
    return True


class GuiBase(BaseTest):
    def setUp(self) -> None:
        self.fsutil, self.gui = _gui()
        super().setUp()


class StageCommandTest(GuiBase):
    def cmd(self, name: str, **kw) -> list[str]:
        o = self.gui.Opts(**{**asdict(self.gui.Opts(staging="stg", out="bld")), **kw})
        return self.gui.stage_command(name, o, prefix=["PY"])

    def test_sync_repeats_mod_in_load_order(self):
        got = self.cmd("sync", mods=("D:/mods/first", "D:/mods/second"))
        self.assertEqual(got[:2], ["PY", "sync"])
        mods = [got[i + 1] for i, a in enumerate(got) if a == "--mod"]
        self.assertEqual(mods, [str(Path("D:/mods/first").resolve()),
                                str(Path("D:/mods/second").resolve())])
        self.assertEqual(got[got.index("--staging") + 1], str(Path("stg").resolve()))
        self.assertEqual(got[got.index("--out") + 1], str(Path("bld").resolve()),
                         "the merge report belongs in the chosen output folder")
        self.assertNotIn("--apply", got)

    def test_sync_without_mods_passes_no_mod_flag(self):
        self.assertNotIn("--mod", self.cmd("sync"))

    def test_apply_only_reaches_stages_that_write(self):
        # sync mirrors the mod by definition and has no --apply switch.
        for name in self.gui.STAGE_NAMES:
            got = self.cmd(name, apply=True, mods=("m",))
            want = name in self.gui.WRITE and name != "sync"
            self.assertEqual("--apply" in got, want, name)

    def test_dry_run_never_passes_apply(self):
        for name in self.gui.STAGE_NAMES:
            self.assertNotIn("--apply", self.cmd(name, apply=False), name)

    def test_vanilla_flag_omitted_when_empty(self):
        self.assertNotIn("--vanilla", self.cmd("scan"))
        got = self.cmd("scan", vanilla="V:/game")
        self.assertEqual(got[got.index("--vanilla") + 1],
                         str(Path("V:/game").resolve()))

    def test_texconv_stages_carry_workers_and_only_texfix_carries_cap(self):
        conv = self.cmd("convert", workers="4", cap="2048")
        self.assertEqual(conv[conv.index("--workers") + 1], "4")
        self.assertNotIn("--cap", conv)
        fix = self.cmd("texfix", workers="4", cap="2048")
        self.assertEqual(fix[fix.index("--cap") + 1], "2048")
        self.assertNotIn("--cap", self.cmd("texfix", cap=""))

    def test_entityfix_cull_and_cutover_dedup(self):
        ef = self.cmd("entityfix", cull="250")
        self.assertEqual(ef[ef.index("--cull-radius") + 1], "250")
        ct = self.cmd("cutover", vanilla_dedup=True)
        self.assertIn("--vanilla-dedup", ct)
        self.assertNotIn("--vanilla-dedup", self.cmd("cutover", vanilla_dedup=False))

    def test_skip_vanilla_only_for_scanning_stages(self):
        for name in ("scan", "analyze", "meshscan"):
            self.assertIn("--skip-vanilla", self.cmd(name, skip_vanilla=True), name)
        for name in ("convert", "cutover", "validate", "entityfix", "normalize"):
            self.assertNotIn("--skip-vanilla", self.cmd(name), name)

    def test_normalize_no_moves(self):
        self.assertIn("--no-moves", self.cmd("normalize", no_moves=True))

    def test_check_log_tail(self):
        got = self.cmd("check-log", tail="1500")
        self.assertEqual(got[1], "check-log")
        self.assertEqual(got[got.index("--tail") + 1], "1500")

    def test_unknown_stage_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            self.cmd("nope")

    def test_prefix_default_is_python_module_invocation(self):
        got = self.gui.stage_command("scan", self.gui.Opts(staging="s", out="o"))
        self.assertEqual(got[:3], [self.gui.sys.executable, "-m", "hoi4port"])

    def test_frozen_prefix_reinvokes_the_executable(self):
        self.assertEqual(self.gui.cli_prefix(), [self.gui.sys.executable, "-m", "hoi4port"])
        saved = getattr(self.gui.sys, "frozen", None)
        self.gui.sys.frozen = True  # type: ignore[attr-defined]
        try:
            self.assertEqual(self.gui.cli_prefix(),
                             [self.gui.sys.executable, "--cli"])
        finally:
            if saved is None:
                del self.gui.sys.frozen  # type: ignore[attr-defined]
            else:
                self.gui.sys.frozen = saved  # type: ignore[attr-defined]


class ParserContractTest(GuiBase):
    """Every argv the window can build must be accepted by the real CLI."""

    def test_every_stage_argv_parses(self):
        import hoi4port

        parser = hoi4port.build_parser()
        o = self.gui.Opts(mods=("D:/mods/m", "D:/mods/n"), staging="stg", out="bld",
                          vanilla="V:/game", apply=True, workers="4", cap="2048",
                          cull="250", skip_vanilla=True, vanilla_dedup=True,
                          no_moves=True, tail="1500")
        for name in (*self.gui.STAGE_NAMES, "check-log"):
            argv = self.gui.stage_command(name, o, prefix=["PY"])[1:]
            with self.subTest(stage=name):
                ns = parser.parse_args(argv)  # raises SystemExit on bad flags
                self.assertEqual(ns.cmd, name)

    def test_minimal_argv_parses(self):
        import hoi4port

        parser = hoi4port.build_parser()
        o = self.gui.Opts(staging="stg", out="bld")
        for name in (*self.gui.STAGE_NAMES, "check-log"):
            with self.subTest(stage=name):
                parser.parse_args(self.gui.stage_command(name, o, prefix=["PY"])[1:])


class MachineTest(GuiBase):
    def test_worker_recommendation_is_floored_and_capped(self):
        for threads, want in ((0, 2), (1, 2), (2, 2), (4, 4), (12, 12),
                              (16, 16), (32, 16), (128, 16)):
            self.assertEqual(self.gui.recommend_workers(threads), want, threads)

    def profile(self, **kw):
        base = dict(threads=12, ram_gb=31.8, free_gb=210.0,
                    staging_drive="D:", vanilla="V:/game")
        return self.gui.SystemProfile(**{**base, **kw})

    def test_plan_sets_workers_from_threads_and_keeps_vanilla(self):
        plan = self.gui.plan_auto(self.profile())
        self.assertEqual(plan.workers, "12")
        self.assertEqual(plan.vanilla, "V:/game")
        self.assertTrue(any("12 logical threads" in n for n in plan.notes))
        self.assertEqual(plan.cap, "", "cap must stay automatic")
        self.assertEqual(plan.cull, "100")
        self.assertTrue(any("210 GB" in n for n in plan.notes))

    def test_plan_warns_about_low_disk_space(self):
        plan = self.gui.plan_auto(self.profile(free_gb=3.2))
        self.assertTrue(any("Less than 5 GB" in n for n in plan.notes), plan.notes)
        ok = self.gui.plan_auto(self.profile())
        self.assertFalse(any("Less than 5 GB" in n for n in ok.notes), ok.notes)

    def test_plan_reports_missing_vanilla(self):
        plan = self.gui.plan_auto(self.profile(vanilla=""))
        self.assertEqual(plan.vanilla, "")
        self.assertTrue(any("not found" in n for n in plan.notes), plan.notes)

    def test_plan_survives_unknown_ram_and_disk(self):
        plan = self.gui.plan_auto(self.profile(ram_gb=0.0, free_gb=0.0,
                                               staging_drive=""))
        self.assertEqual(plan.workers, "12")
        self.assertFalse(any("GB RAM" in n for n in plan.notes), plan.notes)


class TooltipCoverageTest(GuiBase):
    """Every control the user can touch must explain itself."""

    REQUIRED = {"mods", "add_mod", "remove_mod", "up", "down", "staging", "out",
                "vanilla", "browse", "detect", "apply", "workers", "cap", "cull",
                "skip_vanilla", "vanilla_dedup", "no_moves", "continue", "run",
                "cancel", "open_out", "diag", "copy", "clear", "log", "bar",
                "auto", "pc"}

    def test_every_control_has_an_explanation(self):
        missing = self.REQUIRED - set(self.gui.TOOLTIPS)
        self.assertFalse(missing, f"tooltip mancante per: {sorted(missing)}")
        for key, text in self.gui.TOOLTIPS.items():
            self.assertGreaterEqual(len(text), 40, f"tooltip troppo corto: {key}")

    def test_every_stage_has_an_explanation(self):
        self.assertEqual(set(self.gui.STAGE_HELP), set(self.gui.STAGE_NAMES))
        for name, text in self.gui.STAGE_HELP.items():
            self.assertGreaterEqual(len(text), 60, f"spiegazione corta: {name}")
            if name in self.gui.WRITE:
                self.assertNotIn("Solo lettura", text, name)


class ClassifyTest(GuiBase):
    def test_zero_counts_are_not_problems(self):
        for line in ("Fine: 0.3 s \u2014 0 errori, 0 avvisi.",
                     "Unique asset paths: 2 \u2014 missing: 0 (true: 0)",
                     "Duplicates: 0 groups \u2014 unused: 0",
                     "Missing GFX_*: 0 (static: 0)"):
            self.assertEqual(self.gui.classify(line), "info", line)

    def test_nonzero_counts_and_tracebacks_are_errors(self):
        for line in ("Fine: 1.2 s \u2014 2 errori, 0 avvisi.",
                     "  \u2717 convert     0.40 s   (exit 1)",
                     "Traceback (most recent call last):",
                     "Conversion failed: 3"):
            self.assertEqual(self.gui.classify(line), "err", line)

    def test_warnings(self):
        for line in ("WARNING \u2014 2 unreadable files",
                     "Unique asset paths: 2 \u2014 missing: 41"):
            self.assertEqual(self.gui.classify(line), "warn", line)


class HelpersTest(GuiBase):
    def test_short_path_keeps_the_informative_tail(self):
        self.assertEqual(self.gui.short_path(Path("C:/a/b/c/mod_tfr")),
                         "\u2026\\b\\c\\mod_tfr")
        self.assertEqual(self.gui.short_path(Path("mod_tfr")), "mod_tfr")

    def test_clamp_int(self):
        self.assertEqual(self.gui.clamp_int("8", 12, 1, 32), 8)
        self.assertEqual(self.gui.clamp_int("999", 12, 1, 32), 32)
        self.assertEqual(self.gui.clamp_int("zero", 12, 1, 32), 12)

    def test_cache_dir_is_the_shared_hoi4port_directory(self):
        self.assertEqual(self.fsutil.cache_dir().name, "hoi4port")


class SettingsTest(GuiBase):
    def test_roundtrip(self):
        target = self.ws.root / "self.gui.json"
        src = self.gui.Settings(mods=["D:/m", "D:/n"], staging="s", apply=True,
                           workers="6", auto_workers=True,
                           stages=["sync", "validate"], geometry="900x700+10+10")
        src.save(target)
        got = self.gui.Settings.load(target)
        self.assertEqual(got.mods, ["D:/m", "D:/n"])
        self.assertTrue(got.auto_workers)
        self.assertEqual(got.workers, "6")
        self.assertTrue(got.apply)
        self.assertEqual(got.stages, ["sync", "validate"])
        self.assertEqual(got.geometry, "900x700+10+10")

    def test_missing_and_corrupt_files_fall_back_to_defaults(self):
        self.assertEqual(self.gui.Settings.load(self.ws.root / "nope.json").staging,
                         "mod_tfr")
        bad = self.ws.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.gui.Settings.load(bad).out, "build")
        arr = self.ws.root / "arr.json"
        arr.write_text(json.dumps([1, 2]), encoding="utf-8")
        self.assertEqual(self.gui.Settings.load(arr).out, "build")

    def test_unknown_keys_and_bad_types_are_ignored(self):
        target = self.ws.root / "self.gui.json"
        target.write_text(json.dumps({"mods": 5, "apply": "yes", "stages":
                                      ["sync", "bogus"], "extra": 1}),
                          encoding="utf-8")
        got = self.gui.Settings.load(target)
        self.assertEqual(got.mods, [])
        self.assertFalse(got.apply)
        self.assertEqual(got.stages, ["sync"])

    def test_default_settings_target_the_cache_dir(self):
        self.assertEqual(self.gui.Settings.path().name, "gui.json")
        self.assertEqual(self.gui.Settings.path().parent, self.fsutil.cache_dir())


class AppSmokeTest(GuiBase):
    @unittest.skipUnless(_tk_available(), "no display for Tk")
    def test_auto_applies_the_machine_profile(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            app = self.gui.App(root, self.gui.Settings(staging="s", out="o"))
            app.v_van.set("")
            app.auto()
            self.assertTrue(app.pc_txt.cget("text"))
            self.assertIn("thread", app.pc_txt.cget("text"))
            self.assertEqual(app.v_workers.get(),
                             str(self.gui.recommend_workers(self.gui.os.cpu_count() or 0)))
        finally:
            root.destroy()

    @unittest.skipUnless(_tk_available(), "no display for Tk")
    def test_builds_the_window_with_ten_stages(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            app = self.gui.App(root, self.gui.Settings(staging="s", out="o"))
            self.assertEqual(tuple(app.rows), self.gui.STAGE_NAMES)
            self.assertTrue(app.finished)
            self.assertEqual(app.btn_run["state"], "normal")
            app.txt.configure(state="normal")
            app.txt.delete("1.0", "end")
            app.txt.configure(state="disabled")
        finally:
            root.destroy()
