# Changelog

## 1.5.0 - 2026-11-17

Merging several mods, an English interface, and a layout that survives DPI
scaling.

- **Multiple mods, merged in `sync`.** `--mod` is repeatable in every command
  that takes it (`hoi4port sync --mod A --mod B --mod C`), `HOI4PORT_MOD`
  accepts an `os.pathsep` list and `hoi4port.toml` accepts
  `mods = ["A", "B"]`. Sources are overlaid in load order into one staging
  tree: a file present in a later mod replaces the earlier one, exactly like
  enabling the mods together in the launcher, and a later mod never deletes
  another mod's files. The descriptor comes from the last mod that has one.
  `sync --out build` writes `merge_report.json` listing every overridden file
  per mod. The base mod still uses `robocopy /MIR`; overlays use `/E /IS` so a
  same-size, same-timestamp file is still copied byte for byte.
- **Generated descriptor for the merged mod.** After the merge `sync` writes
  `<staging>/descriptor.mod` and `<staging>.mod` (the file the launcher reads,
  with `path="mod/<folder>"`): name = the staging folder name, `version` and
  `supported_version` = the **oldest** among the sources, `tags` = the
  case-insensitive union of their `tags`/`categories`, plus `picture` when a
  `thumbnail.png` is present. It is written *after* the mirror, so the
  descriptors copied from the sources cannot overwrite it. `sync
  --no-descriptor` (or the GUI checkbox) leaves whatever the sources shipped.
- **GUI: mods list.** Ordered list with Add…, Remove, ↑/↓ and "Detect
  installed mods": the order shown is the load order used by `sync`.
- **GUI: English.** Every label, tooltip, dialog and log line.
- **GUI: no more overlapping/clipped widgets.** The content lives in a
  scrollable canvas and the window is sized from Tk's own request instead of
  hand-computed pixel constants, so the footer stays inside the window at
  100 %, 133 % and 150 % scaling; on small screens the page scrolls. The log
  box is pinned to nine lines (a canvas default height used to stretch it) and
  the scrollbars are 1 px tall so they cannot inflate their container.
- **GUI: Auto is real and per field.** `texconv`, `cap` and `cull` each have an
  `auto` checkbox: texconv = `min(16, max(2, logical CPUs))`, cap = auto from
  the `.gui` sizes, cull = the CLI default 100. The `Auto` button detects CPU,
  RAM, free space and the HoI4 install, applies all three and logs the reason
  for each decision; it never turns on Apply changes.

Diagnostics, after validating a real 44 000-file merge:

- `validate` no longer treats DDS dimensions that are not a multiple of 4 as a
  problem: vanilla HoI4 ships **11 745 of 21 898** DDS that way (111x106
  achievements, 500x250 event pictures) and loads them, and reencoded sources
  keep their size on purpose. On that mod the rule produced **3 664 phantom
  errors**; it is now a reported count.
- The real finding behind them: 82 `.dds` files that are not DDS at all (PNG
  bytes with a dds extension, plus empty files). `convert` names the cause
  (`not a dds (PNG data), left untouched`) and `validate` lists them.
- Any `*SpriteType` block now registers its sprite name, not just `spriteType`
  and `corneredTileSpriteType`, so `textSpriteType` labels are no longer
  reported as missing GFX (vanilla sprite cache version bumped).
