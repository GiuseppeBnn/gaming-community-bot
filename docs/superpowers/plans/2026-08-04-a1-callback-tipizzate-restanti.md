# A.1 Remaining Typed Callbacks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete A.1 by replacing every hand-written callback grammar in the remaining thirteen handler files with centrally declared aiogram `CallbackData` factories, including every producer and affected test.

**Architecture:** Keep pure callback declarations in `handlers/callbacks.py`, a leaf module that imports only aiogram and the standard library. Each wire family gets one class with an `action` field; producers call `.pack()`, filters inject `callback_data`, and handlers guard optional fields before use. The existing unhandled-callback catch-all handles malformed and pre-deploy payloads, while the live-filter structural tests prevent dead actions and hand-written typed prefixes.

**Tech Stack:** Python 3.12 · aiogram 3.30.0 `CallbackData` · Python `ast` · pytest with automatic asyncio mode · Ruff · mypy.

## Global Constraints

- Work only on branch `test_giu`; never touch `main`.
- Current measured baseline: **2125 passed, 30 skipped, coverage 99.65%; Ruff and configured mypy clean**.
- Preserve user-visible and business behavior. The only accepted runtime difference is the already-approved one: malformed or old callback payloads fail their typed filter and reach `common.cb_unhandled`.
- Do not modify anything under `src/services/`, database models, migrations, money logic, XP logic, state-transition logic, gating semantics, or router order.
- These tests are immutable: all money/XP tests, `tests/unit/test_admin_routers_gated.py`, and `tests/unit/test_router_order.py`.
- User-facing messages stay Italian. New developer-facing names, comments, docstrings, logs, tests, and plan prose are English.
- Keep top-level imports (`from handlers.callbacks import AdminCb`), `from __future__ import annotations`, no new dependency, and no type-ignore for optional callback fields.
- Optional fields retain empty wire separators. Bind them to locals and guard `is None` before registry lookups, integer use, or calls requiring `str`/`int`.
- Every producer under `src/keyboards/`, `src/handlers/event_types/`, other handlers, and tests must use the same factory as its consumer. Deep-link payloads such as `bet_custom_<event>_<option>` are not callback data and remain unchanged.
- Preserve parameterized logs and the unhandled-callback deduplication contract.
- In every conversion task, run the two structural wiring guards and a repo-wide old-prefix search before the task gate.
- Before every commit run the focused tests, immutable guards, `.venv/bin/pytest --cov=src --cov-report=term-missing`, `.venv/bin/ruff check src/ tests/`, `.venv/bin/mypy`, and `PYTHONPATH=src .venv/bin/python -c 'import main'`.
- Commit after every task. Do not combine money/XP/gating/router-order test edits with callback work; those files must remain byte-for-byte unchanged.

---

## File Structure

| File | Responsibility in this plan | Tasks |
|---|---|---|
| `src/handlers/callbacks.py` | Pure central declarations for all callback families; no project-local imports | 1–14 |
| `tests/unit/test_callbacks.py` | AST leaf-module guard, pack/unpack/filter tests, prefix scan, live registered-action wiring | 1–14 |
| `src/handlers/admin_dashboard.py`, `src/keyboards/admin_dashboard_kb.py`, `src/handlers/events.py`, `src/handlers/event_types/bet_type.py` | `AdminCb` consumer and every `adm` producer | 2 |
| `src/handlers/admin_betting.py`, `src/keyboards/admin_betting_kb.py` | `AdminBetCb` consumer/producers | 3 |
| `src/handlers/betting.py`, `src/keyboards/betting_kb.py` | Six typed betting callback families | 4 |
| `src/handlers/shop.py`, `src/keyboards/shop_kb.py` | `ShopCb`; immutable PG test compatibility | 5 |
| `src/handlers/quiz/creation.py` | `QuizNewCb` | 6 |
| `src/handlers/quiz/editing.py`, `src/handlers/event_types/quiz_type.py` | `QuizEditCb` and external producer | 7 |
| `src/handlers/quiz/play.py` | `QuizAnswerCb` | 8 |
| `src/handlers/quiz/trying.py`, `src/handlers/quiz/creation.py`, `src/handlers/event_types/quiz_type.py` | `QuizTryCb` and external producers | 9 |
| `src/handlers/guess/creation.py` | `GuessNewCb` | 10 |
| `src/handlers/guess/editing.py`, `src/handlers/event_types/guess_type.py` | `GuessAliasCb` and external producers | 11 |
| `src/handlers/guess/play.py` | `GuessPlayCb` | 12 |
| `src/handlers/leaderboard.py` | `LeaderboardCb` | 13 |
| `src/handlers/onboarding.py`, `src/keyboards/onboarding_kb.py` | `RulesCb` | 14 |
| Existing focused integration/unit tests named in each task | Direct-handler calls and producer assertions use typed instances | 2–14 |
| `STEERING.md`, `docs/superpowers/specs/2026-08-03-fondamenta-presentazione-design.md` | Normative factory/wire documentation and final A.1 status | 2–15 |

