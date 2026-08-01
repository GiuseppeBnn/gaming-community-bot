# CLAUDE.md

Istruzioni operative per Claude Code (e per chiunque scriva codice qui).

**Gerarchia dei documenti:** [STEERING.md](STEERING.md) è **normativo** e vince su questo file.
Qui c'è il condensato di ciò che serve subito; ogni regola rimanda al paragrafo che la spiega.
Per trovare un file usa [INDEX.md](INDEX.md). Per far girare il bot usa [README.md](README.md).

---

## Cos'è

Bot Telegram per una community di gaming (italiano lato utente, inglese nei commenti/log).
Economia in CoInn, scommesse, quiz e giochi «indovina» a premi, XP/ranghi/trofei, negozio,
suite admin, moderazione, backup.

**Stack vincolante** (§1): Python **3.12** · aiogram **3.13.1** (mai 2.x) · SQLAlchemy 2.0 async ·
pydantic-settings 2.x · PostgreSQL 16/asyncpg in prod, SQLite/aiosqlite in dev e test ·
aiohttp per le chiamate LLM (**mai** HTTP bloccante) · Groq (opzionale).

---

## Comandi

```bash
source .venv/bin/activate        # Python 3.12

pytest                           # 2092 test (2062 passed, 30 `pg` skipped), ~40s, nessun token reale
pytest tests/unit/               # solo unit
pytest -m "not pg"               # esplicitamente senza Postgres
pytest --cov=src --cov-report=term-missing   # gate: fail_under = 99

ruff check src/ tests/           # gate CI
mypy                             # gate CI (config in pyproject.toml)

python src/main.py               # avvio locale
docker compose up -d --build     # stack completo (db, redis, bot, watchtower)
```

Test `pg` (gare sul denaro + DDL di migrazione): serve un Postgres usa-e-getta.
Senza `TEST_PG_URL` **skippano**; il nome del DB deve finire in `_test` (il fixture fa `drop_all`).

```bash
export TEST_PG_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test"
pytest -m pg -rxX
```

---

## Layout e import

**src-layout**: il codice sta in `src/`, i test nella root. I package restano **top-level
all'import** perché `src/` è sul path (`pythonpath = ["src"]` in `pyproject.toml`,
`python src/main.py` in avvio, `COPY src/ ./src/` nell'immagine).

```python
from config_data.config import settings      # ✅  non `from src.config_data...`
from services import economy_service
```

Convenzione: i path di modulo citati nei documenti (`handlers/quiz/play.py`) sono **relativi a
`src/`**; quelli di test/infra (`tests/…`, `.github/…`) alla root.

---

## Architettura in dieci righe

1. `main.py` fa il bootstrap: tabelle → migrazioni → cataloghi CSV → id gruppo effettivo →
   `event_types.register_builtin()` → middleware → router → `dp.errors` → polling + 2 task
   (scheduler, backup).
2. **Ordine middleware** (§6, non invertire): `RateLimit` → `DbSession` → `BanGuard` → `GroupGuard`.
3. **Ordine router** (§7): dichiarato **una volta** in `handlers/__init__.py` (`ROUTERS`) e
   asserito da `tests/unit/test_router_order.py`. Due invarianti: `admin_betting` prima di
   `betting`, `common` **ultimo**.
4. **handlers** = aiogram + presentazione + **commit**. **services** = SQL, **mai commit**.
5. `handlers/errors.py` è registrato su `dp.errors` (non è un router): logga con contesto e
   avvisa l'utente invece di lasciare il bot muto.

---

## Le regole che rompono qualcosa se violate

Elenco completo: **STEERING §22 (regole 1–27)** e **§24 (checklist pre-PR)**. Le essenziali:

1. **La sessione si inietta come `db_session`** (§4). `session: AsyncSession` non funziona e basta.
2. **I service non committano** (§5): committa l'handler. Unica eccezione documentata:
   `shop_service.record_purchase` / `mark_success` (audit trail separato).
3. **Denaro, XP e transizioni di stato si decidono in SQL, non in Python** (§22 regola 22).
   Il check va nella `WHERE`, l'aritmetica nella `SET` (`coins = coins + :delta`), `rowcount == 0`
   significa gara persa, sempre `.execution_options(synchronize_session=False)` seguito da
   `session.refresh(obj, [colonne])`.
   > Un lock **non** basta: con `expire_on_commit=False` un'entità già nella identity map viene
   > restituita **con i valori vecchi** anche sotto `with_for_update`. Leggi **colonne**
   > (`select(Wallet.coins)`), non entità. Mai `populate_existing=True`, mai `session.expire`
   > (in async diventano `MissingGreenlet`). Le guardie di regressione sono in
   > `tests/integration/test_money_concurrency_pg.py`.
