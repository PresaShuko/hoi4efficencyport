"""Meshscan: automatic analysis of Clausewitz .mesh files (geometry, materials,
collision, bones) with an adaptive baseline on the installed vanilla.

The file is read once into memory (size = the .mesh itself); the parser walks
only the structure (objects/properties) and skips the numeric payloads, so it
never materializes the geometry. Format verified on game files: header
b'@@b@', objects `[name\\0`, properties `!`+len+name+type(i/f/s)+count+payload;
meshes expose `p` (positions, 3/vertex), `tri` (indices, 3/triangle), `u0`,
`n`, `ta`, and the materials `shader`/`diff`/`n`/`spec` as strings.
"""
from __future__ import annotations

import json
import struct
from datetime import UTC, datetime
from pathlib import Path

from . import fsutil

MESH_CLASSES_FALLBACK = "other"

_BRACKET = ord("[")
_BANG = ord("!")
_TYPE_STRING = ord("s")
_TYPE_NUMERIC = (ord("i"), ord("f"))


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
    size = len(data)
    submeshes: list[dict] = []
    cur_mesh: dict | None = None
    cur_obj = ""
    while pos < size:
        marker = data[pos]
        if marker == _BRACKET:
            pos += 1
            while pos < size and data[pos] == _BRACKET:
                pos += 1
            end = data.index(b"\0", pos)
            cur_obj = data[pos:end].decode("latin-1")
            pos = end + 1
            if cur_obj == "mesh":
                cur_mesh = {"shader": "", "verts": 0, "tris": 0,
                            "diff": "", "n": "", "spec": "", "collision": False}
                submeshes.append(cur_mesh)
        elif marker == _BANG:
            pos += 1
            name_len = data[pos]
            pos += 1
            name = data[pos:pos + name_len].decode("latin-1")
            pos += name_len
            typ = data[pos]
            pos += 1
            (cnt,) = struct.unpack_from("<i", data, pos)
            pos += 4
            if typ == _TYPE_STRING:
                (str_len,) = struct.unpack_from("<i", data, pos)
                val = data[pos + 4:pos + 4 + str_len].decode("latin-1").rstrip("\x00")
                pos += 4 + str_len
                if cur_obj == "material" and cur_mesh is not None:
                    if name == "shader":
                        cur_mesh["shader"] = val
                        cur_mesh["collision"] = val.lower().startswith("collision")
                    elif name in ("diff", "n", "spec"):
                        cur_mesh[name] = val
            elif typ in _TYPE_NUMERIC:
                if cur_mesh is not None:
                    if cur_obj == "mesh" and name == "p":
                        cur_mesh["verts"] += cnt // 3
                    elif cur_obj == "mesh" and name == "tri":
                        cur_mesh["tris"] += cnt // 3
                    elif cur_obj == "skin" and name == "bones" and cnt > 0:
                        cur_mesh["bones"] = struct.unpack_from("<i", data, pos)[0]
                pos += 4 * cnt
            else:
                raise ValueError(
                    f"unknown property type {chr(typ)!r} in {path}")
        else:
            raise ValueError(f"unexpected byte {marker!r} at {pos} in {path}")
    for m in submeshes:
        m.setdefault("bones", 0)
    return {
        "path": "",  # filled in by the caller (relative to the root)
        "bytes": size,
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
        rel = p.relative_to(root).as_posix()
        try:
            rec = _parse_mesh(p)
            rec["path"] = rel
            rec["class"] = mesh_class(rel)
            rec["ok"] = True
        except Exception as e:  # corrupt file: recorded, does not block the scan
            rec = {"path": rel, "ok": False, "note": repr(e)}
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


def summary(stats: list[dict]) -> dict:
    """Counts and record buckets used by both the report and the CLI output."""
    ok = [r for r in stats if r.get("ok")]
    return {
        "ok": ok,
        "ko": [r for r in stats if not r.get("ok")],
        "verts": sum(r["verts"] for r in ok),
        "tris": sum(r["tris"] for r in ok),
        "multi_material": sum(1 for r in ok if r["materials"] >= 2),
        "with_collision": sum(1 for r in ok if r["collision_verts"] > 0),
    }


def vanilla_baseline(vanilla: Path, cache: Path) -> dict:
    """Vanilla mesh baseline, computed once and cached. The cache carries the
    install stamp so a game update invalidates it."""
    stamp = fsutil.install_stamp(vanilla)
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict) and cached.get("stamp") == stamp:
            return cached
    stats = scan_dir(vanilla)
    result = {"stamp": stamp, "class_median": class_medians(stats),
              "files": len(stats)}
    cache.parent.mkdir(parents=True, exist_ok=True)
    fsutil.write_json(cache, result)
    return result


def write_report(stats: list[dict], baseline: dict | None, out_file: Path) -> None:
    s = summary(stats)
    ok, ko = s["ok"], s["ko"]
    med = (baseline or {}).get("class_median", {})
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    lines = [
        f"Meshscan {ts}",
        f"Meshes: {len(ok)} ok, {len(ko)} errors — total vertices: "
        f"{s['verts']:,} — triangles: {s['tris']:,}",
        f"Multi-material (≥2 draw calls): {s['multi_material']}"
        f" — with mesh collision: {s['with_collision']}",
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
    fsutil.write_json(out / "mesh_stats.json", stats)
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
    s = summary(stats)
    print(f"Meshes: {len(s['ok'])} ok, {len(s['ko'])} errors — "
          f"vertices: {s['verts']:,}")
    print(f"Output: {out / 'mesh_stats.json'} + mesh_report.txt")
    return 0
