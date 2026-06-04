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
| Python | 3.11+ | `from __future__ import annotations` richiesto per compat 3.9+ |
| aiogram | 3.13.1 | **Non** usare aiogram 2.x — API completamente diversa |
| SQLAlchemy | 2.0 (async) | `mapped_column`, `Mapped[T]`, `AsyncSession` |
| pydantic-settings | 2.x | `BaseSettings`, `SettingsConfigDict` |
| DB prod | PostgreSQL 16 (asyncpg) | |
| DB dev | SQLite (aiosqlite) | default in `.env` locale |
| FSM storage | `MemoryStorage` (dev) / `RedisStorage` (prod) | configurabile via `.env` |
| aiohttp | 3.10.11 | client async per le chiamate LLM Groq — **mai** librerie HTTP bloccanti |
| LLM | Groq API (OpenAI-compatible) | modello via `GROQ_MODEL` (default `llama-3.3-70b-versatile`) |

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
- `fsm_storage: str` — `"memory"` | `"redis"`
- `redis_url: str`
- `groq_api_key: str` — chiave API Groq per il modulo AI (vuota = AI disattivato, fallback)
- `groq_model: str` — default `"llama-3.3-70b-versatile"` (il vecchio `llama3-70b-8192` è **dismesso**)
- `ai_cooldown_seconds: int` (default 60) — anti-spam comandi AI per non-admin
- `warn_mute_threshold: int` (default 3), `warn_ban_threshold: int` (default 5), `warn_mute_duration_seconds: int` (default 3600) — sistema warn admin
- `quiz_default_prize: int` (default 1000, **legacy** pool), `quiz_xp_per_correct: int` (default 10) — modalità quiz
- **Premi quiz per-rango**: `quiz_default_first` (1000), `quiz_default_second` (500), `quiz_default_third` (250), `quiz_default_consolation` (100) — default suggeriti nella creazione; `quiz_participation_floor_ratio` (0.2) + `quiz_participation_floor_min` (1) → minimo garantito = `max(floor_min, round(consolation*ratio))`
- **XP & cataloghi** (§12.1/§12.2): `catalog_dir: str` (default `"data"`, dir dei CSV trofei/ranghi/cosmetici); `xp_daily_participation_cap: int` (default 50, tetto XP farmabili/giorno); `xp_per_daily_claim: int` (default 10); `xp_per_bet_won: int` (default 15)
- `scheduler_timezone: str` (default `"Europe/Rome"`), `scheduler_poll_interval: int` (default 20) — scheduler eventi

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
                 rarity, xp_reward, condition_type, condition_value)   ← rarity = trofei (§12)
user_badges     (id PK, user_tg_id FK, badge_id FK, earned_at, notified)
                UniqueConstraint(user_tg_id, badge_id)
