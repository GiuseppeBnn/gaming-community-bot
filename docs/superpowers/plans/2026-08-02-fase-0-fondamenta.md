# Fase 0 — fondamenta · piano di implementazione

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rendere il terreno adatto ad aiogram-dialog senza ancora installarlo — storage FSM che degrada invece di mentire, aiogram aggiornato alla versione che la libreria richiede, e ogni guasto del bot che arriva in DM agli admin.

**Architecture:** tre cambi indipendenti, tre commit, nessuno dei quali dipende dall'esito dello spike. `main._build_storage` diventa asincrona e fa un `ping` prima di fidarsi di Redis; `requirements.txt` passa da `aiogram==3.13.1` a `aiogram==3.30.0` in un commit isolato con l'intera suite come collaudo; `utils/alerts.py` aggancia un `logging.Handler` alla radice, così ogni `log.warning`/`log.error`/`log.exception` già scritto diventa un alert senza che il file che lo emette sappia che questo modulo esiste.

**Tech Stack:** Python 3.12.13 · aiogram 3.13.1 → 3.30.0 · redis 5.2.0 · pydantic-settings 2.x · pytest 8.3.4 (`asyncio_mode=auto`) · ruff 0.16.0 · mypy 2.3.0.

**Spec di riferimento:** [2026-08-02-refactoring-aiogram-dialog-design.md](../specs/2026-08-02-refactoring-aiogram-dialog-design.md) §3.
Aggiornare la **tabella di stato (§10 dello spec)** dopo ogni task.

**Perché il piano si ferma alla Fase 0.** La Fase 1 (spike su `guess/creation.py`) non è pianificabile adesso: i suoi task usano l'API di `aiogram_dialog`, che non è installabile finché il Task 2 non è verde. Scriverne ora i passi TDD vorrebbe dire inventare codice non verificabile. Il piano della Fase 1 si scrive quando il Task 5 è chiuso.

---

## Global Constraints

Valgono per **ogni** task, senza ripeterle.

- **Import top-level**: `from config_data.config import settings`, **mai** `from src.config_data...` (`pythonpath = ["src"]`).
- **`from __future__ import annotations`** in ogni modulo nuovo (STEERING regola 5).
- **Mai `get_settings()`**: esiste un solo singleton `settings` (regola 2).
- **Messaggi all'utente in italiano; commenti, log e nomi in inglese.**
- **`src/utils` è dentro mypy** (`files = ["src/services", "src/database", "src/utils", "src/config_data", "src/filters"]`): `utils/alerts.py` va annotato. `src/main.py` **non** è in mypy.
- **`src/main.py` è escluso dal coverage** (`omit` in `[tool.coverage.run]`): il test del Task 1 è vero ma non muove il numero. `src/utils/alerts.py` **è** incluso, quindi va coperto o il gate `fail_under = 99` scende.
- **Gate statici a zero findings**: `ruff check src/ tests/` e `mypy` non segnalano niente oggi. Ogni segnalazione nuova è una regressione del commit corrente.
- **Gate coverage**: `fail_under = 99`, ratchet — si alza, non si abbassa mai per far passare la CI.
- **Env di test**: `tests/conftest.py` imposta `BOT_TOKEN`, `DAILY_REWARD_COINS`, `DB_URL` con `os.environ.setdefault`. Non serve passarle a mano.
- **Nessuna dipendenza nuova** oltre a quelle nominate qui.
- **Branch**: si lavora fuori da `main`. Per riportare lavoro su `main` si usa **cherry-pick, non merge** (`main` è stato squash-merged in passato, `test` ha tenuto la storia completa).

---

## File Structure

| file | stato | responsabilità |
|---|---|---|
| `src/main.py` | modifica | `_build_storage` diventa `async` e fa il ping; `alerts.install()` all'inizio di `main()`; terzo task in background |
| `tests/unit/test_fsm_storage.py` | **nuovo** | i tre esiti di `_build_storage`: memory, redis vivo, redis morto |
| `.env.example` | modifica | `FSM_STORAGE` coerente col codice; `ALERT_MIN_LEVEL` nuovo |
| `STEERING.md` | modifica | §1 (stack) nel Task 2; §2 (`fsm_storage`) nel Task 1; sezione alert nel Task 4 |
| `requirements.txt` | modifica | `aiogram==3.30.0` |
| `src/utils/alerts.py` | **nuovo** | cattura (handler + buffer + dedup + formattazione) e consegna (`alert_loop`) |
| `tests/unit/test_alerts.py` | **nuovo** | copre `alerts.py` — è dentro il gate coverage |
| `src/config_data/config.py` | modifica | `alert_min_level` |

`alerts.py` tiene cattura e consegna nello stesso file **apposta**: sono un'unica responsabilità («i guasti arrivano agli admin») e cambiano insieme. Sono ~150 righe; spezzarle in due moduli sarebbe un layer per il gusto del layer.

---

### Task 1: storage FSM — il ping che manca

Scioglie la contraddizione a tre di §2.3 nodo B dello spec: `STEERING.md:74` dice `memory`, `.env.example:13` dice `redis`, e `main.py:101-108` intercetta solo `ImportError`. `RedisStorage.from_url` costruisce un client **senza connettersi**, quindi oggi un Redis assente si scopre più tardi, un handler fallito alla volta.

**Files:**
- Modify: `src/main.py:101-108` (`_build_storage`) e `src/main.py:146` (il chiamante)
- Modify: `.env.example:13`
- Modify: `STEERING.md:74` (la voce `fsm_storage` di §2)
- Test: `tests/unit/test_fsm_storage.py` (nuovo)

**Interfaces:**
- Consumes: `settings.fsm_storage`, `settings.redis_url`
- Produces: `async def _build_storage() -> BaseStorage` — **la firma cambia**, il chiamante in `main()` deve diventare `storage = await _build_storage()`

- [ ] **Step 1: Scrivere il test che fallisce**

Crea `tests/unit/test_fsm_storage.py`:

