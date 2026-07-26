# Roadmap di evoluzione strutturale — gaming-community-bot (v2, verificata)

> Branch di lavoro: `test_giu`. Versione precedente di questo documento: git `e707867`.
>
> **Cosa cambia rispetto alla v1.** La v1 era una buona diagnosi con misure sbagliate.
> Ogni numero qui sotto è stato rimisurato eseguendo gli strumenti, non con `grep`.
> Tre raccomandazioni della v1 si sono rivelate infondate e sono state rimosse; una
> era attivamente pericolosa.
>
> **Stato: Fasi 0, 1a e 1b fatte.** Il path denaro non prende più decisioni su valori in
> cache, e ogni sito ha la sua guardia di regressione su Postgres vero. Le Fasi 2-5 e le
> decisioni aperte in fondo sono ancora da fare. Da qui in avanti, quando una sezione
> descrive un difetto al presente, sta descrivendo *com'era quando è stato misurato*: il
> registro serve a spiegare come lo si è saputo, non lo stato attuale del codice.

## Il criterio aggiunto: costo cognitivo

La v1 ordinava per rischio/costo. Manca un asse che per un progetto a manutentore
singolo conta di più: **quanti concetti nuovi devo tenere in testa dopo**. Un fix che
usa un pattern già presente nel repo costa zero; una dipendenza nuova con un
toolchain proprio (Alembic, Babel) costa per sempre. Ogni voce qui sotto è etichettata.

---

## Parte 1 — Cosa nella v1 era falso

### ❌ «`economy_service.py` ha 1 su 7 funzioni con return type — l'intero path denaro è scoperto»

**Falso, e in modo istruttivo.** Tutte e 7 le funzioni sono annotate. La v1 ha
prodotto quel numero con un `grep '^async def.*->'`, che non matcha le firme su più
righe — e in quel file **tutte** le firme sono su più righe. Verifica reale:

```
$ mypy --disallow-untyped-defs src/services/economy_service.py
(nessun errore)
```

Era la giustificazione più concreta della Fase 0.2, ed era un artefatto di misura.
La ragione **vera** per aggiungere mypy è migliore: il codebase è già a 0 errori, quindi
il gate si installa gratis.

### ❌ «Alembic va anticipato prima della Fase 1.5, perché il cambio di larghezza colonne è la prima migrazione che non è un `ADD COLUMN`»

**Falso.** `_MIGRATIONS` contiene già due `ALTER COLUMN ... TYPE BIGINT`
(`connection.py`, righe su `wallets.coins` e `ledger.amount`), in produzione, idempotenti,
funzionanti. Allargare `total_wagered` e `xp` è **una riga in più per colonna** nella
lista che esiste. La premessa che rendeva Alembic urgente non esiste.

Alembic resta una scelta legittima *quando* servirà un downgrade o una migrazione di
dati, non di schema. Oggi aggiunge `env.py`, `versions/`, l'autogenerate che va
riconciliato a mano fra SQLite e Postgres, e la cerimonia dello stamp del baseline:
**costo cognitivo alto, problema che ancora non esiste.**

### ❌ «6 cache a mano, nessuna con un bound: crescono con gli utenti e non si svuotano mai» → sostituirle con `cachetools`

**Falso su tutti e tre i punti.**

- Sono **4**, non 6 (`admin_filter`, `group_guard`, `ban_guard`, `fun_ai._last_used`).
- `group_guard` e `ban_guard` hanno `_CACHE_PRUNE_THRESHOLD = 4096`; `utils/cooldown`
  ha `_PRUNE_THRESHOLD = 1024` + `_PRUNE_MAX_AGE`. **Il bound c'è.**
- La v1 cita `cooldown.py:25` e `static_reply.py:24` come middleware: stanno in
  `src/utils/`, e `utils/cooldown.py` **è già** l'astrazione condivisa per questa cosa
  (il suo docstring dice esplicitamente che generalizza il throttle di `fun_ai`).

Quindi la voce corretta non è «aggiungi `cachetools`» (dipendenza nuova, rung 5) ma
«`fun_ai._last_used` dovrebbe usare `utils/cooldown` che esiste già» (rung 2). Costo
cognitivo: negativo — un concetto in meno. **Fatto**, quindi le cache a mano sono ora **3**.

### ❌ «45 `# noqa` senza un linter che li onori → annotazioni morte»

