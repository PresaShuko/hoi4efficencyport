# hoi4port — Report sessione di test esaustiva

**Data**: 2026-10-03 · **Piattaforma**: Windows 11 (10.0.26300, x64) ·
**Python**: 3.13.7 · **Pillow**: 12.2.0 · **texconv**: bundled `pipeline/bin/texconv.exe`
(versione reale usata nei test di conversione).

## Risultato

```
run=206  failures=0  errors=0  expectedFailures=0  skipped=0   → OK
tests/smoke.py (pre-esistente)                                 → OK
```

La sessione ha trovato **10 bug** (dettaglio e fix in `BUGS.md`), tutti
**corretti e coperti da regressioni permanenti**. Durante la fase di bug-hunting
i test documentavano i bug aperti con `@unittest.expectedFailure` (15 test);
dopo i fix i decorator sono stati rimossi e l'intera suite gira verde.

Storico: prima dei fix la suite segnava 205 test con 15 expected failure;
oggi 206 test, 0 fallimenti (il 206° è il test aggiunto per la priorità di
regole in `classify_token`, emerso correggendo BUG-06).

> **Aggiornamento 1.1.0 (2026-11-16)**: dopo il refactor di pulizia la suite
> conta **205 test**, tutti verdi. Il test rimosso è
> `test_texfix.ExpectedMipsReference`, tautologico (`texfix.expected_mips` è
> importato da `meshscan`); la copertura dei vettori mip resta in
> `test_meshscan.ExpectedMipsTest`. Aggiunto il filtro `run_all.py -k`.
>
> **Aggiornamento 1.2.0 (2026-11-16)**: **228 test**, tutti verdi. Nuovi:
> `test_dds` (equivalenza esatta `alpha_opaque` ↔ Pillow, gate PNG),
> regressioni byte-per-byte dei percorsi diretti `texfix`/`convert`,
> `tree_size` vs `rglob`, cache sprite vanilla, indice di esistenza
> (directory, case, `..`). La pipeline completa è stata inoltre confrontata
> old-vs-new: staging identico byte per byte.
>
> **Aggiornamento 1.3.0 (2026-11-17)**: **254 test**, tutti verdi. Nuovo
> `test_gui.py` (26): ogni argv generato dalla GUI viene ripparsato con il
> parser reale del CLI — ha subito trovato `convert`/`texfix` a cui la GUI
> passava `--vanilla` (flag inesistente) —, classificazione delle severità del
> log (`errori: 0` non è un errore), `Settings` (roundtrip, file corrotto, tipi
> sbagliati, filtro fasi) e smoke del widget con `Tk()` (skippato senza
> display). Verifica end-to-end: la GUI reale ha eseguito
> `sync → scan → analyze → convert --apply` su una fixture, producendo i DDS e
> il manifest, 0 errori.
>
> **Aggiornamento 1.4.0 (2026-11-17)**: **262 test**, tutti verdi. Nuovi in
> `test_gui.py`: copertura dei tooltip (ogni controllo richiesto ha una
> spiegazione ≥ 40 caratteri, ogni fase ≥ 60 e coerente con scrive/legge),
> `plan_auto` (workers da thread con floor 2 e cap 16, avviso sotto 5 GB,
> vanilla assente, RAM/spazio sconosciuti) e smoke di `Auto` sulla finestra
> reale.

## Come rieseguire

```bash
python tests/run_all.py            # intera suite (offline, hermetic)
python -m unittest discover -s tests -p "test_*.py"   # equivalente stdlib
python tests/smoke.py              # smoke end-to-end pre-esistente
```

Suite: ~12 s su Windows (i test texconv invocano il vero `texconv.exe`).
Nessun accesso a Steam/vanilla reali: Steam è mockato, tutto gira in
`TemporaryDirectory`, i subprocess CLI ricevono `PYTHONPATH` e un env senza
`HOI4PORT_*`.

## Fix applicati (→ dettaglio in `BUGS.md`)

| ID | File toccati | Fix |
|---|---|---|
| BUG-01 | `cutover.py` | `conv_ok` → cancella solo sorgenti non-.dds con gemello esistente |
| BUG-02 | `normalize.py` | `plan_moves` prepopola `taken` con i file su disco; destinazioni occupate → suffisso `_2` |
| BUG-03 | `entityfix.py` | `TOKEN_RE` con numeri; `KEY_RE`/`NODE_KEY_RE` costanti |
| BUG-04 | `entityfix.py` | chiavi numeriche accettate nel loop `attach` |
| BUG-05 | `entityfix.py` | blocchi annidati (≠ attach) saltati con `_match_brace` |
| BUG-06 | `scanner.py` | nomi definiti per file esclusi da `gfx_refs` |
| BUG-07 | `cutover.py` | gemello `.dds` richiesto prima di cancellare le sorgenti convertite |
| BUG-08 | `config.py` | bool toml esclusi da `_settings()` |
| BUG-09 | `validate.py` | `check_log` graceful su path inesistente (rc 2) |
| BUG-10 | `convert.py`, `texfix.py`, `cutover.py` | `out.mkdir(parents=True, exist_ok=True)` a inizio `run()` |

