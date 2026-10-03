"""F7 — Meshscan: automatic analysis of Clausewitz .mesh files (geometry, materials,
collision, bones) with an adaptive baseline on the installed vanilla.

The parser is streaming: it reads the header and structure (objects/properties)
and skips the numeric payloads, so it costs O(bytes) without ever materializing
the geometry. Format verified on game files: header b'@@b@', objects `[name\\0`,
properties `!`+len+name+type(i/f/s)+count+payload; meshes expose `p` (positions,
3/vertex), `tri` (indices, 3/triangle), `u0`, `n`, `ta`, and the materials
`shader`/`diff`/`n`/`spec` as strings.
"""
from __future__ import annotations

import json
import struct
from datetime import datetime
from pathlib import Path

from . import fsutil

MESH_CLASSES_FALLBACK = "other"


def expected_mips(w: int, h: int) -> int:
    m = max(w, h)
    if m <= 0 or (w & (w - 1)) or (h & (h - 1)):
        return 1
    return m.bit_length()


def _parse_mesh(path: Path) -> dict:
    data = path.read_bytes()
    if data[:4] != b"@@b@":
        raise ValueError(f"unrecognized mesh header: {path}")
    pos = 4
    submeshes: list[dict] = []
    cur_mesh: dict | None = None
    cur_obj = ""
    while pos < len(data):
        c = data[pos:pos + 1]
        if c == b"[":
            pos += 1
            while data[pos:pos + 1] == b"[":
                pos += 1
            end = data.index(b"\0", pos)
            cur_obj = data[pos:end].decode("latin-1")
            pos = end + 1
            if cur_obj == "mesh":
                cur_mesh = {"shader": "", "verts": 0, "tris": 0,
                            "diff": "", "n": "", "spec": "", "collision": False}
                submeshes.append(cur_mesh)
        elif c == b"!":
            pos += 1
            ln = data[pos]
            pos += 1
            name = data[pos:pos + ln].decode("latin-1")
            pos += ln
            t = data[pos:pos + 1].decode("ascii")
            pos += 1
            (cnt,) = struct.unpack_from("<i", data, pos)
            pos += 4
            if t == "s":
                (sl,) = struct.unpack_from("<i", data, pos)
                val = data[pos + 4:pos + 4 + sl].decode("latin-1").rstrip("\x00")
                pos += 4 + sl
                if cur_obj == "material" and cur_mesh is not None:
                    if name == "shader":
                        cur_mesh["shader"] = val
                        cur_mesh["collision"] = val.lower().startswith("collision")
                    elif name in ("diff", "n", "spec"):
                        cur_mesh[name] = val
            elif t in ("i", "f"):
                if cur_obj == "mesh" and cur_mesh is not None:
                    if name == "p":
                        cur_mesh["verts"] += cnt // 3
                    elif name == "tri":
                        cur_mesh["tris"] += cnt // 3
                elif cur_obj == "skin" and name == "bones" and cnt > 0:
                    if cur_mesh is not None:
                        cur_mesh["bones"] = struct.unpack_from("<i", data, pos)[0]
                pos += 4 * cnt
            else:
                raise ValueError(f"unknown property type {t!r} in {path}")
        else:
            raise ValueError(f"unexpected byte {c!r} at {pos} in {path}")
    for m in submeshes:
        m.setdefault("bones", 0)
    return {
        "path": "",  # filled in by the caller (relative to the root)
        "bytes": len(data),
        "verts": sum(m["verts"] for m in submeshes),
        "tris": sum(m["tris"] for m in submeshes),
        "submeshes": len(submeshes),
        "materials": len({(m["diff"], m["n"], m["spec"]) for m in submeshes}
                         - {("", "", "")}),
        "bones": max((m["bones"] for m in submeshes), default=0),
        "collision_verts": sum(m["verts"] for m in submeshes if m["collision"]),
        "detail": submeshes,
    }


def mesh_class(rel_path: str) -> str:
    parts = rel_path.split("/")
    if len(parts) > 2 and parts[0] == "gfx" and parts[1] == "models":
        return parts[2]
    return MESH_CLASSES_FALLBACK