I `# noqa` sono 45, ma **morti sono 12** (`ruff --select=RUF100`). Gli altri 33 sono
soppressioni valide di regole reali. E la fotografia generale era assente: con le regole
di default ruff trova **133** problemi, di cui 90 autofixabili e quasi tutti cosmetici
(`UP045`, `UP037`, `I001`).

Questo cambia la Fase 0.2 in modo sostanziale. La v1 proponeva «default + `BLE`/`ASYNC`»:
sarebbe partita con **oltre 130 errori**, cioè un gate rosso al primo giro, che finisce
disattivato o annegato in `# noqa`. Calibrazione reale:

| Ruleset | Errori |
|---|---|
| `F` (pyflakes: nomi non definiti, import morti, f-string rotte) | **0** |
| `E9,F` | **0** |
| `E9,F,B` | 5 |
| `E9,F,B,ASYNC` | **6** |
| `+ RUF100, I` | 59 |
| default completo | 133 |

`E9,F,B,ASYNC` = 6 errori, tutti sistemati in pochi minuti → **il gate parte verde**.

### ⚠️ «Estendere `DbSessionMiddleware` perché committi in uscita — zero dipendenze, zero astrazioni nuove» (Fase 3.1)

Questa era la raccomandazione **più rischiosa** del documento, presentata come la più
sicura. Il problema non è il rollback (vedi sotto), è il commit in uscita:

1. La transazione resterebbe aperta per **tutta** la durata dell'handler, **incluse le
   chiamate alle API Telegram**. Con `with_for_update()` sul path denaro, i lock di riga
   verrebbero tenuti per la durata di un round-trip di rete verso `api.telegram.org`
   (centinaia di ms, secondi in caso di flood-wait e retry). Oggi gli handler committano
   *e poi* rispondono. Invertire quest'ordine rende il tempo di lock **dipendente dalla
   rete**: è una regressione di throughput e un rischio di contesa, non una pulizia.
2. Un handler che risponde all'utente a metà flusso e poi solleva manderebbe il
   messaggio «fatto» per una transazione che poi rollbacka.
3. Non è incrementale come dichiarato: i 64 siti di `commit()` diventano coerenti solo
   quando spariscono tutti insieme.

**Il rollback esplicito invece non serve affatto:** `DbSessionMiddleware` apre la sessione
con `async with async_session_maker()`, e uscire da quel blocco la chiude, scartando la
transazione non committata. La v1 dice «la transazione resta appesa» — non è vero.

Il confine transazionale nei servizi resta un obiettivo giusto (Causa A è reale). Ma la
strada è **spostare le query nei servizi** (3.2), non spostare il commit nella middleware.

### ❌ Numeri di inventario sbagliati (il documento era scritto su un albero più vecchio)

| Metrica | v1 | Reale |
|---|---|---|
| File in `src/` | 71 | **75** |
| Righe `handlers/` | 7.266 (47%) | **8.447 (55%)** |
| File handler | 20 | **22** |
| Handler che importano SQLAlchemy | 18 su 20 | **17 su 22** |
| `data.split(":")` negli handler | «12+» | **45** |

Conseguenza pratica: i riferimenti riga della v1 (`quiz.py:1521`, `betting.py:504`…)
non sono affidabili. E l'inventario **omette del tutto** `src/services/backup/`
(chat archive Telethon + export di stato) più `handlers/backup.py`: `backup/loop.py` è
a **0% di copertura**, il buco singolo più grosso dopo `quiz.py`, e non è mai citato.

---

## Parte 2 — Cosa nella v1 era giusto (confermato con misure)

| Affermazione | Stato |
|---|---|
| `src/` 15.419 righe | ✅ esatto |
| `quiz.py` 1.820 righe, 649 statement scoperti | ✅ esatto |
| 64 `commit()` in 12 file di `handlers/` | ✅ esatto |
| Copertura totale 58%, servizi alti, handler 21-44% | ✅ esatto (misurato su 712 test, prima della Fase 1a) |
| `dp.errors` assente | ✅ era assente |
| `handlers/__init__.py` vuoto (0 byte), ordine router in un commento | ✅ esatto |
| `ledger` senza indici oltre la PK | ✅ esatto |
| Zero `Field(ge=, le=)` in config | ✅ esatto (0 occorrenze di `Field(`) |
| `parse_duration` illimitata | ✅ **e peggio** — vedi sotto |
| SQLite nei test non implementa `FOR UPDATE` | ✅ esatto |
| **Blocker 1: i lock di riga non proteggono il path denaro** | ✅ **confermato sperimentalmente** |