```python
"""`_build_storage` deve degradare, non mentire.

`RedisStorage.from_url` costruisce un client senza connettersi: prima di questo
test un Redis irraggiungibile passava il bootstrap indisturbato e si manifestava
molto dopo, come una conversazione che non ricorda niente. STEERING §2 poneva
esattamente questa condizione per passare a Redis — «non riproporre il passaggio
senza prima aggiungere un fallback su errore di connessione».
"""

from __future__ import annotations

import logging

import pytest
from aiogram.fsm.storage.memory import MemoryStorage

import main
from config_data.config import settings


class _FakeRedis:
    def __init__(self, *, fails: bool) -> None:
        self._fails = fails
        self.pinged = False

    async def ping(self) -> bool:
        self.pinged = True
        if self._fails:
            raise ConnectionError("Connection refused")
        return True


class _FakeStorage:
    """Sta al posto di RedisStorage: si costruisce senza connettersi, come il vero."""

    def __init__(self, *, fails: bool) -> None:
        self.redis = _FakeRedis(fails=fails)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def redis_storage(monkeypatch):
    """Sostituisce RedisStorage e registra se e come è stato costruito."""

    def _install(*, fails: bool) -> dict:
        made: dict = {}

        class _Factory:
            @staticmethod
            def from_url(url: str) -> _FakeStorage:
                made["url"] = url
                made["storage"] = _FakeStorage(fails=fails)
                return made["storage"]

        monkeypatch.setattr("aiogram.fsm.storage.redis.RedisStorage", _Factory)
        return made

    return _install


async def test_memory_setting_never_builds_a_redis_client(redis_storage, monkeypatch):
    made = redis_storage(fails=False)
    monkeypatch.setattr(settings, "fsm_storage", "memory")

    storage = await main._build_storage()

    assert isinstance(storage, MemoryStorage)
    assert "storage" not in made, "con memory non si tocca Redis nemmeno per costruirlo"


async def test_reachable_redis_is_used(redis_storage, monkeypatch):
    made = redis_storage(fails=False)
    monkeypatch.setattr(settings, "fsm_storage", "redis")
    monkeypatch.setattr(settings, "redis_url", "redis://example:6379/0")

    storage = await main._build_storage()

    assert storage is made["storage"]
    assert made["storage"].redis.pinged, "il ping è tutto il punto di questo cambio"
    assert made["url"] == "redis://example:6379/0"


async def test_unreachable_redis_degrades_to_memory(redis_storage, monkeypatch, caplog):
    made = redis_storage(fails=True)
    monkeypatch.setattr(settings, "fsm_storage", "redis")

    with caplog.at_level(logging.WARNING):
        storage = await main._build_storage()

    assert isinstance(storage, MemoryStorage), "un Redis morto non deve impedire l'avvio"
    assert made["storage"].closed, "il client a metà non deve restare appeso"
    assert "Redis" in caplog.text
```

- [ ] **Step 2: Eseguire il test per vederlo fallire**

Run: `pytest tests/unit/test_fsm_storage.py -v`
Expected: FAIL. `_build_storage` è sincrona, quindi `await main._build_storage()` solleva
`TypeError: object ... can't be used in 'await' expression`.

- [ ] **Step 3: Implementare**

In `src/main.py`, aggiungi agli import in cima:

```python
import contextlib
```

e

```python
from aiogram.fsm.storage.base import BaseStorage
```

Sostituisci `_build_storage` (righe 101-108) con:

```python
async def _build_storage() -> BaseStorage:
    """Scegli lo storage FSM, degradando a memoria se Redis non risponde.

    `RedisStorage.from_url` builds a client without connecting, so a broken or
    absent Redis used to sail through startup and surface much later, one
    amnesiac conversation at a time. The ping moves that discovery here and
    turns it into a degradation instead of a bot that looks alive and cannot
    remember anything.

    Degrading rather than refusing to start is the trade STEERING §2 asks for:
    losing the open FSM flows is worth less than losing the bot.
    """
    if settings.fsm_storage != "redis":
        return MemoryStorage()

    try:
        from aiogram.fsm.storage.redis import RedisStorage
    except ImportError:
        logger.warning("redis non installato, uso MemoryStorage")
        return MemoryStorage()

    storage = RedisStorage.from_url(settings.redis_url)
    try:
        await storage.redis.ping()
    except Exception as exc:  # noqa: BLE001 — any connection failure degrades
        logger.warning(
            "Redis non raggiungibile su %s (%s): uso MemoryStorage. "
            "Le conversazioni FSM aperte non sopravvivranno a un riavvio.",
            settings.redis_url, exc,
        )
        # The half-built client holds a connection pool; closing it must not be
        # able to replace a degradation with a crash.
        with contextlib.suppress(Exception):
            await storage.close()
        return MemoryStorage()

    return storage
```

Poi, alla riga 146, il chiamante:

```python
    storage = await _build_storage()
```

- [ ] **Step 4: Eseguire i test**

Run: `pytest tests/unit/test_fsm_storage.py -v`
Expected: PASS, 3 test.

- [ ] **Step 5: Allineare `.env.example` e STEERING**

In `.env.example`, la riga 13 resta `FSM_STORAGE=redis` — ora è **vera**, non più in contraddizione. Aggiungi sopra il commento che spiega il degrado:

```
# memory | redis. Con `redis` il bot fa un ping all'avvio: se non risponde,
# logga un warning e riparte in memoria invece di non partire (STEERING §2).
FSM_STORAGE=redis
```

In `STEERING.md`, riscrivi la voce `fsm_storage` di §2 (riga 74). Il testo attuale dice
«**Resta `memory`, ed è una scelta, non una svista**» e motiva col fatto che
«`_build_storage` intercetta solo l'`ImportError` del pacchetto, **non** una connessione
fallita, quindi con `redis` il bot non degrada, non parte». Quel baratto non esiste più.
Sostituisci con:

