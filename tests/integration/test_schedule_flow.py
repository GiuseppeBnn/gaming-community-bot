"""The scheduling flow — `handlers/schedule.py`, at 53%.

The scheduler *loop* is already covered (`test_scheduler_loop.py`,
`test_scheduler_failure_path.py`); what was not is the half an admin actually
touches: pick a type → pick an item → say when → confirm, plus `/programmati` and
its cancel button.

The point of this layer is that scheduling is **generic**: the picker is built from
the registry, so a new event type becomes schedulable with no edits here. Most tests
therefore drive a fake registered type; two things get pinned specifically:

  * a run-at that cannot be parsed (or is in the past) **re-prompts and creates
    nothing** — a task with a wrong time is worse than no task, because it will
    fire, in the group, at that wrong time;
  * `/programmati` and the cancel button work on tasks that already exist and are
    the only way to undo a mistake, so the cancel must be committed, not left
    pending in the session.

`ScheduleStates.event_runat` has no timeout, which is exactly why the router is
admin-gated as a whole (§8); that gate is asserted in `test_schedule_admin_gate.py`
and not re-tested here.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import ScheduledTask
from handlers import event_types, schedule
from handlers.callbacks import SchedCb
from handlers.event_types import StartResult
from services import schedule_service
from utils import cooldown

ADMIN_ID = 1
GROUP_CHAT = -100_123


class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(username="testbot")


class _FakeMessage:
    def __init__(self, text: str = "", *, chat_type: str = "private") -> None:
        self.text = text
        self.bot = _FakeBot()
        self.from_user = SimpleNamespace(id=ADMIN_ID, username="admin", full_name="Admin")
        self.chat = SimpleNamespace(
            id=ADMIN_ID if chat_type == "private" else GROUP_CHAT, type=chat_type
        )
        self.message_id = 1
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=len(self.texts))

    async def reply(self, text, reply_markup=None, **kw):
        return await self.answer(text, reply_markup, **kw)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

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
    def alerts(self) -> list[str]:
        return [t for t, alert in self.answers if alert and t]


class _FakeType:
    key = "fake"
    hub_label = "🧪 Finto"
    create_label = "➕ Crea finto"

    def __init__(self, items=((7, "Il finto numero sette"),)) -> None:
        self.items = list(items)

    async def render_list(self, message, db_session) -> None:
        await message.answer("elenco finto")

    async def schedulable_items(self, db_session):
        return self.items

    async def start_creation(self, message, state, creator_id) -> None:
        pass

    async def start_now(self, bot, db_session, item_id) -> StartResult:
        return StartResult(True, "ok")

    async def execute_scheduled(self, bot, session, task, group_id) -> None:
        pass

    async def close_now(self, bot, db_session, item_id):
        return None


@pytest.fixture(autouse=True)
def _registry_and_cooldowns():
    event_types.clear()
    event_types.register_builtin()
    cooldown.reset()
    yield
    event_types.clear()
    event_types.register_builtin()
    cooldown.reset()


class _ClosableType(_FakeType):
    """A type whose close does something worth putting on a clock (quiz, guess)."""

    key = "chiudibile"
    hub_label = "🧪 Chiudibile"
    closable = True


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


async def _tasks(session) -> list[ScheduledTask]:
    return list((await session.execute(select(ScheduledTask))).scalars().all())


# ---------------------------------------------------------------------------
# Entering the flow
# ---------------------------------------------------------------------------

class TestEntry:
    async def test_the_menu_offers_every_registered_type(self, session):
        message = _FakeMessage()

        await schedule.start_schedule_flow(message, _state())

        assert set(_callbacks(message.markups[0])) == {
            SchedCb(action="type", key="quiz").pack(),
            SchedCb(action="type", key="guess").pack(),
            SchedCb(action="type", key="sound").pack(),
            SchedCb(action="type", key="poll").pack(),
            SchedCb(action="type", key="bet").pack(),
            SchedCb(action="type", key="twentyq").pack(),
            SchedCb(action="type", key="raid").pack(),
            SchedCb(action="cancel").pack(),
        }
        assert all(len(row) == 1 for row in message.markups[0].inline_keyboard)

    async def test_a_new_type_is_schedulable_without_touching_this_file(self, session):
        event_types.register(_FakeType())
        message = _FakeMessage()

        await schedule.start_schedule_flow(message, _state())

        assert SchedCb(action="type", key="fake").pack() in _callbacks(message.markups[0])

    async def test_entering_the_flow_clears_a_previous_one(self, session):
        state = _state()
        await state.set_state(schedule.ScheduleStates.event_runat)
        await state.update_data(sched_ref=99)

        await schedule.start_schedule_flow(_FakeMessage(), state)

        assert await state.get_state() is None and await state.get_data() == {}

    async def test_in_the_group_it_only_hands_back_a_link(self, session):
        message = _FakeMessage(chat_type="supergroup")

        await schedule.cmd_programma(message, _state())

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=programma")

    async def test_in_private_it_opens_the_menu(self, session):
        message = _FakeMessage()

        await schedule.cmd_programma(message, _state())

        assert "Cosa vuoi programmare" in message.said


class TestPicker:
    async def test_choosing_a_type_lists_its_schedulable_items(self, session, only_fake):
        cb = SchedCb(action="type", key="fake")
        callback = _FakeCallback(cb.pack())

        await schedule.cb_type(callback, cb, _state(), session)

        assert SchedCb(action="pick", key="fake", item_id=7).pack() in _callbacks(
            callback.message.markups[0]
        )

    async def test_a_type_with_nothing_ready_says_so_instead_of_an_empty_list(
        self, session
    ):
        """An empty picker is a dead end: the admin needs to be told to go and create
        one, not handed a keyboard with only «annulla» on it."""
        event_types.clear()
        event_types.register(_FakeType(items=[]))
        cb = SchedCb(action="type", key="fake")
        callback = _FakeCallback(cb.pack())

        await schedule.cb_type(callback, cb, _state(), session)

        assert callback.alerts and "Crealo dagli Eventi" in callback.alerts[0]
        assert callback.message.texts == []

    async def test_an_unknown_type_is_ignored(self, session, only_fake):
        cb = SchedCb(action="type", key="inesistente")
        callback = _FakeCallback(cb.pack())

        await schedule.cb_type(callback, cb, _state(), session)

        assert callback.said == ""

    async def test_picking_an_item_asks_when(self, session, only_fake):
        state = _state()
        cb = SchedCb(action="pick", key="fake", item_id=7)
        callback = _FakeCallback(cb.pack())

        await schedule.cb_pick_event(callback, cb, state)

        data = await state.get_data()
        assert (data["sched_type"], data["sched_ref"]) == ("fake", 7)
        assert await state.get_state() == schedule.ScheduleStates.event_runat.state

    # A non-numeric id ("sched:pick:fake:abc") used to be ignored here; now the
    # filter drops it before it ever reaches cb_pick_event, so the coverage moved
    # to tests/unit/test_callbacks.py::test_a_non_numeric_id_never_reaches_the_handler.

    async def test_picking_an_unknown_type_is_ignored(self, session, only_fake):
        state = _state()
        cb = SchedCb(action="pick", key="inesistente", item_id=7)

        await schedule.cb_pick_event(_FakeCallback(cb.pack()), cb, state)

        assert await state.get_state() is None

    async def test_the_hub_entry_point_shows_the_label_it_was_given(self, session):
        """The Events hub passes a human label («🧠 Quiz #3»); it is what the admin
        sees while typing the time, so it must be carried through, not rebuilt."""
        state = _state()
        message = _FakeMessage()

        await schedule.start_schedule_for(message, state, "quiz", 3, "🧠 Quiz #3")

        assert "🧠 Quiz #3" in message.said
        assert (await state.get_data())["sched_label"] == "🧠 Quiz #3"


# ---------------------------------------------------------------------------
# The run-at step
# ---------------------------------------------------------------------------

class TestRunAt:
    async def _armed(self, state) -> None:
        await state.update_data(sched_type="fake", sched_ref=7, sched_label="🧪 Finto #7")
        await state.set_state(schedule.ScheduleStates.event_runat)

    async def test_a_relative_time_creates_the_task(self, session, only_fake, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await self._armed(state)
        message = _FakeMessage("2h")

        await schedule.fsm_event_runat(message, state, session)

        task = (await _tasks(session))[0]
        assert (task.task_type, task.ref_id, task.status) == ("fake", 7, "pending")
        assert task.run_at > schedule_service.utcnow() + timedelta(minutes=110)
        assert await state.get_state() is None

    async def test_an_absolute_time_works_too(self, session, only_fake, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await self._armed(state)
        future = schedule_service.to_local(schedule_service.utcnow() + timedelta(days=2))
        message = _FakeMessage(future.strftime("%Y-%m-%d %H:%M"))

        await schedule.fsm_event_runat(message, state, session)

        assert len(await _tasks(session)) == 1

    @pytest.mark.parametrize("raw", ["domani", "2020-01-01 10:00", ""])
    async def test_an_unusable_time_re_prompts_and_creates_nothing(
        self, session, only_fake, user_factory, raw
    ):
        """Including a time in the past: accepting it would schedule something the
        loop fires on its very next tick, in the group."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await self._armed(state)
        message = _FakeMessage(raw)

        await schedule.fsm_event_runat(message, state, session)

        assert await _tasks(session) == []
        assert await state.get_state() == schedule.ScheduleStates.event_runat.state
        assert SchedCb(action="cancel").pack() in _callbacks(message.markups[-1])

    async def test_the_confirmation_names_the_type_and_the_local_time(
        self, session, only_fake, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await self._armed(state)
        message = _FakeMessage("2h")

        await schedule.fsm_event_runat(message, state, session)

        assert "🧪 Finto" in message.said and "Programmato" in message.said

    async def test_a_task_of_an_unregistered_type_still_confirms(
        self, session, user_factory
    ):
        """A type can be unregistered between scheduling and reading back (a rollout
        removing it); the confirmation must degrade to the raw key, not crash."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        task = SimpleNamespace(task_type="scomparso", id=1,
                               run_at=schedule_service.utcnow() + timedelta(hours=1))
        message = _FakeMessage()

        await schedule._confirm(message, task)

        assert "scomparso" in message.said


class TestWhatToSchedule:
    """Types that can also be closed on a timer are asked *what* to schedule.

    The rest keep going straight to the time: a question with one possible answer
    is not a question, and every existing flow (poll, bet) is one of those.
    """

    @pytest.fixture
    def closable(self):
        event_types.clear()
        et = _ClosableType()
        event_types.register(et)
        return et

    async def test_a_closable_type_asks_start_or_close_first(self, session, closable):
        message = _FakeMessage()

        await schedule.start_schedule_for(message, _state(), "chiudibile", 7, "🧪 #7")

        assert set(_callbacks(message.markups[0])) == {
            SchedCb(action="act", key="start").pack(),
            SchedCb(action="act", key="close").pack(),
            SchedCb(action="cancel").pack(),
        }

    async def test_a_type_that_cannot_close_goes_straight_to_the_time(
        self, session, only_fake
    ):
        state = _state()
        message = _FakeMessage()

        await schedule.start_schedule_for(message, state, "fake", 7, "🧪 Finto #7")

        assert await state.get_state() == schedule.ScheduleStates.event_runat.state

    async def test_an_action_given_up_front_skips_the_question(self, session, closable):
        """The «🗓️ Programma chiusura» button on a running item already knows."""
        state = _state()
        message = _FakeMessage()

        await schedule.start_schedule_for(
            message, state, "chiudibile", 7, "🧪 #7", "close"
        )

        assert await state.get_state() == schedule.ScheduleStates.event_runat.state
        assert (await state.get_data())["sched_action"] == "close"

    async def test_choosing_close_arms_the_run_at_step(self, session, closable):
        state = _state()
        await schedule.start_schedule_for(_FakeMessage(), state, "chiudibile", 7, "🧪 #7")
        cb = SchedCb(action="act", key="close")
        callback = _FakeCallback(cb.pack())

        await schedule.cb_action(callback, cb, state)

        assert (await state.get_data())["sched_action"] == "close"
        assert await state.get_state() == schedule.ScheduleStates.event_runat.state

    async def test_a_stale_action_button_says_so_instead_of_arming_nothing(
        self, session, closable
    ):
        """The flow was cleared (cancelled, or another one started): without a
        target, arming the run-at step would take a time and drop it."""
        state = _state()
        cb = SchedCb(action="act", key="close")
        callback = _FakeCallback(cb.pack())

        await schedule.cb_action(callback, cb, state)

        assert callback.alerts and "Ricomincia" in callback.alerts[0]
        assert await state.get_state() is None

    async def test_an_unknown_action_is_refused(self, session, closable):
        state = _state()
        await schedule.start_schedule_for(_FakeMessage(), state, "chiudibile", 7, "🧪 #7")
        cb = SchedCb(action="act", key="inventata")
        callback = _FakeCallback(cb.pack())

        await schedule.cb_action(callback, cb, state)

        assert await state.get_state() is None

    async def test_a_scheduled_close_carries_the_action_in_its_payload(
        self, session, closable, user_factory
    ):
        """Same task_type as the start — the close rides an action payload, the way
        the betting auto-lock already does, so there is no second task type."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await schedule.start_schedule_for(_FakeMessage(), state, "chiudibile", 7, "🧪 #7")
        cb = SchedCb(action="act", key="close")
        await schedule.cb_action(_FakeCallback(cb.pack()), cb, state)

        await schedule.fsm_event_runat(_FakeMessage("2h"), state, session)

        task = (await _tasks(session))[0]
        assert task.task_type == "chiudibile"
        assert schedule_service.task_payload(task) == {"action": "close"}

    async def test_a_scheduled_start_still_carries_no_payload(
        self, session, closable, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await schedule.start_schedule_for(_FakeMessage(), state, "chiudibile", 7, "🧪 #7")
        cb = SchedCb(action="act", key="start")
        await schedule.cb_action(_FakeCallback(cb.pack()), cb, state)

        await schedule.fsm_event_runat(_FakeMessage("2h"), state, session)

        assert (await _tasks(session))[0].payload_json is None


# ---------------------------------------------------------------------------
# /programmati
# ---------------------------------------------------------------------------

class TestPendingList:
    async def _task(self, session, *, task_type="fake", hours=1) -> ScheduledTask:
        task = await schedule_service.schedule_task(
            session, task_type, schedule_service.utcnow() + timedelta(hours=hours),
            ADMIN_ID, GROUP_CHAT, ref_id=7,
        )
        await session.commit()
        return task

    async def test_with_nothing_scheduled_it_says_so(self, session):
        message = _FakeMessage()

        await schedule.cmd_programmati(message, session)

        assert "Nessun evento programmato" in message.said

    async def test_each_pending_task_gets_a_cancel_button(self, session, only_fake, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        task = await self._task(session)
        message = _FakeMessage()

        await schedule.cmd_programmati(message, session)

        assert SchedCb(action="del", item_id=task.id).pack() in _callbacks(message.markups[0])
        assert "🧪 Finto" in message.said

    async def test_the_list_says_which_tasks_are_closes(self, session, only_fake, user_factory):
        """An item can have a start and a close pending at once: telling them apart
        is the difference between cancelling the right one and the wrong one."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        await self._task(session)
        await schedule_service.schedule_task(
            session, "fake", schedule_service.utcnow() + timedelta(hours=2),
            ADMIN_ID, GROUP_CHAT, ref_id=7, payload={"action": "close"},
        )
        await session.commit()
        message = _FakeMessage()

        await schedule.cmd_programmati(message, session)

        assert "▶️ Avvio" in message.said and "🏁 Chiusura" in message.said

    async def test_a_task_whose_type_is_gone_is_still_listed(self, session, user_factory):
        """Otherwise the only way to cancel it would disappear with its type."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        task = await self._task(session, task_type="scomparso")
        message = _FakeMessage()

        await schedule.cmd_programmati(message, session)

        assert "scomparso" in message.said
        assert SchedCb(action="del", item_id=task.id).pack() in _callbacks(message.markups[0])

    async def test_cancelling_removes_it_from_the_list_for_good(
        self, session, only_fake, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        task = await self._task(session)
        cb = SchedCb(action="del", item_id=task.id)
        callback = _FakeCallback(cb.pack())

        await schedule.cb_sched_del(callback, cb, session)
        await session.rollback()  # only a committed cancel survives this

        assert await schedule_service.list_pending(session) == []
        assert "annullato" in callback.said.lower()

    async def test_cancelling_a_task_that_already_ran_is_refused(
        self, session, only_fake, user_factory
    ):
        """It has already fired in the group: pretending it was cancelled would tell
        the admin something untrue."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        task = await self._task(session)
        await schedule_service.mark_done(session, task)
        await session.commit()
        cb = SchedCb(action="del", item_id=task.id)
        callback = _FakeCallback(cb.pack())

        await schedule.cb_sched_del(callback, cb, session)

        assert callback.alerts and "Non annullabile" in callback.alerts[0]
        assert callback.message.texts == []


# ---------------------------------------------------------------------------
# Cancelling the flow
# ---------------------------------------------------------------------------

class TestCancel:
    async def test_cancelling_before_anything_is_entered_is_immediate(self, session):
        """Nothing to lose → no «sei sicuro?» in the way."""
        callback = _FakeCallback(SchedCb(action="cancel").pack())

        await schedule.cb_sched_cancel(callback, _state())

        assert "annullata" in callback.said.lower()

    async def test_cancelling_mid_flow_asks_first(self, session):
        state = _state()
        await state.set_state(schedule.ScheduleStates.event_runat)
        callback = _FakeCallback(SchedCb(action="cancel").pack())

        await schedule.cb_sched_cancel(callback, state)

        assert set(_callbacks(callback.message.markups[0])) == {
            SchedCb(action="cancel_yes").pack(), SchedCb(action="cancel_no").pack(),
        }
        assert await state.get_state() is not None

    async def test_confirming_clears_the_flow(self, session):
        state = _state()
        await state.set_state(schedule.ScheduleStates.event_runat)
        await state.update_data(sched_ref=7)

        await schedule.cb_sched_cancel_yes(_FakeCallback(SchedCb(action="cancel_yes").pack()), state)

        assert await state.get_state() is None and await state.get_data() == {}

    async def test_refusing_keeps_it(self, session):
        state = _state()
        await state.set_state(schedule.ScheduleStates.event_runat)
        await state.update_data(sched_ref=7)

        await schedule.cb_sched_cancel_no(_FakeCallback(SchedCb(action="cancel_no").pack()))

        assert (await state.get_data())["sched_ref"] == 7


# ---------------------------------------------------------------------------
# /sondaggio
# ---------------------------------------------------------------------------

class TestSondaggio:
    async def test_in_the_group_it_only_hands_back_a_link(self, session):
        """The creation prompts would otherwise run in the group, where anyone can
        read them and any other admin's message can drive the FSM (§16)."""
        message = _FakeMessage(chat_type="supergroup")

        await schedule.cmd_sondaggio(message, _state())

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=create_poll")

    async def test_in_private_it_enters_the_canonical_poll_flow(self, session):
        from handlers import events

        state = _state()
        message = _FakeMessage()

        await schedule.cmd_sondaggio(message, state)

        assert await state.get_state() == events.PollTemplateStates.question.state

    async def test_the_second_attempt_within_the_cooldown_is_refused(self, session):
        """Admins are deliberately not exempt: creating events is the one thing an
        admin can spam the event list with by accident."""
        await schedule.cmd_sondaggio(_FakeMessage(), _state())
        second = _FakeMessage()
        state = _state()

        await schedule.cmd_sondaggio(second, state)

        assert "più piano" in second.said.lower()
        assert await state.get_state() is None
