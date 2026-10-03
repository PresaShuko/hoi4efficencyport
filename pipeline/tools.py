"""Resolution of the external texconv tool — one place, no hardcoded paths
in calling code.

Resolution order:
1. the ``HOI4PORT_TEXCONV`` environment variable (explicit exe path);
2. the binary shipped with the package (``pipeline/bin/texconv.exe``, also
   inside the PyInstaller executable);
3. ``texconv`` on the PATH.

On Linux/macOS texconv.exe is a Windows binary: if Wine is installed it is
invoked transparently (disable with ``--no-wine`` or ``HOI4PORT_NO_WINE=1``).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ENV_TEXCONV = "HOI4PORT_TEXCONV"
ENV_NO_WINE = "HOI4PORT_NO_WINE"

_NOT_FOUND = f"""texconv not found. Possible routes, in order:
  1. set {ENV_TEXCONV}=<path to texconv.exe>;
  2. texconv ships in pipeline/bin/ of the package (install the full wheel);
  3. put texconv.exe on the PATH.
Note: texconv is a Windows binary; on Linux/macOS install Wine to use it.
Pure-Python commands (sync, scan, normalize, analyze, meshscan, entityfix,
validate) work fine without texconv."""


def _bundled() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller (onefile and onedir)
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "pipeline" / "bin" / "texconv.exe"
    return Path(__file__).resolve().parent / "bin" / "texconv.exe"


def texconv_path() -> Path | None:
    """Path to the texconv executable, or None if it cannot be found."""
    env = os.environ.get(ENV_TEXCONV)
    if env:
        p = Path(env)
        if p.is_file():
            return p.resolve()
        raise SystemExit(f"{ENV_TEXCONV} is set but the file does not exist: {p}")
    bundled = _bundled()
    if bundled.is_file():
        return bundled
    which = shutil.which("texconv")
    return Path(which) if which else None


def texconv_command(no_wine: bool = False) -> list[str]:
    """Full argv to invoke texconv; prepends Wine outside Windows.

    Raises SystemExit with an actionable message if texconv cannot be found.
    """
    exe = texconv_path()
    if exe is None:
        raise SystemExit(_NOT_FOUND)
    if os.name == "nt" or no_wine or os.environ.get(ENV_NO_WINE, "") in ("1", "true"):
        return [str(exe)]
    wine = shutil.which("wine") or shutil.which("wine64")
    if wine:
        return [wine, str(exe)]
    return [str(exe)]