```markdown
- `fsm_storage: str` — `"memory"` | `"redis"`. **Il default è `redis`**, ed è un cambio del 2026-08-02: prima era `memory` perché `_build_storage` intercettava solo l'`ImportError` del pacchetto e non una connessione fallita, quindi con Redis irraggiungibile il bot non degradava, **non partiva** — e perdere un flusso di creazione vale meno che perdere il bot. Ora quel baratto non esiste: `_build_storage` fa un `ping` all'avvio e, se Redis non risponde, logga un warning e riparte con `MemoryStorage`. Il costo residuo è dichiarato nel log: con la memoria, ogni conversazione FSM aperta muore al riavvio del container (Watchtower ricrea l'immagine ogni `WATCHTOWER_POLL_INTERVAL: 600`). Il degrado non è silenzioso: passa dagli alert admin (§ alert)
- `redis_url: str`
```

> Se la sezione alert non esiste ancora (Task 4), lascia il riferimento: sarà valido a fine Fase 0. È l'unico rimando in avanti del piano, ed è voluto — riscrivere due volte la stessa voce costa più che avere un link acceso mezz'ora dopo.

- [ ] **Step 6: Gate completi**

```bash
pytest
ruff check src/ tests/
mypy
PYTHONPATH=src python -c "import main"
```

Expected: suite verde. Baseline misurata il 2026-08-02: **2067 passed, 30 skipped** senza
`TEST_PG_URL` — non 2062, che è il numero fermo in `CLAUDE.md` e `INDEX.md`. Coi 3 nuovi:
**2070 passed, 30 skipped**. Ruff e mypy silenziosi, l'import non esplode.

- [ ] **Step 7: Commit**

```bash
git add src/main.py tests/unit/test_fsm_storage.py .env.example STEERING.md
git commit -m "fix: Redis irraggiungibile degrada a MemoryStorage invece di sparire nel silenzio

RedisStorage.from_url costruisce il client senza connettersi, quindi un Redis
assente passava il bootstrap e si manifestava molto dopo, come una conversazione
che non ricorda niente. Ora _build_storage fa un ping: se non risponde, warning e
MemoryStorage.

E' la condizione che STEERING §2 poneva per passare a redis, quindi il default
cambia e .env.example smette di contraddire il documento normativo."
```

---

### Task 2: aiogram 3.13.1 → 3.30.0

`aiogram_dialog >= 2.3.0` richiede `aiogram >= 3.14.0` (§2.1 dello spec): questo upgrade è il prezzo d'ingresso della libreria, e va pagato **da solo**, in un commit che si può revertire senza toccare altro. È la parte più rischiosa dell'intera roadmap.

**Files:**
- Modify: `requirements.txt:1`
- Modify: `STEERING.md` §1 (stack e versioni vincolanti)
- Possibly modify: qualunque file che l'upgrade rompa — non prevedibile, si scopre eseguendo

**Interfaces:**
- Consumes: niente dal Task 1 se non un albero verde
- Produces: un ambiente su cui `pip install aiogram-dialog==2.6.0` non forza altri upgrade

- [ ] **Step 1: Inventario delle superfici aiogram usate**

Prima di aggiornare, sapere cosa può rompersi. Esegui:

```bash
grep -rhoE "from aiogram[a-z_.]* import [A-Za-z_, ]+" src/ | sort -u
```

Le superfici note (verificate il 2026-08-02) sono: `Bot`, `Dispatcher`, `Router`,
`DefaultBotProperties`, `ParseMode`/`MessageEntityType` da `aiogram.enums`,
`MemoryStorage`, `RedisStorage`, `FSMContext`, `StatesGroup`/`State`, `ErrorEvent`,
`TelegramBadRequest`, `ChatActionSender`, `InlineKeyboardBuilder`,
`BotCommand`/`BotCommandScope*`, il filtro `F`, `BaseMiddleware`,
`dp.resolve_used_update_types`, `router.message.filter`.

Salva l'output: è la lista da ricontrollare se qualcosa esplode.

- [ ] **Step 2: Leggere il CHANGELOG fra 3.14.0 e 3.30.0**

<https://github.com/aiogram/aiogram/blob/dev-3.x/CHANGES.rst>

Cerca i breaking change che toccano le superfici dello Step 1. Annota quelli rilevanti in
un commento del commit — servono a chi leggerà il diff fra sei mesi.

- [ ] **Step 3: Aggiornare `requirements.txt` e installare**

```bash
sed -i '' 's/^aiogram==3.13.1$/aiogram==3.30.0/' requirements.txt
pip install -r requirements-dev.txt
pip show aiogram | head -2
```

Expected: `Version: 3.30.0`.

- [ ] **Step 4: Verificare che `redis==5.2.0` regga**

aiogram 3.30 dichiara `redis[hiredis]<8,>=6.2.0` nel suo extra `redis` (che non installiamo),
ma è la versione contro cui `RedisStorage` è testato. Il Task 1 ha appena messo quel percorso
sotto test, quindi la risposta è a un comando:

```bash
pytest tests/unit/test_fsm_storage.py -v
```

Expected: PASS. Se fallisce con un `TypeError`/`AttributeError` dentro `redis`, alza
`redis==6.2.0` in `requirements.txt`, reinstalla e ripeti. Registra la scelta nel commit.

- [ ] **Step 5: Suite completa**

```bash
pytest -q
```

Expected: `2070 passed, 30 skipped`. Ogni fallimento qui **è** l'upgrade: la suite era verde
un commit fa.

- [ ] **Step 6: I test `pg` — non saltabili**

L'upgrade tocca lo storage, e le gare sul denaro non sono esprimibili su SQLite.

```bash
docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=postgres --name pgtest postgres:16
export TEST_PG_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test"
# il nome del DB deve finire in _test: il fixture fa drop_all
docker exec pgtest psql -U postgres -c "CREATE DATABASE gamingbot_test;"
pytest -m pg -rxX
docker rm -f pgtest
```

Expected: 30 passed.

- [ ] **Step 7: Gate statici e import**

```bash
ruff check src/ tests/
mypy
PYTHONPATH=src python -c "import main"
```

Expected: tutti silenziosi. `ruff` e `mypy` sono a zero findings, quindi qualunque riga
in uscita è una regressione di questo commit.

- [ ] **Step 8: Se qualcosa si rompe**

Nell'ordine, senza saltare passaggi:

1. Ripara, se la rottura è locale e chiara (un import spostato, un parametro rinominato).
2. Se non si ripara in giornata, scendi al minimo che soddisfa `aiogram_dialog`:
   `aiogram==3.14.0`, e ripeti dallo Step 4.
