# Changelog

## 1.1.0 — 2026-11-17

Merge several mods into one, ship the launcher descriptor the game needs, and
let `check-log` read the game log for you. The performance and cleanup work of
the intermediate builds is included here: every 1.x build before this one was
withdrawn.

### Merging several mods (`sync`)

- `--mod` is repeatable in every command that takes it
  (`hoi4port sync --mod A --mod B --mod C`), `HOI4PORT_MOD` accepts a
  `os.pathsep` list and `hoi4port.toml` accepts `mods = ["A", "B"]`.
- Sources are overlaid in load order into one staging tree: a file present in
  a later mod replaces the earlier one, exactly like enabling the mods together
  in the launcher, and a later mod never deletes another mod's files. The base
  mod is mirrored with `robocopy /MIR`; overlays use `/E /IS` so a same-size,
  same-timestamp file is still copied byte for byte.
- `sync` writes `merge_report.json` listing every overridden file per mod.

### The generated descriptor

After the merge `sync` writes `<staging>/descriptor.mod` **and**
`<staging>.mod`, the file the launcher actually reads:

| Field | Rule |
|---|---|
| `name` | the staging folder name, never the last source's name |
| `version`, `supported_version` | the **oldest** value among the sources, numeric order (`1.9 < 1.14 < 1.15`, `*` = 0), original spelling kept; `version` falls back to `1.0` and `supported_version` is omitted when no source declares it |
| `tags` | case-insensitive union of the sources' `tags` and `categories`, first spelling kept |
| `replace_path` | union of every entry the sources declare, one line each, separators normalized and trailing slashes/duplicates removed |
| `picture` | `thumbnail.png` when the merged tree has one |
| `path` | absolute POSIX path of the mod root (the documented form) |

The descriptor is written *after* the mirror, so the `descriptor.mod` files
copied from the sources cannot overwrite it. `sync --no-descriptor` (or the GUI
checkbox *Generate mod descriptor*) keeps whatever the sources shipped.

Why it matters: a launcher `.mod` without `replace_path` makes vanilla and the
mod load the same directories. On a real 44 000-file merge that produced
**505 `State ID conflict`**, **466 `Failed to create id … already exists`** and
an access violation at load.

### Validation and diagnostics

- **`validate` checks the descriptor**: it compares `<staging>.mod` with
  `descriptor.mod` (name, version, `supported_version`, tags, `replace_path`,
  presence of `path`) and counts state ids shared with vanilla, naming the
  missing `replace_path` instead of leaving it to the game log.
- **`check-log` classifies the whole `error.log`**: one line per error class
  with counts and an example (`State ID conflict`, duplicate id, malformed
  token, invalid trigger/effect, missing autonomy_state/texture/asset, invalid
  `supported_version`), the latest `crashes/hoi4_*` exception, exit code 1 when
  a load-breaking class is present, `--tail N` for a partial scan.
- DDS dimensions that are not a multiple of 4 are no longer errors: vanilla
  HoI4 ships 11 745 of 21 898 that way (111x106 achievements, 500x250 event
  pictures) and loads them. On the real mod that rule produced 3 664 phantom
  errors; it is now a reported count.
- The real finding behind them is reported instead: 82 `.dds` files that are
  not DDS at all (PNG bytes with a dds extension, plus empty files).
  `convert` names the cause (`not a dds (PNG data), left untouched`) and
  `validate` lists them.
- Any `*SpriteType` block registers its sprite name, not just `spriteType` and
  `corneredTileSpriteType`, so `textSpriteType` labels are no longer reported
  as missing GFX.

### GUI

`hoi4port gui` / `hoi4port-gui.exe`, no new dependency, same engine as the CLI.

- Mods list (any number, load order) with `Add…`, `Remove`, `↑`/`↓` and
  `Detect installed mods`; the ten stages with per-stage status (`✓ 2.4 s`,
  `● running`, `✗ error`) and `writes`/`reads` tags; global `Apply changes`
  switch (off = dry run, turning it on asks which folder will change).