No task creates or modifies a service, model, migration, gating guard, router registry, or money/XP test.

---

## Required conversion protocol for Tasks 2–14

Each conversion task uses this exact TDD and gate sequence in addition to its task-specific focused
test files:

1. Add the class import plus literal pack/unpack/filter tests to `tests/unit/test_callbacks.py`.
2. Run `.venv/bin/pytest tests/unit/test_callbacks.py -v` and verify RED is the missing class/import,
   not a syntax or fixture error.
3. Add only the class declaration to `handlers/callbacks.py`.
4. Run `.venv/bin/pytest tests/unit/test_callbacks.py -v` and verify the factory tests are GREEN.
5. Convert every producer, filter, direct-handler test call, and developer-facing grammar doc named
   by the task. Preserve handler bodies after removing parsing.
6. Run the task's repo-wide prefix search and these live wiring guards:

```bash
.venv/bin/pytest tests/unit/test_callbacks.py::test_no_handwritten_payload_shadows_a_typed_prefix tests/unit/test_callbacks.py::test_every_constructed_action_reaches_a_registered_filter -v
```

7. Run every focused test path listed in the task, then the immutable and global gates:

```bash
.venv/bin/pytest tests/unit/test_admin_routers_gated.py tests/unit/test_router_order.py -v
.venv/bin/pytest --cov=src --cov-report=term-missing
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
PYTHONPATH=src .venv/bin/python -c 'import main'
```

8. Commit only after every command exits zero and coverage is at least 99%.

---

### Task 1: Enforce the callback declaration leaf-module invariant

**Files:**
- Modify: `tests/unit/test_callbacks.py`
- Verify only: `src/handlers/callbacks.py`

**Interfaces:**
- Consumes: `src/` top-level packages/modules and Python `ast`.
- Produces: `test_callback_declarations_have_no_project_local_imports`, which permits standard-library/third-party imports and rejects relative or project-local imports in `handlers/callbacks.py`.

- [ ] **Step 1: Add the AST invariant test**

```python
def test_callback_declarations_have_no_project_local_imports():
    src_dir = Path(__file__).resolve().parents[2] / "src"
    callback_module = src_dir / "handlers" / "callbacks.py"
    local_roots = {path.stem for path in src_dir.glob("*.py")}
    local_roots.update(
        path.name
        for path in src_dir.iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    )
    tree = ast.parse(callback_module.read_text(encoding="utf-8"), filename=str(callback_module))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name for alias in node.names if alias.name.split(".", 1)[0] in local_roots
            )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if node.level or root in local_roots:
                offenders.append("." * node.level + (node.module or ""))
    assert offenders == [], (
        "handlers/callbacks.py must remain a project-import-free leaf module; "
        f"project imports: {offenders}"
    )
```

- [ ] **Step 2: Prove RED by mutation, then restore**

Temporarily add `from handlers import events` below the aiogram import in `src/handlers/callbacks.py`.

Run: `.venv/bin/pytest tests/unit/test_callbacks.py::test_callback_declarations_have_no_project_local_imports -v`

Expected: FAIL listing `handlers`. Remove exactly that temporary import.

- [ ] **Step 3: Verify GREEN and commit**

Run:

```bash
.venv/bin/pytest tests/unit/test_callbacks.py -v
.venv/bin/pytest tests/unit/test_admin_routers_gated.py tests/unit/test_router_order.py -v
.venv/bin/pytest --cov=src --cov-report=term-missing
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
PYTHONPATH=src .venv/bin/python -c 'import main'
git add tests/unit/test_callbacks.py
git commit -m "test: callbacks resta un modulo foglia"
```

---

