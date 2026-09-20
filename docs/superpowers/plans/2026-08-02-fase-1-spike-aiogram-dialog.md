# Fase 1 — lo spike su aiogram-dialog · piano di implementazione

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rispondere con numeri, non con opinioni, alla domanda «aiogram-dialog conviene a questo progetto?» — misurando la baseline che manca e facendo girare la libreria dentro *questo* stack prima di riscrivere qualunque schermata.

**Architecture:** due mosse, in quest'ordine e non nell'altro. Prima si conta quante query costa oggi un flusso di creazione completo, perché senza il numero di partenza il confronto dopo non vuol dire niente. Poi si fa la **fetta verticale più sottile possibile**: `aiogram_dialog` installato, `setup_dialogs` nel bootstrap, **un** dialogo banale dietro un comando admin, e i suoi test. Quella fetta non serve a costruire una schermata: serve a verificare che la libreria conviva con i quattro middleware, con la DI per chiave `db_session`, con `RedisStorage` e coi gate statici. Se non convive, lo si scopre in un giorno invece che dopo aver riscritto 898 righe.

**Tech Stack:** Python 3.12.13 · aiogram **3.30.0** · **aiogram_dialog 2.6.0** (nuovo) · SQLAlchemy 2.0 async · pytest 8.3.4 (`asyncio_mode=auto`) · ruff 0.16.0 · mypy 2.3.0.

**Spec di riferimento:** [2026-08-02-refactoring-aiogram-dialog-design.md](../specs/2026-08-02-refactoring-aiogram-dialog-design.md) §4.
La **tabella di stato (§10 dello spec)** va aggiornata a fine fase.

**Prerequisito, già soddisfatto:** la Fase 0 è chiusa (spec §11). aiogram è a 3.30.0, che è ciò che `aiogram_dialog >= 2.3.0` richiede.

---

## Perché questo piano si ferma alla fetta verticale

Il piano della Fase 0 si è rivelato sbagliato **cinque volte**, e due di quelle volte erano test che il piano stesso aveva scritto: uno asseriva sul contatore sbagliato e restava verde sotto la mutazione che doveva catturare (spec §11.6).

Quel piano descriveva codice in un linguaggio e con librerie che conosco. Qui si tratta di scrivere finestre, widget e `getter` di una libreria che **non ho mai visto girare in questo progetto**. Scrivere adesso i passi TDD per tutte e sette le schermate di `guess/creation.py` vorrebbe dire produrre centinaia di righe di codice plausibile e non verificato — cioè rifare, in grande, l'errore che la Fase 0 ha già pagato cinque volte.

Quindi: questo piano arriva fino a **una fetta che gira**. La conversione vera di `guess/creation.py` avrà il suo piano, scritto quando l'API sarà stata eseguita e non solo letta.

---

## Global Constraints

Valgono per **ogni** task.

- **Import top-level**: `from config_data.config import settings`, **mai** `from src.config_data...` (`pythonpath = ["src"]`).
- **`from __future__ import annotations`** in ogni modulo nuovo.
- **Mai `get_settings()`**.
- **Messaggi all'utente in italiano; commenti, log e nomi in inglese.**
- **`src/handlers` NON è coperto da mypy** (`files = ["src/services", "src/database", "src/utils", "src/config_data", "src/filters"]`), ma **è** coperto da coverage.
- **Gate statici a zero findings**: `ruff check src/ tests/` e `mypy`. Ogni segnalazione nuova è una regressione del commit corrente.
- **Gate coverage**: `fail_under = 99`, ratchet — si alza, non si abbassa mai.
- **Check admin sempre via `filters.admin_filter.is_admin`**, mai `user.id in settings.admin_ids`. I router 100% admin montano il filtro **alla radice**.
- **Escaping HTML**: ogni stringa user-controlled interpolata in un messaggio HTML passa da `utils.text.esc`. Testi dei bottoni inline **non** sono HTML-parsed: niente `esc`.
- **I service non committano**: committa l'handler.
- **Branch**: si lavora su `test_giu`. **Mai** su `main` (che è antenato di `test_giu`: un merge porterebbe 127 commit, non i nostri — spec §11.5).
- **Nessuna dipendenza nuova oltre a `aiogram_dialog`**, che ne trascina tre transitive: `jinja2`, `MarkupSafe`, `cachetools`.

