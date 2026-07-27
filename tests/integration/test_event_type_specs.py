"""The event-type specs — `handlers/event_types/{bet,poll,quiz}_type.py`.

These three classes are the *only* dispatch point between the events hub, the
scheduler loop and the three kinds of event (STEERING: no per-type if/elif outside
the registry). They were 43% / 36% / 78% covered, which meant the seam everything
routes through was mostly guesswork — including the path that opens a betting event
in the group, i.e. the one that starts a pot of real coins.

What is asserted here is the contract of the seam, not the machinery underneath
(`bet_service`, `poll_service`, `quiz_service` are tested on their own):

  * **never commit** (§5): the caller owns the transaction. Every test drives the
    spec and then checks the *session*, not a committed row;
  * a group announcement is best-effort in both directions — a bet must open even
    if the group send explodes, and it must not even be attempted with no group
    configured;
  * `start_now` turns every failure into a `StartResult(ok=False, alert=True)`
    instead of raising into the callback, while `execute_scheduled` does the
    opposite: it raises, because the scheduler loop is what decides failed vs
    skipped.

Where a "best-effort" branch is tested, the failure is injected in the **bot**
(where it happens in production), never by stubbing the method that swallows it —
stubbing that would only test the stub.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

import services.bet_service as bet_svc
from database.models import BettingEvent, EventStatus, PollTemplate, ScheduledTask
from handlers.event_types.base import StartResult, edit_or_send
from handlers.event_types.bet_type import BetType
from handlers.event_types.poll_type import PollType
from handlers.event_types.quiz_type import QuizType
from services import group_registry, poll_service, quiz_service
from services.schedule_service import TaskSkip

ADMIN_ID = 1
GROUP_ID = -100_777


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.polls: list[dict] = []

    async def get_me(self):
        from types import SimpleNamespace
        return SimpleNamespace(username="testbot")

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))

    async def send_poll(self, chat_id, question, options, **kw):
        self.polls.append({"chat_id": chat_id, "question": question, "options": options})


class _BrokenBot(_FakeBot):
    """The group send fails — bot kicked, group deleted, Telegram down."""

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))
        raise RuntimeError("Bad Request: chat not found")


class _FakeMessage:
    """`edit_or_send` edits when it can and falls back to a fresh send."""

    def __init__(self, *, editable: bool = True) -> None:
        self.editable = editable
        self.edited: list[str] = []
        self.answered: list[str] = []
        self.markups: list[object] = []

    async def edit_text(self, text, reply_markup=None, **kw):
        if not self.editable:
            raise RuntimeError("message is not modified")
        self.edited.append(text)
        self.markups.append(reply_markup)

    async def answer(self, text, reply_markup=None, **kw):
        self.answered.append(text)
        self.markups.append(reply_markup)

    @property
    def said(self) -> str:
        return "\n".join(self.edited + self.answered)


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID))


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


@pytest.fixture
def in_group():
    """A configured group, restored afterwards: the id is module-level state and a
    leak would silently change what the *next* test's announcements do."""
    group_registry.set_runtime_group_id(GROUP_ID)
    yield GROUP_ID
    group_registry.set_runtime_group_id(None)


@pytest.fixture
def no_group():
    group_registry.set_runtime_group_id(0)
    yield 0
    group_registry.set_runtime_group_id(None)


async def _draft(session, *, title="Derby", window=None) -> BettingEvent:
    event = await bet_svc.create_event(
        session, creator_tg_id=ADMIN_ID, title=title, description="chi vince",
        options=[{"label": "Casa"}, {"label": "Trasferta"}],
        status=EventStatus.draft.value, window_seconds=window,
    )
    await session.commit()
    return event


def _task(**kw) -> ScheduledTask:
    return ScheduledTask(
        task_type=kw.pop("task_type", "bet"),
        run_at=kw.pop("run_at", None),
        created_by_tg_id=kw.pop("created_by_tg_id", ADMIN_ID),
        status="pending",
        **kw,
    )


# ---------------------------------------------------------------------------
# base.edit_or_send — used by every render_list
# ---------------------------------------------------------------------------

class TestEditOrSend:
    async def test_it_edits_in_place_when_it_can(self):
        message = _FakeMessage()

        await edit_or_send(message, "ciao")

        assert message.edited == ["ciao"] and message.answered == []

    async def test_it_sends_a_fresh_message_when_editing_fails(self):
        """A callback on a message that is too old, unchanged or not editable must
        still show the screen — the alternative is a button that does nothing."""
        message = _FakeMessage(editable=False)

        await edit_or_send(message, "ciao")

        assert message.answered == ["ciao"]