3. Se rompe anche 3.14.0: **la roadmap si ferma qui.** Reverta questo task, lascia una
   riga in §10 dello spec (`✗ abbandonata` + il perché) e ferma le Fasi 1-5. I Task 1, 3, 4
   restano acquisiti: hanno valore da soli.

- [ ] **Step 9: Aggiornare STEERING §1**

La sezione «Stack e versioni vincolanti» nomina `aiogram 3.13.1`. Portala a `3.30.0` e
aggiungi la ragione, che non è cosmetica:

```markdown
`aiogram` **3.30.0** (mai 2.x). Il floor è `3.14.0` e non è arbitrario: è ciò che
`aiogram_dialog >= 2.3.0` richiede (`Requires-Dist: aiogram>=3.14.0`). Chi volesse
tornare sotto quella soglia deve prima togliere aiogram-dialog.
```

- [ ] **Step 10: Commit**

```bash
git add requirements.txt STEERING.md
git commit -m "chore: aiogram 3.13.1 -> 3.30.0

aiogram_dialog >= 2.3.0 richiede aiogram >= 3.14.0, quindi l'upgrade e' il prezzo
d'ingresso della libreria. Commit isolato apposta: e' la parte piu' rischiosa della
roadmap e deve poter essere revertita senza toccare altro.

Collaudo: suite intera + i 30 test pg con TEST_PG_URL (l'upgrade tocca lo storage,
e le gare sul denaro non sono esprimibili su SQLite) + ruff + mypy."
```

- [ ] **Step 11: Prova sull'immagine di staging**

Spingi il branch, attendi la CI, e prova `latest-<branch>` sul bot di test:

1. il bot parte;
2. un comando qualsiasi risponde;
3. un flusso FSM completo (crea un quiz fino alla scheda);
4. **riavvia il container** e verifica che il flusso aperto sopravviva — è la prova che
   Redis è davvero in uso dopo il Task 1.

---

### Task 3: `utils/alerts.py` — cattura, dedup, formattazione

La metà del modulo che non parla con Telegram: un `logging.Handler` che bufferizza, un dedup a impronta, e la formattazione del messaggio. Tutto sincrono e testabile senza un bot.

**Files:**
- Create: `src/utils/alerts.py`
- Create: `tests/unit/test_alerts.py`
- Modify: `src/config_data/config.py` (campo `alert_min_level`)

**Interfaces:**
- Consumes: `settings.admin_ids`, `settings.alert_min_level`
- Produces, per il Task 4:
  - `class TelegramAlertHandler(logging.Handler)` — `emit(record) -> None`
  - `def install() -> TelegramAlertHandler`
  - `def format_alert(record: logging.LogRecord, suppressed: int = 0) -> str`
  - `def reset() -> None` (helper di test)
  - variabili di modulo `_buffer: deque[logging.LogRecord]`, `_dropped: int`, `_undelivered: int`

- [ ] **Step 1: Aggiungere il setting**

In `src/config_data/config.py`, sotto `admin_ids`:

```python
    # Alert channel: the minimum level that reaches the admins' DMs. Raising it
    # to CRITICAL is also the off switch — no second variable just for that.
    alert_min_level: str = "WARNING"
```

> `tests/unit/test_no_dead_config.py` fallisce se un setting non è letto da nessuna riga di
> `src/`. Questo lo legge `_min_level()` nello Step 3: finché quel passo non è fatto, il test
> è **rosso**, ed è corretto che lo sia.

- [ ] **Step 2: Scrivere il test che fallisce**

Crea `tests/unit/test_alerts.py`:

```python
"""Il canale di alert deve essere impossibile da trasformare in un guasto.

Tre modi in cui un canale del genere si autodistrugge, e i tre test che li
chiudono: `emit` che fa I/O e blocca l'event loop dentro un handler; il sender
che logga i propri errori e si rialimenta all'infinito; una singola riga di log
in loop che riempie la chat e fa smettere di guardarla.
"""

from __future__ import annotations

import logging

import pytest

from config_data.config import settings
from utils import alerts


@pytest.fixture(autouse=True)
def clean_alerts():
    alerts.reset()
    yield
    alerts.reset()


def _record(
    msg: str = "Annuncio round %s fallito",
    *,
    level: int = logging.WARNING,
    name: str = "handlers.guess.lifecycle",
    args: tuple = (7,),
    exc_info=None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=exc_info,
    )


def test_emit_only_buffers():
    """Se `emit` facesse I/O bloccherebbe l'event loop a ogni riga di log."""
    handler = alerts.TelegramAlertHandler()

    handler.emit(_record())

    assert len(alerts._buffer) == 1


def test_buffer_is_bounded_and_counts_what_it_drops():
    handler = alerts.TelegramAlertHandler()

    for _ in range(alerts._MAX_BUFFERED + 5):
        handler.emit(_record())

    assert len(alerts._buffer) == alerts._MAX_BUFFERED
    assert alerts._dropped == 5, "gli scartati si contano, non spariscono"


def test_own_records_are_ignored():
    """Un guasto di questo modulo che diventasse un alert si rialimenterebbe."""
    handler = alerts.TelegramAlertHandler()

    handler.emit(_record(name=alerts.__name__))

    assert not alerts._buffer


def test_repeats_inside_the_window_are_suppressed_and_counted():
    now = 1000.0
    fingerprint = ("handlers.guess.lifecycle", "Annuncio round %s fallito")

    first, suppressed = alerts._should_send(fingerprint, now)
    second, _ = alerts._should_send(fingerprint, now + 1)
    third, _ = alerts._should_send(fingerprint, now + 2)

    assert first is True and suppressed == 0
    assert second is False and third is False

    after, suppressed_after = alerts._should_send(
        fingerprint, now + alerts._DEDUP_WINDOW_SECONDS + 1
    )
    assert after is True
    assert suppressed_after == 2, "le ripetizioni si riportano, non si buttano"


def test_different_templates_are_not_deduplicated():
    now = 1000.0

    first, _ = alerts._should_send(("a", "template uno"), now)
    second, _ = alerts._should_send(("a", "template due"), now)

    assert first is True and second is True


def test_fingerprint_groups_by_template_not_by_formatted_message():
    """«round 7» e «round 8» sono lo stesso guasto, non due."""
    assert alerts._fingerprint(_record(args=(7,))) == alerts._fingerprint(_record(args=(8,)))


def test_format_carries_level_logger_and_message():
    text = alerts.format_alert(_record())

    assert "WARNING" in text
    assert "handlers.guess.lifecycle" in text
    assert "Annuncio round 7 fallito" in text, "il messaggio va formattato con i suoi args"


def test_format_includes_the_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = _record(msg="esploso", args=(), exc_info=sys.exc_info())

    text = alerts.format_alert(record)

    assert "ValueError: boom" in text
    assert "Traceback" in text


def test_format_reports_suppressed_repeats():
    text = alerts.format_alert(_record(), suppressed=12)

    assert "12" in text


def test_format_truncates_instead_of_letting_telegram_refuse():
    text = alerts.format_alert(_record(msg="x" * 10_000, args=()))

    assert len(text) <= alerts._MAX_TEXT + 20
    assert "troncato" in text


def test_level_threshold_comes_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "alert_min_level", "ERROR")

    assert alerts._min_level() == logging.ERROR


def test_a_nonsense_level_falls_back_to_warning(monkeypatch):
    monkeypatch.setattr(settings, "alert_min_level", "URGENTISSIMO")

    assert alerts._min_level() == logging.WARNING


def test_install_attaches_to_the_root_logger(monkeypatch):
    monkeypatch.setattr(settings, "alert_min_level", "ERROR")
    handler = alerts.install()
    try:
        assert handler in logging.getLogger().handlers
        assert handler.level == logging.ERROR
    finally:
        logging.getLogger().removeHandler(handler)
```

