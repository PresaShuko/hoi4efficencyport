"""Texfix: compliance of MODEL textures (gfx/models, gfx/entities and those
referenced by the .mesh materials): full mip chain (texconv -m 0) and
DXT1/DXT5 format. Does not touch 2D UI (the domain of 'convert').

Automatic fixes:
- PNG/TGA/JPG under models/entities → compressed .dds twin with mips;
- .dds with fourcc DXT3 (or other) or incomplete mip chain → reencode;
- non-pow-2 texture → resized down to the lower power of two (required for
  the mip chain), flagged in the manifest.
Idempotent: a compliant texture is skipped.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from PIL import Image

from . import dds, fsutil, images, parallel
from .meshscan import expected_mips
from .tools import convert_to_dds, decode_to_png, texconv_command

TARGET_DIRS = ("gfx/models", "gfx/entities")
TARGET_EXTS = {".dds", ".png", ".tga", ".jpg", ".jpeg", ".bmp"}
GOOD_FOURCC = dds.MODEL_FOURCC


def collect_targets(root: Path, out: Path) -> list[Path]:
    """Textures in gfx/models + gfx/entities, plus those referenced by the
    mesh materials (mesh_stats.json) wherever they live under gfx/."""
    files: set[Path] = set()
    for d in TARGET_DIRS:
        base = root / d
        if base.is_dir():
            files.update(p for p in base.rglob("*")
                         if p.is_file() and p.suffix.lower() in TARGET_EXTS)
    stats_file = out / "mesh_stats.json"
    if stats_file.exists():
        stats = json.loads(stats_file.read_text(encoding="utf-8"))
        for rec in stats:
            for m in rec.get("detail", []) or []:
                for key in ("diff", "n", "spec"):
                    ref = m.get(key, "")
                    if ref:
                        cand = root / "gfx" / ref.replace("\\", "/")
                        if cand.is_file() and cand.suffix.lower() in TARGET_EXTS:
                            files.add(cand)
    return sorted(files)


def needed_action(path: Path) -> dict:
    """Classify the work needed on a target texture."""
    ext = path.suffix.lower()
    if ext != ".dds":
        if path.with_suffix(".dds").exists():
            return {"action": "skip", "note": "twin .dds already exists"}
        return {"action": "convert"}
    hdr = dds.read_header(path)
    if hdr is None:
        return {"action": "fix", "why": "unreadable DDS header"}
    why = []
    if hdr["fourcc"] not in GOOD_FOURCC:
        why.append(f"fourcc {hdr['fourcc']!r}")
    if hdr["mips"] != expected_mips(hdr["w"], hdr["h"]):
        why.append(f"mips {hdr['mips']} != {expected_mips(hdr['w'], hdr['h'])}")
    if why:
        return {"action": "fix", "why": "; ".join(why),
                "w": hdr["w"], "h": hdr["h"], "mips": hdr["mips"],
                "fourcc": hdr["fourcc"]}
    return {"action": "skip", "note": "compliant"}


def _to_pow2(img: Image.Image) -> tuple[Image.Image, bool]:
    w, h = img.size
    pw = 1 << (max(1, w).bit_length() - 1)
    ph = 1 << (max(1, h).bit_length() - 1)
    if pw == w and ph == h:
        return img, False
    return img.resize((pw, ph), Image.Resampling.LANCZOS), True


def _is_pow2(n: int) -> bool:
    return n > 0 and not n & (n - 1)


def _needs_resize(w: int, h: int, cap: int | None) -> bool:
    """True when _fix_one would resize (cap or pow2 floor)."""
    if cap is not None and max(w, h) > cap:
        scale = cap / max(w, h)
        w, h = max(1, int(w * scale)), max(1, int(h * scale))
    return not (_is_pow2(w) and _is_pow2(h))


def _fix_one(path: Path, cap: int | None, cmd: list[str],
             plan: dict | None = None) -> dict:
    rec: dict = {"path": str(path), "action": "", "ok": False}
    try:
        size_before = path.stat().st_size
        plan = plan or needed_action(path)
        if plan["action"] == "skip":
            rec.update({"ok": True, "skipped": True, "note": plan.get("note", "")})
            return rec
        target = path.with_suffix(".dds") if plan["action"] == "convert" else path
        # Uncompressed DDS that needs no resize: format and mips are decided
        # from the header/pixels, so texconv can read the DDS directly. Same
        # pixels, same format choice, no Pillow decode and no PNG round-trip.
        hdr = dds.read_header(path) if path.suffix.lower() == ".dds" else None
        opaque = dds.alpha_opaque(path) if hdr is not None else None
        direct = (
            hdr is not None and opaque is not None
            and not _needs_resize(hdr["w"], hdr["h"], cap)
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            if direct:
                fmt = "DXT1" if opaque else "DXT5"
                out_dds = tdp / f"{path.stem}.dds"
                r = convert_to_dds(cmd, path, tdp, fmt, mips=0)
                if r.returncode != 0 or not out_dds.exists():
                    raise RuntimeError(f"texconv rc={r.returncode}")
                shutil.copy2(out_dds, target)
                rec.update({
                    "ok": True,
                    "action": plan["action"],
                    "why": plan.get("why", ""),
                    "fmt": fmt,
                    "mips": (dds.read_header(target) or {}).get("mips"),
                    "resized_cap": False,
                    "non_pow2_resized": False,
                    "before_bytes": size_before,
                    "after_bytes": target.stat().st_size,
                })
                return rec
            try:
                img = images.load_rgba(path)
            except Exception as e:
                # format not decodable by Pillow (e.g. BC7): decode via
                # texconv to PNG and reopen.
                png_out = tdp / f"{path.stem}.png"
                r = decode_to_png(cmd, path, tdp)
                if r.returncode != 0 or not png_out.exists():
                    raise RuntimeError(
                        f"texconv decode rc={r.returncode} {r.stderr[-120:]}"
                    ) from e
                img = images.load_rgba(png_out)
            img, resized_cap = images.resize_cap(img, cap)
            rec["resized_cap"] = resized_cap
            img, pow2 = _to_pow2(img)
            rec["non_pow2_resized"] = pow2
            fmt = "DXT1" if images.alpha_kind(img) == "opaque" else "DXT5"
            feed = tdp / f"{path.stem}.png"
            img.save(feed)
            out_dds = tdp / f"{path.stem}.dds"
            r = convert_to_dds(cmd, feed, tdp, fmt, mips=0)
            if r.returncode != 0 or not out_dds.exists():
                raise RuntimeError(f"texconv rc={r.returncode}")
            shutil.copy2(out_dds, target)
            hdr = dds.read_header(target) or {}
            rec.update({
                "ok": True,
                "action": plan["action"],
                "why": plan.get("why", ""),
                "fmt": fmt,
                "mips": hdr.get("mips"),
                "before_bytes": size_before,
                "after_bytes": target.stat().st_size,
            })
        return rec
    except Exception as e:
        rec["note"] = repr(e)
        return rec


def run(root: Path, out: Path, apply: bool, workers: int = 12,
        cap: int | None = None, no_wine: bool = False) -> int:
    out.mkdir(parents=True, exist_ok=True)
    targets = collect_targets(root, out)
    if not targets:
        print(f"No model textures in {root} (missing gfx/models, gfx/entities?)")
        return 2
    plans = [(needed_action(p), p) for p in targets]
    todo = [(p, pl) for pl, p in plans if pl["action"] != "skip"]
    by_action = Counter(pl["action"] for pl, _ in plans)
    print(f"Model textures: {len(targets)} — actions: {dict(by_action)}")
    if not apply:
        shown = 0
        for pl, p in plans:
            if pl["action"] == "fix":
                if shown < 8:
                    print(f"  fix: {p.relative_to(root).as_posix()} — {pl.get('why', '')}")
                shown += 1
        print("dry-run: no file written (use --apply).")
        return 0
    cmd = texconv_command(no_wine)
    manifest = parallel.run_pool(
        todo, lambda item: _fix_one(item[0], cap, cmd, item[1]),
        workers=workers, every=200)
    ok, ko = parallel.split(manifest)
    delta = sum((m.get("before_bytes") or 0) - (m.get("after_bytes") or 0)
                for m in ok)
    print(f"Texfix: {len(ok)} ok ({sum(1 for m in ok if m.get('skipped'))} skipped) — "
          f"failed: {len(ko)} — delta: {delta / 1e6:+.1f} MB")
    fsutil.write_json(out / "texfix_manifest.json", {"ok": ok, "failed": ko})
    print(f"Manifest: {out / 'texfix_manifest.json'}")
    return 0
