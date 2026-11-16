"""Single scanner for mod text files: asset references, spriteType definitions
and GFX_* usage across a mod tree.

Collects from every text file in the mod:
- asset path references (quoted) with verified existence
- GFX_* references and spriteType definitions (to find missing GFX)
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from . import fsutil

QUOTED_RE = re.compile(r'"([^"\r\n]+)"')
GFX_NAME_RE = re.compile(r"\bGFX_[A-Za-z0-9_]+")
SUSPECT_RE = re.compile(r"https?:|%|\[|\]")
SPRITE_BLOCK_RE = re.compile(
    r"\b(?:corneredTileSpriteType|spriteType)\s*=\s*\{([^{}]*)\}", re.S | re.I
)
NAME_RE = re.compile(r'\bname\s*=\s*"([^"]+)"')
TEXTUREFILE_RE = re.compile(r'texturefile\s*=\s*"([^"]+)"', re.I)
NOFRAMES_RE = re.compile(r"\bnoOfFrames\s*=\s*(\d+)", re.I)

# Folders the engine resolves by path convention (flags, models, entities,
# fonts): never reclassified, deduped or deleted by the pipeline.
CONVENTION_DIRS = ("gfx/flags/", "gfx/models/", "gfx/entities/", "gfx/fonts/")
# Convention folders outside the 2D texture analysis/conversion.
TEXTURE_SKIP_DIRS = ("gfx/models/", "gfx/entities/", "gfx/flags/")


def _suffix(token: str) -> str:
    """Last-component suffix, with the same semantics as ``Path(token).suffix``
    (empty for '.png' dotfiles, trailing separators and names without a dot)."""
    name = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    i = name.rfind(".")
    return name[i:].lower() if i > 0 else ""


def _has_asset_ext(token: str) -> bool:
    return _suffix(token) in fsutil.ASSET_EXTS


def _looks_like_path(token: str) -> bool:
    if "/" in token or "\\" in token:
        return True
    return _has_asset_ext(token)


# --- existence index -----------------------------------------------------------
# One scandir walk replaces one stat() per reference token. Path.exists() costs
# ~20 us on Windows (Defender); a mod with 20k references pays ~0.4 s in stats.
# Keys are relative posix paths; case folding follows the platform default.

_CASE_INSENSITIVE = os.name == "nt" or sys.platform == "darwin"


def _index_key(rel: str) -> str:
    return rel.lower() if _CASE_INSENSITIVE else rel


def _plain_rel(token: str) -> str | None:
    """Normalized relative path, or None when a real stat() is required.

    Absolute paths, drive letters, leading separators and '..' keep the
    original Path.exists() behaviour (the index covers only the tree).
    """
    t = token.replace("\\", "/")
    if not t or t[0] == "/" or (len(t) > 1 and t[1] == ":"):
        return None
    if ".." in t.split("/"):
        return None
    return posixpath.normpath(t)


def _walk_index(root: Path) -> set[str]:
    """Relative keys of every file and directory under ``root``."""
    keys: set[str] = set()
    base = os.fspath(root)
    stack = [base]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    recurse = entry.is_dir(follow_symlinks=False)
                    if not (recurse or entry.is_file() or entry.is_dir()):
                        continue  # broken symlink / special file: not "exists"
                except OSError:
                    continue
                keys.add(_index_key(os.path.relpath(entry.path, base)
                                    .replace(os.sep, "/")))
                if recurse:
                    stack.append(entry.path)
    return keys


def _norm(token: str) -> str:
    return token.replace("\\", "/")


def _strip_line(line: str) -> str:
    """Remove a trailing #comment (outside quotes) from one line."""
    if "#" not in line:  # fast path: most lines carry no comment at all
        return line
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == "#" and not in_q:
            return line[:i]
    return line


def strip_comments(text: str) -> str:
    """Strip #comments (outside quotes): .gfx files are full of commented-out blocks."""
    return "\n".join(_strip_line(line) for line in text.splitlines())


