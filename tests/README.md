# tests/

Exhaustive test suite for hoi4port (stdlib `unittest`, no extra dependency).

```bash
python tests/run_all.py            # whole suite + summary
python tests/run_all.py -k cutover # only the tests whose id contains "cutover"
python tests/smoke.py              # end-to-end smoke (fixture, no game)
python -m unittest discover -s tests -p "test_*.py"
```

The suite is tracked in git and run by CI (`ci.yml`) on Ubuntu and Windows:
`run_all.py` for the whole suite, `smoke.py` for the end-to-end pass.

## Files

| File | Content |
|---|---|
| `run_all.py` | runner (discovery, `-k` filter, counts summary) |
| `_harness.py` | shared helpers: `Workspace` (temp tree), `crafted_dds`, `MeshBuilder`, `make_scan`, `TEXCONV`/`CMD`, `clean_env`, `run_cli` |
| `test_fsutil.py` | encoding, JSON, hashing, install stamp, iterators, robocopy sync, texconv resolution |
| `test_config.py` | CLI/env/toml precedence, Steam autodetect (mocked), single toml read |
| `test_scanner.py` | references, sprites, missing/noise/suspect, indexes, baseline with stamp |
| `test_normalize.py` | classification, move/repair, rewrite, idempotence |
| `test_analyze.py` | classes, caps, alpha (`images.alpha_kind`), dedup, unused, report |
| `test_dds.py` | DDS headers and `alpha_opaque` (exact equality with Pillow), PNG pass-through gate |
| `test_convert.py` | padding (`images.pad_to_4`), formats, real texconv, guards |
| `test_texfix.py` | DDS header (`dds.read_header`), mips, pow2, real fixes, idempotence |
| `test_meshscan.py` | binary `.mesh` parser, medians, baseline with stamp, report |
| `test_entityfix.py` | entity parser, ambient, `cull_radius` insertion |
| `test_cutover.py` | rewrite/dedup/guards, apply, verify |
| `test_validate.py` | DDS validation, ledger, orphans, descriptor check, `check_log` |
| `test_descriptor.py` | generated descriptor: parsing, oldest versions, tag union, `replace_path`, syntax, `sync` end-to-end, `--no-descriptor` |
| `test_cli.py` | CLI end-to-end through subprocess (full pipeline) |
| `test_gui.py` | per-stage argv validated against `hoi4port.build_parser()`, log severities, persisted settings, widget smoke (skipped without a display) |

## Conventions

- Everything is hermetic: temporary directories per test, Steam/vanilla
  mocked, subprocesses with a clean environment plus `PYTHONPATH`.
- Tests that invoke the real `texconv.exe` are skipped outside Windows
  (`TEXCONV`/`CMD` from `_harness`).
- Regression tests are named after the bug they cover (`BUG-xx` in the
  comment) so a fixed defect cannot come back silently.