### Il Blocker 1 è reale, ma la sua causa NON è quella che dicevamo

Il meccanismo è confermato: se una riga è già nella identity map, il
`SELECT ... FOR UPDATE` prende il lock sul DB ma **restituisce i valori Python
vecchi**, quindi il check-then-write passa due volte.

Ma la premessa sulla *portata* — presente sia nella v1 sia in questo documento fino a
ora — **era sbagliata**, e l'ha smentita l'harness Postgres appena costruito.

> «`DbSessionMiddleware` carica `User` e `Wallet` nella identity map a **ogni** update,
> quindi ogni handler eredita una sessione avvelenata.»

**Falso. La identity map di SQLAlchemy usa riferimenti *deboli*.** `_upsert_user()` carica
`User` e `Wallet` in variabili locali e non li conserva: appena la funzione ritorna gli
oggetti vengono garbage-collected e la identity map resta **vuota**. Misurato:

```
solo _upsert_user (nessuno tiene l'oggetto) -> FOR UPDATE legge xp=999  (fresco, sicuro)
un chiamante tiene lo User in una variabile -> FOR UPDATE legge xp=0    (STALE, bug)
```

Quindi il pericolo richiede un chiamante che **tenga vivo** l'oggetto attraverso una
chiamata che lockta la stessa riga. Leggerne un valore e scartare l'oggetto — come fa
`shop._balance`, che ritorna un `int` — è sicuro.

Non è una questione globale ma **per-flusso**, e questo riduce di molto il raggio d'azione:

| Flusso | Verdetto |
|---|---|
| `handlers/economy.cmd_daily` → `claim_daily` senza precarichi | ✅ sicuro |
| `handlers/admin_betting.cb_admin_confirm_resolve` → `resolve_event` diretto | ✅ sicuro |
| `handlers/event_types/bet_type._auto_lock` → lega `event`, controlla `event.status`, poi `lock_event` tenendo il riferimento | ❌ **è il flusso raggiungibile** |

Il test `TestMiddlewareDoesNotPoisonTheSession` in
`tests/integration/test_money_concurrency_pg.py` fissa questa proprietà: se un domani
qualcuno mettesse lo User in `data` (una cache «innocua»), il pericolo diventerebbe globale
su tutti i ~190 handler, e quel test lo direbbe subito.

### La diagnosi misurata: 9 siti vulnerabili, 5 già sicuri

> **Stato: tutti corretti.** Questa sezione resta come registro di *cosa era rotto e come lo
> si è saputo*. Gli xfail non esistono più: ogni test qui sotto è diventato la guardia di
> regressione del proprio sito (Fase 1b). Una sola voce si è rivelata mal diagnosticata — il
> doppio `lock_event` — ed è discussa nella Fase 1b.

Gli xfail erano `strict=True`, quindi «questo sito è rotto» non era un'affermazione ma un
fatto che la CI ricontrollava a ogni push — e un xpass **faceva fallire la build** per dire
«questo sito in realtà è sano».

**Vulnerabili (9 xfail):** doppio `/daily`; streak ricalcolata da valore stale; `debit`
che scopre da saldo stale; 20 `debit` concorrenti (**tutti** i task leggono 100 prima che
qualcuno committi, quindi 20 scritture di `100-10`: 100 CoInn creati); `transfer` che
scopre da saldo stale; cap XP giornaliero aggirato; `lock_event` doppio nel flusso
`_auto_lock`; puntata piazzata prima del resolve mai liquidata; `get_quiz(for_update=True)`
che legge uno status stale (e `award_prizes` non ha alcuna guardia di idempotenza).

**Già sicuri, ora guardie permanenti (5 verdi):** il middleware non trattiene entità;
`transfer` concorrenti conservano le monete; transfer opposti non vanno in deadlock
(l'ordinamento crescente per `tg_id` funziona); il resolve da sessione pulita
(`cb_admin_confirm_resolve`) paga una volta sola.

### …ma il fix della v1 va corretto in due punti

**a) La v1 propone `UPDATE` condizionale + `rowcount` come tecnica nuova. È già pattern
di casa.** Esistono tre usi in produzione:

- `bet_service.py` — `update(BettingOption).values(total_wagered=BettingOption.total_wagered + amount)`,
  col commento *«Atomic increment so concurrent bets on the same option can't lose updates»*