---

## Fatti verificati il 2026-08-02 — non ri-derivarli

Letti dal wheel `aiogram_dialog-2.6.0`, non dalla documentazione.

### API pubblica

```
setup_dialogs(router, *, dialog_manager_factory=None, message_manager=None,
              media_id_storage=None, stack_access_validator=None,
              events_isolation=None, getter=None) -> BgManagerFactory

Dialog(*windows, on_start=None, on_close=None, on_process_result=None,
       launch_mode=LaunchMode.STANDARD, getter=None, preview_data=None, name=None)
```

Esportati da `aiogram_dialog`: `Dialog`, `Window`, `DialogManager`, `StartMode`, `ShowMode`,
`LaunchMode`, `setup_dialogs`, `BgManagerFactory`, `ChatEvent`, `Data`, `SubManager`,
`AccessSettings`, `DEFAULT_STACK_ID`, `GROUP_STACK_ID`.

Widget disponibili:

| modulo | nomi |
|---|---|
| `widgets.kbd` | `Button` `Group` `Row` `Column` `SwitchTo` `Back` `Cancel` `Start` `Next` `ScrollingGroup` `Select` `Radio` `Multiselect` `Checkbox` `Counter` `Toggle` `NumberedPager` `StubScroll` `ListGroup` `Url` `WebApp` `Calendar` |
| `widgets.text` | `Const` `Format` `Case` `Multi` `Jinja` `List` `Progress` `ScrollingText` |
| `widgets.media` | `DynamicMedia` `StaticMedia` `MediaScroll` |
| `widgets.input` | `MessageInput` `TextInput` `ManagedTextInput` `CombinedInput` |
| `test_tools` | `BotClient` `MockMessageManager` |

### Il fatto che sblocca tutto: la DI funziona

`window.py:118` fa:

```python
data.update(await self.getter(**manager.middleware_data))
```

Il getter riceve **i dati dei middleware come kwargs**. Quindi un getter scritto così:

```python
async def card_getter(db_session: AsyncSession, **kwargs) -> dict:
```

riceve la sessione da `DbSessionMiddleware` **senza cambiare niente** della convenzione di
STEERING §4. Era il rischio di compatibilità più grosso della roadmap ed è chiuso.

### `MockMessageManager` e `BotClient` — l'impianto di test

`setup_dialogs(..., message_manager=...)` è il punto di iniezione: nei test ci si passa un
`MockMessageManager`, che espone `reset_history()`, `assert_one_message()`, `last_message()`,
`first_message()`, `one_message()`, `assert_answered(callback_id)`.

`BotClient` simula un utente vero contro il dispatcher reale: `send(text)`, `click(...)`,
`request_chat_join()`, `my_chat_member_update(...)`. **Non è meno fedele dell'impianto attuale
del progetto: è di più**, perché gli update passano dai middleware invece di essere iniettati
a valle. Questo è un dato che cambia la stima del costo di riscrittura dei test.

### La baseline delle query è quasi certamente zero

`handlers/guess/creation.py` tocca il DB **una volta sola**, in `cb_publish` (riga 815, l'unico
handler del file con `db_session: AsyncSession` in firma). Tutto il resto del flusso — le tre
domande, le modifiche dalla scheda, i suggerimenti — vive nello stato FSM.

**Conseguenza per lo spike, ed è la sua vera insidia:** con una baseline vicina a zero, un
`getter` che interroga il DB a ogni ridisegno non pareggia — *peggiora*. Mettere i dati in
`dialog_manager.dialog_data` smette di essere un'ottimizzazione e diventa un requisito, e il
Task 1 esiste per rendere quel confronto misurabile invece che opinabile.

---

## File Structure

