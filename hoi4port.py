"""hoi4port — asset porting/optimization pipeline for HoI4 mods.

Commands:
  sync       Mirror the Workshop mod → staging (mod_tfr/), the safe workspace.
  scan       Scan staging: asset references, missing GFX_*, sprite definitions,
             vanilla hash baseline for dedup.
  normalize  Vanilla-style asset sorting + reference repair.
  analyze    Asset plan: alpha, resolution cap, dedup, unused files.
  convert    Per-class DDS conversion (BC1/BC3, cap, multiple-of-4 padding).
  cutover    Reference rewrite + dedup + original removal.
  validate   Post-cutover validation: DDS, scan, disk sizes.
  check-log  Texture/sprite errors from the game error.log.
  meshscan   .mesh analysis: vertices, materials, collision, vanilla baseline.
  texfix     Model textures: full mip chain + DXT1/DXT5.
  entityfix  Entity audit + cull_radius for ambient objects.

Configuration: CLI flags → env HOI4PORT_MOD/STAGING/VANILLA/OUT →
hoi4port.toml → Steam library autodetect. `--mod` accepts a numeric
Workshop ID or a path. Install: pip install hoi4port.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys

from pipeline import (__version__, analyze, convert, cutover, entityfix,
                      fsutil, meshscan, normalize, scanner, texfix, validate)
from pipeline.config import load_config, workshop_candidates


def require_staging(cfg) -> int | None:
    """Return 2 (with the shared message) when the staging tree is missing."""
    if cfg.staging.is_dir():
        return None
    print(f"Staging not found: {cfg.staging} — run 'sync' first.", file=sys.stderr)
    return 2


def cmd_sync(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging)
    if cfg.workshop_mod is None:
        print("Source mod not determined "
              "(no --mod, no HOI4PORT_MOD env, no hoi4port.toml; autodetect "
              "ambiguous or empty).", file=sys.stderr)
        cands = workshop_candidates()
        if cands:
            print("HoI4 Workshop mods found:", file=sys.stderr)
            for c in cands:
                print(f"  - {c}", file=sys.stderr)
            print("Use: hoi4port sync --mod <Workshop ID>", file=sys.stderr)
        else:
            print("No HoI4 Workshop mods found in known Steam libraries. "
                  "Use: hoi4port sync --mod <Workshop ID or path>",
                  file=sys.stderr)
        return 2
    print(f"Sync: {cfg.workshop_mod}\n   -> {cfg.staging}")
    fsutil.sync_staging(cfg.workshop_mod, cfg.staging)
    return 0


def cmd_scan(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, vanilla=args.vanilla, out=args.out)
    rc = require_staging(cfg)
    if rc:
        return rc
    cfg.out.mkdir(parents=True, exist_ok=True)

    print(f"Scanning {cfg.staging} ...")
    result = scanner.scan(cfg.staging, cfg.vanilla)
    scan_json = cfg.out / "scan.json"
    fsutil.write_json(scan_json, result, indent=2)
    scanner.write_report(result, cfg.out / "scan_report.txt")

    s = scanner.summary(result)
    print(f"Text files: {s['text_files']} ({s['encodings']})")
    print(f"Unique asset paths: {s['references']} — missing: {s['missing_count']} "
          f"(true: {s['missing_true']}, "
          f"vanilla fallback: {s['fallback_vanilla']}, "
          f"suspect: {s['missing_suspect']})")
    print(f"spriteTypes defined: {s['sprite_types']} "
          f"(vanilla: {s['gfx_vanilla_defs']}) — "
          f"GFX_* referenced: {s['gfx_refs']} — "
          f"GFX_* missing: {s['gfx_missing']} "
          f"(static: {s['gfx_missing_static']}, "
          f"covered by vanilla: {s['gfx_covered_vanilla']})")
    if s["unreadable"]:
        print(f"WARNING — {s['unreadable']} unreadable files (details in scan.json)")

    status = scanner.vanilla_baseline(cfg.vanilla, cfg.out / "vanilla_hash.json",
                                      args.skip_vanilla)
    print(f"Vanilla baseline: {status}")
    print(f"Output: {scan_json} + scan_report.txt")
    return 0


def cmd_normalize(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, vanilla=args.vanilla, out=args.out)
    normalize.run(cfg.staging, cfg.out, apply=args.apply, vanilla=cfg.vanilla,
                  do_moves=not args.no_moves)
    return 0


def cmd_analyze(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, vanilla=args.vanilla, out=args.out)
    rc = require_staging(cfg)
    if rc:
        return rc
    cfg.out.mkdir(parents=True, exist_ok=True)
    print(f"Analyzing assets in {cfg.staging} ...")
    scan_result = scanner.scan(cfg.staging, cfg.vanilla)
    vhash = None
    vfile = cfg.out / "vanilla_hash.json"
    if not args.skip_vanilla and vfile.exists():
        data = json.loads(vfile.read_text(encoding="utf-8"))
        vhash = data.get("hashes") if isinstance(data, dict) else None
        if vhash is None:
            print("Note: vanilla_hash.json has an old format — rerun a full 'scan'.")
    elif not vfile.exists():
        print("Note: vanilla_hash.json missing — vanilla dedup skipped "
              "(run a full 'scan').")
    res = analyze.plan(cfg.staging, scan_result, vhash)
    plan_json = cfg.out / "asset_plan.json"
    fsutil.write_json(plan_json, res)
    analyze.write_report(res, cfg.out / "asset_report.txt")

    s = analyze.summary(res)
    print(f"Files in gfx/: {s['files']} — actions: {dict(s['actions'])}")
    print(f"Duplicates: {s['duplicates']} groups — "
          f"vanilla-identical: {s['vanilla_identical']} — "
          f"unused: {s['unused']} — errors: {s['errors']}")
    print(f"Output: {plan_json} + asset_report.txt")
    return 0


def cmd_convert(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, out=args.out)
    rc = require_staging(cfg)
    if rc:
        return rc
    return convert.run(cfg.staging, cfg.out, apply=args.apply, workers=args.workers,
                       no_wine=args.no_wine)


def cmd_cutover(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, vanilla=args.vanilla, out=args.out)
    rc = require_staging(cfg)
    if rc:
        return rc
    return cutover.run(cfg.staging, cfg.out, apply=args.apply,
                       vanilla=cfg.vanilla, do_dedup=not args.no_dedup,
                       do_vanilla_dedup=args.vanilla_dedup)


def cmd_validate(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, vanilla=args.vanilla, out=args.out)
    rc = require_staging(cfg)
    if rc:
        return rc
    return validate.run(cfg.staging, cfg.workshop_mod, cfg.out, cfg.vanilla)


def cmd_check_log(args) -> int:
    return validate.check_log(tail=args.tail)


def cmd_meshscan(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, vanilla=args.vanilla, out=args.out)
    return meshscan.run(cfg.staging, cfg.out, cfg.vanilla, skip_vanilla=args.skip_vanilla)


def cmd_texfix(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, out=args.out)
    return texfix.run(cfg.staging, cfg.out, apply=args.apply,
                      workers=args.workers, cap=args.cap, no_wine=args.no_wine)


def cmd_entityfix(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, vanilla=args.vanilla, out=args.out)
    return entityfix.run(cfg.staging, cfg.out, apply=args.apply,
                         vanilla=cfg.vanilla, cull_radius=args.cull_radius)


# --- argument helpers -----------------------------------------------------------

def add_common(p, *, out: bool = True) -> None:
    p.add_argument("--mod", help="Workshop ID or path of the mod "
                                 "(default: autodetect; env HOI4PORT_MOD)")
    p.add_argument("--staging", help="staging path (default: mod_tfr/)")
    if out:
        p.add_argument("--out", help="output dir (default: build/)")


def add_vanilla(p, help: str = "HoI4 install (vanilla fallback)") -> None:
    p.add_argument("--vanilla", help=help)


def add_skip_vanilla(p, help: str = "skip the vanilla baseline") -> None:
    p.add_argument("--skip-vanilla", action="store_true", help=help)


def add_apply(p, help: str = "actually perform changes (default: dry-run)") -> None:
    p.add_argument("--apply", action="store_true", help=help)


def add_texconv(p) -> None:
    p.add_argument("--workers", type=int, default=12, help="parallel texconv jobs")
    p.add_argument("--no-wine", action="store_true",
                   help="do not invoke texconv through wine (Linux/macOS)")


def main() -> int:
    # Windows consoles default to legacy code pages (cp1252 etc.): force UTF-8
    # on the CLI streams so output survives redirection (pipes, CI logs).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(prog="hoi4port", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"hoi4port {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="mirror Workshop mod -> staging")
    add_common(p_sync, out=False)
    p_sync.set_defaults(fn=cmd_sync)

    p_scan = sub.add_parser("scan", help="scan asset references and sprites")
    add_common(p_scan)
    add_vanilla(p_scan, "HoI4 install for hash baseline/dedup")
    add_skip_vanilla(p_scan, "skip hashing the vanilla install")
    p_scan.set_defaults(fn=cmd_scan)

    p_norm = sub.add_parser("normalize", help="vanilla-style sorting + reference repair")
    add_common(p_norm)
    add_vanilla(p_norm, "HoI4 install (fallback classification)")
    add_apply(p_norm)
    p_norm.add_argument("--no-moves", action="store_true",
                        help="reference repair only, no directory reordering")
    p_norm.set_defaults(fn=cmd_normalize)

    p_an = sub.add_parser("analyze", help="asset analysis: alpha, cap, dedup, unused")
    add_common(p_an)
    add_vanilla(p_an, "HoI4 install (dedup fallback)")
    add_skip_vanilla(p_an, "ignore vanilla_hash.json")
    p_an.set_defaults(fn=cmd_analyze)

    p_cv = sub.add_parser("convert", help="DDS conversion (BC1/BC3, cap, multiple-of-4 padding)")
    add_common(p_cv)
    add_apply(p_cv, "run the conversion (default: plan only)")
    add_texconv(p_cv)
    p_cv.set_defaults(fn=cmd_convert)

    p_ct = sub.add_parser("cutover", help="rewrite .dds + dedup + remove originals")
    add_common(p_ct)
    add_vanilla(p_ct)
    add_apply(p_ct)
    p_ct.add_argument("--no-dedup", action="store_true", help="skip intra-mod dedup")
    p_ct.add_argument("--vanilla-dedup", action="store_true",
                      help="delete overrides identical to vanilla (default: no — "
                           "per-file fallback is unreliable for flags/bookmarks)")
    p_ct.set_defaults(fn=cmd_cutover)

    p_val = sub.add_parser("validate", help="DDS validation + scan + disk sizes")
    add_common(p_val)
    add_vanilla(p_val)
    p_val.set_defaults(fn=cmd_validate)

    p_log = sub.add_parser("check-log", help="texture/sprite errors from the game error.log")
    p_log.add_argument("--tail", type=int, default=5000, help="tail lines to analyze")
    p_log.set_defaults(fn=cmd_check_log)

    p_ms = sub.add_parser("meshscan", help=".mesh analysis: vertices, materials, collision, vanilla baseline")
    add_common(p_ms)
    add_vanilla(p_ms, "HoI4 install for per-class baseline")
    add_skip_vanilla(p_ms, "do not use/compute vanilla_mesh_stats.json")
    p_ms.set_defaults(fn=cmd_meshscan)

    p_tx = sub.add_parser("texfix", help="model textures: full mip chain + DXT1/DXT5")
    add_common(p_tx)
    add_apply(p_tx)
    add_texconv(p_tx)
    p_tx.add_argument("--cap", type=int, default=None,
                      help="optional resolution cap (default: no resize)")
    p_tx.set_defaults(fn=cmd_texfix)

    p_ef = sub.add_parser("entityfix", help="entity audit + cull_radius for ambient objects")
    add_common(p_ef)
    add_vanilla(p_ef, "HoI4 install (entity/ambient name fallback)")
    add_apply(p_ef, "actually perform changes (default: audit only)")
    p_ef.add_argument("--cull-radius", type=int, default=100,
                      help="cull_radius value to insert (default: 100)")
    p_ef.set_defaults(fn=cmd_entityfix)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
