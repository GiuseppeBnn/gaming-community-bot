# INDEX — mappa del repository

Indice di navigazione: **dove sta cosa**, e quale documento risponde a quale domanda.
Non contiene regole (quelle stanno in [STEERING.md](STEERING.md) e [CLAUDE.md](CLAUDE.md))
né istruzioni di setup (quelle stanno in [README.md](README.md)).

Bot Telegram per una community di gaming: economia in **CoInn**, scommesse stile Twitch,
quiz e giochi «indovina» a premi, XP/ranghi/trofei, negozio cosmetici, suite admin,
moderazione, backup. Stack: **Python 3.12** · aiogram 3.30.0 · SQLAlchemy 2.0 async ·
PostgreSQL 16 (prod) / SQLite (dev-test) · Groq LLM (opzionale).

Dimensioni attuali: **~47.700 righe** Python (`src/` + `tests/` + `scripts/`), **2121 test**.

---

## 1. Documentazione — quale file leggere

| File | Cosa contiene | Leggilo quando |
| --- | --- | --- |
| [README.md](README.md) | Setup, avvio locale/Docker, comandi del bot, CI/CD, FAQ | Devi **far girare** il bot |
| [STEERING.md](STEERING.md) | Documento **normativo** (§0–§25, ~1800 righe): architettura, invarianti, ogni sottosistema in dettaglio | Devi **modificare** il codice |
| [CLAUDE.md](CLAUDE.md) | Regole operative condensate + ricette per agenti/nuovi contributor | Prima di scrivere codice, come riassunto di STEERING |
| [INDEX.md](INDEX.md) | Questo file: mappa dei file | Non sai **dove** sta una cosa |
| [analyze_plan.md](analyze_plan.md) | Roadmap di evoluzione strutturale (Fasi 0/1a/1b **fatte**, 2–5 aperte) | Vuoi sapere cosa è pianificato e perché |
| [docs/product-shortlist.md](docs/product-shortlist.md) | Solo direzioni di prodotto approvate; le idee non scelte restano fuori | Devi sapere cosa vale ancora come decisione futura |
| [catalogs/README.md](catalogs/README.md) | Formato dei CSV (trofei, ranghi, cosmetici, consumabili, categorie) | Devi aggiungere contenuti senza toccare codice |
| [docs/superpowers/specs/](docs/superpowers/specs/) | Design approvati dei giochi «indovina» e di 20 Domande (2026-08-12) | Serve il *perché* dietro i motori di gioco |
| [docs/superpowers/plans/](docs/superpowers/plans/) | Piano di implementazione task-by-task degli stessi giochi | Ricostruire la sequenza di lavoro |

### Sezioni di STEERING.md

| § | Argomento | § | Argomento |
| --- | --- | --- | --- |
| 0 | Layout src-layout | 14 | Rate limiting + anti-flood statico |
| 1 | Stack, versioni, **gate statici ruff/mypy** | 15 | FSM states attivi |
| 2 | Config (`Settings`) | 16 | Comandi registrati (privato/gruppo/admin) |
| 3 | Schema DB + regole di schema | 17 | Modulo intrattenimento AI |
| 4 | **DI negli handler** (`db_session`) | 18 | Suite admin · 18.1 dashboard · 18.2 hub eventi |
| 5 | **I service non committano** | 19 | Quiz mode (podio, premi) |
| 6 | **Ordine middleware** | 19.b | Guess The Game & Sound Quest (giudice AI) |
| 7 | **Ordine router** · 7.a handler errori | 20 | Scheduling |
| 8 | Filtri admin + gating di router | 21 | Docker & Compose |
| 9 | Deep-link pattern | 22 | **Regole di sviluppo (1–27)** |
| 10 | Betting payout · 10.a `/daily` | 23 | Test suite |
| 11 | Locanda: 11.a cosmetici · 11.b consumabili | 24 | **Checklist pre-PR** |
| 12 | Trofei · 12.1 XP · 12.2 cataloghi CSV | 25 | Backup & export stato |
| 13 | `group_registry` (id gruppo effettivo) | 26 | Alert al maintainer |

---

## 2. Albero del sorgente

