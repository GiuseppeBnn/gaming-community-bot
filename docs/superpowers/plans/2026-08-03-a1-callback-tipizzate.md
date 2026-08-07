# A.1 — Callback tipizzate: catch-all e prime due conversioni

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** dare al bot una risposta per ogni callback non gestita, e sostituire il parsing a mano di
`callback.data` con `CallbackData` tipizzata in `handlers/schedule.py` e `handlers/events.py`.

**Architecture:** `CallbackData` è la factory che **aiogram ha già in casa** e che questo progetto
non ha mai usato: il payload si dichiara come classe, il filtro lo spacchetta e lo inietta
nell'handler come `callback_data`. Il parsing sparisce dagli handler; un payload malformato non fa
più match e finisce nel catch-all di `common`, che è l'ultimo router. Nessuna dipendenza nuova.

**Tech Stack:** Python 3.12 · aiogram 3.30.0 (`aiogram.filters.callback_data.CallbackData`) ·
pytest (asyncio in modalità automatica: i test sono `async def` nudi, senza decoratore).

**Spec:** [2026-08-03-fondamenta-presentazione-design.md](../specs/2026-08-03-fondamenta-presentazione-design.md)
— questo piano realizza **A.1**, limitatamente a due file su quindici (§7.1 di quel documento e la
nota di scope qui sotto).

## Global Constraints

- **`from __future__ import annotations`** in ogni modulo nuovo (CLAUDE.md, regola 12).
- **Messaggi all'utente in italiano**, commenti / log / nomi in **inglese**.
- **Import top-level**: `from handlers import schedule`, mai `from src.handlers…`.
- **La sessione si inietta come `db_session`**, mai `session: AsyncSession`.
- **I service non committano**: committa l'handler.
- **Escaping HTML** via `utils.text.esc` per ogni stringa user-controlled interpolata in un
  messaggio HTML. I testi dei bottoni inline **non** sono HTML-parsed: niente `esc` lì.
- **Nessuna dipendenza nuova.** Tutto ciò che serve è già installato.
- **Gate, da superare prima di ogni commit:** `pytest` verde, `pytest --cov=src` ≥ `fail_under = 99`,
  `ruff check src/ tests/` e `mypy` a **zero findings** (sono a zero: ogni segnalazione è una
  regressione nuova, non rumore preesistente).
- **Base di partenza:** branch `test_giu`, **2098 passed, 30 skipped**, coverage 99,67 %.
- **Non toccare `main`.**
- **Intoccabili** (spec §3.1): i test su denaro, XP, gating admin
  (`tests/unit/test_admin_routers_gated.py`) e ordine dei router
  (`tests/unit/test_router_order.py`). Se uno di questi diventa rosso è una **regressione**.
- I numeri di riga citati sono **al 2026-08-03** e si spostano man mano che si edita: usali per
  trovare il punto, non come indirizzo assoluto.

---

## Nota di scope

`A.1` per intero sono 15 file e 120 handler. Questo piano ne copre **due**, scelti così:

- `schedule.py` — 7 handler, e da solo esibisce **tutte** le forme di payload del progetto (nessun
  argomento, una stringa, una stringa + un intero, solo un intero). I suoi 26 punti di test stanno
  in **un** file. È il pattern.
- `events.py` — 13 handler, la grammatica più usata (`ev`, 63 occorrenze), più due irregolarità che
  il pattern deve reggere: l'azione incollata al prefisso (`ev:askstart`) e un **quinto segmento
  opzionale** (`ev:sched:<t>:<id>:close`). I payload sono costruiti anche fuori da `events.py`, in
  `event_types/quiz_type.py` e `event_types/guess_type.py`: vanno convertiti nello stesso commit o
  i bottoni smettono di funzionare.

Gli altri 13 file si pianificano quando questi due sono verdi.

---

## Fatti verificati, da non ri-verificare

Misurati eseguendo il codice il 2026-08-03, non dedotti:

1. **I campi opzionali allungano il payload.** `SchedCb(action="cancel").pack()` produce
   `'sched:cancel::'` (14 byte) dove oggi c'è `'sched:cancel'` (12). Ogni campo non usato resta come
   separatore vuoto. Con il tetto di 64 byte va tenuto d'occhio, non ignorato.
2. **`pack()` alza `ValueError` oltre i 64 byte** e **`ValueError` se un valore contiene `:`**.
   Entrambi in fase di *costruzione*, quindi si vedono in test, non in produzione.
3. **Il filtro non propaga gli errori di parsing.** `CallbackQueryFilter.__call__`
   (`aiogram/filters/callback_data.py:187-190`) cattura `TypeError` e `ValueError` e ritorna
   `False`. Conseguenza diretta: `'sched:pick:fake:abc'` → `False`, e **`'sched:cancel'` — il
   payload vecchio, senza i separatori dei campi opzionali — → `False`.
4. **Quindi i bottoni già aperti in chat al momento del deploy non fanno match** e cadono nel
   catch-all. È il comportamento voluto, ed è il motivo per cui il catch-all va per primo.
5. **Per testare un filtro serve una `CallbackQuery` vera**, non un finto: `__call__` fa
   `isinstance(query, CallbackQuery)` e un finto ritornerebbe `False` per il motivo sbagliato.
   Costruirla è a buon mercato:
   ```python
   CallbackQuery(id="1", from_user=User(id=1, is_bot=False, first_name="A"),
                 chat_instance="x", data="sched:pick:fake:7")
   ```
6. **`alert_min_level` è `WARNING`** (`config_data/config.py:27`), quindi un `log.warning` del
   catch-all raggiunge gli admin via `utils/alerts.py`.
7. **`utils/alerts.py` deduplica sul *template* del messaggio**, non sul testo formattato
   (`alerts.py`, chiave di dedup su `record.msg`). Un `log.warning("… %s", data)` collassa in un
   alert solo ogni 5 minuti; una f-string ne produrrebbe uno per click.
8. **`common.py` non ha nessun handler di callback.** Oggi una callback non gestita non riceve
   risposta affatto.

---

## File Structure

