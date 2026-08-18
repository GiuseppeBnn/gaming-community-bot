# 🎮 Gaming Community Bot

Un bot Telegram production-grade per community di videogiocatori: economia con
ledger, **XP & progressione** (trofei stile PlayStation, ranghi, tag cosmetici),
scommesse stile Twitch, **classifiche multiple**, **La Locanda** (negozio di
personalizzazioni + menù di consumabili da collezionare), onboarding interattivo,
quiz a premi con podio, un modulo di intrattenimento AI e una **dashboard admin a
bottoni**. Trofei, ranghi, cosmetici e consumabili sono **personalizzabili via CSV**
senza toccare il codice.

---

## Requisiti

- Python 3.12
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
python3.12 -m venv .venv
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

pytest                                   # tutta la suite (~776 test, ~25s)
pytest --cov=src --cov-report=term-missing
pytest tests/unit/                       # solo unit (senza DB)
pytest tests/integration/                # solo integration (SQLite in-memory)

ruff check src/                          # lint (gate CI)
mypy                                     # type check (gate CI)
```

I test non richiedono token Telegram reali né un server in esecuzione: le env vars
necessarie sono impostate da `tests/conftest.py`, e `pyproject.toml` espone `src/`
al path di import (`pythonpath = ["src"]`).

La coverage ha un **ratchet**: `fail_under = 59` in `pyproject.toml`. Si alza, non si abbassa.

### Test su PostgreSQL reale (opzionali, marker `pg`)

Una ventina di test richiede un Postgres vero, perché due cose non sono esprimibili su SQLite:
`SELECT ... FOR UPDATE` è un no-op, e l'engine in-memory dà a ogni sessione la stessa connessione
(quindi la stessa transazione), rendendo impossibile scrivere una gara a due sessioni. Coprono le
migrazioni DDL — che girano in produzione a ogni deploy — e la concorrenza sul path denaro.

**Senza `TEST_PG_URL` questi test si skippano**, quindi il run normale non richiede Docker.

```bash
docker run -d --name gcb-pg-test -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=gamingbot_test -p 5433:5432 postgres:16-alpine
export TEST_PG_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test"
pytest -m pg -rxX
```

> ⚠️ Il fixture ricrea lo schema da zero (`drop_all`): punta `TEST_PG_URL` **solo** a un database
> usa-e-getta. Rifiuta qualsiasi nome che non finisca in `_test`, perché quello del compose si
> chiama `gamingbot` — a un carattere di distanza.

Queste gare sono nate come `xfail(strict=True)`, una per ogni difetto di concorrenza misurato sul
path denaro. Sono state corrette una alla volta e **oggi sono tutte verdi**: ognuna è la guardia di
regressione del proprio sito. Se una torna rossa, una decisione sul denaro è tornata in Python
invece che in SQL — vedi la regola 22 di `STEERING.md`.

---

## CI/CD (GitHub Actions)

Tre workflow in `.github/workflows/`:

| Workflow | Trigger | Cosa fa |
| --- | --- | --- |
| **`tests.yml`** | ogni `push` e `pull_request` (tutti i branch) | suite completa con coverage (+ un service **`postgres:16-alpine`** per i test `pg`), poi **`ruff`** e **`mypy`** come gate. È anche `workflow_call` (riusabile). |
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

**Auto-update (Watchtower):** il compose include un servizio `watchtower` che ogni
**10 min** controlla se il tag in esecuzione (`BOT_IMAGE`, es. `…:latest-test`) punta a
un **digest** nuovo nel registry. Se sì → pull, stop graceful del bot, ricreazione con la
stessa config, restart, e **rimozione della vecchia immagine** (`--cleanup` → niente
accumulo di versioni). Aggiorna **solo** il `bot` (scope via label
`com.centurylinklabs.watchtower.enable=true`); db/redis restano intatti.

- **Auth:** il package GHCR è **pubblico** → nessuna credenziale necessaria. (Se lo rendi
  privato, monta `~/.docker/config.json:/config.json:ro` nel servizio `watchtower` dopo un
  `docker login ghcr.io` sull'host.)
- **Rollback manuale:** punta `BOT_IMAGE` al tag immutabile precedente
  (`…:sha-XXXXXXX-test`) e `docker compose up -d`.
- **Intervallo:** `WATCHTOWER_POLL_INTERVAL` (secondi) nel compose.

---

## Comandi del Bot

### Utenti

| Comando | Descrizione |
| --- | --- |
| `/start` | Onboarding (primo accesso) o menu principale; gestisce i deep-link |
| `/profilo` · `/saldo` · `/storico` | Profilo, saldo, cronologia movimenti |
| `/daily` | Premio giornaliero: si azzera a **mezzanotte locale**, con un gap minimo di 6h dall'ultima riscossione |
| `/trasferisci @user importo` | Trasferisci CoInn |
| `/scommesse` · `/crea_scommessa` | Vedi/crea scommesse |
| `/traguardi` · `/catalogo_badge` | I tuoi trofei (per rarità) + rango / catalogo |
| `/classifiche` | Classifiche: 💰 ricchezza · ⚡ XP · 🏆 trofei (switcher inline) |
| `/locanda` (alias `/negozio`) | 🍺 La Locanda: tag cosmetici + 🍖 menù di consumabili (riempiono la 🎒 dispensa e sbloccano trofei) |
| `/quiz` (gioco) | Partecipa ai quiz in chat privata |
| `/d20` | Tira un d20: Alduino risponde soltanto con un numero da 1 a 20 |
| AI (in gruppo, in reply): `/maestro` `/complotto` `/difendi` `/accusa` `/drama` `/dialetto` `/insulta` | Intrattenimento AI |
| `/alduino <messaggio>` | Parla con Alduino; dopo la prima risposta basta rispondere direttamente ai suoi messaggi |

### Admin (`ADMIN_IDS` **o** creator/admin del gruppo `GROUP_ID`)

| Comando | Descrizione |
| --- | --- |
| **`/admin`** | **Dashboard a bottoni** (stats, classifica, audit, 🎬 eventi, utenti, economia) |
| `/credita` · `/addebita` · `/setsaldo` · `/airdrop` · `/saldo_di` | Valuta |
| `/dai_xp` · `/set_xp` | Assegna / imposta gli XP di un utente (gestione XP solo admin) |
| `/ban` · `/sban` · `/kick` · `/mute [10m]` · `/unmute` | Moderazione (reply o @user/ID) |
| `/warn [motivo]` · `/warns` · `/unwarn` | Warn/strike (auto mute/ban a soglia) |
| `/info` · `/cerca` · `/classifica` · `/stats` · `/audit` · `/lista_ranghi` | Info & dossier |
| `/crea_quiz` · `/quiz` · `/avvia_quiz <id>` · `/chiudi_quiz <id>` | Quiz |
| `/gestisci_scommesse` · `/sondaggio` · `/programma` · `/programmati` | Scommesse, sondaggi, scheduling |
| `/eventi` | Hub privato: crea, gestisce, avvia subito o programma gli eventi disponibili |

> Gli admin possono fare **tutto da `/admin` con i soli bottoni**, senza digitare comandi.

### Alduino conversazionale

`/alduino` ha una corsia provider indipendente. La configurazione consigliata usa
DeepSeek V4 Flash via OpenRouter; Gemini e Groq restano selezionabili e Groq può
fare da fallback operativo. Dopo la prima risposta non serve ripetere il comando:
una normale risposta Telegram a un messaggio del bot continua il ramo corretto.

Alduino ora capisce anche ciò che si stava dicendo nel gruppo: combina il ramo dei
reply, gli ultimi messaggi ordinari, il catalogo dei comandi e gli eventi realmente
aperti/in programma. Non spedisce l'archivio intero solo perché un modello accetta
1M token: il rolling context è configurabile e limitato per righe e caratteri, così
rumore, latenza e costo restano bassi. Il transcript locale viene potato
automaticamente; al provider arrivano nomi visualizzati e testo, non i Telegram ID.

La corsia contestuale forza `zdr=true` e `data_collection=deny` e contiene solo
modelli DeepSeek con endpoint ZDR. **Qwen 3.7 Flash non riceve mai la cronologia del
gruppo**: è riservato ai comandi comici one-shot, dove offre il costo minimo senza
trasportare memoria. Il ledger costi salva modello, token e costo, mai prompt o
risposte. Ogni richiesta è protetta sia dal cap mensile persistente del bot sia dal
limite e dal prezzo massimo del provider.

Per ricevere i messaggi ordinari devi disattivare la privacy del bot da BotFather:
`/setprivacy` → scegli il bot → **Disable**. Se Telegram non applica il cambio a un
gruppo esistente, rimuovi e riaggiungi il bot. Senza questo passaggio Alduino continua
a funzionare, ma vede solo comandi, mention e reply che Telegram gli consegna.

### Quiz a premi

Creazione guidata (`/crea_quiz` o dall'hub **🎬 Eventi**) con tasti **« Indietro »**, premi
**personalizzabili** per 1°/2°/3°, un **premio di consolazione a scendere** con **minimo
garantito** per *tutti* i finisher, e una schermata di riepilogo prima di pubblicare. Si
gioca in chat privata; alla chiusura viene pubblicato il **podio** con i premi.

I quiz sono **oggetti persistenti**: dall'hub Eventi si toccano per aprirne la **scheda info**
(non si avviano con un tap) e da lì si sceglie **Avvia · Programma · Chiudi · Riproponi · Elimina**,
sempre **con conferma**. Un quiz concluso resta come archivio finché non lo elimini, e «Riproponi»
lo azzera per rigiocarlo.

### Guess The Game & Sound Quest

Due giochi «indovina», creati e gestiti solo dagli admin dall'hub **🎬 Eventi**. La creazione
chiede **tre cose** — titolo, media (**foto** per Guess The Game, **audio** per Sound Quest) e
risposta corretta — poi mostra una **scheda** con tutto il resto già compilato: tentativi,
tempo, chiusura automatica, suggerimenti e premi. Tocchi un campo, lo cambi, torni alla scheda.
Il round si avvia subito o si programma, come i quiz.

I **suggerimenti** si aggiungono senza scrivere niente di tecnico: `➕ Aggiungi`, scrivi il
testo, e scegli con un tocco dopo quanti tentativi deve arrivare. I numeri già usati non
vengono nemmeno proposti. Se poi abbassi i tentativi, i suggerimenti che nessuno vedrebbe più
vengono tolti e il bot te lo dice.

Nel gruppo arriva **solo l'invito**: l'immagine e l'audio non si postano lì, altrimenti la
soluzione si discute in chat e giocare in privato non vuol più dire niente. Si rivelano col
**podio** alla chiusura, insieme alla risposta. Vince chi ci arriva in **meno tentativi**; a
parità conta il tempo.

Le risposte sono **libere** e le giudica l'AI, quindi «GTA SA» vale «Grand Theft Auto: San
Andreas» — ma la **serie da sola non basta**: «GTA» per «GTA San Andreas» è sbagliato. In
creazione puoi aggiungere grafie sempre accettate: sono la rete di sicurezza se l'AI non
risponde, perché quelle vengono riconosciute **senza** interpellarla.

Se il giudice non risponde, il tentativo **non viene contato**: il messaggio te lo dice e il
tuo budget resta intero. Dopo qualche risposta non giudicata di fila il bot si ferma da solo e
ti invita a riprovare più tardi, invece di lasciarti bruciare tentativi a vuoto.

### Scommesse (stile Twitch)

Payout **proporzionale** al pool (stile Twitch) e una sola scommessa per utente per evento.
Alla creazione si sceglie la **finestra temporale** entro cui si può puntare (preset
**15m/30m/1h/3h**, durata personalizzata, oppure **♾️ illimitata**). All'avvio parte il
conto alla rovescia: allo scadere la scommessa **si chiude da sola** (non accetta più puntate)
e resta in attesa che un admin **dichiari il vincitore** dal pannello `/gestisci_scommesse`.
Con «illimitata» la chiusura resta manuale, come prima. Nella lista `/scommesse` una scommessa
su cui hai già puntato è marcata **✅**, così lo vedi subito.

---

## XP, Trofei, Ranghi e Tag

Le **monete** (CoInn) sono spendibili e farmabili; gli **XP** sono una metrica di
**merito separata** e **non farmabile**:

- **Si guadagnano XP partecipando, non solo vincendo.** Gli XP arrivano da due fonti:
  - **Eventi curati dagli admin** (XP *senza tetto*, perché non spammabili):
    - **Quiz** — chi gioca prende `QUIZ_XP_PARTICIPATION` di base (basta **1 risposta**),
      `+QUIZ_XP_PER_CORRECT` per ogni risposta giusta, e il podio (1°/2°/3°) un **bonus**
      extra (`QUIZ_XP_PODIUM_FIRST/SECOND/THIRD`).
    - **Scommesse** — piazzare una scommessa dà `XP_PER_BET_PLACED` di partecipazione;
      se vinci, `XP_PER_BET_WON` in più.
    - **Assegnazione manuale** admin (`/dai_xp`, `/set_xp`, Airdrop XP).
  - **Quota giornaliera con tetto** (anti-farm): il `/daily` dà `XP_PER_DAILY_CLAIM`,
    limitato a `XP_DAILY_PARTICIPATION_CAP` XP al giorno per utente (lato server).
- **Trofei** (stile PlayStation): achievement con **rarità** Bronzo/Argento/Oro/Platino,
  sbloccati da condizioni — saldo, streak, scommesse, **XP**, **acquisti alla Locanda**
  (per oggetto / per categoria), **podio nel Trivia**, e **collezioni** (sblocca tutti i
  trofei di un set). Li vedi con `/traguardi` (raggruppati per rarità) e `/catalogo_badge`.
- **Livelli & Ranghi** (stile GTA Online): gli XP si traducono in un **livello numerico** —
  ogni livello costa il **15% in più** del precedente (configurabile) — ed è il livello, non
  l'XP grezzo, a essere mostrato su profilo, traguardi e classifiche. I **nomi rango** (Novizio
  → Veterano → Leggenda) sono titoli mappati su **fasce di livello** (personalizzabili via CSV);
  level-up e rank-up vengono annunciati. Gli admin vedono il sistema completo con `/lista_ranghi`.
- **La Locanda** (`/locanda`): 🏷️ **tag cosmetici** (flair, acquisto una tantum) e 🍖 **menù
  di consumabili** (cibi/bevande riacquistabili che riempiono la 🎒 **dispensa** mostrata sul
  profilo). Tutto **solo estetico/collezionabile** — nessun permesso reale, nessuna escalation.

### Personalizzare con i CSV

Nomi, soglie, rarità e prezzi sono **modificabili senza ricompilare**, via CSV nella
cartella dati montata (`CATALOG_DIR`, default `data/`):

```bash
cp catalogs/trophies.example.csv               data/trophies.csv
cp catalogs/ranks.example.csv                  data/ranks.csv
cp catalogs/shop_cosmetics.example.csv         data/shop_cosmetics.csv
cp catalogs/consumable_categories.example.csv  data/consumable_categories.csv
cp catalogs/consumables.example.csv            data/consumables.csv
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
│   │                                 #   catalog_loader, giochi AI, cache/sync IGDB
│   ├── handlers/                     # common, onboarding, economy, betting, badges,
│   │                                 #   leaderboard, shop, quiz, schedule, fun_ai, admin,
│   │                                 #   admin_betting, admin_dashboard, backup, events,
│   │                                 #   event_types/ (registro tipi-evento)
│   │   └── errors.py                 # handler globale dp.errors (log con contesto + risposta utente)
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
| `GROQ_API_KEY` | — | giudice, intrattenimento legacy e fallback opzionale di Alduino |
| `GROQ_JUDGE_MODEL` | `openai/gpt-oss-120b` | giudice di Guess The Game / Sound Quest |
| `OPENROUTER_API_KEY` | — | corsie paid DeepSeek/Qwen; vuota = nessuna chiamata OpenRouter |
| `AI_MONTHLY_BUDGET_USD` | `5.00` | hard cap interno persistente per mese UTC (`0` lo disabilita esplicitamente) |
| `AI_ENTERTAINMENT_PROVIDER` | `groq` | `openrouter` abilita Qwen 3.7 Flash + fallback DeepSeek per i comandi comici |
| `OPENROUTER_CHAT_MODELS` | DeepSeek V4 Flash 0731 → V4 Flash | fallback ordinato ZDR della chat |
| `OPENROUTER_FUN_MODELS` | Qwen 3.7 Flash → DeepSeek V4 Flash | fallback ordinato one-shot, senza memoria |
| `GEMINI_API_KEY` | — | giochi AI persistenti e chat legacy opzionale di Alduino |
| `GEMINI_MODEL` | `gemini-3.5-flash` | modello di 20 Domande |
| `GEMINI_THINKING_LEVEL` | `medium` | default Gemini; 20 Domande forza `minimal` per il verdetto ternario |
| `ALDUINO_PROVIDER` | `gemini` | provider della sola chat: `openrouter`, `gemini` o `groq` |
| `ALDUINO_GEMINI_MODEL` | `gemini-3.6-flash` | modello conversazionale, separato dai giochi strutturati |
| `ALDUINO_THINKING_LEVEL` | `minimal` | thinking breve per risposte rapide da chat |
| `ALDUINO_FALLBACK_TO_GROQ` | `true` | usa Groq se Gemini fallisce |
| `ALDUINO_HISTORY_TURNS` / `_CHARS` | `10` / `8000` | limiti della memoria per ramo |
| `ALDUINO_MEMORY_ROWS_PER_GROUP` | `1000` | cap persistente per gruppo, con potatura automatica |
| `ALDUINO_GROUP_CONTEXT_MESSAGES` / `_CHARS` | `80` / `24000` | finestra ambientale inviata al modello |
| `ALDUINO_GROUP_MEMORY_ROWS` | `3000` | rolling transcript locale; richiede privacy mode Telegram disabilitata |
| `IGDB_CLIENT_ID` / `IGDB_CLIENT_SECRET` | — | abilita il catalogo IGDB in cache; app Twitch confidential |
| `IGDB_CATALOG_SIZE` | `300` | giochi principali più noti mantenuti nella cache di 20 Domande |
| `IGDB_MIN_RATING_COUNT` | `100` | soglia minima di valutazioni IGDB per escludere titoli oscuri |
| `IGDB_SYNC_INTERVAL_HOURS` | `24` | frequenza massima di aggiornamento del catalogo IGDB |
| `CATALOG_DIR` | `data` | cartella con i CSV opzionali (trofei/ranghi/cosmetici) |
| `XP_DAILY_PARTICIPATION_CAP` | `50` | tetto XP *capped* per utente al giorno |
| `XP_PER_DAILY_CLAIM` | `10` | XP (capped) sul `/daily` |
| `XP_PER_BET_PLACED` | `10` | XP (evento) per aver piazzato una scommessa |
| `XP_PER_BET_WON` | `25` | XP (evento) extra se la scommessa vince |
| `QUIZ_XP_PARTICIPATION` | `20` | XP (evento) per aver giocato il quiz (≥1 risposta) |
| `QUIZ_XP_PER_CORRECT` | `10` | XP (evento) per ogni risposta corretta |
| `QUIZ_XP_PODIUM_FIRST` / `_SECOND` / `_THIRD` | `50` / `30` / `20` | bonus podio quiz |
| `XP_LEVEL_BASE` | `100` | XP per salire dal livello 1 al 2 |
| `XP_LEVEL_GROWTH` | `1.15` | ogni livello costa +15% del precedente |
| `BOT_IMAGE` | `gaming-community-bot:local` | immagine usata dal compose (override → GHCR) |