| file | stato | responsabilità |
|---|---|---|
| `tests/integration/test_creation_query_cost.py` | **nuovo** | conta gli statement SQL di un flusso di creazione completo — la metrica che manca al gate, e una guardia contro le N+1 |
| `requirements.txt` | modifica | `aiogram-dialog==2.6.0` |
| `src/handlers/dialog_spike.py` | **nuovo** | la fetta verticale: un dialogo minimo dietro un comando admin. **Codice temporaneo**, e il Task 3 decide se muore o cresce |
| `src/handlers/__init__.py` | modifica | il router dello spike in `ROUTERS` |
| `src/main.py` | modifica | `setup_dialogs(dp)` nel bootstrap |
| `tests/integration/test_dialog_spike.py` | **nuovo** | guida la fetta con `BotClient` + `MockMessageManager` |
| `docs/superpowers/specs/2026-08-02-…-design.md` | modifica | tabella §10 e i numeri del gate |

---

### Task 1: la baseline che manca — quanto costa oggi un flusso di creazione

Delle cinque metriche del gate (spec §4.1) quattro hanno un numero. Questa no, ed è l'unica che
misura il **comportamento a runtime** invece della dimensione del codice. Va presa **prima** di
installare qualunque cosa, perché è il termine di paragone.

Il test che ne esce non è usa-e-getta: un conteggio di query pinnato è la guardia classica
contro le N+1, e resta utile anche se lo spike venisse abbandonato.

**Files:**
- Create: `tests/integration/test_creation_query_cost.py`
- Modify: `docs/superpowers/specs/2026-08-02-refactoring-aiogram-dialog-design.md` (§4.1, la riga «da misurare»)

**Interfaces:**
- Consumes: le fixture `engine` e `session` di `tests/conftest.py`; gli helper e gli stub già presenti in `tests/integration/test_guess_creation_flow.py`
- Produces: il numero di statement di un flusso completo, scritto nello spec

- [ ] **Step 1: Scrivere il test**

Crea `tests/integration/test_creation_query_cost.py`:

```python
"""Quanto costa, in query, creare un round dall'inizio alla pubblicazione.

Serve a due cose. La prima è il confronto: aiogram-dialog sposta i dati dallo
stato FSM ai `getter`, che girano a ogni ridisegno, e senza il numero di
partenza «costa di più» resterebbe un'impressione. La seconda vale comunque,
anche se lo spike venisse abbandonato: un conteggio pinnato è la guardia contro
le N+1, che è precisamente il difetto che non si vede finché il DB non è grande.

Il numero atteso è basso per costruzione, non per fortuna: tutto il flusso vive
nello stato FSM e il DB si tocca una volta sola, alla pubblicazione.
"""

from __future__ import annotations

import types

import pytest
from sqlalchemy import event

from handlers.guess import creation as cr
from tests.integration.test_guess_creation_flow import (
    _BOT, _Cb, _Msg, _Photo, _to_card,
)


@pytest.fixture
def sql_counter(engine):
    """Ogni statement che passa dal cursore, in ordine.

    Si aggancia al `sync_engine` sottostante perché è lì che SQLAlchemy emette
    l'evento anche per un engine async.
    """
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", _record)


@pytest.fixture
def state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=999, chat_id=1, user_id=1))


async def test_the_questions_and_the_edits_cost_nothing_at_all(
    state, sql_counter, session
):
    """Il flusso prima della pubblicazione non deve toccare il DB.

    È l'invariante che rende sensato il confronto con i `getter`: se qui
    comparisse anche una sola query, vorrebbe dire che una schermata sta
    leggendo dal DB ciò che ha già in mano.
    """
    _BOT.reset()
    await _to_card(state)

    for field, value in (("title", "Un titolo nuovo"), ("answer", "Quake")):
        await cr.cb_edit(_Cb(f"guess_new:edit:{field}"), state)
        await cr.fsm_edit_value(_Msg(value), state)

    assert sql_counter == [], (
        f"il flusso pre-pubblicazione ha toccato il DB {len(sql_counter)} volte: "
        f"{sql_counter}"
    )


async def test_publishing_costs_a_known_number_of_statements(
    state, sql_counter, session
):
    """Pinna il costo della pubblicazione.

    Non si asserisce un numero esatto — un `INSERT` in più per una colonna nuova
    è un cambio legittimo — ma un tetto: se il costo raddoppia, qualcuno ha
    introdotto una lettura per riga e questo test è il posto in cui accorgersene.
    """
    _BOT.reset()
    await _to_card(state)
    sql_counter.clear()

    await cr.cb_publish(_Cb("guess_new:publish"), state, session)

    assert sql_counter, "la pubblicazione deve scrivere qualcosa"
    assert len(sql_counter) <= 12, (
        f"la pubblicazione costa {len(sql_counter)} statement, erano ≤12: "
        + "\n".join(sql_counter)
    )
```

