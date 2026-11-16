"""Normalization: reshuffle assets onto vanilla layout + repair references.

- move: existing assets outside gfx/ reclassified by consumer (focus icons →
  gfx/interface/goals/, ideas → ideas/, decisions → decisions/, events →
  event_pictures/, portraits → leaders/, other UI → interface/).
- repair: broken references with a resolvable twin on disk (leading slash,
  different extension).
Every change is tracked in build/moves_ledger.json + normalize_manifest.json;
dry-run by default.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from . import fsutil, scanner

# (prefix of the referencing file, canonical destination) — first match wins.
# Direct: whoever contains the literal path.
DIRECT_RULES = (
    ("portraits/", "gfx/leaders/"),
    ("common/characters/", "gfx/leaders/"),
    ("common/national_focus/", "gfx/interface/goals/"),
    ("common/ideas/", "gfx/interface/ideas/"),
    ("common/decisions/", "gfx/interface/decisions/"),
    ("events/", "gfx/event_pictures/"),
)
# Two hops: where the sprite defined with that texturefile is USED.
SPRITE_RULES = DIRECT_RULES + (("interface/", "ui"),)
# Canonical subfolder per class: if the asset isn't there, it gets moved.
CANONICAL_DIR = {
    "gfx/leaders/": "gfx/leaders/",
    "gfx/interface/goals/": "gfx/interface/goals/",
    "gfx/interface/ideas/": "gfx/interface/ideas/",
    "gfx/interface/decisions/": "gfx/interface/decisions/",
    "gfx/event_pictures/": "gfx/event_pictures/",
    "ui": "gfx/interface/",
}

RE_TAG = re.compile(r"^GFX_([A-Z0-9]{3})_")
# Top-level folders that are already in vanilla layout (the engine resolves
# them by convention); 'gfx' is handled by CANONICAL_DIR below.
LAYOUT_TOP = ("map", "sound", "music")


def _unique_dest(dest: str, taken: set[str]) -> str:
    if dest.lower() not in taken:
        return dest
    parent, name = dest.rsplit("/", 1)
    stem, ext = Path(name).stem, Path(name).suffix
    i = 2
    while True:
        cand = f"{parent}/{stem}_{i}{ext}"
        if cand.lower() not in taken:
            return cand
        i += 1


def _match_rule(dirs: list[str], rules) -> str | None:
    for prefix, target in rules:
        if any(d.startswith(prefix) for d in dirs):
            return target
    return None


def _locs_dirs(locs: list[str]) -> list[str]:
    return [loc.rsplit(":", 1)[0].replace("\\", "/").lower() for loc in locs]


def _usage_dirs(gfx_refs: dict, name: str) -> list[str]:
    rec = gfx_refs.get(name, {})
    return _locs_dirs(rec.get("quoted", []) + rec.get("bare", []))


def classify_token(token: str, scan_result: dict, sprite_by_tex: dict[str, list[str]],
                   ref: dict) -> str | None:
    """Consumer class of the asset: direct rules first, then the sprite→use chain.

    Returns the canonical destination ('ui' = gfx/interface/) or None when the
    classification is missing/ambiguous (the asset is then not moved).
    """
    token_l = token.lower()
    dirs = _locs_dirs(ref["files"])

    direct = _match_rule(dirs, DIRECT_RULES)

    # chain: sprites defined with this texturefile → where they're used
    sprite_names = sprite_by_tex.get(token_l, [])
    classes: set[str] = set()
    for n in sprite_names:
        target = _match_rule(_usage_dirs(scan_result["gfx_refs"], n), SPRITE_RULES)
        if target:
            classes.add(target)
    if len(classes) > 1:
        return None  # conflicting uses: don't decide from the consumer

    if direct:
        return direct
    if len(classes) == 1:
        return next(iter(classes))
    if not sprite_names and any(d.startswith("interface/") for d in dirs):
        return "ui"  # path quoted directly by .gui/.gfx files
    return None


def plan_moves(root: Path, scan_result: dict) -> list[dict]:
    # Files already on disk count as taken: a planned destination that would
    # overwrite an existing asset must get a suffixed name instead (BUG-02).
    taken: set[str] = {
        p.relative_to(root).as_posix().lower()
        for p in root.rglob("*") if p.is_file()
    }
    sprite_by_tex = scanner.sprites_by_texture(scan_result)
    ref_by_lower = scanner.index_by_token_lower(scan_result)
    canonical_all = tuple(d for d in CANONICAL_DIR.values() if d != "gfx/interface/")
    dest_by_tl: dict[str, str] = {}
    moves: list[dict] = []
    for r in scan_result["references"]:
        if not r["exists"]:
            continue
        t = r["token"]
        t_norm = Path(t).as_posix()  # collapse //, preserve case
        tl = t_norm.lower()
        if tl.startswith(scanner.CONVENTION_DIRS):
            continue
        if t.split("/", 1)[0].lower() in LAYOUT_TOP:
            continue
        if not (root / t).is_file():
            continue  # resolved via the file's own folder (entities): don't touch
        if Path(t).suffix.lower() not in fsutil.ASSET_EXTS:
            continue  # the pipeline moves ONLY textures: never shader/audio/script
        if t_norm != t and tl.startswith(canonical_all + ("gfx/interface/",)):
            # already in canonical position: only clean up the path in text
            moves.append({"kind": "move", "src": t, "dst": t_norm, "cls": "norm",
                          "locs": r["files"]})
            continue
        if tl.startswith(canonical_all):
            continue  # already in another class's canonical folder: untouched
        cls = classify_token(t, scan_result, sprite_by_tex, r)
        if cls is None:
            continue
        canon = CANONICAL_DIR[cls]
        parts = t_norm.split("/")
        if (cls == "ui" and len(parts) > 2 and parts[0].lower() == "gfx"
                and not tl.startswith("gfx/interface/")):
            # preserve the whole structure: gfx/X/Y/z → gfx/interface/X/Y/z
            canon = "gfx/interface/" + "/".join(parts[1:-1]) + "/"
        if tl.startswith(canon):
            continue  # already in the right place
        if tl in dest_by_tl:
            # case variant of the same file: same destination, different rewrite
            moves.append({"kind": "move", "src": t, "dst": dest_by_tl[tl],
                          "cls": cls, "locs": r["files"]})
            continue
        dest_dir = canon
        if cls == "gfx/leaders/":
            # TAG: first from the sprite name, then from a 3-letter folder in the path
            tag = next(
                (m.group(1) for n in sprite_by_tex.get(tl, [])
                 if (m := RE_TAG.match(n))),
                None,
            )
            if not tag:
                tag = next(
                    (p for p in parts[1:-1] if re.fullmatch(r"[A-Z0-9]{3}", p)),
                    None,
                )
            dest_dir = f"gfx/leaders/{tag}/" if tag else "gfx/leaders/"
        dest = _unique_dest(f"{dest_dir}{t_norm.rsplit('/', 1)[-1]}", taken)
        taken.add(dest.lower())
        dest_by_tl[tl] = dest
        moves.append({"kind": "move", "src": t, "dst": dest, "cls": cls,
                      "locs": r["files"]})
        # case variants (same physical file): rewrite now, move is a no-op later
        for v in ref_by_lower.get(tl, []):
            if v["token"] != t:
                moves.append({"kind": "move", "src": v["token"], "dst": dest,
                              "cls": cls, "locs": v["files"]})
    return moves


def plan_repairs(root: Path, scan_result: dict) -> list[dict]:
    repairs: list[dict] = []
    for r in scan_result["references"]:
        if r["exists"] or r["in_vanilla"] or r["suspect"]:
            continue
        t = r["token"]
        if Path(t).suffix.lower() not in fsutil.ASSET_EXTS:
            continue
        cand = t.lstrip("/")
        if cand != t and (root / cand).exists():
            repairs.append({"kind": "repair", "src": t, "dst": cand, "locs": r["files"]})
            continue
        p = root / cand
        if p.parent.is_dir():
            for q in p.parent.iterdir():
                if (
                    q.stem.lower() == p.stem.lower()
                    and q.suffix.lower() in fsutil.ASSET_EXTS
                ):
                    repairs.append(
                        {
                            "kind": "repair",
                            "src": t,
                            "dst": q.relative_to(root).as_posix(),
                            "locs": r["files"],
                        }
                    )
                    break
    return repairs


def _rewrite_refs(root: Path, changes: list[dict], apply: bool) -> dict:
    file_map: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for c in changes:
        for loc in c["locs"]:
            file_map[loc.rsplit(":", 1)[0]].append((c["src"], c["dst"]))
    files_touched = 0
    total_subs = 0
    skipped: list[str] = []
    for rel, pairs in file_map.items():
        fp = root / rel
        try:
            text, enc = fsutil.read_text(fp)
        except (UnicodeDecodeError, OSError) as e:
            skipped.append(f"{rel} ({e})")
            continue
        subs = 0
        for old, new in sorted(pairs, key=lambda p: -len(p[0])):
            for o in (old, old.replace("/", "\\")):
                cnt = text.count(o)
                if cnt:
                    text = text.replace(o, new)
                    subs += cnt
        if subs:
            files_touched += 1
            total_subs += subs
            if apply:
                fsutil.write_text(fp, text, enc)
    return {"files_touched": files_touched, "substitutions": total_subs, "skipped": skipped}


def verify(root: Path, changes: list[dict], vanilla: Path | None = None) -> dict:
    post = scanner.scan(root, vanilla)
    by_token = scanner.index_by_token(post)
    src_residual = sorted(
        {c["src"] for c in changes} & set(by_token)
    )
    dst_broken = sorted(
        c["dst"] for c in changes
        if c["dst"] not in by_token or not by_token[c["dst"]]["exists"]
    )
    return {
        "src_still_present": src_residual,
        "dst_unresolved": dst_broken,
        "true_missing_after": len(post["missing_true"]),
    }


def run(root: Path, out: Path, apply: bool, vanilla: Path | None = None,
        do_moves: bool = True) -> dict:
    if not root.is_dir():
        raise SystemExit(f"Staging not found: {root} — run 'sync' first")
    out.mkdir(parents=True, exist_ok=True)
    print(f"Normalizing {root} ({'APPLY' if apply else 'dry-run'}) …")
    scan_result = scanner.scan(root, vanilla)

    moves = plan_moves(root, scan_result) if do_moves else []
    repairs = plan_repairs(root, scan_result)
    changes = moves + repairs
    print(f"Plan: {len(moves)} move(s), {len(repairs)} repair(s) — "
          f"{len({loc.rsplit(':', 1)[0] for c in changes for loc in c['locs']})} file(s) to rewrite")

    result: dict = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dry_run": not apply,
        "moves": moves,
        "repairs": repairs,
    }
    if apply:
        ledger_file = out / "moves_ledger.json"
        ledger: dict[str, str] = {}
        if ledger_file.exists():
            ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
        for c in moves:
            ledger[c["src"].lower()] = c["dst"]
        fsutil.write_json(ledger_file, ledger, indent=0)
        result["ledger_entries"] = len(ledger)

    if changes:
        rewrite_stats = _rewrite_refs(root, changes, apply)
        result["rewrite"] = rewrite_stats
        print(f"Reference rewrite: {rewrite_stats['files_touched']} file(s), "
              f"{rewrite_stats['substitutions']} substitutions")
        if apply:
            moved = 0
            for c in moves:
                src_p, dst_p = root / c["src"], root / c["dst"]
                if not src_p.exists():
                    continue  # already moved via the case variant of the same file
                if os.path.normcase(os.path.normpath(str(src_p))) == \
                        os.path.normcase(os.path.normpath(str(dst_p))):
                    continue  # path normalization in text only
                dst_p.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_p), str(dst_p))
                moved += 1
            print(f"Files moved: {moved}")
            result["verify"] = verify(root, changes, vanilla)
            print(f"Verify: residual src: {len(result['verify']['src_still_present'])}, "
                  f"unresolved dst: {len(result['verify']['dst_unresolved'])}, "
                  f"true missing after: {result['verify']['true_missing_after']}")
    else:
        print("No changes needed.")

    manifest = out / "normalize_manifest.json"
    fsutil.write_json(manifest, result, indent=2)
    print(f"Manifest: {manifest}")
    return result
