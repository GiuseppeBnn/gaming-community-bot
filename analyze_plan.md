# Roadmap di evoluzione strutturale — gaming-community-bot

> **Branch di lavoro: `test`.** `main` è allineato a `origin/main`.

## Principio guida: non reinventare la ruota

Prima di scrivere una sola astrazione custom, la domanda è: **esiste già, ed è
collaudata?** Nella maggior parte dei casi qui la risposta è sì, e spesso **la
dipendenza è già installata**. Sintesi delle scelte, prima delle fasi:

| Problema | Soluzione custom (da NON fare) | Soluzione collaudata | Già installato? |
|---|---|---|---|
| Parsing callback data | helper `split(":")` custom | **`aiogram.filters.callback_data.CallbackData`** — factory tipizzata, pydantic-backed, valida i campi e gestisce il limite 64 byte | **sì**, aiogram 3.13 |
| Confine transazionale | decoratore `@transactional` custom | estendere **`DbSessionMiddleware`** — è il pattern raccomandato da aiogram: la middleware apre la sessione e committa in uscita | **sì**, esiste già in `src/middlewares/db_middleware.py` |
| Handler globale errori | try/except a tappeto | **`dp.errors` + `ErrorEvent`** | **sì**, aiogram |
| Vincoli config | validator custom | **`pydantic.Field(ge=, le=)`** | **sì**, pydantic-settings |
| Migrazioni schema | lista `_MIGRATIONS` di stringhe DDL | **Alembic** | no, da aggiungere |
| Cache TTL (6 implementazioni a mano) | dict a livello di modulo + timestamp | **`cachetools.TTLCache`** | no, ~1 dipendenza |
| Lint / format / type check | convenzioni in STEERING.md | **ruff + mypy** | no, da aggiungere |
| Postgres nei test | mock del locking | **`services:` di GitHub Actions** (più semplice di testcontainers) | n/a, config CI |
| FSM persistente | — | **`aiogram.fsm.storage.redis.RedisStorage`** | parziale: `redis_url` già in config, storage `memory` di default |
| i18n / catalogo copy | modulo lexicon custom | **`aiogram.utils.i18n`** (gettext) — *oppure* estendere il registry `CommandDoc` già presente | aiogram sì, ma richiede Babel |

**Dove invece il custom resta giustificato**, e va detto esplicitamente:

- **Lo scheduler.** `scheduler_loop` è fatto in casa, ma è fatto *bene*: task persistiti
  in DB, sopravvive ai restart, `TaskSkip` distinto dagli errori, un rollback per task,
  notifica al creatore. APScheduler + `SQLAlchemyJobStore` non darebbe la semantica di
  dominio (skip/failed/notify) e aggiungerebbe una dipendenza per sostituire ~40 righe
  corrette. **Lasciarlo**, spostarlo solo di package.
- **I fake dei test.** `aiogram-tests` non è mantenuto in modo affidabile. La risposta
  giusta qui è centralizzare i `SimpleNamespace` esistenti in un `tests/fakes.py`, non
  adottare una libreria.
- **`catalog_loader` / CSV.** Già un buon design, nessuna libreria fa meglio per questo
  caso.

## Context

Il bot funziona: 700 test verdi, 16 router, ~190 handler registrati, economia + XP +
scommesse + quiz + shop in produzione. Il problema non è la mancanza di feature — è che
**il costo marginale di ogni feature nuova sta crescendo**, e alcune classi di bug sono
oggi *invisibili alla suite di test*.

Questa roadmap non aggiunge funzionalità. Interviene sulle proprietà del codice che
determinano quanto costerà tutto quello che verrà dopo: dove vivono le transazioni, cosa
può essere verificato automaticamente, e quali regole di STEERING.md sono eseguibili
invece che volontarie.

Il checkup precedente su `main` ha già isolato 2 blocker aperti (lock di riga inefficaci
sul path denaro; validazione input mancante su durate e callback). Questa roadmap li
inquadra come **sintomi di 3 cause strutturali**, e li risolve dentro un intervento più
ampio invece che come patch isolate.

**Stato attuale, misurato:**