4. **`User.xp` si muta solo via `xp_service`** (§12.1). Sorgenti nuove: classificale capped
   (soggette al tetto giornaliero) o uncapped (eventi admin-gated).
5. **Mai `settings.group_id` a runtime** (§13): usa `group_registry.get_group_id()`. Solo
   `config.py`, lo startup e `group_registry` toccano il setting.
6. **Escaping HTML obbligatorio** (§22 regola 20): ogni stringa user-controlled interpolata in un
   messaggio HTML passa da `utils.text.esc`. Service e DB restano raw — l'escaping è presentation
   layer. Testi dei bottoni inline e opzioni dei poll **non** sono HTML-parsed: niente `esc`.
7. **Check admin sempre via `filters.admin_filter.is_admin`** (§8), mai
   `user.id in settings.admin_ids`. I router 100% admin montano il filtro **alla radice**
   (`router.message.filter(...)`), perché gli handler guidati dal solo stato FSM non
   ri-controllerebbero nulla e lo stato FSM non ha TTL.
8. **Nuovi tipi-evento solo via registro** (§18.2): una spec `EventType` + una riga in
   `register_builtin()`. Vietato ramificare per tipo nell'hub o nello scheduler.
9. **Colonna nuova ⇒ voce in `_MIGRATIONS`** (`database/connection.py`), anche per un cambio di
   **tipo**. DDL idempotente, gira solo su PostgreSQL. Non c'è Alembic.
10. **Contenuti (trofei, ranghi, cosmetici, consumabili) si aggiungono nei CSV**, mai in codice
    (§12.2). I cataloghi si leggono **solo all'avvio**, con fallback ai default: non assumere mai
    che un file esista.
11. **Comandi admin mai in `_PRIVATE_COMMANDS`/`_GROUP_COMMANDS`**, solo in `_ADMIN_EXTRA_COMMANDS`
    e in `/help`.
12. **`from __future__ import annotations`** in ogni modulo.
13. **Azioni admin mutanti** → `admin_service.log_action` prima del commit, + guard self/bot-target
    per la moderazione.
14. **LLM**: l'intrattenimento passa da `ai_service.generate_completion`; un **giudizio** passa da
    `judge_equivalence` — temperature 0, schema `strict`, parse che rifiuta tutto ciò che non è un
    booleano. Un raise significa *non dimostrato corretto*, mai *corretto* (§19.b).

---

## Ricette

| Aggiungere… | Passi |
| --- | --- |
| un comando utente | handler in `handlers/<area>.py` → voce in `main._PRIVATE_COMMANDS`/`_GROUP_COMMANDS` → voce in `help_content.py` |
| un comando admin | idem, ma in `main._ADMIN_EXTRA_COMMANDS`; gate via `IsAdminFilter` |
| un deep-link | payload gestito in `common.cmd_start` (§9) |
| un tipo di evento | spec `EventType` in `handlers/event_types/` + riga in `register_builtin()`. Se due giochi differiscono per etichette e media, **parametrizza la spec** invece di duplicarla |
| un trofeo | riga nel CSV; condizione nuova ⇒ dispatch in `badge_service.check_and_award_milestones` + `catalog_loader.TROPHY_CONDITIONS` + `badge_service.describe_condition` |
| un cosmetico/consumabile | riga nel CSV (`tag_*` per i cosmetici, `cons_*` per i consumabili: namespace condiviso, chiavi disgiunte) |
| una sorgente XP | `xp_service.grant_xp` con la `XpSource` giusta + costanti in `config.py` |
| un metodo di service | + integration test (regola di §24) |

---

## Prima di consegnare

- [ ] `pytest` verde (2062 passed, 30 `pg` skipped senza Postgres) e coverage ≥ `fail_under`
- [ ] `ruff check src/ tests/` e `mypy` puliti — sono **a zero findings**: ogni segnalazione è una
      regressione nuova, non rumore preesistente
- [ ] `PYTHONPATH=src python -c "import main"` non esplode
- [ ] Checklist completa: **STEERING §24**

Se una modifica cambia un invariante documentato, **aggiorna STEERING.md nello stesso commit**.

---

## Note per l'agente

- Lingua: messaggi all'utente in **italiano**, commenti/log/nomi in **inglese**.
- Non introdurre dipendenze nuove per cose che stdlib o una dipendenza già presente coprono.
- Non aggiungere Alembic, `pg_insert`, o `get_settings()`: sono tre scelte già prese (§22).
- I test `pg` non girano di default: se tocchi il path denaro, dillo esplicitamente e indica come
  farli girare — un verde locale senza Postgres **non** copre le gare.
- `analyze_plan.md` è la roadmap: Fasi 0/1a/1b sono fatte, 2–5 no. Non trattarlo come stato attuale.
