"""F3 — Asset analysis: alpha, in-use size (from .gui), hash dedup, unused.

Produces build/asset_plan.json (consumed by convert/cutover) + asset_report.txt.
Conservative policy: no changes to the mod here, planning only.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
import re

from PIL import Image

from . import fsutil

# gfx subfolders outside the conversion perimeter (hardcoded position/function).
SKIP_SUBDIRS = ("gfx/models/", "gfx/entities/", "gfx/flags/")

# Resolution cap per class (max side), ~2x the display size.
CLASS_CAPS = {
    "goals": 256,
    "ideas": 256,
    "decisions": 256,
    "events": 1024,
    "portraits": 512,
    "loading": 1920,
    "ui": 256,
    "skip": 0,
}

# Exact 'size' only: borderSize/minSize/maxSize must not match (the
# lookbehind blocks any word prefix).
SIZE_RE = re.compile(r"(?<![A-Za-z_])size\s*=\s*\{\s*x\s*=\s*(-?\d+)\s*y\s*=\s*(-?\d+)", re.I)
GFX_QUOTED_RE = re.compile(r'"(GFX_[A-Za-z0-9_]+)"')


def classify(rel: str) -> str:
    r = rel.lower()
    if r.startswith(SKIP_SUBDIRS):
        return "skip"
    if r.startswith("gfx/interface/goals/"):
        return "goals"
    if r.startswith("gfx/interface/ideas/"):
        return "ideas"
    if r.startswith("gfx/interface/decisions/"):
        return "decisions"
    if r.startswith("gfx/event_pictures/"):
        return "events"
    if r.startswith("gfx/leaders/"):
        return "portraits"
    if r.startswith("gfx/loading_screens/"):
        return "loading"
    return "ui"


def gui_sprite_sizes(root: Path) -> dict[str, tuple[int, int]]:
    """GFX_* used in .gui → largest nearby declared size (ground truth of use)."""
    sizes: dict[str, tuple[int, int]] = {}
    for fp in fsutil.iter_text_files(root):
        if fp.suffix.lower() != ".gui":
            continue
        try:
            text, _ = fsutil.read_text(fp)
        except (UnicodeDecodeError, OSError):
            continue
        recent: deque[tuple[int, int]] = deque(maxlen=12)
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = SIZE_RE.search(line)
            if m:
                recent.append((int(m.group(1)), int(m.group(2))))
                continue
            for g in GFX_QUOTED_RE.finditer(line):
                name = g.group(1)
                size = recent[-1] if recent else None
                if size is None:
                    for j in range(1, 3):
                        if i + j < len(lines):
                            ms = SIZE_RE.search(lines[i + j])
                            if ms:
                                size = (int(ms.group(1)), int(ms.group(2)))
                                break
                if size is None or max(size) <= 0:
                    continue
                # MAX, not MIN: if a sprite is used at several sizes, the cap
                # must cover the largest use, not shrink to the smallest.
                cur = sizes.get(name)
                if cur is None or size[0] * size[1] > cur[0] * cur[1]:
                    sizes[name] = size
    return sizes


def alpha_kind(img: Image.Image) -> str:
    if img.mode not in ("RGBA", "LA", "PA"):
        return "opaque"
    a = img.getchannel("A")
    lo, hi = a.getextrema()
    if lo == 255:
        return "opaque"
    if lo == 0 and hi == 255 and not any(
        0 < v < 255 for v in a.histogram()[1:255]
    ):
        return "binary"
    return "gradient"


def dds_fourcc(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(128)
    if len(head) < 88 or head[:4] != b"DDS ":
        return "?"
    four = head[84:88]
    return four.decode("ascii", "replace")


def plan(root: Path, scan_result: dict, vanilla_hash: dict | None) -> dict:
    ref_count = Counter()
    for r in scan_result["references"]:
        if r["exists"]:
            ref_count[r["token"].lower()] = len(r["files"])

    sprite_tex = {}
    for name, s in scan_result["sprite_types"].items():
        if s["texturefile"]:
            sprite_tex.setdefault(s["texturefile"].lower(), []).append(name)
    gui_sizes = gui_sprite_sizes(root)

    files: list[dict] = []
    hash_groups: dict[str, list[str]] = defaultdict(list)
    errors: list[str] = []

    for fp in sorted(fsutil.iter_asset_files(root / "gfx")):
        rel = fp.relative_to(root).as_posix()
        cls = classify(rel)
        entry = {
            "path": rel,
            "bytes": fp.stat().st_size,
            "class": cls,
            "refs": ref_count.get(rel.lower(), 0),
        }
        try:
            img = Image.open(fp)
            w, h = img.size
            entry["w"], entry["h"] = w, h
            entry["alpha"] = alpha_kind(img) if fp.suffix.lower() != ".dds" else "dds"
        except Exception as e:
            errors.append(f"{rel} ({e})")
            w = h = 0
            entry["alpha"] = "?"
        if fp.suffix.lower() == ".dds":
            entry["fourcc"] = dds_fourcc(fp)

        # Resolution cap:
        # - size declared in .gui → 2x the largest display (never below).
        # - otherwise class 'ui': quads without `size` draw the texture at
        #   native resolution → downscaling changes the layout. No downscale.
        # - otherwise class cap (engine-loaded grids/conventions).
        gui_cap = 0
        for name in sprite_tex.get(rel.lower(), []):
            gs = gui_sizes.get(name)
            if gs:
                gui_cap = max(gui_cap, 2 * max(gs[0], gs[1], 1))
        if gui_cap:
            cap = gui_cap
        elif cls == "ui":
            cap = max(CLASS_CAPS.get(cls, 256), w, h)
        else:
            cap = CLASS_CAPS.get(cls, 256)
        entry["cap"] = cap
        if cls == "skip":
            entry["action"] = "skip"
        elif fp.suffix.lower() == ".dds":
            entry["action"] = (
                "keep" if entry.get("fourcc", "?") in ("DXT1", "DXT3", "DXT5")
                else "reencode"
            )
        else:
            entry["action"] = "convert"
        files.append(entry)

        try:
            hash_groups[fsutil.sha256_file(fp)].append(rel)
        except OSError as e:
            errors.append(f"hash {rel} ({e})")

    duplicates = {
        h: paths for h, paths in hash_groups.items() if len(paths) > 1
    }
    vanilla_identical = []
    if vanilla_hash:
        for h, paths in hash_groups.items():
            for p in paths:
                if vanilla_hash.get(p) == h:
                    vanilla_identical.append(p)

    unused = [e["path"] for e in files if e["refs"] == 0]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": files,
        "errors": errors,
        "duplicates": {h: ps for h, ps in sorted(duplicates.items(), key=lambda kv: -sum(
            (root / p).stat().st_size for p in kv[1]))},
        "vanilla_identical": sorted(vanilla_identical),
        "unused": unused,
        "gui_sizes": {k: list(v) for k, v in sorted(gui_sizes.items())},
    }


def write_report(res: dict, root: Path, out_file: Path) -> None:
    files = res["files"]
    by_cls = Counter(f["class"] for f in files)
    over = [
        f for f in files
        if f["class"] != "skip"
        and f.get("w", 0) and (f["w"] > f["cap"] or f.get("h", 0) > f["cap"])
    ]
    waste = sum(f["bytes"] for f in over)
    dup_waste = sum(
        sum((root / p).stat().st_size for p in group[1:])
        for group in res["duplicates"].values() if len(group) > 1
    )
    actions = Counter(f["action"] for f in files)
    lines = [
        f"Asset analysis {res['generated_at']}",
        f"Files in gfx/: {len(files)} — per class: {dict(by_cls)}",
        f"Planned actions: {dict(actions)}",
        f"Above resolution cap (resizable): {len(over)} ({waste / 1e6:.1f} MB)",
        f"Duplicates: {len(res['duplicates'])} groups "
        f"({dup_waste / 1e6:.1f} MB recoverable)",
        f"Vanilla-identical (no-op overrides): {len(res['vanilla_identical'])}",
        f"Unreferenced (unused): {len(res['unused'])}",
        f"Read errors: {len(res['errors'])}",
    ]
    out_file.write_text("\n".join(lines), encoding="utf-8")