| Metrica | Valore |
|---|---|
| `src/` | 15.419 righe, 71 file |
| Layer handlers | 7.266 righe (**47%** del totale) |
| File più grande | `src/handlers/quiz.py` — **1.820 righe**, 102 def, 30 callback |
| `session.commit()` chiamati da handler | **64 siti su 12 file** |
| Handler che importano SQLAlchemy | **18 su 20** |
| Tastiere costruite inline negli handler | 125 (vs 30 factory in `keyboards/`) |
| Copertura servizi | ~93% |
| Copertura handler | **~30-37%** (`quiz.py` 37%, da solo 21% di tutte le righe scoperte) |
| Copertura totale | 58%, **nessun `fail_under`** |
| Linter / formatter / type checker | **nessuno configurato** |
| Test su Postgres | **zero** (prod è `postgres:16`, test è SQLite in-memory) |
| Literal di copy italiano in `src/` | ~1.370, nessun lexicon |
| Handler globale errori (`dp.errors`) | **assente** |
| Indici sulla tabella `ledger` | **nessuno oltre la PK** |

---

## Le 3 cause strutturali

### Causa A — Il confine handler↔servizio è rotto in una direzione sola

I servizi sono puliti: 13 su 15 non importano aiogram, e la convenzione "i servizi non
committano mai" è rispettata. Ma **gli handler scavalcano i servizi**: 18 su 20 importano
SQLAlchemy, 64 siti chiamano `commit()`, 18 costruiscono `select()` a mano, e 2
(`quiz.py:1521`, `schedule.py:359`) aprono sessioni proprie bypassando il middleware.

Conseguenza diretta: **il transaction boundary appartiene al livello di presentazione**.
Non esiste unit-of-work. La correttezza transazionale non è testabile a livello di
servizio, e ogni nuovo handler è una nuova occasione di sbagliarla. La copertura
lo conferma: servizi 93%, handler 30%.

### Causa B — La suite di test non può vedere un'intera classe di bug

Prod è PostgreSQL 16. I test girano su **SQLite in-memory**, che:
- **no-oppa `SELECT ... FOR UPDATE`** — i test di locking asseriscono su un backend che
  non implementa la cosa testata. `tests/integration/test_economy_locking.py:5` lo
  documenta esplicitamente;
- accetta int64 in colonne `INTEGER`, quindi l'overflow di `betting_window_seconds`
  (`models.py:181`) e `total_wagered` (`models.py:204`) non si manifesta mai;
- rifiuterebbe la DDL `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` di `_MIGRATIONS`, che
  infatti **non ha nessun test**.

700 test verdi non sono una garanzia sui path denaro sotto concorrenza. Il Blocker 1 del
checkup (identity map + `with_for_update()`) è esattamente questo: reale in prod,
invisibile in test.

### Causa C — Le regole di STEERING.md non sono eseguibili

STEERING.md è 94KB, normativo, toccato da quasi ogni commit. Le sue regole sono buone
(§5 servizi non committano, §8 gating admin, §22 26 regole). Ma **niente le verifica**:
zero linter, zero formatter, zero type checker, zero `fail_under`, nessun pre-commit.
La CI ha un solo gate: pytest deve passare. `# noqa` compare 45 volte senza un linter che
lo onori — annotazioni morte. Zero `TODO`/`FIXME` in `src/`: non è pulizia, è debito non
tracciato.

Dove il progetto *ha* reso una regola strutturale, ha funzionato: il gate admin passa da
`IsAdminFilter` in 40+ punti, zero controlli manuali sparsi. È il modello da replicare.

---

## Roadmap ordinata per rapporto rischio/costo

### Fase 0 — Rete di sicurezza (prerequisito, ~1 giorno)

Da fare **prima** di qualsiasi refactor: senza questi, un refactor rompe cose in silenzio.

**0.1 — Handler globale errori.** Non esiste. Un'eccezione non gestita in un handler
muore nel log, l'utente vede il bot muto, la transazione resta appesa. Aggiungere in
`src/main.py` un `@dp.errors` che: logga con `exc_info`, include user_id/chat_id/callback
data, risponde all'utente un messaggio generico, e fa rollback. Trasforma ogni bug futuro
da "bot congelato" a "riga di log azionabile".

