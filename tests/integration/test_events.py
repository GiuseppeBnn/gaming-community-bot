"""Integration tests for the unified Events model: draft bets + poll templates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from database.models import EventStatus
from exceptions.economy import EventNotFoundError
from handlers.callbacks import EventCb, QuizTryCb
from services import bet_service, poll_service


class _FakeBot:
    """Records send_poll so a test can assert a poll was NOT published."""

    def __init__(self):
        self.polls = []

    async def send_poll(self, **kwargs):
        self.polls.append(kwargs)


class _FakeMessage:
    def __init__(self, text, bot, user_id=1):
        self.text = text
        self.bot = bot
        self.from_user = SimpleNamespace(id=user_id)
        self.replies = []

    async def answer(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class _FakeCallback:
    """Drives the poll-creation menu steps (description/prize/close), which are
    callback-driven. `message` is the panel the handlers answer on."""

    def __init__(self, bot, user_id=1):
        self.message = _FakeMessage("", bot, user_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


def _fresh_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


class TestDraftBets:
    async def test_create_draft_and_list(self, session, user_factory):
        await user_factory(1, "creator")
        event = await bet_service.create_event(
            session,
            creator_tg_id=1,
            title="Match",
            description="d",
            options=[{"label": "A"}, {"label": "B"}],
            status=EventStatus.draft.value,
        )
        await session.commit()
        assert event.status == EventStatus.draft.value
        # Drafts are excluded from the user/admin open lists...
        assert await bet_service.get_open_events(session) == []
        # ...but visible in the Events hub draft list.
        drafts = await bet_service.list_drafts(session)
        assert [d.id for d in drafts] == [event.id]

    async def test_activate_draft_opens_it(self, session, user_factory):
        await user_factory(1, "creator")
        event = await bet_service.create_event(
            session,
            creator_tg_id=1,
            title="Match",
            description="d",
            options=[{"label": "A"}, {"label": "B"}],
            status=EventStatus.draft.value,
        )
        await session.commit()
        activated = await bet_service.activate_event(session, event.id)
        await session.commit()
        assert activated.status == EventStatus.open.value
        assert await bet_service.list_drafts(session) == []
        opened = await bet_service.get_open_events(session)
        assert [e.id for e in opened] == [event.id]

    async def test_activate_is_idempotent_for_open(self, session, user_factory):
        await user_factory(1, "creator")
        event = await bet_service.create_event(
            session,
            creator_tg_id=1,
            title="M",
            description="",
            options=[{"label": "A"}, {"label": "B"}],
            status=EventStatus.draft.value,
        )
        await session.commit()
        await bet_service.activate_event(session, event.id)
        again = await bet_service.activate_event(session, event.id)  # no error
        assert again.status == EventStatus.open.value

    async def test_activate_missing_raises(self, session):
        with pytest.raises(EventNotFoundError):
            await bet_service.activate_event(session, 9999)

    async def test_default_status_is_open(self, session, user_factory):
        await user_factory(1, "creator")
        event = await bet_service.create_event(
            session,
            creator_tg_id=1,
            title="M",
            description="",
            options=[{"label": "A"}, {"label": "B"}],
        )
        await session.commit()
        assert event.status == EventStatus.open.value


class TestPollTemplates:
    async def test_create_list_and_use(self, session):
        poll = await poll_service.create_template(
            session,
            creator_tg_id=1,
            question="Best game?",
            options=["A", "B", "C"],
            group_id=None,
        )
        await session.commit()
        assert poll.status == "ready"
        assert poll_service.options_of(poll) == ["A", "B", "C"]
        assert [p.id for p in await poll_service.list_ready(session)] == [poll.id]

        await poll_service.mark_used(session, poll.id)
        await session.commit()
        used = await poll_service.get(session, poll.id)
        assert used.status == "used" and used.used_at is not None
        assert await poll_service.list_ready(session) == []


class TestPollCreationFlow:
    """A poll must be created → stored → and only then started/scheduled by an
    explicit admin choice — never auto-published on creation (like quiz/scommesse)."""

    async def test_finishing_creation_stores_template_without_publishing(self, session):
        """The minimal path (skip description, no prize, no auto-close) still stores
        a ready template and never publishes it — the admin picks avvia/programma."""
        from handlers.events import (
            PollTemplateStates,
            cb_pt_close_none,
            cb_pt_desc_skip,
            cb_pt_prize_none,
            fsm_pt_options,
        )

        state = _fresh_state()
        await state.set_state(PollTemplateStates.description)
        await state.update_data(pt_question="Best game?", pt_creator=1)
        bot = _FakeBot()

        await cb_pt_desc_skip(_FakeCallback(bot), state)
        await fsm_pt_options(_FakeMessage("A\nB\nC", bot), state)
        await cb_pt_prize_none(_FakeCallback(bot), state)
        cb = _FakeCallback(bot)
        await cb_pt_close_none(cb, state, session)

        # Stored as a ready (pre-created) template...
        polls = await poll_service.list_ready(session)
        assert len(polls) == 1
        assert polls[0].question == "Best game?"
        assert poll_service.options_of(polls[0]) == ["A", "B", "C"]
        assert not poll_service.has_prize(polls[0]) and polls[0].closes_at is None
        # ...and NOT posted to the group.
        assert bot.polls == []
        # The admin is offered the explicit choice (Avvia ora / Programma).
        _text, kb = cb.message.replies[-1]
        assert isinstance(kb, InlineKeyboardMarkup)
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert EventCb(action="start", task_type="poll", item_id=polls[0].id).pack() in callbacks
        assert EventCb(action="sched", task_type="poll", item_id=polls[0].id).pack() in callbacks
        # Flow is finished.
        assert await state.get_state() is None

    async def test_full_creation_with_prize_description_and_close(self, session):
        """The rich path: a description, the default prize and a scheduled close are
        all captured on the stored template."""
        from datetime import datetime, timedelta, timezone

        from config_data.config import settings
        from handlers.events import (
            PollTemplateStates,
            cb_pt_prize_default,
            fsm_pt_close_at,
            fsm_pt_description,
            fsm_pt_options,
        )

        state = _fresh_state()
        await state.set_state(PollTemplateStates.description)
        await state.update_data(pt_question="Best game?", pt_creator=1)
        bot = _FakeBot()

        await fsm_pt_description(_FakeMessage("Vota il migliore!", bot), state)
        await fsm_pt_options(_FakeMessage("A\nB", bot), state)
        await cb_pt_prize_default(_FakeCallback(bot), state)
        # An absolute future date for the auto-close.
        when = (datetime.now(tz=timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        await fsm_pt_close_at(_FakeMessage(when, bot), state, session)

        poll = (await poll_service.list_ready(session))[0]
        assert poll.description == "Vota il migliore!"
        assert poll.prize_coins == settings.poll_reward_coins
        assert poll.prize_xp == settings.poll_reward_xp
        assert poll.closes_at is not None
        assert await state.get_state() is None

    async def test_a_description_that_overflows_the_question_is_reprompted(self, session):
        """Question + description are folded into one native poll question (max 300),
        so an over-long description is refused and re-asked (like the trivia question
        length check) until it fits; a shorter one then advances to the options."""
        from handlers.events import (
            PollTemplateStates,
            _poll_length_overflow,
            fsm_pt_description,
        )

        # An empty description always fits, whatever the question length.
        assert _poll_length_overflow("Q" * 300, "") == 0

        state = _fresh_state()
        await state.set_state(PollTemplateStates.description)
        await state.update_data(pt_question="Q" * 280, pt_creator=1)
        bot = _FakeBot()

        too_long = _FakeMessage("D" * 100, bot)  # 280 + 2 + 100 = 382 > 300
        await fsm_pt_description(too_long, state)
        assert await state.get_state() == PollTemplateStates.description.state
        assert "300" in too_long.replies[-1][0]

        ok = _FakeMessage("D" * 10, bot)  # 280 + 2 + 10 = 292 ≤ 300
        await fsm_pt_description(ok, state)
        assert await state.get_state() == PollTemplateStates.options.state
        assert (await state.get_data())["pt_description"] == "D" * 10

    async def test_custom_prize_forces_a_close_date(self, session):
        """Custom-prize branch (with an invalid-entry reprompt on each amount). A
        prize is paid at the close, so choosing one jumps straight to the required
        close-date step — there is no «no close» option once a prize is set."""
        from datetime import datetime, timedelta, timezone

        from handlers.events import (
            PollTemplateStates,
            cb_pt_desc_skip,
            cb_pt_prize_custom,
            fsm_pt_close_at,
            fsm_pt_options,
            fsm_pt_prize_coins,
            fsm_pt_prize_xp,
        )

        state = _fresh_state()
        await state.set_state(PollTemplateStates.description)
        await state.update_data(pt_question="Best game?", pt_creator=1)
        bot = _FakeBot()

        await cb_pt_desc_skip(_FakeCallback(bot), state)
        await fsm_pt_options(_FakeMessage("A\nB", bot), state)
        await cb_pt_prize_custom(_FakeCallback(bot), state)
        assert await state.get_state() == PollTemplateStates.prize_coins.state

        await fsm_pt_prize_coins(_FakeMessage("abc", bot), state)  # invalid → reprompt
        assert await state.get_state() == PollTemplateStates.prize_coins.state
        await fsm_pt_prize_coins(_FakeMessage("100", bot), state)
        assert await state.get_state() == PollTemplateStates.prize_xp.state

        await fsm_pt_prize_xp(_FakeMessage("-5", bot), state)  # invalid → reprompt
        assert await state.get_state() == PollTemplateStates.prize_xp.state
        await fsm_pt_prize_xp(_FakeMessage("7", bot), state)
        # Prize set → the close date is REQUIRED, so we land straight on it.
        assert await state.get_state() == PollTemplateStates.close_at.state

        await fsm_pt_close_at(_FakeMessage("boh non è una data", bot), state, session)  # reprompt
        assert await state.get_state() == PollTemplateStates.close_at.state
        when = (datetime.now(tz=timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        await fsm_pt_close_at(_FakeMessage(when, bot), state, session)

        poll = (await poll_service.list_ready(session))[0]
        assert poll.prize_coins == 100 and poll.prize_xp == 7
        assert poll.closes_at is not None
        assert await state.get_state() is None

    async def test_no_prize_with_a_scheduled_close(self, session):
        """A close date WITHOUT a prize is allowed: at close the bot announces the
        winning option but pays nothing. Exercises the «schedule close» menu button."""
        from datetime import datetime, timedelta, timezone

        from handlers.events import (
            PollTemplateStates,
            cb_pt_close_set,
            cb_pt_desc_skip,
            cb_pt_prize_none,
            fsm_pt_close_at,
            fsm_pt_options,
        )

        state = _fresh_state()
        await state.set_state(PollTemplateStates.description)
        await state.update_data(pt_question="Best game?", pt_creator=1)
        bot = _FakeBot()

        await cb_pt_desc_skip(_FakeCallback(bot), state)
        await fsm_pt_options(_FakeMessage("A\nB", bot), state)
        await cb_pt_prize_none(_FakeCallback(bot), state)
        # No prize → the close is a menu (none / schedule); pick «schedule».
        assert await state.get_state() == PollTemplateStates.close_choice.state
        await cb_pt_close_set(_FakeCallback(bot), state)
        assert await state.get_state() == PollTemplateStates.close_at.state
        when = (datetime.now(tz=timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        await fsm_pt_close_at(_FakeMessage(when, bot), state, session)

        poll = (await poll_service.list_ready(session))[0]
        assert not poll_service.has_prize(poll) and poll.closes_at is not None
        assert await state.get_state() is None

    async def test_a_relative_close_token_is_refused(self, session):
        """'1d' is ambiguous for a poll not yet running (from now or from start?),
        so the close field takes only an absolute date."""
        from handlers.events import PollTemplateStates, fsm_pt_close_at

        state = _fresh_state()
        await state.set_state(PollTemplateStates.close_at)
        await state.update_data(pt_question="Q", pt_creator=1, pt_options=["A", "B"])
        bot = _FakeBot()
        msg = _FakeMessage("1d", bot)

        await fsm_pt_close_at(msg, state, session)

        # Nothing created, still waiting on the close field.
        assert await poll_service.list_ready(session) == []
        assert await state.get_state() == PollTemplateStates.close_at.state
        assert "durata" in msg.replies[-1][0]

    async def test_sondaggio_entry_starts_creation_without_publishing(self):
        """/sondaggio reuses the canonical creation flow: it prompts for the
        question and posts nothing (no immediate publish)."""
        from handlers.events import PollTemplateStates, start_poll_creation

        state = _fresh_state()
        bot = _FakeBot()
        message = _FakeMessage("ignored", bot)

        await start_poll_creation(message, state)

        assert await state.get_state() == PollTemplateStates.question.state
        assert bot.polls == []  # nothing published
        assert message.replies  # prompted for the question


class TestQuizEventType:
    """Quizzes are persistent objects in the hub: the list routes every item to its
    detail screen (never a one-tap launch), and each impactful action goes through
    an ``ev:ask*`` confirmation step."""

    @staticmethod
    def _cbs(kb) -> list[str]:
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    async def _quiz(self, session, *, status="ready", title="Quiz"):
        import services.quiz_service as qz

        quiz = await qz.create_quiz(session, 9, title, "d")  # created as `draft`
        await qz.add_question(session, quiz.id, "Q", ["a", "b"], 0, None)
        await qz.set_status(session, quiz.id, status)
        await session.commit()
        return quiz

    async def test_render_list_shows_all_statuses_and_routes_to_detail(self, session):
        from handlers.event_types.quiz_type import QuizType

        ready = await self._quiz(session, status="ready", title="R")
        running = await self._quiz(session, status="running", title="G")
        finished = await self._quiz(session, status="finished", title="F")

        message = _FakeMessage("", _FakeBot())
        await QuizType().render_list(message, session)
        _text, kb = message.replies[-1]
        cbs = self._cbs(kb)
        for q in (ready, running, finished):
            # tap → detail, not launch
            assert EventCb(action="item", task_type="quiz", item_id=q.id).pack() in cbs
        assert not any(c.startswith("ev:start:") for c in cbs)

    async def test_render_detail_ready_confirms_start_and_offers_delete(self, session):
        from handlers.event_types.quiz_type import QuizType

        quiz = await self._quiz(session, status="ready")
        message = _FakeMessage("", _FakeBot())
        await QuizType().render_detail(message, session, quiz.id)
        cbs = self._cbs(message.replies[-1][1])
        # start is confirmed
        assert EventCb(action="askstart", task_type="quiz", item_id=quiz.id).pack() in cbs
        assert EventCb(action="sched", task_type="quiz", item_id=quiz.id).pack() in cbs
        assert EventCb(action="askdel", task_type="quiz", item_id=quiz.id).pack() in cbs
        assert QuizTryCb(action="start", quiz_id=quiz.id).pack() in cbs  # dry-run before going live
        assert not any(c.startswith("ev:start:") for c in cbs)  # no one-tap launch

    async def test_render_detail_finished_offers_reset_and_delete(self, session):
        from handlers.event_types.quiz_type import QuizType

        quiz = await self._quiz(session, status="finished")
        message = _FakeMessage("", _FakeBot())
        await QuizType().render_detail(message, session, quiz.id)
        cbs = self._cbs(message.replies[-1][1])
        assert EventCb(action="askreset", task_type="quiz", item_id=quiz.id).pack() in cbs
        assert EventCb(action="askdel", task_type="quiz", item_id=quiz.id).pack() in cbs
