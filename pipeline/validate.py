"""Post-cutover validation + size measurement + game error.log analysis.

- validate: DDS headers of converted files (fourcc DX9, %4 dimensions, mips),
  final scan (missing/fallback), disk usage, orphan files.
- check-log: texture/sprite errors from the error.log of the last game session.
"""
from __future__ import annotations

import contextlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import config, dds, descriptor, fsutil, meshscan, scanner, texfix

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


_STATE_ID_RE = re.compile(r'(?m)^\s*id\s*=\s*(\d+)')


def _state_map(states_dir: Path) -> dict[int, str]:
    """state id -> defining file name, from history/states/*.txt."""
    found: dict[int, str] = {}
    if not states_dir.is_dir():
        return found
    for path in states_dir.glob("*.txt"):
        try:
            text = fsutil.read_text(path)[0]
        except (OSError, UnicodeDecodeError):
            continue
        for ident in _STATE_ID_RE.findall(text):
            found.setdefault(int(ident), path.name)
    return found


def descriptor_problems(root: Path, vanilla: Path | None) -> list[str]:
    """Consistency of <staging>.mod with descriptor.mod, and replace_path vs
    vanilla.

    A launcher file that lacks ``replace_path`` makes the game load vanilla
    *and* the mod's own copies of the same directories: every state id then
    collides ("State ID conflict", "Failed to create id ... This might crash
    the game") and the load can crash.
    """
    problems: list[str] = []
    inner_file = root / "descriptor.mod"
    outer_file = root.parent / f"{root.name}.mod"
    inner = descriptor.parse_descriptor(
        fsutil.read_text(inner_file)[0]) if inner_file.is_file() else None
    bare_tree = inner is None and not outer_file.is_file()
    if inner is None and not bare_tree:
        problems.append(f"missing descriptor: {inner_file.name}")
    if not outer_file.is_file() and not bare_tree:
        problems.append(
            f"missing launcher file {outer_file.name}: the game cannot load "
            f"the mod without it")
    elif outer_file.is_file():
        outer = descriptor.parse_descriptor(fsutil.read_text(outer_file)[0])
        if not outer.path:
            problems.append(f'{outer_file.name}: no path="..."')
        if inner is not None:
            for field in ("name", "version", "supported_version", "tags",
                          "replace_paths"):
                a, b = getattr(inner, field), getattr(outer, field)
                if a != b:
                    problems.append(
                        f"{outer_file.name}: {field} differs from "
                        f"descriptor.mod ({b!r} vs {a!r})")
            missing = [p for p in inner.replace_paths
                       if p not in outer.replace_paths]
            if missing:
                problems.append(
                    f"{outer_file.name}: replace_path missing for "
                    f"{', '.join(missing[:5])}"
                    + (" …" if len(missing) > 5 else ""))

    declared = {descriptor.normalize_path(p).casefold()
                for p in (inner or descriptor.Descriptor()).replace_paths}
    if (vanilla is not None and "history/states" not in declared
            and (root / "history" / "states").is_dir()):
        mine = _state_map(root / "history" / "states")
        theirs = _state_map(vanilla / "history" / "states")
        shared = [(i, theirs[i], mine[i]) for i in sorted(mine) if i in theirs]
        if shared:
            examples = ", ".join(f"{i}: {v} / {m}" for i, v, m in shared[:3])
            problems.append(
                f"{len(shared)} state ids exist in vanilla and in the mod, and "
                f'replace_path="history/states" is not declared: both copies '
                f"load (State ID conflict, duplicate ids, load crash) "
                f"({examples})")
    return problems


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

    desc_problems = descriptor_problems(root, vanilla)
    problems.extend(desc_problems)
    desc_state = ("consistent" if not desc_problems
                  else f"{len(desc_problems)} problem(s)")
    lines = [
        f"Validation {scan['generated_at']}",
        f"Descriptor: {desc_state}",
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
        "descriptor_problems": desc_problems,
    })
    print(report)
    return 1 if problems else 0


