"""FS utilities: encoding-aware I/O, JSON, file iteration, hashing, staging sync."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
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


def iter_files(root: Path, exts: Iterable[str] | None = None) -> Iterator[Path]:
    """Files under ``root`` whose suffix is in ``exts`` (case-insensitive).

    ``exts=None`` yields every file (used by the mod merge).

    os.scandir instead of Path.rglob: the dirent already carries the file
    type, so no per-entry stat (rglob+is_file is ~16x slower on Windows).
    Directory symlinks are not followed (same as Path.rglob); file symlinks
    are yielded.
    """
    wanted = None if exts is None else {e.lower() for e in exts}
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
                    elif entry.is_file() and (wanted is None or
                                              os.path.splitext(entry.name)[1].lower() in wanted):
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


def cache_dir() -> Path:
    """Per-user cache directory for install-scoped and UI state."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "hoi4port"


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


def _robocopy(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["robocopy", *args],
        capture_output=True,
        text=True,
        # robocopy writes in the console's OEM code page (cp850 on it-IT),
        # not UTF-8: decoding as UTF-8 mangled "Velocità"/"Terminato".
        encoding="oem",
        errors="replace",
    )


def _robocopy_ok(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode not in _ROBOCOPY_OK:
        tail = "\n".join(result.stdout.splitlines()[-15:])
        raise SystemExit(f"robocopy failed (code {result.returncode}):\n{tail}")
    print("\n".join(result.stdout.splitlines()[-12:]))


def _overridden(src: Path, dst: Path) -> list[str]:
    """Files of `src` that already exist in `dst` (i.e. this mod overrides)."""
    hits: list[str] = []
    for f in iter_files(src):
        rel = f.relative_to(src)
        if (dst / rel).exists():
            hits.append(rel.as_posix())
    return sorted(hits)


def merge_staging(sources: list[Path], dst: Path) -> dict[str, Any]:
    """Overlay mods into the staging tree in load order (later wins).

    Same semantics as enabling several mods in HoI4: a file present in a later
    mod replaces the earlier one entirely (no per-key merging). The first sync
    mirrors with /MIR so stale files disappear; later sources only add and
    overwrite, never purge, so a mod cannot delete another mod's files.
    """
    if not sources:
        raise SystemExit("Source mod not found: no mod given")
    for src in sources:
        if not src.is_dir():
            raise SystemExit(f"Source mod not found: {src}")
    report: dict[str, Any] = {"sources": [str(s) for s in sources],
                              "overridden": {}, "files": 0}
    if shutil.which("robocopy"):
        for i, src in enumerate(sources):
            if i == 0:
                _robocopy_ok(_robocopy([str(src), str(dst), "/MIR", "/MT:16",
                                        "/R:2", "/W:2", "/NFL", "/NDL", "/NP"]))
            else:
                hits = _overridden(src, dst)
                if hits:
                    report["overridden"][str(src)] = hits
                # /IS: robocopy skips "same" files (equal size + timestamp) by
                # default, which would silently keep the earlier mod's bytes for
                # a later file that happens to match in size and mtime.
                _robocopy_ok(_robocopy([str(src), str(dst), "/E", "/IS", "/MT:16",
                                        "/R:2", "/W:2", "/NFL", "/NDL", "/NP"]))
    else:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(sources[0], dst)
        print(f"Copied (copytree fallback): {dst}")
        for src in sources[1:]:
            hits = _overridden(src, dst)
            if hits:
                report["overridden"][str(src)] = hits
            shutil.copytree(src, dst, dirs_exist_ok=True)
            print(f"Overlaid: {src}")
    report["files"] = sum(1 for _ in iter_files(dst))
    if report["overridden"]:
        total = sum(len(v) for v in report["overridden"].values())
        print(f"Merged {len(sources)} mods — {total} file(s) overridden by later mods")
    elif len(sources) > 1:
        print(f"Merged {len(sources)} mods — no overlapping files")
    return report


def sync_staging(src: Path | list[Path], dst: Path) -> dict[str, Any]:
    """Mirror one mod (or merge several) into staging."""
    sources = [src] if isinstance(src, Path) else list(src)
    return merge_staging(sources, dst)