def scan_dir(root: Path) -> list[dict]:
    out = []
    gfx = root / "gfx"
    if not gfx.is_dir():
        return out
    for p in sorted(gfx.rglob("*.mesh")):
        try:
            rec = _parse_mesh(p)
            rec["path"] = p.relative_to(root).as_posix().replace("\\", "/")
            rec["class"] = mesh_class(rec["path"])
            rec["ok"] = True
        except Exception as e:  # corrupt file: recorded, does not block the scan
            rec = {"path": p.relative_to(root).as_posix().replace("\\", "/"),
                   "ok": False, "note": repr(e)}
        out.append(rec)
    return out


def class_medians(stats: list[dict]) -> dict[str, int]:
    by_class: dict[str, list[int]] = {}
    for r in stats:
        if r.get("ok") and r["verts"] > 0:
            by_class.setdefault(r["class"], []).append(r["verts"])
    med = {}
    for cls, vals in by_class.items():
        vals.sort()
        n = len(vals)
        med[cls] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) // 2
    return med


def vanilla_baseline(vanilla: Path, cache: Path) -> dict:
    """Vanilla mesh baseline, computed once and cached (vanilla_hash.json
    pattern). Returns {class_median: {...}, files: n}."""
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    stats = scan_dir(vanilla)
    result = {"class_median": class_medians(stats), "files": len(stats)}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def write_report(stats: list[dict], baseline: dict | None, out_file: Path) -> None:
    ok = [r for r in stats if r.get("ok")]
    ko = [r for r in stats if not r.get("ok")]
    med = (baseline or {}).get("class_median", {})
    ts = datetime.now().isoformat(timespec="seconds")
    lines = [
        f"Meshscan {ts}",
        f"Meshes: {len(ok)} ok, {len(ko)} errors — total vertices: "
        f"{sum(r['verts'] for r in ok):,} — triangles: {sum(r['tris'] for r in ok):,}",
        f"Multi-material (≥2 draw calls): {sum(1 for r in ok if r['materials'] >= 2)}"
        f" — with mesh collision: {sum(1 for r in ok if r['collision_verts'] > 0)}",
    ]
    if med:
        lines.append("Vanilla baseline (median vertices per class): "
                     + ", ".join(f"{k}={v:,}" for k, v in sorted(med.items())))
        ranked = [r for r in ok
                  if med.get(r["class"], 0) > 0
                  and r["verts"] > 2 * med[r["class"]]]
        ranked.sort(key=lambda r: -(r["verts"] / med[r["class"]]))
        lines.append(f"\nOver 2× the vanilla median of their class: {len(ranked)}")
        for r in ranked[:25]:
            lines.append(f"  {r['verts'] / med[r['class']]:5.1f}×  "
                         f"{r['verts']:>7,}v {r['tris']:>7,}t  {r['path']}")
    lines.append("\nTop 25 by vertices:")
    for r in sorted(ok, key=lambda r: -r["verts"])[:25]:
        extra = f"  [collision {r['collision_verts']:,}v]" if r["collision_verts"] else ""
        extra += f"  [mat {r['materials']}]" if r["materials"] >= 2 else ""
        lines.append(f"  {r['verts']:>7,}v {r['tris']:>7,}t  {r['path']}{extra}")
    if ko:
        lines.append("\nParse errors (first 10):")
        for r in ko[:10]:
            lines.append(f"  {r['path']}: {r.get('note', '')}")
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(root: Path, out: Path, vanilla: Path | None, skip_vanilla: bool) -> int:
    if not (root / "gfx").is_dir():
        print(f"No gfx directory in {root}")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    print(f"Meshscan of {root} …")
    stats = scan_dir(root)
    (out / "mesh_stats.json").write_text(
        json.dumps(stats, indent=1, ensure_ascii=False), encoding="utf-8")
    baseline = None
    if vanilla and not skip_vanilla and (vanilla / "gfx").is_dir():
        baseline = vanilla_baseline(vanilla, out / "vanilla_mesh_stats.json")
        print(f"Vanilla baseline: {baseline['files']} meshes, "
              f"{len(baseline['class_median'])} classes")
    elif skip_vanilla:
        print("Vanilla baseline skipped (--skip-vanilla).")
    else:
        print("Note: vanilla not available — no per-class comparison.")
    write_report(stats, baseline, out / "mesh_report.txt")
    ok = [r for r in stats if r.get("ok")]
    print(f"Meshes: {len(ok)} ok, {len(stats) - len(ok)} errors — "
          f"vertices: {sum(r['verts'] for r in ok):,}")
    print(f"Output: {out / 'mesh_stats.json'} + mesh_report.txt")
    return 0