- [ ] **Step 3: Eseguire per vederli fallire**

Run: `pytest tests/unit/test_alerts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'utils.alerts'`.

- [ ] **Step 4: Implementare la metà «cattura»**

Crea `src/utils/alerts.py`:

```python
"""
Telegram alert channel for the maintainer: every problem the bot already logs,
delivered to the admins' private chats.

Wired as a **logging handler** rather than as a `notify_admins()` helper called
by hand. Every `log.warning`/`log.error`/`log.exception` already written — the
global error handler, the backup loop, the scheduler, the Redis degradation of
`main._build_storage` — becomes an alert without any of those files knowing this
module exists. A helper would have covered only the call sites someone
remembered to add, and «remembered to add» is how a channel like this rots.

Three constraints shape everything here:

* **`emit()` never does I/O.** Logging is synchronous and is called from inside
  handlers; a send in `emit` would block the event loop on every log line. It
  appends to a bounded buffer and returns.
* **The sender never logs.** A delivery failure that reached the logger would
  come straight back into this buffer and the bot would feed itself alerts
  forever. Delivery failures are counted in memory instead, and reported with
  the next successful drain.
* **Repeats are deduplicated by template.** A group announcement failing in a
  loop is one warning every few seconds — exactly the flood that makes an alert
  channel worth ignoring. Repeats are counted and reported, not dropped.
"""

from __future__ import annotations

import logging
import time
from collections import deque

from config_data.config import settings

# An alert storm must cost the memory we agreed to spend, not all of it.
_MAX_BUFFERED = 200
# Two identical alerts inside this window are one alert plus a count.
_DEDUP_WINDOW_SECONDS = 300.0
# Telegram refuses over 4096 characters; leave room for the header.
_MAX_TEXT = 3500

_buffer: deque[logging.LogRecord] = deque()
_dropped = 0
# fingerprint → (monotonic time of last delivery, repeats suppressed since then)
_seen: dict[tuple[str, str], tuple[float, int]] = {}
# Delivery failures are counted, never logged — see the module docstring.
_undelivered = 0

# Formatting a traceback is all we borrow from the logging machinery.
_formatter = logging.Formatter()


def _min_level() -> int:
    """Threshold from config, falling back to WARNING on anything unreadable.

    A typo in `.env` must not silence the channel: a level nobody can parse is
    the one case where guessing beats refusing.
    """
    level = logging.getLevelName(settings.alert_min_level.upper())
    return level if isinstance(level, int) else logging.WARNING


class TelegramAlertHandler(logging.Handler):
    """Buffers records for `alert_loop`. Does no I/O, so it cannot block."""

    def emit(self, record: logging.LogRecord) -> None:
        global _dropped
        if record.name.startswith(__name__):
            # Our own failures must never become alerts about themselves.
            return
        if len(_buffer) >= _MAX_BUFFERED:
            _dropped += 1
            return
        _buffer.append(record)


def _fingerprint(record: logging.LogRecord) -> tuple[str, str]:
    """Group by **template**, not by formatted message: «Annuncio round %s
    fallito» is one problem whether it fires for round 7 or round 8."""
    return (record.name, str(record.msg))


def _should_send(fingerprint: tuple[str, str], now: float) -> tuple[bool, int]:
    """(send?, repeats suppressed since the last delivery of this fingerprint)."""
    last, suppressed = _seen.get(fingerprint, (0.0, 0))
    if last and now - last < _DEDUP_WINDOW_SECONDS:
        _seen[fingerprint] = (last, suppressed + 1)
        return False, 0
    _seen[fingerprint] = (now, 0)
    return True, suppressed


def format_alert(record: logging.LogRecord, suppressed: int = 0) -> str:
    """Plain text, never HTML.

    A traceback is full of characters that HTML mode would reject, and one
    missed escape would turn an alert about a bug into a bug of its own. Sent
    with `parse_mode=None`, like the AI commands' output (STEERING §17).
    """
    parts = [f"[{record.levelname}] {record.name}", record.getMessage()]
    if record.exc_info:
        parts.append(_formatter.formatException(record.exc_info))
    if suppressed:
        parts.append(
            f"(+{suppressed} ripetizioni soppresse negli ultimi "
            f"{int(_DEDUP_WINDOW_SECONDS)}s)"
        )
    text = "\n".join(parts)
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "\n…(troncato)"
    return text


def install() -> TelegramAlertHandler:
    """Attach the handler to the root logger. Called once, from `main()`."""
    handler = TelegramAlertHandler()
    handler.setLevel(_min_level())
    logging.getLogger().addHandler(handler)
    return handler


def reset() -> None:
    """Clear buffer and dedup state — test helper, mirrors `utils.cooldown.reset`."""
    global _dropped, _undelivered
    _buffer.clear()
    _seen.clear()
    _dropped = 0
    _undelivered = 0
```

