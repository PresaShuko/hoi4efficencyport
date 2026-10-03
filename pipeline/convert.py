"""F4 — Per-class DDS conversion (BC1 for opaque / BC3 for alpha, resolution
cap, pad to multiples of 4 with edge replication) via texconv in a parallel
pool.

Consumes build/asset_plan.json produced by 'analyze'. Deletes nothing and
touches no references: that is the cutover (F5). Overwrites an existing .dds
only if produced by this pipeline? No: if a twin .dds already exists, skip.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from .tools import texconv_command

VALID_FOURCC = {"DXT1", "DXT3", "DXT5"}


def _pad_to_4(img: Image.Image) -> tuple[Image.Image, int, int]:
    w, h = img.size
    pw, ph = (-w) % 4, (-h) % 4
    if pw == ph == 0:
        return img, 0, 0
    out = Image.new(img.mode, (w + pw, h + ph))
    out.paste(img, (0, 0))
    if pw:
        col = img.crop((w - 1, 0, w, h)).resize((pw, h), Image.NEAREST)
        out.paste(col, (w, 0))
    if ph:
        row = out.crop((0, h - 1, w + pw, h)).resize((w + pw, ph), Image.NEAREST)
        out.paste(row, (0, h))
    return out, pw, ph


def _fmt_for(entry: dict, root: Path) -> str:
    alpha = entry.get("alpha")
    if entry["action"] == "reencode" and alpha == "dds":
        try:
            with Image.open(root / entry["path"]) as img:
                alpha = alpha_kind(img)
        except Exception:
            alpha = None
    return "DXT1" if alpha == "opaque" else "DXT5"


def alpha_kind(img: Image.Image) -> str:
    if img.mode not in ("RGBA", "LA", "PA"):
        return "opaque"
    a = img.getchannel("A")
    lo, hi = a.getextrema()
    if lo == 255:
        return "opaque"
    hist = a.histogram()
    if lo == 0 and hi == 255 and not any(0 < v < 255 for v in hist[1:255]):
        return "binary"
    return "gradient"


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
                img = Image.open(src)
                img.load()
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                w, h = img.size
                cap = entry["cap"]
                if max(w, h) > cap:
                    scale = cap / max(w, h)
                    img = img.resize(
                        (max(1, int(w * scale)), max(1, int(h * scale))),
                        Image.LANCZOS,
                    )
                    resized = True
                img, pw, ph = _pad_to_4(img)
                padded = bool(pw or ph)
                feed = tdp / f"{src.stem}.png"
                img.save(feed)
            else:  # reencode of uncompressed dds
                feed = src
            out = tdp / f"{src.stem}.dds"
            r = subprocess.run(
                [*cmd, "-nologo", "-f", fmt, "-m", "1", "-y", "-dx9",
                 "-o", str(tdp), str(feed)],
                capture_output=True, text=True, errors="replace",
            )
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
    manifest: list[dict] = []
    done = 0
    cmd = texconv_command(no_wine)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {}
        for e in todo:
            key = (str((root / e["path"]).parent).lower(), (root / e["path"]).stem.lower())
            if key in seen_stems:
                manifest.append({"path": e["path"], "ok": False,
                                 "note": "duplicate stem in the same folder"})
                continue
            seen_stems.add(key)
            futs[pool.submit(_convert_one, e, root, cmd)] = e
        for f in as_completed(futs):
            rec = f.result()
            manifest.append(rec)
            done += 1
            if done % 500 == 0:
                okc = sum(1 for m in manifest if m.get("ok"))
                print(f"  … {done}/{len(futs)} (ok: {okc})")

    ok = [m for m in manifest if m.get("ok")]
    ko = [m for m in manifest if not m.get("ok")]
    saved = sum(m["orig_bytes"] for m in ok) - sum(m["bytes"] for m in ok)
    print(f"Converted: {len(ok)} — failed: {len(ko)} — "
          f"size delta: {saved / 1e6:+.1f} MB")
    (out / "convert_manifest.json").write_text(
        json.dumps({"ok": ok, "failed": ko}, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Manifest: {out / 'convert_manifest.json'}")
    return 0