**0.2 — Gate statici in CI.** Aggiungere `ruff` (lint + format) e `mypy` a
`requirements-dev.txt` e uno step in `.github/workflows/tests.yml`. Partire permissivi:
ruff con le regole di default + `BLE`/`ASYNC`, mypy **non-strict** su `src/services/` e
`src/database/` soltanto. L'obiettivo non è la purezza, è impedire che il debito cresca.
Nota: `src/services/economy_service.py` ha **1 su 7 funzioni** con return type — i 6
scoperti sono `credit`, `debit`, `transfer`, `claim_daily`, `_get_wallet`, `get_history`,
cioè l'intero path denaro.

**0.3 — `fail_under` sulla copertura.** Fissare la soglia al valore attuale (58%) come
ratchet: non si può scendere. Non alzarla ora.

**0.4 — Allineare Python.** CI gira 3.11, il venv locale 3.12.13. Matrice `[3.11, 3.12]`
o allineamento secco.

### Fase 1 — Rendere verificabile il path denaro (~2-3 giorni)

Risolve la Causa B e chiude il Blocker 1 con una rete sotto.

**1.1 — Container Postgres in CI.** `services: postgres:16-alpine` in `tests.yml`, e un
marker `@pytest.mark.postgres` che gira solo dove `DB_URL` punta a Postgres. Serve anche
un `conftest` che parametrizzi l'engine.

**1.2 — Test di concorrenza reali.** 5-6 test, due sessioni parallele: doppio `/daily`,
doppio `transfer` sulla stessa coppia, doppio riscatto premio quiz, superamento del cap
XP giornaliero, doppia chiusura scommessa. **Devono fallire prima del fix** — è il punto.

**1.3 — Test sulle migrazioni.** `_MIGRATIONS` è DDL Postgres-only in un file al 56% di
copertura e senza nessun test. Un test che applica lo schema da zero + le migrazioni e
verifica la convergenza.

**1.4 — Fix del Blocker 1.** Per i check-then-write (`claim_daily`, cap XP, doppia
chiusura), sostituire il read-modify-write in Python con un **UPDATE condizionale +
controllo `rowcount`**:
```sql
UPDATE users SET last_daily_claim = :now
WHERE tg_id = :id AND (last_daily_claim IS NULL OR last_daily_claim < :threshold)
```
Immune sia alla identity map sia a `autoflush=False`. **Non** usare
`populate_existing=True` a tappeto: già provato, rompe 3 test perché sovrascrive le
modifiche in memoria non flushate (vedi memoria `sqlalchemy-lock-idmap`).

**1.5 — Larghezza colonne coerente.** `Wallet.coins` e `LedgerEntry.amount` sono
`BigInteger`, ma `BettingOption.total_wagered` (`models.py:204`) e `User.xp`
(`models.py:61`) sono `Integer` (int32). Denaro e XP che confluiscono in colonne più
strette di quelle da cui vengono. Uniformare a `BigInteger` + migrazione.

### Fase 2 — Validazione al confine (~1 giorno)

Chiude il Blocker 2 e ne previene la ricomparsa.

**2.1 — Vincoli sulla config.** `src/config_data/config.py` ha ~50 impostazioni,
**zero `Field(ge=, le=)`**, un solo validator (parsing `admin_ids`). `daily_min_hours`
ha un commento che dice "must stay < 24h" senza nulla che lo imponga. Aggiungere vincoli
a tutte le impostazioni numeriche: il commento diventa il vincolo.

**2.2 — Callback data tipizzate via `CallbackData` factory.** Oggi:
`int(callback.data.split(":")[2])` nudo, ripetuto (4 volte solo in
`betting.py:504, 551, 615, 695`, 4 in `quiz.py:985-1118`, 4 in `events.py:129-263`).
**Non scrivere un helper custom**: aiogram 3 ha già
`aiogram.filters.callback_data.CallbackData` — una factory pydantic-backed che dichiara i
campi con i loro tipi, li valida in ingresso, li serializza in uscita e verifica il limite
di 64 byte. Un payload malformato non arriva mai all'handler: il filtro non matcha.

```python
class BetWindow(CallbackData, prefix="bet:win"):
    event_id: int
    seconds: int

@router.callback_query(BetWindow.filter())
async def cb_bet_window(cb: CallbackQuery, callback_data: BetWindow): ...
```

Risolve insieme il Blocker 2, le 12+ duplicazioni di parsing e il rischio di superare i
64 byte. Migrazione incrementale, un prefisso alla volta.