betting_events  (id PK, title, description, creator_tg_id FK, status, resolution_option_id,
                 created_at, locked_at, resolved_at)
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
- La valuta si chiama **Alduero** (sing.) / **Aldueuri** (plur.) nei testi utente; la colonna/attributo DB resta `Wallet.coins` (NON rinominare il campo, solo le stringhe visibili)
- `LedgerEntry` traccia ogni movimento — `amount` positivo per credit, negativo per debit
- `UserBet` ha UniqueConstraint(user_tg_id, event_id) — un utente non può scommettere due volte sullo stesso evento
- `daily_streak`, `bets_won`, `transfers_made` su `User` vengono aggiornati nei rispettivi service — **non** calcolati on-the-fly
- `User.xp` è una **metrica di merito separata dalle monete** e si muta **solo** via `xp_service` (§12.1). `xp_today`/`xp_today_date` sono il contatore del **tetto giornaliero** delle sorgenti capped; `rank_slug` è l'ultimo rango visto (per annunciare i rank-up); `cosmetic_tag` è il flair acquistato nel negozio (§11)
- `warnings`/`admin_actions`/`quizzes`/`quiz_questions`/`quiz_answers`/`scheduled_tasks` sono tabelle **nuove**: create da `create_all`. Le **colonne premio per-rango** (`prize_first/second/third/consolation/min`), le colonne progressione di `users` (`cosmetic_tag`, `rank_slug`, `xp_today`, `xp_today_date`) e `badges.rarity` sono invece state aggiunte a tabelle esistenti *dopo* il primo deploy → hanno voci `ALTER TABLE … ADD COLUMN IF NOT EXISTS …` in `_MIGRATIONS` (idempotenti, solo Postgres; SQLite ricrea da `create_all`). Regola: colonne aggiunte a tabelle esistenti ⇒ voce in `_MIGRATIONS`; tabelle nuove ⇒ no.
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
    wallet.coins += amount
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
dp.update.middleware(GroupMemberMiddleware())  # 3. blocca non-membri in privato
```

**Non invertire.** Il DB middleware deve girare prima del guard perché il guard fa API call che richiede il bot (disponibile dal framework), non la sessione DB.

---

## 7. Ordine router (critico)

```python
dp.include_router(onboarding.router)
dp.include_router(economy.router)
dp.include_router(admin_betting.router)  # ← DEVE stare prima di betting
dp.include_router(betting.router)
dp.include_router(badges.router)
dp.include_router(shop.router)
dp.include_router(common.router)         # ← DEVE stare per ultimo (catch-all /start)
```

`admin_betting` prima di `betting` perché in fondo ad `admin_betting.router` c'è un catch-all deny per tutti i callback `admin_bet:*`. Se `betting.router` fosse registrato prima, i callback `admin_bet:*` non verrebbero mai visti dall'admin.

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
**fail-closed** su errore API). Quindi **tutti gli admin del gruppo** hanno i poteri bot-admin senza
doverli elencare in `ADMIN_IDS`. Usare sempre `is_admin` per i check inline (non `user.id in
settings.admin_ids` diretto); chiamare `invalidate_admin_cache()` su promozioni/retrocessioni.

---

## 9. Deep-link pattern

Tutti i redirect gruppo → privato usano `?start=<payload>`.

| Payload | Handler | Destinazione |
|---|---|---|
| `help` | `common.cmd_start` | Mostra guida comandi |
| `manage_bets` | `common.cmd_start` | Apre pannello admin scommesse (`admin_betting._show_event_list`) |
| `admin` | `common.cmd_start` → `admin.show_admin_panel` | Apre il pannello admin (dashboard) |
| `create_quiz` | `common.cmd_start` → `quiz.start_quiz_creation` | FSM creazione quiz (admin) |
| `quiz_<id>` | `common.cmd_start` → `quiz.start_quiz_session` | Gioca/riprendi un quiz in privato |
| `programma` | `common.cmd_start` → `schedule.start_schedule_flow` | FSM programmazione evento (admin) |
| `shop_<group_id>` | `common.cmd_start` → `shop.start_shop_private` | Catalogo negozio |
| `create_bet` | `common.cmd_start` → `betting.start_bet_creation` | FSM creazione scommessa |
| `bet_custom_<e>_<o>` | `common.cmd_start` → `betting.start_custom_amount` | FSM importo custom |
| `bet_<event_id>` | `common.cmd_start` → `betting.start_bet_view` | Dettaglio evento |

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

## 11. Shop — personalizzazioni cosmetiche

File: `services/shop_service.py` · handler `handlers/shop.py` · kb `keyboards/shop_kb.py`

Il negozio vende **cosmetici** (tag/titoli), **non** azioni di moderazione. I vecchi item
mute (`mute_user/mute_admin/mute_random`) e tutto il flusso target sono stati **rimossi**:
erano un vettore di grief. Catalogo da CSV via `catalog_loader` (§12.2):

```python
COSMETICS: dict[str, CosmeticItem]  # = catalog_loader.get_cosmetics()
# CosmeticItem(key, name, tag_text, emoji, price)
```

**Apre ovunque:** `/negozio` mostra il catalogo inline in **qualunque chat** (privato o
gruppo) con `message.answer` — niente più `group_id`/deep-link (il deep-link
`?start=shop_<id>` resta accettato per retro-compat, `start_shop_private` chiama `_show_catalog`).

### Flow acquisto

```
/negozio → _show_catalog (in ogni chat)
shop:buy:<key>  → idempotenza (già posseduto? alert) → balance check → conferma + anteprima tag
shop:exec:<key> → idempotenza → debit → record_purchase (no-commit) → apply_cosmetic → commit
shop:owned      → alert "già posseduto"
shop:list       → torna al catalogo · shop:close → elimina
```

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

**Condizioni di sblocco** (`check_and_award_milestones`): `onboarding`, `balance`,
`daily_streak`, `bets_won`, `transfers_made`, **`xp`** (nuova). Il "Platino" è semplicemente
un trofeo con condizione `xp` ad alta soglia. I counter vengono incrementati in:
- `daily_streak` → `economy_service.claim_daily()`
- `bets_won` → `bet_service.resolve_event()`
- `transfers_made` → `economy_service.transfer()`

`check_and_award_milestones` va chiamato dopo ogni azione che può sbloccare un trofeo, prima
del commit finale; **non committa**. `leaderboard_trophies(session)` ordina per numero di
trofei (tie-break XP).

> **I Trofei NON danno XP.** `Badge.xp_reward` resta solo come dato di "valore" mostrato a
> schermo (storicamente non era comunque applicato a `User.xp`). Gli XP arrivano solo dalle
> sorgenti di §12.1 → niente cascata di sblocchi.

`/traguardi` mostra i trofei **raggruppati per rarità** + rango/tag correnti; `/catalogo_badge`
elenca tutto con rarità e condizione.

---

## 12.1 XP & progressione

File: `services/xp_service.py` — **unico** punto che muta `User.xp` (no-commit, §5).

```python
grant_xp(session, tg_id, amount, source: XpSource, *, capped) -> XpGrantResult(granted, capped, new_rank)
set_xp(session, tg_id, value)          # admin: valore assoluto
airdrop_xp(session, amount)            # admin: +amount a tutti
rank_for_xp(xp) -> Rank | None         # rango = max(min_xp ≤ xp), registry §12.2
leaderboard_xp(session, limit=10)
```

**Sorgenti XP** (`XpSource`):

- `quiz` (evento, **non capped**) — `quiz_xp_per_correct` per risposta corretta (in `quiz_service._grant_xp`)
- `daily` / `bet_won` (**capped**) — `xp_per_daily_claim` / `xp_per_bet_won`
- `admin_grant` / `admin_airdrop` (**non capped**) — `/dai_xp`, `/set_xp`, Airdrop XP dashboard

**Tetto giornaliero** (anti-farm): le sorgenti `capped=True` accreditano al massimo
`xp_daily_participation_cap` per utente al giorno, contato in `User.xp_today`/`xp_today_date`
(reset automatico al cambio data). Gli eventi admin (quiz incluso) sono uncapped perché curati.

**Rank-up:** `grant_xp` ricalcola il rango; se è una promozione, lo restituisce in
`new_rank` e aggiorna `User.rank_slug` (gli handler `/daily`, `/dai_xp` lo annunciano).

**Regola d'oro:** nessun handler deve fare `user.xp += …` direttamente — sempre via `xp_service`.

### Classifiche — `handlers/leaderboard.py`

Comando utente `/classifiche` con switcher inline `lead:coins|xp|trofei` (`render_board`
riusato anche dalla dashboard `adm:lead:*`). Board: 💰 `admin_service.leaderboard`,
⚡ `xp_service.leaderboard_xp`, 🏆 `badge_service.leaderboard_trophies`.

---

## 12.2 Cataloghi CSV (catalog_loader)

File: `services/catalog_loader.py` — carica **trofei**, **ranghi**, **cosmetici** da CSV nella
dir `settings.catalog_dir` (default `data/`), **una sola volta all'avvio** (riavvio per
applicare le modifiche). Template tracciati in `catalogs/*.example.csv` + `catalogs/README.md`.

- `load_trophies()/load_ranks()/load_cosmetics()` — puri, **validano** le righe, **saltano e
  loggano** quelle malformate, e in caso di file assente/vuoto **fanno fallback ai default
  Python** (`DEFAULT_TROPHIES/RANKS/COSMETICS`) → il bot parte sempre, anche a freddo/nei test.
- `init_registries(catalog_dir=None)` (chiamata in `main()`) popola i registry in-memoria
  `_ranks`/`_cosmetics`; accessori `get_ranks()/get_cosmetics()`. I **ranghi non hanno tabella**
  (derivati dagli XP); i **cosmetici** neppure (registry in-memoria, acquisto loggato in `ShopPurchase`).
- `main()` chiama `badge_service.sync_trophies(session)` (trofei → DB) + `catalog_loader.init_registries()` e logga i conteggi.

**Per personalizzare:** copia `catalogs/*.example.csv` in `data/` (senza `.example`), edita, riavvia.

---

## 13. GroupMemberMiddleware

- Attivo solo se `settings.group_id != 0`
- Bypassa update del gruppo (chat.type != "private")
- Cache per-utente TTL 300s → chiama `invalidate_cache(user_id)` su join/leave
- Fail **open** in caso di errore API (non blocca utenti se il bot è rimosso dal gruppo)

---

## 14. Rate limiting

`RateLimitMiddleware`: max 12 chiamate in 10 secondi per utente (sliding window in-memory).
Si applica a tutti gli update: `Message` e `CallbackQuery`.

---

## 15. FSM states attivi

| State | File | Descrizione |
|---|---|---|
| `BetCreationStates.waiting_for_title` | `handlers/betting.py` | |
| `BetCreationStates.waiting_for_description` | `handlers/betting.py` | |
| `BetCreationStates.waiting_for_options` | `handlers/betting.py` | |
| `BetCustomAmountState.waiting_for_amount` | `handlers/betting.py` | |
| `QuizCreationStates.*` | `handlers/quiz.py` | creazione quiz: title→desc→**prize_mode**→{prize_first/second/third/consolation}→loop domande {text→options→correct→explanation}→**reviewing**. Tasti «⬅️ Indietro» (`quiz_new:back`, mappa `_BACK_PROMPTERS`) e schermata di riepilogo prima di pubblicare. |
| `AdminPanelStates.*` | `handlers/admin_dashboard.py` | input della dashboard a bottoni: `waiting_amount` (credit/debit/setbal/**xpgrant/xpset**) · `waiting_duration` · `waiting_reason` · `waiting_search` · `waiting_airdrop` · `waiting_xp_airdrop` |

> Il negozio non usa più una FSM: i cosmetici si applicano al volo (§11), nessun `ShopState`.
| `ScheduleStates.*` | `handlers/schedule.py` | programmazione quiz/poll/bet (config + orario) |
| `SondaggioStates.*` | `handlers/schedule.py` | `/sondaggio` (domanda + opzioni) |

---

## 16. Comandi registrati

### Privato
`/start`, `/profilo`, `/saldo`, `/storico`, `/daily`, `/trasferisci`, `/scommesse`, `/crea_scommessa`, `/traguardi`, `/catalogo_badge`, `/classifiche`, `/negozio`, `/help`

### Gruppo
`/scommesse`, `/crea_scommessa`, `/daily`, `/saldo`, `/profilo`, `/traguardi`, `/classifiche`, `/negozio`, `/help`

**Intrattenimento AI** (gruppo): `/maestro`, `/complotto`, `/difendi`, `/accusa`, `/drama`, `/dialetto`, `/insulta`

### Admin only (non registrati nelle command list pubbliche — §18 regola 11)
- **Scommesse**: `/gestisci_scommesse`
- **Valuta**: `/credita`, `/addebita`, `/setsaldo`, `/airdrop`, `/saldo_di`
- **XP**: `/dai_xp @u <n>` (grant, uncapped), `/set_xp @u <n>` (assoluto) — gestione XP solo admin (§12.1)
- **Moderazione**: `/ban`, `/sban`, `/kick`, `/mute [durata]`, `/unmute`
- **Warn**: `/warn [motivo]`, `/warns`, `/unwarn`
- **Info & dashboard**: `/info`, `/cerca`, `/classifica`, `/stats`, `/audit`, `/admin` (UI a bottoni — §18.1)
- **Quiz**: `/crea_quiz`, `/quiz`, `/avvia_quiz <id>`, `/chiudi_quiz <id>`
- **Eventi/scheduling**: `/sondaggio`, `/programma`, `/programmati`

---

## 17. Modulo Intrattenimento AI

Comandi comici "one-shot" che rielaborano un messaggio via LLM. Tono edgy/satirico per adulti.

**File:** `services/ai_service.py` (Service Layer), `handlers/fun_ai.py` (handler).

### ai_service — client Groq

- **Sempre `aiohttp` async** — mai librerie bloccanti (non bloccare l'event loop di aiogram).
- Endpoint OpenAI-compatible: `https://api.groq.com/openai/v1/chat/completions`.
- `generate_completion(system_prompt, user_text, max_tokens=300) -> str`:
  - `settings.groq_api_key` vuota → `AIServiceError` (niente chiamata di rete).
  - Timeout `aiohttp.ClientTimeout(total=20)`; `try/except` su `asyncio.TimeoutError` / `aiohttp.ClientError` / status≠200 / body malformato → tutti normalizzati in **`AIServiceError`**.
  - Payload: solo `model` + `messages` (system+user) + `temperature` + `max_tokens`. **Nessun campo di moderazione** (requisito di design).
- Costante `AI_FALLBACK_MESSAGE = "I server sono a fuoco, riprova dopo."` — usata dagli handler su `AIServiceError`.

### fun_ai — handler

- **Solo gruppo** (`ChatType.GROUP/SUPERGROUP`): in privato il bot invita a usarli nel gruppo.
- Comandi **reply-based** (`/maestro` `/complotto` `/difendi` `/accusa` `/drama` `/dialetto`): operano sul testo del `reply_to_message`; helper `_run_ai_command`. `/insulta` invece prende un target taggato (`@user`/reply).
- `/dialetto` traduce in **catanese stretto** (non siciliano generico): prompt con few-shot di lessico catanese.
- **Cooldown anti-spam** (`_check_cooldown`): max 1 comando AI / `settings.ai_cooldown_seconds` per utente, in-memory; **admin esenti** (via `is_admin`). Controllato in `_run_ai_command` e `cmd_insulta` prima del typing; timestamp aggiornato solo su comando eseguito.
- `send_chat_action(chat_id, ChatAction.TYPING)` prima della generazione.
- Ogni prompt è costruito con `_prompt(persona, max_chars)` che appende `_STYLE` condiviso:
  - regole "senza filtri / no disclaimer / no muri di testo",
  - **flavor gaming globale** (slang/riferimenti ai videogiochi quando ha senso),
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
- **Dossier/stats**: `get_dossier`, `search_users` (ILIKE), `leaderboard`, `economy_stats`.
- **Warn**: `add_warning` (→ count attivi), `active_warnings`, `active_warning_count`, `clear_warnings` (soft-delete).
- **Audit**: `log_action(admin, action_type, target?, group?, amount?, detail?)` (solo `session.add`, no commit), `recent_actions`. `action_type` valuta: `credita/addebita/setsaldo/airdrop`; **XP**: `xp_grant/xp_set/xp_airdrop` (amount = XP, mostrati con suffisso `XP` in `render_audit`); moderazione: `ban/sban/kick/mute/unmute/warn/unwarn`.

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
  Statistiche · Classifica · 🧠 Quiz · 🎲 Scommesse · 👥 Utenti · 💰 Economia · 🧾 Audit · ❓ Comandi.
- **Riuso, zero logica duplicata**: le viste riusano i renderer **pubblici** di `handlers/admin.py`
  (`render_stats`/`render_leaderboard`/`render_audit`/`render_panel_help`); quiz → `open_quiz`/`close_quiz`/`start_quiz_creation`;
  scommesse → `admin_betting._show_event_list`. Le azioni passano dagli **stessi service + `log_action`** dei comandi.
- **Azioni su utente** (`👥 Utenti`, lista paginata + 🔍 ricerca → `adm:user:<tg>`): credita/addebita/set saldo,
  **⚡ Dai XP / Set XP** (via `xp_service` + audit `xp_grant`/`xp_set`), ban/kick/sban, mute/unmute, warn/unwarn.
  Input (importo/XP/durata/motivo) via FSM `AdminPanelStates`; ban/kick passano da una conferma (`adm:ask:…` → `adm:do:…`).
- **Economia**: `💰 Economia` → `🎁 Airdrop monete` (`adm:airdrop`) e **`⚡ Airdrop XP`** (`adm:xpairdrop`, `xp_service.airdrop_xp` + audit `xp_airdrop`).
- **Classifica**: `adm:lead` con switcher `adm:lead:<coins|xp|trofei>` (riusa `handlers.leaderboard.render_board` + `lead_kb`).
- **Gating**: ogni callback `adm:*` con `IsAdminCallbackFilter` + **catch-all deny** `adm:` in fondo al router;
  azioni di moderazione disattivate se `group_id == 0`; guard self/target. `admin_dashboard.router` incluso
  dopo `admin.router` in `main.py`.
- **Grammatica callback** (≤ 64 byte): `adm:home|stats|lead|audit|help|close`, `adm:lead:<board>`, `adm:quiz[:open|close:<id>|:new]`,
  `adm:bets`, `adm:econ|airdrop|xpairdrop|search`, `adm:users:<page>`, `adm:user:<tg>`, `adm:act:<credit|debit|setbal|xpgrant|xpset|mute|warn>:<tg>`,
  `adm:ask:<ban|kick>:<tg>`, `adm:do:<…>:<tg>`.
- Il vecchio pannello read-only `admin_panel:*` + `keyboards/admin_panel_kb.py` è **rimosso** (assorbito dalla dashboard).

### Regole

- Il namespace `adm:*` (dashboard) non collide con `admin_bet:*` né con gli altri → ordine router indifferente, ma `admin_dashboard.router` va dopo `admin.router` e comunque prima di `common.router`.
- Tutte le azioni che modificano valuta/moderazione **devono** chiamare `log_action` prima del commit (vale per comandi **e** dashboard).
- I comandi admin **non** vanno nelle command list pubbliche (`_PRIVATE/_GROUP_COMMANDS`), ma vanno documentati nella sezione admin di `/help`.

---

## 19. Quiz mode (privato, con podio)

Quiz a risposta multipla creati dall'admin e giocati da ogni utente nella **propria chat privata**
(NO poll di gruppo: in un poll di gruppo si "risponde per tutti" / non si avanza bene). Niente
limite di tempo; vince chi ne azzecca di più, a parità conta l'**ordine di arrivo**.

**File:** `services/quiz_service.py` (DB), `handlers/quiz.py` (FSM + comandi + play privato).

### Creazione

FSM admin in privato (redirect dal gruppo con deep-link `create_quiz`, oppure dalla dashboard
`adm:quiz:new` che passa `creator_id` esplicito perché lì `message.from_user` è il bot): titolo →
descrizione → **premi** → loop domande {testo → opzioni (una per riga, 2–10) → opzione corretta
(inline) → spiegazione opzionale} → **riepilogo** {➕ Aggiungi · 🗑 Rimuovi ultima · ✅ Pubblica}.
**Nessun timer.** A fine: quiz `ready`.

- **Premi**: schermata `prize_mode` con ⚡ Consigliati (default da settings) · ✏️ Personalizza · 🚫 Nessuno.
  In personalizzato si impostano 1°/2°/3° e la **consolazione (4°)**; il `prize_min` è **derivato**
  (`quiz_service.participation_floor`). Il quiz viene creato (`create_quiz`) a fine flusso premi.
- **Navigazione**: ogni step ha «⬅️ Indietro» (`quiz_new:back`, dispatch via `_BACK_PROMPTERS` per stato);
  «⬅️ Riepilogo» quando si aggiungono altre domande. «🗑 Rimuovi ultima» → `quiz_service.delete_last_question`.
- **Hardening**: handler di input gated `IsAdminFilter()`/`IsAdminCallbackFilter()`.

### Avvio & gioco

- `open_quiz(bot, session, quiz_id)`: annuncia nel gruppo (bottone deep-link `quiz_<id>`) **poi**
  mette il quiz `running` (se l'annuncio fallisce resta `ready`). Usato da `/avvia_quiz`, `/quiz` e
  dallo scheduler. Caller committa.
- Ogni utente apre `?start=quiz_<id>` → `start_quiz_session`: gioca in privato, una domanda alla
  volta con **bottoni inline** (`quiz_ans:<quiz>:<question>:<opt>`). Alla risposta: feedback
  immediato (✅/❌ + spiegazione), poi domanda successiva. È **resumable** (riprende dalla domanda
  non ancora risposta). `record_answer` è idempotente per (domanda, utente) — dedup + `IntegrityError` guard.

### Podio & premi

- `podium(quiz_id)`: solo i **finisher** (hanno risposto a tutte le domande), ordinati per
  **corrette DESC, finish-time ASC** (ordine di arrivo).
- `award_prizes(quiz_id)` — due modalità (premi **mintati** via `economy_service.credit` `quiz_reward`,
  niente prelievo da un pot):
  - **Esplicita** (se almeno uno tra `prize_first/second/third/consolation` > 0): podio 1°/2°/3° →
    importi espliciti; dal 4° in giù **consolazione a scendere** `consolation_amounts(n, top=prize_consolation,
    floor=prize_min)` — funzione **pura**: scala lineare da `top` (4°) a `floor` (ultimo), non crescente,
    tutti ≥ floor. Solo finisher.
  - **Legacy** (altrimenti, se `prize_coins` > 0): pool diviso top-3 `_PRIZE_SPLIT` 0.5/0.3/0.2 (resto al 1°) —
    comportamento **invariato** per i quiz vecchi.
  - XP: `quiz_xp_per_correct` × corrette per chiunque abbia ≥1 corretta.
- `close_quiz(bot, session, quiz_id) -> (ok, msg)`: helper condiviso da `/chiudi_quiz` **e** dalla dashboard
  (`adm:quiz:close`) → `award_prizes` → `finished` → annuncio podio (🎖️ per le consolazioni).
- `format_prize_summary(quiz)` riassume i premi nelle schede/annunci.

### Regole

- Stati quiz: `draft → ready → running → finished`.
- Play in **privato** (i poll di gruppo non sono usati per i quiz; `send_poll` resta solo per `/sondaggio`).
- Service no-commit (§5): commit negli handler. `open_quiz` annuncia prima di flippare lo stato.

---

## 20. Scheduling (quiz / sondaggio / scommessa)

Telegram Bot API **non** permette di schedulare poll → scheduler in-process DB-backed.

**File:** `services/schedule_service.py` (DB + `parse_run_at`), `handlers/schedule.py`
(comandi + FSM + `scheduler_loop`/`execute_task`).

- `scheduler_loop(bot)` avviato in `main()` con `asyncio.create_task` prima di `start_polling`; ogni
  `scheduler_poll_interval`s esegue i `due_tasks` (try/except per task → `mark_done`/`mark_failed`).
- `parse_run_at(text)`: assoluto `AAAA-MM-GG HH:MM` o relativo `30m`/`2h`/`1d` → **UTC naive**
  (timezone `scheduler_timezone`). Rifiuta orari passati.
- `execute_task` per tipo: `bet` → `bet_service.create_event` (open) da payload + annuncio gruppo;
  `quiz` → `quiz.open_quiz` (annuncia + apre); `poll` → `bot.send_poll` (regolare) nel gruppo.
- Comandi: `/programma` (FSM scelta tipo → config → orario), `/programmati` (lista + annulla),
  `/sondaggio` (poll subito nel gruppo). Tutti `IsAdminFilter`.
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
  (`docker compose pull`); in dev resta `build: .`.
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
```

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
│   ├── test_catalog_loader.py # parse CSV trofei/ranghi/cosmetici + fallback + righe malformate
│   ├── test_group_guard.py   # invalidate_cache, _chat_type, _NON_MEMBER_STATUSES
│   ├── test_ai_service.py    # Groq client (aioresponses): success/timeout/http/malformed/no-key
│   ├── test_moderation_service.py # parse_duration + mappatura errori (Bot fake)
│   ├── test_admin_filter.py  # is_admin (admin_ids, cache TG mockata, fail-closed)
│   ├── test_ai_cooldown.py   # _check_cooldown (esenzione admin, finestra)
│   ├── test_schedule_parse.py # parse_run_at (assoluto/relativo/passato/invalid)
│   ├── test_quiz_prizes.py   # consolation_amounts / participation_floor (funzioni pure)
│   ├── test_keyboards.py     # keyboard builder (incl. shop cosmetici: affordable/owned/callback)
│   └── test_admin_dashboard_kb.py # tastiere dashboard (grammatica callback, paginazione)
└── integration/
    ├── test_economy_service.py  # credit / debit / transfer / daily / history
    ├── test_badge_service.py    # sync_trophies / award / milestones (rarità, default catalog)
    ├── test_trophies.py         # condizione xp / sync upsert / leaderboard_trophies
    ├── test_xp_admin.py         # grant/set/airdrop XP + parità audit (xp_grant/xp_set/xp_airdrop) + leaderboard_xp
    ├── test_bet_service.py      # create_event / place_bet / resolve / cancel
    ├── test_shop_service.py     # cosmetici: acquisto debita + applica tag, idempotenza, niente mute
    ├── test_db_middleware.py    # _upsert_user (upsert, update, idempotenza)
    ├── test_admin_service.py    # set_balance / mass_credit / warn / dossier / stats / audit / list_users
    ├── test_quiz_service.py     # create/add_question / record_answer / podium / award_prizes (legacy + per-rango + consolazione)
    ├── test_admin_dashboard.py  # apply_warning (audit + escalation) / render_user_detail / user picker
    └── test_schedule_service.py # schedule / due_tasks / mark_done|failed / cancel
```

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

| Modulo | Coverage |
| --- | --- |
| `services/*` | 96–100% |
| `keyboards/*` | 100% |
| `exceptions/*` | 100% |
| `config_data/*` | 100% |
| `middlewares/*` | 40–92% |
| `handlers/*` | 0% — richiedono mock framework aiogram, testati E2E con Docker |

### Note implementative

- **identity map SQLAlchemy**: non pre-caricare `user_bets` con `selectinload` nel fixture `_create_event` — segnerebbe la collezione come "loaded" (vuota), impedendo a `resolve_event` di rileggere i bet dal DB.
- **timestamp SQLite**: `func.now()` ha precisione al secondo → ordinamento `get_history` usa `(created_at DESC, id DESC)` come secondary sort.

### GitHub Actions (CI/CD)

Tre workflow in `.github/workflows/`:

- **`tests.yml`** — push + PR su qualsiasi branch; `pytest --cov=src`; opzionale Codecov
  (`CODECOV_TOKEN` nei secrets). È anche `workflow_call` (riusabile come gate).
- **`docker-image.yml`** — push che tocca `src/**`/`requirements.txt`/`Dockerfile`: job `test`
  (chiama `tests.yml`) → `build-and-push` su **GHCR** (`ghcr.io/${{ github.repository }}`, tag
  `latest` solo sul branch di default + branch + sha; cache `gha`, `packages: write`). L'immagine
  si pubblica **solo se i test passano**. Build **multi-arch** `linux/amd64,linux/arm64` (via
  `setup-qemu-action` + `platforms:` su `build-push-action`).
- **`compose-artifact.yml`** — push che tocca `docker-compose.yml`: valida (`docker compose config`)
  e pubblica `docker-compose.yml` + `.env.example` come **artifact** (nessuna immagine).

Regola: **sorgente cambia ⇒ immagine GHCR** (gated dai test) · **compose cambia ⇒ solo artifact** ·
**ogni push ⇒ i test girano**.

---

## 24. Checklist prima di ogni PR

- [ ] `pytest` passa (330+ test verdi)
- [ ] `python src/main.py` importa senza errori (o `PYTHONPATH=src python -c "import main"`)
- [ ] Tutti i nuovi handler usano `db_session: AsyncSession`
- [ ] Service non committano (salvo eccezioni documentate)
- [ ] Nuovi deep-link registrati in `common.cmd_start`
- [ ] Nuovi comandi **utente** aggiunti a `_PRIVATE_COMMANDS` / `_GROUP_COMMANDS`; comandi **admin** solo in `/help`
- [ ] Trofei conditions aggiornate se aggiunte nuove metriche su `User`; nuove colonne `User` ⇒ voce in `_MIGRATIONS`
- [ ] `User.xp` mutato solo via `xp_service`; nuove sorgenti XP classificate capped/uncapped
- [ ] Nuovi cosmetici/trofei/ranghi: aggiungere al CSV (`catalogs/*.example.csv` + default Python), non hardcodare nel codice
- [ ] Nuovi service method coperti da integration test
- [ ] Azioni admin mutanti loggate via `log_action`; nuovi comandi AI con cap di lunghezza + nessun filtro moderazione nel payload
- [ ] Nuovi check admin via `is_admin`; nuovi `ScheduledTask` con esecuzione in `execute_task`; quiz: handler `poll_answer` registrato