### Task 2: Convert `admin_dashboard.py` and all `adm` producers

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/admin_dashboard.py`, `src/keyboards/admin_dashboard_kb.py`, `src/handlers/events.py`, `src/handlers/event_types/bet_type.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/unit/test_admin_dashboard_kb.py`, `tests/integration/test_admin_dashboard_money.py`, `tests/integration/test_events_hub.py`

**Interfaces:**
- Produces: `AdminCb(action: str, key: str | None = None, item_id: int | None = None)`, prefix `adm`.
- Action map: simple `home|stats|lead|audit|help|close|bets|econ|airdrop|xpairdrop|search`; `lead_board(key)`; `users(item_id=page)`; `user(item_id=tg_id)`; `act|ask|do(key=verb, item_id=tg_id)`.

- [ ] **Step 1: Write RED factory tests**

```python
@pytest.mark.parametrize(("cb", "packed"), [
    (AdminCb(action="home"), "adm:home::"),
    (AdminCb(action="lead_board", key="coins"), "adm:lead_board:coins:"),
    (AdminCb(action="users", item_id=2), "adm:users::2"),
    (AdminCb(action="act", key="credit", item_id=42), "adm:act:credit:42"),
])
def test_pack_admin_callbacks(cb, packed):
    assert cb.pack() == packed

async def test_admin_numeric_field_is_typed_by_the_filter():
    assert await AdminCb.filter()(_query("adm:users::two")) is False
```

Run: `.venv/bin/pytest tests/unit/test_callbacks.py -v`

Expected: collection FAIL because `AdminCb` does not exist.

- [ ] **Step 2: Add the declaration**

```python
class AdminCb(CallbackData, prefix="adm"):
    """Button-driven admin dashboard."""

    action: str
    key: str | None = None
    item_id: int | None = None
```

- [ ] **Step 3: Convert producers and consumers with these exact shapes**

```python
callback_data=AdminCb(action="home").pack()
callback_data=AdminCb(action="lead_board", key=board).pack()
callback_data=AdminCb(action="users", item_id=page).pack()
callback_data=AdminCb(action="user", item_id=user.tg_id).pack()
callback_data=AdminCb(action="act", key="credit", item_id=tg_id).pack()

@router.callback_query(AdminCb.filter(F.action == "lead_board"), IsAdminCallbackFilter())
async def cb_lead_board(callback: CallbackQuery, callback_data: AdminCb, db_session: AsyncSession) -> None:
    board = callback_data.key
    if board not in ("coins", "xp", "trofei"):
        await callback.answer()
        return
    await callback.message.edit_text(await render_board(db_session, board), reply_markup=lead_kb(board))
    await callback.answer()

@router.callback_query(AdminCb.filter(F.action == "act"), IsAdminCallbackFilter())
async def cb_act(callback: CallbackQuery, callback_data: AdminCb, state: FSMContext) -> None:
    action = callback_data.key
    tg_id = callback_data.item_id
    if action is None or tg_id is None:
        await callback.answer()
        return
```

Use the same local-guard shape for `users`, `user`, `ask`, and `do`; keep their existing bodies after the removed parsing lines. Keep the deny handler and make its prefix non-literal:

```python
@router.callback_query(F.data.startswith(f"{AdminCb.__prefix__}:"))
async def cb_deny(callback: CallbackQuery) -> None:
    await callback.answer("⛔ Accesso non autorizzato.", show_alert=True)
```

Convert the dashboard-return producers in `events.py` and `event_types/bet_type.py`. Do not change admin filters, money/XP calls, commits, or router position.

- [ ] **Step 4: Update tests and normative documentation**

Pass `AdminCb` to direct handlers that consume fields and assert literal packed values from the factory. In `STEERING.md` §18.1 replace the raw grammar with the class fields, action map, empty-separator examples, and the reason the deny filter derives its prefix from the class.

- [ ] **Step 5: Completeness, wiring, gate, commit**

Run:

```bash
rg -n 'callback_data\s*=\s*f?["'"']adm:' src tests --glob '*.py'
.venv/bin/pytest tests/unit/test_callbacks.py::test_no_handwritten_payload_shadows_a_typed_prefix tests/unit/test_callbacks.py::test_every_constructed_action_reaches_a_registered_filter -v
.venv/bin/pytest tests/unit/test_callbacks.py tests/unit/test_admin_dashboard_kb.py tests/integration/test_admin_dashboard_money.py tests/integration/test_events_hub.py -v
.venv/bin/pytest tests/unit/test_admin_routers_gated.py tests/unit/test_router_order.py -v
.venv/bin/pytest --cov=src --cov-report=term-missing
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
PYTHONPATH=src .venv/bin/python -c 'import main'
git add src/handlers/callbacks.py src/handlers/admin_dashboard.py src/keyboards/admin_dashboard_kb.py src/handlers/events.py src/handlers/event_types/bet_type.py tests/unit/test_callbacks.py tests/unit/test_admin_dashboard_kb.py tests/integration/test_admin_dashboard_money.py tests/integration/test_events_hub.py STEERING.md
git commit -m "refactor: tipizza le callback della dashboard"
```

Expected first `rg`: no output.

---

### Task 3: Convert `admin_betting.py` and its keyboard

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/admin_betting.py`, `src/keyboards/admin_betting_kb.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/unit/test_keyboards.py`, `tests/integration/test_admin_betting.py`