**2.3 — Limitare `parse_duration`.** `schedule_service.py:69-84` è illimitata: input
pubblico da `/crea_scommessa` che porta a `OverflowError` e a `integer out of range` su
Postgres. Cap a un valore sensato (30 giorni).

### Fase 3 — Restituire ai servizi il confine transazionale (~1 settimana, incrementale)

Risolve la Causa A. **Da fare per moduli, non in blocco**, con i test della Fase 1 attivi.

**3.1 — Estendere `DbSessionMiddleware`, non inventare un unit-of-work.** Il pattern
raccomandato da aiogram è già mezzo implementato in `src/middlewares/db_middleware.py`:
la middleware apre la sessione e la inietta. Manca solo che **committi in uscita e faccia
rollback su eccezione**. Gli handler smettono di chiamare `commit()`, la transazione
diventa una proprietà del ciclo di vita dell'update invece che una scelta per-handler.
Zero dipendenze nuove, zero astrazioni nuove.

**3.2 — Spostare le query nei servizi.** Ordine per rapporto valore/rischio:
`economy.py` (23% cov, e reimplementa a mano `_targeting.py:45-52`, che già esiste) →
`betting.py` (36%, stessa query `selectinload` copiata 4 volte a
`:421, :469, :575, :654`) → `admin_betting.py` (21%, stessa scala di `except` di 12 righe
3 volte a `:186, :298, :406`) → `quiz.py` (37%, il pezzo grosso).

**3.3 — Spezzare `quiz.py`.** 1.820 righe, 30 callback, 13 factory di tastiere private,
649 statement scoperti. Separare per flusso: creazione/editing, esecuzione, viste. Le
tastiere private vanno in `keyboards/quiz_kb.py`.

**3.4 — Router aggregante.** `src/handlers/__init__.py` è **vuoto (0 byte)**. I 16
`include_router` sono a mano in `main.py:183-198`, con un commento load-bearing
(*"admin_betting MUST precede betting"*). L'ordine è correttezza affidata a un commento.
Spostare la registrazione in `__init__.py` con l'ordine dichiarato e un test che lo
verifica.

**3.5 — `scheduler_loop` fuori da `handlers/`.** È un daemon di background dentro il
package di presentazione (`handlers/schedule.py:355`). Va in `services/` o `runtime/`.
Il design in sé è corretto — task persistiti in DB, sopravvive ai restart, `TaskSkip`
distinto dagli errori, un rollback per task — solo collocato male.

### Fase 4 — Copy e presentazione (~2-3 giorni, opzionale ma ad alto ritorno)

**4.1 — Catalogo del copy.** ~1.370 literal italiani, 577 f-string nei soli handler,
`quiz.py` 194. Cambiare il tono del bot oggi = grep su 20 file. Già oggi lo stesso
messaggio ha **tre grafie diverse** (`admin.py:130, :164, :571`).

Due strade, la seconda è quella consigliata:
- `aiogram.utils.i18n` (gettext + Babel). Standard, ma il bot è monolingua italiano:
  paghi il toolchain `.po`/`.mo` per una feature che non serve. **Solo se un giorno
  servisse davvero il multilingua.**
- **Estendere il registry già presente nel repo.** `help_content.py` (dataclass
  `CommandDoc`) e `catalog_loader.py` (copy da CSV) sono due implementazioni riuscite
  dello stesso pattern, scritte da voi e già collaudate. Applicare lo stesso approccio al
  copy generale, iniziando dai messaggi di errore. Coerente con il codice esistente, zero
  dipendenze.

**4.2 — Fixture condivise per i test.** 78+ `SimpleNamespace` a mano su ~30 file per
fingere `Message`/`CallbackQuery`, una sola classe fake nominata, 18 fixture `autouse`
duplicate. Un aggiornamento di aiogram richiederebbe modifiche in ~30 punti. Un
`tests/fakes.py` con costruttori condivisi.

