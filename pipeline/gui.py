"""hoi4port GUI — Tkinter front-end for the pipeline.

The window only orchestrates the CLI: every stage runs as a child process
(``python -m hoi4port`` in a checkout, ``hoi4port.exe --cli`` in the frozen
build), so a crash in a stage cannot take the UI down and "Annulla" can kill
the whole process tree.

No third-party dependency: Tkinter ships with Python and with the executable.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox

from pipeline import __version__, fsutil

# --- design tokens (dark slate + run green; AA contrast on every text tone) ---
C = {
    "bg": "#0F172A", "header": "#111A2E", "card": "#1B2336", "field": "#0B1220",
    "border": "#2A3550", "border_hi": "#475569", "fg": "#F8FAFC",
    "muted": "#94A3B8", "dim": "#8494AE", "faint": "#5B6B85",
    "accent": "#22C55E", "accent_hi": "#16A34A", "accent_fg": "#0F172A",
    "warn": "#F59E0B", "err": "#F87171", "focus": "#38BDF8", "hover": "#232F4A",
    "ts": "#7C8CA8", "disabled": "#1A2437",
}
UI = ("Segoe UI", 9)
UI_SM = ("Segoe UI", 8)
UI_B = ("Segoe UI Semibold", 10)
UI_T = ("Segoe UI Semibold", 12)
MONO = ("Consolas", 9)

# (name, label, one-line help, writes files?)
STAGES = (
    ("sync", "Sync", "mods → staging (merged)", "write"),
    ("scan", "Scan", "references, GFX, baseline", "read"),
    ("normalize", "Normalize", "folder layout, ref repair", "write"),
    ("analyze", "Analyze", "alpha, cap, dedup, unused", "read"),
    ("convert", "Convert", "PNG/TGA → DDS BC1/BC3", "write"),
    ("cutover", "Cutover", "rewrite refs, drop originals", "write"),
    ("meshscan", "Meshscan", "vertices, materials, collision", "read"),
    ("texfix", "Texfix", "mip chain + DXT1/5 models", "write"),
    ("entityfix", "Entityfix", "cull_radius for ambient", "write"),
    ("validate", "Validate", "DDS, references, sizes", "read"),
)
WRITE = frozenset(n for n, _, _, m in STAGES if m == "write")
STAGE_NAMES = tuple(n for n, *_ in STAGES)
# Stages whose parser takes --vanilla (convert/texfix resolve no vanilla data).
VANILLA_STAGES = frozenset({"scan", "normalize", "analyze", "cutover",
                            "meshscan", "entityfix", "validate"})


# --- pure helpers (unit-tested without a display) ---------------------------
@dataclass(frozen=True)
class Opts:
    """Everything the CLI needs for one run, as typed in the window."""

    mods: tuple[str, ...] = ()
    staging: str = "mod_tfr"
    out: str = "build"
    vanilla: str = ""
    apply: bool = False
    workers: str = "12"
    cap: str = ""
    cull: str = "100"
    auto_workers: bool = False
    auto_cap: bool = True
    auto_cull: bool = False
    skip_vanilla: bool = False
    vanilla_dedup: bool = False
    no_moves: bool = False
    continue_on_error: bool = False
    descriptor: bool = True
    tail: str = "5000"


def cli_prefix() -> list[str]:
    """How to invoke the pipeline from this process.

    A frozen GUI is ``hoi4port-gui.exe``: ``sys.executable -m hoi4port`` would
    not work, so the executable re-invokes itself with the hidden ``--cli``
    switch handled in ``hoi4port.main``.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--cli"]
    return [sys.executable, "-m", "hoi4port"]


def abs_path(raw: str) -> str:
    raw = raw.strip()
    return str(Path(raw).resolve()) if raw else raw


def stage_command(name: str, o: Opts, prefix: list[str] | None = None) -> list[str]:
    """argv for one stage. Unknown stage names raise (programming error)."""
    if name not in STAGE_NAMES and name != "check-log":
        raise ValueError(f"unknown stage: {name}")
    cmd = [*(prefix if prefix is not None else cli_prefix()), name]
    if name == "check-log":
        return cmd + ["--tail", o.tail or "5000"]
    stg, out, van = abs_path(o.staging), abs_path(o.out), abs_path(o.vanilla)
    if name == "sync":
        for mod in o.mods:
            cmd += ["--mod", abs_path(mod)]
        # --out receives merge_report.json when more than one mod is merged.
        cmd += ["--staging", stg, "--out", out]
        if not o.descriptor:
            cmd += ["--no-descriptor"]
        return cmd
    cmd += ["--staging", stg, "--out", out]
    if van and name in VANILLA_STAGES:
        cmd += ["--vanilla", van]
    if name in ("scan", "analyze", "meshscan") and o.skip_vanilla:
        cmd += ["--skip-vanilla"]
    if name in WRITE and o.apply:
        cmd += ["--apply"]
    if name == "normalize" and o.no_moves:
        cmd += ["--no-moves"]
    if name in ("convert", "texfix"):
        cmd += ["--workers", o.workers or "12"]
    if name == "texfix" and o.cap.strip():
        cmd += ["--cap", o.cap.strip()]
    if name == "cutover" and o.vanilla_dedup:
        cmd += ["--vanilla-dedup"]
    if name == "entityfix":
        cmd += ["--cull-radius", o.cull or "100"]
    return cmd


BAD_ANY = re.compile(r"(?i)\btraceback\b")
_BAD = r"(?:errori?|errors?|failed|failures?|falliti)"
BAD_COUNT = re.compile(rf"(?i)\b{_BAD}\b\s*:?\s*[1-9]\d*|"
                       rf"\b[1-9]\d*\s+{_BAD}\b")
WARN_ANY = re.compile(r"(?i)\bwarn(ing)?\b")
_WARN = r"(?:missing|duplicat\w*|skipped|saltat\w*|avvis[oi])"
WARN_COUNT = re.compile(rf"(?i)\b{_WARN}\b\s*:?\s*[1-9]\d*|"
                        rf"\b[1-9]\d*\s+{_WARN}\b")


def classify(line: str) -> str:
    """Log severity of one pipeline line (a '0 errors' summary is not an error)."""
    if BAD_ANY.search(line) or BAD_COUNT.search(line) or line.lstrip().startswith("✗"):
        return "err"
    if WARN_ANY.search(line) or WARN_COUNT.search(line):
        return "warn"
    return "info"


def short_path(path: Path, keep: int = 3) -> str:
    """'C:\\a\\b\\mod_tfr' -> '…\\b\\mod_tfr' (keeps the informative tail)."""
    parts = [p for p in path.parts if p not in ("\\", "/")]
    tail = "\\".join(parts[-keep:])
    return ("…\\" if len(parts) > keep else "") + tail


