"""Per-class DDS conversion (BC1 for opaque / BC3 for alpha, resolution cap,
pad to multiples of 4 with edge replication) via texconv in a parallel pool.

Consumes build/asset_plan.json produced by 'analyze'. Deletes nothing and
touches no references: that is the cutover. If a twin .dds already exists,
the source is skipped.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from . import fsutil, images, parallel
from .tools import convert_to_dds, texconv_command


def _fmt_for(entry: dict, root: Path) -> str:
    alpha = entry.get("alpha")
    if entry["action"] == "reencode" and alpha == "dds":
        try:
            alpha = images.alpha_kind(images.load_rgba(root / entry["path"]))
        except Exception:
            alpha = None
    return "DXT1" if alpha == "opaque" else "DXT5"


def _convert_one(entry: dict, root: Path, cmd: list[str]) -> dict:
    src = root / entry["path"]
    target = src.with_suffix(".dds")
    # The "twin" guard applies to png/tga → dds conversions: for reencodes the
    # target IS the source (it always exists) and is rewritten in place.
    if entry["action"] == "convert" and target.exists():
        return {"path": entry["path"], "ok": False,
                "note": "twin .dds already exists, skip"}
    fmt = _fmt_for(entry, root)
    resized = padded = False
    try:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            if entry["action"] == "convert":
                img = images.load_rgba(src)
                img, resized = images.resize_cap(img, entry["cap"])
                img, pw, ph = images.pad_to_4(img)
                padded = bool(pw or ph)
                feed = tdp / f"{src.stem}.png"
                img.save(feed)
            else:  # reencode of uncompressed dds
                feed = src
            out = tdp / f"{src.stem}.dds"
            r = convert_to_dds(cmd, feed, tdp, fmt, mips=1)
            if r.returncode != 0 or not out.exists():
                return {"path": entry["path"], "ok": False,
                        "note": f"texconv rc={r.returncode} {r.stderr[-200:]}",
                        "fmt": fmt}
            shutil.copy2(out, target)
            return {
                "path": entry["path"], "ok": True, "fmt": fmt,
                "resized": resized, "padded": padded,
                "bytes": target.stat().st_size,
                "orig_bytes": entry["bytes"],
            }
    except Exception as e:
        return {"path": entry["path"], "ok": False, "note": repr(e)}


def run(root: Path, out: Path, apply: bool, workers: int = 12,
        no_wine: bool = False) -> int:
    out.mkdir(parents=True, exist_ok=True)
    plan_file = out / "asset_plan.json"
    if not plan_file.exists():
        print("asset_plan.json missing — run 'analyze' first.")
        return 2
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    todo = [e for e in plan["files"] if e["action"] in ("convert", "reencode")]
    if not apply:
        by_action = Counter(e["action"] for e in todo)
        by_ext = Counter(Path(e["path"]).suffix.lower() for e in todo)
        over = sum(
            1 for e in todo
            if e.get("w", 0) and (e["w"] > e["cap"] or e.get("h", 0) > e["cap"])
        )
        print(f"To convert: {len(todo)} ({dict(by_action)}, extensions: {dict(by_ext)})")
        print(f"Above resolution cap: {over} — dry-run: no file written.")
        return 0

    seen_stems: set[tuple[str, str]] = set()
    skipped: list[dict] = []
    queue: list[dict] = []
    for e in todo:
        path = root / e["path"]
        key = (str(path.parent).lower(), path.stem.lower())
        if key in seen_stems:
            skipped.append({"path": e["path"], "ok": False,
                            "note": "duplicate stem in the same folder"})
            continue
        seen_stems.add(key)
        queue.append(e)

    cmd = texconv_command(no_wine)
    manifest = skipped + parallel.run_pool(
        queue, lambda e: _convert_one(e, root, cmd), workers=workers, every=500)
    ok, ko = parallel.split(manifest)
    saved = sum(m["orig_bytes"] for m in ok) - sum(m["bytes"] for m in ok)
    print(f"Converted: {len(ok)} — failed: {len(ko)} — "
          f"size delta: {saved / 1e6:+.1f} MB")
    fsutil.write_json(out / "convert_manifest.json", {"ok": ok, "failed": ko})
    print(f"Manifest: {out / 'convert_manifest.json'}")
    return 0
