"""F5 — Cutover: rewrites references to .dds, applies dedup and removes the
converted originals. Consumes build/asset_plan.json + convert_manifest.json.

Logical order:
1. extension mapping: .png/.tga/... tokens → .dds (if the twin exists in the mod)
2. intra-mod dedup: duplicates point at the canonical member (preferring .dds)
3. deletions: converted sources, dedup losers, vanilla-identical overrides
Vanilla-identical deletions are safe: the engine fallback loads the same file
from the base install. Post-apply verification is included in the manifest.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from . import fsutil, normalize, scanner

NON_DDS = fsutil.ASSET_EXTS - {".dds"}

# The engine resolves these assets with its own rules: no extension rewrite.
REWRITE_EXCLUDE = ("gfx/fonts/",)
# Convention-loaded folders: never dedup/delete (path-based lookup).
SKIP_DIRS = ("gfx/flags/", "gfx/models/", "gfx/entities/", "gfx/fonts/")


def _redirect(mapping: dict[str, str], frm: str, to: str) -> None:
    if frm == to:
        return
    for s in [k for k, v in mapping.items() if v == frm]:
        mapping[s] = to
    mapping[frm] = to


def plan_cutover(root: Path, scan_result: dict, plan: dict, conv_ok: set[str],
                 do_dedup: bool = True, do_vanilla_dedup: bool = True):
    skip_prefixes = SKIP_DIRS
    ref_by_token = {r["token"]: r for r in scan_result["references"]}
    ref_by_lower_lists: dict[str, list[dict]] = defaultdict(list)
    for r in scan_result["references"]:
        ref_by_lower_lists[r["token"].lower()].append(r)

    def locs_for(t: str) -> list[str]:
        out: list[str] = []
        for r in ref_by_lower_lists.get(t.lower(), []):
            out += r["files"]
        return out

    def refn(t: str) -> int:
        return len(locs_for(t))

    mapping: dict[str, str] = {}
    for token, r in ref_by_token.items():
        if not r["exists"] or token.lower().startswith(REWRITE_EXCLUDE):
            continue
        p = Path(token)
        if p.suffix.lower() not in NON_DDS:
            continue
        dds = p.with_suffix(".dds").as_posix()
        if dds != token and (root / dds).exists():
            mapping[token] = dds

    deletions: set[str] = set()
    dedup_applied = 0
    if do_dedup:
        for paths in plan["duplicates"].values():
            # never dedup convention-loaded folders (flags/models/entities/fonts):
            # the engine resolves them by path, without textual references
            existing = [
                p for p in paths
                if (root / p).exists() and not p.lower().startswith(skip_prefixes)
            ]
            if len(existing) < 2:
                continue
            canonical = max(
                existing,
                key=lambda p: (p.lower().endswith(".dds"), refn(p), p),
            )
            for p in existing:
                if p == canonical:
                    continue
                cur = mapping.get(p, p)
                canon_cur = mapping.get(canonical, canonical)
                if refn(p) or cur != p and refn(cur):
                    _redirect(mapping, cur, canon_cur)
                deletions.add(p)
                if cur != p:
                    deletions.add(cur)
                dedup_applied += 1

    # Converted originals: only non-dds sources whose .dds twin actually
    # exists. A reencoded .dds is the conversion result itself and must stay
    # (BUG-01); a stale/failed manifest must not delete anything (BUG-07).
    deletions |= {
        p for p in conv_ok
        if (root / p).exists()
        and Path(p).suffix.lower() != ".dds"
        and (root / p).with_suffix(".dds").exists()
    }
    if do_vanilla_dedup:
        deletions |= {
            p for p in plan.get("vanilla_identical", [])
            if (root / p).exists()
            and not p.lower().startswith(skip_prefixes)
        }
    # Extension-substitution guard: the engine resolves a token with an
    # extension different from the file on disk (e.g. script says .png, only
    # .dds exists). Indexes: (dir, basename) → extensions present on disk /
    # demanded by tokens.
    present: dict[tuple[str, str], set[str]] = defaultdict(set)
    for p in (root / "gfx").rglob("*"):
        if p.is_file():
            present[(str(p.parent).lower(), p.stem.lower())].add(p.suffix.lower())
    demanded: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in scan_result["references"]:
        t = Path(r["token"])
        demanded[(str(t.parent).lower(), t.stem.lower())].add(t.suffix.lower())

    def _needed_via_substitution(d: str) -> bool:
        p = Path(d)
        key = (str(p.parent).lower(), p.stem.lower())
        # a twin with another extension satisfies the substitution → safe
        if present.get(key, set()) - {p.suffix.lower()}:
            return False
        # no twin, but a script demands a different extension of the same asset
        return bool(demanded.get(key, set()) - {p.suffix.lower()})

    # never delete a rewrite destination or a missing file; nor anything the
    # engine would still resolve via extension substitution
    kept_ext_guard: list[str] = []
    safe: set[str] = set()
    rewrite_dest = set(mapping.values())
    for d in deletions:
        if d in rewrite_dest or not (root / d).exists():
            continue
        if _needed_via_substitution(d):
            kept_ext_guard.append(d)
        else:
            safe.add(d)
    deletions = safe

    changes = [
        {"kind": "cutover", "src": s, "dst": d, "locs": locs_for(s)}
        for s, d in sorted(mapping.items()) if s != d
    ]
    return changes, sorted(deletions), dedup_applied, kept_ext_guard


def run(root: Path, workshop: Path | None, out: Path, apply: bool, vanilla: Path | None,
        do_dedup: bool = True, do_vanilla_dedup: bool = True) -> int:
    out.mkdir(parents=True, exist_ok=True)
    plan_file = out / "asset_plan.json"
    conv_file = out / "convert_manifest.json"
    for f in (plan_file, conv_file):
        if not f.exists():
            print(f"{f.name} missing — run 'analyze' and 'convert --apply' first.")
            return 2
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    conv = json.loads(conv_file.read_text(encoding="utf-8"))
    conv_ok = {m["path"] for m in conv["ok"]}

    print(f"Cutover on {root} ({'APPLY' if apply else 'dry-run'}) …")
    scan_result = scanner.scan(root, vanilla)
    changes, deletions, dedup_applied, kept_ext_guard = plan_cutover(
        root, scan_result, plan, conv_ok, do_dedup, do_vanilla_dedup
    )
    files_touched = {loc.rsplit(":", 1)[0] for c in changes for loc in c["locs"]}
    print(f"Plan: {len(changes)} rewrites in {len(files_touched)} files — "
          f"{dedup_applied} duplicates — {len(deletions)} files to delete"
          + (f" — {len(kept_ext_guard)} held back (extension guard)"
             if kept_ext_guard else ""))

    result = {
        "rewrite": len(changes),
        "files_touched": len(files_touched),
        "dedup": dedup_applied,
        "deletions": deletions,
        "kept_ext_guard": kept_ext_guard,
        "dry_run": not apply,
    }
    if apply and changes:
        stats = normalize._rewrite_refs(root, changes, apply)
        result["rewrite_stats"] = stats
        print(f"Rewrote {stats['files_touched']} files "
              f"({stats['substitutions']} substitutions)")
    if apply:
        removed = 0
        for d in deletions:
            try:
                (root / d).unlink()
                removed += 1
            except OSError as e:
                print(f"  deletion failed {d}: {e}")
        result["removed"] = removed
        print(f"Deleted: {removed} files")

        post = scanner.scan(root, vanilla)
        by_token = {r["token"]: r for r in post["references"]}
        broken = [
            d for d in deletions
            if d in by_token and not by_token[d]["in_vanilla"]
            and len(by_token[d]["files"]) > 0
        ]
        result["verify"] = {
            "true_missing_after": len(post["missing_true"]),
            "deleted_still_referenced": broken[:20],
        }
        print(f"Verify: true missing: {len(post['missing_true'])} — "
              f"deleted but still referenced: {len(broken)}")

    (out / "cutover_manifest.json").write_text(
        json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Manifest: {out / 'cutover_manifest.json'}")
    return 0
