"""Integration tests for the unified Events model: draft bets + poll templates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from database.models import EventStatus
from exceptions.economy import EventAlreadySettledError, EventNotFoundError
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


def _fresh_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


class TestDraftBets:
    async def test_create_draft_and_list(self, session, user_factory):
        await user_factory(1, "creator")
        event = await bet_service.create_event(
            session, creator_tg_id=1, title="Match", description="d",
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
            session, creator_tg_id=1, title="Match", description="d",
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
            session, creator_tg_id=1, title="M", description="",
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
            session, creator_tg_id=1, title="M", description="",
            options=[{"label": "A"}, {"label": "B"}],
        )
        await session.commit()
        assert event.status == EventStatus.open.value


class TestPollTemplates:
    async def test_create_list_and_use(self, session):
        poll = await poll_service.create_template(
            session, creator_tg_id=1, question="Best game?",
            options=["A", "B", "C"], group_id=None,
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
        from handlers.events import PollTemplateStates, fsm_pt_options

        state = _fresh_state()
        await state.set_state(PollTemplateStates.options)
        await state.update_data(pt_question="Best game?")
        bot = _FakeBot()
        message = _FakeMessage("A\nB\nC", bot)

        await fsm_pt_options(message, state, session)

        # Stored as a ready (pre-created) template...
        polls = await poll_service.list_ready(session)
        assert len(polls) == 1
        assert polls[0].question == "Best game?"
        assert poll_service.options_of(polls[0]) == ["A", "B", "C"]
        # ...and NOT posted to the group.
        assert bot.polls == []
        # The admin is offered the explicit choice (Avvia ora / Programma).
        assert message.replies
        _text, kb = message.replies[-1]
        assert isinstance(kb, InlineKeyboardMarkup)
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"ev:start:poll:{polls[0].id}" in callbacks
        assert f"ev:sched:poll:{polls[0].id}" in callbacks
        # Flow is finished.
        assert await state.get_state() is None

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
            assert f"ev:item:quiz:{q.id}" in cbs         # tap → detail, not launch
        assert not any(c.startswith("ev:start:") for c in cbs)

    async def test_render_detail_ready_confirms_start_and_offers_delete(self, session):
        from handlers.event_types.quiz_type import QuizType

        quiz = await self._quiz(session, status="ready")
        message = _FakeMessage("", _FakeBot())
        await QuizType().render_detail(message, session, quiz.id)
        cbs = self._cbs(message.replies[-1][1])
        assert f"ev:askstart:quiz:{quiz.id}" in cbs      # start is confirmed
        assert f"ev:sched:quiz:{quiz.id}" in cbs
        assert f"ev:askdel:quiz:{quiz.id}" in cbs
        assert f"quiz_try:start:{quiz.id}" in cbs        # dry-run before going live
        assert not any(c.startswith("ev:start:") for c in cbs)  # no one-tap launch

    async def test_render_detail_finished_offers_reset_and_delete(self, session):
        from handlers.event_types.quiz_type import QuizType

        quiz = await self._quiz(session, status="finished")
        message = _FakeMessage("", _FakeBot())
        await QuizType().render_detail(message, session, quiz.id)
        cbs = self._cbs(message.replies[-1][1])
        assert f"ev:askreset:quiz:{quiz.id}" in cbs
        assert f"ev:askdel:quiz:{quiz.id}" in cbs
