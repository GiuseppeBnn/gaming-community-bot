# 🎮 Gaming Community Bot

Un bot Telegram production-grade per community di videogiocatori: economia con
ledger, **XP & progressione** (trofei stile PlayStation, ranghi, tag cosmetici),
scommesse stile Twitch, **classifiche multiple**, negozio di personalizzazioni,
onboarding interattivo, quiz a premi con podio, un modulo di intrattenimento AI e
una **dashboard admin a bottoni**. Trofei, ranghi e cosmetici sono
**personalizzabili via CSV** senza toccare il codice.

---

## Requisiti

- Python 3.11+
- Docker & Docker Compose (per il deploy)
- Un bot Telegram (crealo con [@BotFather](https://t.me/BotFather))

---

## Avvio Rapido (5 minuti)

### 1. Clona il progetto

```bash
git clone <repo-url>
cd gaming-community-bot
```

### 2. Crea il file `.env`

```bash
cp .env.example .env
```

Modifica `.env` con un editor:

```env
BOT_TOKEN=1234567890:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DB_URL=sqlite+aiosqlite:///./data/bot.db
GROUP_ID=-1001234567890
ADMIN_IDS=123456789
DAILY_REWARD_COINS=100
FSM_STORAGE=memory
```

> - `BOT_TOKEN` → da [@BotFather](https://t.me/BotFather) con `/newbot`.
> - `ADMIN_IDS` → il tuo ID da [@userinfobot](https://t.me/userinfobot).
> - `GROUP_ID` → l'ID del **supergruppo**, in forma **negativa `-100…`** (un ID positivo
>   rompe il riconoscimento admin via Telegram).

### 3. Avvio locale (senza Docker)

Il codice applicativo vive sotto `src/` (src-layout): si avvia con `python src/main.py`.

```bash
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
mkdir -p data
python src/main.py
```

Output atteso:

```text
INFO - Tabelle DB pronte.
INFO - Catalogo badge caricato.
INFO - FSM storage: memory
INFO - Bot avviato — polling in corso.
```

### 4. Avvio con Docker

```bash
docker compose up --build         # build + avvio
docker compose up -d --build      # in background
docker compose logs -f bot        # log
docker compose down               # stop
```

Per usare l'immagine pubblicata da CI invece di buildare in locale, imposta
`BOT_IMAGE` (vedi sotto) e fai `docker compose pull`.

---

## Testing

```bash
pip install -r requirements-dev.txt

pytest                                   # tutta la suite (330 test, ~5s)
pytest --cov=src --cov-report=term-missing
pytest tests/unit/                       # solo unit (senza DB)
pytest tests/integration/                # solo integration (SQLite in-memory)
```

I test non richiedono token Telegram reali né un server in esecuzione: le env vars
necessarie sono impostate da `tests/conftest.py`, e `pyproject.toml` espone `src/`
al path di import (`pythonpath = ["src"]`).

---

## CI/CD (GitHub Actions)

Tre workflow in `.github/workflows/`:

| Workflow | Trigger | Cosa fa |
| --- | --- | --- |
| **`tests.yml`** | ogni `push` e `pull_request` (tutti i branch) | esegue l'intera suite con coverage. È anche `workflow_call` (riusabile). |
| **`docker-image.yml`** | `push` che tocca `src/**`, `requirements.txt`, `Dockerfile` | **prima** esegue i test (gate), **poi** builda e pubblica l'immagine su **GHCR**. |
| **`compose-artifact.yml`** | `push` che tocca `docker-compose.yml` | valida il compose e lo pubblica come **artifact** (con `.env.example`) — nessuna immagine. |

In pratica: **ogni modifica al sorgente → nuova immagine GHCR** (solo se i test passano),
**ogni modifica al compose → solo un artefatto compose**, e **il sorgente passa sempre i
test a ogni push**.

**Usare l'immagine pubblicata:**

```bash
docker pull ghcr.io/<owner>/gaming-community-bot:latest
# oppure nel .env:  BOT_IMAGE=ghcr.io/<owner>/gaming-community-bot:latest
docker compose pull && docker compose up -d
```

Tag immagine: `latest` (solo sul branch di default), nome del branch e SHA breve.
L'immagine è **multi-arch** (`linux/amd64` + `linux/arm64`): Docker tira automaticamente la
variante giusta per la tua macchina (incluse Apple Silicon e i server ARM).

---

## Comandi del Bot

### Utenti

| Comando | Descrizione |
| --- | --- |
| `/start` | Onboarding (primo accesso) o menu principale; gestisce i deep-link |
| `/profilo` · `/saldo` · `/storico` | Profilo, saldo, cronologia movimenti |
| `/daily` | Premio giornaliero (ogni 20h) |
| `/trasferisci @user importo` | Trasferisci CoInn |
| `/scommesse` · `/crea_scommessa` | Vedi/crea scommesse |
| `/traguardi` · `/catalogo_badge` | I tuoi trofei (per rarità) + rango / catalogo |
| `/classifiche` | Classifiche: 💰 ricchezza · ⚡ XP · 🏆 trofei (switcher inline) |
| `/negozio` | Compra personalizzazioni (tag cosmetici) con i CoInn — apre ovunque |
| `/quiz` (gioco) | Partecipa ai quiz in chat privata |
| AI (in gruppo, in reply): `/maestro` `/complotto` `/difendi` `/accusa` `/drama` `/dialetto` `/insulta` | Intrattenimento AI |

### Admin (`ADMIN_IDS` **o** creator/admin del gruppo `GROUP_ID`)

| Comando | Descrizione |
| --- | --- |
| **`/admin`** | **Dashboard a bottoni** (stats, classifica, audit, quiz, scommesse, utenti, economia) |
| `/credita` · `/addebita` · `/setsaldo` · `/airdrop` · `/saldo_di` | Valuta |
| `/dai_xp` · `/set_xp` | Assegna / imposta gli XP di un utente (gestione XP solo admin) |
| `/ban` · `/sban` · `/kick` · `/mute [10m]` · `/unmute` | Moderazione (reply o @user/ID) |
| `/warn [motivo]` · `/warns` · `/unwarn` | Warn/strike (auto mute/ban a soglia) |
| `/info` · `/cerca` · `/classifica` · `/stats` · `/audit` | Info & dossier |
| `/crea_quiz` · `/quiz` · `/avvia_quiz <id>` · `/chiudi_quiz <id>` | Quiz |
| `/gestisci_scommesse` · `/sondaggio` · `/programma` · `/programmati` | Scommesse, sondaggi, scheduling |

> Gli admin possono fare **tutto da `/admin` con i soli bottoni**, senza digitare comandi.

### Quiz a premi

Creazione guidata (`/crea_quiz` o dalla dashboard) con tasti **« Indietro »**, premi
**personalizzabili** per 1°/2°/3°, un **premio di consolazione a scendere** con **minimo
garantito** per *tutti* i finisher, e una schermata di riepilogo prima di pubblicare. Si
gioca in chat privata; alla chiusura viene pubblicato il **podio** con i premi.

---

## XP, Trofei, Ranghi e Tag

Le **monete** (CoInn) sono spendibili e farmabili; gli **XP** sono una metrica di
**merito separata** e **non farmabile**:

- **Si guadagnano XP** dagli **eventi curati dagli admin** (quiz, scommesse risolte) e
  dall'**assegnazione manuale** dell'admin (`/dai_xp`, `/set_xp`, Airdrop XP), più una
  **piccola quota di partecipazione giornaliera con tetto** (`/daily`, vittoria scommessa).
  Il tetto è applicato lato server (`XP_DAILY_PARTICIPATION_CAP`), così nessuno può farmare XP.
- **Trofei** (stile PlayStation): achievement con **rarità** Bronzo/Argento/Oro/Platino,
  sbloccati da condizioni (saldo, streak, scommesse, **XP**…). Li vedi con `/traguardi`
  (raggruppati per rarità) e `/catalogo_badge`.
- **Ranghi**: titoli sbloccati automaticamente al crescere degli XP (es. Novizio → Veterano
  → Leggenda), mostrati sul profilo; il rank-up viene annunciato.
- **Tag cosmetici**: flair acquistabili nel **negozio** con le monete (`/negozio`, apre
  ovunque). Sono **solo estetici** — nessun permesso reale, nessuna escalation.

### Personalizzare con i CSV

Nomi, soglie, rarità e prezzi sono **modificabili senza ricompilare**, via CSV nella
cartella dati montata (`CATALOG_DIR`, default `data/`):

```bash
cp catalogs/trophies.example.csv        data/trophies.csv
cp catalogs/ranks.example.csv           data/ranks.csv
cp catalogs/shop_cosmetics.example.csv  data/shop_cosmetics.csv
# edita data/*.csv e RIAVVIA il bot (i cataloghi si leggono all'avvio)
```

Se un file manca o è malformato, il bot usa i **default integrati** (le righe non valide
vengono saltate e segnalate nei log). Vedi [catalogs/README.md](catalogs/README.md) per il formato.

---

## Struttura del Progetto

```text
gaming-community-bot/
├── src/                              # 📦 sorgente applicativo (src-layout)
│   ├── main.py                       # bootstrap: middleware, router, polling, scheduler
│   ├── config_data/config.py         # Settings (pydantic-settings)
│   ├── database/{connection,models}.py
│   ├── services/                     # logica DB-side (no commit): economy, xp, badge, bet,
│   │                                 #   shop, quiz, admin, moderation, schedule, ai,
│   │                                 #   catalog_loader (CSV → trofei/ranghi/cosmetici)
│   ├── handlers/                     # common, onboarding, economy, betting, badges,
│   │                                 #   leaderboard, shop, quiz, schedule, fun_ai, admin,
│   │                                 #   admin_betting, admin_dashboard
│   ├── keyboards/                    # InlineKeyboard builders (incl. admin_dashboard_kb)
│   ├── filters/admin_filter.py       # IsAdminFilter / IsAdminCallbackFilter / is_admin
│   ├── exceptions/economy.py
│   └── middlewares/{db_middleware,group_guard,rate_limit}.py
├── catalogs/                         # template CSV (trofei/ranghi/cosmetici) + README
├── tests/{conftest.py, unit/, integration/}
├── .github/workflows/{tests,docker-image,compose-artifact}.yml
├── Dockerfile · .dockerignore · docker-compose.yml
├── pyproject.toml                    # pytest (pythonpath=src) + coverage (source=src)
├── requirements.txt · requirements-dev.txt
├── .env.example · STEERING.md · README.md
└── data/                             # volume runtime (sqlite); .gitkeep tracciato
```

---

## Configurazione (estratto)

| Variabile | Default | Note |
| --- | --- | --- |
| `BOT_TOKEN` | — | obbligatorio |
| `DB_URL` | `sqlite+aiosqlite:///./data/bot.db` | in produzione: PostgreSQL |
| `GROUP_ID` | `0` | supergruppo in forma `-100…`; `0` = guard disattivato |
| `ADMIN_IDS` | `[]` | lista separata da virgole |
| `FSM_STORAGE` | `memory` | `redis` in produzione (`REDIS_URL`) |
| `GROQ_API_KEY` | — | modulo AI (opzionale) |
| `CATALOG_DIR` | `data` | cartella con i CSV opzionali (trofei/ranghi/cosmetici) |
| `XP_DAILY_PARTICIPATION_CAP` | `50` | tetto XP farmabili per utente al giorno |
| `XP_PER_DAILY_CLAIM` | `10` | XP (capped) sul `/daily` |
| `XP_PER_BET_WON` | `15` | XP (capped) su una scommessa vinta |
| `BOT_IMAGE` | `gaming-community-bot:local` | immagine usata dal compose (override → GHCR) |

PostgreSQL e Redis sono già pronti nel `docker-compose.yml`.

---

## Aggiungere un Nuovo Handler

1. Crea `src/handlers/nuovo.py`:

```python
from aiogram import Router
from aiogram.filters.command import Command
from aiogram.types import Message

router = Router()

@router.message(Command("nuovo"))
async def cmd_nuovo(message: Message) -> None:
    await message.answer("Ciao dal nuovo handler!")
```

1. Registralo in `src/main.py` (prima di `common.router`):

```python
from handlers import nuovo
...
dp.include_router(nuovo.router)
dp.include_router(common.router)
```

Vedi **STEERING.md** per regole vincolanti (DI negli handler, no-commit nei service,
ordine middleware/router, filtri admin, ecc.).

---

## FAQ

**Il bot non risponde a `/start`** → controlla `BOT_TOKEN` e che non giri già un altro processo.

**`ValidationError: bot_token field required`** → manca `.env` nella root del progetto.

**Errore `aiosqlite` al primo avvio** → crea la cartella dati: `mkdir -p data`.

**Resettare il database (sqlite)** → ferma il bot, cancella `data/bot.db`, riavvia.

**Un admin non viene riconosciuto** → verifica che `GROUP_ID` sia l'ID del supergruppo in
forma **negativa `-100…`** e che il bot sia nel gruppo.

**Log in Docker** → `docker compose logs -f bot`.

---

## Licenza

MIT — Usalo, modificalo, distribuiscilo liberamente.