- `admin_service.py` — `update(Wallet).values(coins=Wallet.coins + amount)`
- `admin_service.py` — `update(User)…` **con controllo di `rowcount`**

Il fix non introduce un concetto: applica quello che il repo già usa e testa. Costo
cognitivo **zero**, non «medio». Analogamente `moderation_service.parse_duration` già
cappa con `min(..., _MAX_DURATION_SECONDS)`: sulla validazione durate il repo aveva già
ragione in un punto, e `schedule_service` era l'unico fuori linea.

**b) L'alternativa a 1 riga (`populate_existing=True`) è stata provata e ha un footgun
documentato.** La v1 dice «già provato, rompe 3 test» senza spiegare perché. Il
meccanismo, misurato: `populate_existing` **invalida le relazioni già caricate**
sull'oggetto. In SQLAlchemy async questo trasforma un accesso attributo funzionante in un
`MissingGreenlet`. Applicandolo agli 8 lock site: 1 test rosso
(`test_bet_locking.py::test_place_bet_rejected_after_lock`), perché `lock_event` non
carica `options` e il chiamante le usa.

È sicuro **solo** se la query porta gli stessi loader option che il chiamante usa. E
`User` riceve `selectinload(User.wallet)`/`badges` in tre punti
(`admin_service`, `badge_service`, `handlers/common`): con la copertura handler al
21-44%, **una suite verde non proverebbe che è sicuro**. Modifica ritirata.

**Conclusione:** l'ordinamento della v1 (Postgres in CI e test di concorrenza **prima**
del fix) era corretto, e questo esperimento lo dimostra dal lato pratico — il solo
segnale ottenuto veniva da un effetto collaterale, non dalla proprietà di concorrenza che
si voleva correggere. Non si tocca il path denaro prima di poterlo verificare.

### `parse_run_at` era illimitata quanto `parse_duration`, e la v1 non l'ha vista

Le due funzioni condividono `_REL_RE`. La v1 segnala solo `parse_duration`
(→ `betting_window_seconds`, colonna int32). Ma `parse_run_at` è chiamata da
`handlers/schedule.py` su testo admin e fa `now_local + timedelta(seconds=...)`:
`999999999d` dà **`OverflowError`**, che nessun handler intercetta (intercettano
`ValueError`). Il cap va nel punto condiviso, non in una delle due.

---

## Parte 3 — Fase 0: FATTA

Tutto verificato: **ruff pulito, mypy pulito, coverage ≥ gate 57%** (conteggi test aggiornati nella Fase 1a).

| # | Cosa | Costo cognitivo |
|---|---|---|
| 0.1 | **`dp.errors`** → `src/handlers/errors.py` + 9 test. Logga user_id/username/chat_id/callback_data/testo con `exc_info`, risponde all'utente (alert sulle callback), e **silenzia il rumore benigno** (`message is not modified`, `query is too old`) fermando comunque lo spinner del bottone. Nessun rollback: la middleware già scarta la transazione alla chiusura della sessione. | basso |
| 0.2 | **ruff `E9,F,B,ASYNC`** — calibrato per partire verde. I 6 findings sistemati includono `zip(others, schedule, strict=True)` sul payout premi (asserzione gratuita su un invariante di denaro) e `ASYNC240`: `dest.mkdir()` bloccante dentro una `async def` del backup, che su volume lento stalla l'intero event loop → `asyncio.to_thread`. | basso |
| 0.3 | **mypy non-strict** su `services/database/utils/config_data/filters` + plugin `pydantic.mypy` (nessuna dipendenza nuova). Ha trovato un finding vero sul **gate admin**: `is_admin(bot: Bot, ...)` riceve `message.bot`, che è `Bot \| None`. Falliva chiuso — ma solo perché un `except Exception` inghiottiva l'`AttributeError`, cioè **l'esito dell'autorizzazione dipendeva da un catch incidentale**. Ora il fail-closed è dichiarato. Chiude 4 errori in un punto. | basso |
| 0.4 | **`fail_under = 57`** come ratchet (sotto il 58,33% attuale, non un obiettivo). | nullo |
| — | **Cap durate condiviso** (`_rel_seconds`, 365gg) su *entrambi* i parser + 3 test di regressione. | nullo |
| — | **Indici `ledger`**: due composti `(from_tg_id, created_at)` e `(to_tg_id, created_at)` — non tre a colonna singola come proponeva la v1: il `WHERE a = X OR b = X` li usa in BitmapOr e ognuno porta già `created_at` per l'`ORDER BY`. In `models.py` (DB nuovi) **e** in `_MIGRATIONS` (prod esistente, `create_all` salta le tabelle già presenti). | nullo |

