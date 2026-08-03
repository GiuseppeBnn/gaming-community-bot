# Refactoring aiogram-dialog — design approvato

**Data:** 2026-08-02 · **Stato:** design approvato, piano di implementazione da scrivere.
**Origine:** [analisi-aiogram-dialog.md](../analisi-aiogram-dialog.md) (2026-08-01).

---

## 0. Per una sessione che riparte da zero

Questo documento è **autosufficiente**: contiene ogni fatto misurato, ogni decisione presa e il
comando per ri-verificarla. Se il contesto è stato compattato, o se questa è una sessione nuova,
si riparte da qui e dalla **tabella di stato (§10)** — non serve rileggere la conversazione.

Ordine di lettura: §1 (perché) → §10 (dove siamo) → la fase in corso.

Documenti correlati: [STEERING.md](../../../STEERING.md) è normativo e vince su tutto.
[CLAUDE.md](../../../CLAUDE.md) è il condensato operativo.

---

## 1. Decisione

L'analisi del 2026-08-01 ha misurato quanto codice del bot è impianto UI e ha concluso:
**non una migrazione, uno spike**. Questo design adotta quella conclusione.

Approccio scelto, fra quattro valutati:

| | Esito |
|---|---|
| Roadmap dell'analisi: spike su `guess/creation.py`, poi i candidati uno alla volta | **scelto** |
| Migrazione completa 1-5 pianificata subito, senza gate d'uscita | scartato: impegna ~3.700 righe di test prima di sapere se la libreria rende |
| Pulizia interna senza dipendenze nuove (panel condiviso, decoder callback) | scartato: non uccide alla radice la classe di bug «ordine dei messaggi» |
| Solo sbloccare i nodi aperti, refactoring dopo | scartato in questa forma: un nodo entra in Fase 0, l'altro resta fuori (§9) |

**Il criterio d'uscita dello spike è misurato ma la decisione è dell'utente.** Si producono
i numeri prima/dopo (§4.3); nessuna regola automatica di no-go. Questo è un rischio noto e
dichiarato: è il modo in cui uno spike può diventare una migrazione per inerzia. La difesa è
che le metriche siano scritte **prima** di iniziare, ed è ciò che fa §4.1.

---

## 2. Fatti verificati il 2026-08-02

Tutto in questa sezione è stato **misurato**, non stimato. Ogni riga ha il comando per rifarlo.

### 2.1 Il vincolo che l'analisi non aveva visto

```
aiogram_dialog 2.6.0 / 2.5.0 / 2.4.0 / 2.3.1 / 2.3.0  →  Requires-Dist: aiogram>=3.14.0
aiogram_dialog 2.1.0                                   →  Requires-Dist: aiogram>=3.0.0
```

Il progetto pinna **`aiogram==3.13.1`** e STEERING §1 lo dichiara vincolante. Quindi:

> **Adottare aiogram-dialog impone un upgrade di aiogram.** `pip install aiogram-dialog`
> risolve a **aiogram 3.30.0** — 17 minor di salto — più tre dipendenze transitive nuove:
> `jinja2`, `MarkupSafe`, `cachetools` (`<6.0.0,>=4.0.0`).

Scendere ad `aiogram_dialog==2.1.0` per restare su aiogram 3.13.1 **non è un'opzione**: è del
2024, e vorrebbe dire adottare una libreria a manutentore singolo partendo da una versione
vecchia di due anni.

Comando per ri-verificare:

```bash
pip download --no-deps aiogram-dialog==2.6.0 -d /tmp/ad
python -c "import zipfile,glob;z=zipfile.ZipFile(glob.glob('/tmp/ad/*.whl')[0]);\
print([l for l in z.read([n for n in z.namelist() if n.endswith('METADATA')][0]).decode().splitlines() if l.startswith('Requires-Dist')])"
```

### 2.2 Cosa muove l'upgrade di aiogram

| pacchetto | installato oggi | richiesto da aiogram 3.30.0 | si muove? |
|---|---|---|---|
| `aiogram` | 3.13.1 | — | **sì**, → 3.30.0 |
| `aiohttp` | 3.10.11 | `<3.15,>=3.9.0` | no |
| `pydantic` | 2.9.2 | `<2.14,>=2.4.1` | no |
| `magic-filter` | 1.0.12 | `<1.1,>=1.0.12` | no (esattamente al minimo) |
| `aiofiles` | 24.1.0 | `<26.0,>=23.2.1` | no |
| `certifi` | 2026.5.20 | `>=2023.7.22` | no |
| `typing-extensions` | — | `<=5.0,>=4.7.0` | no |
| `redis` | 5.2.0 | extra `redis`: `redis[hiredis]<8,>=6.2.0` | **da valutare** |

`Requires-Python: <3.15,>=3.10` — Python 3.12.13 va bene.

Il caso `redis` è l'unico ambiguo: non è un requisito duro (sta in un extra che non installiamo),
ma è la versione contro cui aiogram testa `RedisStorage`. Il progetto importa
`aiogram.fsm.storage.redis` direttamente in `main._build_storage`, quindi **la Fase 0.2 deve
verificare che `redis==5.2.0` regga con aiogram 3.30**, e in caso contrario alzarlo. È il motivo
per cui 0.1 (che rende Redis il default) va **prima** di 0.2: si arriva all'upgrade con il
percorso Redis già coperto da test.

### 2.3 I due nodi aperti al 2026-08-02

