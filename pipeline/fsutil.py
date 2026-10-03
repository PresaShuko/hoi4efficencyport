"""FS utilities: encoding-aware reading, staging sync via robocopy, hashing."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

# Paradox: UTF-8 (sometimes with BOM) or Windows-1252; latin-1 as a safety net.
TEXT_EXTS = {".txt", ".lua", ".gfx", ".gui"}
ASSET_EXTS = {".png", ".tga", ".dds", ".jpg", ".jpeg", ".bmp"}

_ROBOCOPY_OK = set(range(0, 8))  # 0-7 = success (1 = files copied, etc.)


def read_text(path: Path) -> tuple[str, str]:
    """Returns (content, encoding). 'utf-8-sig' ONLY if the file actually has a
    BOM: rewriting a BOM-less file with utf-8-sig adds an artificial BOM that
    the Clausewitz .gfx parser rejects."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        encodings = ("utf-8-sig", "cp1252", "latin-1")
    else:
        encodings = ("utf-8", "cp1252", "latin-1")
    for enc in encodings:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", raw, 0, 1, f"no valid encoding: {path}")


def write_text(path: Path, text: str, encoding: str) -> None:
    path.write_text(text, encoding=encoding, newline="")


def iter_text_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in TEXT_EXTS:
            yield p


def iter_asset_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in ASSET_EXTS:
            yield p


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_staging(src: Path, dst: Path) -> None:
    """Mirror workshop → staging. robocopy /MIR is native, multithreaded and handles GBs."""
    if not src.is_dir():
        raise SystemExit(f"Source mod not found: {src}")
    if shutil.which("robocopy"):
        result = subprocess.run(
            [
                "robocopy", str(src), str(dst),
                "/MIR", "/MT:16", "/R:2", "/W:2",
                "/NFL", "/NDL", "/NP",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode not in _ROBOCOPY_OK:
            tail = "\n".join(result.stdout.splitlines()[-15:])
            raise SystemExit(f"robocopy failed (code {result.returncode}):\n{tail}")
        print("\n".join(result.stdout.splitlines()[-12:]))
    else:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        print(f"Copied (copytree fallback): {dst}")
