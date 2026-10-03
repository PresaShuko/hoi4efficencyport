"""F9 — Entityfix: audit + targeted rewriting of gfx/entities/*.asset.

- AUDIT (always): entities with more than one attach, attach to non-existent
  entities, entities without `cull_radius`.
- APPLY (--apply): adds `cull_radius = N` ONLY to entities instantiated by
  map/ambient_object*.txt (map cosmetics) that do not already define it.
  Gameplay entities (units, buildings) are left untouched: pop-in on armies
  at close zoom is not acceptable.

Generic text parser (no per-mod regex): `#` comments neutralized at unchanged
offsets, tokenization of identifiers/strings/braces, entity block = first
`entity = {` … balanced brace. Encoding-aware read/write via
fsutil.read_text/write_text.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import fsutil

# Numbers are tokens too: without them a numeric value (e.g. `cull_radius
# = 50`) breaks key detection and numeric attach nodes (`0 = "x"`) vanish.
TOKEN_RE = re.compile(
    r'"[^"]*"|[A-Za-z_][A-Za-z0-9_\.\-]*|[-+]?\d+(?:\.\d+)?|[{}=]')
KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\.\-]*")
NODE_KEY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\.\-]*")


def _neutralize_comments(text: str) -> str:
    out = list(text)
    in_str = False
    i = 0
    while i < len(out):
        c = out[i]
        if c == '"':
            in_str = not in_str
        elif c == "#" and not in_str:
            while i < len(out) and out[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _tokens(text: str) -> list[tuple[str, int]]:
    return [(m.group(0), m.start()) for m in TOKEN_RE.finditer(text)]


def _unquote(tok: str) -> str:
    return tok[1:-1] if tok.startswith('"') else tok


def _match_brace(tokens: list[tuple[str, int]], i_open: int) -> int:
    depth = 0
    for j in range(i_open, len(tokens)):
        if tokens[j][0] == "{":
            depth += 1
        elif tokens[j][0] == "}":
            depth -= 1
            if depth == 0:
                return j
    return len(tokens) - 1


def parse_entities(text: str) -> list[dict]:
    """Extract top-level entity blocks with the relevant fields."""
    toks = _tokens(_neutralize_comments(text))
    out: list[dict] = []
    i = 0
    while i < len(toks) - 2:
        if (toks[i][0] == "entity" and toks[i + 1][0] == "="
                and toks[i + 2][0] == "{"):
            i_open = i + 2
            i_close = _match_brace(toks, i_open)
            rec: dict = {"start": toks[i][1], "open": toks[i_open][1],
                         "close": toks[i_close][1], "name": "",
                         "pdxmesh": "", "has_cull": False, "attach": []}
            j = i_open + 1
            while j < i_close:
                tok, _ = toks[j]
                if (j + 2 < i_close and toks[j + 1][0] == "="
                        and KEY_RE.fullmatch(tok)):
                    key = tok
                    val_tok = toks[j + 2][0]
                    if key == "name":
                        rec["name"] = _unquote(val_tok)
                    elif key == "pdxmesh":
                        rec["pdxmesh"] = _unquote(val_tok)
                    elif key == "cull_radius":
                        rec["has_cull"] = True
                    elif key == "attach" and val_tok == "{":
                        a_open = j + 2
                        a_close = _match_brace(toks, a_open)
                        k = a_open + 1
                        while k < a_close:
                            k_tok = toks[k][0]
                            if (k + 2 < a_close and toks[k + 1][0] == "="
                                    and NODE_KEY_RE.fullmatch(k_tok)
                                    and k_tok != "name"):
                                v_tok = toks[k + 2][0]
                                if v_tok.startswith('"'):
                                    rec["attach"].append(_unquote(v_tok))
                                k += 3
                                continue
                            k += 1
                        j = a_close + 1
                        continue
                    elif val_tok == "{":
                        # unknown nested block (sound, animation, …): skip it
                        # whole so its fields can't leak into the entity
                        j = _match_brace(toks, j + 2) + 1
                        continue
                    j += 3
                    continue
                j += 1
            out.append(rec)
            i = i_close + 1
            continue
        i += 1
    return out


def defined_names(records: list[dict]) -> set[str]:
    return {r["name"] for r in records if r["name"]}


def parse_ambient_entities(root: Path, vanilla: Path | None) -> set[str]:
    """Entity names instantiated by map/ambient_object*.txt (staging, else
    vanilla: the engine uses the vanilla file when the mod doesn't redefine it)."""
    for base in (root, vanilla) if vanilla else (root,):
        if not base:
            continue
        for p in sorted((base / "map").glob("ambient_object*.txt")):
            text, _ = fsutil.read_text(p)
            text = _neutralize_comments(text)
            toks = [t for t, _ in _tokens(text)]
            names: set[str] = set()
            i = 0
            while i < len(toks) - 2:
                if toks[i] == "type" and toks[i + 1] == "=" and toks[i + 2].startswith('"'):
                    names.add(_unquote(toks[i + 2]))
                    i += 3
                    continue
                i += 1
            if names:
                return names
    return set()


def collect_asset_files(root: Path) -> list[Path]:
    base = root / "gfx" / "entities"
    return sorted(base.rglob("*.asset")) if base.is_dir() else []


def run(root: Path, out: Path, apply: bool, vanilla: Path | None,
        cull_radius: int = 100) -> int:
    files = collect_asset_files(root)
    if not files:
        print(f"No .asset in {root / 'gfx' / 'entities'}")
        return 2
    out.mkdir(parents=True, exist_ok=True)

    parsed: dict[Path, tuple[str, str, list[dict]]] = {}
    for p in files:
        text, enc = fsutil.read_text(p)
        parsed[p] = (text, enc, parse_entities(text))
    all_records = [r for _, _, recs in parsed.values() for r in recs]
    names_local = defined_names(all_records)
    names_vanilla: set[str] = set()
    if vanilla and (vanilla / "gfx" / "entities").is_dir():
        for p in collect_asset_files(vanilla):
            text, _ = fsutil.read_text(p)
            names_vanilla |= defined_names(parse_entities(text))
    known = names_local | names_vanilla
    ambient = parse_ambient_entities(root, vanilla)

    broken = sorted({t for r in all_records for t in r["attach"]} - known)
    no_cull = [r for r in all_records if not r["has_cull"]]
    targets = [r for r in all_records
               if not r["has_cull"] and r["name"] in ambient]

    lines = [f"Entityfix audit {Path(out / 'entity_report.txt').name}",
             f".asset files: {len(files)} — entities: {len(all_records)}",
             f"Total attach: {sum(len(r['attach']) for r in all_records)}",
             f"Ambient entities (ambient_object): {len(ambient)}",
             f"Attach to non-existent entities (staging+vanilla): {len(broken)}",
             f"Entities without cull_radius: {len(no_cull)} "
             f"(of which eligible ambient: {len(targets)})",
             "\nTop 10 entities by attach:"]
    for r in sorted(all_records, key=lambda r: -len(r["attach"]))[:10]:
        lines.append(f"  {len(r['attach']):>3} attach  {r['name'] or '(unnamed)'}")
    if broken:
        lines.append("\nBroken attach (first 15):")
        lines += [f"  {b}" for b in broken[:15]]

    edits: list[dict] = []
    if apply and targets:
        for p, (text, enc, recs) in parsed.items():
            ins: list[tuple[int, str]] = []
            for r in recs:
                if r["name"] in ambient and not r["has_cull"]:
                    nl = text.find("\n", r["open"])
                    pos = nl + 1 if nl != -1 and nl < r["close"] else r["open"] + 1
                    ins.append((pos, f"\tcull_radius = {cull_radius}\n"))
            if ins:
                new_text = text
                for pos, frag in sorted(ins, reverse=True):
                    new_text = new_text[:pos] + frag + new_text[pos:]
                fsutil.write_text(p, new_text, enc)
                edits.append({"file": p.relative_to(root).as_posix(),
                              "insertions": len(ins)})
    (out / "entityfix_manifest.json").write_text(
        json.dumps({"applied": apply, "cull_radius": cull_radius,
                    "edits": edits, "eligible": len(targets),
                    "broken_attach": broken}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    if apply:
        print(f"Applied cull_radius = {cull_radius} to {len(edits)} files "
              f"({sum(e['insertions'] for e in edits)} ambient entities)")
    else:
        print(f"Eligible (ambient without cull_radius): {len(targets)} — "
              "dry-run (use --apply).")
    (out / "entity_report.txt").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")
    print(f"Report: {out / 'entity_report.txt'}")
    return 0
