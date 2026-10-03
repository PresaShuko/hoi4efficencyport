"""hoi4port — asset porting/optimization pipeline for HoI4 mods.

Commands:
  sync   Mirror the Workshop mod → staging (mod_tfr/), the safe workspace.
  scan   Scan staging: asset references, missing GFX_*, sprite definitions,
         vanilla hash baseline for dedup.

Configuration: CLI flags → env HOI4PORT_MOD/STAGING/VANILLA/OUT →
hoi4port.toml → Steam library autodetect. `--mod` accepts a numeric
Workshop ID or a path. Install: pip install hoi4port.
"""
from __future__ import annotations

import argparse
import json
import sys

from pipeline import fsutil, scanner
from pipeline import analyze, convert, cutover, entityfix, meshscan, normalize, validate, texfix
from pipeline import __version__
from pipeline.config import load_config, workshop_candidates


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
    if not cfg.staging.is_dir():
        print(f"Staging not found: {cfg.staging} — run 'sync' first.", file=sys.stderr)
        return 2
    cfg.out.mkdir(parents=True, exist_ok=True)

    print(f"Scanning {cfg.staging} ...")
    result = scanner.scan(cfg.staging, cfg.vanilla)
    scan_json = cfg.out / "scan.json"
    scan_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    scanner.write_report(result, cfg.out / "scan_report.txt")

    refs = result["references"]
    missing = [r for r in refs if not r["exists"]]
    print(f"Text files: {sum(result['files_by_encoding'].values())} "
          f"({result['files_by_encoding']})")
    print(f"Unique asset paths: {len(refs)} — missing: {len(missing)} "
          f"(true: {len(result['missing_true'])}, "
          f"vanilla fallback: {len(result['missing_fallback_vanilla'])}, "
          f"suspect: {len(result['missing_suspect'])})")
    print(f"spriteTypes defined: {len(result['sprite_types'])} "
          f"(vanilla: {result['gfx_vanilla_defs_count']}) — "
          f"GFX_* referenced: {len(result['gfx_refs'])} — "
          f"GFX_* missing: {len(result['gfx_missing'])} "
          f"(static: {len(result['gfx_missing_static'])}, "
          f"covered by vanilla: {len(result['gfx_covered_by_vanilla'])})")
    if result["unreadable"]:
        print(f"WARNING — {len(result['unreadable'])} unreadable files "
              f"(details in scan.json)")

    status = scanner.vanilla_baseline(cfg.vanilla, cfg.out / "vanilla_hash.json", args.skip_vanilla)
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
    if not cfg.staging.is_dir():
        print(f"Staging not found: {cfg.staging} — run 'sync' first.", file=sys.stderr)
        return 2
    cfg.out.mkdir(parents=True, exist_ok=True)
    print(f"Analyzing assets in {cfg.staging} ...")
    scan_result = scanner.scan(cfg.staging, cfg.vanilla)
    vhash = None
    vfile = cfg.out / "vanilla_hash.json"
    if not args.skip_vanilla and vfile.exists():
        vhash = json.loads(vfile.read_text(encoding="utf-8"))
    elif not vfile.exists():
        print("Note: vanilla_hash.json missing — vanilla dedup skipped "
              "(run a full 'scan').")
    res = analyze.plan(cfg.staging, scan_result, vhash)
    (cfg.out / "asset_plan.json").write_text(
        json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    analyze.write_report(res, cfg.staging, cfg.out / "asset_report.txt")

    from collections import Counter
    acts = Counter(f["action"] for f in res["files"])
    print(f"Files in gfx/: {len(res['files'])} — actions: {dict(acts)}")
    print(f"Duplicates: {len(res['duplicates'])} groups — "
          f"vanilla-identical: {len(res['vanilla_identical'])} — "
          f"unused: {len(res['unused'])} — errors: {len(res['errors'])}")
    print(f"Output: build/asset_plan.json + asset_report.txt")
    return 0


def cmd_convert(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, out=args.out)
    if not cfg.staging.is_dir():
        print(f"Staging not found: {cfg.staging} — run 'sync' first.", file=sys.stderr)
        return 2
    return convert.run(cfg.staging, cfg.out, apply=args.apply, workers=args.workers,
                       no_wine=args.no_wine)


def cmd_cutover(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, vanilla=args.vanilla, out=args.out)
    if not cfg.staging.is_dir():
        print(f"Staging not found: {cfg.staging} — run 'sync' first.", file=sys.stderr)
        return 2
    return cutover.run(cfg.staging, cfg.workshop_mod, cfg.out, apply=args.apply,
                       vanilla=cfg.vanilla, do_dedup=not args.no_dedup,
                       do_vanilla_dedup=args.vanilla_dedup)


def cmd_validate(args) -> int:
    cfg = load_config(mod=args.mod, staging=args.staging, vanilla=args.vanilla, out=args.out)
    if not cfg.staging.is_dir():
        print(f"Staging not found: {cfg.staging}", file=sys.stderr)
        return 2
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


def main() -> int:
    # Windows consoles default to legacy code pages (cp1252 etc.): force UTF-8
    # on the CLI streams so output survives redirection (pipes, CI logs).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    ap = argparse.ArgumentParser(prog="hoi4port", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"hoi4port {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p, staging=True):
        p.add_argument("--mod", help="Workshop ID or path of the mod "
                                     "(default: autodetect; env HOI4PORT_MOD)")
        if staging:
            p.add_argument("--staging", help="staging path (default: mod_tfr/)")
        p.add_argument("--out", help="output dir (default: build/)")

    p_sync = sub.add_parser("sync", help="mirror Workshop mod -> staging")
    add_common(p_sync)
    p_sync.set_defaults(fn=cmd_sync)

    p_scan = sub.add_parser("scan", help="scan asset references and sprites")
    add_common(p_scan)
    p_scan.add_argument("--vanilla", help="HoI4 install for hash baseline/dedup")
    p_scan.add_argument("--skip-vanilla", action="store_true",
                        help="skip hashing the vanilla install")
    p_scan.set_defaults(fn=cmd_scan)

    p_norm = sub.add_parser("normalize", help="vanilla-style sorting + reference repair")
    add_common(p_norm)
    p_norm.add_argument("--vanilla", help="HoI4 install (fallback classification)")
    p_norm.add_argument("--apply", action="store_true",
                        help="actually perform changes (default: dry-run)")
    p_norm.add_argument("--no-moves", action="store_true",
                        help="reference repair only, no directory reordering")
    p_norm.set_defaults(fn=cmd_normalize)

    p_an = sub.add_parser("analyze", help="asset analysis: alpha, cap, dedup, unused")
    add_common(p_an)
    p_an.add_argument("--vanilla", help="HoI4 install (dedup fallback)")
    p_an.add_argument("--skip-vanilla", action="store_true",
                      help="ignore vanilla_hash.json")
    p_an.set_defaults(fn=cmd_analyze)

    p_cv = sub.add_parser("convert", help="DDS conversion (BC1/BC3, cap, multiple-of-4 padding)")
    add_common(p_cv)
    p_cv.add_argument("--apply", action="store_true",
                      help="run the conversion (default: plan only)")
    p_cv.add_argument("--workers", type=int, default=12, help="parallel texconv jobs")
    p_cv.add_argument("--no-wine", action="store_true",
                      help="do not invoke texconv through wine (Linux/macOS)")
    p_cv.set_defaults(fn=cmd_convert)

    p_ct = sub.add_parser("cutover", help="rewrite .dds + dedup + remove originals")
    add_common(p_ct)
    p_ct.add_argument("--vanilla", help="HoI4 install (fallback)")
    p_ct.add_argument("--apply", action="store_true",
                      help="actually perform changes (default: dry-run)")
    p_ct.add_argument("--no-dedup", action="store_true", help="skip intra-mod dedup")
    p_ct.add_argument("--vanilla-dedup", action="store_true",
                      help="delete overrides identical to vanilla (default: no — "
                           "per-file fallback is unreliable for flags/bookmarks)")
    p_ct.set_defaults(fn=cmd_cutover)

    p_val = sub.add_parser("validate", help="DDS validation + scan + disk sizes")
    add_common(p_val)
    p_val.add_argument("--vanilla", help="HoI4 install (fallback)")
    p_val.set_defaults(fn=cmd_validate)

    p_log = sub.add_parser("check-log", help="texture/sprite errors from the game error.log")
    p_log.add_argument("--tail", type=int, default=5000, help="tail lines to analyze")
    p_log.set_defaults(fn=cmd_check_log)

    p_ms = sub.add_parser("meshscan", help=".mesh analysis: vertices, materials, collision, vanilla baseline")
    add_common(p_ms)
    p_ms.add_argument("--vanilla", help="HoI4 install for per-class baseline")
    p_ms.add_argument("--skip-vanilla", action="store_true",
                      help="do not use/compute vanilla_mesh_stats.json")
    p_ms.set_defaults(fn=cmd_meshscan)

    p_tx = sub.add_parser("texfix", help="model textures: full mip chain + DXT1/DXT5")
    add_common(p_tx)
    p_tx.add_argument("--apply", action="store_true",
                      help="actually perform changes (default: dry-run)")
    p_tx.add_argument("--workers", type=int, default=12, help="parallel texconv jobs")
    p_tx.add_argument("--cap", type=int, default=None,
                      help="optional resolution cap (default: no resize)")
    p_tx.add_argument("--no-wine", action="store_true",
                      help="do not invoke texconv through wine (Linux/macOS)")
    p_tx.set_defaults(fn=cmd_texfix)

    p_ef = sub.add_parser("entityfix", help="entity audit + cull_radius for ambient objects")
    add_common(p_ef)
    p_ef.add_argument("--vanilla", help="HoI4 install (entity/ambient name fallback)")
    p_ef.add_argument("--apply", action="store_true",
                      help="actually perform changes (default: audit only)")
    p_ef.add_argument("--cull-radius", type=int, default=100,
                      help="cull_radius value to insert (default: 100)")
    p_ef.set_defaults(fn=cmd_entityfix)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