- Options: texconv workers, cap, cull radius, skip baseline, vanilla dedup,
  reference-repair only, continue after an error, generate descriptor.
- English interface, a tooltip on every control (420 ms, never topmost,
  clamped to the screen), a nine-line live log with severities, `Copy`,
  `Clear`, phase bar, `Cancel` (kills the process tree), `Open output`,
  `error.log` (runs `check-log`).
- `Auto` is real and per field: `texconv = min(16, max(2, logical CPUs))`,
  `cap` from the `.gui` sizes, `cull` = 100. The `Auto` button detects CPU, RAM,
  free space and the HoI4 install, applies all three and logs the reason for
  each decision; it never turns on `Apply changes`.
- Layout is computed from Tk's own request inside a scrollable canvas: no
  clipped or overlapping widget at 100 %, 133 % and 150 % display scaling.
- Shortcuts: `Ctrl+Enter` runs, `Esc` cancels, `Ctrl+L` clears the log. The
  last session (mods, paths, options, window size) lives in
  `<cache>/hoi4port/gui.json`.
- A test feeds every argv the window generates through the real
  `hoi4port.build_parser()`.

### Performance

Every produced file is byte-identical to the previous build; the whole pipeline
was run old-vs-new on the same mod and every staging file, `asset_plan.json`,
`scan.json`, `mesh_stats.json` and `validate.json` compared equal.

Measured on a synthetic mod (700 text files / 128k lines, 160 textures =
42.2 MB, 150 meshes, 600 vanilla `.gfx`; Windows 11, Python 3.13):

| Stage | before | after |
|---|---|---|
| `scan` | 2315 ms | **865 ms** (−63%) |
| `scan` with vanilla | 2938 ms | **1062 ms** (−64%) |
| `validate` | 2332 ms | **910 ms** (−61%) |
| `analyze.plan` | 195 ms | **164 ms** (−16%) |
| `texfix` on a 512² raw DDS | 389 ms | **41 ms** (−89%) |
| `import hoi4port` (fresh process) | 150 ms | **87 ms** (−42%) |

How (all output-preserving): one `scandir` walk builds an existence index and
reference tokens resolve by set lookup instead of one `stat()` per token; one
split per file plus a per-line comment fast path; vanilla sprite names cached
per install stamp; `dds.alpha_opaque` decides BC1/BC3 alpha from the pixel
bytes (0.8 ms instead of a 330 ms Pillow decode); raw DDS and 8-bit PNGs that
need no resize go to texconv without a decode/PNG round trip; pipeline modules
are imported per subcommand.

### Internal cleanup

- One DDS header reader (`pipeline/dds.py`), one Pillow helper
  (`pipeline/images.py`) and one thread-pool/texconv layer
  (`pipeline/parallel.py`, `tools.convert_to_dds`/`decode_to_png`) replace
  three header parsers, two `alpha_kind` copies, two padding implementations
  and the duplicated convert/texfix loops.
- One scan-index API and one convention-folder constant replace four copied
  index builders and five policy tuples; report counts come from a single
  `summary()` per module.
- `config` reads at most one `hoi4port.toml` per command; `texfix` reuses the
  plan it already computed; `analyze` reads each texture once for both the
  decode and the dedup hash; `meshscan` no longer copies one `bytes` object per
  token.
- `vanilla_hash.json` and `vanilla_mesh_stats.json` carry an install `stamp`
  (content hash of `launcher-settings.json`): a game update invalidates the
  cache instead of leaving stale hashes and medians behind, and old files are
  detected with a note.
- Dead code removed (unused constants/imports, unreachable branches,
  duplicated `events/` rule, no-op guards, local imports). `--help` documents
  all commands.
- The functional suite is tracked in git and executed by CI on Ubuntu and
  Windows (`run_all.py` + `smoke.py`); `run_all.py -k <substring>` works as
  documented.

Tests: **317**.

## 1.0.1 — 2026-10-03

Bug-fix release from the first exhaustive test session (suite: 206 tests, all
green).

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