- [ ] **Step 2: Eseguire e leggere i numeri veri**

```bash
pytest tests/integration/test_creation_query_cost.py -v
```

Se il primo test fallisce, **non è il test a essere sbagliato**: vuol dire che il flusso
pre-pubblicazione tocca il DB, e allora il messaggio d'errore dice dove. Riportalo così com'è.

Se il secondo fallisce perché il tetto è troppo basso, alzalo **al numero misurato**, non a un
numero comodo, e scrivi nel commento quanti sono e perché.

- [ ] **Step 3: Registrare la misura nello spec**

Nello spec, §4.1, la riga della tabella:

```
| query di un flusso completo | **da misurare** | listener `before_cursor_execute` … |
```

va sostituita coi due numeri veri: quante query costa il flusso prima della pubblicazione
(atteso: 0) e quante ne costa la pubblicazione. Aggiungi una riga sotto la tabella che dica
**perché** conta: con una baseline a zero, un `getter` che interroga per render è una
regressione, non un pareggio.

- [ ] **Step 4: Gate**

```bash
pytest
ruff check src/ tests/
mypy
```

Expected: verde. Baseline della suite prima di questo task: **2096 passed, 30 skipped**.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_creation_query_cost.py docs/superpowers/specs/2026-08-02-refactoring-aiogram-dialog-design.md
git commit -m "test: pinna il costo in query della creazione di un round

E' la metrica che mancava al gate dello spike: aiogram-dialog sposta i dati
dallo stato FSM ai getter, che girano a ogni ridisegno, e senza il numero di
partenza «costa di piu'» resta un'impressione.

Vale anche se lo spike viene abbandonato: un conteggio pinnato e' la guardia
contro le N+1, che non si vedono finche' il DB non e' grande."
```

---

### Task 2: la fetta verticale — aiogram-dialog dentro *questo* stack

Non costruisce una schermata del prodotto. Verifica che la libreria conviva con ciò che questo
progetto ha già: quattro middleware in ordine fisso, la DI per chiave `db_session`, il filtro
admin alla radice dei router, `RedisStorage`, l'ordine dei router asserito da un test, e i gate
statici a zero findings.

Se qualcosa di tutto ciò non regge, si scopre qui — al costo di un file temporaneo, non di 898
righe riscritte.

**Files:**
- Modify: `requirements.txt`
- Create: `src/handlers/dialog_spike.py`
- Modify: `src/handlers/__init__.py` (tupla `ROUTERS`)
- Modify: `src/main.py` (`setup_dialogs`)
- Create: `tests/integration/test_dialog_spike.py`

**Interfaces:**
- Consumes: `filters.admin_filter.IsAdminFilter`, `database.models.User`, la sessione iniettata come `db_session`
- Produces: `router` (per `ROUTERS`), `SpikeStates` (`StatesGroup` con un solo stato `main`), `spike_dialog`

- [ ] **Step 1: Installare la dipendenza**

```bash
source .venv/bin/activate
printf 'aiogram-dialog==2.6.0\n' >> requirements.txt
pip install -r requirements-dev.txt
pip show aiogram aiogram_dialog | grep -E "^Name|^Version"
```

Expected: `aiogram 3.30.0` **invariato** e `aiogram_dialog 2.6.0`. Se pip vuole muovere aiogram,
fermati e riporta: vorrebbe dire che il vincolo verificato nella Fase 0 non vale più.

Ordina la riga in `requirements.txt` accanto ad `aiogram`, non in fondo: sono la stessa cosa.

- [ ] **Step 2: Scrivere il test che fallisce**

Crea `tests/integration/test_dialog_spike.py`:

```python
"""La fetta verticale: aiogram-dialog deve convivere con lo stack che c'è già.

Non prova una schermata del prodotto. Prova le quattro cose che, se non
reggessero, renderebbero inutile riscrivere qualunque flusso: che un dialogo
parta da un comando, che il filtro admin lo protegga, che la sessione DB arrivi
nel getter con la chiave `db_session` di §4, e che il testo mostrato venga dal
getter e non da una costante.

`BotClient` manda update veri attraverso il dispatcher reale, middleware
compresi — è un impianto più fedele di quello a stub usato altrove, non meno.
"""