| file | responsabilità | task |
|---|---|---|
| `src/handlers/common.py` | *modifica* — aggiunge il catch-all delle callback non gestite, come ultimo handler dell'ultimo router | 1 |
| `tests/unit/test_unhandled_callback.py` | *nuovo* — risposta e log del catch-all, incluso il vincolo sul template | 1 |
| `src/handlers/callbacks.py` | *nuovo* — le classi `CallbackData` condivise. Nasce con `SchedCb`, cresce con `EventCb`. Sta in un modulo suo e non dentro i singoli handler perché i payload sono costruiti anche altrove (`event_types/`, `guess/creation.py`): metterli nell'handler creerebbe import circolari | 2, 3 |
| `tests/unit/test_callbacks.py` | *nuovo* — pack / unpack / rifiuto, per ogni forma di payload | 2, 3 |
| `src/handlers/schedule.py` | *modifica* — 7 handler passano a `SchedCb`; spariscono 4 `split(":")` e 1 `isdigit()` | 2 |
| `tests/integration/test_schedule_flow.py` | *modifica* — 26 payload grezzi diventano istanze tipizzate | 2 |
| `src/handlers/events.py` | *modifica* — 13 handler passano a `EventCb` / `PollCreateCb` | 3 |
| `src/handlers/event_types/quiz_type.py` | *modifica* — costruisce payload `ev:*`: stesso commit | 3 |
| `src/handlers/event_types/guess_type.py` | *modifica* — idem | 3 |
| `src/handlers/guess/creation.py` | *modifica* — una riga: costruisce `ev:sched:…` (riga 361) | 3 |
| `tests/integration/test_events.py`, `test_events_hub.py`, `test_guess_type.py`, `test_event_type_specs.py`, `test_quiz_play_view.py`, `tests/unit/test_admin_dashboard_kb.py` | *modifica* — 73 payload grezzi | 3 |

---

### Task 1: il catch-all delle callback non gestite

Va per primo perché è la rete sotto tutte le conversioni successive (fatto 4), e perché vale da solo:
oggi un bottone di una tastiera più vecchia del deploy non produce **niente** e la rotellina gira
finché Telegram non molla (fatto 8).

**Files:**
- Modify: `src/handlers/common.py` (aggiunte in testa agli import e in coda al file)
- Test: `tests/unit/test_unhandled_callback.py` (nuovo)

**Interfaces:**
- Consumes: niente.
- Produces: `common.cb_unhandled(callback) -> None` e la costante
  `common._UNHANDLED_CALLBACK: str`. I task 2 e 3 non li importano, ma ci contano: dopo la
  conversione un payload vecchio finisce lì.

- [ ] **Step 1: scrivere il test che fallisce**

Crea `tests/unit/test_unhandled_callback.py`:

```python
"""Una callback che nessuno gestisce riceve comunque una risposta.

`common.router` è l'ultimo (`handlers/__init__.py`), e prima di questo handler non
aveva **nessun** handler di callback: un bottone di una tastiera più vecchia del
deploy corrente non produceva niente e la rotellina restava a girare finché
Telegram non mollava.

Due cose atterrano qui, e vanno distinte: un bottone vecchio — normale, l'utente
merita una risposta — e un handler che ha smesso di fare match per sbaglio, che
senza un log resterebbe muto per sempre.
"""

from __future__ import annotations

import logging

from handlers import common


class _FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


async def test_unhandled_callback_gets_an_answer():
    callback = _FakeCallback("sched:cancel")

    await common.cb_unhandled(callback)

    assert callback.answers, "senza risposta la rotellina resta a girare"
    text, _alert = callback.answers[0]
    assert text == common._UNHANDLED_CALLBACK


async def test_unhandled_callback_is_logged_for_the_admins(caplog):
    callback = _FakeCallback("ev:list:quiz")

    with caplog.at_level(logging.WARNING):
        await common.cb_unhandled(callback)

    record = next(r for r in caplog.records if "Callback non gestita" in r.getMessage())
    assert "ev:list:quiz" in record.getMessage()
    assert record.msg == "Callback non gestita: %s", (
        "il payload deve restare un argomento: utils.alerts deduplica sul template, "
        "e una f-string trasformerebbe ogni click su un bottone vecchio in un alert nuovo"
    )
```

- [ ] **Step 2: eseguirlo e vederlo fallire**

Run: `pytest tests/unit/test_unhandled_callback.py -v`
Expected: FAIL — `AttributeError: module 'handlers.common' has no attribute 'cb_unhandled'`

- [ ] **Step 3: implementare**

In `src/handlers/common.py`, aggiungi agli import (`logging` non c'è ancora, `CallbackQuery`
neppure):

```python
import logging
import re

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
```

Subito dopo `router = Router()`:

```python
log = logging.getLogger(__name__)

#: Risposta per una callback che nessuno ha rivendicato. Corta: esce come toast.
_UNHANDLED_CALLBACK = "Questo bottone non è più valido."
```

In **fondo al file**:

```python
@router.callback_query()
async def cb_unhandled(callback: CallbackQuery) -> None:
    """Answer any callback no router claimed, instead of leaving the spinner up.

    `common.router` is last (`handlers/__init__.py`), so nothing that another
    router wanted can reach here. Two cases do: a button from a keyboard older
    than the current deploy — normal, and the user deserves a reply — and a
    handler that stopped matching by mistake, which would otherwise be silent
    forever. Hence the WARNING: it reaches the admins through utils.alerts
    (STEERING §26), so a button that quietly stops working reports itself.

    The `%s` is deliberate, not style: utils.alerts deduplicates on the message
    *template*, so an f-string would turn every stale click into its own alert
    and drown the channel meant to protect us.
    """
    log.warning("Callback non gestita: %s", callback.data)
    await callback.answer(_UNHANDLED_CALLBACK)
```

- [ ] **Step 4: eseguire il test e vederlo passare**

Run: `pytest tests/unit/test_unhandled_callback.py -v`
Expected: PASS, 2 test.

- [ ] **Step 5: eseguire la suite intera**

Run: `pytest`
Expected: **2100 passed, 30 skipped**.