def clamp_int(raw: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return max(lo, min(hi, value))



# --- machine detection (pure decision, thin syscall layer) -------------------
@dataclass(frozen=True)
class SystemProfile:
    """What the GUI knows about this PC. No I/O happens after construction."""

    threads: int = 0
    ram_gb: float = 0.0
    free_gb: float = 0.0
    staging_drive: str = ""
    vanilla: str = ""


@dataclass(frozen=True)
class AutoPlan:
    workers: str
    cap: str
    cull: str
    vanilla: str
    notes: tuple[str, ...]


DEFAULT_CULL = "100"


def recommend_workers(threads: int) -> int:
    """Parallel texconv jobs: one per logical CPU, floored at 2, capped at 16.

    texconv saturates the GPU, so past the core count the extra processes only
    add memory pressure and scheduling noise; below 2 a GB-sized mod crawls.
    """
    return max(2, min(16, int(threads or 0)))


def plan_auto(profile: SystemProfile) -> AutoPlan:
    """Settings this machine should use, plus one human sentence per decision."""
    notes: list[str] = []
    workers = recommend_workers(profile.threads)
    notes.append(f"CPU: {profile.threads} logical threads → texconv workers {workers}")
    if profile.ram_gb:
        notes.append(f"RAM: {profile.ram_gb:.1f} GB")
    if profile.free_gb and profile.staging_drive:
        notes.append(f"Free space on {profile.staging_drive} — "
                     f"{profile.free_gb:.0f} GB (staging is a full copy)")
        if profile.free_gb < 5:
            notes.append("Less than 5 GB free: the merged copy may not fit.")
    notes.append("Resolution cap: auto from the .gui sizes (no fixed limit)")
    notes.append(f"cull_radius: {DEFAULT_CULL} (CLI default)")
    notes.append(f"Vanilla: {profile.vanilla}" if profile.vanilla else
                 "Vanilla: not found, set the path by hand")
    return AutoPlan(workers=str(workers), cap="", cull=DEFAULT_CULL,
                    vanilla=profile.vanilla, notes=tuple(notes))


def _ram_gb() -> float:
    if os.name != "nt":
        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
        except (ValueError, OSError):
            return 0.0
    try:
        import ctypes

        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32),
                        ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64),
                        ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64),
                        ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64),
                        ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]

        status = MemStatus()
        status.dwLength = ctypes.sizeof(MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / 2**30
    except Exception:  # noqa: BLE001 - detection is best effort
        pass
    return 0.0


def _free_gb(path: Path) -> tuple[float, str]:
    probe = path.expanduser().resolve()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return 0.0, ""
    drive = (Path(probe).anchor or str(probe)).rstrip("\\/") or str(probe)
    return usage.free / 2**30, drive


def system_profile(staging: str, vanilla: str = "") -> SystemProfile:
    """Read CPU/RAM/disk once; the vanilla install is autodetected if unknown."""
    resolved_vanilla = vanilla.strip()
    if not resolved_vanilla:
        try:
            from pipeline.config import resolve_vanilla

            found = resolve_vanilla(None)
            resolved_vanilla = str(found) if found else ""
        except Exception:  # noqa: BLE001 - config is optional here
            resolved_vanilla = ""
    free, drive = _free_gb(Path(staging or ".").expanduser())
    return SystemProfile(threads=os.cpu_count() or 0, ram_gb=_ram_gb(),
                         free_gb=free, staging_drive=drive,
                         vanilla=resolved_vanilla)


# --- persisted settings -----------------------------------------------------
@dataclass
class Settings:
    """Last-used form state, stored next to the vanilla sprite cache."""

    mods: list[str] = field(default_factory=list)
    staging: str = "mod_tfr"
    out: str = "build"
    vanilla: str = ""
    apply: bool = False
    workers: str = "12"
    cap: str = ""
    cull: str = "100"
    auto_workers: bool = False
    auto_cap: bool = True
    auto_cull: bool = False
    skip_vanilla: bool = False
    vanilla_dedup: bool = False
    no_moves: bool = False
    continue_on_error: bool = False
    descriptor: bool = True
    stages: list[str] = field(default_factory=lambda: list(STAGE_NAMES))
    geometry: str = ""

    @staticmethod
    def path() -> Path:
        return fsutil.cache_dir() / "gui.json"

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        target = path or cls.path()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        out = cls()
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            default = getattr(out, f.name)
            if isinstance(default, list):
                if isinstance(value, list):
                    setattr(out, f.name, [str(v) for v in value])
            elif isinstance(default, bool):
                if isinstance(value, bool):
                    setattr(out, f.name, value)
            elif isinstance(value, str):
                setattr(out, f.name, value)
        out.stages = [n for n in out.stages if n in STAGE_NAMES]
        return out

    def save(self, path: Path | None = None) -> None:
        target = path or self.path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
        except OSError:
            pass



# --- explanations (tooltips) -------------------------------------------------
STAGE_HELP = {
    "sync": "Mirrors the selected mods into the staging tree (robocopy). The "
            "original folders are only read. With several mods they are merged "
            "in load order: a file in a later mod replaces the earlier one, "
            "exactly like enabling them together in the launcher.",
    "scan": "Reads the staging tree and reports asset references, GFX_* used "
            "but never defined, sprites defined and never used, and hashes the "
            "vanilla install for dedup. Writes no mod file.",
    "normalize": "Puts assets in the vanilla folder layout and repairs broken "
                 "references. With Apply changes off it only prints the plan.",
    "analyze": "Builds the asset plan: class (ui, portraits, loading…), alpha, "
               "resolution cap from the .gui files, duplicates, unused files. "
               "convert and cutover read this plan.",
    "convert": "Converts PNG/TGA and other formats to DDS DXT1/DXT5, applies "
               "the resolution cap and 4-pixel padding. Keeps the originals in "
               "place: cutover removes them.",
    "cutover": "Rewrites references from .png/.tga to .dds, removes internal "
               "duplicates and deletes the remaining originals. This is what "
               "makes the conversion effective: without it the game will not "
               "load the new DDS.",
    "meshscan": "Analyses .mesh files: vertices, triangles, materials, "
                "collision, compared with the vanilla baseline. Read-only.",
    "texfix": "Brings model textures to a full mip chain and DXT1/DXT5, which "
              "the game requires for 3D models. Re-reads mesh_stats.json.",
    "entityfix": "Adds cull_radius to ambient objects that have none (default "
                 "100) and reports broken attachments. Without Apply changes "
                 "it only audits.",
    "validate": "Checks the produced DDS (header, fourcc, mips), the references "
                "after cutover, orphans, disk sizes and meshes. Exits non-zero "
                "when it finds problems.",
}

TOOLTIPS = {
    "mods": "Mods to optimize, in load order. Add several to merge them into "
            "one staging tree: a file present in a later mod replaces the "
            "earlier one, like the launcher does. The descriptor comes from the "
            "last mod that has one.",
    "add_mod": "Pick a mod folder from disk and append it to the load order.",
    "remove_mod": "Remove the selected mod from the list; its files stay on disk.",
    "up": "Move the selected mod earlier: it will be overridden by the mods "
          "below it.",
    "down": "Move the selected mod later: its files override the mods above it.",
    "staging": "Working copy: moves, rewrites and conversion happen here, so "
               "the original mods stay untouched. Default mod_tfr next to the "
               "executable; needs as much space as the merged mods.",
    "out": "Reports and JSON manifests that later stages re-read (asset plan, "
           "conversions, verification). Not part of the mod, safe to delete.",
    "vanilla": "Hearts of Iron IV install: used for hash baselines, dedup and "
               "sprite fallbacks. Autodetected from the Steam libraries.",
    "browse": "Choose a folder from disk (nothing is applied until you press "
              "Run).",
    "detect": "Search automatically: Workshop mods for the list, Steam "
              "libraries for the vanilla install.",
    "apply": "On: stages that write really modify the staging tree, after a "
             "confirmation showing the folder. Off: dry run, no file is touched "
             "(this is the default).",
    "workers": "How many texconv conversions run in parallel. Each process "
               "uses the GPU: past the core count the gain is small and memory "
               "use grows. Values 1–32.",
    "cap": "Maximum side in pixels (power of two). Auto = cap derived from the "
           "sizes declared in the .gui files, which is the right choice almost "
           "always.",
    "cull": "cull_radius value written into ambient objects that have none "
            "(entityfix). Default 100.",
    "skip_vanilla": "Skips hashing the vanilla install. Faster, but without the "
                    "baseline there is no dedup against vanilla files.",
    "vanilla_dedup": "Adds removal of files identical to vanilla to cutover. "
                     "Off by default: the per-file fallback is unreliable when "
                     "the file carries flags or bookmarks.",
    "descriptor": "After the merge, write <staging>.mod and descriptor.mod with "
                  "the staging folder as name, the oldest supported_version of "
                  "the sources, their combined tags and every replace_path. "
                  "Turn off to keep whatever descriptor the sources shipped.",
    "no_moves": "normalize rewrites references only, without reordering the "
                "asset folders.",
    "continue": "Does not stop the pipeline on the first error. Useful for a "
                "complete report, risky because the following stages work on a "
                "partial state.",
    "run": "Runs the selected stages in order 1→10 (Ctrl+Enter).",
    "cancel": "Terminates the running process and its children (Esc). The "
              "staging tree may be left half-done: run the stages again.",
    "open_out": "Opens the reports folder in Explorer: asset plan, conversion "
                "manifests and validation report.",
    "diag": "Parses the game error.log: textures and sprites the game cannot "
            "load, with the file that declares them.",
    "copy": "Copies the whole log to the clipboard, handy when reporting a "
            "problem.",
    "clear": "Clears the log (Ctrl+L). Files on disk are untouched.",
    "log": "Stage output, coloured by severity: a summary saying «0 errors» is "
           "not an error.",
    "bar": "One segment per selected stage: green = done, amber = running.",
    "auto": "Detects this PC (logical CPUs, RAM, free space, HoI4 install) and "
            "sets workers, cap and cull automatically. Does not turn on Apply "
            "changes.",
    "pc": "Detected on this PC. Refreshed by Auto; nothing is sent anywhere.",
}


class Tooltip:
    """Delayed hover bubble. Never takes focus, always on top, screen-clamped."""

    DELAY = 420

    def __init__(self, widget: tk.Misc, text: str, wrap: int = 360):
        self.widget, self.text, self.wrap = widget, text, wrap
        self._after: str | None = None
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")
        widget.bind("<Destroy>", self._leave, add="+")

    def _enter(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.DELAY, self.show)

    def _leave(self, _e=None):
        self._cancel()
        self.hide()

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def show(self):
        if self.tip is not None or not self.widget.winfo_exists():
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        frame = tk.Frame(tip, bg=C["border_hi"], padx=1, pady=1)
        frame.pack()
        tk.Label(frame, text=self.text, bg="#1E293B", fg=C["fg"], font=UI_SM,
                 justify="left", wraplength=self.wrap, padx=9, pady=7).pack()
        tip.update_idletasks()
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        w, h = tip.winfo_width(), tip.winfo_height()
        x = max(4, min(x, tip.winfo_screenwidth() - w - 8))
        y = max(4, min(y, tip.winfo_screenheight() - h - 8))
        tip.wm_geometry(f"+{x}+{y}")
        self.tip = tip

    def hide(self):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


def tip_tree(widget: tk.Misc, text: str) -> None:
    """Attach one explanation to a widget and everything inside it."""
    Tooltip(widget, text)
    for child in widget.winfo_children():
        tip_tree(child, text)


# --- widgets ----------------------------------------------------------------
class Card(tk.Frame):
    def __init__(self, parent, title: str, **kw):
        super().__init__(parent, bg=C["card"], highlightbackground=C["border"],
                         highlightthickness=1, **kw)
        self.head = tk.Frame(self, bg=C["card"])
        self.head.pack(fill="x", padx=14, pady=(11, 7))
        tk.Label(self.head, text=title.upper(), bg=C["card"], fg=C["muted"],
                 font=("Segoe UI Semibold", 8)).pack(side="left")
        self.body = tk.Frame(self, bg=C["card"])
        self.body.pack(fill="both", expand=True, padx=14, pady=(0, 12))


def entry(parent, var, width=0) -> tk.Entry:
    e = tk.Entry(parent, textvariable=var, bg=C["field"], fg=C["fg"],
                 insertbackground=C["fg"], relief="flat", font=UI,
                 highlightthickness=1, highlightbackground=C["border"],
                 highlightcolor=C["focus"])
    if width:
        e.configure(width=width)
    return e


def button(parent, text, kind="ghost", cmd=None):  # noqa: ANN001 - tk widget factory
    bg, fg, hov = {
        "primary": (C["accent"], C["accent_fg"], C["accent_hi"]),
        "ghost": (C["hover"], C["fg"], "#2E3B5C"),
        "quiet": (C["card"], C["muted"], C["hover"]),
        "danger": (C["card"], C["err"], C["hover"]),
    }[kind]
    b = tk.Button(parent, text=text, command=cmd or (lambda: None), bg=bg, fg=fg,
                  activebackground=hov, activeforeground=fg, relief="flat",
                  borderwidth=0, font=UI_B if kind == "primary" else UI,
                  padx=11 if kind == "primary" else 8, pady=5, cursor="hand2",
                  disabledforeground=C["faint"], highlightthickness=1,
                  highlightbackground=C["border"] if kind == "quiet" else bg,
                  highlightcolor=C["focus"], takefocus=1)
    b.bind("<Enter>", lambda _e: b.configure(
        bg=hov, highlightbackground=hov) if str(b["state"]) == "normal" else None)
    b.bind("<Leave>", lambda _e: b.configure(
        bg=bg, highlightbackground=C["border"] if kind == "quiet" else bg))
    return b


class Check(tk.Frame):
    """Canvas checkbox: identical on every Windows theme."""

    def __init__(self, parent, text, var, bg=C["card"]):
        super().__init__(parent, bg=bg)
        self.var = var
        self.box = tk.Canvas(self, width=15, height=15, bg=bg,
                             highlightthickness=0, cursor="hand2")
        self.box.pack(side="left")
        self.lbl = tk.Label(self, text=text, bg=bg, fg=C["fg"], font=UI,
                            cursor="hand2")
        self.lbl.pack(side="left", padx=(7, 0))
        self.var.trace_add("write", lambda *_a: self.draw())
        for w in (self.box, self.lbl):
            w.bind("<Button-1>", lambda _e: self.var.set(not self.var.get()))
        self.draw()

    def draw(self):
        on = bool(self.var.get())
        self.box.delete("all")
        self.box.create_rectangle(1, 1, 14, 14,
                                  fill=C["accent"] if on else C["field"],
                                  outline=C["border_hi"])
        if on:
            self.box.create_line(4, 8, 7, 11, fill=C["accent_fg"], width=2)
            self.box.create_line(7, 11, 12, 5, fill=C["accent_fg"], width=2)


class Switch(tk.Frame):
    """Pill switch for the global apply toggle."""

    def __init__(self, parent, text, var, bg=C["card"]):
        super().__init__(parent, bg=bg)
        self.var = var
        self.cv = tk.Canvas(self, width=36, height=19, bg=bg,
                            highlightthickness=0, cursor="hand2")
        self.cv.pack(side="left")
        lbl = tk.Label(self, text=text, bg=bg, fg=C["fg"], font=UI_B,
                       cursor="hand2")
        lbl.pack(side="left", padx=(8, 0))
        for w in (self.cv, lbl):
            w.bind("<Button-1>", lambda _e: self.var.set(not self.var.get()))
        self.var.trace_add("write", lambda *_a: self.draw())
        self.draw()

    def draw(self):
        on = bool(self.var.get())
        line = C["accent"] if on else C["border_hi"]
        self.cv.delete("all")
        self.cv.create_oval(1, 2, 15, 16, fill=C["field"], outline=line)
        self.cv.create_oval(21, 2, 35, 16, fill=C["field"], outline=line)
        self.cv.create_rectangle(8, 2, 28, 16, fill=C["field"], outline="")
        self.cv.create_line(2, 9, 34, 9, fill=line, width=1)
        knob = 21 if on else 3
        self.cv.create_oval(knob, 3, knob + 12, 15,
                            fill=C["accent"] if on else C["muted"], outline="")


class StageBar(tk.Canvas):
    """Segmented stage indicator: done / current / pending."""

    def __init__(self, parent, n, width=340):
        super().__init__(parent, height=6, width=width, bg=C["header"],
                         highlightthickness=0)
        self.n, self.done, self.current = n, 0, -1
        self.bind("<Configure>", lambda _e: self.draw())

    def set(self, done, current=-1):
        self.done, self.current = done, current
        self.draw()

    def draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 60)
        gap = 3
        seg = (w - gap * (self.n - 1)) / self.n
        for i in range(self.n):
            x0 = i * (seg + gap)
            color = (C["accent"] if i < self.done else
                     C["warn"] if i == self.current else C["border"])
            self.create_rectangle(x0, 0, x0 + seg, 6, fill=color, outline="")