Verifica end-to-end dei due critici (stessi script del primo repro):
`convert --apply` + `cutover --apply` su un mod con DDS non compresso → il
`.dds` ri-codificato **sopravvive** (prima: cancellato); `normalize --apply`
con collisione di nome → file canonico **intatto**, stray rinominato
`goal_a_2.png` e riferimento riscritto (prima: sovrascritto).

## Perimetro e copertura

| File di test | Modulo | Cosa copre |
|---|---|---|
| `test_fsutil.py` | fsutil, tools | encoding (utf-8/BOM/cp1252/latin-1), newline, iteratori case-insensitive, sha256, sync robocopy con prune, risoluzione texconv (env/bundle/no-wine) |
| `test_config.py` | config | precedenza CLI > env > toml > default, cwd-relative, toml invalido/bool, resolve_workshop (path/ID/garbage/ambiguo), resolve_vanilla (esplicito/autodetect/warning) |
| `test_scanner.py` | scanner | strip commenti (anche in stringhe), sprite/cornered defs, token \ e //, exists mod+entity-relative, vanilla fallback, missing true/noise/suspect, noOfFrames, bucket quoted/bare, baseline hash+cache, report |
| `test_normalize.py` | normalize | regole dirette e catena sprite, conflitti multi-sprite, priorità single-sprite, TAG portrait, skip models/flags, struttura ui preservata, case-variant, repairs (slash//gemello estensione), rewrite \ e longest-first, dry-run, apply+ledger+verify, idempotenza, `--no-moves`, no-overwrite a destinazione |
| `test_analyze.py` | analyze | classi, caps per classe, gui cap (2×size, max-area), alpha opaque/binary/gradient, fourcc (crafted + Pillow raw), keep/reencode, skip models, duplicati, unused, vanilla_identical, report |
| `test_convert.py` | convert | padding %4 con replica bordi, scelta DXT1/DXT5 (anche riapertura DDS), conversione **reale** texconv (resize+pad verificate da header), twin guard, stem-conflict guard, dry-run, manifest, out dir |
| `test_texfix.py` | texfix | header DDS (offset 12/16/28/84), mip attesi (vettori pow2/non-pow2), needed_action (convert/fix/skip), pow2 floor, fix **reale** (DXT + catena mip completa + resize pow2/cap), collect da mesh_stats, run+idempotenza, out dir mancante |
| `test_meshscan.py` | meshscan | parser binario sintetico (verts/tris/materiali/bones/collision, multi-submesh), header invalido/tipo sconosciuto, errori non bloccanti, mediane pari/dispari, baseline cache, run+report |
| `test_entityfix.py` | entityfix | neutralize commenti a offset fisso, parse campi (valori numerici, nodi attach numerici, blocchi annidati), ambient (staging/vanilla/fallback), audit attach rotti, inserimento cull (solo ambient, posizione, idempotenza, entity mono-riga), vanilla names |
| `test_cutover.py` | cutover | rewrite estensione, esclusioni fonts, dedup (preferenza dds/refn/lessicografico), skip convenzione, vanilla dedup opt-in, ext-substitution guard, dest-never-deleted, sorgente senza gemello trattenuta, apply+verify, `--no-dedup`, reencode mai cancellato |
| `test_validate.py` | validate | header check (dimensioni %4, fourcc), moved-after-convert via ledger+size, unverifiable, mesh corrotto → rc 1, orfani, check_log filtri/default/path inesistente |
| `test_cli.py` | CLI e2e | `--version`, sync reale (robocopy) da fixture, errori mod, scan+env var, meshscan CLI, **pipeline completa** su mod sintetico (sync→scan→normalize→analyze→meshscan→entityfix×2→convert→cutover→texfix→validate→normalize no-op), error path rc |

### Helper (`_harness.py`)

- `Workspace`: albero temporaneo `modsrc/ → staging/ → build/`.
- `crafted_dds()`: header DDS 128 byte valido con fourcc/mips/dimensioni arbitrarie
  (usato per validare i parser header e come input reale per texconv).
- `MeshBuilder`: genera `.mesh` binari nel formato che `_parse_mesh` si attende.
- `run_cli()`: subprocess `python -m hoi4port` con env pulito + `PYTHONPATH`.

## Convenzioni

- Ogni bug ha un test di regressione permanente con ID `BUG-xx` nel commento:
  se un test ricomincia a fallire, la regressione corrispondente indica dove.
- Tutto hermetic: directory temporanee per test, Steam/vanilla mockati,
  subprocess con env pulito + `PYTHONPATH`.
- I test che invocano il vero `texconv.exe` vengono skippati automaticamente
  su piattaforme senza `texconv.exe` (CI Linux): la suite resta verde anche lì.