Se qualche test diventa rosso, è quasi certamente uno che asserisce che una certa callback **non**
riceve risposta. Non silenziarlo: leggilo. Se il suo punto era «questo bottone non fa niente»,
adesso fa qualcosa di meglio (dice perché), e il test va aggiornato dicendolo nel commit. Se invece
era un test di sicurezza — «un non-admin non deve ricevere risposta» — allora è una **regressione**
e va risolta restringendo il catch-all, non il test.

- [ ] **Step 6: gate**

Run: `ruff check src/ tests/ && mypy && pytest --cov=src --cov-report=term | tail -2`
Expected: `All checks passed!`, `Success: no issues found`, coverage ≥ 99.

- [ ] **Step 7: commit**

```bash
git add src/handlers/common.py tests/unit/test_unhandled_callback.py
git commit -m "$(cat <<'EOF'
feat: una callback non gestita adesso riceve una risposta

`common.router` è l'ultimo e non aveva nessun handler di callback: un
bottone di una tastiera più vecchia del deploy non produceva niente e la
rotellina girava finché Telegram non mollava.

Il log usa %s e non una f-string perché utils.alerts deduplica sul
template: formattato, ogni click su un bottone vecchio sarebbe un alert
nuovo, e affogherebbe il canale che dovrebbe proteggerci.

Serve anche da rete per le conversioni a CallbackData che seguono: un
payload che non fa più match cade qui invece di restare muto.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `SchedCb` e la conversione di `handlers/schedule.py`

Il pattern. `schedule.py` esibisce **tutte** le forme di payload del progetto e i suoi test stanno in
un file solo.

**La grammatica di oggi, per intero:**

| payload | handler | riga |
|---|---|---|
| `sched:cancel` | `cb_sched_cancel` | 106 |
| `sched:cancel_yes` | `cb_sched_cancel_yes` | 121 |
| `sched:cancel_no` | `cb_sched_cancel_no` | 128 |
| `sched:act:<action_key>` | `cb_action` | 186 |
| `sched:type:<event_type_key>` | `cb_type` | 200 |
| `sched:pick:<task_type>:<item_id>` | `cb_pick_event` | 219 |
| `sched:del:<task_id>` | `cb_sched_del` | 277 |

**Files:**
- Create: `src/handlers/callbacks.py`, `tests/unit/test_callbacks.py`
- Modify: `src/handlers/schedule.py` (righe 52, 84-85, 106-130, 139-140, 163-164, 186-232, 272-286)
- Modify: `tests/integration/test_schedule_flow.py` (26 payload grezzi)

**Interfaces:**
- Consumes: il catch-all del Task 1 (un payload vecchio ci cade dentro).
- Produces: `handlers.callbacks.SchedCb`, con campi `action: str`, `key: str | None = None`,
  `item_id: int | None = None`, prefisso `"sched"`. Il Task 3 aggiunge classi **nello stesso
  modulo** e non tocca questa.

- [ ] **Step 1: scrivere il test che fallisce**

Crea `tests/unit/test_callbacks.py`:

```python
"""Typed callback payloads.

Before this module every screen invented its own grammar and re-parsed it by
hand — `_, _, task_type, raw = callback.data.split(":")`, over and over, with
`isdigit()` guards scattered around and the 64-byte limit respected by eye.

What is pinned here is the three things hand-rolled parsing never guaranteed:
that the payload produced is the one expected, that a malformed payload does
**not** reach the handler, and that Telegram's limits show up as test failures
instead of broken buttons in a chat.
"""

from __future__ import annotations

import pytest
from aiogram.types import CallbackQuery, User

from handlers.callbacks import SchedCb


def _query(data: str) -> CallbackQuery:
    """A real CallbackQuery: the filter does an `isinstance` check, so a fake
    would return `False` for the wrong reason."""
    return CallbackQuery(
        id="1", from_user=User(id=1, is_bot=False, first_name="A"),
        chat_instance="x", data=data,
    )


@pytest.mark.parametrize("cb, expected", [
    (SchedCb(action="cancel"), "sched:cancel::"),
    (SchedCb(action="type", key="quiz"), "sched:type:quiz:"),
    (SchedCb(action="pick", key="quiz", item_id=7), "sched:pick:quiz:7"),
    (SchedCb(action="del", item_id=7), "sched:del::7"),
])
def test_pack(cb, expected):
    assert cb.pack() == expected


def test_unpack_restores_the_types():
    cb = SchedCb.unpack("sched:pick:quiz:7")
    assert cb.action == "pick"
    assert cb.key == "quiz"
    assert cb.item_id == 7, "the id must come back as int, not str"


async def test_a_non_numeric_id_never_reaches_the_handler():
    """Today `cb_pick_event` guards with `raw_id.isdigit()`. Tomorrow it never arrives."""
    assert await SchedCb.filter()(_query("sched:pick:quiz:abc")) is False


async def test_a_well_formed_payload_is_injected():
    result = await SchedCb.filter()(_query("sched:pick:quiz:7"))
    assert result == {"callback_data": SchedCb(action="pick", key="quiz", item_id=7)}


async def test_a_payload_from_an_older_deploy_falls_through():
    """Optional fields still cost their separators: the old payload is shorter.

    Not a flaw to hide — it is the reason the catch-all in `common` (Task 1)
    exists, and the reason it lands first.
    """
    assert await SchedCb.filter()(_query("sched:cancel")) is False


def test_the_separator_cannot_hide_in_a_value():
    with pytest.raises(ValueError, match="Separator symbol"):
        SchedCb(action="type", key="a:b").pack()


def test_the_64_byte_ceiling_shows_up_in_tests_not_in_chat():
    with pytest.raises(ValueError, match="too long"):
        SchedCb(action="x" * 70).pack()