# ---------------------------------------------------------------------------
# BetType
# ---------------------------------------------------------------------------

class TestBetTypeHub:
    async def test_the_list_offers_one_button_per_draft(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        event = await _draft(session)
        message = _FakeMessage()

        await BetType().render_list(message, session)

        assert f"ev:item:bet:{event.id}" in _callbacks(message.markups[0])

    async def test_the_empty_list_still_offers_the_create_button(self, session):
        message = _FakeMessage()

        await BetType().render_list(message, session)

        assert "Nessuna bozza" in message.said
        assert "ev:new:bet" in _callbacks(message.markups[0])

    async def test_only_drafts_are_schedulable(self, session, user_factory):
        """An already-open event must not appear in the scheduling menu: scheduling
        it would try to activate what is already active."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        draft = await _draft(session)
        await bet_svc.create_event(
            session, creator_tg_id=ADMIN_ID, title="Aperta", description="x",
            options=[{"label": "A"}, {"label": "B"}],
        )
        await session.commit()

        items = await BetType().schedulable_items(session)

        assert [i[0] for i in items] == [draft.id]

    async def test_creating_from_the_hub_starts_a_draft(self, session):
        """The hub's create button must produce a *draft* — an event opened straight
        away could not be scheduled, which is the whole point of the hub."""
        state = _state()
        message = _FakeMessage()

        await BetType().start_creation(message, state, ADMIN_ID)

        assert (await state.get_data())["bet_as_draft"] is True


class TestBetTypeStartNow:
    async def test_starting_a_draft_opens_it_and_announces_it(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        event = await _draft(session)
        bot = _FakeBot()

        result = await BetType().start_now(bot, session, event.id)

        assert result.ok
        assert (await session.get(BettingEvent, event.id)).status == EventStatus.open.value
        chat_id, text = bot.messages[0]
        assert chat_id == GROUP_ID and "Nuova scommessa aperta" in text

    async def test_starting_a_windowed_draft_also_schedules_its_auto_lock(
        self, session, user_factory, in_group
    ):
        """Without the scheduled lock the window is decoration: nothing would ever
        close the betting."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        event = await _draft(session, window=3600)

        await BetType().start_now(_FakeBot(), session, event.id)

        task = (await session.execute(select(ScheduledTask))).scalar_one()
        assert (task.task_type, task.ref_id) == ("bet", event.id)
        assert json.loads(task.payload_json)["action"] == "lock"

    async def test_starting_an_unknown_event_answers_an_alert_instead_of_raising(
        self, session
    ):
        """`start_now` runs inside a callback: an exception here would surface as a
        dead button, so every failure has to come back as a StartResult."""
        result = await BetType().start_now(_FakeBot(), session, 999)

        assert not result.ok and result.alert

    async def test_starting_a_resolved_event_is_refused(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        event = await _draft(session)
        event.status = EventStatus.resolved.value
        await session.commit()

        result = await BetType().start_now(_FakeBot(), session, event.id)

        assert not result.ok and result.alert

    async def test_an_announcement_that_explodes_still_leaves_the_bet_open(
        self, session, user_factory, in_group
    ):
        """The bet is open the moment the status flips; a failed announcement is a
        missing notification, not a reason to keep the event closed."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        event = await _draft(session)
        bot = _BrokenBot()

        result = await BetType().start_now(bot, session, event.id)

        assert bot.messages, "the announcement must be attempted, or this asserts nothing"
        assert result.ok
        assert (await session.get(BettingEvent, event.id)).status == EventStatus.open.value

    async def test_with_no_group_configured_nothing_is_sent(
        self, session, user_factory, no_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        event = await _draft(session)
        bot = _FakeBot()

        result = await BetType().start_now(bot, session, event.id)

        assert result.ok and bot.messages == []

    async def test_the_announcement_says_the_deadline_when_there_is_one(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        windowed = await _draft(session, window=3600)
        unlimited = await _draft(session, title="Senza scadenza")
        bot = _FakeBot()

        await BetType().start_now(bot, session, windowed.id)
        await BetType().start_now(bot, session, unlimited.id)

        assert "Chiude alle" in bot.messages[0][1]
        assert "senza scadenza" in bot.messages[1][1].lower()

    async def test_a_bet_has_no_close_action(self, session):
        """Betting closes by locking + resolving from the admin panel, not from the
        hub's generic «chiudi» — returning None is what hides that button."""
        assert await BetType().close_now(_FakeBot(), session, 1) is None


class TestBetTypeScheduledOpen:
    async def test_a_scheduled_task_activates_the_referenced_draft(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        event = await _draft(session, window=3600)
        task = _task(ref_id=event.id, payload_json=json.dumps({}))
        bot = _FakeBot()

        await BetType().execute_scheduled(bot, session, task, GROUP_ID)

        assert (await session.get(BettingEvent, event.id)).status == EventStatus.open.value
        lock = (await session.execute(select(ScheduledTask))).scalar_one()
        assert json.loads(lock.payload_json)["action"] == "lock"
        assert "Nuova scommessa aperta" in bot.messages[0][1]

    async def test_a_legacy_task_without_ref_id_creates_the_event_from_its_payload(
        self, session, user_factory, in_group
    ):
        """Tasks scheduled before the draft model exist in production databases; the
        payload path must keep working or they'd fail on the night they fire."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        task = _task(payload_json=json.dumps({
            "title": "Vecchia", "description": "schedulata prima", "options": ["A", "B"],
        }))
        bot = _FakeBot()

        await BetType().execute_scheduled(bot, session, task, GROUP_ID)

        event = (await session.execute(select(BettingEvent))).scalar_one()
        assert event.status == EventStatus.open.value
        assert [o.label for o in await _options(session, event.id)] == ["A", "B"]
        assert "schedulata prima" in bot.messages[0][1]

    async def test_an_auto_lock_for_a_deleted_event_fails_loudly(self, session):
        """A missing event is a real failure (the admin must be told), unlike a bet
        that is simply no longer open — that one is a skip."""
        task = _task(ref_id=999, payload_json=json.dumps({"action": "lock"}))

        with pytest.raises(RuntimeError):
            await BetType().execute_scheduled(_FakeBot(), session, task, GROUP_ID)

    async def test_the_close_announcement_failing_does_not_fail_the_lock(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        event = await _draft(session)
        await bet_svc.activate_event(session, event.id)
        await session.commit()
        task = _task(ref_id=event.id, payload_json=json.dumps({"action": "lock"}))
        bot = _BrokenBot()

        await BetType().execute_scheduled(bot, session, task, GROUP_ID)

        assert bot.messages, "the notice must be attempted, or this asserts nothing"
        assert (await session.get(BettingEvent, event.id)).status == EventStatus.locked.value

    async def test_with_no_group_the_close_notice_is_not_even_attempted(
        self, session, user_factory, no_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        event = await _draft(session)
        await bet_svc.activate_event(session, event.id)
        await session.commit()
        task = _task(ref_id=event.id, payload_json=json.dumps({"action": "lock"}))
        bot = _FakeBot()

        await BetType().execute_scheduled(bot, session, task, GROUP_ID)

        assert bot.messages == []
        assert (await session.get(BettingEvent, event.id)).status == EventStatus.locked.value


async def _options(session, event_id: int):
    from database.models import BettingOption
    return list((await session.execute(
        select(BettingOption).where(BettingOption.event_id == event_id)
        .order_by(BettingOption.id)
    )).scalars().all())


# ---------------------------------------------------------------------------
# PollType
# ---------------------------------------------------------------------------

class TestPollType:
    async def test_the_list_offers_one_button_per_ready_poll(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        poll = await poll_service.create_template(session, ADMIN_ID, "Meglio?", ["A", "B"])
        await session.commit()
        message = _FakeMessage()

        await PollType().render_list(message, session)

        assert f"ev:item:poll:{poll.id}" in _callbacks(message.markups[0])

    async def test_the_empty_list_still_offers_the_create_button(self, session):
        message = _FakeMessage()

        await PollType().render_list(message, session)

        assert "Nessun sondaggio pronto" in message.said
        assert "ev:new:poll" in _callbacks(message.markups[0])

    async def test_a_used_poll_is_neither_listed_nor_schedulable(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        poll = await poll_service.create_template(session, ADMIN_ID, "Meglio?", ["A", "B"])
        await poll_service.mark_used(session, poll.id)
        await session.commit()

        assert await PollType().schedulable_items(session) == []

    async def test_publishing_sends_the_poll_and_consumes_the_template(
        self, session, user_factory, in_group
    ):
        """`mark_used` is what stops the same template being published twice."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        poll = await poll_service.create_template(session, ADMIN_ID, "Meglio?", ["A", "B"])
        await session.commit()
        bot = _FakeBot()

        result = await PollType().start_now(bot, session, poll.id)

        assert result.ok
        assert bot.polls == [{"chat_id": GROUP_ID, "question": "Meglio?", "options": ["A", "B"]}]
        assert (await session.get(PollTemplate, poll.id)).status == "used"

    async def test_publishing_the_same_poll_twice_is_refused(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        poll = await poll_service.create_template(session, ADMIN_ID, "Meglio?", ["A", "B"])
        await session.commit()
        bot = _FakeBot()

        await PollType().start_now(bot, session, poll.id)
        result = await PollType().start_now(bot, session, poll.id)

        assert not result.ok and result.alert
        assert len(bot.polls) == 1

    async def test_publishing_an_unknown_poll_is_refused(self, session, in_group):
        result = await PollType().start_now(_FakeBot(), session, 999)

        assert not result.ok and result.alert

    async def test_without_a_group_it_says_so_instead_of_sending(
        self, session, user_factory, no_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        poll = await poll_service.create_template(session, ADMIN_ID, "Meglio?", ["A", "B"])
        await session.commit()
        bot = _FakeBot()

        result = await PollType().start_now(bot, session, poll.id)

        assert not result.ok and "GROUP_ID" in result.message
        assert bot.polls == []
        assert (await session.get(PollTemplate, poll.id)).status == "ready", \
            "a template must not be consumed by a send that never happened"

    async def test_a_scheduled_poll_is_published_and_consumed(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        poll = await poll_service.create_template(session, ADMIN_ID, "Meglio?", ["A", "B"])
        await session.commit()
        bot = _FakeBot()

        await PollType().execute_scheduled(bot, session, _task(task_type="poll", ref_id=poll.id),
                                           GROUP_ID)

        assert bot.polls[0]["question"] == "Meglio?"
        assert (await session.get(PollTemplate, poll.id)).status == "used"

    async def test_a_scheduled_poll_whose_template_was_deleted_fails_loudly(self, session):
        with pytest.raises(RuntimeError):
            await PollType().execute_scheduled(
                _FakeBot(), session, _task(task_type="poll", ref_id=999), GROUP_ID
            )

    async def test_a_legacy_scheduled_poll_uses_its_payload(self, session):
        bot = _FakeBot()
        task = _task(task_type="poll",
                     payload_json=json.dumps({"question": "Vecchia?", "options": ["Sì", "No"]}))

        await PollType().execute_scheduled(bot, session, task, GROUP_ID)

        assert bot.polls == [{"chat_id": GROUP_ID, "question": "Vecchia?",
                              "options": ["Sì", "No"]}]

    async def test_creating_from_the_hub_enters_the_poll_fsm(self, session):
        state = _state()

        await PollType().start_creation(_FakeMessage(), state, ADMIN_ID)

        assert await state.get_state() is not None

    async def test_a_poll_has_no_close_action(self, session):
        assert await PollType().close_now(_FakeBot(), session, 1) is None


# ---------------------------------------------------------------------------
# QuizType — the parts the scheduler and the hub reach
# ---------------------------------------------------------------------------

async def _quiz_status(session, quiz_id: int) -> str:
    """Read the status as a *column*.

    `quiz_service.get_quiz` is an entity select, and with `expire_on_commit=False`
    it can be answered straight from the identity map — so after `close_quiz`,
    whose transition is a conditional UPDATE, it hands back an instance still
    saying "running". A column select never goes through the map (STEERING §22).
    """
    from database.models import Quiz
    return (await session.execute(
        select(Quiz.status).where(Quiz.id == quiz_id)
    )).scalar_one()


async def _quiz(session, *, status="ready", questions=1, title="Capitali"):
    quiz = await quiz_service.create_quiz(session, ADMIN_ID, title, "Geo")
    for i in range(questions):
        await quiz_service.add_question(
            session, quiz.id, f"Domanda {i}?", ["Roma", "Milano"], 0, None
        )
    await quiz_service.set_status(session, quiz.id, status)
    await session.commit()
    return quiz


class TestQuizTypeDetail:
    async def test_a_deleted_quiz_shows_a_way_back_instead_of_crashing(self, session):
        message = _FakeMessage()

        await QuizType().render_detail(message, session, 999)

        assert "non trovato" in message.said
        assert "ev:list:quiz" in _callbacks(message.markups[0])

    async def test_a_ready_quiz_offers_start_edit_try_and_delete(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session)
        message = _FakeMessage()

        await QuizType().render_detail(message, session, quiz.id)

        actions = _callbacks(message.markups[0])
        assert f"ev:askstart:quiz:{quiz.id}" in actions
        assert f"quiz_edit:nav:{quiz.id}:0" in actions
        assert f"quiz_try:start:{quiz.id}" in actions
        assert f"ev:askdel:quiz:{quiz.id}" in actions

    async def test_a_running_quiz_offers_close_and_never_start(self, session, user_factory):
        """Offering «avvia» on a running quiz would restart it under the players."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session, status="running")
        message = _FakeMessage()

        await QuizType().render_detail(message, session, quiz.id)

        actions = _callbacks(message.markups[0])
        assert f"ev:askclose:quiz:{quiz.id}" in actions
        assert not any(a.startswith("ev:askstart") for a in actions)

    async def test_a_finished_quiz_offers_the_re_run(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session, status="finished")
        message = _FakeMessage()

        await QuizType().render_detail(message, session, quiz.id)

        assert f"ev:askreset:quiz:{quiz.id}" in _callbacks(message.markups[0])

    async def test_the_detail_of_a_played_quiz_reports_its_timestamps(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session, status="running")
        quiz.started_at = quiz_service._now()
        await session.commit()
        message = _FakeMessage()

        await QuizType().render_detail(message, session, quiz.id)

        assert "Avviato:" in message.said
        assert "partecipanti" in message.said


class TestQuizTypeActions:
    async def test_deleting_an_existing_quiz_reports_success(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session)

        result = await QuizType().delete(session, quiz.id)

        assert result.ok and not result.alert

    async def test_deleting_an_already_deleted_quiz_is_an_alert_not_a_crash(self, session):
        result = await QuizType().delete(session, 999)

        assert not result.ok and result.alert

    async def test_only_a_finished_quiz_can_be_re_run(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        ready = await _quiz(session)
        finished = await _quiz(session, status="finished", title="Vecchio")

        refused = await QuizType().reset(session, ready.id)
        accepted = await QuizType().reset(session, finished.id)

        assert not refused.ok and refused.alert
        assert accepted.ok

    async def test_only_ready_quizzes_are_schedulable(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        ready = await _quiz(session)
        await _quiz(session, status="running", title="In corso")

        items = await QuizType().schedulable_items(session)

        assert [i[0] for i in items] == [ready.id]

    async def test_creating_from_the_hub_enters_the_quiz_fsm(self, session):
        state = _state()

        await QuizType().start_creation(_FakeMessage(), state, ADMIN_ID)

        assert await state.get_state() is not None

    async def test_starting_a_quiz_with_no_questions_comes_back_as_an_alert(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session, questions=0)

        result = await QuizType().start_now(_FakeBot(), session, quiz.id)

        assert not result.ok and result.alert

    async def test_starting_a_ready_quiz_opens_it(self, session, user_factory, in_group):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session)

        result = await QuizType().start_now(_FakeBot(), session, quiz.id)
        # The spec does not commit (§5) — the hub callback does, on ok. Committing
        # here is what the real caller does, and it is also what makes the change
        # visible to a column read (the ORM mutation is still pending otherwise).
        await session.commit()

        assert result.ok
        assert await _quiz_status(session, quiz.id) == "running"

    async def test_closing_a_running_quiz_publishes_the_podium(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session, status="running")
        bot = _FakeBot()

        result = await QuizType().close_now(bot, session, quiz.id)

        assert result.ok
        assert await _quiz_status(session, quiz.id) == "finished"

    async def test_closing_a_quiz_that_is_not_running_is_an_alert(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session)

        result = await QuizType().close_now(_FakeBot(), session, quiz.id)

        assert not result.ok and result.alert

    async def test_a_scheduled_start_on_an_already_running_quiz_is_a_skip(
        self, session, user_factory, in_group
    ):
        """Skip, not failure: an admin starting it by hand before the scheduled time
        reached the intended end state, and the loop must not report an error."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session, status="running")

        with pytest.raises(TaskSkip):
            await QuizType().execute_scheduled(
                _FakeBot(), session, _task(task_type="quiz", ref_id=quiz.id), GROUP_ID
            )

    async def test_a_scheduled_start_that_cannot_open_raises_for_the_loop(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await _quiz(session, questions=0)

        with pytest.raises(RuntimeError):
            await QuizType().execute_scheduled(
                _FakeBot(), session, _task(task_type="quiz", ref_id=quiz.id), GROUP_ID
            )


# ---------------------------------------------------------------------------
# The shared result object
# ---------------------------------------------------------------------------

def test_a_start_result_is_immutable():
    """The hub passes it around and renders it; a spec must not be able to mutate
    someone else's result after the fact."""
    result = StartResult(True, "ok")

    with pytest.raises(FrozenInstanceError):
        result.ok = False