> `_seen` non ha pruning **apposta**: cresce col numero di template di log distinti, non col
> traffico. Oggi `src/` ne ha 65 (44 `warning`, 11 `error`, 10 `exception`). Una struttura
> che si pota a un limite che non verrà mai raggiunto è codice che non verrà mai eseguito.

- [ ] **Step 5: Eseguire i test**

Run: `pytest tests/unit/test_alerts.py -v`
Expected: PASS, 13 test. Se `test_a_nonsense_level_falls_back_to_warning` fallisce,
controlla `.upper()`: `logging.getLevelName("warning")` restituisce una stringa, non 30.

- [ ] **Step 6: Gate**

```bash
pytest -q
ruff check src/ tests/
mypy
```

Expected: verde. `mypy` copre `src/utils`, quindi le annotazioni di `alerts.py` **sono**
controllate: se segnala, sistema le firme, non aggiungere `# type: ignore`.

- [ ] **Step 7: Commit**

```bash
git add src/utils/alerts.py tests/unit/test_alerts.py src/config_data/config.py
git commit -m "feat: cattura degli alert admin (handler, buffer, dedup)

Un logging.Handler invece di un notify_admins() da chiamare a mano: ogni warning
ed exception gia' scritti diventano alert senza toccare i file che li emettono.

emit() non fa I/O (bloccherebbe l'event loop dentro un handler), il buffer e'
limitato e conta cio' che scarta, e le ripetizioni dello stesso template si
sopprimono per 300s riportando quante ne sono state saltate.

Meta' consegna nel commit successivo."
```

---

### Task 4: consegna in DM e wiring in `main`

L'altra metà: il task in background che svuota il buffer e manda in DM a ogni id di `settings.admin_ids`.

**Files:**
- Modify: `src/utils/alerts.py` (aggiunge `_deliver`, `_flush_housekeeping`, `drain`, `alert_loop`)
- Modify: `tests/unit/test_alerts.py` (aggiunge i test della consegna)
- Modify: `src/main.py` (install + terzo task in background)
- Modify: `.env.example` (`ALERT_MIN_LEVEL`)
- Modify: `STEERING.md` (sezione nuova sugli alert)

**Interfaces:**
- Consumes dal Task 3: `_buffer`, `_dropped`, `_undelivered`, `_should_send`, `_fingerprint`, `format_alert`, `install`, `reset`
- Produces: `async def drain(bot) -> int` · `async def alert_loop(bot) -> None`

- [ ] **Step 1: Scrivere i test che falliscono**

Aggiungi in fondo a `tests/unit/test_alerts.py`:

```python
class _FakeBot:
    """Registra le consegne. `fails=True` è il caso che non deve mai loggare."""

    def __init__(self, *, fails: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.parse_modes: list = []
        self._fails = fails

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        if self._fails:
            raise RuntimeError("chat not found")
        self.sent.append((chat_id, text))
        self.parse_modes.append(parse_mode)


async def test_drain_delivers_to_every_admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [11, 22])
    alerts._buffer.append(_record())
    bot = _FakeBot()

    sent = await alerts.drain(bot)

    assert sent == 1
    assert [chat_id for chat_id, _ in bot.sent] == [11, 22]
    assert bot.parse_modes == [None, None], "un traceback non è HTML"


async def test_drain_empties_the_buffer(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [11])
    alerts._buffer.append(_record())
    bot = _FakeBot()

    await alerts.drain(bot)

    assert not alerts._buffer


async def test_a_delivery_failure_neither_raises_nor_logs(monkeypatch, caplog):
    """Se il sender loggasse, l'errore rientrerebbe nel buffer, per sempre."""
    monkeypatch.setattr(settings, "admin_ids", [11])
    alerts._buffer.append(_record())
    bot = _FakeBot(fails=True)

    with caplog.at_level(logging.DEBUG):
        await alerts.drain(bot)

    assert alerts._undelivered == 1, "la consegna fallita si conta"
    ours = [r for r in caplog.records if r.name.startswith("utils.alerts")]
    assert not ours, "…e non si logga: rientrerebbe nel buffer, per sempre"


async def test_repeats_are_delivered_once(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [11])
    for _ in range(5):
        alerts._buffer.append(_record())
    bot = _FakeBot()

    sent = await alerts.drain(bot)

    assert sent == 1
    assert len(bot.sent) == 1


async def test_housekeeping_reports_what_was_lost(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [11])
    alerts._dropped = 7
    alerts._undelivered = 3
    bot = _FakeBot()

    await alerts.drain(bot)

    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "7" in text and "3" in text
    assert alerts._dropped == 0 and alerts._undelivered == 0


async def test_housekeeping_does_not_report_itself_forever(monkeypatch):
    """I contatori si azzerano **prima** dell'invio: una notifica di consegna
    fallita che fallisce a sua volta non deve ripresentarsi a ogni giro."""
    monkeypatch.setattr(settings, "admin_ids", [11])
    alerts._dropped = 2
    bot = _FakeBot(fails=True)

    await alerts.drain(bot)
    await alerts.drain(bot)

    assert alerts._dropped == 0


async def test_housekeeping_is_rate_limited(monkeypatch):
    """Col canale a terra, ogni tick riproverebbe: una chiamata API ogni 2s, per
    sempre. La notifica di servizio passa dallo stesso dedup di tutto il resto."""
    monkeypatch.setattr(settings, "admin_ids", [11])
    bot = _FakeBot()

    alerts._dropped = 2
    await alerts.drain(bot)
    alerts._dropped = 3
    await alerts.drain(bot)

    assert len(bot.sent) == 1, "la seconda cade nella finestra di dedup"
    assert alerts._dropped == 3, "…e il conteggio resta in attesa, non si perde"


async def test_drain_on_an_empty_buffer_sends_nothing(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [11])
    bot = _FakeBot()

    assert await alerts.drain(bot) == 0
    assert not bot.sent
```

