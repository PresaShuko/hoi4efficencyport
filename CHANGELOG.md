# Changelog

## 1.1.0 — 2026-11-16

Internal cleanup release: no pipeline behaviour change, but three
compatibility notes.

- **Breaking**: `sync` no longer accepts `--out` (it never used it; the flag
  was silently ignored). Every other command is unchanged.
- **Breaking**: `vanilla_hash.json` and `vanilla_mesh_stats.json` now carry a
  `stamp` (content hash of the install's `launcher-settings.json`) and the
  vanilla hashes live under `hashes`. Old files are detected: `analyze`
  prints a note and skips vanilla dedup until the next full `scan`; baselines
  are recomputed automatically. A game update now invalidates the cache
  instead of leaving stale hashes/medians behind.
- Removed the unused `cutover.run(..., workshop)` parameter and the dead
  parse field `start`.

Cleanup:

- One DDS header reader (`pipeline/dds.py`) and one Pillow helper module
  (`pipeline/images.py`) replace three header parsers, two `alpha_kind`
  copies and two padding implementations.
- One thread-pool runner (`pipeline/parallel.py`) and one texconv invocation
  layer (`tools.convert_to_dds`/`decode_to_png`) replace the duplicated
  convert/texfix loops.
- One scan-index API (`scanner.index_by_token`, `index_by_token_lower`,
  `sprites_by_texture`) and one convention-folder constant replace four
  copied index builders and five policy tuples.
- Counts for the reports and the CLI come from a single `summary()` per
  module (scan, analyze, meshscan) instead of being recomputed in two places.
- `config`: at most one `hoi4port.toml` read per command (was up to four).
- `texfix` uses the plan it already computed (no second header read per
  texture); `scan` parses each quoted line once (the regex ran twice).
- `analyze` reads each texture once for both the Pillow decode and the
  dedup hash; `meshscan` no longer copies one `bytes` object per parsed
  token and its docstring no longer claims streaming.
- Dead code removed: unused constants/imports, unreachable branches,
  duplicated `events/` classification rule, `if not base` guard, no-op
  `.replace("\\", "/")` after `as_posix()`, no-op `direct if ... else`
  ternary, local imports.
- `--help` now documents all 11 commands (it listed 2).
- Test harness: shared `make_scan`, `TEXCONV`/`CMD`, `clean_env`;
  `run_all.py -k <substring>` implemented as documented; tautological
  mip test removed.
- The functional suite (`tests/`) is now tracked in git and executed by CI
  (`run_all.py` + `smoke.py`) instead of being ignored via `.git/info/exclude`.

## 1.0.1 — 2026-10-03

Bug-fix release from the first exhaustive test session (suite: 206 tests,
all green — see `tests/TEST_REPORT.md` and `tests/BUGS.md`).

- **Critical**: `cutover --apply` deleted freshly re-encoded `.dds` files
  (they were listed as "converted sources" in `convert_manifest.json`), and
  deleted converted sources even when the `.dds` twin never existed (stale
  manifest). Deletions from the conversion manifest now require a non-`.dds`
  source whose `.dds` twin actually exists.
- **Critical**: `normalize --apply` silently overwrote an existing asset when
  a planned destination path was already taken on disk. Destinations now
  account for every file on disk (suffix `_2`, `_3`, …) and references are
  rewritten to the new name.
- `entityfix`: the Clausewitz tokenizer now understands numeric tokens.
  `cull_radius = 50` is detected (no more duplicate insertions), numeric
  attach nodes (`attach = { 0 = "..." }`) are audited, and nested blocks
  (`sound = { ... }`) no longer overwrite entity-level `name`/`pdxmesh`.
- `scan`: spriteType definitions are no longer counted as references —
  "spriteType never used" works and the sprite→use classification in
  `normalize` is no longer polluted by the definition file location.
- `convert --apply`, `texfix --apply`, `cutover --apply` create the output
  directory themselves (first-ever run with no `build/` no longer crashes
  after touching the staging tree).
- `hoi4port.toml`: boolean values are ignored instead of becoming `"True"`.
- `check-log` exits cleanly (rc 2, message) when the given error.log path
  does not exist instead of raising.

## 1.0.0 — 2026-10-03

First "product" release: installable on any machine.

- Packaging: `pyproject.toml`, `hoi4port` entry point, texconv shipped
  inside the package (`pipeline/bin/`).
- Portable configuration: flags -> `HOI4PORT_*` env vars -> `hoi4port.toml`
  -> Steam library autodetect (Windows via registry, Linux, macOS).
- `--mod` accepts a numeric Workshop ID; `sync` guides the user by listing
  found mods when autodetect is ambiguous.
- `hoi4port --version`.
- Cross-platform: `check-log` finds the game error.log on Linux (Proton and
  native) and macOS too; transparent Wine wrapper for texconv (`--no-wine`
  to disable); CLI output forced to UTF-8 (survives cp1252 consoles).
- Fixed: `convert --apply` never re-encoded uncompressed DDS (twin-file
  guard also matched re-encodes); `--help` crashed on a `%` in a help
  string; crash when the vanilla install or the Workshop mod was missing.
- Removed every hardcoded path from the original machine; one-off legacy
  scripts deleted.

Pre-1.0: internal development (3D models plan: meshscan, texfix, entityfix,
extended validate).