class ThinScroll(tk.Canvas):
    """6 px scrollbar matching the dark theme (native ones ignore colors)."""

    def __init__(self, parent, target: tk.Text):
        # height=1: a Canvas defaults to 199 px and would stretch its container
        # (it fills vertically in both uses), inflating the log box.
        super().__init__(parent, width=6, height=1, bg=C["field"],
                         highlightthickness=0)
        self.target = target
        self._first, self._last = 0.0, 1.0
        self.thumb = self.create_rectangle(0, 0, 0, 0, fill=C["border_hi"],
                                           outline="")
        target.configure(yscrollcommand=self.on_set)
        self.bind("<Configure>", lambda _e: self.draw())
        self.bind("<Button-1>", self._jump)
        self.bind("<B1-Motion>", self._jump)
        target.bind("<MouseWheel>", self._wheel, add="+")

    def on_set(self, first, last):
        self._first, self._last = float(first), float(last)
        self.draw()

    def draw(self):
        h = self.winfo_height()
        f, l = self._first, self._last
        if h <= 1 or l - f >= 0.999:
            self.coords(self.thumb, 0, 0, 0, 0)
            return
        top = f * h
        self.coords(self.thumb, 0, top, 6, max(top + 18, l * h))

    def _jump(self, e):
        self.target.yview_moveto(max(0.0, e.y / max(self.winfo_height(), 1) - 0.1))

    def _wheel(self, e):
        self.target.yview_scroll(-1 * (e.delta // 120), "units")
        return "break"


# --- application ------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk, settings: Settings | None = None):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self.rows: dict[str, dict] = {}
        self.errors = self.warns = 0
        self.t0 = 0.0
        self.finished = True
        self._fresh = settings is None and not Settings.path().exists()
        self.settings = settings or Settings.load()

        root.title(f"hoi4port {__version__}")
        root.configure(bg=C["bg"])
        self._pick_fonts()

        s = self.settings
        self.mods: list[str] = [m for m in s.mods if m.strip()]
        self.v_stg = tk.StringVar(value=s.staging)
        self.v_out = tk.StringVar(value=s.out)
        self.v_van = tk.StringVar(value=s.vanilla)
        self.v_apply = tk.BooleanVar(value=s.apply)
        self.v_workers = tk.StringVar(value=s.workers)
        self.v_cap = tk.StringVar(value=s.cap or "auto")
        self.v_cull = tk.StringVar(value=s.cull)
        self.v_auto_workers = tk.BooleanVar(value=s.auto_workers)
        self.v_auto_cap = tk.BooleanVar(value=s.auto_cap)
        self.v_auto_cull = tk.BooleanVar(value=s.auto_cull)
        self.v_skipvan = tk.BooleanVar(value=s.skip_vanilla)
        self.v_vdedup = tk.BooleanVar(value=s.vanilla_dedup)
        self.v_nomoves = tk.BooleanVar(value=s.no_moves)
        self.v_cont = tk.BooleanVar(value=s.continue_on_error)
        self.v_desc = tk.BooleanVar(value=s.descriptor)
        chosen = set(s.stages) if s.stages else set(STAGE_NAMES)
        self.v_stage = {n: tk.BooleanVar(value=n in chosen) for n in STAGE_NAMES}

        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(0, weight=1)
        self._build_header()
        self._build_scroll_area()
        self._build_paths(self.inner)
        mid = tk.Frame(self.inner, bg=C["bg"])
        mid.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self._build_pipeline(mid)
        self._build_options(mid)
        self._build_log(self.inner)
        self._build_footer(root)
        self._size_window()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Control-Return>", lambda _e: self.run())
        self.root.bind("<Escape>", lambda _e: self.cancel())
        self.root.bind("<Control-l>", lambda _e: self.clear_log())
        self.root.bind_all("<MouseWheel>", self._wheel)
        self._refresh_mods()
        self._hints()
        self._chip()
        self._sync_auto_states()
        self.root.bind("<Destroy>", self._on_destroy, add="+")
        self._pump_id = self.root.after(80, self._pump)
        self.log("Ready. Pick the mods and press Run (Ctrl+Enter).", "muted")
        self.log("Apply changes is off: stages that write stay a dry run.", "muted")
        self._boot_id = self.root.after(120, self._boot_detect)

    @staticmethod
    def _pick_fonts():
        global MONO
        families = set(tkfont.families())
        for cand in ("Cascadia Mono", "Consolas", "Courier New"):
            if cand in families:
                MONO = (cand, 9)
                return

    # -- layout --------------------------------------------------------------
    def _build_header(self):
        h = tk.Frame(self.root, bg=C["header"], height=54)
        h.grid(row=0, column=0, sticky="ew")
        h.grid_propagate(False)
        left = tk.Frame(h, bg=C["header"])
        left.pack(side="left", padx=16, pady=14)
        tk.Label(left, text="hoi4port", bg=C["header"], fg=C["fg"],
                 font=UI_T).pack(side="left")
        tk.Label(left, text=__version__, bg=C["header"], fg=C["faint"],
                 font=UI_SM).pack(side="left", padx=(8, 0), pady=(5, 0))
        tk.Label(left, text="DDS · mesh · entity", bg=C["header"], fg=C["faint"],
                 font=UI_SM).pack(side="left", padx=(10, 0), pady=(5, 0))

        right = tk.Frame(h, bg=C["header"])
        right.pack(side="right", padx=16)
        self.van_dot = tk.Label(right, text="●", bg=C["header"], fg=C["faint"],
                                font=("Segoe UI", 8))
        self.van_dot.pack(side="left")
        self.van_txt = tk.Label(right, text="vanilla: autodetect", bg=C["header"],
                                fg=C["muted"], font=UI_SM)
        self.van_txt.pack(side="left", padx=(5, 0))

    def _build_scroll_area(self):
        """Everything above the footer scrolls, so no DPI or screen size clips."""
        holder = tk.Frame(self.root, bg=C["bg"])
        holder.grid(row=1, column=0, sticky="nsew")
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(holder, bg=C["bg"], highlightthickness=0,
                                borderwidth=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.inner = tk.Frame(self.canvas, bg=C["bg"])
        self.inner.columnconfigure(0, weight=1)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.scrollbar = ThinScroll(holder, self.canvas)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._win, width=e.width))

    def _wheel(self, event):
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if isinstance(widget, (tk.Text, tk.Listbox)):
                return
            widget = getattr(widget, "master", None)
        if hasattr(self, "canvas"):
            self.canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def _size_window(self):
        """Fit the content and let Tk size the window around header + canvas + footer.

        Hand-computing the height from constants drifts with DPI scaling and
        pushes the footer outside the window; asking Tk for the requested size
        after fixing the canvas height cannot.
        """
        self.root.update_idletasks()
        content = self.inner.winfo_reqheight()
        width = min(max(self.inner.winfo_reqwidth() + 48, 1020),
                    self.root.winfo_screenwidth() - 80)
        avail = (self.root.winfo_screenheight() - 80
                 - self.header_h() - self.footer_h())
        self.canvas.configure(width=width, height=max(260, min(content, avail)))
        self.root.update_idletasks()
        if self.settings.geometry:
            self.root.geometry(self.settings.geometry)
        else:
            self.root.geometry("")
        self.root.minsize(min(900, width),
                          self.header_h() + self.footer_h() + 240)

    @staticmethod
    def header_h() -> int:
        return 54

    @staticmethod
    def footer_h() -> int:
        return 58

    # -- cards ---------------------------------------------------------------
    def _build_paths(self, parent):
        c = Card(parent, "Paths")
        c.grid(row=0, column=0, sticky="ew")
        b = c.body
        self.hints: dict[str, tk.Label] = {}

        lab = tk.Label(b, text="Mods", bg=C["card"], fg=C["muted"], font=UI,
                       width=8, anchor="nw")
        lab.grid(row=0, column=0, sticky="nw", pady=(3, 3))
        Tooltip(lab, TOOLTIPS["mods"])

        self.mod_list = tk.Listbox(
            b, height=4, bg=C["field"], fg=C["fg"], font=UI, relief="flat",
            highlightthickness=1, highlightbackground=C["border"],
            highlightcolor=C["focus"], selectbackground="#1E3A5F",
            selectforeground=C["fg"], activestyle="none", exportselection=False)
        self.mod_list.grid(row=0, column=1, sticky="ew", pady=(3, 3), ipady=3)
        Tooltip(self.mod_list, TOOLTIPS["mods"])
        self.mod_list.bind("<<ListboxSelect>>", lambda _e: self._mods_state())

        actions = tk.Frame(b, bg=C["card"])
        actions.grid(row=0, column=2, sticky="nw", padx=(8, 0), pady=(3, 3))
        add = button(actions, "Add…", "quiet", self.add_mod)
        add.grid(row=0, column=0, sticky="ew")
        Tooltip(add, TOOLTIPS["add_mod"])
        rem = button(actions, "Remove", "quiet", self.remove_mod)
        rem.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        Tooltip(rem, TOOLTIPS["remove_mod"])
        up = button(actions, "↑", "quiet", lambda: self.move_mod(-1))
        up.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        Tooltip(up, TOOLTIPS["up"])
        down = button(actions, "↓", "quiet", lambda: self.move_mod(1))
        down.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))
        Tooltip(down, TOOLTIPS["down"])

        self.mod_hint = tk.Label(b, text="", bg=C["card"], fg=C["dim"],
                                 font=UI_SM, anchor="nw", justify="left")
        self.mod_hint.grid(row=0, column=3, sticky="nw", padx=(10, 0), pady=(3, 3))
        Tooltip(self.mod_hint, TOOLTIPS["mods"])
        self.hints["Mods"] = self.mod_hint

        rows = (
            ("Staging", self.v_stg, False, "working copy", "staging"),
            ("Output", self.v_out, False, "reports and manifests", "out"),
            ("Vanilla", self.v_van, True, "optional: baseline and dedup", "vanilla"),
        )
        for i, (label, var, detect, hint, key) in enumerate(rows, start=1):
            lab = tk.Label(b, text=label, bg=C["card"], fg=C["muted"], font=UI,
                           width=8, anchor="w")
            lab.grid(row=i, column=0, sticky="w", pady=3)
            Tooltip(lab, TOOLTIPS[key])
            e = entry(b, var)
            e.grid(row=i, column=1, sticky="ew", pady=3, ipady=4)
            Tooltip(e, TOOLTIPS[key])
            bf = tk.Frame(b, bg=C["card"])
            bf.grid(row=i, column=2, sticky="e", padx=(8, 0), pady=3)
            browse = button(bf, "Browse…", "quiet", lambda v=var: self._browse(v))
            browse.pack(side="left")
            Tooltip(browse, TOOLTIPS["browse"])
            if detect:
                find = button(bf, "Detect", "quiet", lambda v=var: self._detect(v))
                find.pack(side="left", padx=(6, 0))
                Tooltip(find, TOOLTIPS["detect"])
            lb = tk.Label(b, text=hint, bg=C["card"], fg=C["dim"], font=UI_SM,
                          anchor="w")
            lb.grid(row=i, column=3, sticky="w", padx=(10, 0))
            Tooltip(lb, TOOLTIPS[key])
            self.hints[label] = lb

        detect_mods = button(b, "Detect installed mods", "quiet", self.detect_mods)
        detect_mods.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        Tooltip(detect_mods, TOOLTIPS["detect"])
        b.columnconfigure(1, weight=1)
        self.v_stg.trace_add("write", lambda *_a: self._hints())
        self.v_out.trace_add("write", lambda *_a: self._hints())
        prio = tk.Label(b, text="Priority: these fields → hoi4port.toml → Steam "
                                "autodetect.",
                        bg=C["card"], fg=C["dim"], font=UI_SM)
        prio.grid(row=5, column=0, columnspan=4, sticky="w", pady=(7, 0))
        Tooltip(prio, "Each path resolves in this order: the value written here, "
                      "the HOI4PORT_* variable, hoi4port.toml, then Steam.")

    def _build_pipeline(self, parent):
        c = Card(parent, "Pipeline")
        c.grid(row=0, column=0, sticky="nsew")
        b = c.body
        note = tk.Label(b, text="Execution order 1 → 10. Stages that write follow "
                                "the «Apply changes» switch.",
                        bg=C["card"], fg=C["dim"], font=UI_SM)
        note.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        Tooltip(note, "Order matters: convert produces the DDS, cutover rewrites "
                      "the references and deletes the originals, validate checks "
                      "the result. Hover a stage for details.")
        for i, (name, label, desc, mode) in enumerate(STAGES):
            col, row = divmod(i, 5)
            cell = tk.Frame(b, bg=C["card"])
            cell.grid(row=row + 1, column=col, sticky="ew",
                      padx=(0, 22) if col == 0 else 0, pady=2)
            cell.columnconfigure(2, weight=1)
            b.columnconfigure(col, weight=1)
            Check(cell, "", self.v_stage[name]).grid(row=0, column=0, sticky="w")
            tk.Label(cell, text=f"{i + 1:>2}", bg=C["card"], fg=C["faint"],
                     font=UI_SM).grid(row=0, column=1, sticky="w", padx=(7, 4))
            tk.Label(cell, text=label, bg=C["card"], fg=C["fg"], font=UI_B,
                     anchor="w").grid(row=0, column=2, sticky="w")
            status = tk.Label(cell, text="—", bg=C["card"], fg=C["dim"],
                              font=UI_SM, anchor="e", width=10)
            status.grid(row=0, column=3, columnspan=2, sticky="e")
            tk.Label(cell, text=desc, bg=C["card"], fg=C["dim"], font=UI_SM,
                     anchor="w").grid(row=1, column=1, columnspan=3, sticky="w",
                                      padx=(7, 0))
            tk.Label(cell, text="● writes" if mode == "write" else "○ reads",
                     bg=C["card"], fg=C["warn"] if mode == "write" else C["faint"],
                     font=("Segoe UI", 8), anchor="e").grid(row=1, column=4,
                                                            sticky="e")
            help_text = STAGE_HELP[name]
            if mode == "write":
                help_text += "\n\nWrites into the staging tree: needs Apply changes."
            tip_tree(cell, help_text)
            self.rows[name] = {"status": status, "state": "pending"}

    def _build_options(self, parent):
        c = Card(parent, "Options")
        c.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        auto = button(c.head, "Auto", "quiet", self.auto)
        auto.pack(side="right")
        Tooltip(auto, TOOLTIPS["auto"])
        b = c.body
        sec = tk.Label(b, text="Safety", bg=C["card"], fg=C["muted"],
                       font=("Segoe UI Semibold", 8))
        sec.pack(anchor="w")
        Tooltip(sec, "Stages that write start switched off: look at the plan "
                     "first, then apply it.")
        sw = Switch(b, "Apply changes", self.v_apply)
        sw.pack(anchor="w", pady=(5, 0))
        tip_tree(sw, TOOLTIPS["apply"])
        hint = tk.Label(b, text="Off = dry run: no file is touched.",
                        bg=C["card"], fg=C["dim"], font=UI_SM)
        hint.pack(anchor="w", pady=(2, 7))
        Tooltip(hint, TOOLTIPS["apply"])

        grid = tk.Frame(b, bg=C["card"])
        grid.pack(fill="x")
        self.auto_entries: dict[str, tuple] = {}
        for r, (lab, var, auto_var, hint_text, key) in enumerate((
                ("texconv", self.v_workers, self.v_auto_workers,
                 "parallel jobs 1–32", "workers"),
                ("cap", self.v_cap, self.v_auto_cap,
                 "pixels, power of two", "cap"),
                ("cull", self.v_cull, self.v_auto_cull,
                 "entityfix radius", "cull"))):
            lbl = tk.Label(grid, text=lab, bg=C["card"], fg=C["muted"], font=UI)
            lbl.grid(row=r, column=0, sticky="w", pady=1)
            Tooltip(lbl, TOOLTIPS[key])
            chk = Check(grid, "auto", auto_var, bg=C["card"])
            chk.grid(row=r, column=1, sticky="w", padx=(6, 8), pady=1)
            tip_tree(chk, TOOLTIPS["auto"] + "\n\n" + TOOLTIPS[key])
            e = entry(grid, var, width=5)
            e.grid(row=r, column=2, sticky="w", padx=(0, 6), ipady=3)
            Tooltip(e, TOOLTIPS[key])
            self.auto_entries[key] = (auto_var, e, var)
            h = tk.Label(grid, text=hint_text, bg=C["card"], fg=C["dim"], font=UI_SM)
            h.grid(row=r, column=3, sticky="w")
            Tooltip(h, TOOLTIPS[key])
            auto_var.trace_add("write", lambda *_a: self._sync_auto_states())
        tk.Frame(b, bg=C["border"], height=1).pack(fill="x", pady=7)
        for text, var, key in (("Generate mod descriptor", self.v_desc, "descriptor"),
                               ("Skip vanilla baseline", self.v_skipvan, "skip_vanilla"),
                               ("Vanilla dedup", self.v_vdedup, "vanilla_dedup"),
                               ("Reference repair only", self.v_nomoves, "no_moves"),
                               ("Continue after an error", self.v_cont, "continue")):
            chk = Check(b, text, var)
            chk.pack(anchor="w", pady=1)
            tip_tree(chk, TOOLTIPS[key])

    def _build_log(self, parent):
        c = Card(parent, "Log")
        c.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        tools = tk.Frame(c.head, bg=C["card"])
        tools.pack(side="right")
        clear = button(tools, "Clear", "quiet", self.clear_log)
        clear.pack(side="right")
        Tooltip(clear, TOOLTIPS["clear"])
        copy = button(tools, "Copy", "quiet", self.copy_log)
        copy.pack(side="right", padx=(0, 6))
        Tooltip(copy, TOOLTIPS["copy"])
        lines = 9
        wrap = tk.Frame(c.body, bg=C["field"],
                        highlightbackground=C["border"], highlightthickness=1)
        wrap.pack(fill="x")
        self.txt = tk.Text(wrap, bg=C["field"], fg=C["fg"], font=MONO,
                           relief="flat", highlightthickness=0, wrap="word",
                           padx=10, pady=8, insertbackground=C["fg"],
                           selectbackground="#1E3A5F", state="disabled",
                           height=lines)
        self.txt.pack(side="left", fill="y")
        ThinScroll(wrap, self.txt).pack(side="right", fill="y", padx=(0, 2))
        for tag, col in (("ts", C["ts"]), ("cmd", C["focus"]), ("ok", C["accent"]),
                         ("warn", C["warn"]), ("err", C["err"]),
                         ("muted", C["muted"]), ("info", C["fg"])):
            self.txt.tag_configure(tag, foreground=col)
        Tooltip(self.txt, TOOLTIPS["log"])

    def _build_footer(self, root):
        f = tk.Frame(root, bg=C["header"], height=self.footer_h())
        f.grid(row=2, column=0, sticky="ew")
        f.grid_propagate(False)
        left = tk.Frame(f, bg=C["header"])
        left.pack(side="left", padx=16, pady=8)
        self.phase = tk.Label(left, text="Ready", bg=C["header"], fg=C["muted"],
                              font=UI_SM, anchor="w")
        self.phase.pack(anchor="w", pady=(0, 4))
        self.bar = StageBar(left, len(STAGES))
        self.bar.pack(anchor="w")
        tip_tree(left, TOOLTIPS["bar"])
        self.pc_txt = tk.Label(f, text="", bg=C["header"], fg=C["faint"],
                               font=UI_SM, anchor="w")
        self.pc_txt.pack(side="left", padx=(28, 0))
        Tooltip(self.pc_txt, TOOLTIPS["pc"])

        self.btn_run = button(f, "▶  Run", "primary", self.run)
        self.btn_run.pack(side="right", padx=(0, 16), pady=14)
        Tooltip(self.btn_run, TOOLTIPS["run"])
        self.btn_cancel = button(f, "Cancel", "danger", self.cancel)
        self.btn_cancel.pack(side="right", padx=(0, 10), pady=14)
        self.btn_cancel.configure(state="disabled")
        Tooltip(self.btn_cancel, TOOLTIPS["cancel"])
        self.btn_diag = button(f, "error.log", "quiet", self.diagnostics)
        self.btn_diag.pack(side="right", padx=(0, 10), pady=14)
        Tooltip(self.btn_diag, TOOLTIPS["diag"])
        open_out = button(f, "Open output", "quiet", self.open_out)
        open_out.pack(side="right", padx=(0, 10), pady=14)
        Tooltip(open_out, TOOLTIPS["open_out"])

    # -- mod list ------------------------------------------------------------
    def _refresh_mods(self, select: int | None = None):
        self.mod_list.delete(0, "end")
        for i, mod in enumerate(self.mods, 1):
            self.mod_list.insert("end", f"{i}. {short_path(Path(mod), 2)}")
        if select is not None and 0 <= select < len(self.mods):
            self.mod_list.selection_set(select)
            self.mod_list.see(select)
        self._mods_state()

    def _mods_state(self):
        n = len(self.mods)
        text = (f"{n} mod{'s' if n != 1 else ''} · last one wins"
                if n else "no mod selected yet")
        self.mod_hint.configure(text=text)

    def _selected_mod(self) -> int | None:
        sel = self.mod_list.curselection()
        return int(sel[0]) if sel else None

    def add_mod(self):
        d = filedialog.askdirectory(parent=self.root, mustexist=True,
                                    title="Select a mod folder")
        if d:
            self.mods.append(str(Path(d)))
            self._refresh_mods(select=len(self.mods) - 1)

    def remove_mod(self):
        index = self._selected_mod()
        if index is not None:
            del self.mods[index]
            self._refresh_mods(select=min(index, len(self.mods) - 1))

    def move_mod(self, delta: int):
        index = self._selected_mod()
        if index is None:
            return
        target = index + delta
        if 0 <= target < len(self.mods):
            self.mods[index], self.mods[target] = self.mods[target], self.mods[index]
            self._refresh_mods(select=target)

    def detect_mods(self):
        try:
            from pipeline.config import workshop_candidates
        except Exception:  # noqa: BLE001 - degraded without the package
            self.log("Detection is not available in this build.", "warn")
            return
        found = [str(p) for p in workshop_candidates()]
        if not found:
            self.log("No HoI4 Workshop mods found in the Steam libraries.", "warn")
            return
        added = [m for m in found if m not in self.mods]
        self.mods.extend(added)
        self._refresh_mods(select=len(self.mods) - 1 if added else None)
        self.log(f"Detected {len(found)} Workshop mod(s), added {len(added)}.",
                 "ok" if added else "muted")

    # -- settings ------------------------------------------------------------
    def opts(self) -> Opts:
        cap = self.v_cap.get().strip()
        if self.v_auto_cap.get() or cap.lower() == "auto":
            cap = ""
        return Opts(
            mods=tuple(self.mods), staging=self.v_stg.get(), out=self.v_out.get(),
            vanilla=self.v_van.get(), apply=bool(self.v_apply.get()),
            workers=self.v_workers.get(), cap=cap, cull=self.v_cull.get(),
            skip_vanilla=bool(self.v_skipvan.get()),
            vanilla_dedup=bool(self.v_vdedup.get()),
            no_moves=bool(self.v_nomoves.get()),
            continue_on_error=bool(self.v_cont.get()),
            descriptor=bool(self.v_desc.get()))

    def collect_settings(self) -> Settings:
        s = Settings(mods=list(self.mods), staging=self.v_stg.get().strip(),
                     out=self.v_out.get().strip(), vanilla=self.v_van.get().strip(),
                     apply=bool(self.v_apply.get()),
                     workers=self.v_workers.get().strip(), cap=self.v_cap.get().strip(),
                     cull=self.v_cull.get().strip(),
                     auto_workers=bool(self.v_auto_workers.get()),
                     auto_cap=bool(self.v_auto_cap.get()),
                     auto_cull=bool(self.v_auto_cull.get()),
                     skip_vanilla=bool(self.v_skipvan.get()),
                     vanilla_dedup=bool(self.v_vdedup.get()),
                     no_moves=bool(self.v_nomoves.get()),
                     continue_on_error=bool(self.v_cont.get()),
                     descriptor=bool(self.v_desc.get()),
                     stages=[n for n in STAGE_NAMES if self.v_stage[n].get()])
        try:
            s.geometry = self.root.winfo_geometry()
        except tk.TclError:
            pass
        return s

    def on_close(self):
        if self.proc is not None:
            if not messagebox.askokcancel(
                    "Stage running",
                    "A stage is running: stop it and quit?", parent=self.root):
                return
            self.cancel()
        self.collect_settings().save()
        self.root.destroy()

    # -- helpers -------------------------------------------------------------
    def _browse(self, var):
        d = filedialog.askdirectory(parent=self.root, mustexist=False)
        if d:
            var.set(d)

    def _detect(self, var):
        try:
            from pipeline.config import resolve_vanilla
        except Exception:  # noqa: BLE001 - degraded without the package
            self.log("Detection is not available in this build.", "warn")
            return
        path = resolve_vanilla(None)
        if not path:
            self.log("HoI4 install not found: set the path by hand.", "warn")
            return
        var.set(str(path))
        self._chip()
        self.log(f"Vanilla: {path}", "ok")

    def _hints(self):
        for key, var in (("Staging", self.v_stg), ("Output", self.v_out)):
            lb = self.hints.get(key) if hasattr(self, "hints") else None
            if lb is None:
                continue
            raw = var.get().strip() or ("mod_tfr" if key == "Staging" else "build")
            try:
                lb.configure(text=short_path(Path(raw).resolve()) + "\\")
            except OSError:
                lb.configure(text=raw)

    def _chip(self):
        van = self.v_van.get().strip()
        if van:
            self.van_txt.configure(text=f"vanilla: {Path(van).name}")
            self.van_dot.configure(fg=C["accent"])
        else:
            self.van_txt.configure(text="vanilla: autodetect")
            self.van_dot.configure(fg=C["faint"])

    def _sync_auto_states(self):
        """Disable a field whose auto box is on, and show the auto value."""
        auto_values = {
            "workers": lambda: str(recommend_workers(os.cpu_count() or 0)),
            "cap": lambda: "auto",
            "cull": lambda: DEFAULT_CULL,
        }
        for key, (var, widget, shown) in getattr(self, "auto_entries", {}).items():
            on = bool(var.get())
            widget.configure(state="disabled" if on else "normal",
                             disabledbackground=C["field"],
                             disabledforeground=C["dim"])
            if on:
                shown.set(auto_values[key]())

    def _pc(self, profile: "SystemProfile") -> None:
        bits = [f"{profile.threads} threads", f"{profile.ram_gb:.1f} GB RAM"]
        if profile.free_gb and profile.staging_drive:
            bits.append(f"{profile.staging_drive} {profile.free_gb:.0f} GB free")
        bits.append("vanilla detected" if profile.vanilla else "vanilla not set")
        self.pc_txt.configure(text=" · ".join(bits))

    def _apply_auto(self, profile: "SystemProfile") -> "AutoPlan":
        plan = plan_auto(profile)
        self.v_auto_workers.set(True)
        self.v_auto_cap.set(True)
        self.v_auto_cull.set(True)
        self.v_workers.set(plan.workers)
        self.v_cap.set("auto")
        self.v_cull.set(plan.cull)
        if plan.vanilla:
            self.v_van.set(plan.vanilla)
        self._sync_auto_states()
        self._chip()
        self._pc(profile)
        return plan

    def _boot_detect(self) -> None:
        profile = system_profile(self.v_stg.get(), self.v_van.get())
        plan = plan_auto(profile)
        if self._fresh:
            self._apply_auto(profile)
            self.log(f"PC: {self.pc_txt.cget('text')}", "muted")
            self.log(f"Auto: workers {plan.workers} for {profile.threads} logical "
                     f"threads; cap and cull left automatic (press Auto to redo).",
                     "muted")
        else:
            if plan.vanilla and not self.v_van.get().strip():
                self.v_van.set(plan.vanilla)
            self._pc(profile)
            self._chip()

    def auto(self) -> None:
        profile = system_profile(self.v_stg.get(), self.v_van.get())
        plan = self._apply_auto(profile)
        for note in plan.notes:
            self.log(f"Auto: {note}", "muted")
        self.log(f"Auto: workers {plan.workers}, cap auto, cull {plan.cull}"
                 + (", vanilla detected" if plan.vanilla else
                    ", vanilla not found"), "ok")

    def log(self, text, tag="info", ts=None):
        self.txt.configure(state="normal")
        self.txt.insert("end", f"{ts or time.strftime('%H:%M:%S')}  ", "ts")
        self.txt.insert("end", text + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")
        if tag == "err":
            self.errors += 1
        elif tag == "warn":
            self.warns += 1

    def clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.txt.get("1.0", "end"))

    def open_out(self):
        p = Path(self.v_out.get().strip() or "build")
        if p.exists():
            os.startfile(p)  # noqa: S606 - Windows shell
        else:
            self.log(f"Output folder not found: {p.resolve()}", "warn")

    def set_stage(self, name, state, secs=None):
        row = self.rows[name]
        row["state"] = state
        text, color = {
            "pending": ("—", C["dim"]), "running": ("● running", C["warn"]),
            "ok": ("✓", C["accent"]), "err": ("✗ error", C["err"]),
        }[state]
        if state == "ok" and secs is not None:
            text = f"✓ {secs:.1f} s"
        row["status"].configure(text=text, fg=color)

    def _run_state(self, enabled: bool):
        if enabled:
            self.btn_run.configure(state="normal", bg=C["accent"], fg=C["accent_fg"],
                                   activebackground=C["accent_hi"])
        else:
            self.btn_run.configure(state="disabled", bg=C["disabled"], fg=C["faint"],
                                   activebackground=C["disabled"])
        self.btn_diag.configure(state="normal" if enabled else "disabled")

    # -- execution -----------------------------------------------------------
    def _validate(self, stages) -> Opts | None:
        if not stages:
            self.log("No stage selected.", "warn")
            return None
        if "sync" in stages and not self.mods:
            self.log("Add at least one mod folder for sync.", "err")
            return None
        if not self.v_stg.get().strip() or not self.v_out.get().strip():
            self.log("Staging and Output cannot be empty.", "err")
            return None
        cap = self.v_cap.get().strip()
        if cap and cap.lower() != "auto":
            try:
                if int(cap) < 1:
                    raise ValueError
            except ValueError:
                self.log("cap must be a positive integer (or auto).", "err")
                return None
        o = self.opts()
        o = Opts(**{**asdict(o),
                    "workers": str(clamp_int(o.workers, 12, 1, 32)),
                    "cull": str(clamp_int(o.cull, 100, 0, 100_000))})
        if not self.v_auto_workers.get():
            self.v_workers.set(o.workers)
        if not self.v_auto_cull.get():
            self.v_cull.set(o.cull)
        if o.apply and any(s in WRITE for s in stages):
            if not messagebox.askokcancel(
                    "Apply changes",
                    "Stages in APPLY mode will modify files in:\n\n"
                    f"{Path(o.staging).resolve()}\n\nContinue?", parent=self.root):
                self.log("Cancelled before starting.", "muted")
                return None
        return o

    def run(self):
        if not self.finished:
            return
        stages = [n for n in STAGE_NAMES if self.v_stage[n].get()]
        o = self._validate(stages)
        if o is None:
            return
        cmds = [(name, stage_command(name, o)) for name in stages]
        self.collect_settings().save()
        self._start(cmds)

    def diagnostics(self):
        if not self.finished:
            return
        self._start([("check-log", stage_command("check-log", self.opts()))])

    def _start(self, cmds):
        self.errors = self.warns = 0
        self.t0 = time.perf_counter()
        self.finished = False
        self._run_state(False)
        self.btn_cancel.configure(state="normal")
        self.clear_log()
        self.log(f"hoi4port {__version__} — {' → '.join(n for n, _ in cmds)}", "cmd")
        self.bar.set(0, 0)
        self.phase.configure(text=f"Stage 1 of {len(cmds)} — starting")
        for name in STAGE_NAMES:
            self.set_stage(name, "pending")
        keep_going = bool(self.v_cont.get())
        threading.Thread(target=self._worker, args=(cmds, keep_going),
                         daemon=True).start()

    def _worker(self, cmds, keep_going: bool):
        total = len(cmds)
        for i, (name, cmd) in enumerate(cmds):
            self.q.put(("stage", (name, "running", i, total)))
            shown = cmd[3:] if cmd[1:2] == ["--cli"] else cmd[2:]
            self.q.put(("log", ("  $ " + " ".join(shown), "cmd")))
            t0 = time.perf_counter()
            rc = 1
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    line = line.rstrip()
                    if line:
                        self.q.put(("log", (line, classify(line))))
                rc = self.proc.wait()
            except Exception as exc:  # noqa: BLE001 - surfaced in the log
                self.q.put(("log", (f"Error: {exc}", "err")))
            finally:
                self.proc = None
            secs = time.perf_counter() - t0
            self.q.put(("stage", (name, "ok" if rc == 0 else "err", i + 1, total, secs)))
            self.q.put(("log", (f"  {'✓' if rc == 0 else '✗'} {name:<10}{secs:6.2f} s"
                                + ("" if rc == 0 else f"   (exit {rc})"),
                                "ok" if rc == 0 else "err")))
            if rc != 0 and not keep_going:
                break
        self.q.put(("done", None))

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload[0], payload[1])
                elif kind == "stage":
                    name, state, i, total, *rest = payload
                    if name in self.rows:
                        self.set_stage(name, state, rest[0] if rest else None)
                    if state == "running":
                        self.bar.set(i, i)
                        self.phase.configure(text=f"Stage {i + 1} of {total} — {name}")
                elif kind == "done":
                    self.finished = True
                    self._run_state(True)
                    self.btn_cancel.configure(state="disabled")
                    dt = time.perf_counter() - self.t0
                    bad = any(r["state"] == "err" for r in self.rows.values())
                    done = sum(1 for r in self.rows.values() if r["state"] == "ok")
                    self.bar.set(done)
                    self.phase.configure(
                        text=f"{'Interrupted' if bad else 'Completed'} — "
                             f"{self.errors} errors · {self.warns} warnings")
                    self.log(f"Done: {dt:.1f} s — {self.errors} errors, "
                             f"{self.warns} warnings.", "err" if bad else "ok")
        except queue.Empty:
            pass
        self._chip()
        self._pump_id = self.root.after(80, self._pump)

    def _on_destroy(self, event=None):
        if event is not None and getattr(event, "widget", None) is not self.root:
            return
        for attr in ("_pump_id", "_boot_id"):
            tid = getattr(self, attr, None)
            if tid:
                try:
                    self.root.after_cancel(tid)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def cancel(self):
        if self.proc is not None:
            self.log("Cancellation requested …", "warn")
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                               capture_output=True, creationflags=0x08000000)
            except Exception:  # noqa: BLE001 - best effort
                self.proc.terminate()


def main() -> int:
    """Entry point for ``hoi4port gui`` and the ``hoi4port-gui`` script."""
    if "--selftest" in sys.argv[1:]:
        # Frozen-build smoke test: proves tkinter and the pipeline are bundled
        # without needing a desktop session.
        import tkinter  # noqa: F401 - import is the assertion
        probe = stage_command("scan", Opts(staging="s", out="o"), prefix=["x"])
        assert probe[0] == "x" and probe[1] == "scan", probe
        print(f"hoi4port-gui {__version__} selftest ok")
        return 0
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - not Windows, or already per-monitor
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