```text
src/                                  # src-layout: i package restano top-level all'import
├── main.py                           # bootstrap: cataloghi → registry eventi → middleware → router → polling
├── config_data/config.py             # Settings (pydantic-settings), unica istanza `settings`
├── database/
│   ├── models.py                     # tutte le tabelle (DeclarativeBase) + enum
│   └── connection.py                 # engine async, session maker, _MIGRATIONS (DDL idempotente, solo Postgres)
├── middlewares/                      # in ordine di esecuzione: rate_limit → db → ban_guard → group_guard
├── filters/admin_filter.py           # is_admin + IsAdminFilter / IsAdminCallbackFilter
├── exceptions/economy.py             # eccezioni di dominio (saldo, daily, scommesse)
├── handlers/                         # layer aiogram — l'unico che committa
│   ├── __init__.py                   # ROUTERS: ordine di registrazione (è comportamento, non bookkeeping)
│   ├── errors.py                     # dp.errors: fallback globale (nessun router)
│   ├── event_types/                  # registro dei tipi-evento (unico punto di estensione)
│   ├── quiz/                         # pacchetto: _shared · creation · editing · lifecycle · play · trying
│   └── guess/                        # pacchetto: _shared · creation · editing · lifecycle · play
├── services/                         # logica DB-side, **senza commit**
├── keyboards/                        # builder di InlineKeyboardMarkup
└── utils/                            # helper puri (testo, cooldown, tempo, IO atomico)
```

### `src/handlers/` — layer Telegram

| File | Ruolo |
| --- | --- |
| `__init__.py` | Tupla `ROUTERS` + `register(dp)`. Due invarianti: `admin_betting` **prima** di `betting`, `common` **ultimo** |
| `errors.py` | `on_error` su `dp.errors`: logga con contesto, avvisa l'utente, silenzia i rifiuti benigni di Telegram |
| `common.py` | `/start` (dispatch dei deep-link), `/profilo`, `/comandi`, fallback |
| `onboarding.py` | Primo contatto: accettazione regole prima di ogni altra cosa |
| `economy.py` | `/saldo` `/storico` `/daily` `/trasferisci` |
| `betting.py` | Lato giocatore: `/scommesse`, `/crea_scommessa`, piazzamento |
| `admin_betting.py` | Pannello admin scommesse (`admin_bet:*`) — chiude con un catch-all deny |
| `admin.py` | Valuta, moderazione, dossier, `/stats`, `/audit`, warn/strike |
| `admin_dashboard.py` | Dashboard a bottoni (`adm:*`) |
| `events.py` | Hub eventi (`ev:*`): crea → avvia ora / programma. **Zero `if/elif` per tipo** |
| `event_types/` | `base.py` (protocollo `EventType` + registro), `quiz_type`, `poll_type`, `bet_type`, `guess_type` |
| `quiz/` | Quiz: creazione FSM, editor, lifecycle, gioco in privato, dry-run admin |
| `guess/` | Guess The Game (immagine) e Sound Quest (audio): un motore, due giochi |
| `schedule.py` | `/programma`, `/programmati` + `scheduler_loop` (task in-process) |
| `shop.py` | Locanda: tag cosmetici + consumabili |
| `badges.py` | `/trofei`, `/catalogo_trofei` |
| `leaderboard.py` | `/classifiche` con switcher inline (monete · XP · trofei) |
| `fun_ai.py` | Comandi comici one-shot + `/alduino` |
| `group_events.py` | Migrazioni chat, `chat_member`/`my_chat_member`, invalidazione cache admin |
| `backup.py` | `/backup`, `/esporta` (admin) |
| `help_content.py` | Registro comandi: sorgente unica di `/comandi` e `/spiega_comando` |
| `_mentions.py` `_privacy.py` `_targeting.py` `_trophy_announce.py` | Helper condivisi (menzioni, dati personali fuori dal gruppo, risoluzione bersaglio, annuncio trofei) |

### `src/services/` — logica DB-side (nessun commit, §5)