PostgreSQL e Redis sono già pronti nel `docker-compose.yml`.

---

## Inline mode (eventi)

Il bot risponde a `@<bot>` con gli eventi utilizzabili adesso e quelli in
programma, con data e ora locale. Le query `aperti`/`live` e
`prossimi`/`coming soon` filtrano le due viste. Le card sono read-only: l'azione
apre un vero deep-link quando l'evento si gioca in privato; i sondaggi e i giochi
collaborativi non mostrano pulsanti finti.

Attivazione (una volta, con @BotFather):
1. `/setinline` → testo: `Eventi aperti e in arrivo`
2. `/setinlinefeedback` → non serve (nessuna mutazione su `chosen_inline_result`).

Sicurezza: il gate membership (`GroupGuard`) si applica anche alle inline query;
chi non è membro del gruppo non può usare la superficie inline.

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

1. Registralo in `src/handlers/__init__.py`, **prima di `common.router`** (che è il
   catch-all e deve restare ultimo):

```python
from handlers import ..., nuovo

ROUTERS: tuple[Router, ...] = (
    ...
    nuovo.router,
    common.router,      # ← resta ultimo
)
```

Se te ne dimentichi, `tests/unit/test_router_order.py` fallisce: cammina il package e
pretende che ogni modulo con un `router` sia registrato. Senza quel test un handler
non registrato è semplicemente morto, senza nessun errore.

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