**Interfaces:**
- Produces: `AdminBetCb(action: str, event_id: int | None = None, option_id: int | None = None)`, prefix `admin_bet`.
- Actions: `list|close`; `event|lock|confirm_lock|resolve|cancel|confirm_cancel(event_id)`; `pick_winner|confirm_resolve(event_id, option_id)`.

- [ ] **Step 1: RED tests and declaration**

```python
class AdminBetCb(CallbackData, prefix="admin_bet"):
    """Admin management of betting events."""

    action: str
    event_id: int | None = None
    option_id: int | None = None
```

Test literal packs `admin_bet:list::`, `admin_bet:event:7:`, and `admin_bet:pick_winner:7:9`; test `admin_bet:event:x:` is rejected. Run the test before adding the class and require the missing-import failure.

- [ ] **Step 2: Convert all producers/handlers**

Use these producer mappings in both production files: `list`/`close` with no ids;
`event`/`lock`/`confirm_lock`/`resolve`/`cancel`/`confirm_cancel` with `event_id`;
`pick_winner`/`confirm_resolve` with both ids. Every handler with ids receives
`callback_data: AdminBetCb`, binds required ids, answers and returns on `None`, then runs its
unchanged body. Keep the deny handler as `F.data.startswith(f"{AdminBetCb.__prefix__}:")`; do not
change its router position or any settlement/refund code.

- [ ] **Step 3: Update direct tests and STEERING §18**

Replace fake payload construction with `AdminBetCb`; do not edit `tests/unit/test_router_order.py`. Document the class/action map and trailing empty fields.

- [ ] **Step 4: Completeness, wiring, gate, commit**

```bash
rg -n 'callback_data\s*=\s*f?["'"']admin_bet:' src tests --glob '*.py'
.venv/bin/pytest tests/unit/test_callbacks.py::test_no_handwritten_payload_shadows_a_typed_prefix tests/unit/test_callbacks.py::test_every_constructed_action_reaches_a_registered_filter tests/unit/test_keyboards.py tests/integration/test_admin_betting.py tests/unit/test_admin_routers_gated.py tests/unit/test_router_order.py -v
.venv/bin/pytest --cov=src --cov-report=term-missing
.venv/bin/ruff check src/ tests/ && .venv/bin/mypy
PYTHONPATH=src .venv/bin/python -c 'import main'
git add src/handlers/callbacks.py src/handlers/admin_betting.py src/keyboards/admin_betting_kb.py tests/unit/test_callbacks.py tests/unit/test_keyboards.py tests/integration/test_admin_betting.py STEERING.md
git commit -m "refactor: tipizza le callback delle scommesse admin"
```

---

### Task 4: Convert all betting-flow callback families

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/betting.py`, `src/keyboards/betting_kb.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/unit/test_keyboards.py`, `tests/unit/test_bet_amount.py`, `tests/unit/test_error_handler.py`, `tests/integration/test_betting_handlers.py`, `tests/integration/test_scommesse_privacy.py`, `tests/integration/test_admin_betting.py`

**Interfaces:**

```python
class BetCb(CallbackData, prefix="bet"):
    action: str
    seconds: int | None = None

class BetEventCb(CallbackData, prefix="event"):
    action: str
    event_id: int

class BetOptionCb(CallbackData, prefix="bet_option"):
    action: str
    event_id: int
    option_id: int

class BetAmountCb(CallbackData, prefix="bet_amount"):
    action: str
    event_id: int
    option_id: int
    amount: int

class BetCustomCb(CallbackData, prefix="bet_custom"):
    action: str
    event_id: int
    option_id: int

class BetConfirmCb(CallbackData, prefix="bet_confirm"):
    action: str
    event_id: int
    option_id: int
    amount: int
