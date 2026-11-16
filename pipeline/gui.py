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
    ("sync", "Sync", "Workshop → staging", "write"),
    ("scan", "Scan", "riferimenti, GFX, baseline", "read"),
    ("normalize", "Normalize", "ordina asset, ripara ref", "write"),
    ("analyze", "Analyze", "alpha, cap, dedup, inutilizzati", "read"),
    ("convert", "Convert", "PNG/TGA → DDS BC1/BC3", "write"),
    ("cutover", "Cutover", "riscrive ref, rimuove originali", "write"),
    ("meshscan", "Meshscan", "vertici, materiali, collisione", "read"),
    ("texfix", "Texfix", "mip chain + DXT1/5 modelli", "write"),
    ("entityfix", "Entityfix", "cull_radius per ambient", "write"),
    ("validate", "Validate", "DDS, riferimenti, dimensioni", "read"),
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

    mod: str = ""
    staging: str = "mod_tfr"
    out: str = "build"
    vanilla: str = ""
    apply: bool = False
    workers: str = "12"
    cap: str = ""
    cull: str = "100"
    skip_vanilla: bool = False
    vanilla_dedup: bool = False
    no_moves: bool = False
    continue_on_error: bool = False
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
        return cmd + ["--mod", abs_path(o.mod), "--staging", stg]
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
    vanilla: str
    notes: tuple[str, ...]


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
    notes.append(f"CPU: {profile.threads} thread logici → texconv workers {workers}")
    if profile.ram_gb:
        notes.append(f"RAM: {profile.ram_gb:.1f} GB")
    if profile.free_gb and profile.staging_drive:
        notes.append(f"Spazio libero su {profile.staging_drive}: "
                     f"{profile.free_gb:.0f} GB (la staging è una copia integrale)")
        if profile.free_gb < 5:
            notes.append("Spazio sotto 5 GB: la copia della mod potrebbe non entrare.")
    notes.append(f"Vanilla: {profile.vanilla}" if profile.vanilla else
                 "Vanilla: non rilevata, imposta il percorso a mano")
    return AutoPlan(workers=str(workers), vanilla=profile.vanilla,
                    notes=tuple(notes))


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

    mod: str = ""
    staging: str = "mod_tfr"
    out: str = "build"
    vanilla: str = ""
    apply: bool = False
    workers: str = "12"
    cap: str = ""
    cull: str = "100"
    skip_vanilla: bool = False
    vanilla_dedup: bool = False
    no_moves: bool = False
    continue_on_error: bool = False
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
    "sync": "Copia speculare della mod in Staging (robocopy /MIR). "
            "La cartella originale viene solo letta. Cancella dalla staging "
            "ciò che non è più nella mod.",
    "scan": "Legge la staging e riporta riferimenti agli asset, GFX_* usati ma "
            "non definiti, sprite definiti e mai usati, hash della vanilla per "
            "il dedup. Non scrive file della mod.",
    "normalize": "Rimette gli asset nell'ordine delle cartelle vanilla e ripara "
                 "i riferimenti rotti. Con Applica modifiche spento mostra solo "
                 "il piano.",
    "analyze": "Costruisce il piano asset: classe (ui, portraits, loading…), "
               "alpha, dimensione massima dalle .gui, duplicati, file inutilizzati. "
               "convert e cutover leggono questo piano.",
    "convert": "Converte PNG/TGA/altri formati in DDS DXT1/DXT5, applica il cap "
               "di risoluzione e il padding multiplo di 4. Lascia i file "
               "originali al loro posto: li rimuove cutover.",
    "cutover": "Riscrive i riferimenti da .png/.tga a .dds, elimina i duplicati "
               "interni e cancella gli originali rimasti. È la fase che rende "
               "effettiva la conversione: senza, il gioco non carica i nuovi DDS.",
    "meshscan": "Analizza i .mesh: vertici, triangoli, materiali, collisione, "
                "confronto con la baseline vanilla. Solo lettura.",
    "texfix": "Porta le texture dei modelli a mip chain completa e DXT1/DXT5, "
              "che il gioco richiede per i modelli 3D. Rilegge mesh_stats.json.",
    "entityfix": "Aggiunge cull_radius agli ambient object che non ce l'hanno "
                 "(default 100) e segnala gli attach rotti. Senza --apply fa "
                 "solo l'audit.",
    "validate": "Controlla i DDS prodotti (header, fourcc, mip), i riferimenti "
                "dopo il cutover, gli orfani, le dimensioni su disco e le mesh. "
                "Esce con codice ≠ 0 se trova problemi.",
}

