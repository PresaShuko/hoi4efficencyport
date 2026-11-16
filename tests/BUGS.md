# hoi4port — Bug report (sessione di test esaustiva, 2026-10-03)

> **STATO: TUTTI RISOLTI** (fix del 2026-10-03, versione 1.0.1).
> Ogni bug ha un test di regressione permanente (ex `@unittest.expectedFailure`,
> ora attivo e verde).

| ID | Gravità | Modulo | Sintesi | Stato |
|---|---|---|---|---|
| BUG-01 | **CRITICO** | `cutover.py` | Cutover cancellava i `.dds` appena ri-codificati | ✅ RISOLTO |
| BUG-02 | **CRITICO** | `normalize.py` | Una move sovrascriveva un file esistente a destinazione | ✅ RISOLTO |
| BUG-03 | ALTA | `entityfix.py` | Tokenizer senza numeri: `cull_radius = 50` invisibile | ✅ RISOLTO |
| BUG-04 | ALTA | `entityfix.py` | `attach = { 0 = "..." }` non parsed → audit attach cieco | ✅ RISOLTO |
| BUG-05 | MEDIA | `entityfix.py` | Blocchi annidati sovrascrivono i campi dell'entity | ✅ RISOLTO |
| BUG-06 | MEDIA | `scanner.py` | Definizioni sprite contate come riferimenti | ✅ RISOLTO |
| BUG-07 | MEDIA | `cutover.py` | Manifest stantio → sorgente cancellata senza .dds gemello | ✅ RISOLTO |
| BUG-08 | BASSA | `config.py` | `staging = true` nel toml → `Path("True")` | ✅ RISOLTO |
| BUG-09 | BASSA | `validate.py` | `check-log` con path inesistente → traceback | ✅ RISOLTO |
| BUG-10 | BASSA | `convert.py`, `texfix.py` | `--apply` senza `build/` → crash dopo modifiche | ✅ RISOLTO |

---

## BUG-01 — CRITICO: cutover cancellava i `.dds` ri-codificati

**Dove**: `pipeline/cutover.py`, `run()` → `deletions |= {p for p in conv_ok ...}`.

**Causa**: `convert` registra in `convert_manifest.json` anche i file `reencode`,
il cui `path` È il `.dds` stesso (riscrittura in place). `conv_ok` includeva quindi
`.dds` esistenti e referenziati, che finivano tra le "origini convertite" da
cancellare. Con un DDS non compresso nel mod (caso comune: la fixture stessa
`tests/mini_mod/gfx/fixture/test.dds` è `reencode`), `convert --apply` +
`cutover --apply` distruggevano il mod appena ottimizzato; il danno veniva
riportato in `verify.deleted_still_referenced` ma l'exit code restava 0.

**Fix applicato**: le cancellazioni da `conv_ok` includono solo sorgenti
**non-.dds** il cui gemello `.dds` esiste davvero (la stessa condizione copre
BUG-07). Regressione: `test_cutover.RunTest.test_reencoded_dds_is_not_deleted`.

## BUG-02 — CRITICO: normalize sovrascriveva file a destinazione

**Dove**: `pipeline/normalize.py` — `_unique_dest()` conosceva solo le
destinazioni già pianificate, non i file su disco; `shutil.move` sovrascriveva.

**Scenario**: `gfx/interface/goals/goal_a.png` (canonica, verde) + stray
`art/goal_a.png` (rossa) → la mossa `art → goals/goal_a.png` sostituiva il
file canonico, con `verify` tutto verde.

**Fix applicato**: `plan_moves` prepopola `taken` con tutti i file esistenti
sul disco (lowercase, posix): una destinazione già occupata riceve il suffisso
`_2`, `_3`, … e i riferimenti vengono riscritti sul nuovo nome (meccanismo
`locs` già esistente). Regressione:
`test_normalize.MoveOverwriteRegression.test_existing_destination_file_is_not_overwritten`.

## BUG-03 — ALTA: tokenizer entityfix senza numeri

**Dove**: `pipeline/entityfix.py` — `TOKEN_RE` escludeva i token che iniziano
con cifra; `parse_entities` perdeva quindi i valori numerici.

**Sintomi concatenati**: `cull_radius = 50` non riconosciuto (audit falso +
doppio inserimento con `--apply`); una chiave con valore numerico "divorava"
il blocco successivo (`j += 3` saltava `attach = {`); dopo l'inserimento di
`cull_radius = 100` il re-parse perdeva il `name` dell'entity.