```

`BetCb` actions are `cancel_creation|cancel_yes|cancel_no|window|window_custom|back|close`; the other class actions are respectively `view`, `pick`, `pick`, `open`, `place`.

- [ ] **Step 1: RED pack/filter tests**

Pin `bet:cancel_creation:`, `bet:window:60`, `event:view:7`, `bet_option:pick:7:9`, `bet_amount:pick:7:9:100`, `bet_custom:open:7:9`, and `bet_confirm:place:7:9:100`. Add nonnumeric filter rejections, run, and require missing imports.

- [ ] **Step 2: Add declarations, producers, and typed handlers**

Replace every keyboard literal with the relevant `.pack()`. `bet:win:custom` becomes `BetCb(action="window_custom")`; numeric presets use `BetCb(action="window", seconds=sec)`. Handlers consume integer fields directly; keep the `amount <= 0` business guard and all service/commit/error branches unchanged. Do not alter `bet_custom_<e>_<o>` deep links in `common.py`.

- [ ] **Step 3: Update tests and STEERING §16**

Move malformed-number coverage from direct handler parsing to real `CallbackQuery` filter tests. Direct business tests pass typed instances. Do not modify money/XP or router-order tests.

- [ ] **Step 4: Completeness, wiring, gate, commit**

Search every prefix: `bet:`, `event:`, `bet_option:`, `bet_amount:`, `bet_custom:`, `bet_confirm:` in `callback_data=` assignments across `src/` and `tests/`; expected no hand-written producer. Run structural guards, all focused files above, immutable guards, full coverage, Ruff, mypy, import smoke; then commit `refactor: tipizza il flusso scommesse`.

---

### Task 5: Convert `shop.py` while preserving the immutable money guard

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/shop.py`, `src/keyboards/shop_kb.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/unit/test_keyboards.py`, `tests/integration/test_shop_handlers.py`, `tests/integration/test_shop_home_balance.py`
- Immutable verification: `tests/integration/test_money_concurrency_pg.py`

**Interfaces:**
- Produces: `ShopCb(action: str, key: str | None = None)`, prefix `shop`; actions `home|list|owned|buy|exec|menu|cat|cbuy|cexec|pantry|tags|tag|close`.

- [ ] **Step 1: RED tests and declaration**

Pin `shop:home:` and `shop:exec:tag_dragon`; reject values containing `:`. Require the missing-import RED before adding:

```python
class ShopCb(CallbackData, prefix="shop"):
    action: str
    key: str | None = None
```

- [ ] **Step 2: Convert production with the immutable-test adapter**

All keyed handlers receive `callback_data: ShopCb` and guard `key is None`. The immutable PG guard directly calls `cb_exec(callback, db_session)` with the valid old full form `shop:exec:<key>`. Preserve that call without editing the test by using this exact boundary adapter only on `cb_exec`:

```python
@router.callback_query(ShopCb.filter(F.action == "exec"))
async def cb_exec(
    callback: CallbackQuery,
    db_session: AsyncSession,
    callback_data: ShopCb | None = None,
) -> None:
    data = callback_data or ShopCb.unpack(callback.data)
    item_key = data.key
    if item_key is None:
        await callback.answer()
        return
```

Production always receives the injected object; the fallback exists solely to keep the immutable direct-call concurrency guard intact. Do not touch debit, purchase, flush, milestone, or commit code.

- [ ] **Step 3: Tests, docs, completeness, gate, commit**

Convert every other shop fake and keyboard assertion. Document the adapter and class in STEERING §11. Search hand-written `shop:` producers, run structural/focused/immutable gates including the PG file when `TEST_PG_URL` is available, full coverage/static/import gates, and commit `refactor: tipizza le callback della locanda`.

---

