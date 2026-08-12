"""The events hub — `handlers/events.py`, at 41%.

The hub is deliberately generic: it never asks *which* kind of event it is holding,
it dispatches everything through the registry, so a new event type appears in it
with no edits here (STEERING §18.2). Testing it with the three real types would
therefore test the types, not the hub — so most of what follows drives a **fake
registered type** and checks the hub's own decisions:

  * the confirmation gate. `ev:askstart` / `askclose` / `askdel` / `askreset` exist
    so that no single tap ever starts, closes, deletes or re-runs anything. The
    test asserts both halves: the prompt, and that the action has *not* happened
    yet when the prompt is shown;
  * **commit only on ok** (§5). The specs mutate and never commit; the hub commits
    after a successful action and must leave a failed one uncommitted. That is
    asserted by rolling the session back afterwards and looking at what survived —
    a row that is still there was committed, one that vanished was not;
  * the optional capabilities (`render_detail`, `delete`, `reset`) are probed with
    `getattr`, so a type that doesn't implement them must fall back cleanly instead
    of raising — that fallback is what keeps the registry open to new types.

The registry is process-global, so every test that touches it restores the built-in
registration afterwards; leaking a fake type into the rest of the suite would be a
very confusing failure somewhere else.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import PollTemplate
from handlers import event_types, events
from handlers.callbacks import AdminCb, EventCb, PollCreateCb
from handlers.event_types import StartResult

ADMIN_ID = 1
GROUP_CHAT = -100_123


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(username="testbot")


class _FakeMessage:
    def __init__(self, *, chat_type: str = "private", text: str = "") -> None:
        self.text = text
        self.bot = _FakeBot()
        self.from_user = SimpleNamespace(id=ADMIN_ID, username="admin", full_name="Admin")
        self.chat = SimpleNamespace(
            id=ADMIN_ID if chat_type == "private" else GROUP_CHAT, type=chat_type
        )
        self.message_id = 1
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=len(self.texts))

    async def reply(self, text, reply_markup=None, **kw):
        return await self.answer(text, reply_markup, **kw)

    @property
    def said(self) -> str:
        return "\n".join(self.texts)


class _FakeCallback:
    def __init__(self, data: str, message=None) -> None:
        self.data = data
        self.message = message or _FakeMessage()
        self.bot = self.message.bot
        self.from_user = SimpleNamespace(id=ADMIN_ID, username="admin")
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    @property
    def said(self) -> str:
        return self.message.said

    @property
    def toasts(self) -> list[str]:
        return [t for t, _ in self.answers if t]


class _FakeType:
    """A minimal registered type: no detail screen, no delete, no reset.

    That combination is the important one — it is what a *new* event type looks
    like before it opts into the optional capabilities.
    """

    key = "fake"
    hub_label = "🧪 Finto"
    create_label = "➕ Crea finto"

    def __init__(self, *, start_ok=True, close_result=..., seed_on_start=False) -> None:
        self.start_ok = start_ok
        self.close_result = StartResult(True, "chiuso") if close_result is ... else close_result
        self.seed_on_start = seed_on_start
        self.rendered_lists = 0
        self.created_for: list[int] = []
        self.started: list[int] = []

    async def render_list(self, message, db_session) -> None:
        self.rendered_lists += 1
        await message.answer("elenco finto")

    async def schedulable_items(self, db_session):
        return [(1, "finto #1")]

    async def start_creation(self, message, state, creator_id) -> None:
        self.created_for.append(creator_id)

    async def start_now(self, bot, db_session, item_id) -> StartResult:
        self.started.append(item_id)
        if self.seed_on_start:
            # Something persistable, so "did the hub commit?" is answerable.
            db_session.add(PollTemplate(question=f"avviato {item_id}", options_json="[]",
                                        creator_tg_id=ADMIN_ID, status="ready"))
            await db_session.flush()
        return StartResult(self.start_ok, "avviato" if self.start_ok else "no", alert=not self.start_ok)

    async def execute_scheduled(self, bot, session, task, group_id) -> None:
        pass

    async def close_now(self, bot, db_session, item_id):
        return self.close_result


class _RichType(_FakeType):
    """A type that opts into every optional capability, like QuizType does."""

    key = "rich"
    hub_label = "💎 Ricco"

    def __init__(self, *, delete_ok=True, reset_result=...) -> None:
        super().__init__()
        self.delete_ok = delete_ok
        self.reset_result = StartResult(True, "riproposto") if reset_result is ... else reset_result
        self.details: list[int] = []
        self.deleted: list[int] = []

    async def render_detail(self, message, db_session, item_id) -> None:
        self.details.append(item_id)
        await message.answer(f"dettaglio #{item_id}")

    async def delete(self, db_session, item_id) -> StartResult:
        self.deleted.append(item_id)
        db_session.add(PollTemplate(question=f"eliminato {item_id}", options_json="[]",
                                    creator_tg_id=ADMIN_ID, status="ready"))
        await db_session.flush()
        return StartResult(self.delete_ok, "eliminato" if self.delete_ok else "no",
                           alert=not self.delete_ok)

    async def reset(self, db_session, item_id):
        return self.reset_result


@pytest.fixture(autouse=True)
def _restore_registry():
    """The registry is process-global and is only populated by `main` at startup, so
    each test both establishes the canonical state and puts it back: a fake type left
    behind would surface as a baffling failure in a completely different file."""
    event_types.clear()
    event_types.register_builtin()
    yield
    event_types.clear()
    event_types.register_builtin()


@pytest.fixture
def only_fake():
    event_types.clear()
    fake = _FakeType()
    event_types.register(fake)
    return fake


def _state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID))


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def _survived(session, needle: str) -> bool:
    """Did it survive a rollback? On SQLite-in-memory a second session shares the
    same transaction, so rolling back is the honest way to ask "was this committed?"."""
    await session.rollback()
    rows = (await session.execute(
        select(PollTemplate.question).where(PollTemplate.question.like(f"%{needle}%"))
    )).scalars().all()
    return bool(rows)


# ---------------------------------------------------------------------------
# The hub itself
# ---------------------------------------------------------------------------

class TestHub:
    async def test_the_hub_has_one_button_per_registered_type(self, session):
        message = _FakeMessage()

        await events.show_hub(message)

        assert set(_callbacks(message.markups[0])) == {
            EventCb(action="list", task_type="quiz").pack(),
            EventCb(action="list", task_type="guess").pack(),
            EventCb(action="list", task_type="sound").pack(),
            EventCb(action="list", task_type="poll").pack(),
            EventCb(action="list", task_type="bet").pack(),
            EventCb(action="list", task_type="twentyq").pack(),
            AdminCb(action="home").pack(),
        }

    async def test_a_newly_registered_type_appears_without_touching_the_hub(self, session):
        """This is the whole point of the registry: the hub has no per-type code."""
        event_types.register(_FakeType())
        message = _FakeMessage()

        await events.show_hub(message)

        assert EventCb(action="list", task_type="fake").pack() in _callbacks(message.markups[0])

    async def test_the_command_is_private_only(self, session):
        """The hub starts events and deletes them: its buttons must not sit in the
        group where any admin's tap is one message away from everyone else's."""
        message = _FakeMessage(chat_type="supergroup")

        await events.cmd_eventi(message, _state())

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=eventi")

    async def test_opening_the_hub_clears_a_half_finished_flow(self, session):
        """Otherwise a stale FSM state keeps swallowing the admin's next message."""
        state = _state()
        await state.set_state(events.PollTemplateStates.question)

        await events.cmd_eventi(_FakeMessage(), state)

        assert await state.get_state() is None

    async def test_the_home_button_also_clears_it(self, session):
        state = _state()
        await state.set_state(events.PollTemplateStates.options)

        await events.cb_hub(_FakeCallback(EventCb(action="home").pack()), state)

        assert await state.get_state() is None

    async def test_an_unknown_type_is_ignored_quietly(self, session):
        """Old keyboards outlive deployments: a button for a type that no longer
        exists must do nothing, not raise."""
        cb = EventCb(action="list", task_type="inesistente")
        callback = _FakeCallback(cb.pack())

        await events.cb_list(callback, cb, session)

        assert callback.said == ""

    async def test_listing_delegates_to_the_type(self, session, only_fake):
        cb = EventCb(action="list", task_type="fake")
        callback = _FakeCallback(cb.pack())

        await events.cb_list(callback, cb, session)

        assert only_fake.rendered_lists == 1


