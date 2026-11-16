# tests/

Suite di test esaustiva per hoi4port (stdlib `unittest`, nessuna dipendenza extra).

```bash
python tests/run_all.py            # intera suite + riepilogo
python tests/run_all.py -k cutover # solo i test con "cutover" nell'id
python tests/smoke.py              # smoke end-to-end (fixture, nessun gioco)
python -m unittest discover -s tests -p "test_*.py"
```

I test sono tracciati in git e la CI (`ci.yml`) li esegue su Ubuntu e Windows:
`run_all.py` per la suite completa, `smoke.py` per l'end-to-end.

## File

| File | Contenuto |
|---|---|
| `run_all.py` | runner (discovery, filtro `-k`, riepilogo counts) |
| `_harness.py` | helper condivisi: `Workspace` (temp tree), `crafted_dds`, `MeshBuilder`, `make_scan`, `TEXCONV`/`CMD`, `clean_env`, `run_cli` |
| `test_fsutil.py` | encoding, JSON, hashing, install stamp, iteratori, sync robocopy, texconv resolution |
| `test_config.py` | precedenza CLI/env/toml, autodetect Steam (mockato), lettura singola del toml |
| `test_scanner.py` | riferimenti, sprite, missing/noise/suspect, indici, baseline con stamp |
| `test_normalize.py` | classificazione, move/repair, rewrite, idempotenza |
| `test_analyze.py` | classi, caps, alpha (`images.alpha_kind`), dedup, unused, report |
| `test_dds.py` | header DDS e `alpha_opaque` (equivalenza esatta con Pillow), gate PNG pass-through |
| `test_convert.py` | padding (`images.pad_to_4`), formati, texconv reale, guards |
| `test_texfix.py` | header DDS (`dds.read_header`), mips, pow2, fix reali, idempotenza |
| `test_meshscan.py` | parser .mesh binario, medians, baseline con stamp, report |
| `test_entityfix.py` | parser entity, ambient, inserimento cull_radius |
| `test_cutover.py` | rewrite/dedup/guardie, apply, verify |
| `test_validate.py` | validazione DDS, ledger, orfani, check_log |
| `test_cli.py` | CLI end-to-end con subprocess (pipeline completa) |
| `test_gui.py` | argv per fase validato contro `hoi4port.build_parser()`, severità log, settings persistiti, smoke del widget (skip senza display) |
| `BUGS.md` | catalogo bug trovati, con evidenza e fix proposti |
| `TEST_REPORT.md` | report della sessione: risultati, copertura, convenzioni |

## Convenzioni

- I bug trovati dalla suite sono descritti in `BUGS.md` (tutti risolti):
  ogni bug ha un test di regressione permanente con ID `BUG-xx` nel commento.
- Tutto hermetic: directory temporanee per test, Steam/vanilla mockati,
  subprocess con env pulito + `PYTHONPATH`.
- I test che invocano il vero `texconv.exe` vengono skippati fuori da Windows
  (`TEXCONV`/`CMD` da `_harness`).