**Nodo A — il modello AI non è in produzione.** `main` ha ancora
`groq_model = "llama-3.3-70b-versatile"` (`src/config_data/config.py:37` su main); `qwen/qwen3.6-27b`
sta solo su `test`/`test_giu`. `latest` (l'immagine di produzione) segue `main`.
`llama-3.3-70b-versatile` **si spegne il 16 agosto 2026**.

> **Fuori dallo scope di questo documento, per decisione esplicita.** Lo gestisce l'utente.
> Resta qui come rischio: se il 16 agosto arriva prima che qwen sia su `main`, i comandi AI
> smettono di funzionare in produzione — e sarà una priorità che interrompe qualunque fase.

```bash
git show main:src/config_data/config.py | grep groq_model   # verifica
```

**Nodo B — la contraddizione sullo storage FSM.** Tre sorgenti in disaccordo:

| dove | dice |
|---|---|
| `STEERING.md:74` (§2) | «resta `memory`, ed è una scelta, non una svista» |
| `.env.example:13` | `FSM_STORAGE=redis` |
| `src/main.py:101-108` | intercetta solo `ImportError`, **non** un errore di connessione |

STEERING pone anche la condizione per riaprire il discorso: «Non riproporre il passaggio senza
prima aggiungere un fallback su errore di connessione». **Questo è il nodo che entra in Fase 0.1**,
perché con i dialoghi lo storage FSM smette di contenere solo lo stato: contiene la UI.

### 2.4 Dimensioni misurate

Handler candidati (`wc -l src/handlers/...`):

| file | righe |
|---|---|
| `handlers/guess/creation.py` | 898 |
| `handlers/admin_dashboard.py` | 675 |
| `handlers/quiz/creation.py` | 758 |
| `handlers/quiz/editing.py` | 371 |
| `handlers/shop.py` | 457 |
| `handlers/events.py` | 385 |
| `handlers/event_types/` (6 file) | 853 |
| **totale candidati** | **4.397** |

Test dei flussi candidati (`wc -l tests/integration/...`):

| file | righe |
|---|---|
| `test_guess_creation_flow.py` | 915 |
| `test_shop_handlers.py` | 679 |
| `test_events_hub.py` | 685 |
| `test_quiz_creation_flow.py` | 650 |
| `test_quiz_edit_flow.py` | 412 |
| `test_guess_alias_edit.py` | 209 |
| `test_admin_dashboard.py` | 129 |
| **totale** | **3.679** |

Suite intera: **28.993 righe**, **2.092 test raccolti** (2.062 passano, 30 `pg` skippano senza
`TEST_PG_URL`), gate coverage `fail_under = 99`.

### 2.5 Anatomia di `guess/creation.py` — il bersaglio dello spike

- **898 righe**, **16 handler registrati**: 11 `@router.callback_query`, 5 `@router.message`.
- **7 stati FSM** (`GuessCreationStates`, riga 76): `waiting_title`, `waiting_media`,
  `waiting_answer`, `editing`, `card`, `hints`, `waiting_hint_text`.
- **8 campi** nel registro `FIELDS` (riga 227): `title`, `answer`, `aliases`, `max_attempts`,
  `time_limit_seconds`, `round_duration_seconds`, `hints`, `prizes`.
  `media` **non** è in `FIELDS` apposta: il suo input è una foto o un audio, non testo.
- `_panel()` (riga 499) è un `MessageManager` scritto a mano: tiene `card_message_id` nello stato,
  prova `edit_message_text`, ricade su `answer()` se Telegram rifiuta.

### 2.6 Volume di log — dimensiona gli alert (§3.3)

```
warning     44
error       11
exception   10
critical     0
```

Comando: `grep -rn "log\(ger\)\?\.warning(" src/ | wc -l` (e così per gli altri livelli).

Sono percorsi di guasto, non traffico: «annuncio round fallito», «media non inviabile»,
«cartella backup non scrivibile», «comandi admin non registrati». È il motivo per cui la soglia
di default è `WARNING` e non `ERROR`.

### 2.7 Pipeline immagini (serve per il rollback)

`.github/workflows/docker-image.yml`: il nome del branch va in coda al tag quando non è `main`.

| branch | tag prodotti |
|---|---|
| `main` | `latest`, `sha-<short>` |
| `test` | `latest-test`, `sha-<short>-test` |
| `test_giu` | `latest-test-giu`, `sha-<short>-test-giu` |

I tag `sha-<short>` sono pinnati apposta: **sono la via di ritorno**.

---

## 3. Fase 0 — sbloccare il terreno

Nessun `aiogram_dialog` in questa fase. Tre commit indipendenti, ognuno con un valore proprio
anche se lo spike poi fallisce.

### 3.1 — Storage FSM: ping e fallback

**Problema:** §2.3 nodo B.

**Intervento:** `main._build_storage` prova una connessione vera a Redis all'avvio (un `ping`).
Se non risponde: `log.warning` e `MemoryStorage`. Sciolta questa condizione — che è
letteralmente quella posta da STEERING §2 — `redis` diventa il default coerente.

**Ricadute obbligatorie nello stesso commit:**
- `.env.example` smette di contraddire il documento normativo;
- **STEERING §2 riscritto**: la voce `fsm_storage` oggi dice «resta `memory`, ed è una scelta» e
  spiega perché; dopo questo commit la scelta è l'opposta e la ragione va aggiornata, non
  cancellata (il baratto rifiutato allora era «il bot non parte», e ora non è più il baratto).

**Perché prima di 0.2:** l'upgrade di aiogram tocca `RedisStorage` (§2.2). Ci si arriva con il
percorso Redis coperto da test, non al buio.

**Test:** unit su `_build_storage` — Redis irraggiungibile ⇒ il bot parte in memory e logga;
Redis raggiungibile ⇒ `RedisStorage`; `FSM_STORAGE=memory` ⇒ nessun tentativo di connessione.

**Attenzione:** `_build_storage` è sincrona oggi e un ping non lo è. Il piano di implementazione
deciderà dove va il ping — la scelta è fra rendere asincrona la funzione (è chiamata da `main()`,
che è già `async`) e fare il ping nello startup subito dopo. La prima è più pulita, la seconda
tocca meno codice; si sceglie leggendo il chiamante, non a tavolino.

### 3.2 — aiogram 3.13.1 → 3.30.0

**Commit separato, senza una riga di `aiogram_dialog`.** È la parte più rischiosa dell'intera
roadmap e deve poter essere revertita da sola.

**Passi:**
1. Leggere il CHANGELOG di aiogram fra 3.14.0 e 3.30.0, cercando i breaking change sulle
   superfici usate qui: `Dispatcher`/`Router`, middleware, `FSMContext`, `RedisStorage`,
   `ChatActionSender`, `InlineKeyboardBuilder`, `DefaultBotProperties`, `ErrorEvent`,
   i filtri `F`, `BotCommandScope*`.
2. `requirements.txt`: `aiogram==3.30.0`. Valutare `redis` (§2.2).
3. `pytest` intero.
4. `pytest -m pg` con `TEST_PG_URL` — **non saltabile**: l'upgrade tocca lo storage, e le gare
   sul denaro sono l'unica cosa che non si può verificare su SQLite.
5. `ruff check src/ tests/` e `mypy`: sono a **zero findings**, quindi ogni segnalazione nuova è
   una regressione di questo commit, non rumore preesistente.
6. `PYTHONPATH=src python -c "import main"`.
7. Immagine `latest-test` provata sul bot di test: avvio, un comando, un flusso FSM completo,
   un riavvio del container per vedere lo stato sopravvivere in Redis.

**Aggiornare STEERING §1** (stack vincolante) nello stesso commit.

**Se rompe:** si scende alla versione più bassa che soddisfa `aiogram-dialog` (**3.14.0**) e si
riprova. Se rompe anche lì, **la roadmap si ferma qui** e il costo perso è una fase — le 0.1 e
0.3 restano acquisite, perché hanno valore da sole.

### 3.3 — Alert admin su Telegram

**Perché sta qui:** la 0.1 introduce un degrado silenzioso (Redis giù ⇒ memory). Un degrado
silenzioso in produzione è un guasto che nessuno vede. Questa fase lo rende visibile — e vale
per ogni altro guasto già loggato, non solo per quello.

**Meccanismo: un `logging.Handler`, non chiamate esplicite.** Ogni `log.error`/`log.exception`/
`log.warning` già scritto — `handlers/errors.py`, `backup/loop.py`, lo scheduler, il fallback
Redis, la registrazione comandi — diventa un alert **senza toccare nessuno di quei file**.
Un `notify_admins()` da chiamare a mano copre meno e va ricordato ogni volta.

**File nuovo: `src/utils/alerts.py`.** Superficie:

| pezzo | contratto |
|---|---|
| `TelegramAlertHandler(logging.Handler)` | `emit()` fa **solo** `queue.put_nowait(record)`. Nessun I/O, nessun `await`: il logging resta sincrono e non blocca mai. Coda con `maxsize`: piena ⇒ scarta e conta |
| `alert_loop(bot)` | Terzo task in background accanto a scheduler e backup. `try/except` totale come `backup/loop.py`. Drena, formatta, invia |
| dedup | Impronta `(record.name, record.msg)` → un dizionario locale `impronta → (ultimo invio, soppressi)`, finestra 300 s. I soppressi si **contano** e il numero entra nel messaggio successivo |

> **Perché non `utils.cooldown`.** Sembra il riuso ovvio, e non lo è: la sua chiave è
> `(bucket: str, user_id: int)`, quindi un'impronta testuale ci entra solo passando per un hash
> — collisioni silenziose su un canale che esiste per segnalare guasti — e non sa contare i
> soppressi, che è metà del requisito. Servirebbero due strutture invece di una. Dieci righe
> locali che tengono timestamp e conteggio nello stesso valore sono meno codice **e** più
> oneste.

**Le tre difese non tagliabili:**

1. **Il sender non può loggare i propri errori.** Un errore di consegna che finisce nel logger
   rientra nella coda e il bot si autoalimenta alert all'infinito. Gli errori di consegna si
   contano in memoria. In più un `addFilter` che scarta i record del proprio modulo.
2. **La coda non blocca l'event loop** (§22 regola 24): `put_nowait` in `emit`, invio nel task.
3. **Il dedup non è opzionale**: un annuncio di gruppo che fallisce in loop è un `log.warning`
   ogni pochi secondi, cioè uno spam che rende gli alert inutili proprio quando servono.

**Destinazione: DM privato a ogni id in `settings.admin_ids`.** Valutati e scartati: un canale
Telegram dedicato con id in `.env` (storico in un posto solo, ma configurazione nuova), e la
combinazione dei due (due percorsi di consegna da mantenere).

**Limiti accettati, non difetti da correggere dopo:**
- N admin = N messaggi per alert;
- riceve solo chi ha già avviato il bot in privato — è lo **stesso limite noto** di
  `main.py:180`, dove i comandi admin si registrano «best-effort: needs them to have started
  the bot»;
- gli admin Telegram del gruppo (che `is_admin` riconosce) **non** ricevono alert: la sorgente
  è `settings.admin_ids`, non `filters.admin_filter`;
- nessuna persistenza, nessun ack, nessun rate globale oltre il dedup.

**Config:** `alert_min_level: str = "WARNING"` in `config_data/config.py`. Alzarlo a `CRITICAL`
è anche l'interruttore di spegnimento — nessuna variabile in più solo per quello.

**Formato:** livello, logger, messaggio, traceback troncato. **`parse_mode=None`** — un traceback
non è HTML, e questa è la stessa scelta già presa per i comandi AI (§17). Nessun `esc` da
ricordare, quindi nessun `esc` da dimenticare.

**Test:** la coda non blocca chi logga · il dedup sopprime il duplicato e ne riporta il conteggio ·
un errore di consegna non solleva e **non logga** · la soglia filtra sotto `alert_min_level` ·
il fallback Redis della 0.1 produce un alert vero.

**Aggiornare STEERING** con una sezione sugli alert (dove finiscono, cosa li scatena, i limiti).

### 3.4 — Uscita della Fase 0

- [ ] `pytest` verde, `pytest -m pg` verde con `TEST_PG_URL`, `ruff` e `mypy` a zero
- [ ] `PYTHONPATH=src python -c "import main"` non esplode
- [ ] `latest-test` provata: avvio, flusso FSM, riavvio container con stato che sopravvive
- [ ] Un alert vero arrivato in DM (si provoca staccando Redis)
- [ ] STEERING §1 e §2 aggiornati, più la sezione alert

---

## 4. Fase 1 — lo spike su `guess/creation.py`

Il caso #1 dell'analisi: il file più grosso, quello col media, e quello che ha appena prodotto
un bug che la libreria rende inesprimibile (la scheda modificata sul posto restava **sopra** i
due messaggi nuovi dell'upload, e l'admin vedeva una foto senza bottoni).

### 4.1 Baseline — si misura **prima** di toccare

Cinque numeri, scritti nel piano di implementazione prima della prima riga di codice:

| metrica | valore oggi | come si misura |
|---|---|---|
| righe handler | 898 | `wc -l src/handlers/guess/creation.py` |
| righe test | 915 | `wc -l tests/integration/test_guess_creation_flow.py` |
| query di un flusso completo | **0** prima della pubblicazione · **2** alla pubblicazione | listener `before_cursor_execute` di SQLAlchemy su un test che fa 3 domande + 2 edit + pubblicazione (`tests/integration/test_creation_query_cost.py`) |
| tempo della suite | ~40 s | `pytest` |
| coverage | ≥ 99 | `pytest --cov=src --cov-report=term-missing` |

Misurato il 2026-08-02, prima di installare `aiogram_dialog` (Task 1 della fase, com'era previsto).
Il flusso pre-pubblicazione costa **zero** query — tutto vive nello stato FSM, come atteso — e la
pubblicazione ne costa **due**: un `INSERT INTO guess_rounds` (`guess_service.create_round`, il
round nasce `draft`) seguito da un `UPDATE guess_rounds SET status=?` (l'armamento a `ready` in
`cb_publish`). Conta perché è il termine di paragone per il resto della fase: con una baseline a
**zero**, un `getter` di aiogram-dialog che interroga il DB per ridisegnare una finestra — il suo
meccanismo normale, non un caso patologico — sarebbe una **regressione** rispetto a oggi, non un
pareggio. Il confronto prima/dopo di §4.3 si misura contro questo zero, non contro un valore comodo.

### 4.2 Cosa si costruisce

- `setup_dialogs(dp)` nel bootstrap di `main.py`.
- Un dialogo con quattro finestre: le tre domande (`title`, `media`, `answer`), la **scheda** con
  `DynamicMedia`, i **suggerimenti**, la conferma d'uscita.
- **`FIELDS` sopravvive.** È già il registro dichiarativo giusto — etichetta, prompt, parser,
  renderer, un solo handler per tutti — e alimenta i widget invece di un handler. Chi l'ha
  scritto era già arrivato per conto suo a un pezzo di aiogram-dialog: quel lavoro non si ripaga.
- I 16 handler registrati diventano widget più `getter`.
- I dati stanno in `dialog_manager.dialog_data`, cioè nel contesto del dialogo, cioè nello
  storage FSM, cioè in Redis dopo la 0.1: si legge dal DB quando il dato cambia, non a ogni
  ridisegno.
- Test riscritti con `aiogram_dialog.test_tools.MockMessageManager`.

**Il media entra nella finestra.** È il punto dello spike: scheda e media diventano **un solo
messaggio**, e non c'è più nessun ordine di messaggi da azzeccare.

### 4.3 Uscita della Fase 1 — la tabella che decide

Si produce il prima/dopo sulle cinque metriche di §4.1, più:

- il media è dentro la finestra (sì/no);
- gli invarianti di §6 valgono ancora, uno per uno.

**Poi decide l'utente**, guardando i numeri. Nessuna regola automatica.

Se la risposta è no: si è perso **un** flusso, non il bot; si reverte la fase; la Fase 0 resta
acquisita; e la risposta a «conviene aiogram-dialog?» è **documentata invece che opinata**.

#### Cosa ha detto la fetta verticale (Task 2, commit `1d2f487` e `c6b3ec9`)

**Attenzione al nome:** questo non è il gate qui sopra. Nessuna schermata del prodotto è stata
convertita, quindi il prima/dopo di §4.1 non esiste ancora. Quello che segue è il gate più stretto
ed economico che precede quella spesa: la libreria è compatibile con questo stack, sì o no?
Risponde ai sette punti del brief del Task 3, con fatti — dove un fatto è stato riprodotto in
questo task (non solo letto nei report del Task 2), lo dico esplicitamente.

Misure raccolte in questo task (comandi del brief, Step 1):

| cosa | valore |
|---|---|
| peso su disco di `aiogram_dialog` | 1,4 MB (`du -sh .venv/lib/python3.12/site-packages/aiogram_dialog`) |
| `aiogram_dialog` | 2.6.0 |
| `jinja2` | 3.1.6 |
| `MarkupSafe` | 3.0.3 |
| `cachetools` | 5.5.2 |

Riconfermato, non ri-misurato per sostanza (era già noto): suite **2103 passed, 30 skipped**,
coverage **99,66%**, `ruff` e `mypy` puliti — invariati rispetto a quanto già chiuso nel Task 2.

**1. La libreria convive coi quattro middleware?** La versione precedente di questo paragrafo
diceva che `setup_dialogs(dp)` «si è solo aggiunto in coda» dopo i quattro middleware di progetto,
come se fosse inerte finché nessuno apre un dialogo. **Non è così, ed è verificabile nel sorgente
installato, non solo nel diff:**

- `aiogram_dialog/setup.py:139-159` (`_register_middleware`) registra middleware **outer** su
  `router.message`, `.business_message`, `.callback_query`, `.my_chat_member`,
  `.chat_join_request` (`IntentMiddlewareFactory.process_*` più `context_unlocker_middleware`).
- Le righe 131-137 e 161-166 dello stesso file registrano middleware **inner**
  (`ManagerMiddleware`, `context_saver_middleware`) sugli stessi observer. Le righe 125-129 e 137
  ne mettono altri due — `IntentErrorMiddleware` e `ManagerMiddleware` — su `router.errors`: da
  `src/main.py:223`, `handlers.errors.on_error` gira dentro `IntentErrorMiddleware`.
- La chiamata in `main.py:218` è `setup_dialogs(dp)`: `router`, lì dentro, **è** `dp`. Quel
  middleware finisce sugli observer del dispatcher, non su un sotto-router qualunque.
- `aiogram/dispatcher/event/telegram.py:49-56` (`_resolve_middlewares`) cammina
  `self.router.chain_head` a ogni singolo `trigger()` — e ogni router lo riceve, perché
  `aiogram/dispatcher/router.py:170-199` (`_propagate_event`) ridiscende in `self.sub_routers`
  finché qualcuno risponde. Il `chain_head` di un sotto-router qualsiasi include sempre `dp` alla
  radice. Quindi il middleware inner registrato sugli observer di `dp` avvolge **ogni handler di
  ogni router**, `common.router` compreso — non solo quelli sotto un `Dialog`.

Effetto per update, letto nel codice che lo esegue: `aiogram_dialog/context/intent_middleware.py`
(`process_message`, l'outer sui messaggi) chiama `split_reply_callback(event.text)`
**incondizionatamente** (riga 307, `aiogram_dialog/utils.py:76-83`) e poi, salvo il caso di reply
a un bottone del bot, `self._load_default_context(...)` (riga 340) **su ogni messaggio**, dialogo
aperto o no — se il testo finisce coi caratteri marcatori invisibili della libreria, viene
decodificato e ri-dispacciato come `callback_query` sintetico (righe 307-330) senza mai raggiungere
gli handler dei messaggi. `_load_default_context` finisce in
`aiogram_dialog/context/storage.py:71-80` (`StorageProxy.load_stack`), che fa
`await self.storage.get_data(key)` — una lettura vera sullo stesso storage FSM del bot
(`data["fsm_storage"]`, popolato da aiogram stesso in `aiogram/fsm/middleware.py:37` con l'oggetto
passato a `Dispatcher(storage=...)`). Un `save_stack`/`save_context` simmetrico gira dopo
l'handler, come middleware **inner** (`context_saver_middleware`, `intent_middleware.py:437-443`
→ `storage.py:82-128`, `set_data`): almeno una scrittura in più per ogni update gestito. aiogram fa
già un `get_state()` per update — questo li fa grosso modo raddoppiare, non li introduce da zero.

**Scoperta di questo task, non richiesta da nessun finding della review, ma diretta conseguenza
del verificare questo stesso punto:** `main._build_storage` (`src/main.py:126`) costruisce
`RedisStorage.from_url(settings.redis_url)` **senza** un `key_builder`, quindi con
`DefaultKeyBuilder(with_destiny=False)` (`aiogram/fsm/storage/redis.py:47-48`). `StorageProxy`
costruisce le sue chiavi con `destiny="aiogd:stack:…"` / `"aiogd:context:…"`
(`context/storage.py:130-160`) — non il default. Riprodotto senza bisogno di un Redis vero (la
costruzione della chiave è pura Python, prima di ogni I/O):

```bash
python -c "
from aiogram.fsm.storage.base import StorageKey, DefaultKeyBuilder
key = StorageKey(bot_id=1, chat_id=1, user_id=1, destiny='aiogd:stack:default')
DefaultKeyBuilder().build(key, 'data')
"
# ValueError: Default key builder is not configured to use key destiny other
# than the default. Probably, you should set with_destiny=True ...
```

`MemoryStorage` non passa da un `KeyBuilder` (indicizza per `StorageKey` come dataclass), quindi
né questa suite né la verifica a mano di §11.2 — che precede `dialog_spike.py` — potevano vederlo.
**Con Redis raggiungibile, che dalla Fase 0.1 è il default, il primo messaggio che arriva al bot
dopo questo `setup_dialogs(dp)` solleva `ValueError` nel middleware outer, prima di qualunque
handler** — non solo dentro un dialogo aperto, perché quella lettura gira su *ogni* messaggio
(paragrafo sopra). Nessun codice toccato per questo in questo task — non è fra i findings assegnati
e la fetta è temporanea — ma è un fatto verificato, non un'ipotesi, e chi decide deve saperlo prima
di chiunque riapra questo file per la Fase 1 vera. La correzione più diretta, se e quando servirà,
è passare `key_builder=DefaultKeyBuilder(with_destiny=True)` a `RedisStorage.from_url` in
`_build_storage` — non fatto qui.

> **CHIUSO il 2026-08-03 col commit `6697d91`, poi rientrato con la fetta.** Il gate ha detto stop
> (§10): senza `setup_dialogs` nessun codice qui scrive un destiny non-default, quindi il flag
> sarebbe solo il costo delle chiavi orfane senza il beneficio, ed è stato tolto insieme al resto.
> La trappola resta documentata in `STEERING.md` §2, perché vale per **qualunque** libreria che usi
> `StorageKey.destiny`, non solo per aiogram_dialog. Quel che segue descrive il fix mentre c'era.
>
> (fuori dal Task 2, perché il rischio sopravviveva a
> qualunque esito del gate: se lo spike va avanti serve corretto, se si ferma resta `setup_dialogs`
> da revertire e nel frattempo il branch non si avvia con Redis). `_build_storage` ora passa
> `key_builder=DefaultKeyBuilder(with_destiny=True)`; la guardia è
> `tests/unit/test_fsm_storage.py::test_key_builder_accepts_the_destiny_aiogram_dialog_uses`, che
> asserisce sul **comportamento** — costruisce la chiave col destiny vero — quindi `with_destiny=False`
> non la passerebbe (verificato per mutazione). **Costo dichiarato, non gratuito:** il flag aggiunge
> `:default` anche alle chiavi normali (`fsm:2:3` diventa `fsm:2:3:default`), quindi le chiavi FSM
> scritte da un build precedente restano orfane e i flussi aperti muoiono una volta, al deploy.
> Stesso commit: `STEERING.md:74` dichiarava `redis` come default del **campo**, ma `config.py:37`
> dice `memory` e il README pure — a spedire `redis` è `.env.example`. Contraddizione della Fase 0,
> corretta nel documento normativo.

Sui quattro middleware di progetto in sé, questo task ha ristretto il buco invece di limitarsi a
descriverlo: `tests/integration/test_dialog_spike.py` ora fa girare `RateLimitMiddleware`,
`BannedUserMiddleware`, `GroupMemberMiddleware` **veri**, nello stesso ordine di `main.py`, insieme
a `setup_dialogs` su un `Dispatcher` reale — verde su entrambi i test del file. Resta finto solo
`DbSessionMiddleware`, per una ragione verificata: apre `async_session_maker()`
(`src/database/connection.py:13-18`), un **singleton di modulo** costruito all'import-time su
`settings.db_url`, senza nessuna redirezione verso l'infrastruttura di test in tutta la suite (il
pattern qui è sempre l'opposto: `session`/`engine` per-test, iniettati a mano). Usarlo davvero
avrebbe puntato a un secondo database, isolato da quello che `user_factory` popola, per un singolo
test. **Il buco residuo sui quattro middleware è quindi uno solo, preciso** — la combinazione con
la sessione DB reale del bootstrap — non più "quattro su quattro non provati".

**2. `db_session` arriva nel getter senza toccare la DI?** Sì, e qui il verificato è solido.
`aiogram_dialog/window.py:118` chiama `data.update(await self.getter(**manager.middleware_data))`:
spacchetta come kwargs lo stesso dict che i middleware aiogram riempiono, nessun canale di
iniezione separato per i dialoghi. L'asserzione del test dipende da una query vera
(`"Utenti a DB: 2" in message_manager.last_message().text`, con due utenti creati nel test), non da
una costante: con un solo utente — così era scritto durante il Task 2 — un getter che ritornasse
`1` fisso sarebbe passato identico; con due, no. Corretto in questo task proprio perché la frase
qui sopra fosse vera, non solo scritta.

> **Quello che due utenti *non* chiudono, ed è bene dirlo.** Un getter che contasse la tabella
> sbagliata passerebbe ancora: `user_factory` inserisce sempre un `User` **e** un `Wallet`, uno
> a uno, quindi `SELECT count(*) FROM wallets` dà 2 esattamente come `users`. Per discriminare
> anche quel caso servirebbe seminare le due tabelle in modo asimmetrico. Non è stato fatto, e
> la tesi sulla DI non ne dipende: se `db_session` non arrivasse sarebbe un `TypeError`
> sull'argomento mancante del getter, non un'asserzione sbagliata — è quello il vero testimone.

**3. `IsAdminFilter` alla radice protegge anche il dialogo?** Sì, verificato sia leggendo il
codice sia rompendolo apposta. `aiogram/dispatcher/router.py::Router._propagate_event` esegue
`check_root_filters` **prima** sia degli handler del router sia della discesa nei `sub_routers`:
un dialogo innestato non viene mai raggiunto se il filtro di radice respinge. Riprodotto (nel
Task 2) togliendo il filtro e guardando il test passare da verde a rosso, due volte — una per
`router.message.filter(IsAdminFilter())` (comando `/spike`), una per
`router.callback_query.filter(IsAdminCallbackFilter())` (bottone "Chiudi", registrato
sull'observer del `Dialog` innestato, non su quello del router scritto a mano: è il caso che prova
che la protezione non dipende dal comando, ma dal router genitore). Il secondo filtro non era nel
codice del brief del Task 2 — aggiunto per STEERING §8 — e in un primo momento non aveva un test
proprio: corretto in un round di fix, ora `tests/unit/test_admin_routers_gated.py` lo
parametrizza insieme agli altri sei router 100% admin del progetto.

**4. `test_router_order.py` ha retto, o è stato corretto?** Ha retto **senza una riga toccata** —
riconfermato qui: `pytest tests/unit/test_router_order.py` passa sull'albero attuale (5 passed).
Non è fortuna: la scoperta cerca `getattr(module, "router", None)` poi `isinstance(router,
Router)`; `dialog_spike.router` è un `aiogram.Router()` semplice, e il `Dialog` (`spike_dialog`)
vive sotto un altro nome di modulo, agganciato solo via `router.include_router(spike_dialog)`. Il
caso peggiore è stato escluso leggendo il codice, non congetturato: `Dialog.__mro__` mostra che
eredita da `Router`, quindi anche un ipotetico modulo futuro che chiamasse `router` il `Dialog`
stesso supererebbe comunque l'`isinstance` — il rischio scritto nel piano di Fase 1 non aveva, con
l'API pubblica attuale, un modo concreto di materializzarsi.

**5. Quante righe costa una schermata banale?** 69 (`src/handlers/dialog_spike.py`) + 119
(`tests/integration/test_dialog_spike.py`) = **188 righe**, per **una** finestra, un comando, un
getter con una query, un bottone. Il file di test è cresciuto da 101 a 119 righe **in questo task
di fix**, non nel Task 2 originale: tre middleware veri in più nella fixture (punto 1) e
un'asserzione a due utenti invece di uno (punto 2) — difesa dei test, non costo della libreria.
Zero stati multipli, zero campi da modificare, zero media. Non va moltiplicato linearmente per
stimare `guess/creation.py` (898 righe, 16 handler, 7 stati FSM, 8 campi, media): è la prova che il
meccanismo compila e gira, non un preventivo. Quel preventivo, se si arriva a scriverlo, è compito
del piano della conversione vera.

**6. `BotClient` + `MockMessageManager` sono più o meno verbosi degli stub attuali?** Il confronto
grezzo (119 righe/2 test contro 915 righe) non è onesto da solo — sono scale diverse. Misurato con
l'AST invece che a occhio, contando solo le righe dentro i corpi delle funzioni `test_*`
(`ast.walk`, somma `end_lineno - lineno + 1` per ogni `FunctionDef`/`AsyncFunctionDef` il cui nome
inizia per `test_`; il resto del file è "impianto") — ricalcolato su questo task, i numeri di
`test_guess_creation_flow.py` sono invariati perché quel file non è stato toccato:

| paradigma | file | funzioni test | righe di test | righe/test | righe di impianto |
|---|---|---|---|---|---|
| nuovo (`BotClient`) | `test_dialog_spike.py` | 2 | 36 | 18,0 | 83 |
| esistente (stub) | `test_guess_creation_flow.py` | 60 (87 casi con `parametrize`) | 537 | 8,9 | 378 |

Sul corpo dei singoli test, il nuovo paradigma non è chiaramente più verboso — 18,0 righe/test
contro 8,9 — ma il campione è **due test**: troppo poco per pesare, e lo dico esplicitamente invece
di lasciarlo implicito. Dove il costo si vede è nell'impianto: 83 righe per 2 test (41,5/test)
contro 378 righe per 60 (6,3/test). Una fetta grossa delle 83 (~25 righe) è il commento e il reset
del router-singleton (`dialog_spike.router._parent_router = None`) — un costo che nasce
dall'**essere il primo test del repo a costruire un `Dispatcher` vero**, non un costo di
`aiogram_dialog` in sé. Altre ~13 sono i tre middleware veri aggiunti da questa review per chiudere
il buco del punto 1 (import, docstring, le tre righe di registrazione) — un costo di test più
difeso, non un costo della libreria neanche questo. Nessuna delle due voci si accorcerebbe
scrivendo meno, solo centralizzandole in una fixture condivisa — che oggi non esiste perché non
esiste un secondo test dello stesso tipo che la giustifichi.

Quello che i numeri non catturano, ed è il punto che l'utente ha chiesto esplicitamente di non
nascondere: gli stub attuali (`_StubBot`, `_Photo`, `_Msg`, `_Cb`, 132 righe scritte e mantenute a
mano in `test_guess_creation_flow.py`) guidano gli handler **come funzioni**, senza `Dispatcher`,
senza middleware, senza routing — il modello che condivide ogni test del repo tranne questi due.
`BotClient`/`MockMessageManager` (437 righe totali in `aiogram_dialog/test_tools/`, ma di
libreria: non scritte né mantenute da questo progetto) instradano un update **vero** attraverso un
`Dispatcher` **vero** — più fedele, come dice il docstring del test — ma anche un meccanismo
diverso da imparare, con vincoli propri (il parent singolo di `Router` è uno) che finora nessun
altro test del repo doveva conoscere. E per lo scope dichiarato in §7, questo non è un costo
transitorio che finisce con l'ultima fase: `quiz/play.py`, `guess/play.py`, `betting.py` restano
fuori **per sempre**. Quindi anche nello scenario migliore — Fasi 2-5 tutte completate — la suite
finirebbe con **due paradigmi di test di integrazione permanenti**, non uno che sostituisce
l'altro. Il costo non è "riscrivere 915 righe una volta": è mantenere due modi di scrivere test di
integrazione in parallelo, a tempo indeterminato.

**7. I numeri del Task 1.** Flusso di creazione **prima** della pubblicazione: **0 statement
SQL**. **Pubblicazione**: **2 statement** (`INSERT INTO guess_rounds … RETURNING`, poi `UPDATE
guess_rounds SET status`). Riportati qui per completezza — non ri-misurati in questo task, come
da istruzione, perché installare `aiogram_dialog` ha reso quella misura non più vergine. Nessun
"dopo" da affiancare: nessuna schermata del prodotto è stata convertita, quindi questa riga della
tabella di §4.1 resta con un solo lato compilato. È esattamente il confronto che la Fase 1 vera
deve ancora produrre, non qualcosa che questo task potesse produrre.

#### Limiti di questa prova, dichiarati

- **Sui quattro middleware di progetto, il buco si è ristretto a uno solo** (punto 1):
  `RateLimitMiddleware`, `BannedUserMiddleware`, `GroupMemberMiddleware` veri ora girano insieme a
  `setup_dialogs`, nell'ordine di `main.py`, in `tests/integration/test_dialog_spike.py` — verde
  su entrambi i test del file. Resta finto solo `DbSessionMiddleware`, perché la sua sessione vera
  passa da `async_session_maker` (`src/database/connection.py:13-18`), un singleton di modulo
  senza redirezione di test in tutta la suite. **Più grave, e non richiesto da nessun finding di
  questa review**: con `RedisStorage` costruito come lo costruisce oggi `main._build_storage`
  (senza `key_builder`), il primo messaggio che arriva al bot dopo `setup_dialogs(dp)` solleva
  `ValueError` prima di qualunque handler — verificato nel sorgente, non congetturato (punto 1).
  Non corretto in questo task: fuori dai findings assegnati e dallo scope della fetta. **Corretto
  poi, il 2026-08-03, nel commit `6697d91`** — vedi il riquadro al punto 1.
- **Un `Dialog` innestato oscura permanentemente i fallback di `common.router` per chi ci resta
  dentro, e `state.clear()` non lo libera.** Verificato in `aiogram_dialog/dialog.py:187-190`
  (`_register_handlers`): l'unico handler `message` di un `Dialog` si registra **senza filtri
  propri** — il solo cancello è il filtro di radice `IntentFilter` (`dialog.py:180-185`,
  `context/intent_filter.py:13-21`), che ritorna `True` appena esiste un `Context` caricato per lo
  stesso gruppo di stati, qualunque sia il testo del messaggio. Finché lo stack non è vuoto, quel
  singolo handler "gestisce" ogni messaggio dell'utente — anche uno che il dialogo non riconosce
  (`_need_refresh`, `dialog.py:160-178`, in chat privata ridisegna comunque la finestra) — e la
  propagazione (`_propagate_event` che ridiscende in `sub_routers`,
  `aiogram/dispatcher/router.py:170-199`) non arriva mai a `common.router`, che nella lista di §7
  sta **dopo** `dialog_spike.router`: un admin che manda `/spike` e non preme mai «Chiudi» perde
  `/profilo`, `/comandi`, `/start`, `/spiega_comando`, in silenzio. Uno stato FSM aperto ha già
  questa proprietà oggi, quindi non è un rischio nuovo *in genere* — ma `state.clear()`, l'uscita
  di sicurezza del progetto (**16** volte nel solo `admin_dashboard.py`), pulisce solo la chiave
  FSM di default (`aiogram/fsm/context.py:42-44`, `destiny=DEFAULT_DESTINY` in
  `aiogram/fsm/storage/base.py:11`); lo stack e il contesto di aiogram-dialog vivono sotto
  `destiny=f"aiogd:stack:…"` / `f"aiogd:context:…"` (`context/storage.py:130-160`), una chiave
  diversa che `state.clear()` non tocca. Niente in questo repo, oggi, può liberare un utente
  incastrato in un dialogo. **Materiale per le Fasi 2-5**: `admin_dashboard.py` convertito a
  dialogo (Fase 2, §5) oscurerebbe `common.router` allo stesso modo, ma **permanentemente**, per
  chiunque ci resti con uno stack aperto — non più "temporaneo, sparisce con la fetta".
- **Un'anomalia di coverage**: `dialog_spike.py` (25 statement) risulta con 1 riga "missing" — il
  `return` del getter — **solo** quando raggiunta via `aiogram_dialog`, non quando chiamata
  direttamente. Riprodotta due volte nel Task 2 (chiamata diretta del getter, e rimozione
  temporanea del file dal path per vedere lo spostamento della riga "missing"): la riga si esegue
  davvero (il test che dipende dal suo valore passa), è l'attribuzione del tracer a sbagliare.
  Ipotesi più probabile — non dimostrata — il ponte greenlet di SQLAlchemy async attraversato
  senza `concurrency = ["greenlet"]` in `pyproject.toml`. Non blocca il gate (99,66% ≫ 99%), ma
  resta un comportamento di coverage non capito fino in fondo.
- **188 righe per una finestra sola non stimano il costo di `guess/creation.py`.** (69 di
  dialogo + 119 di test, dopo l'ondata di fix della review finale.) Nessun numero di questo
  task lo fa: è precisamente ciò che il brief del Task 3 dichiara non ancora misurabile.
- **Il confronto sulle query "prima/dopo" della Fase 1 vera non esiste** (punto 7), per lo stesso
  motivo: nessuna schermata del prodotto è stata convertita.

#### Le due strade, come le presenta il brief

Il criterio resta quello di §1 e di questo stesso §4.3: **si misura, decide l'utente**. Nessuna
regola automatica di no-go, qui come altrove in questo documento.

- **Avanti**: si scrive il piano della conversione vera di `guess/creation.py` — è lì che il costo
  vero, la riscrittura delle 915 righe di test in un secondo paradigma che resterà permanente
  (punto 6), si paga davvero, e dove nasce il confronto prima/dopo che il punto 7 lascia ancora
  aperto. **Deve aprire chiudendo il buco sui middleware** (punto 1): far girare `setup_dialogs`
  con `DbSessionMiddleware` vero, non finto, e decidere il `key_builder` di `RedisStorage` — prima
  di scrivere qualunque finestra del prodotto, perché è la scoperta più importante di questa fase,
  non un dettaglio da rimandare a dopo.
- **Stop**: si reverte il Task 2 — `src/handlers/dialog_spike.py`,
  `tests/integration/test_dialog_spike.py`, `setup_dialogs` in `main.py`, la riga
  `aiogram-dialog==2.6.0` in `requirements.txt`, la voce `dialog_spike` in
  `src/handlers/__init__.py` (import e tupla `ROUTERS`), e la voce `dialog_spike` in
  `tests/unit/test_admin_routers_gated.py` (import e lista `ADMIN_ROUTERS`). Il Task 1 **resta** —
  ha valore da solo, indipendente dalla libreria. La Fase 0 resta acquisita in ogni caso (§11). La
  risposta a «conviene aiogram-dialog?» è comunque **documentata invece che opinata**, che è il
  risultato che questo documento si era proposto fin da §1.

---

## 5. Fasi 2-5 — solo se la Fase 1 passa, una alla volta

| # | area | righe | perché |
|---|---|---|---|
| 2 | `admin_dashboard.py` | 675 | 18 callback di **sola navigazione ad albero**: il caso da manuale |
| 3 | `quiz/creation.py` + `quiz/editing.py` | 1.129 | 26 callback, navigazione fra domande, `ScrollingGroup` al posto della paginazione a mano |
| 4 | `shop.py` | 457 | 13 callback: sfoglia catalogo → conferma acquisto |
| 5 | hub eventi (`events.py`) + `event_types/` | 385 + 853 | **ultimo** |

**Perché il 5 va per ultimo, sempre.** È l'unico che chiede di cambiare un contratto: oggi il
protocollo `EventType` espone `render_detail(item)` che ritorna **testo impaginato più tastiera
costruita**; perché l'hub diventi un solo dialogo con finestre parametriche, quella firma deve
ritornare **i dati** (titolo, stato, contatori, azioni disponibili). Il registro resta — è la
scelta giusta e non si tocca (§18.2, regola 25) — sparisce la sua superficie di presentazione.
Si affronta quando il resto ha già dimostrato che la libreria regge.

Ogni fase, senza eccezioni: baseline misurata prima → branch → `latest-test` → provata sul bot
di test → `main`. STEERING aggiornato nello stesso commit della fase.

---

## 6. Invarianti che non si negoziano

Restano validi: cambia **chi** li applica, non **se** valgono. Ogni fase li ri-verifica.

Dal panel (§19.b):
- **un solo pannello vivo** sullo schermo, sempre;
- **il media si posta una volta sola**, quando viene scelto o sostituito (sei upload in una chat
  sono un burst che Telegram rate-limita: è ciò che «impallava il bot»);
- l'eco del media in creazione **è** la verifica che il `file_id` sia ri-inviabile;
- un messaggio che non comanda più niente **non resta sullo schermo coi bottoni vivi**;
- la pulizia dei messaggi morti è **best-effort e non può fallire rumorosamente**.

Dal resto di STEERING:
- **`esc`** su ogni stringa user-controlled in un messaggio HTML (regola 20); bottoni inline e
  opzioni dei poll non sono HTML-parsed, quindi niente `esc` lì;
- **`is_admin` alla radice del router** per i router 100% admin (regola 15, §8) — gli handler
  guidati dal solo stato non ri-controllano nulla, e lo stato non ha TTL;
- **i service non committano** (§5);
- **denaro, XP e transizioni di stato si decidono in SQL** (regola 22): check nella `WHERE`,
  aritmetica nella `SET`, `rowcount == 0` = gara persa, `synchronize_session=False` + `refresh`;
- `from __future__ import annotations` ovunque;
- **coverage ≥ 99**, `ruff` e `mypy` a zero findings;
- l'ordine dei router (`handlers/__init__.py`, asserito da `tests/unit/test_router_order.py`) e
  l'ordine dei middleware restano quelli.

Nota su `test_router_order.py`: scopre i moduli registrabili cercando l'attributo `router`. Un
dialogo aiogram-dialog non è un `Router` nello stesso senso — **come conviva con quel test è un
task esplicito della Fase 1**, non un dettaglio da scoprire a fine fase.

---

## 7. Fuori scope — dichiarato, non dimenticato

Non si tocca, in nessuna fase:

- **`services/`** — SQL, denaro, XP, transizioni. È dove sta il valore;
- **i giochi in gruppo**: `quiz/play.py`, `guess/play.py`, `betting.py`. Un dialogo possiede
  **un** messaggio in **una** chat con **un** utente; questi flussi sono guidati dagli eventi e
  vivono in gruppo. Piegarli a dialoghi vuol dire cambiare il gioco per far contenta la
  libreria, e il gioco è il prodotto;
- `fun_ai.py`, scheduler, backup, moderazione, deep-link, onboarding, `/help`;
- `handlers/errors.py` su `dp.errors`;
- il rilascio di qwen (§2.3 nodo A): lo gestisce l'utente.

Regola pratica: **aiogram-dialog paga dove un admin naviga, non dove un utente gioca.**

---

## 8. Rilascio e rollback

Il pipeline che c'è già (§2.7), nessun meccanismo nuovo:

1. la fase vive su un branch;
2. la CI pubblica `latest-<branch>` e `sha-<short>-<branch>`;
3. si prova sul bot di test;
4. arriva su `main`.

**Rollback = ripuntare l'immagine al tag `sha-<short>` precedente.** Niente feature flag runtime
(due implementazioni vive, doppia superficie di test, e un flag che qualcuno deve ricordarsi di
togliere) e niente codice vecchio lasciato in repo (coverage al 99% su codice morto è un
problema in più).

**Nota sui branch:** `main` è stato squash-merged in passato mentre `test` ha tenuto la storia
completa. Per riportare lavoro su `main` si usa **cherry-pick, non merge**.

---

## 9. Rischi noti

| rischio | mitigazione |
|---|---|
| L'upgrade di aiogram (3.13.1 → 3.30.0) rompe qualcosa di non ovvio | Fase isolata e revertibile da sola, suite intera come collaudo, `pytest -m pg` obbligatorio, prova su `latest-test` |
| `redis==5.2.0` non regge con aiogram 3.30 | 0.1 prima di 0.2: si arriva all'upgrade col percorso Redis già testato |
| Lo spike diventa una migrazione per inerzia (nessun no-go automatico) | Le metriche sono scritte **prima** di iniziare (§4.1) |
| Riscrivere ~3.700 righe di test è il costo vero della migrazione | Se ne paga **una fetta sola** (915 righe) prima di decidere |
| `aiogram_dialog` è di fatto a manutentore singolo | Ci si lega alla sua cadenza per la compatibilità con aiogram. Accettato consapevolmente; è anche il motivo per cui il gate dopo lo spike esiste |
| Due paradigmi in casa per mesi | Inevitabile in una migrazione parziale, che è l'unica sensata. Va messo in conto, non nascosto |
| Gli alert diventano spam e si smette di leggerli | Dedup a impronta obbligatorio, `alert_min_level` alzabile |
| Il 16 agosto arriva prima che qwen sia su `main` | Fuori scope, ma interrompe qualunque fase in corso |

---

## 10. Tabella di stato — **aggiornare qui**

Una sessione nuova legge questa tabella per sapere dove siamo.

| fase | descrizione | stato |
|---|---|---|
| 0.1 | Storage FSM: ping + fallback, `.env.example`, STEERING §2 | ☑ fatta |
| 0.2 | aiogram 3.13.1 → 3.30.0, isolato | ☑ fatta |
| 0.3 | Alert admin (`utils/alerts.py`) | ☑ fatta |
| — | **Gate Fase 0** (§3.4) | ☑ **superato** il 2026-08-02 — gate locali + le due verifiche a mano (§11.2, §11.3) |
| — | **Fetta verticale** (Task 1-3: baseline query + `dialog_spike.py` + verdetto, §4.3) | ☑ fatta — compatibile a livello di meccanismo (DI, filtro admin, ordine router verificati); combinazione con i 4 middleware reali mai girata; il costo del secondo paradigma di test è la voce aperta. Decide l'utente |
| 1 | Spike `guess/creation.py` con aiogram-dialog | ✗ abbandonata |
| — | **Gate spike** (§4.3) — decide l'utente | ☑ **deciso il 2026-08-03: stop** |
| 2 | `admin_dashboard.py` | ✗ abbandonata col gate |
| 3 | `quiz/creation.py` + `quiz/editing.py` | ✗ abbandonata col gate |
| 4 | `shop.py` | ✗ abbandonata col gate |
| 5 | hub eventi + `event_types/` (cambia `render_detail`) | ✗ abbandonata col gate |

Legenda: ☐ da fare · ▣ in corso · ☑ fatta · ✗ abbandonata (con il perché, in una riga sotto).

**Perché stop** (decisione dell'utente al gate, motivata così: «ampliare il tutto è mettere pezza su
pezza»): la fetta ha dimostrato che aiogram-dialog *funziona* in questo stack, e nello stesso
movimento che il conto è più alto di quanto l'analisi di partenza stimasse — due paradigmi di test
di integrazione in parallelo **a tempo indeterminato**, `setup_dialogs` che aggiunge middleware sul
**100%** del traffico (dialogo aperto o no), un `Dialog` che oscura i fallback di `common` senza che
`state.clear()` possa liberare chi ci resta dentro, e una baseline di **0 query** nel flusso di
creazione che qualunque getter può solo peggiorare. Adottarlo avrebbe riscritto la superficie
lasciando intatto ciò che rende il codice difficile da mantenere.

**Cosa resta, e cosa è stato rimosso.** Resta tutta la Fase 0 — alert admin, ping Redis con degrado,
aiogram 3.30.0 — più `tests/integration/test_creation_query_cost.py`, che pinna il costo in query
della creazione indipendentemente da chi disegni la UI. Rimossi nel commit di pulizia:
`src/handlers/dialog_spike.py`, `tests/integration/test_dialog_spike.py`, `setup_dialogs(dp)`, la
riga in `ROUTERS`, la dipendenza `aiogram-dialog==2.6.0`, e il `key_builder` con `with_destiny=True`
— che senza dialoghi è solo il costo delle chiavi orfane, senza il beneficio. La conoscenza
acquisita resta **qui**: è il prodotto dello spike, il codice no.

---

## 11. Chiusura della Fase 0 — stato reale al 2026-08-02

Scritto qui e non in un file di lavoro perché i file di lavoro spariscono. Chi riprende in una
sessione nuova trova in questa sezione **tutto** ciò che è rimasto aperto.

### 11.1 Cosa è stato fatto, e dove

Dieci commit su `test_giu`, da `38182d9` a `0886fb9`: questo documento e il piano, poi i cinque
task, poi un'unica ondata di fix dalla review finale.

Gate locali, misurati sull'albero finale: **2096 passed, 30 skipped** (baseline di partenza
2067) · **31 test `pg` verdi** contro un PostgreSQL 16 vero · coverage **99.67%** con
`fail_under = 99` · `ruff` e `mypy` a zero findings · `import main` pulito.

### 11.2 Verificato in esecuzione, non solo dai test

Con un bot Telegram di prova e Redis in Docker:

- il bot parte e usa Redis;
- **con Redis fermo degrada a `MemoryStorage` e resta vivo** — che è l'intero scopo della 0.1;
- quando Redis torna, riparte su Redis senza warning;
- il percorso bot → DM verso l'admin funziona (`sendMessage` diretta, `ok: true`).

### 11.3 Le due verifiche a mano — **chiuse il 2026-08-02**

- [x] **L'alert arriva davvero in DM.** Confermato dall'utente: fermato Redis, il messaggio
      `[WARNING] __main__ — Redis non raggiungibile…` è arrivato in privato. È la catena
      intera (`log.warning` → handler → buffer → `drain` → consegna), che nessun test unitario
      copre: i pezzi erano testati singolarmente, mancava la composizione.
- [x] **Lo stato FSM sopravvive al riavvio del processo.** Aperta una creazione quiz fino a
      `waiting_description`, le chiavi sono state lette **da Redis mentre nessun processo era
      vivo**:

      fsm:<id>:<id>:state  →  QuizCreationStates:waiting_description
      fsm:<id>:<id>:data   →  {"creator_id": …, "title": "Patata"}

      Un processo **nuovo** ha poi ripreso e gestito gli update senza errori. La prova sta nella
      lettura a bot spento: è lì che si vede che lo stato non era nel processo. (Il flusso è
      stato poi abbandonato, quindi nessun quiz è finito a DB — irrilevante per ciò che si
      voleva dimostrare.)

### 11.4 I quattro difetti noti — **chiusi il 2026-08-02**

Erano stati parcheggiati perché il metodo non prevede una seconda ondata di fix dopo la review
finale. Sono stati chiusi subito dopo, in un commit a parte, prima che la Fase 1 ci partisse
sopra. Restano elencati qui perché la storia di *cosa* è stato sbagliato serve più della
rassicurazione che ora è a posto.

| dove | cosa | com'è stato chiuso |
|---|---|---|
| `src/utils/alerts.py` (docstring di modulo) | diceva «deduplicated by template», mentre `_fingerprint` deduplica per **template + tipo di eccezione**: due docstring nello stesso file che si contraddicevano | docstring riscritto, con la ragione (i logger catch-all riusano un solo template) |
| `src/main.py` (blocco `finally`) | il drain finale girava **prima** dei `.cancel()`, quindi per ≤5 s il loop di background poteva drenare in concorrenza. Nessun record perso né consegnato due volte (`_buffer.popleft()` non attraversa mai un `await`); a rischio era solo il *conteggio* dei soppressi | `alert_task.cancel()` spostato **prima** del drain: si drena una volta sola, senza contese. Nessun test nuovo — è un riordino in un file di bootstrap escluso dal coverage, e un test su quella corsa sarebbe teatro |
| piano Fase 0, Task 5 Step 5 | si aspettava che il log dicesse `redis`; dopo il fix dice `RedisStorage` | attesa corretta nel runbook, prima che qualcuno lo esegua cercando una stringa che il codice non stampa più |
| `STEERING.md` §26, punto 3 | descriveva il dedup «per template», senza il tipo di eccezione | punto riscritto con la ragione per cui il tipo entra nella chiave |

### 11.5 Il branch

Il lavoro è su **`test_giu`** e ci resta: nessun merge, nessun push, `main` intatto.

**Attenzione a come lo si porta su `main`.** `main` è un antenato di `test_giu`
(`git rev-list --left-right --count main...test_giu` → `0 127`), quindi un merge sarebbe un
fast-forward che porta **127 commit**, non i 10 della Fase 0 — compresa la migrazione del
modello AI, che è fuori dallo scope di questo documento (§2.3 nodo A). Per portare solo la
Fase 0 servono i suoi dieci commit, `38182d9..0886fb9`, in cherry-pick.

### 11.6 Difetti del piano, per chi scriverà quello della Fase 1

Il piano della Fase 0 si è rivelato sbagliato **cinque volte**, e tutte e cinque sono state
trovate dalle review, non dagli implementer:

1. un `import time` prematuro, che ruff segnalava come F401;
2. un test che asseriva sul contatore sbagliato e **passava identico** sotto la mutazione che
   avrebbe dovuto catturare;
3. il criterio di accettazione dello staging (Task 5 step 4) codificava la riga di log errata;
4. il test sulla soglia richiesto da §3.3 di questo documento è sparito fra spec e piano senza
   una parola;
5. la nota sui soppressi era formattata in modo da essere la prima cosa troncata.

La lezione operativa, che vale per la Fase 1: **un test scritto nel piano non è un test
verificato.** Vanno provati per mutazione — si rompe di proposito il codice che devono
proteggere, e si guarda se diventano rossi. Due dei cinque difetti sono stati trovati così.