**Non fatto della v1 Fase 0:** allineare Python CI 3.11 / venv locale 3.12. Va deciso, non
indovinato: matrice `[3.11, 3.12]` o allineamento secco.

---

## Parte 4 — Roadmap rivista

### Fase 1a — La rete: FATTA (nessuna riga di `src/` modificata)

*Conteggi a fine Fase 1a: 724 verdi + 9 xfail con Postgres, 713 + 20 skip senza. Ruff e mypy
puliti. (I conteggi correnti sono in Fase 1b.)*

- **Harness Postgres**: fixture `pg_engine`/`pg_sessions`/`pg_session`/`pg_user_factory` in
  `tests/conftest.py`, tutte **aggiunte** (le fixture SQLite esistenti sono intatte).
  Marker `pg` registrato; `services: postgres:16-alpine` + `TEST_PG_URL` in `tests.yml`.
  Zero dipendenze nuove: `asyncpg` era già in `requirements.txt`.
  Senza `TEST_PG_URL` i test PG **skippano**, quindi il run locale resta senza Docker.
- **Guardia distruttiva**: `pg_engine` fa `drop_all`, quindi rifiuta ogni URL il cui DB non
  finisce in `_test` (il DB del compose si chiama `gamingbot`, a un carattere di distanza).
  Verificata a mano in tutti e tre i casi: skip / rifiuto / pass.
- **`_MIGRATIONS`: prima copertura, 7 test.** Applicazione su schema fresco, idempotenza,
  guardia sul dialetto, ri-aggiunta di colonne droppate da un deploy vecchio (con verifica
  che torni il `NOT NULL DEFAULT`, non solo la colonna), allargamento a BIGINT con un saldo
  oltre 2^31, e presenza dei due indici su `ledger` anche su tabella preesistente.
  Scoperta collaterale: su schema `create_all` quelle colonne **non** hanno default
  server-side (i modelli usano default Python), mentre `_MIGRATIONS` le ricrea **con**
  `DEFAULT` — un DB nuovo e uno migrato differiscono nella DDL.
- **Strumento diagnostico**: 14 test di gara, `xfail(strict=True)`. Risultato nella
  sezione «La diagnosi misurata» sopra.

### Fase 1b — Il fix: FATTA, un commit per sito

**746 test verdi con Postgres, 721 + 25 skip senza. Zero xfail. Ruff e mypy puliti.**

Sette commit, ognuno con la sua prova. La regola è stata la stessa ovunque, e alla fine si
riduce a una frase: **le decisioni sul denaro si prendono in SQL, non in Python.**

| # | Commit | Sito | Cosa è diventato |
|---|---|---|---|
| 0 | `test:` | `set_balance` | la gara che mancava alla rete (vedi sotto) |
| 1 | `fix:` | `claim_daily` | `UPDATE` condizionale; streak via `CASE` nella `SET` |
| 2 | `fix:` | `debit`/`credit`/`transfer` | aritmetica in SQL; letture per colonna |
| 3 | `fix:` | `grant_xp` | cap letto sotto lock, XP sommato in SQL |
| 4 | `fix:` | `lock_event`/`resolve_event` | transizione condizionale; bet riletti con una query |
| 5 | `fix:` | chiusura quiz | nuova `quiz_service.claim_close()` |
| 6 | `fix:` | `xp`, `total_wagered` | `BigInteger`, modello + `_MIGRATIONS` |

**Le tre regole che ne sono uscite** (ora regola 22 di `STEERING.md`): leggi le colonne, non
le entità (`select(Wallet.coins)` non può venire dalla cache, `select(Wallet)` sì); scrivi in
SQL e relativo dove puoi (`coins = coins + :delta` somma, `wallet.coins += x` sovrascrive);
`synchronize_session=False` sempre, poi `refresh` per non lasciare mentire l'istanza.

I lock restano in tre punti, e solo dove SQL da solo non basta: `transfer` (due righe insieme
⇒ deadlock possibile, ordine `tg_id` crescente), `lock_balance` per `set_balance` (un target
assoluto ha bisogno del valore corrente), e il ramo `capped` di `grant_xp` (un min/max contro
un valore memorizzato non ha una scrittura portabile PG/SQLite).