**Fix applicato**: `TOKEN_RE` include `[-+]?\d+(?:\.\d+)?`; le regex di chiave
sono costanti module-level (`KEY_RE`, `NODE_KEY_RE`). Regressioni: 4 test in
`test_entityfix.py` (`test_cull_radius_numeric_value_detected`,
`test_no_duplicate_cull_insertion_when_already_present`,
`test_existing_cull_respected`, `test_single_line_entity_insertion`).

## BUG-04 — ALTA: attach con nodi numerici ignorati

**Dove**: stesso tokenizer; nel loop di `attach` la chiave (`0`, `1`, …) non
passava il fullmatch solo-identificatore.

**Fix applicato**: il loop di attach usa `NODE_KEY_RE`
(`[A-Za-z0-9_][A-Za-z0-9_\.\-]*`), che accetta chiavi numeriche; l'esclusione
di `name` interna ad attach resta. Regressione:
`test_numeric_attach_nodes_are_parsed` (`0 = "node_lod0"` → `["node_lod0", …]`).

## BUG-05 — MEDIA: blocchi annidati inquinavano l'entity

**Dove**: `parse_entities` gestiva solo `attach = {`; per le altre chiavi con
valore blocco (`sound`, `animation`, …) il contenuto veniva interpretato come
campi dell'entity (`name = "tree"` + `sound = { name = "treesound" }` →
`rec["name"] == "treesound"`).

**Fix applicato**: `chiave = {` con chiave ≠ `attach` salta all'intero blocco
con `_match_brace`. Regressione: `test_nested_block_does_not_leak_name`.

## BUG-06 — MEDIA: definizioni sprite contate come riferimenti

**Dove**: `scanner.scan` — `GFX_NAME_RE` matchava anche le righe
`name = "GFX_x"` dei blocchi `spriteType`: ogni sprite definito risultava
"referenziato" dalla sua stessa definizione.

**Conseguenze**: `gfx_unused_defs` sempre vuoto (feature morta),
`gfx_missing_static` sovrastimato, catena sprite→uso di `classify_token`
inquinata dalla posizione del file `.gfx`.

**Fix applicato**: `scan()` raccoglie i nomi definiti per file (`local_defs`)
e li esclude dalla raccolta `gfx_refs` nel file che li definisce.
Regressioni: `test_scanner.ScanTest.test_gfx_unused_defs`,
`test_normalize.ClassifyTokenTest.test_conflicting_uses_return_none` (ora su
due sprite con classi diverse; il caso "uno sprite, usi multipli" segue la
priorità di regole per design — nuovo test
`test_single_sprite_multi_use_uses_rule_priority`).

## BUG-07 — MEDIA: cancellazione su manifest stantio

**Dove**: `cutover.plan_cutover` — le sorgenti in `conv_ok` venivano cancellate
se esistevano, senza verificare che il `.dds` gemello fosse mai nato.

**Fix applicato**: stesso filtro di BUG-01 (gemello `.dds` richiesto per le
sorgenti non-dds in `conv_ok`). Regressione:
`test_cutover.RunTest.test_converted_source_without_twin_kept`.

## BUG-08 — BASSA: boolean nel toml

`_settings()` includeva i bool (`isinstance(True, int)`): `staging = true`
diventava `Path("True")`. **Fix**: filtro `not isinstance(v, bool)`.
Regressione: `test_config.PrecedenceTest.test_bool_toml_value_ignored`.

## BUG-09 — BASSA: check_log con path inesistente

`check_log(path=X)` con `X` inesistente → `FileNotFoundError` non gestito.
**Fix**: check `is_file()` con messaggio dedicato e `return 2`.
Regressione: `test_validate.CheckLogTest.test_explicit_missing_path_is_graceful`.

## BUG-10 — BASSA: `--apply` senza `build/` esistente

`convert.run`, `texfix.run` (e per uniformità `cutover.run`) non creavano mai
`out/`: `texfix --apply` al primo utilizzo crashava su
`texfix_manifest.json` **dopo** aver modificato lo staging. **Fix**:
`out.mkdir(parents=True, exist_ok=True)` a inizio delle tre `run`.
Regressione: `test_texfix.RunTest.test_apply_creates_missing_out_dir`
(il caso `convert` coperto da `test_apply_writes_manifest_into_out`).

---

## Note di design (non bug, invariate)

- `descriptor.mod` non è in `TEXT_EXTS`: il riferimento `picture = "thumb.png"`
  non viene scansionato (il thumb risulta orfano/unused).
- `validate` conta gli orfani con confronto case-sensitive: token con case
  diverso dal file ⇒ falso orfano.

Risolti nella pulizia 1.1.0: il ramo "doubled separator" morto di
`plan_repairs` è stato rimosso; `cmd_analyze` stampa il percorso reale di
`--out`; la prima riga di `entity_report.txt` è un timestamp.

