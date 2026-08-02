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
| dedup | Impronta `(record.name, record.msg)` → `utils.cooldown.remaining`/`mark`, finestra 300 s. I soppressi si **contano** e il numero entra nel messaggio successivo |

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
| query di un flusso completo | **da misurare** | listener `before_cursor_execute` di SQLAlchemy su un test che fa 3 domande + 6 edit + pubblicazione |
| tempo della suite | ~40 s | `pytest` |
| coverage | ≥ 99 | `pytest --cov=src --cov-report=term-missing` |

La riga «query di un flusso completo» è **il primo task della fase**: senza il numero di partenza
il confronto dopo non vuole dire niente.

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
| 0.1 | Storage FSM: ping + fallback, `.env.example`, STEERING §2 | ☐ da fare |
| 0.2 | aiogram 3.13.1 → 3.30.0, isolato | ☐ da fare |
| 0.3 | Alert admin (`utils/alerts.py`) | ☐ da fare |
| — | **Gate Fase 0** (§3.4) | ☐ |
| 1 | Spike `guess/creation.py` con aiogram-dialog | ☐ da fare |
| — | **Gate spike** (§4.3) — decide l'utente | ☐ |
| 2 | `admin_dashboard.py` | ☐ subordinata al gate |
| 3 | `quiz/creation.py` + `quiz/editing.py` | ☐ subordinata al gate |
| 4 | `shop.py` | ☐ subordinata al gate |
| 5 | hub eventi + `event_types/` (cambia `render_detail`) | ☐ subordinata al gate |

Legenda: ☐ da fare · ▣ in corso · ☑ fatta · ✗ abbandonata (con il perché, in una riga sotto).