| File | Superficie principale |
| --- | --- |
| `economy_service.py` | `credit` `debit` `transfer` `claim_daily` `get_balance` `get_history` |
| `bet_service.py` | `create_event` `place_bet` `lock_event` `resolve_event` `cancel_event` `compute_payout_preview` `arm_close` |
| `xp_service.py` | **Unico** mutatore di `User.xp`: `grant_xp` `set_xp` `airdrop_xp` `level_for_xp` `rank_for_xp` `leaderboard_xp` |
| `badge_service.py` | `sync_trophies` `award_badge` `check_and_award_milestones` `describe_condition` `leaderboard_trophies` |
| `progress_service.py` | `record_podium` / `record_event` + i relativi conteggi (alimentano i trofei) |
| `quiz_service.py` | Quiz: creazione, domande, risposte, `podium`, `award_prizes`, `claim_close` |
| `guess_service.py` | Round, sessioni per giocatore, tentativi, `standings`, `award_prizes` |
| `guess_judge.py` | `normalize` `aliases_of` `judge` — verdetto in 4 stadi, dal più economico al più costoso |
| `prizes.py` | `participation_floor` `consolation_amounts` — schedule premi condiviso da ogni gioco col podio |
| `poll_service.py` | Template di sondaggio pre-creati |
| `shop_service.py` | Cosmetici: acquisto, tag attivi multipli, `apply_cosmetic` |
| `consumable_service.py` | Consumabili ripetibili (menu Locanda) + conteggi per i trofei |
| `catalog_loader.py` | CSV → trofei/ranghi/cosmetici/consumabili/categorie. Letti **solo all'avvio**, fallback ai default |
| `admin_service.py` | `set_balance` `mass_credit` `get_dossier` `search_users` warn/ban `log_action` `recent_actions` |
| `moderation_service.py` | Wrapper Bot API (ban/kick/mute/`parse_duration`) — **non tocca il DB** |
| `schedule_service.py` | `parse_run_at` `schedule_task` `due_tasks` `mark_done/failed` — timestamp UTC naive |
| `group_registry.py` | Id gruppo **effettivo** (sopravvive alle migrazioni chat) + `send_group_message` |
| `ai_service.py` | Client Groq async: `generate_completion` (intrattenimento) e il giudizio structured-output |
| `ai_game_service.py` | Aggregate persistente di 20 Domande, claim dei turni e rotazione catalogo |
| `structured_ai.py` | Porta JSON Schema + adapter Gemini per i giochi AI persistenti |
| `igdb_catalog.py` | OAuth/fetch IGDB, quality gate, cache DB atomica e loop di sincronizzazione |
| `twenty_questions_catalog.py` | CSV e 24 dossier integrati usati come fallback di 20 Domande |
| `backup/state_export.py` | Export/import logico dell'intero DB (streaming, atomico) |
| `backup/chat_archive.py` | Archivio incrementale della chat via MTProto/Telethon (opt-in) |
| `backup/loop.py` | Driver in background dei due precedenti — non blocca mai l'event loop |

### `src/middlewares/`, `src/filters/`, `src/utils/`, `src/keyboards/`

| File | Ruolo |
| --- | --- |
| `middlewares/rate_limit.py` | Finestra scorrevole: 12 update / 10 s per utente |
| `middlewares/db_middleware.py` | Inietta `db_session` + upsert di `User`/`Wallet` |
| `middlewares/ban_guard.py` | `is_banned` ⇒ update **scartato in silenzio**, ovunque |
| `middlewares/group_guard.py` | In privato risponde solo ai membri del gruppo (cache 300 s, fail-open) |
| `filters/admin_filter.py` | `is_admin` (ADMIN_IDS **o** admin Telegram del gruppo), fail-closed, guardia «tutti admin» |
| `utils/text.py` | `esc` (escaping HTML **obbligatorio**), `chunk_blocks`, `format_duration` |
| `utils/daytime.py` | Sorgente unica di «cos'è un giorno»: `local_day`, `next_local_midnight` |
| `utils/cooldown.py` | Cooldown anti-spam per (bucket, utente) |
| `utils/static_reply.py` | Anti-flood dei comandi statici in gruppo |
| `utils/atomic_io.py` | Scritture crash-safe (tmp+fsync+replace), sha256, membri gzip |
| `keyboards/*_kb.py` | `betting` · `admin_betting` · `admin_dashboard` · `shop` · `onboarding` · `common` |

---

## 3. Schema DB (`src/database/models.py`)

| Tabella | Contenuto |
| --- | --- |
| `users` | Anagrafica, XP, streak, contatori trofei, tag attivi, `is_banned`, cap XP giornaliero |
| `wallets` | Saldo CoInn (BIGINT) |
| `ledger` | Ogni movimento di denaro (tipo, importo, riferimento) |
| `badges` / `user_badges` | Catalogo trofei (rarità, condizione, `condition_param`, hidden) e assegnazioni |
| `betting_events` / `betting_options` / `user_bets` | Scommesse: evento, esiti, puntate |
| `quizzes` / `quiz_questions` / `quiz_answers` | Quiz e partite |
| `guess_rounds` / `guess_sessions` / `guess_attempts` | Giochi «indovina»: round, run per giocatore, tentativi (+cache verdetti) |
| `poll_templates` | Sondaggi pre-creati |
| `game_podiums` / `user_progress_events` | Metriche generiche per i trofei (podi, «fatto X volte») |
| `shop_purchases` | Acquisti cosmetici (`tag_*`) e consumabili (`cons_*`) |
| `warnings` / `admin_actions` | Moderazione e audit trail |
| `scheduled_tasks` | Azioni future eseguite dallo scheduler in-process |
| `bot_state` | Key-value di runtime (es. id gruppo effettivo) |