# Error classes that break a load instead of only polluting the log.
FATAL_CLASSES = (
    ("State ID conflict", re.compile(r"State ID conflict", re.I)),
    ("duplicate id", re.compile(r"Failed to create id .*already exists", re.I)),
    ("access violation",
     re.compile(r"Unhandled Exception .*EXCEPTION_ACCESS_VIOLATION", re.I)),
)
# Everything else worth counting, in reporting order.
_LOG_CLASSES = FATAL_CLASSES + (
    ("malformed token", re.compile(r"Malformed token", re.I)),
    ("invalid trigger", re.compile(r"Invalid trigger", re.I)),
    ("invalid effect", re.compile(r"Invalid effect", re.I)),
    ("failed to validate", re.compile(r"Trigger failed to validate", re.I)),
    ("missing autonomy_state", re.compile(r"Failed to find autonomy_state", re.I)),
    ("missing texture", re.compile(r"(Failed to load texture|missing texture|"
                                   r"Couldn't find texture)", re.I)),
    ("missing asset", re.compile(r"(Could not load animation|Missing font|"
                                 r"No font with name|Failed to find sprite)", re.I)),
    ("invalid version", re.compile(r"Invalid supported_version", re.I)),
)


def _crash_report(log: Path) -> list[str]:
    """Latest crash of the same install, if any."""
    crashes = log.parent.parent / "crashes"
    if not crashes.is_dir():
        return []
    dirs = sorted((d for d in crashes.iterdir() if d.is_dir()), reverse=True)
    if not dirs:
        return []
    exc = dirs[0] / "exception.txt"
    head = ["", f"Latest crash: {dirs[0].name}"]
    if exc.is_file():
        body = exc.read_text(encoding="utf-8", errors="replace").splitlines()
        head += ["  " + ln for ln in body[:8] if ln.strip()]
    return head


def check_log(path: Path | None = None, tail: int = 0) -> int:
    """Classify a game error.log.

    ``tail`` restricts the analysis to the last N lines (0 = whole file, the
    default: the game rewrites the log on every launch).
    """
    log = path or next((p for p in GAME_LOG_CANDIDATES if p.exists()), None)
    if log is None or not Path(log).is_file():
        if path is not None:
            print(f"error.log not found: {log}")
        else:
            print(f"error.log not found in: {[str(p) for p in GAME_LOG_CANDIDATES]}")
            print("Run a game session with the orchestrator, then try again.")
        return 2
    lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
    if tail:
        lines = lines[-tail:]
    keys = ("texture", "sprite", ".dds", ".tga", ".png", ".fnt", ".anim")
    counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    for label, rx in _LOG_CLASSES:
        matched = [ln for ln in lines if rx.search(ln)]
        if matched:
            counts[label] = len(matched)
            examples[label] = matched[0].split("]: ", 1)[-1][:160]
    print(f"error.log: {len(lines)} lines analyzed"
          + (f" (tail {tail})" if tail else ""))
    if counts:
        print(f"{'class':24} {'lines':>6}  example")
        for label, _ in _LOG_CLASSES:
            if label in counts:
                print(f"{label:24} {counts[label]:>6}  {examples[label]}")
    else:
        print("  no known error class in the tail")
    fatal = [c for c in FATAL_CLASSES if c[0] in counts]
    if fatal:
        labels = ", ".join(c[0] for c in fatal)
        print(f"FATAL: {labels} — the game cannot load this state; see below")
        if "State ID conflict" in counts or "duplicate id" in counts:
            print('  hint: the descriptor is not replacing vanilla. Run '
                  '"hoi4port validate": it compares descriptor.mod with '
                  '<staging>.mod and the state ids against vanilla.')
    for ln in _crash_report(Path(log)):
        print(ln)
    hits = [ln for ln in lines if any(k.lower() in ln.lower() for k in keys)]
    if hits:
        print(f"Texture/sprite-related lines: {len(hits)}")
        for ln in hits[:30]:
            print(" ", ln[:200])
        if len(hits) > 30:
            print(f"  … {len(hits) - 30} more")
    return 1 if fatal else 0
