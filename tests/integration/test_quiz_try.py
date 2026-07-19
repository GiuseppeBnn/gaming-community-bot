"""Tests for the admin test-run of a quiz (§19.b).

The defining property is negative: a test run must leave NO trace in the DB, so
it can never reach the podium, the prizes, the XP or the leaderboard. These tests
assert that directly (zero `quiz_answers` rows) rather than trusting a filter.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import func, select

from database.models import QuizAnswer
from handlers import quiz as quiz_handlers
from services import quiz_service

ADMIN_ID = 42


class _FakeBot:
    async def send_message(self, *a, **kw):  # pragma: no cover - not exercised
        raise AssertionError("the test run must not send group messages")


class _FakeMessage:
    def __init__(self, user_id=ADMIN_ID):
        self.bot = _FakeBot()
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.replies = []

    async def answer(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))
        return SimpleNamespace(message_id=len(self.replies))

    async def edit_text(self, text, **kw):
        self.replies.append((text, None))


class _FakeCallback:
    def __init__(self, data, message, user_id=ADMIN_ID):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


def _cbs(markup):
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def _make_quiz(session, *, status="ready"):
    quiz = await quiz_service.create_quiz(session, 9, "Prova", "d")
    await quiz_service.add_question(session, quiz.id, "Capitale d'Italia?", ["Roma", "Milano"], 0, "È Roma.")
    await quiz_service.add_question(session, quiz.id, "2+2?", ["3", "4"], 1, None)
    await quiz_service.set_status(session, quiz.id, status)
    await session.commit()
    return quiz


async def _answer_count(session, quiz_id):
    return (await session.execute(
        select(func.count()).select_from(QuizAnswer).where(QuizAnswer.quiz_id == quiz_id)
    )).scalar_one()


class TestQuizTestRun:
    def teardown_method(self):
        quiz_handlers._TRY.clear()

    async def test_full_run_records_nothing_in_db(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()

        await quiz_handlers.start_quiz_try(message, session, quiz.id)
        # Answer every question correctly, following the buttons the flow offers.
        for _ in range(2):
            cbs = [c for c in _cbs(message.replies[-1][1]) if c.startswith("quiz_try:ans:")]
            assert cbs, "the test run should offer answer buttons"
            cb = _FakeCallback(cbs[0], message)
            await quiz_handlers.cb_try_answer(cb, session)

        assert await _answer_count(session, quiz.id) == 0
        # ...and the quiz is still ready to be started for real.
        refreshed = await quiz_service.get_quiz(session, quiz.id)
        assert refreshed.status == "ready"

    async def test_run_does_not_reach_the_podium(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()
        await quiz_handlers.start_quiz_try(message, session, quiz.id)
        for _ in range(2):
            cbs = [c for c in _cbs(message.replies[-1][1]) if c.startswith("quiz_try:ans:")]
            await quiz_handlers.cb_try_answer(_FakeCallback(cbs[0], message), session)

        assert await quiz_service.podium(session, quiz.id) == []
        assert await quiz_service.answered_count(session, quiz.id, ADMIN_ID) == 0

    async def test_final_screen_reports_the_score(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()
        await quiz_handlers.start_quiz_try(message, session, quiz.id)
        for _ in range(2):
            cbs = [c for c in _cbs(message.replies[-1][1]) if c.startswith("quiz_try:ans:")]
            # Index 0 of the *stored* order: correct for Q1 ("Roma"), wrong for Q2 ("3").
            await quiz_handlers.cb_try_answer(_FakeCallback(cbs[0], message), session)

        final = message.replies[-1][0]
        assert "Prova completata" in final
        assert "Nessun dato è stato salvato" in final

    async def test_every_message_is_marked_as_a_test(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()
        await quiz_handlers.start_quiz_try(message, session, quiz.id)
        assert "MODALITÀ PROVA" in message.replies[0][0]
        assert "🧪" in message.replies[1][0]

    async def test_refused_for_a_running_quiz(self, session):
        quiz = await _make_quiz(session, status="running")
        message = _FakeMessage()
        await quiz_handlers.start_quiz_try(message, session, quiz.id)
        assert "solo per un quiz" in message.replies[-1][0]
        assert _try_state(quiz.id) is None

    async def test_stop_clears_the_run(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()
        await quiz_handlers.start_quiz_try(message, session, quiz.id)
        assert _try_state(quiz.id) is not None

        await quiz_handlers.cb_try_stop(_FakeCallback(f"quiz_try:stop:{quiz.id}", message))
        assert _try_state(quiz.id) is None
        assert await _answer_count(session, quiz.id) == 0

    async def test_answering_without_an_active_run_is_rejected(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()
        loaded = await quiz_service.get_quiz(session, quiz.id)
        cb = _FakeCallback(f"quiz_try:ans:{quiz.id}:{loaded.questions[0].id}:0", message)
        await quiz_handlers.cb_try_answer(cb, session)
        assert any("scaduta" in (a or "") for a in cb.answers)
        assert await _answer_count(session, quiz.id) == 0


def _try_state(quiz_id: int):
    return quiz_handlers._TRY.get((quiz_id, ADMIN_ID))