Le migrazioni sono una lista di DDL **idempotente** in `database/connection.py` (`_MIGRATIONS`),
eseguita solo su PostgreSQL. Non c'è Alembic.

---

## 4. Test (`tests/`)

- **2121 test** raccolti — 2091 passano, 30 marcati `pg` skippano senza Postgres (~40 s).
  `pytest` con `asyncio_mode=auto`, SQLite in-memory, un DB nuovo per test.
- `tests/unit/` — funzioni pure, keyboard, middleware, parser, cooldown, ordine router.
- `tests/integration/` — service + flussi handler end-to-end con Telegram finto.
- Marker **`pg`**: richiedono un PostgreSQL vero (`TEST_PG_URL`), altrimenti **skippano**.
  Coprono `_MIGRATIONS` e le gare sul path denaro (`test_money_concurrency_pg.py`,
  `test_migrations_pg.py`) — cose non esprimibili su SQLite.
- Fixture (`tests/conftest.py`): `engine` · `session` · `user_factory` · `seeded_session`;
  lato Postgres `pg_engine` · `pg_sessions` (factory) · `pg_session` · `pg_user_factory`.
- Gate coverage: `fail_under = 99` in `pyproject.toml` — **ratchet**, si alza mai si abbassa.

---

## 5. Infrastruttura e tooling

| File | Ruolo |
| --- | --- |
| `pyproject.toml` | pytest (`pythonpath=["src"]`, marker `pg`), coverage (`fail_under=99`), **ruff** (`E9,F,B,ASYNC`), **mypy** (services/database/utils/config_data/filters) |
| `requirements.txt` / `requirements-dev.txt` | Runtime / + pytest, aioresponses, ruff 0.16.0, mypy 2.3.0 |
| `Dockerfile` | python:3.12-slim, utente non root, `python src/main.py` |
| `docker-compose.yml` | Servizi `db` (postgres:16) · `redis` · `bot` · `watchtower`; volumi `postgres_data`, `bot_backups` |
| `.github/workflows/tests.yml` | pytest **con Postgres di servizio** + coverage, poi ruff, poi mypy |
| `.github/workflows/docker-image.yml` | `test` ⇒ immagini `-test`; `main` ⇒ `latest`; tag `v*` ⇒ release semver |
| `.github/workflows/compose-artifact.yml` | Pubblica il compose come artifact di deploy |
| `.env.example` | Tutte le variabili con commenti |
| `scripts/export_state.py` | Snapshot totale del DB (`state-*.jsonl.gz`) |
| `scripts/import_state.py` | Ripristino post-migrazione (`--mode empty\|replace`) |
| `scripts/login_telethon.py` | Login MTProto una tantum → `TELEGRAM_SESSION` (**credenziale sensibile**) |
| `catalogs/*.example.csv` | Template dei cataloghi: copiali in `data/` senza `.example` |

---

## 6. Dove guardo se…

| Devo… | Guarda |
| --- | --- |
| aggiungere un comando utente | `handlers/<area>.py` + `main._PRIVATE_COMMANDS`/`_GROUP_COMMANDS` + `help_content.py` |
| aggiungere un comando admin | stesso handler + `main._ADMIN_EXTRA_COMMANDS` (**mai** nelle liste pubbliche) |
| cambiare come si guadagnano XP | `services/xp_service.py` (unico mutatore) + `config_data/config.py` |
| aggiungere un trofeo | `catalogs/trophies.example.csv` + `data/trophies.csv`; condizione nuova ⇒ `badge_service` |
| aggiungere un cosmetico o un consumabile | `catalogs/shop_cosmetics.example.csv` / `consumables.example.csv` — **mai** in codice |
| aggiungere un tipo di evento | spec in `handlers/event_types/` + una riga in `register_builtin()`. Nient'altro |
| toccare denaro | `services/economy_service.py` + STEERING §22 regola 22 (**si decide in SQL, non in Python**) |
| capire perché un callback non risponde | ordine in `handlers/__init__.py` (`ROUTERS`) + `tests/unit/test_router_order.py` |
| capire l'id del gruppo a runtime | `services/group_registry.py` (**mai** `settings.group_id`) |
| aggiungere una colonna | `database/models.py` **+** voce in `_MIGRATIONS` (`database/connection.py`) |
| capire il giudizio delle risposte libere | `services/guess_judge.py` + STEERING §19.b |
| ripristinare dati dopo una migrazione DB | `scripts/import_state.py` + STEERING §25 |