### Task 6: Convert quiz creation

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/quiz/creation.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/integration/test_quiz_creation_flow.py`

**Interfaces:**
- Produces: `QuizNewCb(action: str, key: str | None = None, value: int | None = None)`, prefix `quiz_new`.
- Simple actions: `cancel|cancel_yes|cancel_no|back|review|quickprize|customprize|noprize|usedefault|time_limit_custom|skip_explanation|add|remove_last|publish`.
- Field actions: `time_limit(value=seconds)`, `randomize(key=q|a|both|none)`, `correct(value=option_index)`.

- [ ] **Step 1: RED tests and declaration**

Pin `quiz_new:cancel::`, `quiz_new:time_limit::60`, `quiz_new:randomize:both:`, `quiz_new:correct::2`; reject nonnumeric values. Add the exact class fields above after the missing-import RED.

- [ ] **Step 2: Convert every builder/filter and direct test**

Use `QuizNewCb.filter(F.action == ...)`, injected data for the three field actions, explicit `None` guards, and unchanged FSM transitions/prompt bodies. Convert positional `confirm_cancel_kb` arguments with `.pack()`. The `QuizTryCb` producer at publication remains for Task 9 and must not be rewritten as `QuizNewCb`.

- [ ] **Step 3: Docs, completeness, gate, commit**

Update STEERING §19 creation callback description. Search `quiz_new:` producers repo-wide; run structural guards, `test_quiz_creation_flow.py`, immutable guards, full coverage/static/import gates; commit `refactor: tipizza le callback di creazione quiz`.

---

### Task 7: Convert quiz editing and its Events producer

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/quiz/editing.py`, `src/handlers/event_types/quiz_type.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/integration/test_quiz_edit_flow.py`, `tests/integration/test_event_type_specs.py`

**Interfaces:**
- Produces: `QuizEditCb(action: str, quiz_id: int | None = None, index: int | None = None)`, prefix `quiz_edit`.
- Actions: `noop|cancel|redo_skip_explanation`; `nav|text|options|explanation|redo(quiz_id,index)`; `correct(index)`.

- [ ] **Step 1: RED tests and class**

Pin `quiz_edit:cancel::`, `quiz_edit:nav:7:2`, `quiz_edit:correct::1`; reject malformed ids. Require RED before adding the declaration.

- [ ] **Step 2: Convert all producers/handlers/tests**

Convert the Edit button in `event_types/quiz_type.py`. Each data-consuming handler binds and guards required fields; `correct` reads only `index`, while navigation/edit actions require both ids. Keep every state/status/service/commit check unchanged.

- [ ] **Step 3: Docs, completeness, gate, commit**

Update STEERING §15/§19. Search `quiz_edit:` producers, run both structural tests, focused tests, immutable guards, full gates; commit `refactor: tipizza le callback di modifica quiz`.

---

### Task 8: Convert public quiz answers

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/quiz/play.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/integration/test_quiz_play_session.py`

**Interfaces:**

```python
class QuizAnswerCb(CallbackData, prefix="quiz_ans"):
    action: str
    quiz_id: int
    question_id: int
    option_id: int
```

Only action: `answer`.

- [ ] **Step 1: RED**

Pin `QuizAnswerCb(action="answer", quiz_id=7, question_id=8, option_id=2)` to `quiz_ans:answer:7:8:2`; reject a nonnumeric option.

- [ ] **Step 2: Convert**

The builder packs the exact class. `cb_answer` filters `F.action == "answer"`, receives the injected object, and assigns the three integer fields without `split`/`int`. Preserve public routing, idempotency, timers, answer recording, rewards, and XP behavior.

- [ ] **Step 3: Docs, completeness, gate, commit**

Search `quiz_ans:` producers, run structural/focused/immutable/full gates, update STEERING §19, commit `refactor: tipizza le risposte quiz`.

---

### Task 9: Convert quiz dry-run callbacks and all entry producers

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/quiz/trying.py`, `src/handlers/quiz/creation.py`, `src/handlers/event_types/quiz_type.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/integration/test_quiz_try.py`, `tests/integration/test_events.py`, `tests/integration/test_event_type_specs.py`

**Interfaces:**
- Produces: `QuizTryCb(action: str, quiz_id: int, question_id: int | None = None, option_id: int | None = None)`, prefix `quiz_try`.
- Actions: `start|stop(quiz_id)` and `answer(quiz_id,question_id,option_id)`.

- [ ] **Step 1: RED/class**

Pin `quiz_try:start:7::`, `quiz_try:stop:7::`, `quiz_try:answer:7:8:2`; reject malformed numeric fields, then add the exact declaration.

- [ ] **Step 2: Convert all three producer locations and handlers**

Use injected ids and explicit `None` guards for `answer`; `start`/`stop` require `quiz_id`. Preserve the explicit `callback.from_user.id` actor propagation, admin gates, in-memory `_TRY` isolation, and zero writes to answers/money/XP.

- [ ] **Step 3: Docs, completeness, gate, commit**

Search `quiz_try:` producers, run structural and all focused files, immutable/full gates, update STEERING §19 dry-run section, commit `refactor: tipizza le callback di prova quiz`.

---