from __future__ import annotations

import pytest
from aiogram import Dispatcher
from aiogram_dialog import setup_dialogs
from aiogram_dialog.test_tools import BotClient, MockMessageManager

from database.models import User
from handlers import dialog_spike
from middlewares.db_middleware import DbSessionMiddleware


@pytest.fixture
def dp(session, monkeypatch):
    """Un dispatcher col solo router dello spike e la sessione dei test."""
    async def _fake_session_middleware(handler, event, data):
        data["db_session"] = session
        return await handler(event, data)

    dispatcher = Dispatcher()
    dispatcher.update.middleware(_fake_session_middleware)
    dispatcher.include_router(dialog_spike.router)
    return dispatcher


@pytest.fixture
def message_manager():
    return MockMessageManager()


@pytest.fixture
def admin_env(monkeypatch):
    """Rende admin l'utente 1 **attraverso il codice vero**.

    `admin_filter.is_admin` controlla `settings.admin_ids` per primo, quindi
    impostare quella lista esercita la policy invece di sostituirla. È lo stesso
    approccio di `tests/unit/test_admin_routers_gated.py`, che esiste apposta
    perché non nascano due modi divergenti di simulare un admin.
    """
    from filters import admin_filter as af

    monkeypatch.setattr(af.settings, "admin_ids", [1])


async def test_an_admin_gets_the_dialog_and_the_getter_sees_the_db(
    dp, message_manager, session, user_factory, admin_env
):
    await user_factory(tg_id=1, full_name="Giupeppe")
    setup_dialogs(dp, message_manager=message_manager)
    client = BotClient(dp, user_id=1, chat_id=1)

    await client.send("/spike")

    message_manager.assert_one_message()
    assert "Utenti a DB: 1" in message_manager.last_message().text, (
        "il getter deve mostrare un conteggio letto dal DB, non una costante: "
        "è questo che dimostra che db_session è arrivato dal middleware"
    )


async def test_a_non_admin_gets_nothing(
    dp, message_manager, session, user_factory, admin_env
):
    """Il filtro alla radice deve valere anche per l'ingresso di un dialogo.

    Se fallisse, ogni dialogo admin del futuro sarebbe aperto a chiunque —
    e lo stato del dialogo, come quello FSM, non ha TTL.
    """
    await user_factory(tg_id=2, full_name="Estraneo")
    setup_dialogs(dp, message_manager=message_manager)
    client = BotClient(dp, user_id=2, chat_id=2)

    await client.send("/spike")

    assert not message_manager.sent_messages, (
        "un non-admin non deve nemmeno vedere la prima finestra"
    )
