"""F8 — Texfix: compliance of MODEL textures (gfx/models, gfx/entities and
those referenced by the .mesh materials): full mip chain (texconv -m 0)
and DXT1/DXT5 format. Does not touch 2D UI (the domain of 'convert').

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
import struct
import subprocess
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from .convert import alpha_kind
from .meshscan import expected_mips
from .tools import texconv_command

TARGET_DIRS = ("gfx/models", "gfx/entities")
TARGET_EXTS = {".dds", ".png", ".tga", ".jpg", ".jpeg", ".bmp"}
GOOD_FOURCC = {"DXT1", "DXT5"}


def dds_header(path: Path) -> dict | None:
    try:
        with path.open("rb") as f:
            head = f.read(128)
    except OSError:
        return None
    if len(head) < 128 or head[:4] != b"DDS ":
        return None
    h, w = struct.unpack_from("<II", head, 12)
    (mips,) = struct.unpack_from("<I", head, 28)
    four = head[84:88].decode("ascii", "replace")
    return {"w": w, "h": h, "mips": mips, "fourcc": four}


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
    hdr = dds_header(path)
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
    return img.resize((pw, ph), Image.LANCZOS), True


def _run_texconv(tdp: Path, feed: Path, fmt: str, cmd: list[str]) -> int:
    r = subprocess.run(
        [*cmd, "-nologo", "-f", fmt, "-m", "0", "-y", "-dx9",
         "-o", str(tdp), str(feed)],
        capture_output=True, text=True, errors="replace",
    )
    return r.returncode


def _texconv_png(img: Image.Image, tdp: Path, stem: str, fmt: str,
                 cmd: list[str]) -> Path:
    feed = tdp / f"{stem}.png"
    img.save(feed)
    r = _run_texconv(tdp, feed, fmt, cmd)
    out = tdp / f"{stem}.dds"
    if r != 0 or not out.exists():
        raise RuntimeError(f"texconv rc={r}")
    return out


def _fix_one(path: Path, cap: int | None, cmd: list[str]) -> dict:
    rel = str(path)
    rec: dict = {"path": rel, "action": "", "ok": False}
    try:
        size_before = path.stat().st_size
        plan = needed_action(path)
        if plan["action"] == "skip":
            rec.update({"ok": True, "skipped": True, "note": plan.get("note", "")})
            return rec
        target = path.with_suffix(".dds") if plan["action"] == "convert" else path
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            try:
                img = Image.open(path)
                img.load()
            except Exception:
                # format not decodable by Pillow (e.g. BC7): decode via
                # texconv to PNG and reopen.
                png_out = tdp / f"{path.stem}.png"
                r = subprocess.run(
                    [*cmd, "-nologo", "-ft", "png", "-y", "-dx9",
                     "-o", str(tdp), str(path)],
                    capture_output=True, text=True, errors="replace",
                )
                if r.returncode != 0 or not png_out.exists():
                    raise RuntimeError(
                        f"texconv decode rc={r.returncode} {r.stderr[-120:]}")
                img = Image.open(png_out)
                img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            w, h = img.size
            if cap is not None and max(w, h) > cap:
                scale = cap / max(w, h)
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                 Image.LANCZOS)
                rec["resized_cap"] = True
            img, pow2 = _to_pow2(img)
            rec["non_pow2_resized"] = pow2
            fmt = "DXT1" if alpha_kind(img) == "opaque" else "DXT5"
            stem = path.stem
            out_dds = _texconv_png(img, tdp, stem, fmt, cmd)
            shutil.copy2(out_dds, target)
            hdr = dds_header(target) or {}
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
    cmd = texconv_command(no_wine) if apply else []
    if not apply:
        shown = 0
        for pl, p in plans:
            if pl["action"] == "fix":
                if shown < 8:
                    print(f"  fix: {p.relative_to(root).as_posix()} — {pl.get('why', '')}")
                shown += 1
        print("dry-run: no file written (use --apply).")
        return 0
    manifest: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_fix_one, p, cap, cmd): p for p, _ in todo}
        for f in as_completed(futs):
            rec = f.result()
            manifest.append(rec)
            done += 1
            if done % 200 == 0:
                okc = sum(1 for m in manifest if m.get("ok"))
                print(f"  … {done}/{len(futs)} (ok: {okc})")
    ok = [m for m in manifest if m.get("ok")]
    ko = [m for m in manifest if not m.get("ok")]
    delta = sum((m.get("before_bytes") or 0) - (m.get("after_bytes") or 0)
                for m in ok)
    print(f"Texfix: {len(ok)} ok ({sum(1 for m in ok if m.get('skipped'))} skipped) — "
          f"failed: {len(ko)} — delta: {delta / 1e6:+.1f} MB")
    (out / "texfix_manifest.json").write_text(
        json.dumps({"ok": ok, "failed": ko}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    print(f"Manifest: {out / 'texfix_manifest.json'}")
    return 0