#### Quattro cose che il piano non aveva previsto

1. **`refresh` non è un dettaglio, è il contratto.** La prima versione del fix a `claim_daily`
   non lo faceva, e due test esistenti che asseriscono su `user.daily_streak` sono diventati
   rossi. Non era un difetto dei test: dice che il contratto «dopo la chiamata, l'entità in
   sessione riflette la scrittura» **è usato davvero**, quindi va mantenuto.
2. **…ma un `refresh` butta via una modifica pendente sullo stesso attributo** (`autoflush=False`
   ⇒ non è ancora andata al DB). Preso da un test unitario su `grant_xp` chiamato due volte
   nella stessa transazione. Scrivere in SQL invece di mutare l'istanza elimina il problema
   alla radice: non resta mai niente di pendente.
3. **Un secondo difetto in `grant_xp`, non nella lista.** Il ramo *uncapped* non prende lock
   (per scelta: lo prenderebbe nell'ordine sbagliato) e faceva `user.xp += n` in Python: 10
   grant concorrenti da 5 XP lasciavano **5**. Nove persi su dieci, senza nemmeno bisogno
   della identity map. Trovato leggendo la funzione, misurato prima di toccarlo.
4. **Una mia diagnosi era sbagliata.** Il test sul doppio `lock_event` asseriva
   `EventAlreadySettledError`, che non è mai stato il contratto (lockare un evento già lockato
   è idempotente **per definizione**). Falliva prima e dopo il fix per lo stesso motivo, quindi
   non misurava niente. Il difetto reale era un altro: l'evento trattenuto diceva ancora
   `open`, il controllo ripassava e `locked_at` — l'orario di chiusura vera delle scommesse,
   contro cui `place_bet` rifiuta — veniva riscritto più avanti. Test riscritto e verificato
   rosso sul codice vecchio prima di tenerlo.

#### Su `set_balance`, che si comportava al contrario di come pensavo

Non mancava il target: lo **centrava sempre**, perché l'ORM riscriveva un valore assoluto
calcolato da una base stale (`delta = target - stale` ⇒ `stale + delta = target` per
costruzione). A sparire era il movimento concorrente: ledger 250, wallet 200. E la funzione
riportava comunque `target`, quindi entrambi gli handler dicevano all'admin che il saldo era
200 quando non lo era. Corollario che rende il test indispensabile: sistemare `credit`/`debit`
da solo avrebbe **rotto** questa compensazione accidentale.

#### Verificato che NON serviva toccare

`place_bet` e `cancel_event` leggono lo stato dopo un `FOR UPDATE`, ma i loro handler chiamano
il servizio **senza trattenere niente**: la lettura che locka è la prima della riga, quindi
funzionano già. Nessuna modifica speculativa lì.

#### Resta fuori, e per un motivo

`xp_service.airdrop_xp` cicla su tutti gli utenti mutando `user.xp` in Python — stessa forma di
difetto, ma **nessun test lo prova** e serve che due admin lancino un airdrop nello stesso
istante. La regola che mi sono dato per tutta la fase è: prima la misura, poi il fix. Vale
anche quando il fix sarebbe di una riga.

### Fase 2 — Validazione al confine

- **Vincoli config** (`Field(ge=, le=)`): mettili dove un valore invalido causa un danno
  reale, partendo da `daily_min_hours` (ha un commento che dice «must stay < 24h» e nulla
  che lo imponga). **Non** su tutte e 50 le impostazioni per simmetria. Costo: nullo.
- **`CallbackData` factory**: la v1 la vende come fix del Blocker 2. Ridimensionata: con
  `dp.errors` attivo (0.1), una callback malformata è già una riga di log + un messaggio
  all'utente invece di un bot muto — l'urgenza di sicurezza è chiusa. Restano 45 siti di
  parsing duplicato e il limite dei 64 byte, che sono ragioni di **qualità**, non di
  sicurezza. Quindi: **adottala nel codice nuovo**, e migra un prefisso alla volta quando
  tocchi già quel file. Migrare 45 siti in blocco è costo cognitivo alto per un guadagno
  che `dp.errors` ha già in gran parte incassato.

### Fase 3 — Confine transazionale ai servizi (Causa A)

**Senza** il commit-in-uscita nella middleware (vedi Parte 1). Solo `3.2`, per moduli, con
i test della Fase 1 attivi: `economy.py` (23%, reimplementa a mano `_targeting.py` che
esiste già) → `betting.py` (36%, stessa `selectinload` copiata 4 volte) →
`admin_betting.py` (21%) → `quiz.py` (37%).

Più i tre interventi strutturali della v1, che restano validi e sono a costo cognitivo
nullo perché *togliono* concetti:

- ~~**`handlers/__init__.py`** (0 byte): sposta i 16 `include_router` lì~~ → **fatto.**
  `ROUTERS` + `register(dp)`, `main.py` non sa più nulla dell'ordine. Il test che conta
  non è quello sull'ordine ma quello che **cammina il package**: un modulo con un `router`
  che nessuno registra è morto in silenzio. Ha subito trovato un `Router` inutilizzato in
  `errors.py` (rimosso: `on_error` sta su `dp.errors`, non su un router).
- **`scheduler_loop` fuori da `handlers/`**: è un daemon dentro il package di
  presentazione. Il design è corretto (task persistiti, sopravvive ai restart, `TaskSkip`
  distinto dagli errori) — solo collocato male.
- **Spezzare `quiz.py`** per flusso, tastiere private in `keyboards/quiz_kb.py`.

### Fase 4 — Copy e test

Come la v1, con la sua raccomandazione confermata: **estendere il registry che esiste già**
(`help_content.CommandDoc`, `catalog_loader`), non `aiogram.utils.i18n`. Il bot è
monolingua: il toolchain `.po`/`.mo` è costo cognitivo puro per una feature che non serve.
Più `tests/fakes.py` per i 78+ `SimpleNamespace` a mano.

Aggiunta che la v1 non aveva: **`services/backup/loop.py` è a 0%**. Prima di rifinire il
copy, quel modulo merita un test.

### Fase 5 — Dati e scala

- ~~Indici ledger~~ → fatto.
- ~~**`fun_ai._last_used` → `utils/cooldown`**~~ → **fatto** (rung 2, non `cachetools`). ~15 righe
  in meno e una implementazione di throttle in meno. Due cose da sapere: `cooldown.guard()`
  **non** va bene qui, perché marca mentre controlla e togliere quella separazione farebbe
  costare 60s un `/insulta` malformato (proprietà ora pinnata da un test scritto *prima* del
  refactor); e le due prove sul pruning esistevano solo nei test di `fun_ai`, quindi sono
  migrate in `test_cooldown.py` insieme al comportamento invece di sparire.
- **Processo singolo**: resta l'assunzione di fondo — il bot non gira in 2 repliche senza
  cache incoerenti. Va scritto in STEERING, non scoperto in produzione.
- **Alembic**: quando servirà un downgrade o una migrazione di *dati*. **Confermato dai fatti**:
  l'allargamento a `BigInteger` di `xp` e `total_wagered` è passato con due righe in
  `_MIGRATIONS` più un test che le esercita su Postgres vero.
- **`xp_service.airdrop_xp`**: muta `user.xp` in Python su tutti gli utenti. Stessa forma dei
  difetti della Fase 1b ma **non misurata** — serve prima un test di gara.
- **`quiz_service.reset_quiz`**: read-check-write su `status` con `get_quiz(for_update=True)`,
  l'ultimo uso di quel pattern rimasto. Due reset sovrapposti però producono lo stesso esito
  (nessun denaro si muove), quindi vale meno di un test dedicato. Da rivedere insieme alla
  decisione aperta sulla semantica di `reset_quiz` rispetto ai premi già pagati.

---

## Decisioni aperte (invariate dalla v1, tutte da confermare da te)

- **Storage FSM `memory` + Watchtower che redeploya ad ogni push su `main`** = ogni deploy
  perde i flussi FSM aperti (un admin a metà creazione quiz riparte da zero). `redis_url`
  è già in config e aiogram ha `RedisStorage` pronto. Accettabile o si passa a Redis?
- **Watchtower**: `:latest` non pinnato, socket Docker in lettura/scrittura, può leggere
  `BOT_TOKEN`/`GROQ_API_KEY`/password Postgres/`TELEGRAM_SESSION` via `docker inspect`.
- **`bet_default_window_minutes`**: config morta, documentata come funzionante.
- **Semantica di `reset_quiz`** rispetto ai premi: incoerente.
- **Python 3.11 (CI) vs 3.12 (venv locale)**.
