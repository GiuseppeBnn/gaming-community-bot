# STEERING — Gaming Community Bot

> Documento normativo per lo sviluppo. Leggi questo prima di modificare qualsiasi file.

---

## 0. Layout del progetto (src-layout)

Il **codice applicativo vive sotto `src/`**; i `tests/` restano nella root.

- I package restano **top-level a livello di import** (`from config_data.config import settings`,
  `from handlers.admin import …`) perché `src/` è sul path: `pyproject.toml` ha
  `pythonpath = ["src"]` per i test, l'app si avvia con `python src/main.py`, l'immagine usa
  `COPY src/ ./src/`.
- **Convenzione di questo documento:** i path di modulo citati (es. `handlers/quiz.py`,
  `services/quiz_service.py`) sono **relativi a `src/`**. I path di test/infra
  (`tests/…`, `.github/…`, `docker-compose.yml`) sono relativi alla root.
- Coverage: `--cov=src` (vedi `pyproject.toml`).

---

## 1. Stack e versioni vincolanti

| Dipendenza | Versione | Note |
|---|---|---|
| Python | **3.12** | Una versione sola, ovunque: Dockerfile, CI, `ruff target-version`, `mypy python_version`, venv locale. Prima la CI girava 3.11 e il venv 3.12, quindi «verde in locale» non era la stessa affermazione di «verde in produzione». Se la cambi, cambiala in tutti e cinque i posti — **e gira `pytest -W error::DeprecationWarning`**, che è ciò che ha fatto emergere le `datetime.utcnow()` (deprecate dalla 3.12, in rimozione) quando siamo saliti. Oggi la suite passa anche con quel flag; non è un gate in CI apposta, perché una dipendenza che inizia a deprecare bloccherebbe la build per una cosa non nostra |
| `from __future__ import annotations` | in ogni modulo | Annotazioni pigre: i modelli SQLAlchemy e le firme dei service si auto-referenziano senza virgolette, e i tipi non vengono valutati all'import |
| aiogram | 3.13.1 | **Non** usare aiogram 2.x — API completamente diversa |
| SQLAlchemy | 2.0 (async) | `mapped_column`, `Mapped[T]`, `AsyncSession` |
| pydantic-settings | 2.x | `BaseSettings`, `SettingsConfigDict` |
| DB prod | PostgreSQL 16 (asyncpg) | |
| DB dev | SQLite (aiosqlite) | default in `.env` locale |
| FSM storage | `MemoryStorage` (dev) / `RedisStorage` (prod) | configurabile via `.env` |
| aiohttp | 3.10.11 | client async per le chiamate LLM Groq — **mai** librerie HTTP bloccanti |
| LLM | Groq API (OpenAI-compatible) | modello via `GROQ_MODEL` (default `llama-3.3-70b-versatile`) |
| ruff | 0.16.0 (dev) | **gate CI** su `src/`, ruleset `E9,F,B,ASYNC` — vedi sotto |
| mypy | 2.3.0 (dev) | **gate CI**, non-strict, plugin `pydantic.mypy` — vedi sotto |

### Gate statici (CI, config in `pyproject.toml`)

Entrambi girano **dopo** pytest in `tests.yml` (un nit di lint non deve nascondere un test rosso)
ed erano **a zero findings** quando sono stati introdotti: ogni fallimento è una regressione nuova,
non rumore preesistente da imparare a ignorare.