class TestItemScreen:
    async def test_a_type_without_a_detail_screen_gets_the_generic_one(
        self, session, only_fake
    ):
        cb = EventCb(action="item", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_item(callback, cb, session)

        assert set(_callbacks(callback.message.markups[0])) == {
            EventCb(action="start", task_type="fake", item_id=7).pack(),
            EventCb(action="sched", task_type="fake", item_id=7).pack(),
            EventCb(action="list", task_type="fake").pack(),
        }

    async def test_a_type_with_one_shows_its_own(self, session):
        event_types.clear()
        rich = _RichType()
        event_types.register(rich)
        cb = EventCb(action="item", task_type="rich", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_item(callback, cb, session)

        assert rich.details == [7]

    async def test_an_unknown_type_is_ignored_quietly(self, session):
        """Old keyboards outlive deployments: a button for a type that no longer
        exists must do nothing, not raise."""
        cb = EventCb(action="item", task_type="inesistente", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_item(callback, cb, session)

        assert callback.said == ""

    # A non-numeric id ("ev:item:fake:abc") used to be ignored here; now the filter
    # drops it before it ever reaches cb_item, so the coverage moved to
    # tests/unit/test_callbacks.py::test_a_non_numeric_event_id_never_reaches_the_handler.


# ---------------------------------------------------------------------------
# The confirmation gate
# ---------------------------------------------------------------------------

class TestConfirmationGate:
    @pytest.mark.parametrize("action,exec_action", [
        ("askstart", "start"),
        ("askclose", "close"),
        ("askdel", "del"),
        ("askreset", "reset"),
    ])
    async def test_each_action_asks_first_and_routes_yes_to_its_executor(
        self, session, only_fake, action, exec_action
    ):
        cb = EventCb(action=action, task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_confirm(callback, cb)

        executor = EventCb(action=exec_action, task_type="fake", item_id=7).pack()
        assert executor in _callbacks(callback.message.markups[0])
        item = EventCb(action="item", task_type="fake", item_id=7).pack()
        assert item in _callbacks(callback.message.markups[0]), \
            "«no» must go back to the item, not leave the admin stranded"
        assert only_fake.started == [], "asking must not already do it"

    async def test_the_re_run_prompt_says_the_prize_pool_is_paid_again(
        self, session, only_fake
    ):
        """A re-run pays the full pool a second time — deliberate, since a re-run is
        a new event, which is exactly why the admin has to be told before saying yes."""
        cb = EventCb(action="askreset", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_confirm(callback, cb)

        assert "montepremi intero" in callback.said

    async def test_an_unknown_type_is_ignored_quietly(self, session):
        """Old keyboards outlive deployments: a button for a type that no longer
        exists must do nothing, not raise."""
        cb = EventCb(action="askstart", task_type="inesistente", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_confirm(callback, cb)

        assert callback.said == ""

    # An unrecognised ask-action ("ev:askqualcosa:fake:7") used to be ignored by a
    # `conf is None` branch inside cb_confirm; now the filter (`F.action.in_(_CONFIRM)`)
    # only admits the four known ones, so the coverage moved to
    # tests/unit/test_callbacks.py::test_an_unknown_confirm_action_never_reaches_the_handler.


# ---------------------------------------------------------------------------
# Executors — the §5 commit contract
# ---------------------------------------------------------------------------

class TestStartAndClose:
    async def test_a_successful_start_is_committed(self, session):
        event_types.clear()
        fake = _FakeType(seed_on_start=True)
        event_types.register(fake)
        cb = EventCb(action="start", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_start_now(callback, cb, session)

        assert fake.started == [7]
        assert await _survived(session, "avviato 7")

    async def test_a_failed_start_leaves_nothing_behind(self, session):
        """The spec may have mutated before deciding it couldn't proceed; committing
        that would persist half an event."""
        event_types.clear()
        fake = _FakeType(start_ok=False, seed_on_start=True)
        event_types.register(fake)
        cb = EventCb(action="start", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_start_now(callback, cb, session)

        assert not await _survived(session, "avviato 7")
        assert callback.answers[0][1] is True, "a failure must be an alert, not a toast"

    async def test_the_list_is_redrawn_after_a_start(self, session, only_fake):
        """The item's state changed; leaving the old screen up invites a second tap."""
        cb = EventCb(action="start", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_start_now(callback, cb, session)

        assert only_fake.rendered_lists == 1

    async def test_starting_an_unknown_type_does_nothing(self, session, only_fake):
        cb = EventCb(action="start", task_type="inesistente", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_start_now(callback, cb, session)

        assert only_fake.started == [] and callback.said == ""

    async def test_closing_a_type_that_cannot_be_closed_is_a_no_op(self, session):
        """`close_now` returning None is how a type says «I have no close action» —
        the hub must not treat that as a failure and must not redraw anything."""
        event_types.clear()
        fake = _FakeType(close_result=None)
        event_types.register(fake)
        cb = EventCb(action="close", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_close(callback, cb, session)

        assert fake.rendered_lists == 0 and callback.toasts == []

    async def test_a_successful_close_answers_and_redraws(self, session, only_fake):
        cb = EventCb(action="close", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_close(callback, cb, session)

        assert only_fake.rendered_lists == 1
        assert callback.toasts == ["chiuso"]

    async def test_a_failed_close_is_an_alert(self, session):
        event_types.clear()
        event_types.register(_FakeType(close_result=StartResult(False, "non in corso", alert=True)))
        cb = EventCb(action="close", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_close(callback, cb, session)

        assert callback.answers[0][1] is True

    async def test_closing_an_unknown_type_does_nothing(self, session, only_fake):
        cb = EventCb(action="close", task_type="inesistente", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_close(callback, cb, session)

        assert only_fake.rendered_lists == 0 and callback.said == ""

    # A non-numeric id ("ev:close:fake:x") used to be ignored here; now the filter
    # drops it before it ever reaches cb_close, so the coverage moved to
    # tests/unit/test_callbacks.py::test_a_non_numeric_event_id_never_reaches_the_handler.


class TestDeleteAndReset:
    async def test_a_type_without_delete_ignores_the_button(self, session, only_fake):
        """`delete` is optional; probing it with getattr is what lets a type opt out
        without the hub knowing the type exists."""
        cb = EventCb(action="del", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_delete(callback, cb, session)

        assert callback.said == "" and callback.toasts == []

    async def test_a_successful_delete_is_committed_and_returns_to_the_list(self, session):
        event_types.clear()
        rich = _RichType()
        event_types.register(rich)
        cb = EventCb(action="del", task_type="rich", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_delete(callback, cb, session)

        assert rich.deleted == [7] and rich.rendered_lists == 1
        assert await _survived(session, "eliminato 7")

    async def test_a_failed_delete_is_not_committed(self, session):
        event_types.clear()
        event_types.register(_RichType(delete_ok=False))
        cb = EventCb(action="del", task_type="rich", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_delete(callback, cb, session)

        assert not await _survived(session, "eliminato 7")

    async def test_a_type_that_is_not_re_runnable_ignores_the_button(self, session):
        """`reset` returning None is different from a reset that failed: nothing is
        answered, because there was nothing to do."""
        event_types.clear()
        event_types.register(_RichType(reset_result=None))
        cb = EventCb(action="reset", task_type="rich", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_reset(callback, cb, session)

        assert callback.toasts == []

    async def test_a_successful_reset_refreshes_the_detail_screen(self, session):
        """The item still exists (ready again), so the admin should land back on it
        rather than on the list."""
        event_types.clear()
        rich = _RichType()
        event_types.register(rich)
        cb = EventCb(action="reset", task_type="rich", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_reset(callback, cb, session)

        assert rich.details == [7] and rich.rendered_lists == 0

    async def test_a_type_without_a_detail_screen_falls_back_to_the_list(self, session):
        event_types.clear()

        class _ResettableOnly(_FakeType):
            key = "resettable"

            async def reset(self, db_session, item_id):
                return StartResult(True, "ok")

        fake = _ResettableOnly()
        event_types.register(fake)
        cb = EventCb(action="reset", task_type="resettable", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_reset(callback, cb, session)

        assert fake.rendered_lists == 1

    async def test_a_type_without_reset_ignores_the_button(self, session, only_fake):
        cb = EventCb(action="reset", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_reset(callback, cb, session)

        assert callback.toasts == []


class TestScheduleAndCreate:
    async def test_scheduling_hands_off_with_a_human_label(self, session, only_fake, monkeypatch):
        """The label is what the admin sees in /programmati later; passing the raw
        id would make the list unreadable."""
        seen = []

        async def _fake_start(message, state, task_type, item_id, label, action=None):
            seen.append((task_type, item_id, label, action))

        import handlers.schedule as sched
        monkeypatch.setattr(sched, "start_schedule_for", _fake_start)
        cb = EventCb(action="sched", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_schedule(callback, cb, _state())

        assert seen == [("fake", 7, "🧪 Finto #7", None)]

    async def test_the_close_variant_pins_the_action(self, session, only_fake, monkeypatch):
        """On a running item «avvio» is not one of the answers, so the button says
        which action it means instead of asking."""
        seen = []

        async def _fake_start(message, state, task_type, item_id, label, action=None):
            seen.append(action)

        import handlers.schedule as sched
        monkeypatch.setattr(sched, "start_schedule_for", _fake_start)
        # was the optional 5th segment ("ev:sched:fake:7:close"), now its own action
        cb = EventCb(action="sched_close", task_type="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_schedule(callback, cb, _state())

        assert seen == ["close"]

    async def test_scheduling_an_unknown_type_does_nothing(self, session, only_fake):
        cb = EventCb(action="sched", task_type="inesistente", item_id=7)
        callback = _FakeCallback(cb.pack())

        await events.cb_schedule(callback, cb, _state())

        assert callback.said == ""

    async def test_creating_passes_the_admin_id_not_the_bot_id(self, session, only_fake):
        """The callback's message is authored by the bot, so the creator has to come
        from `from_user` — otherwise every item is created by the bot itself."""
        cb = EventCb(action="new", task_type="fake")
        callback = _FakeCallback(cb.pack())

        await events.cb_new(callback, cb, _state())

        assert only_fake.created_for == [ADMIN_ID]

    async def test_creating_an_unknown_type_does_nothing(self, session, only_fake):
        cb = EventCb(action="new", task_type="inesistente")
        callback = _FakeCallback(cb.pack())

        await events.cb_new(callback, cb, _state())

        assert only_fake.created_for == []


# ---------------------------------------------------------------------------
# Poll-template creation FSM (the one flow that lives in this file)
# ---------------------------------------------------------------------------

class TestPollTemplateCreation:
    async def test_it_asks_for_the_question_first(self, session):
        state = _state()
        message = _FakeMessage()

        await events.start_poll_creation(message, state)

        assert await state.get_state() == events.PollTemplateStates.question.state
        assert "domanda" in message.said.lower()

    async def test_a_too_short_question_does_not_advance(self, session):
        state = _state()
        await state.set_state(events.PollTemplateStates.question)
        message = _FakeMessage(text="ok")

        await events.fsm_pt_question(message, state)

        assert await state.get_state() == events.PollTemplateStates.question.state

    async def test_a_valid_question_moves_on_to_the_options(self, session):
        state = _state()
        message = _FakeMessage(text="Meglio pizza o sushi?")

        await events.fsm_pt_question(message, state)

        assert await state.get_state() == events.PollTemplateStates.options.state
        assert (await state.get_data())["pt_question"] == "Meglio pizza o sushi?"

    @pytest.mark.parametrize("raw", [
        "una sola",
        "\n".join(f"opt{i}" for i in range(11)),
        "valida\n" + "x" * 101,
    ])
    async def test_bad_option_lists_are_refused_and_create_nothing(self, session, raw):
        state = _state()
        await state.update_data(pt_question="Meglio?")
        await state.set_state(events.PollTemplateStates.options)
        message = _FakeMessage(text=raw)

        await events.fsm_pt_options(message, state, session)

        assert (await session.execute(select(PollTemplate))).scalars().all() == []
        assert await state.get_state() == events.PollTemplateStates.options.state

    async def test_a_valid_list_creates_the_template_and_offers_the_next_step(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await state.update_data(pt_question="Meglio?")
        await state.set_state(events.PollTemplateStates.options)
        message = _FakeMessage(text="Pizza\nSushi")

        await events.fsm_pt_options(message, state, session)

        poll = (await session.execute(select(PollTemplate))).scalar_one()
        assert poll.question == "Meglio?"
        assert set(_callbacks(message.markups[-1])) == {
            EventCb(action="start", task_type="poll", item_id=poll.id).pack(),
            EventCb(action="sched", task_type="poll", item_id=poll.id).pack(),
            EventCb(action="list", task_type="poll").pack(),
        }
        assert await state.get_state() is None

    async def test_the_created_template_survives_a_rollback(self, session, user_factory):
        """It is offered for an immediate start on the very next tap, so it has to be
        committed, not merely pending in the session."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await state.update_data(pt_question="Meglio?")
        await state.set_state(events.PollTemplateStates.options)

        await events.fsm_pt_options(_FakeMessage(text="Pizza\nSushi"), state, session)

        assert await _survived(session, "Meglio?")


class TestPollTemplateCancel:
    async def test_cancel_outside_the_flow_is_a_no_op(self, session):
        callback = _FakeCallback(PollCreateCb(action="cancel").pack())

        await events.cb_pt_cancel(callback, _state())

        assert callback.said == ""

    async def test_cancel_inside_the_flow_asks_first(self, session):
        state = _state()
        await state.set_state(events.PollTemplateStates.options)
        callback = _FakeCallback(PollCreateCb(action="cancel").pack())

        await events.cb_pt_cancel(callback, state)

        assert set(_callbacks(callback.message.markups[0])) == {
            PollCreateCb(action="cancel_yes").pack(), PollCreateCb(action="cancel_no").pack(),
        }
        assert await state.get_state() is not None

    async def test_confirming_clears_the_flow(self, session):
        state = _state()
        await state.set_state(events.PollTemplateStates.options)
        await state.update_data(pt_question="Meglio?")

        callback = _FakeCallback(PollCreateCb(action="cancel_yes").pack())
        await events.cb_pt_cancel_yes(callback, state)

        assert await state.get_state() is None and await state.get_data() == {}

    async def test_refusing_keeps_the_half_written_poll(self, session):
        state = _state()
        await state.set_state(events.PollTemplateStates.options)
        await state.update_data(pt_question="Meglio?")

        callback = _FakeCallback(PollCreateCb(action="cancel_no").pack())
        await events.cb_pt_cancel_no(callback)

        assert (await state.get_data())["pt_question"] == "Meglio?"
