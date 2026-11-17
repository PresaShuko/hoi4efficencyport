"""Post-cutover validation + size measurement + game error.log analysis.

- validate: DDS headers of converted files (fourcc DX9, %4 dimensions, mips),
  final scan (missing/fallback), disk usage, orphan files.
- check-log: texture/sprite errors from the error.log of the last game session.
"""
from __future__ import annotations

import contextlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from . import config, dds, fsutil, meshscan, scanner, texfix

_LOG_DIR = (Path("Paradox Interactive") / config.VANILLA_DIRNAME / "logs"
            / "error.log")
_PROTON_DOCS = (Path(".steam/steam/steamapps/compatdata") / config.HOI4_APPID
                / "pfx/drive_c/users/steamuser/Documents")
GAME_LOG_CANDIDATES = (
    # Windows (Documents possibly redirected to OneDrive)
    Path.home() / "Documents" / _LOG_DIR,
    Path.home() / "OneDrive/Documenti" / _LOG_DIR,
    # Linux — Proton (wine prefix for the HoI4 appid)
    Path.home() / _PROTON_DOCS / _LOG_DIR,
    Path.home() / ".local/share/Steam/steamapps/compatdata" / config.HOI4_APPID
    / "pfx/drive_c/users/steamuser/Documents" / _LOG_DIR,
    # Linux — native version
    Path.home() / ".local/share" / _LOG_DIR,
    # macOS
    Path.home() / "Library/Application Support" / _LOG_DIR,
)