```

- [ ] **Step 2: eseguirlo e vederlo fallire**

Run: `pytest tests/unit/test_callbacks.py -v`
Expected: FAIL in raccolta — `ModuleNotFoundError: No module named 'handlers.callbacks'`

- [ ] **Step 3: creare il modulo**

Crea `src/handlers/callbacks.py`:

```python
"""Typed callback payloads, in one place.

Every screen used to invent its own grammar — `guess_new:edit:<field>`,
`ev:sched:<type>:<id>:close`, `quiz_edit:nav:<id>:<i>` — and every handler
re-parsed it by hand. `CallbackData` is aiogram's own answer to that, and it was
never used here: the filter unpacks the payload and injects it as `callback_data`,
so the parsing, the `isdigit()` guards and the 64-byte discipline all move from
the handler to the type.

They live in a module of their own, not next to their handlers, because the same
payloads are built elsewhere: `event_types/` and `guess/creation.py` render the
Events hub buttons. Keeping the classes in `handlers/events.py` would make those
modules import a handler.

**Optional fields still cost their separator.** `SchedCb(action="cancel")` packs
to `"sched:cancel::"`, not `"sched:cancel"` — which is exactly why a button drawn
by an older deploy no longer matches, and falls through to the catch-all in
`handlers/common.py`.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class SchedCb(CallbackData, prefix="sched"):
    """The scheduling flow — `handlers/schedule.py`.

    `key` carries the one string the action refers to: an event-type key for
    "type" and "pick", a schedulable-action key ("start" / "close") for "act".
    """

    #: "cancel" | "cancel_yes" | "cancel_no" | "act" | "type" | "pick" | "del"
    action: str
    key: str | None = None
    item_id: int | None = None
```

- [ ] **Step 4: eseguire il test e vederlo passare**

Run: `pytest tests/unit/test_callbacks.py -v`
Expected: PASS, 10 test.

- [ ] **Step 5: convertire gli handler**

In `src/handlers/schedule.py`, aggiungi l'import:

```python
from handlers.callbacks import SchedCb
```

**Trova i punti così, non a occhio** — i payload non compaiono solo come `callback_data=`: alcuni
sono argomenti posizionali di `confirm_cancel_kb`, e cercare la parola sbagliata li salterebbe.

```bash
grep -n '"sched:\|f"sched:' src/handlers/schedule.py
```

Sono **16 righe**: 52, 84, 85, 106, 116, 121, 128, 139, 140, 163, 164, 186, 200, 219, 272, 277.
Se il tuo conto e il mio non tornano, **vince il `grep`**: i numeri in prosa invecchiano, il comando no.
Quelle nei decoratori (106, 121, 128, 186, 200, 219, 277) le tratta lo Step successivo; le altre
sono costruzione di bottoni. Esempi, uno per forma:

```python
# riga 52 e 85 e 140 e 164 — «❌ Annulla»
callback_data=SchedCb(action="cancel").pack()

# riga 84
b.button(text=et.hub_label, callback_data=SchedCb(action="type", key=et.key).pack())

# riga 139
b.button(text=f"#{iid} {label[:30]}",
         callback_data=SchedCb(action="pick", key=task_type, item_id=iid).pack())

# riga 163
b.button(text=button, callback_data=SchedCb(action="act", key=key).pack())

# riga 272
b.button(text=f"❌ Annulla #{t.id}", callback_data=SchedCb(action="del", item_id=t.id).pack())

# riga 116 — argomenti POSIZIONALI di confirm_cancel_kb, non `callback_data=`:
# è il punto che una ricerca per "callback_data" salterebbe.
reply_markup=confirm_cancel_kb(
    SchedCb(action="cancel_yes").pack(), SchedCb(action="cancel_no").pack()
),
```

`confirm_cancel_kb` (`keyboards/common_kb.py:9-21`) continua a prendere due `str` e **non si
tocca**: la usano anche `betting.py`, `quiz/creation.py` e `guess/creation.py`, che in questo piano
non si convertono.

Poi i sette handler. I tre senza argomenti perdono solo il confronto letterale:

```python
@router.callback_query(SchedCb.filter(F.action == "cancel"))
async def cb_sched_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    ...  # corpo invariato

@router.callback_query(SchedCb.filter(F.action == "cancel_yes"))
async def cb_sched_cancel_yes(callback: CallbackQuery, state: FSMContext) -> None:
    ...  # corpo invariato

@router.callback_query(SchedCb.filter(F.action == "cancel_no"))
async def cb_sched_cancel_no(callback: CallbackQuery) -> None:
    ...  # corpo invariato
```

I quattro con argomenti perdono lo `split` e prendono `callback_data`:

```python
@router.callback_query(SchedCb.filter(F.action == "act"))
async def cb_action(
    callback: CallbackQuery, callback_data: SchedCb, state: FSMContext
) -> None:
    action = callback_data.key
    data = await state.get_data()
    if action not in _ACTIONS or "sched_ref" not in data:
        await callback.answer("Ricomincia da /programma.", show_alert=True)
        return
    await _ask_run_at(callback.message, state, data["sched_label"], action)
    await callback.answer()


@router.callback_query(SchedCb.filter(F.action == "type"))
async def cb_type(
    callback: CallbackQuery, callback_data: SchedCb, state: FSMContext, db_session
) -> None:
    et = event_types.get(callback_data.key)
    ...  # resto invariato


@router.callback_query(SchedCb.filter(F.action == "pick"))
async def cb_pick_event(
    callback: CallbackQuery, callback_data: SchedCb, state: FSMContext
) -> None:
    task_type = callback_data.key
    et = event_types.get(task_type)
    # No isdigit() guard: a non-numeric id no longer reaches this handler, the
    # filter drops it (tests/unit/test_callbacks.py).
    if et is None or callback_data.item_id is None:
        await callback.answer()
        return
    ref_id = callback_data.item_id
    await start_schedule_for(
        callback.message, state, task_type, ref_id, f"{et.hub_label} #{ref_id}"
    )
    await callback.answer()


@router.callback_query(SchedCb.filter(F.action == "del"))
async def cb_sched_del(callback: CallbackQuery, callback_data: SchedCb, db_session) -> None:
    task_id = callback_data.item_id
    if task_id is None:
        await callback.answer()
        return
    ok = await schedule_service.cancel(db_session, task_id)
    await db_session.commit()
    await callback.answer("Annullato." if ok else "Non annullabile.", show_alert=not ok)
    if ok:
        await callback.message.edit_text(f"❌ Evento #{task_id} annullato.")
```

Nota su `cb_sched_del`: oggi fa `int(callback.data.split(":")[2])` **senza** guardia, quindi un
payload storto solleverebbe `ValueError` fin dentro `dp.errors`. Adesso non arriva. Il ramo
`item_id is None` copre il caso residuo — `sched:del::` — che è costruibile ma non lo costruisce
nessuno.

- [ ] **Step 6: aggiornare i payload nel test**

`grep -c '"sched:' tests/integration/test_schedule_flow.py` dà **26 righe**; le occorrenze sono di
più, perché alcune righe ne contengono due. Conta il `grep`, non questa frase.

In `tests/integration/test_schedule_flow.py`, aggiungi `from handlers.callbacks import SchedCb` e
sostituisci ogni payload grezzo con l'istanza corrispondente. Il finto `_FakeCallback` **resta**:
cambia solo cosa gli si passa, perché gli handler ora ricevono `callback_data` come argomento.

```python
# prima
await schedule.cb_type(_FakeCallback("sched:type:fake"), state, session)
# dopo
cb = SchedCb(action="type", key="fake")
await schedule.cb_type(_FakeCallback(cb.pack()), cb, state, session)
```

Il `data` sul finto non lo legge più nessuno — l'handler riceve l'oggetto tipizzato — ma passargli
`cb.pack()` invece di un segnaposto costa una riga e tiene il test leggibile: chi lo apre vede
ancora quale bottone è stato premuto.

**Un test cambia di natura, e non va perso.** Oggi esiste un caso con `"sched:pick:fake:abc"` che
verifica «un id non numerico non crea niente». Quell'asserzione non ha più senso a livello di
handler, perché il payload non ci arriva: la copertura si è **spostata** in
`tests/unit/test_callbacks.py::test_a_non_numeric_id_never_reaches_the_handler`, scritto allo Step 1.
Cancella il caso vecchio e cita quello nuovo nel commento, così chi legge non pensa a una
copertura persa.

I casi `"sched:type:inesistente"` e `"sched:act:inventata"` invece **restano** a livello di handler:
sono stringhe legittime che il payload accetta e che l'handler rifiuta, ed è lì che vanno provate.

- [ ] **Step 6-bis: verificare che non sia rimasto niente**

```bash
grep -n '"sched:\|f"sched:' src/handlers/schedule.py
```

Expected: **nessun risultato**. Un solo letterale sopravvissuto è un bottone che non funziona più,
e la suite può benissimo non accorgersene — nessun test preme *tutti* i bottoni.

- [ ] **Step 7: eseguire la suite intera**

Run: `pytest`
Expected: verde. Il conteggio cala di 1 rispetto al Task 1 (il caso `abc` si sposta da integration a
unit e i parametrizzati ne aggiungono altri): non inseguire un numero, guarda che sia **verde** e
che il file `test_callbacks.py` sia passato tutto.

- [ ] **Step 8: gate**

Run: `ruff check src/ tests/ && mypy && pytest --cov=src --cov-report=term-missing | tail -3`
Expected: zero findings, coverage ≥ 99. Se `mypy` si lamenta di `callback_data.key` come
`str | None` dove serve `str`, **non** mettere un `# type: ignore`: aggiungi il ramo `is None` che
manca. È esattamente il difetto che il parsing a mano nascondeva.

- [ ] **Step 9: commit**

```bash
git add src/handlers/callbacks.py src/handlers/schedule.py \
        tests/unit/test_callbacks.py tests/integration/test_schedule_flow.py
git commit -m "$(cat <<'EOF'
refactor: schedule.py parla CallbackData invece di split(":")

Prima classe tipizzata del progetto, in un modulo suo perché gli stessi
payload verranno costruiti anche fuori dagli handler.

Spariscono quattro `split(":")` e una guardia `isdigit()`: un id non
numerico non arriva più all'handler, lo scarta il filtro. La copertura di
quel caso si sposta da test_schedule_flow.py a test_callbacks.py, dove
adesso vive — non è persa.

`cb_sched_del` faceva `int(...)` senza guardia: un payload storto sarebbe
finito in dp.errors. Ora non arriva.

Nota per il deploy: i campi opzionali lasciano i separatori, quindi
`sched:cancel` diventa `sched:cancel::` e i bottoni già aperti in chat non
fanno più match. Cadono nel catch-all, che per questo è arrivato prima.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `EventCb`, `PollCreateCb` e la conversione di `handlers/events.py`

Lo stress test. Tredici handler, la grammatica più usata, e due irregolarità.

**La grammatica di oggi, per intero:**

| payload | handler | riga |
|---|---|---|
| `ev:home` | `cb_home` | 112 |
| `ev:list:<et_key>` | `cb_list` | 119 |
| `ev:item:<task_type>:<id>` | `cb_item` | 129 |
| `ev:ask{start,del,close,reset}:<task_type>:<id>` | `cb_confirm` | 168 |
| `ev:start:<task_type>:<id>` | `cb_start` | 194 |
| `ev:close:<task_type>:<id>` | `cb_close` | 208 |
| `ev:del:<task_type>:<id>` | `cb_del` | 225 |
| `ev:reset:<task_type>:<id>` | `cb_reset` | 240 |
| `ev:sched:<task_type>:<id>` e `ev:sched:<task_type>:<id>:close` | `cb_schedule` | 267 |
| `ev:new:<et_key>` | `cb_new` | 289 |
| `ev:pt:cancel` / `ev:pt:cancel_yes` / `ev:pt:cancel_no` | 3 handler | 363, 375, 382 |

**Le due decisioni di modellazione, e perché.**

*Il quinto segmento opzionale* (`ev:sched:<t>:<id>:close`) **non** diventa un campo in più. Un campo
usato da un'azione sola su dieci sarebbe un separatore vuoto in ogni altro payload, e un campo il cui
significato dipende dall'azione è la stessa disonestà che il parsing a mano si permetteva. Diventa
un'**azione a sé**: `action="sched"` e `action="sched_close"`. Il payload risultante,
`ev:sched_close:quiz:7`, è lungo esattamente quanto quello di oggi.

*Il triangolo `ev:pt:*`* — annulla la creazione di un sondaggio — **non** è navigazione dell'hub: si
è accampato sotto il prefisso `ev` senza condividerne i campi. Prende una classe e un prefisso suoi,
`PollCreateCb(prefix="evpt")`. Così `EventCb` resta a tre campi invece di quattro, e i payload di
tutti gli altri bottoni si accorciano di un separatore.

*L'azione incollata al prefisso* (`ev:askstart`) invece non è un problema: diventa `action="askstart"`
e il payload prodotto è **identico** a quello di oggi, carattere per carattere.

*E `_CONFIRM` non è una tabella di etichette.* Il primo elemento di ogni tupla
(`events.py:156-165`) è **un pezzo di payload** — `"ev:start"`, `"ev:close"`, `"ev:del"`,
`"ev:reset"` — che `cb_confirm` concatena a mano per costruire il bottone «Sì»:
`f"{exec_prefix}:{task_type}:{raw}"`. Va convertito insieme al resto, e diventa più onesto: il campo
smette di essere un prefisso e diventa **il nome dell'azione da eseguire**, che è ciò che ha sempre
significato.

**Files** — la lista viene da `grep -rn '"ev:\|f"ev:' src/handlers/`, che dà **61 letterali in 7
file**. Cercare solo dove stanno gli handler ne lascerebbe indietro quattro file su sette, e il
risultato sarebbe una manciata di bottoni che rispondono «Questo bottone non è più valido»:

- Modify: `src/handlers/callbacks.py` (aggiunge due classi)
- Modify: `tests/unit/test_callbacks.py` (aggiunge i casi di `ev`)
- Modify: `src/handlers/events.py` — **24** letterali, 13 handler
- Modify: `src/handlers/event_types/quiz_type.py` — **13**
- Modify: `src/handlers/event_types/guess_type.py` — **13**
- Modify: `src/handlers/event_types/bet_type.py` — **3** (righe 38, 41, 43)
- Modify: `src/handlers/event_types/poll_type.py` — **3** (righe 34, 37, 38)
- Modify: `src/handlers/guess/creation.py` — **3** (righe 360, 361, 362)
- Modify: `src/handlers/quiz/editing.py` — **2** (righe 66, 97: «⬅️ Torna al quiz»)
- Modify: `tests/integration/test_events.py`, `tests/integration/test_events_hub.py`,
  `tests/integration/test_guess_type.py`, `tests/integration/test_event_type_specs.py`,
  `tests/integration/test_quiz_play_view.py`, `tests/unit/test_admin_dashboard_kb.py`

**Interfaces:**
- Consumes: `handlers.callbacks.SchedCb` esiste già (Task 2) e non si tocca; il catch-all del Task 1.
- Produces: `handlers.callbacks.EventCb` (`action: str`, `task_type: str | None = None`,
  `item_id: int | None = None`, prefisso `"ev"`) e `handlers.callbacks.PollCreateCb`
  (`action: str`, prefisso `"evpt"`).

- [ ] **Step 1: scrivere il test che fallisce**

Aggiungi in coda a `tests/unit/test_callbacks.py`:

```python
from handlers.callbacks import EventCb, PollCreateCb


@pytest.mark.parametrize("cb, expected", [
    (EventCb(action="home"), "ev:home::"),
    (EventCb(action="list", task_type="quiz"), "ev:list:quiz:"),
    (EventCb(action="item", task_type="quiz", item_id=7), "ev:item:quiz:7"),
    # byte-for-byte the payload we ship today
    (EventCb(action="askstart", task_type="quiz", item_id=7), "ev:askstart:quiz:7"),
    # today's optional 5th segment becomes an action of its own, same length
    (EventCb(action="sched", task_type="quiz", item_id=7), "ev:sched:quiz:7"),
    (EventCb(action="sched_close", task_type="quiz", item_id=7), "ev:sched_close:quiz:7"),
    (PollCreateCb(action="cancel"), "evpt:cancel"),
])
def test_pack_events(cb, expected):
    assert cb.pack() == expected


async def test_the_poll_triangle_does_not_answer_to_the_hub_prefix():
    """`evpt` is a family of its own: a hub payload must not match it."""
    assert await PollCreateCb.filter()(_query("ev:item:quiz:7")) is False
    assert await EventCb.filter()(_query("evpt:cancel")) is False


def test_the_longest_real_event_payload_fits():
    """The ceiling is 64 bytes, and event-type keys are chosen by whoever writes the code."""
    packed = EventCb(action="sched_close", task_type="guess_sound", item_id=999_999).pack()
    assert len(packed.encode()) <= 64, packed
```

- [ ] **Step 2: eseguirlo e vederlo fallire**

Run: `pytest tests/unit/test_callbacks.py -v`
Expected: FAIL — `ImportError: cannot import name 'EventCb' from 'handlers.callbacks'`

- [ ] **Step 3: aggiungere le due classi**

In coda a `src/handlers/callbacks.py`:

```python
class EventCb(CallbackData, prefix="ev"):
    """The Events hub — `handlers/events.py`, plus the buttons that `event_types/`
    and `guess/creation.py` draw for it.

    `action` absorbs two things the old grammar smeared across segments. The
    confirm step glued its verb to the prefix (`ev:askstart`), which is simply an
    action name here. And scheduling used an optional 5th segment to pin *what* to
    schedule (`ev:sched:<t>:<id>:close`); that is an action of its own now —
    "sched_close" — because a field only one action in ten fills would be an empty
    separator in every other payload, and a field whose meaning depends on the
    action is the same dishonesty the hand-rolled parsing allowed itself.
    """

    #: "home" | "list" | "item" | "new"
    #: | "ask{start,del,close,reset}" | "start" | "close" | "del" | "reset"
    #: | "sched" | "sched_close"
    action: str
    task_type: str | None = None
    item_id: int | None = None


class PollCreateCb(CallbackData, prefix="evpt"):
    """Cancelling poll creation — `handlers/events.py`, the `ev:pt:*` triangle.

    It squatted under the `ev` prefix without sharing any of its fields. Given a
    prefix of its own, `EventCb` stays at three fields instead of four and every
    other hub payload loses a separator.
    """

    #: "cancel" | "cancel_yes" | "cancel_no"
    action: str
```

- [ ] **Step 4: eseguire il test e vederlo passare**

Run: `pytest tests/unit/test_callbacks.py -v`
Expected: PASS.

- [ ] **Step 5: convertire chi costruisce i bottoni — prima degli handler**

**Sette** file disegnano bottoni dell'hub, non tre. Trovali così, e non a memoria:

```bash
grep -rn '"ev:\|f"ev:' src/handlers/
```

Se si convertono gli handler prima, i bottoni esistenti smettono di far match a metà commit;
l'ordine giusto è payload prima, handler dopo, e tutto in **un** commit.

In `src/handlers/events.py` (righe 73-83):

```python
b.button(text=et.hub_label, callback_data=EventCb(action="list", task_type=et.key).pack())
b.button(text="⬅️ Dashboard", callback_data="adm:home")   # `adm` non è di questo task
...
b.button(text="▶️ Avvia ora",
         callback_data=EventCb(action="start", task_type=task_type, item_id=item_id).pack())
b.button(text="🗓️ Programma",
         callback_data=EventCb(action="sched", task_type=task_type, item_id=item_id).pack())
b.button(text="⬅️ Indietro",
         callback_data=EventCb(action="list", task_type=task_type).pack())
```

In `src/handlers/event_types/quiz_type.py` (righe 93-101) e
`src/handlers/event_types/guess_type.py` (righe 146-163), stessa sostituzione. Le due che cambiano
forma:

```python
# era: f"ev:sched:quiz:{item_id}:close"
callback_data=EventCb(action="sched_close", task_type="quiz", item_id=item_id).pack()

# era: f"ev:askdel:{self.key}:{item_id}"
callback_data=EventCb(action="askdel", task_type=self.key, item_id=item_id).pack()
```

In `src/handlers/guess/creation.py` (riga 361):

```python
b.button(text="🗓️ Programma",
         callback_data=EventCb(action="sched", task_type=kind, item_id=round_id).pack())
```

In `src/handlers/event_types/bet_type.py` (righe 38, 41, 43),
`src/handlers/event_types/poll_type.py` (righe 34, 37, 38) e `src/handlers/quiz/editing.py`
(righe 66, 97), le stesse tre forme — `ev:item:<t>:<id>`, `ev:new:<k>`, `ev:home`:

```python
callback_data=EventCb(action="item", task_type="bet", item_id=e.id).pack()
callback_data=EventCb(action="new", task_type="bet").pack()
callback_data=EventCb(action="home").pack()
```

E il triangolo del sondaggio, in `_pt_cancel_kb()` e alla riga 370, dove i payload sono **argomenti
posizionali** di `confirm_cancel_kb` e non `callback_data=`:

```python
callback_data=PollCreateCb(action="cancel").pack()

reply_markup=confirm_cancel_kb(
    PollCreateCb(action="cancel_yes").pack(), PollCreateCb(action="cancel_no").pack()
),
```

Una riga che **non** si tocca: `events.py:74`, `callback_data="adm:home"`. È la grammatica della
dashboard, non dell'hub, e in questo piano non si converte.

- [ ] **Step 6: convertire i tredici handler**

Il modello, per le tre forme:

```python
@router.callback_query(EventCb.filter(F.action == "home"), IsAdminCallbackFilter())
async def cb_home(callback: CallbackQuery) -> None:
    ...  # corpo invariato


@router.callback_query(EventCb.filter(F.action == "list"), IsAdminCallbackFilter())
async def cb_list(callback: CallbackQuery, callback_data: EventCb, db_session) -> None:
    et = event_types.get(callback_data.task_type)
    ...  # resto invariato


@router.callback_query(EventCb.filter(F.action == "item"), IsAdminCallbackFilter())
async def cb_item(callback: CallbackQuery, callback_data: EventCb, db_session) -> None:
    et = event_types.get(callback_data.task_type)
    if et is None or callback_data.item_id is None:
        await callback.answer()
        return
    ...  # corpo invariato. The raw.isdigit() guard goes: nothing non-numeric arrives.
```

`cb_confirm` è il punto che cambia di più, perché smette di concatenare payload a mano. Prima
`_CONFIRM` perde il prefisso e tiene il **nome** dell'azione:

```python
_CONFIRM: dict[str, tuple[str, str, str]] = {
    #        the action to run ─┐  (was the payload prefix "ev:start")
    "askstart": ("start", "avviare subito nel gruppo", "▶️ Sì, avvia"),
    "askclose": ("close", "chiudere ora (pubblica il podio)", "🏁 Sì, chiudi"),
    "askdel":   ("del", "eliminare <b>definitivamente</b>", "🗑️ Sì, elimina"),
    # «e premi» diceva il falso: i premi già pagati restano pagati, e alla chiusura
    # successiva il montepremi viene erogato di nuovo per intero. È voluto — una
    # riproposizione è un evento nuovo — quindi è il testo che va detto com'è.
    "askreset": ("reset", "riproporre (azzera le risposte e ripaga il montepremi intero)",
                 "🔁 Sì, riproponi"),
}
```

Poi l'handler filtra su tutta la famiglia `ask*` invece che su un valore solo. `_CONFIRM` ha già le
quattro chiavi giuste, quindi `F.action.in_(_CONFIRM)` — verificato: `in_` su un `dict` guarda le
chiavi — non introduce una lista nuova da tenere allineata a mano, e il ramo `conf is None` sparisce
con essa:

```python
@router.callback_query(EventCb.filter(F.action.in_(_CONFIRM)), IsAdminCallbackFilter())
async def cb_confirm(callback: CallbackQuery, callback_data: EventCb) -> None:
    exec_action, verb, yes_text = _CONFIRM[callback_data.action]
    et = event_types.get(callback_data.task_type)
    if et is None or callback_data.item_id is None:
        await callback.answer()
        return
    item_id = callback_data.item_id
    await edit_or_send(
        callback.message,
        f"⚠️ Vuoi {verb} <b>{et.hub_label} #{item_id}</b>?",
        confirm_cancel_kb(
            EventCb(action=exec_action, task_type=callback_data.task_type,
                    item_id=item_id).pack(),
            EventCb(action="item", task_type=callback_data.task_type,
                    item_id=item_id).pack(),
            yes_text=yes_text,
            no_text="⬅️ No, indietro",
        ),
    )
    await callback.answer()
```

Nota che `et.hub_label` non passa da `esc`: è testo di catalogo, non user-controlled, ed è così
anche oggi. Non cambiarlo qui.

`cb_schedule` perde il parsing del quinto segmento e diventa due filtri su un handler:

```python
@router.callback_query(
    EventCb.filter(F.action.in_({"sched", "sched_close"})), IsAdminCallbackFilter()
)
async def cb_schedule(
    callback: CallbackQuery, callback_data: EventCb, state: FSMContext
) -> None:
    action = "close" if callback_data.action == "sched_close" else None
    et = event_types.get(callback_data.task_type)
    if et is None or callback_data.item_id is None:
        await callback.answer()
        return
    item_id = callback_data.item_id
    from handlers.schedule import start_schedule_for
    await start_schedule_for(
        callback.message, state, callback_data.task_type, item_id,
        f"{et.hub_label} #{item_id}", action,
    )
    await callback.answer()
```

I tre del sondaggio:

```python
@router.callback_query(PollCreateCb.filter(F.action == "cancel"), IsAdminCallbackFilter())
async def cb_pt_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    ...  # corpo invariato
```

- [ ] **Step 7: aggiornare i 73 punti nei sei file di test**

Stesso schema del Task 2: import della classe, payload grezzo → istanza, e l'handler riceve
`callback_data` come argomento.

Tre casi vanno guardati uno per uno invece che sostituiti a macchina:

1. **`tests/unit/test_admin_dashboard_kb.py`** asserisce sui `callback_data` **prodotti** dalle
   tastiere. Lì i letterali vanno aggiornati al payload nuovo — e i due che cambiano davvero sono
   `ev:sched:…:close` → `ev:sched_close:…` e `ev:pt:*` → `evpt:*`. Tutti gli altri restano identici:
   se un'asserzione diversa da queste due diventa rossa, hai cambiato un payload che non doveva
   cambiare.
2. **I test che passano un id non numerico** («`ev:item:quiz:abc` non fa niente») si spostano in
   `tests/unit/test_callbacks.py` come test di filtro, esattamente come il caso `abc` del Task 2.
3. **I test che passano un tipo-evento inesistente** (`ev:list:inesistente`) **restano** dove sono:
   è una stringa legittima che l'handler rifiuta.

- [ ] **Step 7-bis: verificare che non sia rimasto niente**

```bash
grep -rn '"ev:\|f"ev:' src/handlers/
```

Expected: **nessun risultato**. I 61 letterali di partenza devono essere zero. Un solo superstite è
un bottone che non funziona più, e la suite può non accorgersene: nessun test preme *tutti* i
bottoni di tutti i tipi-evento.

- [ ] **Step 8: eseguire la suite intera**

Run: `pytest`
Expected: verde.

- [ ] **Step 9: gate**

Run: `ruff check src/ tests/ && mypy && pytest --cov=src --cov-report=term-missing | tail -3`
Expected: zero findings, coverage ≥ 99.

- [ ] **Step 10: prova a mano che nessun test copre**

I test non guardano un bottone vero in una chat vera. Questa conversione tocca i payload che
`event_types/` disegna, quindi vale la pena vederli una volta.

```bash
# .env.docker-test non esiste più (il token del bot di prova è stato cancellato).
# Se vuoi rifare la prova, ricrea il file con un token nuovo di BotFather.
docker compose up -d --build
docker compose logs -f bot
```

In privato col bot: `/eventi` → apri un tipo → apri un item → «🗓️ Programma». Se un bottone risponde
«Questo bottone non è più valido», il payload che disegna quel bottone non è stato convertito: è il
catch-all del Task 1 che fa il suo mestiere, e ti sta dicendo dove.

Poi `docker compose down`.

- [ ] **Step 11: commit**

```bash
git add src/handlers/callbacks.py src/handlers/events.py \
        src/handlers/event_types/quiz_type.py src/handlers/event_types/guess_type.py \
        src/handlers/event_types/bet_type.py src/handlers/event_types/poll_type.py \
        src/handlers/guess/creation.py src/handlers/quiz/editing.py tests/
git commit -m "$(cat <<'EOF'
refactor: l'hub eventi parla CallbackData

La grammatica più usata del progetto (63 occorrenze) e la più irregolare.

Due decisioni di modellazione, non meccaniche:

Il quinto segmento opzionale di `ev:sched:<t>:<id>:close` diventa
un'azione a sé, `sched_close`, invece di un campo. Un campo che riempie
un'azione su dieci sarebbe un separatore vuoto in ogni altro payload, e un
campo il cui significato dipende dall'azione è la stessa disonestà che il
parsing a mano si permetteva. Il payload resta lungo uguale.

Il triangolo `ev:pt:*` prende prefisso suo (`evpt`): annullare la creazione
di un sondaggio non è navigazione dell'hub, si era solo accampato sotto lo
stesso prefisso. Così EventCb resta a tre campi e ogni altro payload perde
un separatore.

`ev:askstart` e compagni invece restano identici carattere per carattere:
l'azione incollata al prefisso è semplicemente un nome di azione.

`_CONFIRM` smette di tenere un pezzo di payload (`"ev:start"`) che veniva
concatenato a mano per costruire il bottone «Sì»: adesso tiene il nome
dell'azione, che è quello che ha sempre significato.

I payload sono convertiti nello stesso commit degli handler perché li
disegnano sette file, non solo events.py: separarli lascerebbe bottoni che
non fanno match.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Uscita del piano

Alla fine del Task 3:

- `pytest` verde, coverage ≥ 99, `ruff` e `mypy` a zero findings;
- due grammatiche su venti convertite, e l'idioma dimostrato su entrambe le forme che esistono —
  il flusso a stati (`schedule`) e la navigazione ad albero (`events`);
- il catch-all in produzione, che d'ora in poi segnala da solo ogni payload rimasto indietro.

**Poi si aggiorna la tabella di stato dello spec** (§10, riga A.1) dicendo *quali* file sono
convertiti, non solo che il task è iniziato — e si scrive il piano per i tredici restanti, con
l'idioma che a quel punto esiste davvero.

**Da non fare in questo piano:** `utils/panel.py` (A.2) e le viste di `admin_dashboard.py` (A.3a).
Hanno piani loro, e il secondo non va scritto prima che il primo sia verde.