- Skipped conversions are counted separately ("Conversion skipped, original
  kept") instead of looking like header problems.

Tests: 303 (was 262), including the descriptor generator and its integration
through `sync`, the merge overlay (later wins, no purge,
same-size override), `resolve_mods` precedence, the GUI/CLI argv contract with
several `--mod` flags, sprite block types and the mislabeled-DDS detection.

## 1.4.0 - 2026-11-17

Interfaccia: spiegazioni e rilevamento della macchina.

- **Tooltip su ogni controllo** (campi, `Sfoglia`/`Rileva`, ognuna delle 10 fasi,
  interruttore, workers/cap/cull, i quattro flag, Esegui/Annulla/Apri output/
  error.log, log, barra fasi, pulsante Auto). Ritardo 420 ms, mai in primo piano,
  posizione limitata allo schermo; i test verificano che nessun controllo resti
  senza spiegazione e che i testi non siano generici.
- **Auto**: rileva thread logici, RAM (GlobalMemoryStatusEx), spazio libero sul
  volume della staging e install vanilla, poi imposta `workers` e vanilla e
  scrive nel log il motivo di ogni scelta. `workers = min(16, max(2, thread))`
  perché texconv satura la GPU; il cap resta `auto (.gui)` di proposito.
- Riga macchina nel footer (`12 thread · 31.8 GB RAM · C: 28 GB liberi`),
  aggiornata da Auto e al primo avvio.
- Al primo avvio (nessun `gui.json`) i workers vengono seminati dal profilo
  della macchina invece del default del CLI; con un file esistente le scelte
  dell'utente restano intatte.

## 1.3.0 - 2026-11-17

GUI Tkinter (`hoi4port gui`, `hoi4port-gui`, `hoi4port-gui.exe` windowed): nessuna
dipendenza nuova, stesso motore del CLI.

- Percorsi con `Sfoglia`/`Rileva` (mod, staging, output, vanilla) e percorso
  risolto mostrato accanto ai campi relativi.
- Le 10 fasi con stato per fase (`✓ 2.4 s`, `● in corso`, `✗ errore`), tag
  `scrive`/`legge`, interruttore globale `Applica modifiche` (spento = dry-run)
  con conferma che mostra la cartella che verrà modificata.
- Opzioni: workers texconv, cap, cull radius, skip baseline, dedup vanilla,
  solo-ref, continua dopo un errore.
- Log live 9 righe con severità (`errori: 0` non è un errore), `Copia`,
  `Svuota`, scrollbar custom, word-wrap; barra fasi, `Annulla` (kill del process
  tree), `Apri output`, `error.log` (`check-log`).
- Scorciatoie: `Ctrl+Invio` esegue, `Esc` annulla, `Ctrl+L` pulisce il log.
- Stato dell'ultima sessione in `<cache>/hoi4port/gui.json`.
- Build: `hoi4port-gui.exe` con `--windowed`; in esecuzione congelata la GUI
  rilancia sé stessa con il flag nascosto `--cli` (isolamento dei crash e
  cancellazione affidabile), mentre da checkout usa `python -m hoi4port`.
- `hoi4port.build_parser()` estratto: un test verifica che **ogni** argv
  generato dalla GUI sia accettato dal parser vero (ha trovato subito
  `convert`/`texfix` che non accettano `--vanilla`).
- `fsutil.cache_dir()` condivisa (prima privata in `scanner`).

Correzioni:

- `sync`: l'output di robocopy (riepilogo localizzato) viene decodificato con la
  code page OEM invece che UTF-8, quindi "Velocità"/"Terminato" non diventano
  più "Velocit?". Regressione coperta da un test.

Test: 254 (erano 229).

## 1.2.0 — 2026-11-16

Performance release. **Every produced file is byte-identical** to 1.1.0: the
whole pipeline was run old-vs-new on the same mod and every staging file,
`asset_plan.json`, `scan.json`, `mesh_stats.json` and `validate.json`
compared equal; `convert`/`texfix` outputs were compared byte by byte,
including the format choice.

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

How (all output-preserving):

- `scanner`: one scandir walk builds an existence index; reference tokens are
  resolved by set lookup instead of one `stat()` per token (`Path.exists()`
  costs ~20 µs on Windows). Entity-relative resolution is indexed too;
  absolute paths, `..`, padded cases still use the real `stat()`.
- `scanner`: one split per file plus per-line comment fast path, string
  suffix checks instead of `Path(...).suffix`, and lines without a quote or
  `GFX_` skip the corresponding regex.
- `scanner`: vanilla spriteType names cached per install stamp
  (`<cache>/hoi4port/vanilla_sprites_v1_<stamp>.json`; cache directory is
  `%LOCALAPPDATA%`, `$XDG_CACHE_HOME` or `~/Library/Caches`). Disabled when
  the install has no `launcher-settings.json`.
- `fsutil`: `iter_files` and `tree_size` use `os.scandir` (16–27× faster than
  `rglob`+`stat`); directory symlinks are not followed, like `Path.rglob`.
- `dds.alpha_opaque`: exact BC1/BC3 alpha decision for uncompressed 32-bit
  DDS read from the pixel bytes (0.8 ms instead of a 330 ms Pillow decode).
  It returns None for compressed/exotic/padded files, so the old path is
  used there; a differential test asserts the same answer as Pillow.
- `texfix`: raw DDS that needs no resize is fed to texconv directly (no
  Pillow decode, no PNG round-trip); format/mips unchanged.
- `convert`: 8-bit RGB/RGBA PNGs that need no resize/padding are fed to
  texconv directly instead of being decoded and re-saved.
- CLI: pipeline modules are imported per subcommand (PIL no longer loaded for
  `sync`, `scan`, ...).
- `analyze`: size comes from the bytes already read (no extra `stat`).

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