### Task 10: Convert Guess/Sound creation

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/guess/creation.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/integration/test_guess_creation_flow.py`, `tests/integration/test_creation_query_cost.py`

**Interfaces:**
- Produces: `GuessNewCb(action: str, key: str | None = None, value: int | None = None)`, prefix `guess_new`.
- Actions: `edit(key)`; `hint_add|hint_undo|hint_clear|hint_done`; `hint_at(value)`; `back|publish|cancel|cancel_yes|cancel_no`.

- [ ] **Step 1: RED/class**

Pin `guess_new:cancel::`, `guess_new:edit:title:`, `guess_new:hint_at::3`; reject malformed threshold values, then add the exact declaration.

- [ ] **Step 2: Convert**

All builders and positional confirmation strings use `.pack()`. `edit` guards `key`; `hint_at` guards `value` before `free_thresholds()` validation. Preserve the existing three-layer hint validation, panel behavior, media flow, prize defaults, scheduling/EventCb buttons, query counts, and all service calls.

- [ ] **Step 3: Docs, completeness, gate, commit**

Search `guess_new:` producers, run structural/focused/immutable/full gates, update STEERING §19.b creation, commit `refactor: tipizza le callback di creazione guess`.

---

### Task 11: Convert Guess/Sound alias editing and Events producers

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/guess/editing.py`, `src/handlers/event_types/guess_type.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/integration/test_guess_alias_edit.py`, `tests/integration/test_event_type_specs.py`

**Interfaces:**
- Produces: `GuessAliasCb(action: str, round_id: int | None = None)`, prefix `guess_alias`; actions `add(round_id)` and `cancel`.

- [ ] **Step 1: RED/class**

Pin `guess_alias:add:7` and `guess_alias:cancel:`; reject malformed round ids, then add the declaration.

- [ ] **Step 2: Convert**

Convert both `event_types/guess_type.py` Add spellings buttons and the cancel keyboard. `cb_add` receives/guards `round_id`; preserve ready/running checks, alias normalization, future-only semantics, FSM and commits.

- [ ] **Step 3: Docs, completeness, gate, commit**

Search `guess_alias:` producers, run structural/focused/immutable/full gates, update STEERING §19.b alias section, commit `refactor: tipizza le callback delle grafie guess`.

---

### Task 12: Convert Guess/Sound play controls

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/guess/play.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/integration/test_guess_play.py`

**Interfaces:**
- Produces: `GuessPlayCb(action: str, round_id: int | None = None)`, prefix `guess_play`; actions `quit` and `resume(round_id)`.

- [ ] **Step 1: RED/class**

Pin `guess_play:quit:` and `guess_play:resume:7`; reject malformed ids, then add the declaration.

- [ ] **Step 2: Convert**

Use action filters, inject/guard `round_id` for resume, and preserve state/time/attempt/judge/reward behavior. The quit action needs no injected data.

- [ ] **Step 3: Docs, completeness, gate, commit**

Search `guess_play:` producers, run structural/focused/immutable/full gates, update STEERING §19.b play controls, commit `refactor: tipizza i controlli di gioco guess`.

---

### Task 13: Convert leaderboard switching

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/leaderboard.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/integration/test_leaderboard_screens.py`, `tests/integration/test_admin_dashboard_money.py`

**Interfaces:**
- Produces: `LeaderboardCb(action: str, board: str | None = None)`, prefix `lead`; actions `show(board=coins|xp|trofei)` and `close`.

- [ ] **Step 1: RED/class**

Pin `lead:show:coins` and `lead:close:`; reject a value containing `:`, then add the declaration.

- [ ] **Step 2: Convert**

The switcher uses `LeaderboardCb(action="show", board=key)`. The handler filters `show`, guards membership in the three literal boards, and retains renderer calls and privacy behavior. Close uses its own action filter.

- [ ] **Step 3: Docs, completeness, gate, commit**

Search `lead:` producers, run structural/focused/immutable/full gates, update STEERING §12.1, commit `refactor: tipizza le callback delle classifiche`.

---

### Task 14: Convert onboarding acceptance

**Files:**
- Modify: `src/handlers/callbacks.py`, `src/handlers/onboarding.py`, `src/keyboards/onboarding_kb.py`, `STEERING.md`
- Test: `tests/unit/test_callbacks.py`, `tests/integration/test_onboarding_private.py`

**Interfaces:**

```python
class RulesCb(CallbackData, prefix="rules"):
    action: str
```

Only action: `accept`; packed wire remains `rules:accept`.

- [ ] **Step 1: RED/class**

Pin `rules:accept`, verify a different prefix does not match, require the missing-import RED, then add the class.

