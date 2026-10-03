# Changelog

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