- [ ] **Step 2: Eseguire per vederli fallire**

Run: `pytest tests/unit/test_alerts.py -v -k "drain or housekeeping or delivery or repeats_are_delivered"`
Expected: FAIL — `AttributeError: module 'utils.alerts' has no attribute 'drain'`.

- [ ] **Step 3: Implementare la consegna**

Aggiungi in cima a `src/utils/alerts.py`, agli import:

```python
import asyncio
from typing import Any
```

e in fondo al file:

```python
# How often the loop looks at the buffer. Alerts are not interactive: a couple of
# seconds of latency costs nothing and keeps the loop cheap.
_POLL_INTERVAL_SECONDS = 2.0


async def _deliver(bot: Any, text: str) -> None:
    """Best effort, one message per admin. Never raises, **never logs**.

    `bot` is typed loosely on purpose: this module must stay importable and
    testable without dragging aiogram's Bot into a unit test.
    """
    global _undelivered
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
        except Exception:  # noqa: BLE001 — logging here would feed the buffer
            _undelivered += 1


async def _flush_housekeeping(bot: Any) -> int:
    """Report what this module itself lost: dropped alerts, failed deliveries.

    Two orderings matter here, and both are load-bearing:

    * **Rate-limited before anything else.** A failed delivery increments
      `_undelivered`, which would make the next tick try again — every two
      seconds, forever, while the channel is down. Running it through the same
      dedup window as any other repeat caps that at one attempt per window, and
      leaves the counters accumulating in the meantime rather than lost.
    * **Counters reset before the send.** Otherwise a housekeeping message that
      fails to deliver counts its own failure and re-reports itself.
    """
    global _dropped, _undelivered
    if not _dropped and not _undelivered:
        return 0
    send, _ = _should_send((__name__, "housekeeping"), time.monotonic())
    if not send:
        return 0
    dropped, undelivered = _dropped, _undelivered
    _dropped = _undelivered = 0
    lines = [f"[WARNING] {__name__}"]
    if dropped:
        lines.append(f"{dropped} alert scartati: buffer pieno ({_MAX_BUFFERED}).")
    if undelivered:
        lines.append(f"{undelivered} consegne fallite.")
    await _deliver(bot, "\n".join(lines))
    return 1


async def drain(bot: Any) -> int:
    """Send whatever is buffered, deduplicated. Returns how many alerts went out."""
    sent = 0
    now = time.monotonic()
    while _buffer:
        record = _buffer.popleft()
        send, suppressed = _should_send(_fingerprint(record), now)
        if not send:
            continue
        await _deliver(bot, format_alert(record, suppressed))
        sent += 1
    return sent + await _flush_housekeeping(bot)


async def alert_loop(bot: Any) -> None:
    """Endless background loop started from `main()`. Never raises out.

    Mirrors `services.backup.loop.backup_loop`, minus its logging: an exception
    logged from inside the alert path is the one thing that could turn this into
    a feedback loop.
    """
    while True:
        try:
            await drain(bot)
        except Exception:  # noqa: BLE001 — the loop must never die, nor log
            pass
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
```

- [ ] **Step 4: Eseguire i test**

Run: `pytest tests/unit/test_alerts.py -v`
Expected: PASS, 21 test (13 del Task 3 + 8 di questo).

- [ ] **Step 5: Agganciare a `main.py`**

Import, accanto agli altri `from utils...`:

```python
from utils import alerts
```

Come **prima** riga di `async def main()` — prima di `await create_tables()` — così anche
i guasti di avvio (compreso il fallback Redis del Task 1) finiscono nel buffer:

```python
    alerts.install()
```

Accanto agli altri due task in background (riga ~197):

```python
    scheduler_task = asyncio.create_task(scheduler_loop(bot))
    backup_task = asyncio.create_task(backup_loop())
    alert_task = asyncio.create_task(alerts.alert_loop(bot))
```

e nel `finally`:

```python
        scheduler_task.cancel()
        backup_task.cancel()
        alert_task.cancel()
```

- [ ] **Step 6: `.env.example`**

Sotto `ADMIN_IDS`:

```
# Alert al maintainer: ogni problema che il bot logga a questo livello o sopra
# arriva in privato agli ADMIN_IDS. CRITICAL = praticamente spento.
# Riceve solo chi ha gia' avviato il bot in privato.
ALERT_MIN_LEVEL=WARNING
```

- [ ] **Step 7: STEERING — sezione nuova**

L'ultima sezione di STEERING è la §25 («Backup & esportazione stato», riga 1812), quindi la
nuova è la **§26**. Aggiungila in fondo, e aggiungi la riga corrispondente alla tabella
«Sezioni di STEERING.md» in `INDEX.md`:

```markdown
## 26. Alert al maintainer (`utils/alerts.py`)

Ogni `log.warning`/`log.error`/`log.exception` di `src/` a livello ≥ `ALERT_MIN_LEVEL`
arriva in **DM privato** a ogni id di `ADMIN_IDS`. Non c'è niente da chiamare: è un
`logging.Handler` agganciato alla radice in `main()`, quindi un modulo nuovo che logga
un guasto è già coperto.

**Le tre regole che lo tengono in piedi:**

1. **`emit()` non fa I/O.** Bufferizza e basta. Il logging è sincrono e viene chiamato
   dentro gli handler: un invio lì bloccherebbe l'event loop a ogni riga di log.
2. **Il sender non logga mai.** Un errore di consegna che finisse nel logger rientrerebbe
   nel buffer e il bot si alimenterebbe alert all'infinito. Le consegne fallite si
   **contano** e si riportano col drain successivo.
3. **Le ripetizioni si deduplicano per template**, non per messaggio formattato: «Annuncio
   round %s fallito» è un guasto solo, che riguardi il round 7 o l'8. Finestra 300 s, e le
   soppresse si riportano — non si buttano.

**Limiti accettati, non difetti aperti:** N admin = N messaggi; riceve solo chi ha già
avviato il bot in privato (lo stesso limite di `main.py`, dove i comandi admin si
registrano best-effort); gli admin Telegram del gruppo che `is_admin` riconosce **non**
ricevono, perché la sorgente è `settings.admin_ids`; nessuna persistenza e nessun ack.

**Formato `parse_mode=None`**: un traceback non è HTML, e un `esc` dimenticato
trasformerebbe l'alert su un bug in un bug. Stessa scelta dei comandi AI (§17).
```