**4.3 — Spezzare STEERING.md.** 94KB, un file solo, letto interamente ad ogni modifica.
Dividere per dominio con un indice. Dove una regola può diventare un test (§5 "i servizi
non committano mai" → un test che grep-a `commit()` in `services/`), farla diventare un
test.

### Fase 5 — Scalabilità dati (quando i numeri lo giustificano)

**5.1 — Indici sulla tabella `ledger`.** `ledger` è la tabella che cresce più in fretta e
ha **zero indici oltre la PK**. `get_history` (`economy_service.py:217-224`) fa
`WHERE from_tg_id = X OR to_tg_id = X ORDER BY created_at DESC` — scan completo + sort ad
ogni `/storico`. Aggiungere indici su `from_tg_id`, `to_tg_id`, `created_at`.

**5.2 — Assunzione di processo singolo.** 6 cache mutabili a livello di modulo
(`admin_filter.py:25`, `group_guard.py:29`, `ban_guard.py:32`, `cooldown.py:25`,
`static_reply.py:24`, `fun_ai.py:40`) più i globali di `catalog_loader`. Sono 6
implementazioni a mano della stessa cosa: un dict `{chiave: (valore, scadenza)}`.
Sostituirle con **`cachetools.TTLCache`** — una dipendenza, sei implementazioni in meno,
scadenza e limite di dimensione gestiti (oggi nessuna delle sei ha un bound sulla
dimensione: crescono con il numero di utenti e non si svuotano mai).

Resta comunque l'assunzione di fondo: il bot **non può girare in 2 repliche** senza cache
incoerenti. Va bene finché è deliberato — va scritto in STEERING, non scoperto in
produzione. Se un giorno servisse scalare, la sostituzione è Redis (già in config per
l'FSM).

**5.3 — Alembic.** Questo è il "non reinventare la ruota" più netto del progetto.
`_MIGRATIONS` è una lista di stringhe DDL idempotenti, Postgres-only, senza versioning,
senza downgrade e **senza un solo test**. Funziona finché le migrazioni sono solo
`ADD COLUMN`. La Fase 1.5 (cambio larghezza colonne) è la prima che non lo è, quindi
questo punto va **anticipato prima della Fase 1.5**, non lasciato in coda.

Alembic con `autogenerate` a partire dallo schema attuale, prima revisione marcata come
baseline già applicata in produzione.

---

## Decisioni ancora aperte dal checkup precedente

- **Watchtower**: `containrrr/watchtower:latest` non pinnato, socket Docker in
  lettura/scrittura, auto-deploy in prod ad ogni push su `main`. Può leggere `BOT_TOKEN`,
  `GROQ_API_KEY`, password Postgres e `TELEGRAM_SESSION` via `docker inspect`. Scelta di
  postura, da confermare.
- **`bet_default_window_minutes`**: config morta, documentata come funzionante.
- **Semantica di `reset_quiz`** rispetto ai premi: incoerente.
- **Storage FSM.** `fsm_storage` è `memory` di default, ma `redis_url` è già in config e
  aiogram ha `RedisStorage` pronto. Con lo storage in memoria, **ogni riavvio del bot
  perde tutti i flussi FSM aperti** — un admin a metà creazione di un quiz riparte da
  zero. Con Watchtower che redeploya ad ogni push su `main`, succede spesso. Scelta da
  confermare: è accettabile o si passa a Redis?

---

## Verifica

Ogni fase è verificabile da sola:

- **Fase 0**: `ruff check src/` e `mypy src/services` passano in CI; un handler che
  solleva di proposito produce una riga di log con user_id e un messaggio all'utente;
  `pytest --cov-fail-under` fallisce se la copertura scende.
- **Fase 1**: i test di concorrenza **falliscono su `main` prima del fix** e passano
  dopo, su Postgres reale in CI. La suite SQLite resta verde in parallelo.
- **Fase 2**: `/crea_scommessa` con durata `999999999d` e una callback `bet:win:<enorme>`
  forgiata a mano producono un messaggio d'errore, non un crash. Test di regressione per
  entrambi.
- **Fase 3**: un test che asserisce zero `commit()` in `src/handlers/`; copertura degli
  handler in salita ad ogni modulo migrato; suite intera verde ad ogni passo.
- **Fase 4**: nessun test nuovo richiesto, ma `tests/fakes.py` deve sostituire i
  `SimpleNamespace` in almeno i 10 file più grossi senza cambiare le asserzioni.
- **Fase 5**: `EXPLAIN ANALYZE` su `get_history` prima/dopo gli indici.

**Branch**: si lavora su `test`. `main` è allineato a `origin/main` (da riverificare con
`git fetch` all'avvio — la verifica non è stata possibile durante la stesura).
