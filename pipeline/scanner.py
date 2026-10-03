"""Single scanner: replaces trova_path_tutto.py, trovaPercorsiFileLogica.py and trovaPath.py.

Collects from every text file in the mod:
- asset path references (quoted) with verified existence
- GFX_* references and spriteType definitions (to find missing GFX)
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import fsutil

QUOTED_RE = re.compile(r'"([^"\r\n]+)"')
GFX_NAME_RE = re.compile(r"\bGFX_[A-Za-z0-9_]+")
SUSPECT_RE = re.compile(r"https?:|%|\[|\]")
SPRITE_BLOCK_RE = re.compile(
    r"\b(?:corneredTileSpriteType|spriteType)\s*=\s*\{([^{}]*)\}", re.S | re.I
)
NAME_RE = re.compile(r'\bname\s*=\s*"([^"]+)"')
NOFRAMES_RE = re.compile(r"\bnoOfFrames\s*=\s*(\d+)", re.I)


def _looks_like_path(token: str) -> bool:
    if "/" in token or "\\" in token:
        return True
    return Path(token).suffix.lower() in fsutil.ASSET_EXTS


def _resolve(root: Path, token: str) -> Path:
    p = Path(token)
    return p if p.is_absolute() else root / p


def _norm(token: str) -> str:
    return token.replace("\\", "/")


def strip_comments(text: str) -> str:
    """Strip #comments (outside quotes): .gfx files are full of commented-out blocks."""
    out = []
    for line in text.splitlines():
        in_q = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == '"':
                in_q = not in_q
            elif ch == "#" and not in_q:
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


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
            for m in SPRITE_BLOCK_RE.finditer(strip_comments(text)):
                nm = NAME_RE.search(m.group(1))
                if nm:
                    names.add(nm.group(1))
    return names


def scan(root: Path, vanilla: Path | None = None) -> dict:
    references: dict[str, dict] = {}   # normalized token -> record
    gfx_refs: dict[str, list[str]] = {}  # sprite name -> ["file:line", ...]
    sprite_types: dict[str, dict] = {}   # name -> {texturefile, noOfFrames, file}
    enc_stats: Counter[str] = Counter()
    unreadable: list[str] = []

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
        text = strip_comments(text)
        local_defs: set[str] = set()   # sprite names DEFINED by this file

        if fp.suffix.lower() == ".gfx":
            # spriteType blocks span multiple lines: match on the whole text.
            # Names defined here are recorded and excluded from gfx_refs
            # below: a definition is not a usage (BUG-06).
            for m in SPRITE_BLOCK_RE.finditer(text):
                body = m.group(1)
                nm = NAME_RE.search(body)
                if not nm:
                    continue
                tf = re.search(r'texturefile\s*=\s*"([^"]+)"', body, re.I)
                nf = NOFRAMES_RE.search(body)
                sprite_types[nm.group(1)] = {
                    "texturefile": _norm(tf.group(1)) if tf else None,
                    "noOfFrames": int(nf.group(1)) if nf else None,
                    "file": rel,
                }
                local_defs.add(nm.group(1))

        for lineno, line in enumerate(text.splitlines(), 1):
            for m in QUOTED_RE.finditer(line):
                token = m.group(1)
                if _looks_like_path(token):
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
                    if ref["exists"] is None:
                        # PDX resolves paths from the mod root; entity .gfx files
                        # also use paths relative to their own folder.
                        ref["exists"] = (
                            _resolve(root, token).exists()
                            or (fp.parent / token).exists()
                        )
                        if (
                            not ref["exists"]
                            and vanilla is not None
                            and not Path(token).is_absolute()
                        ):
                            ref["in_vanilla"] = (vanilla / token).exists()
            quoted_spans = [m.span() for m in QUOTED_RE.finditer(line)]
            for m in GFX_NAME_RE.finditer(line):
                if m.group(0) in local_defs:
                    continue  # this .gfx file DEFINES it: not a usage (BUG-06)
                rec = gfx_refs.setdefault(m.group(0), {"quoted": [], "bare": []})
                s, e = m.span()
                bucket = (
                    "quoted"
                    if any(s >= qs and e <= qe for qs, qe in quoted_spans)
                    else "bare"
                )
                rec[bucket].append(f"{rel}:{lineno}")

    missing_paths = [r for r in references.values() if r["exists"] is False]
    defined = set(sprite_types)
    referenced_names = set(gfx_refs)
    vanilla_sprites = sprite_defs(vanilla) if vanilla is not None else set()
    gfx_missing = sorted(referenced_names - defined - vanilla_sprites)
    gfx_covered_vanilla = sorted((referenced_names & vanilla_sprites) - defined)

    def _has_asset_ext(tok: str) -> bool:
        return Path(tok).suffix.lower() in fsutil.ASSET_EXTS

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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


def vanilla_baseline(vanilla: Path | None, out_file: Path, skip: bool) -> str:
    """SHA256 of every vanilla asset (gfx/ + map/) for F3-phase dedup."""
    if skip or vanilla is None or not vanilla.is_dir():
        return "skipped"
    if out_file.exists():
        return "cached"
    hashes: dict[str, str] = {}
    for sub in ("gfx", "map"):
        base = vanilla / sub
        if not base.is_dir():
            continue
        for p in fsutil.iter_asset_files(base):
            hashes[f"{sub}/{p.relative_to(base).as_posix()}"] = fsutil.sha256_file(p)
    out_file.write_text(json.dumps(hashes, indent=0), encoding="utf-8")
    return f"{len(hashes)} file"


def write_report(result: dict, out_file: Path) -> None:
    refs = result["references"]
    missing = [r for r in refs if not r["exists"]]
    fallback = len(result["missing_fallback_vanilla"])
    true_miss = len(result["missing_true"])
    suspect = len(result["missing_suspect"])
    gfx_missing_static = len(result["gfx_missing_static"])
    lines = [
        f"Scan {result['generated_at']} — root: {result['root']}",
        f"Text files: {sum(result['files_by_encoding'].values())} "
        f"(encodings: {result['files_by_encoding']})",
        f"Referenced asset paths (unique): {len(refs)}",
        f"  ├─ existing in mod: {len(refs) - len(missing)}",
        f"  ├─ vanilla fallback (legit, don't touch): {fallback}",
        f"  ├─ suspects (non-paths, ignore): {suspect}",
        f"  ├─ TRUE missing: {true_miss}",
        f"  └─ noise (quotes without extension): {len(result['missing_noise'])}",
        f"spriteType defined (mod): {len(result['sprite_types'])} "
        f"+ vanilla: {result['gfx_vanilla_defs_count']}",
        f"Referenced GFX_*: {len(result['gfx_refs'])}",
        f"  ├─ missing covered by vanilla: {len(result['gfx_covered_by_vanilla'])}",
        f"  ├─ missing with static consumption (quoted): {gfx_missing_static}",
        f"  └─ missing dynamic-only (bare): {len(result['gfx_missing']) - gfx_missing_static}",
        f"spriteType never used: {len(result['gfx_unused_defs'])}",
    ]
    true_list = result["missing_true"]
    if true_list:
        lines.append("\n--- True missing (first 50) ---")
        by_token = {r["token"]: r for r in missing}
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