- [ ] **Step 8: Gate completi**

```bash
pytest -q
ruff check src/ tests/
mypy
PYTHONPATH=src python -c "import main"
```

Expected: verde. Controlla anche il coverage di `alerts.py`:

```bash
pytest --cov=src --cov-report=term-missing | grep alerts
```

Expected: ≥ 95%. `alert_loop` è un `while True` e resta scoperto: è accettabile e il gate
è sul totale, ma se il numero complessivo scende sotto 99 il piano è di aggiungere un
test che lo esegue con `asyncio.wait_for` e un `CancelledError` atteso, non di abbassare
`fail_under`.

- [ ] **Step 9: Commit**

```bash
git add src/utils/alerts.py tests/unit/test_alerts.py src/main.py .env.example STEERING.md
git commit -m "feat: gli alert admin arrivano in DM

Terzo task in background accanto a scheduler e backup: svuota il buffer e manda
a ogni ADMIN_IDS con parse_mode=None (un traceback non e' HTML).

I contatori di cio' che si perde - buffer pieno, consegne fallite - si azzerano
prima dell'invio, cosi' una notifica che fallisce non si ripresenta a ogni giro.

install() e' la prima riga di main(), quindi anche il fallback Redis dell'avvio
diventa un alert."
```

---

### Task 5: gate della Fase 0

Non scrive codice: verifica che le tre parti stiano in piedi **insieme**, e sulla vera immagine. È il passo che separa «i test passano» da «funziona».

**Files:**
- Modify: `docs/superpowers/specs/2026-08-02-refactoring-aiogram-dialog-design.md` (tabella §10)

- [ ] **Step 1: Suite intera, gate statici, import**

```bash
pytest -q
ruff check src/ tests/
mypy
PYTHONPATH=src python -c "import main"
```

Expected: `2091 passed, 30 skipped` (2067 di baseline misurata + 3 del Task 1 + 21 dei
Task 3-4), ruff e mypy silenziosi.

> Già che ci sei: `CLAUDE.md` e `INDEX.md` dicono «2092 test (2062 passed)». Il numero vero
> il 2026-08-02 era **2097 raccolti, 2067 passed**. Correggili in questo commit — un conteggio
> sbagliato nei documenti fa sembrare rotta una suite verde.

- [ ] **Step 2: I test `pg`**

```bash
docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=postgres --name pgtest postgres:16
docker exec pgtest psql -U postgres -c "CREATE DATABASE gamingbot_test;"
export TEST_PG_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test"
pytest -m pg -rxX
docker rm -f pgtest
```

Expected: 30 passed.

- [ ] **Step 3: Coverage**

```bash
pytest --cov=src --cov-report=term-missing
```

Expected: totale ≥ 99. Se è **salito**, alza `fail_under` in `pyproject.toml` al nuovo
valore e aggiungi una riga al commento-storico che c'è già sopra — è un ratchet, e il
commento è come si è tenuto finora.

- [ ] **Step 4: Prova sull'immagine di staging**

Spingi il branch, attendi la CI, punta il bot di test su `latest-<branch>`:

1. il bot parte e il log dice `FSM storage: redis`;
2. un flusso FSM completo (crea un quiz fino alla scheda);
3. **riavvia il container**: il flusso aperto sopravvive — Redis è davvero in uso.

- [ ] **Step 5: Provocare un alert vero**

Ferma Redis e riavvia il bot:

```bash
docker compose stop redis
docker compose restart bot
```

Expected, **in DM**: un messaggio `[WARNING] main` con «Redis non raggiungibile». Il bot
è vivo e risponde ai comandi. Poi:

```bash
docker compose start redis
docker compose restart bot
```

Expected: il log dice di nuovo `redis` e nessun alert.

> Questo passo è il collaudo di tutte e tre le parti in una volta: il fallback del Task 1
> esiste, l'handler del Task 3 lo cattura, la consegna del Task 4 lo recapita.

- [ ] **Step 6: Aggiornare la tabella di stato dello spec**

In `docs/superpowers/specs/2026-08-02-refactoring-aiogram-dialog-design.md` §10, porta
0.1, 0.2, 0.3 e «Gate Fase 0» a ☑.

- [ ] **Step 7: Commit e cherry-pick su main**

```bash
git add docs/superpowers/specs/2026-08-02-refactoring-aiogram-dialog-design.md
git commit -m "docs: Fase 0 chiusa - storage, aiogram 3.30, alert admin"
```

Poi porta i quattro commit su `main` con **cherry-pick, non merge**.

- [ ] **Step 8: Scrivere il piano della Fase 1**

Ora `pip install aiogram-dialog==2.6.0` non forza più nulla, quindi l'API è ispezionabile
e lo spike è pianificabile davvero. Rientra da `superpowers:writing-plans` con lo spec §4.

Il primo task di quel piano è già deciso e non va rinegoziato: **misurare le query di un
flusso completo di creazione** (3 domande + 6 modifiche + pubblicazione) con un listener
`before_cursor_execute` di SQLAlchemy. È l'unica delle cinque metriche di §4.1 dello spec
che oggi non ha un numero, e senza quello il confronto dopo non vuol dire niente.

---

## Ordine e dipendenze

```
Task 1 (storage)  →  Task 2 (aiogram)  →  Task 5 (gate)
                          ↑
Task 3 (alerts core) → Task 4 (consegna + wiring)
```

Il Task 1 va **prima** del 2 perché l'upgrade tocca `RedisStorage`: ci si arriva col percorso
Redis già sotto test invece che al buio. I Task 3-4 non dipendono dal 2 e si possono fare in
parallelo su un branch separato, ma il Task 5 li vuole tutti.
