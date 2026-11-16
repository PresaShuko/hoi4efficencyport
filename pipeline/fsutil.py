"""FS utilities: encoding-aware I/O, JSON, file iteration, hashing, staging sync."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

# Paradox: UTF-8 (sometimes with BOM) or Windows-1252; latin-1 as a safety net.
TEXT_EXTS = {".txt", ".lua", ".gfx", ".gui"}
ASSET_EXTS = {".png", ".tga", ".dds", ".jpg", ".jpeg", ".bmp"}

_ROBOCOPY_OK = range(8)  # 0-7 = success (1 = files copied, etc.)


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
    # latin-1 maps every byte, so the loop above always returns.
    raise AssertionError(f"no Latin-1 fallback for {path}")


def write_text(path: Path, text: str, encoding: str) -> None:
    path.write_text(text, encoding=encoding, newline="")


def write_json(path: Path, data: Any, indent: int = 1) -> None:
    path.write_text(json.dumps(data, indent=indent, ensure_ascii=False),
                    encoding="utf-8")


def iter_files(root: Path, exts: Iterable[str]) -> Iterator[Path]:
    """Files under ``root`` whose suffix is in ``exts`` (case-insensitive).

    os.scandir instead of Path.rglob: the dirent already carries the file
    type, so no per-entry stat (rglob+is_file is ~16x slower on Windows).
    Directory symlinks are not followed (same as Path.rglob); file symlinks
    are yielded.
    """
    wanted = {e.lower() for e in exts}
    stack = [os.fspath(root)]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file() and os.path.splitext(entry.name)[1].lower() in wanted:
                        yield Path(entry.path)
                except OSError:
                    continue


def iter_text_files(root: Path) -> Iterator[Path]:
    yield from iter_files(root, TEXT_EXTS)


def iter_asset_files(root: Path) -> Iterator[Path]:
    yield from iter_files(root, ASSET_EXTS)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_size(root: Path) -> int:
    """Total size of the regular files under ``root`` (scandir, no rglob)."""
    total = 0
    stack = [os.fspath(root)]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
    return total


def install_stamp(root: Path) -> str:
    """Cache key of a game install: content hash of ``launcher-settings.json``.

    Steam rewrites that file on every game update, so a stale baseline is
    detected; hashing the content also survives copies of the install.
    """
    try:
        return hashlib.sha256((root / "launcher-settings.json").read_bytes()
                              ).hexdigest()[:16]
    except OSError:
        return "unknown"


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
            # robocopy writes in the console's OEM code page (cp850 on it-IT),
            # not UTF-8: decoding as UTF-8 mangled "Velocità"/"Terminato".
            encoding="oem",
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