- **ruff**, ruleset ristretto di proposito: `E9` (sintassi), `F` (pyflakes: nomi non definiti,
  import morti, f-string rotte), `B` (bugbear), `ASYNC` (**I/O bloccante dentro `async def`** — su
  un bot async è uno stallo dell'intero event loop, non un dettaglio di stile). Allargare a
  `I`/`UP`/`SIM`/`RUF100` sono ~130 autofix quasi tutti cosmetici: vale un commit dedicato, non
  un gate rosso. Il gate copre **`src/`**; `tests/` ha ancora ~21 findings preesistenti.
- **mypy**, non-strict, su `services`/`database`/`utils`/`config_data`/`filters`. `handlers/` è
  escluso per ora (è il layer meno annotato): aggiungerlo per-modulo quando viene migrato.
  `disallow_untyped_defs` resta **off**.

---

## 2. Config

File: `config_data/config.py`

**Pattern obbligatorio:** singleton `settings = Settings()` — **mai** `get_settings()`.

```python
from config_data.config import settings
settings.bot_token  # usage
```

Campi importanti:
- `bot_token: str` — obbligatorio
- `db_url: str` — default SQLite dev (`sqlite+aiosqlite:///./data/bot.db`)
- `group_id: int` — **0 significa "non configurato"** → GroupMemberMiddleware bypassa tutto
- `admin_ids: list[int]` — parse da stringa CSV via `@field_validator`
- `daily_reward_coins: int` — **NON `daily_reward`** — matcha la `.env`
- `daily_min_hours: int` (default 6) — gap minimo dall'ultima riscossione, **in AND** con il reset di mezzanotte del `/daily` (§10.a). Tenere **< 24**
- `fsm_storage: str` — `"memory"` | `"redis"`. **Resta `memory`, ed è una scelta, non una svista.** Il costo è noto e accettato: Watchtower ricrea il container a ogni immagine nuova (`WATCHTOWER_POLL_INTERVAL: 600`), e con `MemoryStorage` ogni conversazione FSM aperta in quel momento sparisce — chi stava creando un quiz ricomincia. Il baratto rifiutato è l'altro: `_build_storage` intercetta solo l'`ImportError` del pacchetto, **non** una connessione fallita, quindi con `redis` il bot non degrada, non parte. Perdere un flusso di creazione vale meno di perdere il bot. Non riproporre il passaggio senza prima aggiungere un fallback su errore di connessione
- `redis_url: str`
- `groq_api_key: str` — chiave API Groq per il modulo AI (vuota = AI disattivato, fallback)
- `groq_model: str` — default `"llama-3.3-70b-versatile"` (il vecchio `llama3-70b-8192` è **dismesso**)
- `ai_cooldown_seconds: int` (default 60) — anti-spam comandi AI per non-admin
- `warn_mute_threshold: int` (default 3), `warn_ban_threshold: int` (default 5), `warn_mute_duration_seconds: int` (default 3600) — sistema warn admin
- **XP quiz** (evento, uncapped): `quiz_xp_participation` (20, per ≥1 risposta), `quiz_xp_per_correct` (10, per risposta giusta), `quiz_xp_podium_first/second/third` (50/30/20, bonus podio)
- **Premi quiz per-rango**: `quiz_default_first` (1000), `quiz_default_second` (500), `quiz_default_third` (250), `quiz_default_consolation` (100) — default suggeriti nella creazione; `quiz_participation_floor_ratio` (0.2) + `quiz_participation_floor_min` (1) → minimo garantito = `max(floor_min, round(consolation*ratio))`
- **XP & cataloghi** (§12.1/§12.2): `catalog_dir: str` (default `"data"`, dir dei CSV trofei/ranghi/cosmetici); `xp_daily_participation_cap: int` (default 50, tetto XP *capped*/giorno); `xp_per_daily_claim: int` (default 10); **XP scommesse** (evento, uncapped) `xp_per_bet_placed` (10, per puntata) + `xp_per_bet_won` (25, extra se vince); **curva livelli** `xp_level_base: int` (default 100, XP per il Lv 1→2) + `xp_level_growth: float` (default 1.15, +15%/livello)
- `scheduler_timezone: str` (default `"Europe/Rome"`), `scheduler_poll_interval: int` (default 20) — scheduler eventi
- **Backup & export** (§25): `backup_dir: str` (default `"backups"`), `backup_state_interval_hours: int` (24), `backup_state_keep: int` (5), `backup_chat_interval_hours: int` (168), `backup_max_message_chars: int` (4096); **MTProto** `telegram_api_id: int` (0), `telegram_api_hash: str` (""), `telegram_session: str` ("") — creds vuote ⇒ archivio chat disattivato (la `telegram_session` è una **credenziale sensibile**, solo `.env`)

---

## 3. Schema DB

File: `database/models.py`

### Tabelle principali

```
users           (tg_id PK BigInt, username, full_name, xp, onboarding_completed,
                 last_daily_claim, daily_streak, bets_won, transfers_made,
                 cosmetic_tag, rank_slug, xp_today, xp_today_date, created_at)
wallets         (id PK, tg_id FK unique → users, coins)
ledger          (id PK, from_tg_id, to_tg_id, amount, tx_type, description,
                 reference_id FK → betting_events, created_at)
badges          (id PK, slug unique, name, description, icon_emoji, category,
                 rarity, xp_reward, condition_type, condition_value, condition_param)
                 ← rarity = trofei (§12); condition_param = scope condizioni parametriche
user_badges     (id PK, user_tg_id FK, badge_id FK, earned_at, notified)
                UniqueConstraint(user_tg_id, badge_id)
betting_events  (id PK, title, description, creator_tg_id FK, status, resolution_option_id,
                 betting_window_seconds, closes_at, created_at, locked_at, resolved_at)
                 ← betting_window_seconds = finestra scelta alla creazione (NULL/0 = illimitata);
                   closes_at = scadenza armata all'apertura (= utcnow()+window), auto-lock via scheduler
betting_options (id PK, event_id FK, label, odds_multiplier, total_wagered)
user_bets       (id PK, user_tg_id FK, event_id FK, option_id FK, amount,
                 potential_win, status, placed_at, settled_at)
                UniqueConstraint(user_tg_id, event_id)  ← un solo bet per utente per evento
shop_purchases  (id PK, user_tg_id BigInt, item_key, group_id, target_tg_id,
                 cost, purchased_at, success, error_reason)
warnings        (id PK, user_tg_id BigInt index, group_id, issued_by_tg_id,
                 reason, active Bool, created_at)   ← sistema warn/strike admin
admin_actions   (id PK, admin_tg_id BigInt index, action_type, target_tg_id index,
                 group_id, amount, detail, created_at)   ← audit log azioni admin
quizzes         (id PK, title, description, creator_tg_id, status, group_id,
                 prize_coins [legacy pool], prize_first, prize_second, prize_third,
                 prize_consolation, prize_min, created_at, started_at, finished_at)
quiz_questions  (id PK, quiz_id FK, position, text, options_json, correct_option_id,
                 explanation, open_period, tg_poll_id index, sent_at)
quiz_answers    (id PK, quiz_id index, question_id FK, user_tg_id, selected_option_id,
                 is_correct, response_ms, answered_at)  UniqueConstraint(question_id, user_tg_id)
scheduled_tasks (id PK, task_type [quiz|poll|bet], ref_id, payload_json, run_at index,
                 status, created_by_tg_id, group_id, created_at, executed_at, error)
game_podiums    (id PK, user_tg_id BigInt index, game_key index, rank, ref_id, created_at)
                 ← podio per gioco (trivia|guess|sound); fuel trofei podium_count/first_place_count (§12)
```

### Enums (str, Enum — valori in DB come stringa)

```python
TransactionType: deposit | withdrawal | transfer_out | transfer_in |
                 bet_placed | bet_won | bet_refund | admin_credit |
                 admin_debit | daily_reward | shop_purchase | quiz_reward

EventStatus:     open | locked | resolved | cancelled
BetStatus:       pending | won | lost | refunded
```

### Regole di schema da non violare

- `Wallet` è **separato** da `User` — `User.coins` non esiste
- La valuta si chiama **CoInn** (invariante, scritta esattamente così) nei testi utente; la colonna/attributo DB resta `Wallet.coins` (NON rinominare il campo, solo le stringhe visibili)
- `LedgerEntry` traccia ogni movimento — `amount` positivo per credit, negativo per debit
- `Wallet.coins` e `LedgerEntry.amount` sono **`BigInteger`** (int64): saldi/airdrop accumulati possono superare int32. Modifiche di **tipo** colonna su tabelle esistenti ⇒ voce `ALTER TABLE … ALTER COLUMN … TYPE …` in `_MIGRATIONS` (idempotente: ri-applicare lo stesso tipo è no-op; solo Postgres, SQLite tipizza dinamicamente)
- `bot_state` (key/value) è una tabella **nuova** (no `_MIGRATIONS`): persiste l'id gruppo effettivo dopo una migrazione Telegram (§13, `group_registry`)
- `UserBet` ha UniqueConstraint(user_tg_id, event_id) — un utente non può scommettere due volte sullo stesso evento
- `daily_streak`, `bets_won`, `transfers_made` su `User` vengono aggiornati nei rispettivi service — **non** calcolati on-the-fly
- `User.is_banned` (bool, default false) è il **ban bot-level** (§18, `BannedUserMiddleware`): aggiunto a `users` *dopo* il primo deploy → ha la sua voce `ALTER TABLE … ADD COLUMN IF NOT EXISTS is_banned …` in `_MIGRATIONS`. Si muta **solo** via `admin_service.set_user_banned`; **non** è una condizione-milestone (regola 10 non coinvolta)
- `User.xp` è una **metrica di merito separata dalle monete** e si muta **solo** via `xp_service` (§12.1). Lato display si mostra il **livello** (curva geometrica, §12.1), non l'XP grezzo. `xp_today`/`xp_today_date` sono il contatore del **tetto giornaliero** delle sorgenti capped; `rank_slug` è l'ultimo **tier** (nome rango) visto, per annunciare i tier-up; `cosmetic_tag` è il flair acquistato nel negozio (§11)
- `warnings`/`admin_actions`/`quizzes`/`quiz_questions`/`quiz_answers`/`scheduled_tasks`/`game_podiums` sono tabelle **nuove**: create da `create_all`. Le **colonne premio per-rango** (`prize_first/second/third/consolation/min`), le colonne progressione di `users` (`cosmetic_tag`, `rank_slug`, `xp_today`, `xp_today_date`), `badges.rarity` e **`badges.condition_param`** sono invece state aggiunte a tabelle esistenti *dopo* il primo deploy → hanno voci `ALTER TABLE … ADD COLUMN IF NOT EXISTS …` in `_MIGRATIONS` (idempotenti, solo Postgres; SQLite ricrea da `create_all`). Regola: colonne aggiunte a tabelle esistenti ⇒ voce in `_MIGRATIONS`; tabelle nuove ⇒ no.
- `quiz_questions.options_json` è una lista di stringhe serializzata in JSON (helper `quiz_service.question_options`); `scheduled_tasks.payload_json` è il config JSON per poll/bet (helper `schedule_service.task_payload`)
- timestamp scheduler in **UTC naive** (`schedule_service.utcnow()`); `parse_run_at` converte l'orario locale (`scheduler_timezone`) in UTC naive
- `Warning.active` è un **soft-delete**: `clear_warnings` setta `active=False`, non cancella la riga (storico preservato)
- ordinamento warn: `(created_at DESC, id DESC)` — secondary sort per la precisione-al-secondo di SQLite (stesso motivo di `get_history`)

---

## 4. Dependency injection negli handler

Il `DbSessionMiddleware` inietta la sessione DB con la chiave **`db_session`**.

```python
# CORRETTO
async def cmd_example(message: Message, db_session: AsyncSession) -> None: ...

# SBAGLIATO — non funziona
async def cmd_example(message: Message, session: AsyncSession) -> None: ...
```

---

## 5. Regola dei commit nei service

I service **non committano mai** — il commit è responsabilità del handler.

```python
# Nel service
async def credit(...) -> None:
    await _add_coins(session, tg_id, amount)   # UPDATE ... SET coins = coins + :n
    await session.refresh(wallet, ["coins"])   # l'istanza in sessione dice la verità (§22)
    session.add(LedgerEntry(...))
    # NO commit qui

# Nel handler
await economy_service.credit(db_session, tg_id, amount, ...)
await db_session.commit()  # ← qui
```

**Eccezione:** `shop_service.record_purchase` e `shop_service.mark_success` committano internamente — sono operazioni atomiche di audit trail separate dalla transazione principale.

---

## 6. Ordine middleware (critico)

```python
dp.update.middleware(RateLimitMiddleware())    # 1. rate-limit (12 req/10s per utente)
dp.update.middleware(DbSessionMiddleware())    # 2. DB session + upsert utente
dp.update.middleware(BannedUserMiddleware())   # 3. bannati dal bot → scarto SILENZIOSO
dp.update.middleware(GroupMemberMiddleware())  # 4. blocca non-membri in privato
```

**Non invertire.** Il DB middleware deve girare prima dei guard perché:
- `BannedUserMiddleware` (§18) legge `User.is_banned` via `db_session` e **scarta in silenzio**
  (nessuna risposta, ovunque) gli update di un utente bannato dal bot — i dati restano intatti;
- `GroupMemberMiddleware` fa una API call che richiede il bot (dal framework), non la sessione DB.

---

## 7. Ordine router (critico)

Dichiarato **in un posto solo**: `handlers/__init__.py`, come tupla `ROUTERS` + `register(dp)`.
`main.py` chiama `handlers.register(dp)` e non sa più nulla dell'ordine.

```python
# handlers/__init__.py
ROUTERS: tuple[Router, ...] = (
    group_events.router,    # migrazione/chat_member: ordine indifferente
    onboarding.router,
    economy.router,
    admin_betting.router,   # ← DEVE stare prima di betting
    betting.router,
    ...
    common.router,          # ← DEVE stare per ultimo (catch-all /start)
)
```

`admin_betting` prima di `betting` perché in fondo ad `admin_betting.router` c'è un catch-all deny per tutti i callback `admin_bet:*`. Se `betting.router` fosse registrato prima, i callback `admin_bet:*` non verrebbero mai visti dall'admin.

> **`tests/unit/test_router_order.py` lo verifica**, e la cosa che vale più delle due
> asserzioni sull'ordine è la terza: **cammina il package** e pretende che ogni modulo che
> definisce un `router` sia in `ROUTERS`. Un `handlers/foo.py` nuovo che nessuno registra è
> semplicemente morto — nessun errore, nessun log, i comandi non partono. Nessuna lista di
> esclusioni: chi non va registrato (`_privacy`, `_trophy_announce`, `errors`) non definisce
> un `router`. È scrivendo quel test che è venuto fuori il `Router` inutilizzato che
> `errors.py` si portava dietro.
>
> Confronti per **identità**, non per `Router.name`: solo `Router(name=...)` dà un nome
> leggibile, e questi sono costruiti nudi (`.name` è un id esadecimale).
>
> `ROUTERS` contiene **singleton di modulo** e aiogram rifiuta di attaccare un router a un
> secondo parent: un test che chiama `register()` con quelli veri li lega per sempre a un
> Dispatcher buttato via e rompe la registrazione dell'app in qualunque test successivo. Per
> questo il test di `register` usa router usa-e-getta.

`group_events.router` (gestione migrazioni chat + `chat_member`/`my_chat_member`) ha filtri su tipi
di update disgiunti dagli altri router → ordine indifferente; registrato per primo per chiarezza.
`allowed_updates=dp.resolve_used_update_types()` auto-iscrive `chat_member`/`my_chat_member` perché
esistono gli handler.

### 7.a Handler globale errori (`dp.errors`)

`dp.errors.register(errors.on_error)` in `main.py`, **dopo** `handlers.register(dp)`.
Implementato in **`handlers/errors.py`** (non in `main.py`, che è escluso dalla coverage —
così è testabile: `tests/unit/test_error_handler.py`). Sul **Dispatcher**, non su un router,
per coprire tutti i ~190 handler.

Cosa fa: logga con `exc_info` più `user_id`/`username`/`chat_id`/`callback_data`/testo — l'obiettivo
è che la riga di log basti da sola ad agire — poi risponde all'utente (alert sulle callback).
Senza di esso un'eccezione non gestita lasciava il bot **muto** e, sulle callback, il bottone con lo
spinner appeso fino al timeout di Telegram.

Due scelte deliberate:
- **Nessun rollback della sessione.** `DbSessionMiddleware` apre con
  `async with async_session_maker()`: uscire dal blocco chiude la sessione e **scarta** la
  transazione non committata. Un rollback qui sarebbe codice morto, e opererebbe su una
  sessione che questa funzione non può nemmeno raggiungere.
- **Rumore benigno silenziato**: `message is not modified`, `query is too old`,
  `message to edit not found` sono normali in un bot a callback (ri-render di una tastiera
  identica, tap su un messaggio vecchio) e non dicono niente sul nostro codice → log a `debug`
  e nessun messaggio d'allarme all'utente, ma la callback viene comunque chiusa per fermare
  lo spinner. Aggiungere frammenti a `_BENIGN_FRAGMENTS`, mai un `except` a tappeto.

---

## 8. Filtri admin

```python
from filters.admin_filter import IsAdminFilter, IsAdminCallbackFilter

# Per comandi (Message)
@router.message(Command("credita"), IsAdminFilter())

# Per callback (CallbackQuery)
@router.callback_query(F.data.startswith("admin_bet:event:"), IsAdminCallbackFilter())
```

Entrambi delegano a **`is_admin(bot, user_id)`**: `True` se `user_id in settings.admin_ids`
**oppure** se è amministratore/creator Telegram del gruppo (`get_chat_administrators`, cache 300s,
**fail-closed** su errore API). Il gruppo interrogato è **`group_registry.get_group_id()`** (id
effettivo, §13) — **non** `settings.group_id` diretto, così dopo una migrazione pubblico↔privato
gli admin Telegram non perdono i poteri. Quindi **tutti gli admin del gruppo** hanno i poteri
bot-admin senza doverli elencare in `ADMIN_IDS`. Usare sempre `is_admin` per i check inline (non
`user.id in settings.admin_ids` diretto).

La firma è **`is_admin(bot: Bot | None, user_id: int)`**: ogni chiamante passa `message.bot` /
`callback.bot`, che aiogram tipizza opzionale. Il ramo `bot is None` è **esplicito** e ritorna
`False`. Prima funzionava comunque, ma solo perché l'`except Exception` di `_telegram_admin_ids`
inghiottiva l'`AttributeError` — cioè l'esito di un'**autorizzazione** dipendeva da un catch
incidentale: chi avesse restretto quell'except avrebbe cambiato le regole di accesso senza
accorgersene. Il fail-closed ora è dichiarato, non ereditato.

L'invalidazione cache è **automatica**: gli handler in
`handlers/group_events.py` chiamano `invalidate_admin_cache()` su promozioni/retrocessioni
(`chat_member`/`my_chat_member`) e migrazioni.

**Guardia "Tutti i membri sono amministratori".** Nei *gruppi base* legacy con quell'opzione attiva,
`get_chat_administrators` restituisce **ogni** membro come amministratore → senza difesa, *ogni*
utente diventerebbe bot-admin. `_telegram_admin_ids` confronta il numero di admin con
`get_chat_member_count`: se la lista admin copre **l'intero gruppo** (`≥3` membri e `admin ≥ totale`)
la lista è priva di autorità → viene **scartata**, restano admin **solo gli `ADMIN_IDS`** dell'.env
(con un `log.warning`). No-op per i gruppi normali, dove gli admin sono sempre un sottoinsieme
stretto. Fix per supergruppi: nessun impatto; per gruppi base: convertire in supergruppo o
disattivare l'opzione per riconoscere gli admin del gruppo.

**Gating a livello di router (obbligatorio per i router 100% admin).** I router interamente
admin — `schedule`, `events`, `admin`, `admin_dashboard`, `admin_betting`, `backup` — montano il
filtro alla radice del router, non solo sui singoli handler:

```python
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminCallbackFilter())
```

Motivo: gli handler guidati **solo dallo stato FSM** (input di un wizard, picker, run-at) non
ri-controllerebbero `is_admin`, e lo stato FSM **non ha TTL** (sopravvive in Redis/Memory). Senza
il gate di router, un admin che entra in un flusso e poi **perde i diritti** potrebbe portarlo a
termine (privilege escalation). Il gate di router chiude l'intera classe. I router **misti**
(`betting` con `/crea_scommessa` community, `quiz` con `quiz_ans:*` pubblico) **non** possono
montarlo: lì ogni handler admin va gated singolarmente.

---

## 9. Deep-link pattern

Tutti i redirect gruppo → privato usano `?start=<payload>`.

| Payload | Handler | Destinazione |
|---|---|---|
| `help` | `common.cmd_start` | Mostra guida comandi |
| `spiega_<cmd>` | `common.cmd_start` → `help_content.render_command_or_hint` | Man page del comando `<cmd>` (dal bottone di `/spiega_comando` nel gruppo) |
| `manage_bets` | `common.cmd_start` | Apre pannello admin scommesse (`admin_betting._show_event_list`) |
| `admin` | `common.cmd_start` → `admin.show_admin_panel` | Apre il pannello admin (dashboard) |
| `create_quiz` | `common.cmd_start` → `quiz.start_quiz_creation` | FSM creazione quiz (admin) |
| `create_poll` | `common.cmd_start` → `events.start_poll_creation` | FSM creazione sondaggio (**admin**, re-check in `cmd_start`) |
| `quiz_<id>` | `common.cmd_start` → `quiz.start_quiz_session` | Gioca/riprendi un quiz in privato |
| `programma` | `common.cmd_start` → `schedule.start_schedule_flow` | FSM programmazione evento (admin) |
| `shop_<group_id>` | `common.cmd_start` → `shop.start_shop_private` | Catalogo Locanda (cosmetici + menù) |
| `create_bet` | `common.cmd_start` → `betting.start_bet_creation` | FSM creazione scommessa |
| `bet_custom_<e>_<o>` | `common.cmd_start` → `betting.start_custom_amount` | FSM importo custom |
| `bet_<event_id>` | `common.cmd_start` → `betting.start_bet_view` | Dettaglio evento |
| `saldo` | `common.cmd_start` → `economy.show_saldo` | Saldo personale (back-compat: usato dal fallback `/daily`) |
| `storico` | `common.cmd_start` → `economy.show_storico` | Storico movimenti (redirect privacy) |
| `profilo` | `common.cmd_start` → `common.show_profilo` | Profilo personale (back-compat) |
| `traguardi` | `common.cmd_start` → `badges.show_traguardi` | Trofei personali (redirect privacy) |
| `daily` | `common.cmd_start` → `economy.show_saldo` | Fallback `/daily` da gruppo (DM fallito) |
| `scommesse` | `common.cmd_start` → `betting.show_events_private` | Lista scommesse aperte (redirect privacy) |

`/profilo` e `/saldo` sono **pubblici**: rispondono in chiaro anche nel gruppo (decisione di
prodotto — il saldo è visibile a tutti), con l'**anti-flood "sostituisci"** di `utils/static_reply`
(§14, una sola risposta viva per utente+comando). `/profilo` **non** mostra mai il Telegram ID
(resta esclusiva del dossier admin `/info`). I comandi che mostrano **dati personali** ancora
**redirezionati** in privato (`handlers/_privacy.redirect_to_private`, bottone deep-link) sono
`/storico`, `/traguardi`, `/locanda`, `/classifiche`, **`/scommesse`**. `/daily` riscuote subito in
gruppo con un **ack minimale** e manda i dettagli (streak/XP/rank) via **DM best-effort**.

> **`/scommesse` è privato** (dalla revisione privacy): la lista marca con «✅ Hai già scommesso»
> gli eventi su cui *chi ha lanciato il comando* ha puntato. In gruppo quel messaggio è leggibile da
> tutti — inclusi utenti che non hanno mai scommesso — quindi era un leak di dati personali. La
> vecchia `betting_kb.get_group_events_keyboard` (bottoni URL con le spunte, postati nel gruppo) è
> stata **rimossa**; il ramo privato vive in `betting.show_events_private`, condiviso fra il comando
> e il deep-link `scommesse`.

I flussi di **creazione eventi** (quiz/sondaggio/scommessa) sono **solo in privato**: nel gruppo i
comandi (`/crea_quiz`, `/sondaggio`, `/crea_scommessa`) rispondono con un bottone deep-link e **non**
avviano la FSM lì (mai prompt/input nel gruppo dove chiunque può leggere o interferire). Poiché
`common.router` è **pubblico**, **ogni** landing deep-link admin in `cmd_start` (`admin`, `eventi`,
`create_quiz`, `create_poll`, `programma`, `manage_bets`, `backup`/`esporta`) **ri-verifica
`is_admin`**: il filtro del comando d'origine non si propaga al deep-link, che un non-admin potrebbe
costruire a mano. (`create_bet` resta pubblico per design, §16.)

---

## 10. Betting — payout Twitch-style

L'intero pool viene redistribuito ai vincitori **proporzionalmente** al loro bet individuale.

```
payout_i = floor((bet_i / total_winning_pool) * total_pot)
leftover  → al biggest winner (evita monete perse per arrotondamento)
```

Implementato in `services/bet_service.py::resolve_event`.
La preview (stima) per l'utente nella schermata di conferma usa la stessa formula applicata sul pool simulato *dopo* il suo bet.

---

## 10.a Premio giornaliero (`/daily`)

`services/economy_service.py::claim_daily` — **una riscossione per giorno di calendario**, con
reset a **mezzanotte locale** (non 24h rolling, che faceva slittare l'orario di giorno in giorno
finché l'utente sbatteva contro la mezzanotte e perdeva la streak senza colpa).

Due regole **in AND**:

1. **Nuovo giorno locale** rispetto all'ultima riscossione (mezzanotte in `scheduler_timezone`).
2. Almeno **`settings.daily_min_hours`** (default 6) dall'ultima riscossione.

La (2) esiste **solo** per impedire il doppio claim 23:59 → 00:01: **non** è una via alternativa.

> ⚠️ **Regola di implementazione.** Le due condizioni sono espresse come **una sola soglia**
> `next_allowed = max(next_local_midnight(last), last + daily_min_hours)` e mai come due booleani:
> un `or` al posto dell'`and` trasformerebbe il gap minimo in "riscuoti ogni 6 ore". Con il `max()`
> l'AND è **strutturale** e non si può sbagliare. La stessa soglia dà i secondi residui
> dell'errore `DailyAlreadyClaimedError` (mai `24h - elapsed`).
>
> Nella `WHERE` le stesse due regole sono riscritte come **istanti fissi**, che è ciò che le fa
> stare in una sola `UPDATE` condizionale (una soglia derivata da `last` non ci starebbe, §22):
> `now >= next_local_midnight(last)` ⟺ `last < daytime.local_midnight(oggi)`, e
> `now >= last + daily_min_hours` ⟺ `last <= now - daily_min_hours`. Lo streak si decide con un
> `CASE` nella `SET`, che vede i valori **precedenti** della riga. Equivalenze verificate una per
> una, giorni di cambio ora inclusi (`tests/unit/test_daytime.py`).

**Invariante:** `daily_min_hours < 24` ⇒ il gap non può mai costare un giorno. Il claim più tardi
possibile (23:59) sblocca alle 05:59 del giorno dopo, sempre dentro quel giorno.

**Streak:** prosegue **solo se la riscossione precedente è di *ieri*** (giorno locale); saltare un
giorno intero la azzera a 1. (Un secondo claim nello stesso giorno non arriva mai al calcolo.)

Il "giorno" viene da **`utils/daytime`**, unica fonte di verità condivisa con il tetto XP
giornaliero (§12.1): timestamp salvati naive-UTC, confronti fatti sul giorno **locale**, DST
gestito da `zoneinfo`. Calcolare il giorno in UTC farebbe scattare il reset all'01:00/02:00 italiane.

Due funzioni, complementari: **`next_local_midnight(stamp)`** = quando finisce il giorno di
`stamp` (soglia derivata da una riga), **`local_midnight(day)`** = quando *apre* un giorno di
calendario (istante fisso, confrontabile con una colonna in SQL). La seconda è quella che permette
di scrivere il reset giornaliero come una `WHERE`; la prima è espressa in termini della seconda,
così la logica DST sta in un posto solo.

---

## 11. Locanda — cosmetici + consumabili

File: `services/shop_service.py` (cosmetici) · `services/consumable_service.py` (consumabili) ·
handler `handlers/shop.py` · kb `keyboards/shop_kb.py`

Il vecchio "negozio" è ora **La Locanda del Drago**: comando **`/locanda`** (alias nascosto
`/negozio`, deep-link `shop_<chat_id>` invariato). Due sezioni nello stesso pannello inline
(`shop:home`): **🏷️ Personalizzazioni** (cosmetici) e **🍖 Menù della Locanda** (consumabili),
più **🎒 Dispensa** (inventario). In gruppo fa **redirect** in privato (il catalogo svela il
saldo dell'apertore). Niente item di moderazione (rimossi: erano grief).

### 11.b Consumabili (cibi/bevande) — `consumable_service`

Acquisto **ripetibile** (non idempotente, a differenza dei cosmetici): spende CoInn, logga una
riga `ShopPurchase` (`group_id=0`, `success=True`) e accumula la **dispensa** dell'utente
(mostrata sul profilo). **Nessun permesso Telegram, nessun effetto di gioco** → puro sink +
fuel trofei. L'inventario è **derivato** da `shop_purchases` (COUNT per `item_key`): nessuna
colonna su `User`. Flusso: `shop:menu` → `shop:cat:<key>` → `shop:cbuy:<key>` →
`shop:cexec:<key>` (debit con lock wallet → `record_consumption` → **flush** → milestone check →
commit). Il **flush prima del milestone check** è obbligatorio (autoflush off: la query dei
conteggi non vedrebbe l'INSERT pendente). Catalogo CSV `consumables.csv` +
`consumable_categories.csv` (§12.2).

> **Anti-collisione chiavi**: consumabili `cons_*`, cosmetici `tag_*` — condividono
> `shop_purchases.item_key` ma i registry sono disgiunti, quindi `has_cosmetic` e
> `consumable_service.purchase_counts` (filtrato sulle chiavi consumabili) non si incrociano mai.

### 11.a Cosmetici — `shop_service`

Il negozio vende **cosmetici** (tag/titoli), **non** azioni di moderazione. I vecchi item
mute (`mute_user/mute_admin/mute_random`) e tutto il flusso target sono stati **rimossi**:
erano un vettore di grief. Catalogo da CSV via `catalog_loader` (§12.2):

```python
COSMETICS: dict[str, CosmeticItem]  # = catalog_loader.get_cosmetics()
# CosmeticItem(key, name, tag_text, emoji, price)
```

**Solo in privato:** in gruppo il catalogo esporrebbe il saldo dell'apertore a tutti (e i bottoni
inline agiscono su chiunque clicchi), quindi `/locanda` in gruppo fa **redirect** con bottone
deep-link `?start=shop_<chat_id>` (`redirect_to_private`, §9); in privato `_show_home` mostra le due
sezioni. `start_shop_private` (deep-link) chiama `_show_home`.

### Flow acquisto

```
/locanda → privato: _show_home (sezioni) · gruppo: redirect deep-link
shop:home → landing · shop:list → catalogo cosmetici · shop:menu → categorie consumabili · shop:pantry → dispensa
shop:buy:<key>  → idempotenza (già posseduto? alert) → balance check → conferma + anteprima tag
shop:exec:<key> → debit (lock wallet) → re-check idempotenza SOTTO LOCK → record_purchase (no-commit) → apply_cosmetic → commit
shop:cbuy:<key> → balance check → conferma (consumabile, ripetibile)
shop:cexec:<key> → debit (lock wallet) → record_consumption → flush → milestone check → commit
shop:owned      → alert "già posseduto" · shop:list/menu/home → naviga · shop:close → elimina
```

Il re-check di `has_cosmetic` **dopo** il debit (che prende il lock di riga del wallet) chiude la
race di doppio acquisto concorrente: se nel frattempo un altro exec ha già applicato il cosmetico,
`rollback()` + alert (mai doppio addebito).

### Tag multipli (switch + combina)

Un utente può tenere **più tag attivi insieme** (cambiare tra i posseduti e combinarli), fino a
`settings.max_active_tags` (default 3, alzabile). Modello: `User.active_tags_json` = lista JSON
ordinata di `item_key`; `User.cosmetic_tag` resta come **fallback legacy** single-tag tenuto in sync.

- `shop_service.toggle_tag(session, uid, key, max_active)` → `activated`/`deactivated`/`cap`/`notowned`
  (verifica possesso + rispetta il cap, no-commit).
- `shop_service.render_active_tags(user)` → stringa flair (chiavi risolte via catalogo, concatenate);
  fallback al `cosmetic_tag` legacy se la lista attiva è vuota. **Tutto il rendering** (profilo §,
  traguardi, dossier admin, **classifiche**) passa di qui.
- `ensure_active_seeded` migra al volo il vecchio `cosmetic_tag` nella lista attiva alla prima apertura
  dello switcher (`shop:tags` → toggle `shop:tag:<key>`). Nessuna migrazione DDL di backfill richiesta.

### Invarianti di sicurezza shop (anti-grief / anti-escalation)

- Il tag è **solo cosmetico** (`User.cosmetic_tag`, mostrato sul profilo) → **nessun permesso Telegram**, nessuna escalation.
- Cosmetici da **lista curata CSV** (mai testo libero) → niente impersonazione.
- Ogni acquisto **debita e applica solo a `callback.from_user.id`** ed è **idempotente** (`has_cosmetic` blocca il ri-acquisto) → nessun utente può intaccare un altro, anche col catalogo condiviso in gruppo.
- `record_purchase` ora è **no-commit** (atomico con debit+apply nel commit del handler); `ShopPurchase` riusata per audit (item_key = chiave cosmetico, group_id=0).

---

## 12. Trofei (badge con rarità)

File: `services/badge_service.py` · handler `handlers/badges.py`

I "traguardi" sono **Trofei** stile PlayStation: il modello `Badge` ha una colonna
**`rarity`** (`bronze | silver | gold | platinum`, vedi `RARITY_ORDER`/`RARITY_LABELS`).
Catalogo **CSV-driven** (§12.2): `sync_trophies(session, rows=None)` fa **upsert per slug**
(insert se manca, aggiorna i campi `_SYNC_FIELDS` se esiste); `seed_badges` = `sync_trophies`
coi default. Catalogo default (9 trofei) in `catalog_loader.DEFAULT_TROPHIES`.

**Motore condizioni — data-driven (`check_and_award_milestones`).** Niente catena `if/elif`
sparsa: un **dispatch** di valutatori per `condition_type`, con aggregati calcolati **lazy** (una
sola query ciascuno, solo se un candidato la richiede). Tipi supportati (vedi
`catalog_loader.TROPHY_CONDITIONS`):

| `condition_type` | sorgente | `condition_param` |
|---|---|---|
| `onboarding`·`balance`·`daily_streak`·`bets_won`·`transfers_made`·`xp` | `User`/`Wallet` | — |
| `item_purchases` | conteggi consumabili (`consumable_service.purchase_counts`) | key consumabile `cons_*` |
| `category_purchases` | somma conteggi categoria | key categoria |
| `shop_purchases` | totale consumabili acquistati | — |
| `podium_count` / `first_place_count` | `progress_service.podium_counts` (tabella `game_podiums`) | game key (`trivia`·`guess`·`sound`, `None`⇒`any`) |
| `collection` | possesso di altri trofei | **slug prerequisiti separati da `;`** |

La colonna **`Badge.condition_param`** (String(128), nullable) scopa le condizioni parametriche;
`condition_value` resta la soglia numerica (per `collection` è `None`). **Pass a punto fisso**: una
`collection` può sbloccarsi nello stesso giro dell'ultimo prerequisito (il loop ricalcola gli slug
posseduti finché stabile) → es. *Critico Gastronomico* (tutti i 12 bronzo del menù) e *Leggenda di
Dragons'Inn* (Critico + i 4 argento). Il "Platino" classico resta un `xp` ad alta soglia.

La resa **user-facing** passa da `badge_service.describe_condition(type, value, param)` (Italiano
leggibile, nomi item/categoria/gioco risolti dal registry — **mai** il gergo `type ≥ value`),
condivisa da `/catalogo_badge` e `/traguardi`. I counter capped vengono incrementati in:
- `daily_streak` → `economy_service.claim_daily()`
- `bets_won` → `bet_service.resolve_event()`
- `transfers_made` → `economy_service.transfer()`
- consumabili → `consumable_service.record_consumption` (Locanda, §11.b)
- podio → `progress_service.record_podium` (quiz `close_quiz` per `trivia`; `guess`/`sound` futuri)

`check_and_award_milestones` va chiamato dopo ogni azione che può sbloccare un trofeo, prima
del commit finale; **non committa**. `leaderboard_trophies(session)` ordina per numero di
trofei (tie-break XP).

> **I Trofei NON danno XP.** `Badge.xp_reward` resta solo come dato di "valore" mostrato a
> schermo (storicamente non era comunque applicato a `User.xp`). Gli XP arrivano solo dalle
> sorgenti di §12.1 → niente cascata di sblocchi.

`/traguardi` mostra i trofei **raggruppati per rarità** + rango/tag correnti; `/catalogo_badge`
elenca tutto con rarità e condizione. Con il catalogo esteso (40+ trofei, ciascuno con la sua
condizione di sblocco) il corpo supera il **limite Telegram di 4096 caratteri** → entrambi i
comandi spezzano l'output in più messaggi via **`utils.text.chunk_blocks(blocks, sep)`** (packing
greedy che non taglia mai un blocco a metà, così nessun tag HTML viene spezzato). Vale per
**qualunque** listato che cresce coi dati (trofei, futuri elenchi): non fare mai
`message.answer("\n".join(...))` su una lista illimitata.

---

## 12.1 XP & progressione

File: `services/xp_service.py` — **unico** punto che muta `User.xp` (no-commit, §5).

```python
grant_xp(session, tg_id, amount, source, *, capped) -> XpGrantResult(granted, capped, new_rank, new_level, leveled_up)
set_xp(session, tg_id, value)          # admin: valore assoluto
airdrop_xp(session, amount)            # admin: +amount a tutti
level_for_xp(xp) -> LevelProgress      # livello + progresso (curva geometrica)
xp_to_reach_level(level) -> int        # XP cumulativi per il livello (inverso, usato da /lista_ranghi)
progress_bar(prog, width=6) -> str     # ▰▰▱▱ puro (no HTML), per il profilo
rank_for_level(level) -> Rank | None   # tier = max(min_level ≤ level), registry §12.2
rank_for_xp(xp) -> Rank | None         # tier via level_for_xp(xp).level
leaderboard_xp(session, limit=10)
```

**Livelli (GTA-style):** la progressione mostrata all'utente è un **livello numerico**, non l'XP
grezzo. Il costo per salire dal livello `n` a `n+1` è `round(xp_level_base · xp_level_growth^(n-1))`
(default 100 · +15%). `level_for_xp` itera (la curva cresce in fretta: poche decine di iterazioni).
I **nomi rango** (`Rank`, §12.2) sono **tier keyed per livello** (`min_level`, CSV-driven) e si
mostrano accanto al livello (`⚡ Livello N · 🎖️ Tier`). Sito di display utente: `/profilo`,
`/traguardi`, `/classifiche` (board ⚡ = livelli) — **mai** l'XP totale grezzo; le view admin
(`/info`, dashboard, `/lista_ranghi`) lo mostrano per diagnostica.

**Sorgenti XP** (`XpSource`) — **premiano la partecipazione, non solo la vincita**:

- `quiz` (evento, **non capped**, in `quiz_service._grant_xp`): a **tutti** quelli che hanno
  risposto ad ≥1 domanda `quiz_xp_participation`; `+quiz_xp_per_correct` per risposta corretta;
  ai primi 3 del podio un bonus `quiz_xp_podium_first/second/third`.
- `bet_placed` / `bet_won` (evento, **non capped**): `xp_per_bet_placed` quando si **piazza** una
  scommessa (una volta per evento — `place_bet`); `xp_per_bet_won` extra ai **vincitori** (`resolve_event`).
- `daily` (**capped**) — `xp_per_daily_claim`.
- `admin_grant` / `admin_airdrop` (**non capped**) — `/dai_xp`, `/set_xp`, Airdrop XP dashboard

**Tetto giornaliero** (anti-farm): le sorgenti `capped=True` accreditano al massimo
`xp_daily_participation_cap` per utente al giorno, contato in `User.xp_today`/`xp_today_date`
(reset automatico al cambio data). Il "giorno" è quello **locale** di `utils/daytime`
(mezzanotte in `scheduler_timezone`), lo **stesso** confine del `/daily` (§10.a): nel bot
esiste **una sola** nozione di giorno. Gli **eventi admin** (quiz e scommesse) sono uncapped
perché curati e non spammabili (1 sola scommessa per evento, quiz aperti dall'admin).

**Rank-up / level-up:** `grant_xp` ricalcola tier e livello; restituisce il **tier-up** in
`new_rank` (+ aggiorna `User.rank_slug`) e il **level-up** in `new_level`/`leveled_up` (derivato,
nessuna colonna). Gli handler `/daily`, `/dai_xp` annunciano entrambi.

**Regola d'oro:** nessun handler deve fare `user.xp += …` direttamente — sempre via `xp_service`.

### Classifiche — `handlers/leaderboard.py`

Comando utente `/classifiche` con switcher inline `lead:coins|xp|trofei` (`render_board`
riusato anche dalla dashboard `adm:lead:*`). Board: 💰 `admin_service.leaderboard`,
⚡ `xp_service.leaderboard_xp`, 🏆 `badge_service.leaderboard_trophies`.

---

## 12.2 Cataloghi CSV (catalog_loader)

File: `services/catalog_loader.py` — carica **trofei**, **ranghi**, **cosmetici**, **consumabili**
e **categorie consumabili** da CSV nella dir `settings.catalog_dir` (default `data/`), **una sola
volta all'avvio** (riavvio per applicare le modifiche). Template in `catalogs/*.example.csv` +
`catalogs/README.md`.

- `load_trophies()/load_ranks()/load_cosmetics()/load_consumables()/load_consumable_categories()` —
  puri, **validano** le righe, **saltano e loggano** quelle malformate, e in caso di file
  assente/vuoto **fanno fallback ai default Python**
  (`DEFAULT_TROPHIES/RANKS/COSMETICS/CONSUMABLES/CONSUMABLE_CATEGORIES`) → il bot parte sempre,
  anche a freddo/nei test. `load_trophies` legge la colonna opzionale `condition_param` (assente ⇒
  back-compat); i tipi *item/category/collection* senza param vengono scartati.
- `init_registries(catalog_dir=None)` (chiamata in `main()`) popola i registry in-memoria
  `_ranks`/`_cosmetics`/`_consumables`/`_consumable_categories`; accessori
  `get_ranks()/get_cosmetics()/get_consumables()/get_consumable_categories()`. I **ranghi non hanno
  tabella** (tier keyed per **`min_level`** nel CSV `ranks.csv` — colonna cambiata da `min_xp`; il
  livello deriva dagli XP via la curva §12.1); **cosmetici e consumabili** neppure (registry
  in-memoria, acquisto loggato in `ShopPurchase`; la dispensa è derivata dai conteggi).
- `main()` chiama `badge_service.sync_trophies(session)` (trofei → DB) + `catalog_loader.init_registries()` e logga i conteggi.

**Per personalizzare:** copia `catalogs/*.example.csv` in `data/` (senza `.example`), edita, riavvia.

---

## 13. GroupMemberMiddleware & group_registry (id gruppo effettivo)

**Bug della migrazione (risolto):** rendere pubblico un gruppo base lo converte in **supergruppo
con un nuovo chat id** → `settings.group_id` (dalla `.env`) diventa stale e admin/membership si
rompono. Soluzione: **mai leggere `settings.group_id` a runtime** — usare
**`services/group_registry.get_group_id()`** (id effettivo in-memoria, fallback a `settings`).

- `group_registry.load(session)` (chiamato in `main()` allo startup) ripristina l'override
  persistito nella tabella `bot_state`, **a meno che** l'operatore non abbia cambiato `GROUP_ID`
  nella `.env` (in quel caso scarta l'override e pulisce le righe). `record_migration(session, old,
  new)` aggiorna l'id effettivo e persiste (no-commit, §5); `set_runtime_group_id(id|None)` per
  test/handler.
- `handlers/group_events.py` intercetta `migrate_to_chat_id`/`migrate_from_chat_id` (→
  `record_migration` + commit + invalidazione cache admin/membership) e `chat_member`/
  `my_chat_member` (→ invalida membership per quell'utente, admin cache su promozione/retrocessione).
  Gli update `chat_member` arrivano **solo se il bot è admin del gruppo**; altrimenti degrado al TTL.
- **Rete di sicurezza**: `group_registry.send_group_message(bot, session, …)` cattura
  `TelegramMigrateToChat` dall'API, registra la migrazione e ritenta (usato da `open_quiz`,
  `close_quiz`, scheduler `execute_task` per gli annunci di gruppo).

`GroupMemberMiddleware`:
- Attivo solo se `group_registry.get_group_id() != 0`
- Bypassa update del gruppo (chat.type != "private")
- Cache per-utente TTL 300s → `invalidate_cache(user_id)` su join/leave (da `group_events`),
  `invalidate_all()` su migrazione; prune lazy delle entry scadute oltre 4096 chiavi
- Fail **open** in caso di errore API (non blocca utenti se il bot è rimosso dal gruppo)

---

## 14. Rate limiting

`RateLimitMiddleware`: max 12 chiamate in 10 secondi per utente (sliding window in-memory).
Si applica a tutti gli update: `Message` e `CallbackQuery`. Il dict per-utente è **prunato**: la
chiave si rimuove quando la finestra si svuota + sweep periodico ogni 512 chiamate (no crescita
illimitata con tanti utenti).

### Anti-flood comandi statici di gruppo (`utils/static_reply`)

Oltre al rate-limit globale, i **comandi statici** che ora rispondono **in chiaro nel gruppo**
(`/profilo`, `/saldo`, la vista non-admin di `/quiz`, il bottone-guida di `/comandi`) usano
`utils.static_reply.reply_static(message, text, bucket, **kwargs)`: strategia **"sostituisci il
precedente"** — tiene una mappa `(chat_id, user_id, bucket) → message_id` e, prima di inviare la
risposta nuova, **cancella** quella precedente del bot per quella chiave (best-effort) ⇒ **una sola
copia viva** per utente+comando, niente muro di duplicati. Solo nei gruppi (in privato risponde e
basta); cancella **solo le risposte del bot**, mai i messaggi-comando dell'utente. Mappa **prunata**
a soglia. Davanti c'è un **cooldown leggero silenzioso** `utils.cooldown.ready(...)`
(`command_cooldown_seconds`, admin esenti): entro la finestra la ripetizione è ignorata **senza**
aggiungere altro rumore (la risposta fresca è già lì).

---

## 15. FSM states attivi

| State | File | Descrizione |
|---|---|---|
| `BetCreationStates.waiting_for_title` | `handlers/betting.py` | |
| `BetCreationStates.waiting_for_description` | `handlers/betting.py` | |
| `BetCreationStates.waiting_for_options` | `handlers/betting.py` | |
| `BetCreationStates.waiting_for_window` | `handlers/betting.py` | finestra puntate: preset (`bet:win:<sec>`)/♾️ (`bet:win:0`)/✏️ custom → crea l'evento |
| `BetCreationStates.waiting_for_window_custom` | `handlers/betting.py` | durata custom (`schedule_service.parse_duration`, 30m/2h/1d) |
| `BetCustomAmountState.waiting_for_amount` | `handlers/betting.py` | |
| `QuizCreationStates.*` | `handlers/quiz.py` | creazione quiz: title→desc→**prize_mode**→{prize_first/second/third/consolation}→loop domande {text→options→correct→explanation}→**reviewing**. Tasti «⬅️ Indietro» (`quiz_new:back`, mappa `_BACK_PROMPTERS`) e schermata di riepilogo prima di pubblicare. |
| `QuizEditStates.*` | `handlers/quiz.py` | modifica domande di un quiz **solo `ready`** dal dettaglio eventi (bottone «✏️ Modifica domande» → `quiz_edit:nav:<quiz_id>:0`). Namespace callback `quiz_edit:*`: scorrimento domanda per domanda (⬅️/➡️) + edit singolo di `editing_text`/`editing_options`(→`editing_correct`)/`editing_explanation`, o «🔄 Rifai domanda» (redo dell'intero flusso, flag `edit_redo`). Persiste via `quiz_service.update_question` (guardia di stato `ready`, no-commit §5); handler admin-gated singolarmente (router misto §8). |
| `AdminPanelStates.*` | `handlers/admin_dashboard.py` | input della dashboard a bottoni: `waiting_amount` (credit/debit/setbal/**xpgrant/xpset**) · `waiting_duration` · `waiting_reason` · `waiting_search` · `waiting_airdrop` · `waiting_xp_airdrop` |

> La Locanda non usa una FSM: cosmetici e consumabili si applicano al volo (§11), nessun `ShopState`.
| `ScheduleStates.*` | `handlers/schedule.py` | programmazione quiz/poll/bet (scelta orario run-at) |
| `PollTemplateStates.*` | `handlers/events.py` | creazione sondaggio (domanda + opzioni); riusata da 🎬 Eventi **e** da `/sondaggio` (`events.start_poll_creation`) |

---

## 16. Comandi registrati

### Privato
`/start`, `/profilo`, `/saldo`, `/storico`, `/daily`, `/trasferisci`, `/scommesse`, `/crea_scommessa`, `/quiz`, `/traguardi`, `/catalogo_badge`, `/classifiche`, `/locanda` (alias `/negozio`), `/comandi`, `/spiega_comando <cmd>`

### Gruppo
`/scommesse`, `/crea_scommessa`, `/daily`, `/saldo`, `/trasferisci`, `/profilo`, `/quiz`, `/traguardi`, `/catalogo_badge`, `/classifiche`, `/locanda`, `/comandi`
> `/trasferisci` e `/catalogo_badge` (alias `/catalogo_trofei`) sono ora nel menù «/» **anche di gruppo**
> (prima solo privato/admin). NB: il menù «/» è cachato dai client Telegram → dopo il deploy può servire
> riaprire l'app per vederli (non serve rinominare il comando).

> **`/help` → `/comandi`**: il comando canonico è ora `/comandi` (`/help` resta come alias nascosto +
> deep-link `?start=help`). `/comandi` e `/spiega_comando <cmd>` (man page per-comando, **solo privato**)
> condividono il registro unico in `handlers/help_content.py` (zero drift legenda↔dettaglio).

> **`/quiz` ha due facce** (un solo handler, ramifica su `is_admin` — niente filtro sul decoratore):
> per i **non-admin** è user-facing (bottone «▶️ Gioca» sul quiz in corso, o un messaggio chiaro
> «Nessun quiz attivo» invece del silenzio); per gli **admin** è la gestione (lista quiz pronti /
> redirect al pannello). Gli handler di gestione restano gated.

> **Privacy (§9):** in gruppo `/storico` · `/traguardi` · `/locanda` · `/classifiche` · `/scommesse`
> **non rispondono in chiaro** ma mandano un bottone deep-link verso il privato (la classifica ha uno
> switcher + tasto Chiudi → chiunque poteva chiuderla, ora è privata). `/profilo` e `/saldo` sono invece
> **pubblici** (rispondono in chiaro nel gruppo, anti-flood "sostituisci" §14; `/profilo` senza Telegram
> ID). `/daily` riscuote in gruppo con ack minimale + DM dei dettagli.

> **Anti-spam (§3 nota):** oltre al rate-limit globale (12/10s, `middlewares/rate_limit.py`) c'è un
> cooldown per-comando riusabile (`utils/cooldown.py`, admin-exempt) su comandi pesanti e sull'avvio
> dei flussi di creazione eventi (`settings.command_cooldown_seconds` / `event_create_cooldown_seconds`).

**Intrattenimento AI** (gruppo): `/maestro`, `/complotto`, `/difendi`, `/accusa`, `/drama`, `/dialetto`, `/insulta`

### Admin only (non registrati nelle command list pubbliche — §18 regola 11)
- **Scommesse**: `/gestisci_scommesse`
- **Valuta**: `/credita`, `/addebita`, `/setsaldo`, `/airdrop`, `/saldo_di`
- **XP**: `/dai_xp @u <n>` (grant, uncapped), `/set_xp @u <n>` (assoluto) — gestione XP solo admin (§12.1)
- **Moderazione**: `/ban`, `/sban`, `/kick`, `/mute [durata]`, `/unmute`
- **Warn**: `/warn [motivo]`, `/warns`, `/unwarn`
- **Info & dashboard**: `/info`, `/cerca` (**solo privato**), `/classifica`, `/stats`, `/audit` (**solo privato**), `/lista_ranghi` (curva livelli + fasce tier, §12.1), `/admin` (UI a bottoni — §18.1)
- **Eventi** (macro-categoria, §18.2): `/eventi` (hub), `/crea_quiz`, `/quiz`, `/avvia_quiz <id>`, `/chiudi_quiz <id>` (gestione quiz **solo privato**), `/sondaggio`, `/programma`, `/programmati`
- **Visibilità menù "/":** gli admin vedono i comandi admin nel menù grazie a uno scope dedicato
  (`BotCommandScopeChat` per ogni `settings.admin_ids` in privato + `BotCommandScopeChatAdministrators`
  sul gruppo) — i comandi restano fuori dalle liste pubbliche (§18 regola 11).
- **Backup** (§25): `/backup` (estende l'archivio chat MTProto + DM del file), `/esporta` (snapshot dello stato totale + DM). Entrambi redirect dal gruppo al privato; ogni run è in `log_action`

---

## 17. Modulo Intrattenimento AI

Comandi comici "one-shot" che rielaborano un messaggio via LLM. Tono edgy/satirico per adulti.

**File:** `services/ai_service.py` (Service Layer), `handlers/fun_ai.py` (handler).

### ai_service — client Groq

- **Sempre `aiohttp` async** — mai librerie bloccanti (non bloccare l'event loop di aiogram).
- Endpoint OpenAI-compatible: `https://api.groq.com/openai/v1/chat/completions`.
- `generate_completion(system_prompt, user_text, max_tokens=300, *, temperature=None) -> str`:
  - `settings.groq_api_key` vuota → `AIServiceError` (niente chiamata di rete).
  - Timeout `aiohttp.ClientTimeout(total=20)`; `try/except` su `asyncio.TimeoutError` / `aiohttp.ClientError` / status≠200 / body malformato → tutti normalizzati in **`AIServiceError`**.
  - `temperature` **per-comando**: `None` ⇒ default `_TEMPERATURE` (0.9, alto → risposte varie/creative); un valore più basso rende il modello conservativo (meno parole inventate). Usato da `/dialetto` (`_DIALETTO_TEMPERATURE = 0.5`) per tenere il catanese autentico.
  - Payload: solo `model` + `messages` (system+user) + `temperature` + `max_tokens`. **Nessun campo di moderazione** (requisito di design).
- Costante `AI_FALLBACK_MESSAGE = "I server sono a fuoco, riprova dopo."` — usata dagli handler su `AIServiceError`.

### fun_ai — handler

- **Solo gruppo** (`ChatType.GROUP/SUPERGROUP`): in privato il bot invita a usarli nel gruppo.
- Comandi **reply-based** (`/maestro` `/complotto` `/difendi` `/accusa` `/drama` `/dialetto`): operano sul testo del `reply_to_message`; helper `_run_ai_command` (accetta `temperature` per-comando opzionale). `/insulta` invece prende un target taggato (`@user`/reply).
- **Tono**: gruppo di **soli adulti** → satira nera, volgare, politicamente scorretta, senza buonismo né disclaimer. I prompt impongono **varietà anti-ripetizione** (mai riciclare aperture/battute/schema; ogni risposta diversa e fantasiosa, es. `/difendi` inventa ogni volta una strategia difensiva nuova) e **vietano i cliché da gamer** ('noob', 'scrub', 'git gud'…) come riempitivi — i riferimenti gaming solo se arguti.
- `/dialetto` traduce in **catanese stretto autentico** (non siciliano generico/macchiettistico): few-shot di lessico catanese + **regola anti-invenzione** (usa solo parole reali, in dubbio lascia l'italiano) + **temperatura abbassata** (`_DIALETTO_TEMPERATURE`) per ridurre le parole inesistenti — su Groq llama non c'è "ragionamento", l'autenticità si forza così.
- `/alduino` è **l'unico comando in cui il bot parla di sé**: una chat diretta col mascotte **Alduino** (draghetto viola, gamer). Prende il testo dopo il comando (fallback: il `reply_to_message`), che è il messaggio *dell'utente ad Alduino*. Usa un prompt **a sé** (`_PROMPT_ALDUINO`) che **NON** include `_STYLE`: carattere gentile/furbo/sarcastico/tenero **con ordine di priorità esplicito** e regola "**una risposta = un tono solo**" (niente satira nera né volgarità gratuita), **self-aware** (sa di chiamarsi Alduino: i riferimenti ad "Alduino" nel CONTENUTO sono a lui) e con **guardia anti-injection + cap** propri. Il prompt è tenuto **volutamente asciutto**: un tentativo precedente (eroe shonen + goffo + tormentoni + descrizione fisica) accumulava troppi tratti per i pochi caratteri di output → voce incoerente tra una risposta e l'altra, e i dettagli fisici spingevano il modello alla narrazione da roleplay (`*svolazza*`), cioè il cringe. Da qui i **divieti espliciti** (asterischi/azioni, presentarsi, esclamativi a raffica, sdolcinato, emoji in serie, tormentoni). I comandi roast restano invariati.
- **Cooldown anti-spam** (`_check_cooldown`): max 1 comando AI / `settings.ai_cooldown_seconds` per utente; **admin esenti** (via `is_admin`). Usa lo store condiviso `utils.cooldown` (bucket `"ai"`), quindi il pruning e la semantica in-memory sono quelli di ogni altro bucket — non c'è più una seconda implementazione di throttle nel repo.
  > **Non usa `cooldown.guard()`**, che marca mentre controlla. Qui check e mark sono due chiamate separate di proposito: l'handler controlla, *poi* valida (serve un reply-to, il bersaglio deve parsare), e solo `_dispatch` marca. Così un `/insulta` malformato non costa niente e si può riprovare subito, invece di bruciare 60s di cooldown per un errore di battitura. Fissato da `tests/unit/test_ai_cooldown.py`.
- `send_chat_action(chat_id, ChatAction.TYPING)` prima della generazione.
- **Anti prompt-injection & anti-HTML** (sicurezza):
  - L'input utente è **troncato** prima della chiamata (`clip_source`, 1500 char; target `/insulta` 64 char) e resta sempre nel ruolo `user`, **mai** concatenato al system prompt.
  - Il testo è **incapsulato** tra i delimitatori `<<<CONTENUTO>>> … <<<FINE CONTENUTO>>>`; `_STYLE` istruisce il modello a trattarlo come **contenuto inerte, mai istruzioni** (ignora cambi di ruolo / "ignora le istruzioni" / system prompt iniettati).
  - L'**output** del modello è inviato con **`parse_mode=None`** (`message.reply(result, parse_mode=None)`): mai renderizzato come HTML → niente injection di tag/`<a>` via output LLM.
- Ogni prompt è costruito con `_prompt(persona, max_chars)` che appende `_STYLE` condiviso:
  - regole "senza filtri / no disclaimer / no buonismo / no muri di testo" (pubblico adulto),
  - **regola anti-ripetizione** (varia sempre angolo, immagini, lessico),
  - **gaming come spezia** (riferimenti solo se arguti; vietati i cliché 'noob/scrub/git gud' come stampella),
  - **regola contenuto≠istruzioni** (anti-injection, vedi sopra),
  - cap di caratteri per comando + **tetto `max_tokens` hard** per-comando (rete di sicurezza anti wall-of-text).

### Regole

- Per cambiare modello: `GROQ_MODEL` in `.env` (zero codice). Modelli uncensored "veri" non esistono sul tier hosted Groq — il tono si pilota col *system prompt*.
- Nuovi comandi AI vanno aggiunti a `_GROUP_COMMANDS` (`main.py`) e alla sezione 🤖 di `/help` (`common.py`).

---

## 18. Suite Admin (valuta, utenti, moderazione)

Strumenti admin per gestire un gruppo numeroso. **UX doppia:** comandi testuali rapidi (azioni) +
**dashboard `/admin` a bottoni** (UI completa, §18.1). Ogni azione mutante è tracciata nell'**audit log**.

**File:** `services/admin_service.py` (DB), `services/moderation_service.py` (Telegram),
`handlers/_targeting.py` (risoluzione target), `handlers/admin.py` (comandi + renderer pubblici +
`apply_warning`), `handlers/admin_dashboard.py` + `keyboards/admin_dashboard_kb.py` (dashboard).

### admin_service (DB-side, no-commit — §5)

- **Valuta**: `set_balance` (delta → riusa `economy_service.credit/debit` con `admin_credit`/`admin_debit`), `mass_credit` (airdrop: bulk `UPDATE wallets` + 1 ledger per utente).
  > `set_balance` è **l'unica** operazione che prende ancora un lock esplicito, via
  > `economy_service.lock_balance`: un target assoluto ha bisogno del valore corrente, quindi
  > non è esprimibile come aritmetica relativa e il lock è ciò che tiene fermo quel valore
  > finché il delta non atterra (§22). Prima leggeva `wallet.coins` dall'entità e poteva
  > atterrare su `target ± quello che si era mosso nel frattempo`.
- **Dossier/stats**: `get_dossier`, `search_users` (ILIKE), `leaderboard`, `economy_stats`.
- **Warn**: `add_warning` (→ count attivi), `active_warnings`, `active_warning_count`, `clear_warnings` (soft-delete).
- **Ban bot-level**: `set_user_banned(session, tg_id, banned) -> bool` (no-commit) setta/azzera `User.is_banned`.
- **Audit**: `log_action(admin, action_type, target?, group?, amount?, detail?)` (solo `session.add`, no commit), `recent_actions`. `action_type` valuta: `credita/addebita/setsaldo/airdrop`; **XP**: `xp_grant/xp_set/xp_airdrop` (amount = XP, mostrati con suffisso `XP` in `render_audit`); moderazione: `ban/sban/kick/mute/unmute/warn/unwarn`.

### Ban bot-level (`User.is_banned` + `BannedUserMiddleware`, §6)

Distinto dal ban **Telegram** (`moderation_service.ban` = rimozione dal gruppo): `is_banned` rende
l'utente **muto-al-bot ovunque** (anche in privato) → `BannedUserMiddleware` scarta i suoi update **in
silenzio** (nessuna risposta). I **dati restano intatti** (solo il flag): `/sban` lo ripristina del
tutto. Settato da `/ban`, dall'**auto-ban a soglia warn** (`apply_warning`), dal **ban della
dashboard** e dalla **sincronizzazione con la moderazione nativa Telegram** (vedi sotto); azzerato da
`/sban` e dallo sban nativo. **`/kick`** (ban+unban per rientro) **non** lo setta. Ogni set/clear
chiama `ban_guard.invalidate(tg_id)` **dopo il commit**, così il nuovo stato vale dal primo update
successivo (la cache del middleware ha comunque un TTL di sicurezza).

> **Sync moderazione nativa** (`group_events.on_chat_member`): un ban/sban fatto dall'**UI di Telegram**
> (non con `/ban`) deve comunque rendere l'utente muto-al-bot → l'handler `chat_member` allinea
> `is_banned` quando lo status passa a/da `"kicked"`. Sono ignorate le transizioni **iniziate dal bot
> stesso** (`event.from_user.id == event.bot.id`): le gestiscono già i comandi, e un `/kick` passa per
> `"kicked"` solo transitoriamente.
>
> **Upsert difensivo**: `set_user_banned(tg_id, True)` su un utente **senza riga** crea uno stub bannato
> (nome/wallet ripopolati al primo contatto), altrimenti il primo update dell'utente creerebbe una riga
> *non* bannata e scavalcherebbe il middleware. Un clear su riga assente è un no-op.

> **Invariante**: il ban bot-level si applica sull'**intenzione** dell'admin, **indipendentemente** dal
> fatto che la rimozione Telegram (`moderation_service.ban`) riesca. Un fallimento della rimozione dal
> gruppo (utente già uscito, target admin, permessi mancanti) **non deve** lasciare l'utente libero di
> usare il bot in privato: `set_user_banned(True)` va chiamato comunque, segnalando l'eventuale
> mancata rimozione come avviso non bloccante. **Vietato** annidare `set_user_banned(True)` dentro un
> `if ok:` legato all'esito del ban Telegram.

### moderation_service (Telegram-side, no DB)

Wrapper su Bot API che ritornano **`(success: bool, reason: str)`** con errori mappati: `ban`,
`unban`, `kick` (ban+unban), `mute`, `unmute`.
`parse_duration("10m"/"1h"/"2d")` + `looks_like_duration` per il parsing della durata mute.

### handlers/admin.py

- Comandi `@router.message(Command(...), IsAdminFilter())`. Flusso mutante: risolvi target → azione → `admin_service.log_action(...)` → **un solo `commit`** nell'handler → notifica best-effort al target in privato.
- **Target**: `handlers/_targeting.resolve_target(message, session, args)` risolve in ordine reply → `text_mention` → `@username` → id numerico. Ritorna `tg_id`, `user` (riga DB o `None`), `display_name`, `remainder`. I comandi **valuta** richiedono `user is not None` (serve il wallet); la **moderazione** basta del `tg_id`.
- **Chat di moderazione**: `message.chat.id` se in gruppo, altrimenti `settings.group_id` (errore se 0).
- **Warn escalation** in `/warn`: a `warn_mute_threshold` → mute automatico; a `warn_ban_threshold` → ban automatico (entrambi loggati). La logica è estratta in `admin.apply_warning(bot, session, admin_id, target_id, chat_id, reason) -> (count, escalation_html)`, **condivisa** tra `/warn` e la dashboard (parità di comportamento + audit).

### 18.1 Dashboard `/admin` (namespace `adm:*`)

UI completa a bottoni in `handlers/admin_dashboard.py`: gli admin fanno **tutto senza digitare comandi**.

- **Entry**: `/admin` (redirect privato via deep-link `?start=admin`) → `show_dashboard_home`. Home:
  Statistiche · Classifica · **🎬 Eventi** · 👥 Utenti · 💰 Economia · 🧾 Audit · ❓ Comandi.
  (Quiz e Scommesse non sono più voci separate: confluiscono nell'hub **Eventi** — §18.2.)
- **Riuso, zero logica duplicata**: le viste riusano i renderer **pubblici** di `handlers/admin.py`
  (`render_stats`/`render_leaderboard`/`render_audit`/`render_panel_help`);
  scommesse → `admin_betting._show_event_list`. Le azioni passano dagli **stessi service + `log_action`** dei comandi.
  **Quiz/sondaggi/scommesse non sono più nella dashboard**: il bottone **🎬 Eventi** apre l'hub (`ev:home`, §18.2) —
  il vecchio hub quiz (`adm:quiz*`, `quiz_hub_kb`) e l'avvio con un tap sono stati **rimossi**.
- **Azioni su utente** (`👥 Utenti`, lista paginata + 🔍 ricerca → `adm:user:<tg>`): credita/addebita/set saldo,
  **⚡ Dai XP / Set XP** (via `xp_service` + audit `xp_grant`/`xp_set`), ban/kick/sban, mute/unmute, warn/unwarn.
  Input (importo/XP/durata/motivo) via FSM `AdminPanelStates`; ban/kick passano da una conferma (`adm:ask:…` → `adm:do:…`).
- **Economia**: `💰 Economia` → `🎁 Airdrop monete` (`adm:airdrop`) e **`⚡ Airdrop XP`** (`adm:xpairdrop`, `xp_service.airdrop_xp` + audit `xp_airdrop`).
- **Classifica**: `adm:lead` con switcher `adm:lead:<coins|xp|trofei>` (riusa `handlers.leaderboard.render_board` + `lead_kb`).
- **Gating**: ogni callback `adm:*` con `IsAdminCallbackFilter` + **catch-all deny** `adm:` in fondo al router;
  azioni di moderazione disattivate se `group_id == 0`; guard self/target. `admin_dashboard.router` incluso
  dopo `admin.router` in `main.py`.
- **Grammatica callback** (≤ 64 byte): `adm:home|stats|lead|audit|help|close`, `adm:lead:<board>`,
  `adm:bets`, `adm:econ|airdrop|xpairdrop|search`, `adm:users:<page>`, `adm:user:<tg>`, `adm:act:<credit|debit|setbal|xpgrant|xpset|mute|warn>:<tg>`,
  `adm:ask:<ban|kick>:<tg>`, `adm:do:<…>:<tg>`.
- Il vecchio pannello read-only `admin_panel:*` + `keyboards/admin_panel_kb.py` è **rimosso** (assorbito dalla dashboard).

### 18.2 Hub Eventi (macro-categoria, namespace `ev:*`)

`handlers/events.py` (router incluso dopo `admin_dashboard`, prima di `quiz`). Unifica **quiz ·
sondaggi · scommesse** (e ogni tipo futuro) sotto un modello unico: ogni evento si **pre-crea**, poi
si **avvia subito** nel gruppo *oppure* si **programma** — come già facevano i quiz. Entry:
`/admin → 🎬 Eventi`, `/eventi`, o deep-link `?start=eventi`.

- **Registro tipi-evento** (`handlers/event_types/`): **unico punto d'estensione**. Ogni tipo è una
  spec `EventType` (`key`, `hub_label`, `create_label`, `render_list`, `schedulable_items`,
  `start_creation`, `start_now`, `execute_scheduled`, `close_now`) registrata in `register_builtin()`
  (chiamata in `main()`). **L'hub e lo scheduler dispatchano *solo* attraverso il registro** — niente
  `if/elif` per tipo. Aggiungere un tipo = una nuova spec + una riga in `register_builtin`, **zero**
  modifiche a `events.py`/`schedule.py`. Le spec **non committano mai** (§5): committa il chiamante
  (callback su `start_now`/`close_now` ok; `scheduler_loop` su `execute_scheduled`).
- **Grammatica** `ev:*` (≤64B): `ev:home`, `ev:list:<type>`, `ev:item:<type>:<id>` (**schermata info**,
  vedi sotto), `ev:start:<type>:<id>`, `ev:close:<type>:<id>`, `ev:del:<type>:<id>`, `ev:reset:<type>:<id>`,
  `ev:sched:<type>:<id>`, `ev:new:<type>`, gli step di conferma `ev:ask{start|close|del|reset}:<type>:<id>`,
  e `ev:pt:cancel[_yes|_no]`. Tutti gli handler sono **generici** (un `<type>` qualsiasi presente nel
  registro), tranne la FSM di creazione sondaggio (`ev:pt:*`) che resta in `events.py` con il suo gate
  admin di router (§8).
- **Schermata info + conferme (no avvio accidentale)**: cliccando un item (`ev:item`) si apre la sua
  **scheda info** — non lo si avvia. Ogni azione impattante (avvia · chiudi · elimina · riproponi) passa
  da uno step di conferma `ev:ask*` (Sì→esecutore, No→`ev:item`). La scheda è fornita dal tipo con i
  metodi **opzionali** `render_detail`/`delete`/`reset` (l'hub li rileva via `getattr` e per i tipi che
  non li implementano ricade sulla vecchia schermata «Avvia ora / Programma» + `ev:start`). Restano fuori
  dal contratto `EventType` per non rompere `isinstance(et, EventType)`.
- **Modello "pre-creato"**: quiz già `status=ready`; **sondaggi** → nuovo `PollTemplate`
  (`poll_service`, status `ready|used`); **scommesse** → nuovo stato `EventStatus.draft` (la creazione
  community via `/crea_scommessa` resta `open`; l'hub crea `draft` con `start_bet_creation(as_draft=True)`
  e `bet_service.activate_event` fa `draft→open`). `get_open_events`/`get_all_active_events` escludono i draft.
- **Quiz persistenti**: l'hub quiz elenca via `quiz_service.list_manageable` **tutti** i quiz non-`draft`
  (running → ready → **finished** come archivio, cap sugli ultimi N) — un quiz avviato/concluso **non
  scompare** più. Dalla scheda info: `delete_quiz` (elimina quiz+domande+risposte e **annulla** i task
  schedulati pendenti; lascia intatti `game_podiums`/`user_metrics`) e `reset_quiz` («Riproponi»: azzera
  risposte/timestamp e riporta `finished→ready` per rigiocarlo). `list_ready` (ready+running) resta per
  lo scheduling (`schedulable_items`).
- **Avvia ora / Chiudi** (`spec.start_now`/`close_now`): quiz→`open_quiz`/`close_quiz`;
  poll→`send_poll` + `mark_used`; bet→`activate_event` + annuncio gruppo (`close_now` → `None`: nessuna
  chiusura). L'annuncio scommessa è centralizzato in `BetType._announce_open` (un'unica fonte per
  avvia-ora **e** scheduler).
- **Finestra scommesse (auto-chiusura)**: la durata si sceglie **alla creazione** (step
  `BetCreationStates.waiting_for_window`, preset/custom/♾️ illimitata → `betting_window_seconds`,
  `NULL/0` = illimitata). All'apertura (`activate_event`/`create_event` open) `bet_service.arm_close`
  arma `closes_at = utcnow()+window`; `bet_service.schedule_close` programma l'auto-lock riusando
  **`task_type="bet"` con payload `{"action":"lock"}`** (nessun nuovo tipo). A scadenza
  `BetType.execute_scheduled` fa `lock_event` + annuncio «⏰ Scommesse chiuse» (o `TaskSkip` se già
  chiusa). `place_bet` rifiuta comunque oltre `closes_at` (guardia difensiva sul tick ~20s). Chiudendo/
  risolvendo/annullando a mano, i tre confirm-handler di `admin_betting` chiamano
  `bet_service.cancel_pending_close` per non lasciare il task orfano. La lista `/scommesse` (privata **e**
  gruppo) marca «✅» le scommesse già giocate dall'utente (`get_user_placed_event_ids`).
- **Programma**: `handlers.schedule.start_schedule_for(type, ref_id, label)` → stato unico
  `ScheduleStates.event_runat` → `schedule_task(type, ref_id)`. `execute_task` delega a
  `spec.execute_scheduled`, che gestisce **`ref_id`** (nuovo: carica PollTemplate / attiva draft) con
  **fallback al `payload`** legacy (task già schedulati).
- **`/programma`** ora fa scegliere **elementi già creati** per tutti e 3 i tipi (niente più creazione
  inline). Conferma «Sicuro di voler annullare?» (§ cancel) prima di scartare un flusso a metà.

### Regole

- Il namespace `adm:*` (dashboard) non collide con `admin_bet:*` né con `ev:*` né con gli altri → ordine router indifferente, ma `admin_dashboard.router` va dopo `admin.router` e comunque prima di `common.router`.
- Tutte le azioni che modificano valuta/moderazione **devono** chiamare `log_action` prima del commit (vale per comandi **e** dashboard).
- I comandi admin **non** vanno nelle command list pubbliche (`_PRIVATE/_GROUP_COMMANDS`), ma vanno documentati nella sezione admin di `/help`.

---

## 19. Quiz mode (privato, con podio)

Quiz a risposta multipla creati dall'admin e giocati da ogni utente nella **propria chat privata**
(NO poll di gruppo: in un poll di gruppo si "risponde per tutti" / non si avanza bene). Limite di
tempo per domanda **opzionale** (scelto in creazione); vince chi ne azzecca di più, **a parità
conta il tempo minore** (poi l'ordine di arrivo).

**File:** `services/quiz_service.py` (DB), `handlers/quiz.py` (FSM + comandi + play privato).

### Creazione

FSM admin in privato (redirect dal gruppo con deep-link `create_quiz`, oppure dall'hub Eventi
`ev:new:quiz` che passa `creator_id` esplicito perché lì `message.from_user` è il bot): titolo →
descrizione → **premi** → loop domande {testo → opzioni (una per riga, 2–10) → opzione corretta
(inline) → spiegazione opzionale} → **riepilogo** {➕ Aggiungi · 🗑 Rimuovi ultima · ✅ Pubblica}.
**Nessun timer.** A fine: quiz `ready`.

- **Premi**: schermata `prize_mode` con ⚡ Consigliati (default da settings) · ✏️ Personalizza · 🚫 Nessuno.
  In personalizzato si impostano 1°/2°/3° e la **consolazione (4°)**; il `prize_min` è **derivato**
  (`quiz_service.participation_floor`). Il quiz viene creato (`create_quiz`) a fine flusso premi.
- **Navigazione**: ogni step ha «⬅️ Indietro» (`quiz_new:back`, dispatch via `_BACK_PROMPTERS` per stato);
  «⬅️ Riepilogo» quando si aggiungono altre domande. «🗑 Rimuovi ultima» → `quiz_service.delete_last_question`.
- **Hardening**: handler di input gated `IsAdminFilter()`/`IsAdminCallbackFilter()`.
- **Limiti di lunghezza**: costanti in cima a `handlers/quiz.py` — `_MAX_TITLE` (256), `_MAX_DESC`
  (1024), `_MAX_QUESTION` (300), `_MAX_OPTION` (100), `_MAX_EXPLANATION` (200). **Unica fonte di
  verità**: i prompt le interpolano e i validatori le applicano, così il limite annunciato non può
  divergere da quello imposto. L'input oltre il limite viene **rifiutato** (`_too_long` →
  `"<len>/<cap>"` + di quanto accorciare; `_options_error` per conteggio + lunghezza per-opzione,
  indica *quale* opzione sfora), **mai troncato in silenzio**: un testo tagliato si scopre a quiz
  già pubblicato. Vale sia in creazione sia in modifica (`QuizEditStates`). I `[:N]` rimasti in
  `quiz_service` sono solo la **rete di sicurezza** allineata alle colonne DB.

### Prova admin (dry-run, §19.b)

Un admin può **giocare un quiz `ready`** — dopo «✅ Pubblica», prima di avviarlo — per verificare
testi, opzioni e spiegazioni sul campo. Ingressi: bottone «🧪 Prova il quiz» nel messaggio «Quiz
pronto!» e «🧪 Prova» nel dettaglio dell'hub Eventi.

**Invariante:** la prova è **interamente in memoria** (`_TRY: dict[(quiz_id, admin_id), _TryCtx]`) e
**non scrive nessuna riga `quiz_answers`**. Quindi non può raggiungere podio, premi, XP o
`game_podiums`: l'isolamento è **strutturale**, non un filtro da ricordarsi in ogni query (era
l'alternativa scartata: colonna `is_test` + filtro in ~6 punti). Namespace callback **`quiz_try:*`**,
disgiunto da `quiz_ans:*`, così una risposta di prova non può finire nel recorder vero. Handler
gated **singolarmente** (`quiz.router` è misto, §8). Nessun timer in prova (il vero limite è
comunicato a schermo). Ogni messaggio porta il marker 🧪 e il riepilogo finale dichiara
esplicitamente che nulla è stato salvato.

> **Identità dell'attore — `admin_id` esplicito.** `start_quiz_try` riceve `admin_id` come
> parametro **obbligatorio, senza default**, e **non** lo deriva mai da `message.from_user`: il
> bottone «🧪 Prova» vive sempre su un messaggio inviato dal *bot*, quindi lì `from_user` **è il
> bot** (stessa trappola di `ev:new:quiz` col `creator_id`). Un bug reale: la prova finiva in `_TRY`
> sotto l'id del bot mentre `cb_try_answer`/`cb_try_stop` la cercavano sotto quello dell'admin →
> ogni risposta rifiutata con «Prova scaduta» e la voce orfana mai ripulita. **Regola: nei flussi
> avviati da callback, l'identità viene solo da `callback.from_user`**, propagata esplicitamente.
> Corollario per i test: esercitare i **veri entry point** (`cb_try_*`) con un messaggio autored
> dal bot — un fake message intestato all'admin è una forma che in produzione non esiste e maschera
> proprio questa classe di bug (`tests/integration/test_quiz_try.py`).

### Avvio & gioco

- `open_quiz(bot, session, quiz_id)`: annuncia nel gruppo (bottone deep-link `quiz_<id>`) **poi**
  mette il quiz `running` (se l'annuncio fallisce resta `ready`). Usato da `/avvia_quiz`, dall'hub Eventi
  (`ev:start:quiz`, con conferma `ev:askstart`) e dallo scheduler. Caller committa. **`/quiz` (admin)**
  non avvia più con un tap: mostra la lista gestione dell'hub (`QuizType.render_list`).
- Ogni utente apre `?start=quiz_<id>` → `start_quiz_session`: gioca in privato, una domanda alla
  volta con **bottoni inline** (`quiz_ans:<quiz>:<question>:<opt>`). Alla risposta: feedback
  immediato (✅/❌ + spiegazione), poi domanda successiva. È **resumable** (riprende dalla domanda
  non ancora risposta). `record_answer` è idempotente per (domanda, utente) — dedup + `IntegrityError` guard.

### Podio & premi

- `podium(quiz_id)`: solo i **finisher** (hanno risposto a tutte le domande), ordinati per
  **corrette DESC, poi tempo di completamento ASC** (più veloce avanti), con il `finished_at`
  (ordine di arrivo) solo come ultimo spareggio. Il `completion_seconds`/`completion_ms` è la
  **somma dei tempi-risposta per domanda** del singolo utente (include la 1ª domanda, vale anche
  con **una sola** risposta; `None` solo senza risposte), **non** il tempo dall'avvio admin
  (`Quiz.started_at`). `user_completion_seconds(session, quiz_id, uid)` per il messaggio di fine partita.
- `award_prizes(quiz_id)` — due modalità (premi **mintati** via `economy_service.credit` `quiz_reward`,
  niente prelievo da un pot):
  - **Esplicita** (se almeno uno tra `prize_first/second/third/consolation` > 0): podio 1°/2°/3° →
    importi espliciti; dal 4° in giù **consolazione a scendere** `consolation_amounts(n, top=prize_consolation,
    floor=prize_min)` — funzione **pura**: scala lineare da `top` (4°) a `floor` (ultimo), non crescente,
    tutti ≥ floor. Solo finisher.
  - **Legacy** (altrimenti, se `prize_coins` > 0): pool diviso top-3 `_PRIZE_SPLIT` 0.5/0.3/0.2 (resto al 1°) —
    comportamento **invariato** per i quiz vecchi.
  - XP (`_grant_xp`, evento uncapped): `quiz_xp_participation` a chiunque abbia ≥1 risposta,
    `+quiz_xp_per_correct` per corretta, `+quiz_xp_podium_first/second/third` ai primi 3.
- `claim_close(quiz_id) -> str | None`: porta un quiz da `running` a `finished` in **una `UPDATE`
  condizionale** e dice se è stata *questa* chiamata a farlo (`None`) o cosa l'ha bloccata (lo stato
  corrente, oppure `QUIZ_MISSING`). Al massimo un chiamante può ricevere `None`: è quello che rende
  sicuro pagare i premi subito dopo. **La transizione è la guardia** — controllare lo stato e
  ribaltarlo dopo sarebbe un read-then-write, e il quiz è spesso già in cache (§22).
- `close_quiz(bot, session, quiz_id) -> (ok, msg)`: helper condiviso da `/chiudi_quiz` **e** dall'hub Eventi
  (`ev:close:quiz`, con conferma `ev:askclose`) → `claim_close` → `award_prizes` → annuncio podio (🎖️ per le
  consolazioni). Un quiz `finished` resta gestibile nell'hub: `ev:reset:quiz` («Riproponi») lo riporta a
  `ready`, `ev:del:quiz` lo elimina.
- `format_prize_summary(quiz)` riassume i premi nelle schede/annunci.

### Regole

- Stati quiz: `draft → ready → running → finished`.
- Play in **privato** (i poll di gruppo non sono usati per i quiz; `send_poll` serve solo per avviare/programmare un sondaggio, mai per i quiz).
- Service no-commit (§5): commit negli handler. `open_quiz` annuncia prima di flippare lo stato.

---

## 20. Scheduling (quiz / sondaggio / scommessa)

Telegram Bot API **non** permette di schedulare poll → scheduler in-process DB-backed.

**File:** `services/schedule_service.py` (DB + `parse_run_at`), `handlers/schedule.py`
(comandi + FSM + `scheduler_loop`/`execute_task`).

- `scheduler_loop(bot)` avviato in `main()` con `asyncio.create_task` prima di `start_polling`; ogni
  `scheduler_poll_interval`s esegue i `due_tasks` (try/except per task → `mark_done`/`mark_failed`).
  > **Sta in `handlers/` di proposito, non per sbaglio.** Sembra un daemon collocato male, ma
  > `execute_task` dispatcha sul registro `handlers/event_types`, e quegli spec **importano
  > funzioni degli handler** (`handlers.quiz.open_quiz`, `handlers.betting.start_bet_creation`, …)
  > e costruiscono tastiere inline: sono presentazione. Spostare il loop in `services/` farebbe
  > importare `handlers` da `services`, cioè un'inversione di layering vera in cambio di uno
  > smell estetico. Non farlo senza prima spostare `event_types`.
  >
  > **L'isolamento per-task sta tutto in `_run_due_task` che non solleva mai**, non nel loop: il
  > `for` è dentro il `try` del tick, quindi un raise lì dentro abbandona i task ancora dovuti
  > fino al giro successivo. Coperto da `tests/unit/test_scheduler_loop.py` +
  > `tests/integration/test_scheduler_failure_path.py` (14 test, garanzie verificate per
  > mutazione).
  >
  > ⚠️ **Rischio latente, misurato.** Dopo un `rollback` che ha lavoro da annullare, leggere
  > *qualsiasi* attributo dell'istanza ORM dà `MissingGreenlet` — e `_run_due_task` passa quella
  > stessa istanza a `_notify_creator`, che ne legge due. Oggi funziona **solo** perché fra il
  > rollback e la notifica ci sono `mark_failed` (che assegna soltanto: un'assegnazione non
  > carica) e `await session.commit()`, il cui flush ricarica la riga in contesto greenlet.
  > Quindi: **non spostare la notifica prima del commit** e non togliere quel commit dal path di
  > errore. Provato per mutazione — il test integration lo prende.
- `parse_run_at(text)`: assoluto `AAAA-MM-GG HH:MM` o relativo `30m`/`2h`/`1d` → **UTC naive**
  (timezone `scheduler_timezone`). Rifiuta orari passati.
- `execute_task` non ramifica per tipo: valida il `group_id`, poi **delega a
  `event_types.get(task.task_type).execute_scheduled(...)`** (tipo ignoto → `RuntimeError`). Le spec:
  `bet` → `activate_event` (o `create_event` da payload legacy) + annuncio gruppo, **oppure** auto-lock
  se `payload.action == "lock"` (chiude la finestra puntate → `locked`); `quiz` →
  `open_quiz` (annuncia + apre); `poll` → `bot.send_poll` nel gruppo. Le spec **non committano** (il
  `scheduler_loop` committa dopo `mark_done`/`mark_failed`).
- `parse_duration(text)` (30m/2h/1d → secondi): **durata** relativa (non un istante), usata dallo step
  finestra puntate. **Cappata a 365 giorni** insieme a `parse_run_at`, tramite l'unico helper
  condiviso `_rel_seconds` — entrambe alimentano aritmetica che va in overflow su input assurdo
  (`parse_run_at` → `datetime + timedelta` = `OverflowError`, che nessun handler intercetta;
  `parse_duration` → `betting_window_seconds`, colonna int32 = «integer out of range» su Postgres,
  **dopo** che l'utente si è già sentito dire che la scommessa era creata). Il cap sta nell'helper
  e non nelle due funzioni proprio perché nessuna delle due possa dimenticarlo.
  La chiusura automatica di una scommessa è un `ScheduledTask` `bet` con
  `payload.action="lock"` armato all'apertura (§18.2) — stesso registry, nessun task-type nuovo.
- Comandi: `/programma` (scegli un evento già creato → orario run-at), `/programmati` (lista + annulla),
  `/sondaggio` (**crea** un sondaggio salvato, poi «Avvia ora / Programma» — come quiz/scommesse, mai
  pubblicato all'istante; **solo in privato**: nel gruppo manda il deep-link `create_poll`, §9;
  riusa `events.start_poll_creation`). Gating a **livello di router** (§8):
  `schedule.router` monta `IsAdminFilter`/`IsAdminCallbackFilter` alla radice, così ogni handler
  (anche quelli guidati solo dallo stato FSM) richiede l'admin.
- I `ScheduledTask` sono **persistiti** → sopravvivono al restart.

---

## 21. Docker & Compose

`Dockerfile` (src-layout): `COPY src/ ./src/` → `CMD ["python", "src/main.py"]` (mette `/app/src` sul
path). Utente non-root `botuser`; `/app/data` creata per il fallback SQLite. `.dockerignore` tiene fuori
`.env`, `.venv`, `tests/`, cache, docs.

```yaml
services:
  db:    postgres:16-alpine  (healthcheck pg_isready)
  redis: redis:7-alpine      (healthcheck ping)
  bot:   build . + image ${BOT_IMAGE:-gaming-community-bot:local} + env_file .env
networks: { bot_network: bridge }   # ← dichiarazioni top-level OBBLIGATORIE
volumes:  { postgres_data }         #   (sono referenziate dai service)
```

- `image:` sul service `bot` usa `${BOT_IMAGE}` → in produzione punta all'immagine GHCR pubblicata da CI
  (`docker compose pull`); in dev resta `build: .`. Tag: `:latest` (HEAD main) o `:1.2.3` (release
  immutabile) in prod, `:latest-test` (HEAD test) in staging — vedi §23 (CI/CD a due branch).
- `networks`/`volumes` **devono** essere dichiarati a top-level: i service li referenziano, senza
  dichiarazione `docker compose config` fallisce.

La `.env` deve contenere almeno:
```
BOT_TOKEN=...
DB_URL=postgresql+asyncpg://user:password@db/gamingbot
GROUP_ID=-100xxxxxxxx
ADMIN_IDS=123456789,987654321
DAILY_REWARD_COINS=100
FSM_STORAGE=redis
REDIS_URL=redis://redis:6379/0
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx   # opzionale: senza chiave i comandi AI rispondono col fallback
# Backup & export (§25) — tutto opzionale. Senza i 3 TELEGRAM_* l'archivio chat
# è disattivato (lo state export funziona comunque, non serve Telegram).
# TELEGRAM_API_ID=1234567               # da my.telegram.org
# TELEGRAM_API_HASH=...
# TELEGRAM_SESSION=...                  # StringSession da scripts/login_telethon.py (SENSIBILE)
```

Il volume `./backups:/app/backups` (compose) persiste gli artefatti tra i restart;
`backups/` e `*.session` sono in `.gitignore`/`.dockerignore`.

---

## 22. Regole di sviluppo

1. **Non usare `session` come chiave di injection** — sempre `db_session`
2. **Non usare `get_settings()`** — sempre `from config_data.config import settings`
3. **Non aggiungere `pg_insert`** — usare select + conditional add (cross-DB)
4. **I service non committano** — il commit è del handler (vale anche per `shop_service` e `xp_service`)
5. **`from __future__ import annotations`** in tutti i file che usano `X | Y` type union
6. **`MessageEntityType`** viene da `aiogram.enums`, non da `aiogram.types`
7. **Shop = solo cosmetici**: nuovi tag si aggiungono nel CSV `data/shop_cosmetics.csv` (catalog_loader), **non** in codice; nessun item può conferire permessi Telegram reali (anti-escalation)
8. **`admin_betting.router` deve stare prima di `betting.router`** nel `main.py`
9. **`common.router` deve stare per ultimo**
10. **Non aggiungere colonne a `User` senza aggiornare `badge_service.check_and_award_milestones`**
11. **Comandi admin** non vanno in `_PRIVATE_COMMANDS`/`_GROUP_COMMANDS` — solo nella sezione admin di `/help`
12. **Chiamate LLM** solo via `aiohttp` async attraverso `ai_service`; **nessun** campo di moderazione nel payload Groq
13. **Azioni admin mutanti** (valuta/moderazione) → sempre `admin_service.log_action` prima del commit
14. **Service no-commit** vale anche per `admin_service`/`quiz_service`/`schedule_service`; `moderation_service` non tocca il DB (solo Bot API)
15. **Check admin inline** sempre via `filters.admin_filter.is_admin` (mai `user.id in settings.admin_ids` diretto) — include gli admin Telegram del gruppo
16. **Comandi AI** passano sempre dal cooldown (`_check_cooldown`); admin esenti
17. **Timestamp scheduler** in UTC naive (`schedule_service.utcnow`); orari schedulati via `parse_run_at`
18. **`User.xp` si muta solo via `xp_service`** (mai `user.xp += …` negli handler/altri service); le sorgenti `capped` rispettano il tetto giornaliero. Assegnazione/airdrop/set XP **solo admin** + `log_action`
19. **Cataloghi CSV** (`catalog_loader`) letti solo all'avvio, con **fallback ai default**: non assumere mai che un file esista; valida e salta le righe malformate
20. **Escaping HTML obbligatorio**: ogni stringa **user-controlled** interpolata in un messaggio `ParseMode.HTML` passa da **`utils.text.esc`** (full_name, username, cosmetic_tag, titoli/descrizioni/opzioni scommesse, testi/risposte quiz, motivi warn, audit detail, query di ricerca, ecc.). I service e il DB restano **raw** — l'escaping è solo presentation layer. Testi dei **bottoni inline** e domande/opzioni dei **poll** non sono HTML-parsed → niente `esc`.
21. **Mai `settings.group_id` a runtime**: usare **`group_registry.get_group_id()`** (id effettivo, §13). Solo `config.py`, lo startup in `main()` e `group_registry` stesso toccano il setting.
22. **Le mutazioni di denaro/XP/stato non si decidono in Python: si decidono in SQL.** Il check va nella `WHERE`, l'aritmetica nella `SET`, e `rowcount == 0` significa "gara persa". `economy_service.credit/debit` richiedono **`amount > 0`** (eccezione `ValueError`).
    > ⚠️ **Un lock NON basta, ed è misurato.** `with_for_update` prende un lock vero su Postgres, ma se la riga è già nella identity map SQLAlchemy restituisce **l'istanza in cache con i valori vecchi** (`expire_on_commit=False`): il lock protegge una riga, non il numero su cui stai decidendo. Il check-then-write passa due volte. Erano 9 gare rosse in `tests/integration/test_money_concurrency_pg.py`; oggi sono **verdi e sono le guardie di regressione** — girano solo con `TEST_PG_URL`, e su SQLite una gara a due sessioni non è nemmeno esprimibile.
    >
    > Cosa rende una riga "già in cache": **un chiamante che tiene l'entità in una variabile** attraverso la chiamata al servizio. La identity map usa riferimenti **deboli**, quindi `DbSessionMiddleware._upsert_user` **non** avvelena la sessione (carica e scarta → garbage collected), e leggere un valore scartando l'oggetto è sicuro. Il pattern pericoloso è quello di `bet_type._auto_lock`: `event = await get_event_detail(...)` → controlla `event.status` → `lock_event(...)` col riferimento ancora vivo.
    >
    > **Le tre regole operative:**
    > 1. **Leggi le colonne, non le entità.** `select(Wallet.coins)` non può essere servita dalla cache, `select(Wallet)` sì. Vedi `economy_service._balance`.
    > 2. **Scrivi in SQL, relativo dove puoi.** `coins = coins + :delta` somma; `wallet.coins += x` sovrascrive con un valore calcolato da una base che potrebbe essere vecchia. Per le transizioni di stato, mettile nella `WHERE`: `claim_close` (quiz), `lock_event`/`resolve_event` (bet), `claim_daily`.
    > 3. **`.execution_options(synchronize_session=False)` sempre**, poi `await session.refresh(obj, [colonne])`. Il default riscrive in cache un valore derivato dalla copia stale (misurato: cache=100, DB=500, `coins - 10` lascia DB=490 e cache=90). Il refresh mantiene il contratto per cui, dopo la chiamata, l'entità in sessione riflette la scrittura — **è usato davvero**, non è cosmetico.
    >
    > **Non aggiungere `populate_existing=True`**: invalida le relazioni già caricate sull'istanza e in async diventa `MissingGreenlet` (provato e ritirato). Mai `session.expire` (stesso motivo).
    >
    > **Attenzione al `refresh` su un attributo con una modifica pendente**: la butta via (`autoflush=False`, quindi la modifica non è ancora andata al DB). Preso da un test unitario esistente su `grant_xp`. Se scrivi in SQL invece di mutare l'istanza il problema non esiste — non resta mai niente di pendente.
    >
    > **I lock restano dove servono davvero**, cioè dove SQL da solo non basta: (a) `transfer` locka i due wallet in ordine di **`tg_id` crescente**, perché due righe toccate insieme possono andare in deadlock; (b) `lock_balance` per l'unica operazione che ha bisogno del valore corrente (`admin_service.set_balance`: un target assoluto non è aritmetica relativa); (c) il ramo `capped` di `grant_xp`, perché un cap è un min/max contro un valore memorizzato e non ha una scrittura portabile fra Postgres e SQLite. Ordine di lock canonico **Event → User → Wallet**; il ramo *uncapped* di `grant_xp` non prende lock **apposta** (invertirebbe l'ordine su cui si appoggia `resolve_event`), per questo la sua aritmetica deve essere sicura da sola.
    >
    > **Ultimi due siti della stessa forma, chiusi.** `xp_service.airdrop_xp` era il read-modify-write più largo del bot — leggeva tutti gli utenti e riscriveva totali assoluti, quindi ogni XP concesso nel frattempo (un quiz che chiude, una scommessa che si risolve) spariva senza traccia; ora è **un solo `UPDATE` relativo**, col tier ricalcolato da una select **per colonna** e raggruppato per tier di destinazione (una manciata di statement, non uno per utente). `quiz_service.reset_quiz` leggeva lo stato da una **entità** sotto `FOR UPDATE` e ora lo legge come **colonna** sotto lock; le sue scritture restano mutazioni ORM **apposta**, perché assegnano solo costanti (mai delta) e sono quindi corrette anche su un'istanza stale — è ciò che permette a `cb_reset` di ri-renderizzare il dettaglio senza ricaricare. Di conseguenza **`get_quiz` non ha più `for_update`**: era senza chiamanti, e lasciarlo disponibile invitava a rifare esattamente questo errore.
    >
    > **Queste due si misurano senza Postgres.** Una scrittura SQL emessa nella stessa sessione è invisibile alla identity map **esattamente quanto** il commit di un'altra transazione: `update(...).values(xp=User.xp + 500).execution_options(synchronize_session=False)` rende l'istanza in memoria bugiarda, e la gara si riproduce con una sessione sola. Vale per ogni difetto la cui causa è la **cache stale** (non per i lost update veri, che restano appannaggio di `TEST_PG_URL`). Utile quando Docker non c'è.
23. **Moderazione**: ogni azione (comando o dashboard) passa dal guard **self/bot-target** (`admin._guard_mod_target` / `admin_dashboard._mod_guard`, basato su `message.bot.id`, niente `get_me()`).
24. **Backup/export** (§25): tutto in **streaming** (mai un dataset intero in RAM — il bot è cappato a 300 MB), scritture **atomiche** (`utils.atomic_io`: tmp+fsync+replace; archivio chat = membri gzip concatenati con manifest + recovery-truncate). Il `backup_loop` e i comandi non devono **mai** bloccare l'event loop né far crashare il bot (loop in `try/except` totale). L'archivio chat è **opt-in** (creds Telethon assenti ⇒ disattivo); la cronologia si legge **solo** via MTProto/Telethon (la Bot API non può). La `TELEGRAM_SESSION` è una credenziale sensibile: solo `.env`, mai committata.
25. **Nuovi tipi-evento solo via registro** (`handlers/event_types`, §18.2): si implementa una spec `EventType` e la si registra in `register_builtin()`. **Vietato** ramificare per tipo in `cb_start_now`/`cb_close`/`cb_type`/`execute_task` o reintrodurre dict tipo→handler (`_TYPE_LABEL`/`_RENDER`). Le spec **non committano** (§5): committa il chiamante.
26. **Trofei & Locanda** (§11/§12): nuove **condizioni trofeo** si aggiungono al **dispatch** di `check_and_award_milestones` + a `TROPHY_CONDITIONS` + a `describe_condition` (mai catene `if/elif` fuori dagli helper). Le condizioni scoped usano **`Badge.condition_param`** (key item/categoria/gioco o slug `;`-separati per `collection`); colonna nuova ⇒ voce in `_MIGRATIONS`. Le **`collection`** si risolvono a **punto fisso** (sblocco a catena nello stesso commit). **Chiavi disgiunte**: consumabili `cons_*`, cosmetici `tag_*` (namespace `shop_purchases.item_key` condiviso). Acquisto consumabile: **flush prima** del milestone check (autoflush off). Cataloghi (consumabili/categorie/trofei) **solo via CSV + default Python**, mai hardcode negli handler. Nuovi giochi col podio chiamano `progress_service.record_podium(game_key, rank)` — i loro trofei `podium_count`/`first_place_count` si attivano da soli.

---

## 23. Test suite

### Struttura

```
tests/
├── conftest.py               # Engine in-memory SQLite + fixture condivise
├── unit/
│   ├── test_config.py        # Settings parsing (admin_ids validator)
│   ├── test_exceptions.py    # Eccezioni custom (attributi + messaggi)
│   ├── test_keyboards.py     # Keyboard builder (testi, callback data, URL)
│   ├── test_rate_limit.py    # RateLimitMiddleware (finestre, isolamento utenti)
│   ├── test_payout.py        # compute_payout_preview (funzione pura)
│   ├── test_xp_service.py    # rank_for_xp + tetto giornaliero + set/airdrop (funzioni pure/async)
│   ├── test_catalog_loader.py # parse CSV trofei/ranghi/cosmetici/consumabili/categorie + condition_param + fallback + righe malformate
│   ├── test_locanda_catalog.py # helper consumabili (sync) + describe_condition (item/category/podio/collection)
│   ├── test_group_guard.py   # invalidate_cache, _chat_type, _NON_MEMBER_STATUSES
│   ├── test_ai_service.py    # Groq client (aioresponses): success/timeout/http/malformed/no-key
│   ├── test_moderation_service.py # parse_duration + mappatura errori (Bot fake)
│   ├── test_admin_filter.py  # is_admin (admin_ids, cache TG mockata, fail-closed)
│   ├── test_ai_cooldown.py   # _check_cooldown (esenzione admin, finestra, check non consuma)
│   ├── test_schedule_parse.py # parse_run_at + parse_duration (assoluto/relativo/passato/invalid/cap 365gg)
│   ├── test_error_handler.py  # dp.errors: log con contesto, alert callback, rumore benigno silenziato
│   ├── test_quiz_prizes.py   # consolation_amounts / participation_floor (funzioni pure)
│   ├── test_keyboards.py     # keyboard builder (incl. shop cosmetici: affordable/owned/callback)
│   ├── test_text_utils.py    # utils.text.esc (escaping HTML, None, troncatura) + chunk_blocks (split ≤4096)
│   ├── test_fun_ai_hardening.py # clip_source / output parse_mode=None + wrapper CONTENUTO
│   ├── test_atomic_io.py       # scrittura atomica, sha256, troncatura, append membri gzip + rollback
│   ├── test_backup_loop.py     # due-ness, pre-flight non scrivibile, il loop sopravvive a un tick rotto
│   ├── test_scheduler_loop.py  # _run_due_task (rollback prima di mark_failed, skip≠errore), il loop non muore
│   ├── test_router_order.py    # ROUTERS: ogni modulo con un router è registrato, admin_betting<betting, common ultimo
│   ├── test_chat_archive.py    # build_record/classify_media, _archive_range (dedup/append/no-op), _recover
│   └── test_admin_dashboard_kb.py # tastiere dashboard (grammatica callback, paginazione)
└── integration/
    ├── test_economy_service.py  # credit / debit / transfer / daily / history
    ├── test_economy_locking.py  # validazione amount>0 (credit/debit) + daily idempotente
    ├── test_badge_service.py    # sync_trophies / award / milestones (rarità, default catalog)
    ├── test_trophies.py         # condizione xp / item_purchases / category_purchases / shop_purchases / podium / collection (punto fisso) / sync upsert / leaderboard_trophies
    ├── test_xp_admin.py         # grant/set/airdrop XP + parità audit (xp_grant/xp_set/xp_airdrop) + leaderboard_xp
    ├── test_bet_service.py      # create_event / place_bet / resolve / cancel
    ├── test_bet_locking.py      # no bet su evento locked + total_wagered atomico
    ├── test_group_registry.py   # id gruppo effettivo: fallback / override / migrazione persistita / restart
    ├── test_shop_service.py     # cosmetici: acquisto debita + applica tag, idempotenza, niente mute
    ├── test_db_middleware.py    # _upsert_user (upsert, update, idempotenza)
    ├── test_admin_service.py    # set_balance / mass_credit / warn / dossier / stats / audit / list_users
    ├── test_consumable_service.py # consumabili: record_consumption ripetibile, purchase_counts, inventory, category_total
    ├── test_progress_service.py  # record_podium / podium_counts (podi + primi posti, aggregato "any")
    ├── test_quiz_service.py     # create/add_question / record_answer / podium / award_prizes (legacy + per-rango + consolazione)
    ├── test_admin_dashboard.py  # apply_warning (audit + escalation) / render_user_detail / user picker
    ├── test_schedule_service.py # schedule / due_tasks / mark_done|failed / cancel
    ├── test_state_roundtrip.py  # export_state → import_state: valori preservati, DB non vuoto rifiutato, checksum
    ├── test_migrations_pg.py    # [pg] _MIGRATIONS: schema fresco, idempotenza, guardia dialetto,
    │                            #   colonne ri-aggiunte da un deploy vecchio, BIGINT >2^31, indici ledger
    ├── test_scheduler_failure_path.py # path di errore su sessione reale: istanza ORM leggibile dopo il rollback
    └── test_money_concurrency_pg.py # [pg] gare su denaro/XP/bet/quiz — 16 guardie di regressione
```

### I test marcati `pg` (PostgreSQL reale)

Servono perché due cose sono **strutturalmente invisibili** alla suite SQLite, ed entrambe stanno sul
path denaro: `SELECT ... FOR UPDATE` è un no-op su SQLite, e il suo engine in-memory usa `StaticPool`,
che dà a ogni sessione la **stessa connessione** — quindi la stessa transazione: una gara a due
sessioni non è scrivibile lì. In più `run_migrations()` esce subito se il dialetto non è postgresql,
quindi `_MIGRATIONS` (che gira in **produzione a ogni deploy**) non era mai stato eseguito da un test.

Fixture in `conftest.py`: `pg_engine` (schema fresco per test) → `pg_sessions` (la **factory**, perché
questi test aprono due sessioni indipendenti) → `pg_session`, `pg_user_factory`. Le fixture SQLite
esistenti sono intatte: senza `TEST_PG_URL` i test `pg` **skippano**, quindi il run locale di default
non richiede Docker.

⚠️ **`pg_engine` fa `drop_all`** e rifiuta ogni URL il cui nome DB non finisce in `_test`: quello del
compose si chiama `gamingbot`, a un carattere di distanza. La guardia non è decorativa.

```bash
docker run -d --name gcb-pg-test -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=gamingbot_test -p 5433:5432 postgres:16-alpine
export TEST_PG_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test"
pytest -m pg -rxX          # -rxX elenca xfail E xpass
pytest -m "not pg"         # esplicitamente senza
```

Questi test sono nati `xfail(strict=True)` come strumento diagnostico: ognuno provava che un sito era
rotto, e `strict` faceva fallire la build su un xpass — cioè "questo sito è già sicuro". Sono stati
tolti uno per commit man mano che i siti venivano corretti. **Oggi sono tutti guardie di regressione
e non c'è più nessun xfail**: se uno torna rosso, qualcuno ha rimesso una decisione sul denaro in
Python. Vedi regola 22 per il meccanismo e il pattern corretto.

### Eseguire i test

```bash
pip install -r requirements-dev.txt
pytest                                       # tutti i test (src-layout: pythonpath=src in pyproject)
pytest --cov=src --cov-report=term-missing  # con coverage
pytest tests/unit/                           # solo unit
pytest tests/integration/                   # solo integration
```

### Env vars richieste

Set automaticamente in `tests/conftest.py` con `os.environ.setdefault`.
In CI vanno passate come `env:` nello step pytest (vedi `.github/workflows/tests.yml`):

```
BOT_TOKEN=1234567890:AAFtesttoken_ci
DAILY_REWARD_COINS=100
DB_URL=sqlite+aiosqlite:///:memory:
```

### Fixture principali

| Fixture | Scope | Descrizione |
| --- | --- | --- |
| `engine` | function | SQLite in-memory, tabelle create, dispose al termine |
| `session` | function | `AsyncSession` legato all'engine del test |
| `user_factory` | function | Factory `async (tg_id, coins=0, **user_kwargs) → (User, Wallet)` (accetta `xp=`, `xp_today=`, …) |
| `seeded_session` | function | Come `session` + catalogo trofei pre-popolato (`seed_badges` → `sync_trophies`) |

### Coverage attuale

Misurati, non stimati (`pytest --cov=src`, con Postgres). Il range è per-file, l'aggregato
è per package — un package può avere un aggregato alto e un file molto peggio, che è
esattamente com'era nascosto `services/backup/loop.py` a 0% sotto un «`services/*` 96-100%».

| Package | Range per-file | Aggregato |
| --- | --- | --- |
| `config_data/*` | 100% | 100% |
| `exceptions/*` | 100% | 100% |
| `database/*` | 88–100% | 99% |
| `filters/*` | 98–100% | 97% |
| `services/*` | 64–100% | 91% |
| `utils/*` | 88–100% | 91% |
| `keyboards/*` | 44–100% | 77% |
| `middlewares/*` | 39–100% | 73% |
| `handlers/*` | 21–100% | 38% — richiedono mock del framework aiogram, testati E2E con Docker |

I due peggiori sono `middlewares/db_middleware.py` e `handlers/_targeting.py`: entrambi
stanno su ogni update o su ogni comando che prende un bersaglio, quindi sono i prossimi
candidati sensati, non i file grandi con l'aggregato basso.

### Note implementative

- **identity map SQLAlchemy**: non pre-caricare `user_bets` con `selectinload` nel fixture `_create_event` — segnerebbe la collezione come "loaded" (vuota). **La stessa trappola era un bug di produzione**: `resolve_event` leggeva i bet da `event.user_bets`, e chi teneva l'evento teneva anche quella collection — una scommessa piazzata dopo lo snapshot veniva addebitata e mai liquidata. Ora li rilegge con una query. Vedi regola 22.
- **un `refresh` butta via una modifica pendente sullo stesso attributo** (`autoflush=False` ⇒ la modifica non è ancora nel DB). Motivo in più per scrivere in SQL invece di mutare l'istanza: così non resta mai niente di pendente. Fissato dalla guardia «due grant nella stessa transazione» in `TestUncappedXp`.
- **la identity map usa riferimenti deboli** — un oggetto caricato e non conservato viene garbage-collected e sparisce dalla mappa. È il motivo per cui `_upsert_user` non avvelena la sessione, e va tenuto presente prima di "ottimizzare" mettendo lo `User` in `data`: lo renderebbe **forte**, e ogni lock di ogni servizio diventerebbe vulnerabile su tutti i ~190 handler. Il tripwire è `TestMiddlewareDoesNotPoisonTheSession`.
- **due sessioni sullo stesso DB non sono esprimibili su SQLite in-memory**: `StaticPool` dà a ogni sessione la **stessa** connessione, quindi la stessa transazione. Ogni test di concorrenza reale richiede il fixture `pg_engine` (marker `pg`).
- **timestamp SQLite**: `func.now()` ha precisione al secondo → ordinamento `get_history` usa `(created_at DESC, id DESC)` come secondary sort.
- **`create_all` e `_MIGRATIONS` producono DDL diverse**: le colonne con `default=…` nei modelli non hanno default *server-side* su uno schema nuovo (SQLAlchemy lo applica in Python), mentre `_MIGRATIONS` le ricrea come `NOT NULL DEFAULT …`. Irrilevante via ORM, rilevante per un `INSERT` SQL grezzo. Fissato in `tests/integration/test_migrations_pg.py`.

### GitHub Actions (CI/CD)

Tre workflow in `.github/workflows/`:

- **`tests.yml`** — push + PR su qualsiasi branch; `pytest --cov=src -rxX` → `ruff check src/` →
  `mypy` (§1); opzionale Codecov (`CODECOV_TOKEN` nei secrets). È anche `workflow_call`
  (riusabile come gate). Include un **service `postgres:16-alpine`** (DB `gamingbot_test`) e
  passa `TEST_PG_URL`, che abilita i test marcati `pg`; `DB_URL` **resta SQLite**, perché
  `database.connection` costruisce il suo engine all'import e non deve puntare al DB di test.
  `-rxX` elenca xfail e **xpass**: serviva quando le gare sul denaro erano `xfail(strict=True)`
  (un PASS inatteso faceva fallire la build). Oggi non ci sono più xfail; il flag resta perché
  è così che si vede subito se qualcuno ne reintroduce uno.
  Coverage con ratchet **`fail_under = 59`** in `pyproject.toml`: si alza, non si abbassa.
- **`docker-image.yml`** — push su `main`/`test` o di un git tag `v*.*.*`: job `test`
  (chiama `tests.yml`) → `build-and-push` su **GHCR** (`ghcr.io/${{ github.repository }}`). L'immagine
  si pubblica **solo se i test passano**. Build **multi-arch** `linux/amd64,linux/arm64` (via
  `setup-qemu-action` + `platforms:` su `build-push-action`); cache `gha`, `packages: write`;
  `concurrency` per-ref (un push supera il precedente sullo stesso branch).
  **Modello di release a due branch (versioning):**
  - **`test`** (staging) ⇒ ogni tag col suffisso **`-test`**: `latest-test` (rolling) + `sha-<short>-test` (pinned).
  - **`main`** (prod) ⇒ ogni merge pubblica `latest` (rolling) + `sha-<short>` (pinned), **senza** suffisso.
  - **git tag `vX.Y.Z`** (su un commit di main) ⇒ release **immutabile** semver: `X.Y.Z` + `X.Y` + `X`.
  `latest` segue **sempre** la HEAD di main; i numeri di versione sono snapshot immutabili. Il suffisso
  `-test` è applicato via `flavor: suffix=…,onlatest=true` condizionato a `github.ref`; i tag per-branch
  sono gated da `enable=${{ github.ref == … }}`; i tag semver da `type=semver` (solo su ref tag).
  **Tagliare una release:** merge su `main` → `git tag v1.2.3 && git push origin v1.2.3`.
  *Niente filtro `paths`*: ogni push a un branch di release pubblica (così "ogni merge su main rilascia"),
  la build cache rende economiche le ricostruzioni ed evita il footgun "tags + paths".
- **`compose-artifact.yml`** — push che tocca `docker-compose.yml`: valida (`docker compose config`)
  e pubblica `docker-compose.yml` + `.env.example` come **artifact** (nessuna immagine).

Regola: **push su `main`/`test` o tag `v*` ⇒ immagine GHCR** (gated dai test, suffisso/versione per ref) ·
**compose cambia ⇒ solo artifact** · **ogni push ⇒ i test girano**.

### Watchtower: rischio noto e accettato

`containrrr/watchtower` è **senza tag** (quindi `:latest`), l'immagine upstream non è più
manutenuta (da lì la env var che forza la versione dell'API Docker: quella immagine manda
la 1.25, rifiutata dai daemon moderni) e monta `/var/run/docker.sock` in **lettura e
scrittura**, che sull'host equivale a root. Cioè: la cosa che aggiorna il bot da sola non è
fissata a un digest e ha le chiavi di casa. Con quel socket, `docker inspect` espone anche
`BOT_TOKEN`, `GROQ_API_KEY`, la password Postgres e `TELEGRAM_SESSION` — quest'ultima è una
credenziale di account **completo**, non del bot.

**Consapevole, e si tiene così.** Le alternative sono state guardate e scartate: fissare un
digest, passare a un fork mantenuto, mettere davanti un socket-proxy, o togliere Watchtower
e fare il pull da GitHub Actions via SSH. Ognuna costa setup e la più pulita costa
l'auto-update. Non riaprire la discussione senza un fatto nuovo (una CVE su quell'immagine,
o il registry che smette di servirla).

---

## 24. Checklist prima di ogni PR

- [ ] `pytest` passa (540+ test verdi)
- [ ] `python src/main.py` importa senza errori (o `PYTHONPATH=src python -c "import main"`)
- [ ] Tutti i nuovi handler usano `db_session: AsyncSession`
- [ ] Service non committano (salvo eccezioni documentate)
- [ ] Nuovi deep-link registrati in `common.cmd_start`
- [ ] Nuovi comandi **utente** aggiunti a `_PRIVATE_COMMANDS` / `_GROUP_COMMANDS`; comandi **admin** solo in `/help`
- [ ] **Stringhe user-controlled** in messaggi HTML passate da `utils.text.esc` (regola 20)
- [ ] **Nessun `settings.group_id` a runtime** — `group_registry.get_group_id()` (regola 21)
- [ ] Mutazioni denaro/XP/bet decise **in SQL**: check nella `WHERE`, aritmetica nella `SET`, `synchronize_session=False` + `refresh`. Letture per colonna, non per entità. Lock solo dove SQL non basta (ordine Event→User→Wallet, wallet per `tg_id` crescente); `credit/debit` con `amount>0` (regola 22)
- [ ] Trofei conditions aggiornate se aggiunte nuove metriche su `User`; nuove colonne `User` ⇒ voce in `_MIGRATIONS` (anche cambi di **tipo** colonna)
- [ ] `User.xp` mutato solo via `xp_service`; nuove sorgenti XP classificate capped/uncapped
- [ ] Nuovi cosmetici/consumabili/categorie/trofei/ranghi: aggiungere al CSV (`catalogs/*.example.csv` + default Python), non hardcodare nel codice; nuove condizioni trofeo via dispatch + `TROPHY_CONDITIONS` + `describe_condition` (mai `if/elif` sparsi); `collection` a punto fisso
- [ ] Nuovi service method coperti da integration test
- [ ] Azioni admin mutanti loggate via `log_action` + guard self/bot-target; nuovi comandi AI con cap di lunghezza, input clippato, output `parse_mode=None`, nessun filtro moderazione nel payload
- [ ] Nuovi check admin via `is_admin`; nuovi **tipi-evento** via spec `EventType` registrata in `register_builtin` (mai `if/elif` in hub/scheduler, §18.2/regola 25); quiz: handler `poll_answer` registrato
- [ ] **Backup/export** (§25): nuove tabelle ⇒ entrano automaticamente nell'export (`Base.metadata.sorted_tables`); IO file solo via `utils.atomic_io`; nessuna nuova lettura non-streaming su tabelle grandi; archivio chat resta opt-in e non bloccante

---

## 25. Backup & esportazione stato

Due sottosistemi **opt-in, non bloccanti, additivi** (se non configurati restano inerti).
**File:** `utils/atomic_io.py` (primitive crash-safe), `services/backup/{state_export,chat_archive,loop}.py`,
`handlers/backup.py`, `scripts/{export_state,import_state,login_telethon}.py`.

**Principi (host a 300 MB):** streaming end-to-end (cursore server-side / iterazione messaggio-per-messaggio,
nessun dataset in RAM); scritture **atomiche** (`atomic_write_bytes`: tmp→fsync→`os.replace`); loop di
background in `try/except` totale (come `scheduler_loop`); compressione gzip streaming; un `asyncio.Lock`
serializza i run sull'archivio chat.

### 25.1 Esportazione stato (`state_export`)

Dump **logico engine-agnostico** (SQLite↔Postgres) di **tutte** le tabelle (`Base.metadata.sorted_tables`,
ordine FK-safe) in **JSONL gzip**: header (`schema_version`, `created_at`, dialect, conteggi) + una riga
`{"t","r"}` per riga DB. Solo `datetime/date` hanno (de)serializzazione speciale; tutto il resto è
JSON-native. Snapshot pubblicati **atomicamente** + ruotati (`backup_state_keep`) + `state-latest.jsonl.gz`
(hardlink, swap atomico) + sidecar `.sha256`. `import_state(session, src, mode)`: verifica sha256 + schema,
`mode="empty"` (rifiuta DB non vuoto, caso migrazione) o `mode="replace"` (svuota, distruttivo). **No-commit**
(§5): il caller (CLI/handler) committa. Le **nuove tabelle entrano nell'export automaticamente** — nessun
elenco da mantenere.

### 25.2 Archivio chat (`chat_archive`, MTProto/Telethon)

La **Bot API non legge la cronologia** → si usa una sessione **utente** Telethon (`StringSession`, credenziale
sensibile, `scripts/login_telethon.py`). **Un solo** file `chat-archive.jsonl.gz` = sequenza di **membri gzip
concatenati** (uno per range) + `chat-archive.manifest.json` (`committed_offset`, `last_message_id`,
`anchor_ts`, `sha256`). **Prima esecuzione**: tutto fino ad `anchor_ts` (= ora del primo backup); **successive**:
solo i messaggi dopo `last_message_id` fino al nuovo cutoff → file che cresce per range. **Recovery**: se un
crash lascia un membro parziale, il run successivo tronca a `committed_offset` (mai rotto). **Foto/audio/media
non scaricati** (solo label `media` + caption). Client **connect-on-demand → disconnect** a fine run (nessuna
2ª connessione persistente). Il core puro (`build_record`/`classify_media`/`_archive_range`/`_recover`) è
Telethon-agnostico e unit-testato con fake.

### 25.3 Loop & comandi

`services/backup/loop.backup_loop()` avviato in `main()` (accanto a `scheduler_loop`): valuta la due-ness dagli
artefatti su disco (mtime snapshot / `updated_at` manifest), esegue `export_state` e/o `run_chat_backup` quando
dovuti. `/backup` (archivio chat) e `/esporta` (stato) — admin, redirect-to-private, DM del file se ≤ 50 MB,
audit `log_action`; deep-link `backup`/`esporta` in `common.cmd_start`. Restore = **solo CLI**
(`scripts/import_state.py`), mai bottone Telegram distruttivo.

> `tests/unit/test_backup_loop.py` (il modulo era a **0%**, ora 100%). Le garanzie coperte sono
> quelle che tengono in piedi il bot, non i dettagli: la soglia di due-ness è `>=` e non `>` (con
> `>` una cadenza giornaliera slitta sempre più tardi); il pre-flight salta il giro con **un**
> warning invece di un traceback EACCES a ogni scrittura; entrambi i backup sono avvolti in un
> `except` largo, quindi uno rotto non ferma l'altro; il loop sopravvive a un tick che solleva.
>
> Verificate **per mutazione**, non per coverage: 5 modifiche introdotte a mano nel modulo, una
> per garanzia, e ognuna fa fallire il test corrispondente. È così che ho scoperto che la mia
> prima asserzione sul confine `>=` non pinnava niente — `now - 24h` è già 24.000001 ore quando
> il confronto gira, quindi passava in entrambi i casi.

### 25.4 Permessi di scrittura (Docker)

`/app/backups` è un **named volume `bot_backups`**, *non* un bind mount `./backups`: alla prima creazione
il volume eredita l'ownership della dir nell'immagine (`botuser`, UID 1001, vedi `Dockerfile`), così il
processo non-root **scrive sempre** — un bind mount sarebbe di proprietà dell'host → `EACCES`. Difese nel
codice: `atomic_io.probe_writable(dir)` (pre-flight `write+unlink`, mai solleva) chiamato all'avvio
(`main`, warning non bloccante) e a ogni tick (`backup/loop`, salta il giro con un warning chiaro invece di
uno stack trace `EACCES`); `atomic_write_bytes`/`GzipMemberWriter.open` loggano il path su `OSError`. I
backup si recuperano via DM `/backup`·/`esporta` o `docker cp`.