- [ ] **Step 2: Convert producer/filter**

`get_rules_keyboard()` packs `RulesCb(action="accept")`; `cb_accept_rules` filters that action. Preserve private-chat defense, identity, trophy, commits, and all Italian user messages.

- [ ] **Step 3: Docs, completeness, gate, commit**

Search `rules:` producers, run structural/focused/immutable/full gates, update the onboarding section of STEERING, commit `refactor: tipizza la callback di onboarding`.

---

### Task 15: Prove repo-wide A.1 completeness and close the design status

**Files:**
- Modify: `docs/superpowers/specs/2026-08-03-fondamenta-presentazione-design.md`, `STEERING.md`
- Verify: all files from Tasks 1–14

**Interfaces:**
- Consumes: all direct `CallbackData` subclasses already imported from `handlers.callbacks`, all registered router filters, and all callback producers under `src/`.
- Produces: A.1 status marked complete with all 15/15 handler families and all current producers converted; A.2/A.3 remain unstarted.

- [ ] **Step 1: Inventory consumers and manual parsing**

Run:

```bash
rg -n 'callback\.data.*(split|startswith)|callback\.data\[[^]]+\]' src/handlers --glob '*.py'
rg -n '@router\.callback_query\([^\n]*F\.data' src/handlers --glob '*.py'
```

Expected: no manual parsing. The only `F.data` uses permitted are prefix-derived admin deny filters; `common.cb_unhandled` may still log `callback.data`.

- [ ] **Step 2: Inventory every producer and prefix**

Run `rg -n 'callback_data\s*=' src tests --glob '*.py'` and inspect every match. All callback producers for the 18 central classes must call `.pack()`; URL deep links are excluded. Then run:

```bash
.venv/bin/pytest tests/unit/test_callbacks.py::test_the_prefix_scan_actually_finds_callback_classes tests/unit/test_callbacks.py::test_no_handwritten_payload_shadows_a_typed_prefix tests/unit/test_callbacks.py::test_the_action_scan_actually_finds_something tests/unit/test_callbacks.py::test_every_constructed_action_reaches_a_registered_filter -v
```

Expected: all four pass against live registered filters.

- [ ] **Step 3: Verify immutable files were not edited**

Run `git diff --name-only HEAD~14..HEAD` and confirm it does not list
`tests/unit/test_admin_routers_gated.py`, `tests/unit/test_router_order.py`, any money/XP test, or
any file under `src/services/`. This range is exact because Tasks 1–14 each end in one commit.

- [ ] **Step 4: Update normative status without overstating scope**

In the design spec mark A.1 **15/15 complete**, name this plan, and retain A.2/A.3a as not started. In STEERING, ensure every shipped factory, field, packed empty separator, and architectural reason is documented; no removed wire form may be described as current.

- [ ] **Step 5: Final gates**

Run:

```bash
.venv/bin/pytest tests/unit/test_callbacks.py tests/unit/test_unhandled_callback.py -v
.venv/bin/pytest tests/unit/test_admin_routers_gated.py tests/unit/test_router_order.py -v
.venv/bin/pytest -v
.venv/bin/pytest --cov=src --cov-report=term-missing
.venv/bin/ruff check src/ tests/
.venv/bin/mypy
PYTHONPATH=src .venv/bin/python -c 'import main'
```

Expected: zero failures, 30 environment-dependent PostgreSQL skips when `TEST_PG_URL` is absent, coverage at least 99%, Ruff clean, configured mypy clean, import exit 0.

- [ ] **Step 6: Final documentation commit**

```bash
git add STEERING.md docs/superpowers/specs/2026-08-03-fondamenta-presentazione-design.md
git commit -m "docs: chiude A.1 su tutte le callback"
```

---

## Plan self-review record

- All thirteen remaining handler files map one-to-one to Tasks 2–14.
- Every external producer found in `src/keyboards/`, `handlers/event_types/`, and other handlers is named in the owning task.
- Every class used by a later task is declared in that task before producer or consumer conversion.
- Task 1 enforces the smaller dependency solution: `handlers/callbacks.py` stays project-import-free; declarations are not moved.
- No task touches a service or changes money, XP, gating, state transitions, or router order.
- The immutable money/XP/gating/router-order tests remain unchanged; Task 5 supplies the one direct-call adapter required by the immutable PG shop guard.
- Developer-facing plan text and code comments are English; Italian appears only in preserved user-facing strings and commit subjects.