TOOLTIPS = {
    "mod": "Cartella della mod da ottimizzare oppure ID Workshop numerico "
           "(es. 3350890356). Viene solo letta: sync ne crea una copia in Staging.",
    "staging": "Copia di lavoro: qui avvengono move, riscritture e conversione, "
               "quindi la mod originale resta intatta. Default mod_tfr accanto "
               "all'eseguibile; serve spazio pari alla mod.",
    "out": "Report e manifest JSON letti dalle fasi successive (piano asset, "
           "conversioni, verifiche). Non fa parte della mod e si può cancellare.",
    "vanilla": "Install di Hearts of Iron IV: serve per baseline degli hash, "
               "dedup e fallback degli sprite. Autodetect dalle librerie Steam.",
    "browse": "Scegli una cartella dal disco (la scelta non viene applicata "
              "finché non premi Esegui).",
    "detect": "Cerca automaticamente la mod nelle Workshop installate o la "
              "vanilla nelle librerie Steam.",
    "apply": "Acceso: le fasi che scrivono modificano davvero la staging, dopo "
             "una conferma che mostra la cartella. Spento: dry-run, nessun file "
             "toccato (è il default).",
    "workers": "Quante conversioni texconv in parallelo. Ogni processo usa la "
               "GPU: oltre il numero di core il guadagno è minimo e cresce la "
               "memoria occupata. Valori 1–32.",
    "cap": "Lato massimo in pixel (potenza di 2). Vuoto = cap dedotto dalle "
           "dimensioni dichiarate nei .gui, che è la scelta giusta quasi sempre.",
    "cull": "Valore di cull_radius inserito negli ambient object privi di uno "
            "(entityfix). Default 100.",
    "skip_vanilla": "Non calcola l'hash dell'install vanilla. Più veloce, ma "
                    "senza baseline non c'è dedup contro i file vanilla.",
    "vanilla_dedup": "Aggiunge al cutover la cancellazione dei file identici alla "
                     "vanilla. Spento di default: il fallback per-file non è "
                     "affidabile quando il file ha flag o bookmark.",
    "no_moves": "normalize riscrive solo i riferimenti, senza riordinare le "
                "cartelle degli asset.",
    "continue": "Non ferma la pipeline al primo errore. Utile per un report "
                "completo, rischioso perché le fasi successive lavorano su uno "
                "stato parziale.",
    "run": "Esegue le fasi selezionate nell'ordine 1→10 (Ctrl+Invio).",
    "cancel": "Termina il processo in corso e i suoi figli (Esc). La staging può "
              "restare a metà: rilancia le fasi da capo.",
    "open_out": "Apre in Explorer la cartella dei report: piano asset, "
                "manifest di conversione e report di validazione.",
    "diag": "Analizza l'error.log del gioco: texture e sprite che il gioco non "
            "riesce a caricare, con il file che li dichiara.",
    "copy": "Copia tutto il log negli appunti, comodo per allegarlo a una "
            "segnalazione.",
    "clear": "Svuota il log (Ctrl+L). Non tocca i report su disco.",
    "log": "Output delle fasi, colorato per severità: un riepilogo con "
           "«0 errori» non è un errore.",
    "bar": "Una tacca per fase selezionata: verde = completata, ambra = in corso.",
    "auto": "Rileva CPU, RAM, spazio libero e install del gioco, poi imposta "
            "workers e vanilla per questa macchina. Non cambia il cap e non "
            "attiva Applica modifiche.",
    "pc": "Rilevato su questo PC. Aggiornato da «Auto»; nessun valore viene "
          "inviato altrove.",
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
        super().__init__(parent, width=6, bg=C["field"], highlightthickness=0)
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
        root.geometry(self.settings.geometry or "1080x930")
        root.minsize(1000, 830)
        self._pick_fonts()

        s = self.settings
        self.v_mod = tk.StringVar(value=s.mod)
        self.v_stg = tk.StringVar(value=s.staging)
        self.v_out = tk.StringVar(value=s.out)
        self.v_van = tk.StringVar(value=s.vanilla)
        self.v_apply = tk.BooleanVar(value=s.apply)
        self.v_workers = tk.StringVar(value=s.workers)
        self.v_cap = tk.StringVar(value=s.cap)
        self.v_cull = tk.StringVar(value=s.cull)
        self.v_skipvan = tk.BooleanVar(value=s.skip_vanilla)
        self.v_vdedup = tk.BooleanVar(value=s.vanilla_dedup)
        self.v_nomoves = tk.BooleanVar(value=s.no_moves)
        self.v_cont = tk.BooleanVar(value=s.continue_on_error)
        chosen = set(s.stages) if s.stages else set(STAGE_NAMES)
        self.v_stage = {n: tk.BooleanVar(value=n in chosen) for n in STAGE_NAMES}

        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(0, weight=1)
        self._build_header()
        content = tk.Frame(root, bg=C["bg"])
        content.grid(row=1, column=0, sticky="nsew", padx=16, pady=(12, 0))
        content.grid_rowconfigure(1, weight=1)
        content.grid_columnconfigure(0, weight=1)
        self._build_paths(content)
        mid = tk.Frame(content, bg=C["bg"])
        mid.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self._build_pipeline(mid)
        self._build_options(mid)
        self._build_log(content)
        self._build_footer(root)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Control-Return>", lambda _e: self.run())
        root.bind("<Escape>", lambda _e: self.cancel())
        root.bind("<Control-l>", lambda _e: self.clear_log())
        self._hints()
        self._chip()
        self.root.after(80, self._pump)
        self.log("Pronto. Scegli la mod e premi Esegui (Ctrl+Invio).", "muted")
        self.log("Applica modifiche è spento: le fasi che scrivono restano dry-run.",
                 "muted")
        self.root.after(120, self._boot_detect)

    @staticmethod
    def _pick_fonts():
        global MONO
        families = set(tkfont.families())
        for cand in ("Cascadia Mono", "Consolas", "Courier New"):
            if cand in families:
                MONO = (cand, 9)
                return

    # -- chrome --------------------------------------------------------------
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

    def _build_paths(self, parent):
        c = Card(parent, "Percorsi")
        c.grid(row=0, column=0, sticky="ew")
        b = c.body
        self.hints: dict[str, tk.Label] = {}
        rows = (
            ("Mod", self.v_mod, True, "ID Workshop o cartella", "mod"),
            ("Staging", self.v_stg, False, "cartella di lavoro", "staging"),
            ("Output", self.v_out, False, "report e manifest", "out"),
            ("Vanilla", self.v_van, True, "opzionale: baseline e dedup", "vanilla"),
        )
        for i, (label, var, detect, hint, key) in enumerate(rows):
            lab = tk.Label(b, text=label, bg=C["card"], fg=C["muted"], font=UI,
                           width=8, anchor="w")
            lab.grid(row=i, column=0, sticky="w", pady=3)
            Tooltip(lab, TOOLTIPS[key])
            e = entry(b, var)
            e.grid(row=i, column=1, sticky="ew", pady=3, ipady=4)
            Tooltip(e, TOOLTIPS[key])
            bf = tk.Frame(b, bg=C["card"])
            bf.grid(row=i, column=2, sticky="e", padx=(8, 0), pady=3)
            browse = button(bf, "Sfoglia", "quiet", lambda v=var: self._browse(v))
            browse.pack(side="left")
            Tooltip(browse, TOOLTIPS["browse"])
            if detect:
                find = button(bf, "Rileva", "quiet",
                              lambda v=var: self._detect(v))
                find.pack(side="left", padx=(6, 0))
                Tooltip(find, TOOLTIPS["detect"])
            lb = tk.Label(b, text=hint, bg=C["card"], fg=C["dim"], font=UI_SM,
                          anchor="w")
            lb.grid(row=i, column=3, sticky="w", padx=(10, 0))
            Tooltip(lb, TOOLTIPS[key])
            self.hints[label] = lb
        b.columnconfigure(1, weight=1)
        self.v_stg.trace_add("write", lambda *_a: self._hints())
        self.v_out.trace_add("write", lambda *_a: self._hints())
        prio = tk.Label(b, text="Priorità: campi → hoi4port.toml → autodetect Steam.",
                        bg=C["card"], fg=C["dim"], font=UI_SM)
        prio.grid(row=len(rows), column=0, columnspan=4, sticky="w", pady=(7, 0))
        Tooltip(prio, "Ogni percorso si risolve in quest'ordine: valore scritto "
                      "qui, variabile HOI4PORT_* , hoi4port.toml, poi Steam.")

    def _build_pipeline(self, parent):
        c = Card(parent, "Pipeline")
        c.grid(row=0, column=0, sticky="nsew")
        b = c.body
        note = tk.Label(b, text="Ordine di esecuzione 1 → 10. Le fasi che scrivono "
                                "seguono l'interruttore «Applica modifiche».",
                        bg=C["card"], fg=C["dim"], font=UI_SM)
        note.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        Tooltip(note, "L'ordine conta: convert produce i DDS, cutover riscrive i "
                      "riferimenti e cancella gli originali, validate controlla "
                      "il risultato. Passa il mouse su una fase per i dettagli.")
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
            tag = tk.Label(cell, text="● scrive" if mode == "write" else "○ legge",
                           bg=C["card"], fg=C["warn"] if mode == "write" else C["faint"],
                           font=("Segoe UI", 8), anchor="e")
            tag.grid(row=1, column=4, sticky="e")
            help_text = STAGE_HELP[name]
            if mode == "write":
                help_text += "\n\nScrive nella staging: serve Applica modifiche."
            tip_tree(cell, help_text)
            self.rows[name] = {"status": status, "state": "pending"}

    def _build_options(self, parent):
        c = Card(parent, "Opzioni")
        c.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        auto = button(c.head, "Auto", "quiet", self.auto)
        auto.pack(side="right")
        Tooltip(auto, TOOLTIPS["auto"])
        b = c.body
        sec = tk.Label(b, text="Sicurezza", bg=C["card"], fg=C["muted"],
                       font=("Segoe UI Semibold", 8))
        sec.pack(anchor="w")
        Tooltip(sec, "Le fasi che scrivono partono spente: prima si guarda il "
                     "piano, poi si applica.")
        sw = Switch(b, "Applica modifiche", self.v_apply)
        sw.pack(anchor="w", pady=(5, 0))
        tip_tree(sw, TOOLTIPS["apply"])
        hint = tk.Label(b, text="Spento = dry-run: nessun file viene toccato.",
                        bg=C["card"], fg=C["dim"], font=UI_SM)
        hint.pack(anchor="w", pady=(2, 7))
        Tooltip(hint, TOOLTIPS["apply"])

        grid = tk.Frame(b, bg=C["card"])
        grid.pack(fill="x")
        for r, (lab, var, text, key) in enumerate((
                ("texconv", self.v_workers, "processi 1–32", "workers"),
                ("cap", self.v_cap, "vuoto = auto (.gui)", "cap"),
                ("cull", self.v_cull, "entityfix radius", "cull"))):
            lbl = tk.Label(grid, text=lab, bg=C["card"], fg=C["muted"], font=UI)
            lbl.grid(row=r, column=0, sticky="w", pady=1)
            Tooltip(lbl, TOOLTIPS[key])
            e = entry(grid, var, width=4)
            e.grid(row=r, column=1, sticky="w", padx=(8, 6), ipady=3)
            Tooltip(e, TOOLTIPS[key])
            h = tk.Label(grid, text=text, bg=C["card"], fg=C["dim"], font=UI_SM)
            h.grid(row=r, column=2, sticky="w")
            Tooltip(h, TOOLTIPS[key])

        tk.Frame(b, bg=C["border"], height=1).pack(fill="x", pady=7)
        for text, var, key in (("Salta baseline vanilla", self.v_skipvan, "skip_vanilla"),
                               ("Dedup identici a vanilla", self.v_vdedup, "vanilla_dedup"),
                               ("Solo ref, nessun move", self.v_nomoves, "no_moves"),
                               ("Continua dopo un errore", self.v_cont, "continue")):
            chk = Check(b, text, var)
            chk.pack(anchor="w", pady=1)
            tip_tree(chk, TOOLTIPS[key])

    def _build_log(self, parent):
        c = Card(parent, "Log")
        c.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        tools = tk.Frame(c.head, bg=C["card"])
        tools.pack(side="right")
        clear = button(tools, "Svuota", "quiet", self.clear_log)
        clear.pack(side="right")
        Tooltip(clear, TOOLTIPS["clear"])
        copy = button(tools, "Copia", "quiet", self.copy_log)
        copy.pack(side="right", padx=(0, 6))
        Tooltip(copy, TOOLTIPS["copy"])
        lines = 9
        lh = tkfont.Font(font=MONO).metrics("linespace")
        wrap = tk.Frame(c.body, bg=C["field"], height=lh * lines + 18,
                        highlightbackground=C["border"], highlightthickness=1)
        wrap.pack(fill="x")
        wrap.pack_propagate(False)
        self.txt = tk.Text(wrap, bg=C["field"], fg=C["fg"], font=MONO,
                           relief="flat", highlightthickness=0, wrap="word",
                           padx=10, pady=8, insertbackground=C["fg"],
                           selectbackground="#1E3A5F", state="disabled",
                           height=lines)
        self.txt.pack(side="left", fill="both", expand=True)
        ThinScroll(wrap, self.txt).pack(side="right", fill="y", padx=(0, 2))
        for tag, col in (("ts", C["ts"]), ("cmd", C["focus"]), ("ok", C["accent"]),
                         ("warn", C["warn"]), ("err", C["err"]),
                         ("muted", C["muted"]), ("info", C["fg"])):
            self.txt.tag_configure(tag, foreground=col)
        Tooltip(self.txt, TOOLTIPS["log"])

    def _build_footer(self, root):
        f = tk.Frame(root, bg=C["header"], height=58)
        f.grid(row=2, column=0, sticky="ew")
        f.grid_propagate(False)
        left = tk.Frame(f, bg=C["header"])
        left.pack(side="left", padx=16, pady=8)
        self.phase = tk.Label(left, text="Pronto", bg=C["header"], fg=C["muted"],
                              font=UI_SM, anchor="w")
        self.phase.pack(anchor="w", pady=(0, 4))
        self.bar = StageBar(left, len(STAGES))
        self.bar.pack(anchor="w")
        tip_tree(left, TOOLTIPS["bar"])
        self.pc_txt = tk.Label(f, text="", bg=C["header"], fg=C["faint"],
                               font=UI_SM, anchor="w")
        self.pc_txt.pack(side="left", padx=(28, 0))
        Tooltip(self.pc_txt, TOOLTIPS["pc"])

        self.btn_run = button(f, "▶  Esegui", "primary", self.run)
        self.btn_run.pack(side="right", padx=(0, 16), pady=14)
        Tooltip(self.btn_run, TOOLTIPS["run"])
        self.btn_cancel = button(f, "Annulla", "danger", self.cancel)
        self.btn_cancel.pack(side="right", padx=(0, 10), pady=14)
        self.btn_cancel.configure(state="disabled")
        Tooltip(self.btn_cancel, TOOLTIPS["cancel"])
        self.btn_diag = button(f, "error.log", "quiet", self.diagnostics)
        self.btn_diag.pack(side="right", padx=(0, 10), pady=14)
        Tooltip(self.btn_diag, TOOLTIPS["diag"])
        open_out = button(f, "Apri output", "quiet", self.open_out)
        open_out.pack(side="right", padx=(0, 10), pady=14)
        Tooltip(open_out, TOOLTIPS["open_out"])

    # -- settings ------------------------------------------------------------
    def opts(self) -> Opts:
        return Opts(
            mod=self.v_mod.get(), staging=self.v_stg.get(), out=self.v_out.get(),
            vanilla=self.v_van.get(), apply=bool(self.v_apply.get()),
            workers=self.v_workers.get(), cap=self.v_cap.get(),
            cull=self.v_cull.get(), skip_vanilla=bool(self.v_skipvan.get()),
            vanilla_dedup=bool(self.v_vdedup.get()),
            no_moves=bool(self.v_nomoves.get()),
            continue_on_error=bool(self.v_cont.get()))

    def collect_settings(self) -> Settings:
        s = Settings(mod=self.v_mod.get().strip(), staging=self.v_stg.get().strip(),
                     out=self.v_out.get().strip(), vanilla=self.v_van.get().strip(),
                     apply=bool(self.v_apply.get()),
                     workers=self.v_workers.get().strip(), cap=self.v_cap.get().strip(),
                     cull=self.v_cull.get().strip(),
                     skip_vanilla=bool(self.v_skipvan.get()),
                     vanilla_dedup=bool(self.v_vdedup.get()),
                     no_moves=bool(self.v_nomoves.get()),
                     continue_on_error=bool(self.v_cont.get()),
                     stages=[n for n in STAGE_NAMES if self.v_stage[n].get()])
        try:
            s.geometry = self.root.winfo_geometry()
        except tk.TclError:
            pass
        return s

    def on_close(self):
        if self.proc is not None:
            if not messagebox.askokcancel(
                    "Esecuzione in corso",
                    "Una fase sta girando: interromperla e uscire?", parent=self.root):
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
            from pipeline.config import resolve_vanilla, workshop_candidates
        except Exception:  # noqa: BLE001 - degraded without the package
            self.log("Rilevamento non disponibile (pipeline non importabile).", "warn")
            return
        if var is self.v_van:
            path = resolve_vanilla(None)
            if not path:
                self.log("Install vanilla non trovata: imposta il percorso a mano.",
                         "warn")
                return
            var.set(str(path))
            self.log(f"Vanilla: {path}", "ok")
        else:
            cands = workshop_candidates()
            if not cands:
                self.log("Nessuna mod Workshop trovata.", "warn")
                return
            var.set(str(cands[0]))
            extra = (f" ({len(cands)} mod trovate, impostata la prima)"
                     if len(cands) > 1 else "")
            self.log(f"Mod: {cands[0]}{extra}", "warn" if len(cands) > 1 else "ok")
        self._chip()

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

    def _pc(self, profile: "SystemProfile") -> None:
        bits = [f"{profile.threads} thread", f"{profile.ram_gb:.1f} GB RAM"]
        if profile.free_gb and profile.staging_drive:
            bits.append(f"{profile.staging_drive} {profile.free_gb:.0f} GB liberi")
        self.pc_txt.configure(text=" · ".join(bits))

    def _boot_detect(self) -> None:
        """First run: seed the machine-dependent options instead of the CLI's."""
        profile = system_profile(self.v_stg.get(), self.v_van.get())
        plan = plan_auto(profile)
        if self._fresh:
            self.v_workers.set(plan.workers)
        if plan.vanilla and not self.v_van.get().strip():
            self.v_van.set(plan.vanilla)
        self._chip()
        self._pc(profile)
        if self._fresh:
            self.log(f"PC: {self.pc_txt.cget('text')}", "muted")
            self.log(f"Auto: workers {plan.workers} per {profile.threads} thread "
                     f"logici (modificabile, o premi Auto per riapplicare).",
                     "muted")

    def auto(self) -> None:
        """Detect this machine and apply the options that depend on it."""
        profile = system_profile(self.v_stg.get(), self.v_van.get())
        plan = plan_auto(profile)
        self.v_workers.set(plan.workers)
        if plan.vanilla:
            self.v_van.set(plan.vanilla)
        self._chip()
        self._pc(profile)
        for note in plan.notes:
            self.log(f"Auto: {note}", "muted")
        self.log(f"Auto: impostati workers {plan.workers}"
                 + (", vanilla rilevata" if plan.vanilla else
                    ", vanilla non trovata"), "ok")

    def _chip(self):
        van = self.v_van.get().strip()
        if van:
            self.van_txt.configure(text=f"vanilla: {Path(van).name}")
            self.van_dot.configure(fg=C["accent"])
        else:
            self.van_txt.configure(text="vanilla: autodetect")
            self.van_dot.configure(fg=C["faint"])

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
            self.log(f"Output non trovato: {p.resolve()}", "warn")

    def set_stage(self, name, state, secs=None):
        row = self.rows[name]
        row["state"] = state
        text, color = {
            "pending": ("—", C["dim"]), "running": ("● in corso", C["warn"]),
            "ok": ("✓", C["accent"]), "err": ("✗ errore", C["err"]),
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
            self.log("Nessuna fase selezionata.", "warn")
            return None
        if "sync" in stages and not self.v_mod.get().strip():
            self.log("Serve la cartella (o l'ID) della mod per sync.", "err")
            return None
        if not self.v_stg.get().strip() or not self.v_out.get().strip():
            self.log("Staging e Output non possono essere vuoti.", "err")
            return None
        if self.v_cap.get().strip():
            try:
                if int(self.v_cap.get()) < 1:
                    raise ValueError
            except ValueError:
                self.log("cap deve essere un intero positivo (o vuoto).", "err")
                return None
        o = self.opts()
        o = Opts(**{**asdict(o),
                    "workers": str(clamp_int(o.workers, 12, 1, 32)),
                    "cull": str(clamp_int(o.cull, 100, 0, 100_000))})
        self.v_workers.set(o.workers)
        self.v_cull.set(o.cull)
        if o.apply and any(s in WRITE for s in stages):
            if not messagebox.askokcancel(
                    "Applica modifiche",
                    "Le fasi in modalità APPLICA modificheranno i file in:\n\n"
                    f"{Path(o.staging).resolve()}\n\nContinuare?", parent=self.root):
                self.log("Annullato prima di iniziare.", "muted")
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
        o = self.opts()
        self._start([("check-log", stage_command("check-log", o))])

    def _start(self, cmds: "list[tuple[str, list[str]]]"):
        self.errors = self.warns = 0
        self.t0 = time.perf_counter()
        self.finished = False
        self._run_state(False)
        self.btn_cancel.configure(state="normal")
        self.clear_log()
        title = " → ".join(n for n, _ in cmds)
        self.log(f"hoi4port {__version__} — {title}", "cmd")
        self.bar.set(0, 0)
        self.phase.configure(text=f"Fase 1 di {len(cmds)} — avvio")
        for name in STAGE_NAMES:
            self.set_stage(name, "pending")
        # Tk variables must only be read on the main thread: snapshot the one
        # option the worker needs before handing it the queue.
        keep_going = bool(self.v_cont.get())
        threading.Thread(target=self._worker, args=(cmds, keep_going),
                         daemon=True).start()

    def _worker(self, cmds, keep_going: bool):
        total = len(cmds)
        for i, (name, cmd) in enumerate(cmds):
            self.q.put(("stage", (name, "running", i, total)))
            self.q.put(("log", ("  $ " + " ".join(cmd[3:] if cmd[1:2] == ["--cli"]
                                                  else cmd[2:]), "cmd")))
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
                self.q.put(("log", (f"Errore: {exc}", "err")))
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
                        self.phase.configure(text=f"Fase {i + 1} di {total} — {name}")
                elif kind == "done":
                    self.finished = True
                    self._run_state(True)
                    self.btn_cancel.configure(state="disabled")
                    dt = time.perf_counter() - self.t0
                    bad = any(r["state"] == "err" for r in self.rows.values())
                    done = sum(1 for r in self.rows.values() if r["state"] == "ok")
                    self.bar.set(done)
                    self.phase.configure(
                        text=f"{'Interrotto' if bad else 'Completato'} — "
                             f"{self.errors} errori · {self.warns} avvisi")
                    self.log(f"Fine: {dt:.1f} s — {self.errors} errori, "
                             f"{self.warns} avvisi.", "err" if bad else "ok")
        except queue.Empty:
            pass
        self._chip()
        self.root.after(80, self._pump)

    def cancel(self):
        if self.proc is not None:
            self.log("Interruzione richiesta …", "warn")
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