def run(root: Path, workshop: Path | None, out: Path, vanilla: Path | None) -> int:
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    conv_file = out / "convert_manifest.json"
    cut_file = out / "cutover_manifest.json"
    dedup_removed: set[str] = set()
    if cut_file.exists():
        cut = json.loads(cut_file.read_text(encoding="utf-8"))
        dedup_removed = {d.lower() for d in cut.get("deletions", [])}
    # moves after conversion also move dds files: resolve via the ledger
    ledger: dict[str, str] = {}
    ledger_file = out / "moves_ledger.json"
    if ledger_file.exists():
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))

    def resolve_moved(p: str) -> str:
        cur, seen = p, set()
        while cur.lower() in ledger and cur.lower() not in seen:
            seen.add(cur.lower())
            cur = ledger[cur.lower()]
        return cur

    checked = 0
    odd_dims = 0
    mislabeled: list[str] = []
    skipped_conv = 0
    unverifiable: list[str] = []
    fourccs: Counter[str] = Counter()
    # one walk of gfx/ feeds both the dds index and the orphan count
    gfx_entries: list[Path] = []
    if (root / "gfx").is_dir():
        gfx_entries = [p for p in (root / "gfx").rglob("*") if p.is_file()]
    # index (stem, bytes) → paths: dds files converted then moved by normalize
    dds_index: dict[tuple[str, int], list[str]] = defaultdict(list)
    for p in gfx_entries:
        if p.suffix.lower() != ".dds":
            continue
        with contextlib.suppress(OSError):
            dds_index[(p.stem.lower(), p.stat().st_size)].append(
                p.relative_to(root).as_posix())
    if conv_file.exists():
        conv = json.loads(conv_file.read_text(encoding="utf-8"))
        for m in conv["ok"]:
            dds_rel = Path(resolve_moved(m["path"])).with_suffix(".dds").as_posix().lower()
            if dds_rel in dedup_removed:
                continue  # removed by dedup: the canonical one stays, not a problem
            dds_path = (root / m["path"]).with_suffix(".dds")
            hdr = dds.read_header(dds_path)
            if hdr is None and not dds_path.exists():
                cands = dds_index.get(
                    (Path(dds_rel).stem.lower(), int(m.get("bytes", -1))))
                if cands and len(cands) == 1:
                    dds_path = root / cands[0]
                    hdr = dds.read_header(dds_path)
            if hdr is None:
                if not dds_path.exists():
                    unverifiable.append(m["path"])
                else:
                    problems.append(f"unreadable dds: {m['path']}")
                continue
            checked += 1
            fourccs[hdr["fourcc"]] += 1
            if hdr["fourcc"] not in dds.UI_FOURCC:
                problems.append(f"fourcc {hdr['fourcc']}: {m['path']}")
            if hdr["w"] % 4 or hdr["h"] % 4:
                # Not a defect: vanilla HoI4 ships thousands of DDS whose
                # dimensions are not multiples of 4 (111x106 achievements,
                # 500x250 event pictures) and the engine loads them. Counted
                # and reported, never a problem.
                odd_dims += 1
        # A .dds that is not a DDS (a PNG renamed by the mod, an empty file)
        # never reaches the header check: it fails conversion. Name it.
        for m in conv.get("failed", []):
            p = root / m["path"]
            if Path(m["path"]).suffix.lower() != ".dds":
                skipped_conv += 1  # original stays in place: reference still works
                continue
            if not p.is_file():
                continue
            head = p.open("rb").read(4)
            if not head.startswith(b"DDS "):
                mislabeled.append(
                    f"not a dds ({dds.magic_kind(head)}): {m['path']}")
        problems.extend(mislabeled)
    else:
        print("convert_manifest.json missing: skipping the DDS header check.")

    scan = scanner.scan(root, vanilla)
    st_size = fsutil.tree_size(root)
    ws_size = fsutil.tree_size(workshop) if workshop is not None and workshop.is_dir() else 0
    referenced = {r["token"] for r in scan["references"] if r["exists"]}
    orphans = sum(
        1 for p in gfx_entries
        if p.suffix.lower() in fsutil.ASSET_EXTS
        and p.relative_to(root).as_posix() not in referenced
    )

    # F7-F9: mesh integrity, model texture conformance, entityfix applied
    meshes = meshscan.scan_dir(root)
    mesh_ko = [m for m in meshes if not m.get("ok")]
    for m in mesh_ko:
        problems.append(f"unreadable mesh: {m['path']} ({m.get('note', '')})")
    tex_nonconf = [p for p in texfix.collect_targets(root, out)
                   if texfix.needed_action(p)["action"] != "skip"]
    ef_file = out / "entityfix_manifest.json"
    ef_applied = 0
    if ef_file.exists():
        ef_applied = sum(e.get("insertions", 0) for e in json.loads(
            ef_file.read_text(encoding="utf-8")).get("edits", []))

    lines = [
        f"Validation {scan['generated_at']}",
        f"Converted DDS files verified: {checked} (fourcc: {dict(fourccs)})",
        f"Header problems: {len(problems)} (not a dds: {len(mislabeled)})",
        f"DDS not a multiple of 4: {odd_dims} (vanilla ships the same -> not an error)",
        f"Conversion skipped, original kept: {skipped_conv}",
        f"Unverifiable (moved after conversion): {len(unverifiable)}",
        f"Asset paths: {len(scan['references'])} — true missing: "
        f"{len(scan['missing_true'])} — vanilla fallbacks: "
        f"{len(scan['missing_fallback_vanilla'])}",
        f"Missing GFX_*: {len(scan['gfx_missing'])} "
        f"(static: {len(scan['gfx_missing_static'])})",
        f"Orphan files (unreferenced) in gfx/: {orphans}",
        f"Staging size: {st_size / 1e9:.2f} GB"
        + (f" (workshop: {ws_size / 1e9:.2f} GB → "
           f"{(st_size - ws_size) / ws_size:+.1%})" if ws_size else ""),
        f"Meshes: {len(meshes)} — parse errors: {len(mesh_ko)}",
        f"Non-conforming model textures (mips/format): {len(tex_nonconf)}"
        + (" — run 'texfix --apply'" if tex_nonconf else ""),
        f"Ambient cull_radius applied (entityfix): {ef_applied}",
    ]
    report = "\n".join(lines)
    if problems:
        report += "\n--- Problems (first 30) ---\n" + "\n".join(problems[:30])
    (out / "validate_report.txt").write_text(report, encoding="utf-8")
    fsutil.write_json(out / "validate.json", {
        "dds_checked": checked, "fourccs": dict(fourccs),
        "problems": problems, "odd_dimensions": odd_dims,
        "mislabeled": len(mislabeled), "conversion_skipped": skipped_conv,
        "unverifiable": len(unverifiable),
        "missing_true": len(scan["missing_true"]),
        "fallback_vanilla": len(scan["missing_fallback_vanilla"]),
        "gfx_missing": len(scan["gfx_missing"]),
        "orphans": orphans,
        "staging_bytes": st_size, "workshop_bytes": ws_size,
        "mesh_files": len(meshes), "mesh_parse_errors": len(mesh_ko),
        "model_textures_nonconforming": len(tex_nonconf),
        "entityfix_cull_applied": ef_applied,
    })
    print(report)
    return 1 if problems else 0


def check_log(path: Path | None = None, tail: int = 5000) -> int:
    log = path or next((p for p in GAME_LOG_CANDIDATES if p.exists()), None)
    if log is None or not Path(log).is_file():
        if path is not None:
            print(f"error.log not found: {log}")
        else:
            print(f"error.log not found in: {[str(p) for p in GAME_LOG_CANDIDATES]}")
            print("Run a game session with the orchestrator, then try again.")
        return 2
    lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
    keys = ("texture", "sprite", "gfx_", ".dds", ".png", ".tga", "Failed", "Error")
    hits = [ln for ln in lines if any(k.lower() in ln.lower() for k in keys)]
    print(f"error.log: {len(lines)} lines analyzed (tail), "
          f"{len(hits)} texture/sprite-related lines")
    for ln in hits[:30]:
        print(" ", ln[:200])
    if len(hits) > 30:
        print(f"  … {len(hits) - 30} more")
    return 0