def sprite_defs_in_text(text: str) -> dict[str, dict]:
    """name → {texturefile, noOfFrames} for every spriteType block in ``text``."""
    defs: dict[str, dict] = {}
    for m in SPRITE_BLOCK_RE.finditer(text):
        body = m.group(1)
        nm = NAME_RE.search(body)
        if not nm:
            continue
        tf = TEXTUREFILE_RE.search(body)
        nf = NOFRAMES_RE.search(body)
        defs[nm.group(1)] = {
            "texturefile": _norm(tf.group(1)) if tf else None,
            "noOfFrames": int(nf.group(1)) if nf else None,
        }
    return defs


def sprite_defs(root: Path, subdirs=("interface", "gfx")) -> set[str]:
    """spriteType names defined in the .gfx files of a tree (mod or vanilla)."""
    names: set[str] = set()
    for sd in subdirs:
        base = root / sd
        if not base.is_dir():
            continue
        for fp in base.rglob("*.gfx"):
            try:
                text, _ = fsutil.read_text(fp)
            except (UnicodeDecodeError, OSError):
                continue
            names.update(sprite_defs_in_text(strip_comments(text)))
    return names


_SPRITE_CACHE_VERSION = 1


def vanilla_sprite_defs(vanilla: Path) -> set[str]:
    """sprite_defs(vanilla) cached per install stamp.

    The vanilla tree is constant between game updates, but scan runs in every
    pipeline stage: re-parsing thousands of .gfx costs seconds per command.
    A missing launcher-settings.json (stamp "unknown") disables the cache.
    """
    stamp = fsutil.install_stamp(vanilla)
    if stamp == "unknown":
        return sprite_defs(vanilla)
    cache = fsutil.cache_dir() / f"vanilla_sprites_v{_SPRITE_CACHE_VERSION}_{stamp}.json"
    try:
        return set(json.loads(cache.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    names = sprite_defs(vanilla)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        fsutil.write_json(cache, sorted(names), indent=0)
    except OSError:
        pass
    return names


def scan(root: Path, vanilla: Path | None = None) -> dict:
    references: dict[str, dict] = {}   # normalized token -> record
    gfx_refs: dict[str, dict] = {}     # sprite name -> {quoted: [...], bare: [...]}
    sprite_types: dict[str, dict] = {}   # name -> {texturefile, noOfFrames, file}
    enc_stats: Counter[str] = Counter()
    unreadable: list[str] = []
    root_index = _walk_index(root)
    vanilla_index: set[str] | None = None

    def _exists(referencer: Path, token: str) -> bool:
        """Same answer as (root/token).exists() or (referencer.parent/token).exists()."""
        rel = _plain_rel(token)
        if rel is None:
            return (root / token).exists() or (referencer.parent / token).exists()
        if _index_key(rel) in root_index:
            return True
        # entity .gfx files resolve paths relative to their own folder
        parent = referencer.parent
        if parent != root:
            ent = _plain_rel(parent.relative_to(root).as_posix() + "/" + token)
            if ent is not None and _index_key(ent) in root_index:
                return True
        return False

    def _in_vanilla(token: str) -> bool:
        nonlocal vanilla_index
        rel = _plain_rel(token)
        if rel is None:
            return (vanilla / token).exists()
        if vanilla_index is None:
            vanilla_index = _walk_index(vanilla)
        return _index_key(rel) in vanilla_index

    for fp in sorted(fsutil.iter_text_files(root)):
        rel = fp.relative_to(root).as_posix()
        try:
            text, enc = fsutil.read_text(fp)
        except UnicodeDecodeError:
            unreadable.append(rel)
            continue
        except OSError as e:
            unreadable.append(f"{rel} ({e})")
            continue
        enc_stats[enc] += 1
        # One split for the whole file: .gfx sprite blocks need the joined
        # text, the line loop below uses the same stripped lines.
        stripped = [_strip_line(line) for line in text.splitlines()]
        local_defs: set[str] = set()   # sprite names DEFINED by this file

        if fp.suffix.lower() == ".gfx":
            # spriteType blocks span multiple lines: match on the whole text.
            # Names defined here are recorded and excluded from gfx_refs
            # below: a definition is not a usage (BUG-06).
            for name, meta in sprite_defs_in_text("\n".join(stripped)).items():
                sprite_types[name] = {**meta, "file": rel}
                local_defs.add(name)

        for lineno, line in enumerate(stripped, 1):
            # One pass for the quoted tokens; the spans are reused by the
            # GFX_* classification below (quoted vs bare). Lines without a
            # quote (or without "GFX_") skip the corresponding regex.
            quoted = (
                [(m.group(1), m.span()) for m in QUOTED_RE.finditer(line)]
                if '"' in line else []
            )
            for token, _ in quoted:
                if not _looks_like_path(token):
                    continue
                key = _norm(token)
                ref = references.setdefault(
                    key,
                    {
                        "token": key,
                        "files": [],
                        "exists": None,
                        "in_vanilla": False,
                        "suspect": bool(SUSPECT_RE.search(key)),
                    },
                )
                ref["files"].append(f"{rel}:{lineno}")
                if ref["exists"] is not None:
                    continue
                # PDX resolves paths from the mod root; entity .gfx files
                # also use paths relative to their own folder.
                ref["exists"] = _exists(fp, token)
                if (
                    not ref["exists"]
                    and vanilla is not None
                    and not Path(token).is_absolute()
                ):
                    ref["in_vanilla"] = _in_vanilla(token)
            if "GFX_" in line:
                for m in GFX_NAME_RE.finditer(line):
                    if m.group(0) in local_defs:
                        continue  # this .gfx file DEFINES it: not a usage (BUG-06)
                    rec = gfx_refs.setdefault(m.group(0), {"quoted": [], "bare": []})
                    start, end = m.span()
                    bucket = (
                        "quoted"
                        if any(qs <= start and end <= qe for _, (qs, qe) in quoted)
                        else "bare"
                    )
                    rec[bucket].append(f"{rel}:{lineno}")

    missing_paths = [r for r in references.values() if r["exists"] is False]
    defined = set(sprite_types)
    referenced_names = set(gfx_refs)
    vanilla_sprites = vanilla_sprite_defs(vanilla) if vanilla is not None else set()
    gfx_missing = sorted(referenced_names - defined - vanilla_sprites)
    gfx_covered_vanilla = sorted((referenced_names & vanilla_sprites) - defined)

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "root": str(root),
        "files_by_encoding": dict(enc_stats),
        "unreadable": unreadable,
        "references": sorted(references.values(), key=lambda r: r["token"]),
        "missing_fallback_vanilla": sorted(
            r["token"] for r in missing_paths if r["in_vanilla"]
        ),
        "missing_true": sorted(
            r["token"]
            for r in missing_paths
            if not r["in_vanilla"]
            and not r["suspect"]
            and _has_asset_ext(r["token"])
        ),
        "missing_noise": sorted(
            r["token"]
            for r in missing_paths
            if not r["in_vanilla"]
            and not r["suspect"]
            and not _has_asset_ext(r["token"])
        ),
        "missing_suspect": sorted(r["token"] for r in missing_paths if r["suspect"]),
        "sprite_types": dict(sorted(sprite_types.items())),
        "gfx_vanilla_defs_count": len(vanilla_sprites),
        "gfx_covered_by_vanilla": gfx_covered_vanilla,
        "gfx_refs": dict(sorted(gfx_refs.items())),
        "gfx_missing": gfx_missing,
        "gfx_missing_static": [n for n in gfx_missing if gfx_refs[n]["quoted"]],
        "gfx_unused_defs": sorted(defined - referenced_names),
    }


# --- indexes over a scan result (shared by normalize/analyze/cutover) -----------

def index_by_token(scan_result: dict) -> dict[str, dict]:
    return {r["token"]: r for r in scan_result["references"]}


def index_by_token_lower(scan_result: dict) -> dict[str, list[dict]]:
    """token.lower() → every reference with that exact (normalized) spelling."""
    index: dict[str, list[dict]] = {}
    for r in scan_result["references"]:
        index.setdefault(r["token"].lower(), []).append(r)
    return index


def sprites_by_texture(scan_result: dict) -> dict[str, list[str]]:
    """texturefile.lower() → sprite names defined with that texture."""
    index: dict[str, list[str]] = {}
    for name, sprite in scan_result["sprite_types"].items():
        tex = sprite.get("texturefile")
        if tex:
            index.setdefault(tex.lower(), []).append(name)
    return index


# --- reporting -----------------------------------------------------------------

def summary(result: dict) -> dict:
    """Counts used by both the report file and the CLI output."""
    refs = result["references"]
    missing = [r for r in refs if not r["exists"]]
    return {
        "text_files": sum(result["files_by_encoding"].values()),
        "encodings": result["files_by_encoding"],
        "references": len(refs),
        "existing": len(refs) - len(missing),
        "missing": missing,
        "missing_count": len(missing),
        "fallback_vanilla": len(result["missing_fallback_vanilla"]),
        "missing_true": len(result["missing_true"]),
        "missing_noise": len(result["missing_noise"]),
        "missing_suspect": len(result["missing_suspect"]),
        "sprite_types": len(result["sprite_types"]),
        "gfx_vanilla_defs": result["gfx_vanilla_defs_count"],
        "gfx_refs": len(result["gfx_refs"]),
        "gfx_missing": len(result["gfx_missing"]),
        "gfx_missing_static": len(result["gfx_missing_static"]),
        "gfx_covered_vanilla": len(result["gfx_covered_by_vanilla"]),
        "gfx_unused": len(result["gfx_unused_defs"]),
        "unreadable": len(result["unreadable"]),
    }


def write_report(result: dict, out_file: Path) -> None:
    s = summary(result)
    lines = [
        f"Scan {result['generated_at']} — root: {result['root']}",
        f"Text files: {s['text_files']} (encodings: {s['encodings']})",
        f"Referenced asset paths (unique): {s['references']}",
        f"  ├─ existing in mod: {s['existing']}",
        f"  ├─ vanilla fallback (legit, don't touch): {s['fallback_vanilla']}",
        f"  ├─ suspects (non-paths, ignore): {s['missing_suspect']}",
        f"  ├─ TRUE missing: {s['missing_true']}",
        f"  └─ noise (quotes without extension): {s['missing_noise']}",
        f"spriteType defined (mod): {s['sprite_types']} + vanilla: {s['gfx_vanilla_defs']}",
        f"Referenced GFX_*: {s['gfx_refs']}",
        f"  ├─ missing covered by vanilla: {s['gfx_covered_vanilla']}",
        f"  ├─ missing with static consumption (quoted): {s['gfx_missing_static']}",
        f"  └─ missing dynamic-only (bare): {s['gfx_missing'] - s['gfx_missing_static']}",
        f"spriteType never used: {s['gfx_unused']}",
    ]
    true_list = result["missing_true"]
    if true_list:
        lines.append("\n--- True missing (first 50) ---")
        by_token = {r["token"]: r for r in s["missing"]}
        lines += [
            f"  {t}  (from {by_token[t]['files'][0]})"
            for t in true_list[:50]
        ]
        if len(true_list) > 50:
            lines.append(f"  … {len(true_list) - 50} more in scan.json")
    static_names = set(result["gfx_missing_static"])
    if static_names:
        lines.append("\n--- Missing static GFX_* (first 30) ---")
        for n in [n for n in result["gfx_missing"] if n in static_names][:30]:
            loc = result["gfx_refs"][n]["quoted"][0]
            lines.append(f"  {n}  (from {loc})")
    out_file.write_text("\n".join(lines), encoding="utf-8")


def vanilla_baseline(vanilla: Path | None, out_file: Path, skip: bool) -> str:
    """SHA256 of every vanilla asset (gfx/ + map/) for F3-phase dedup.

    The cache carries the install stamp: after a game update the hashes are
    recomputed instead of being trusted blindly.
    """
    if skip or vanilla is None or not vanilla.is_dir():
        return "skipped"
    stamp = fsutil.install_stamp(vanilla)
    if out_file.exists():
        try:
            cached = json.loads(out_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict) and cached.get("stamp") == stamp:
            return "cached"
    hashes: dict[str, str] = {}
    for sub in ("gfx", "map"):
        base = vanilla / sub
        if not base.is_dir():
            continue
        for p in fsutil.iter_asset_files(base):
            hashes[f"{sub}/{p.relative_to(base).as_posix()}"] = fsutil.sha256_file(p)
    fsutil.write_json(out_file, {"stamp": stamp, "hashes": hashes}, indent=0)
    return f"{len(hashes)} file"