```

> **Se `message_manager.sent_messages` non esiste con quel nome**, leggi
> `aiogram_dialog/test_tools/mock_message_manager.py` (è nel wheel installato, sotto
> `.venv/lib/python3.12/site-packages/`) e usa l'attributo vero. La superficie pubblica
> verificata è: `reset_history()`, `assert_one_message()`, `last_message()`,
> `first_message()`, `one_message()`, `assert_answered(callback_id)`. Riporta nel report il
> nome che hai usato.

- [ ] **Step 3: Eseguire per vederlo fallire**

```bash
pytest tests/integration/test_dialog_spike.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'handlers.dialog_spike'`.

- [ ] **Step 4: Scrivere la fetta**

Crea `src/handlers/dialog_spike.py`:

```python
"""Fetta verticale dello spike su aiogram-dialog — codice temporaneo.

Non è una schermata del prodotto: è la prova che la libreria convive con questo
stack. Vive finché il gate della Fase 1 (spec §4.3) non decide se la libreria
resta o va via, e in entrambi i casi questo file sparisce — o perché si
abbandona, o perché lo sostituiscono le finestre vere di `guess/creation.py`.

Quello che dimostra, e che nessuna lettura della documentazione può dimostrare:
il `getter` riceve `db_session` dal middleware del progetto, quindi la DI per
chiave di STEERING §4 vale anche dentro un dialogo.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.widgets.kbd import Cancel
from aiogram_dialog.widgets.text import Const, Format
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from filters.admin_filter import IsAdminFilter

router = Router()
# Admin-only at the root: a handler driven by dialog state alone would not
# re-check anything, and dialog state has no TTL (STEERING §8).
router.message.filter(IsAdminFilter())


class SpikeStates(StatesGroup):
    main = State()


async def user_count_getter(db_session: AsyncSession, **kwargs) -> dict:
    """Reads through the session the project's middleware injected.

    The count is deliberately a real query: a constant here would prove nothing
    about dependency injection, which is the whole point of this slice.
    """
    total = await db_session.scalar(select(func.count()).select_from(User))
    return {"total": total or 0}


spike_dialog = Dialog(
    Window(
        Const("Spike aiogram-dialog."),
        Format("Utenti a DB: {total}"),
        Cancel(Const("Chiudi")),
        state=SpikeStates.main,
        getter=user_count_getter,
    ),
)

router.include_router(spike_dialog)


@router.message(Command("spike"))
async def cmd_spike(message: Message, dialog_manager: DialogManager) -> None:
    await dialog_manager.start(SpikeStates.main, mode=StartMode.RESET_STACK)
```

- [ ] **Step 5: Registrare il router e chiamare `setup_dialogs`**

In `src/handlers/__init__.py`, aggiungi `dialog_spike` alla tupla `ROUTERS`. **Non in fondo**:
`common` deve restare ultimo, ed è un invariante asserito da `tests/unit/test_router_order.py`.
Leggi quel test prima di scegliere la posizione.

In `src/main.py`, dopo `handlers.register(dp)` e prima dell'avvio del polling:

```python
    # aiogram-dialog registers its own event observers on the dispatcher; it is
    # not a router, so it goes after the routers are in place.
    setup_dialogs(dp)
```

con l'import `from aiogram_dialog import setup_dialogs` in cima.

- [ ] **Step 6: Eseguire i test**

```bash
pytest tests/integration/test_dialog_spike.py -v
pytest tests/unit/test_router_order.py -v
```

Expected: entrambi verdi. Il secondo **non è una formalità**: `test_router_order.py` scopre i
moduli registrabili cercando l'attributo `router`, e un `Dialog` non è un `Router` nello stesso
senso. Se quel test si rompe, la sua correzione è parte di questo task e va spiegata nel report,
non aggirata.

- [ ] **Step 7: Gate completi**

```bash
pytest
ruff check src/ tests/
mypy
PYTHONPATH=src python -c "import main"
pytest --cov=src --cov-report=term-missing | tail -5
```

Expected: suite verde, statici a zero, coverage ≥ 99. `dialog_spike.py` è dentro `src/handlers`,
quindi **è** contato dal coverage: i suoi test devono coprirlo o il gate scende.

- [ ] **Step 8: Commit**

```bash
git add requirements.txt src/handlers/dialog_spike.py src/handlers/__init__.py src/main.py tests/integration/test_dialog_spike.py
git commit -m "spike: aiogram-dialog gira dentro questo stack

Fetta verticale, non una schermata: un dialogo minimo dietro un comando admin,
per verificare cio' che la documentazione non puo' dimostrare — che il getter
riceva db_session dal middleware del progetto, che il filtro admin alla radice
protegga il dialogo, e che setup_dialogs conviva con l'ordine dei router.

Codice temporaneo: il gate della Fase 1 decide se muore o se lo sostituiscono
le finestre vere."
```

---

### Task 3: il gate **della fetta**, non ancora quello dello spike

Attenzione al nome, perché confonderli farebbe prendere una decisione grossa su dati piccoli.

Lo spec §4.3 descrive il gate dello **spike**, che confronta le righe di `guess/creation.py`
prima e dopo la conversione: quel confronto **non è possibile qui**, perché nessuna schermata
del prodotto è stata convertita. Questo task decide una cosa più stretta e più economica:
**la libreria è compatibile con questo progetto, sì o no?** Solo se la risposta è sì ha senso
scrivere il piano della conversione, che è dove il costo vero — la riscrittura dei test — si paga.

Non scrive codice di produzione. Mette in tabella ciò che si è imparato e lo porta all'utente,
che decide. Il criterio resta quello dello spec §4.3: **si misura tutto, decide l'utente**,
senza regola automatica di no-go.

**Files:**
- Modify: `docs/superpowers/specs/2026-08-02-refactoring-aiogram-dialog-design.md` (§4.3 e §10)

- [ ] **Step 1: Raccogliere le misure**

```bash
wc -l src/handlers/dialog_spike.py tests/integration/test_dialog_spike.py
pytest --cov=src --cov-report=term-missing | tail -5
pip show aiogram_dialog jinja2 cachetools | grep -E "^Name|^Version"
du -sh .venv/lib/python3.12/site-packages/aiogram_dialog
```

- [ ] **Step 2: Scrivere il verdetto nello spec**

Aggiungi in §4.3 una sottosezione «Cosa ha detto la fetta verticale», con **fatti**, non
impressioni:

1. la libreria convive coi quattro middleware? (sì/no, e cosa si è dovuto cambiare)
2. `db_session` arriva nel getter senza toccare la DI? (sì/no)
3. `IsAdminFilter` alla radice protegge anche il dialogo? (sì/no)
4. `test_router_order.py` ha retto, o è stato corretto? come?
5. quante righe costa una schermata banale, fra dialogo e test?
6. `BotClient` + `MockMessageManager` sono più o meno verbosi degli stub attuali? (confronta
   con `tests/integration/test_guess_creation_flow.py`, 915 righe)
7. i numeri del Task 1: query del flusso e della pubblicazione

- [ ] **Step 3: Aggiornare la tabella §10 e portare la decisione all'utente**

Il rigo «Gate spike» resta **☐**: non è questo task a chiuderlo, perché nessuna schermata è
stata convertita e il confronto sulle righe non esiste ancora. Aggiungi semmai una riga
«fetta verticale» col suo esito.

Presenta all'utente la tabella e le due strade:

- **avanti**: si scrive il piano della conversione vera di `guess/creation.py`, che è dove il
  costo dei test si paga davvero;
- **stop**: si reverta il Task 2 (`dialog_spike.py`, `setup_dialogs`, la dipendenza), il Task 1
  **resta** perché ha valore da solo, e la risposta a «conviene aiogram-dialog?» diventa
  documentata invece che opinata.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-02-refactoring-aiogram-dialog-design.md
git commit -m "docs: il verdetto della fetta verticale, coi numeri"
```

---

## Ordine e dipendenze

```
Task 1 (baseline query)  →  Task 2 (fetta verticale)  →  Task 3 (gate)
```

Il Task 1 va **prima** perché misura il progetto **senza** la libreria: dopo aver installato
`aiogram_dialog` e chiamato `setup_dialogs`, quella misura non è più vergine.
