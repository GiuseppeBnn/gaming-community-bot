"""Tests for the admin test-run of a quiz (§19.b).

Two properties are under test.

1. The defining one is negative: a test run must leave NO trace in the DB, so it
   can never reach the podium, the prizes, the XP or the leaderboard. Asserted
   directly (zero `quiz_answers` rows) rather than by trusting a filter.

2. The run must be keyed on the ADMIN who tapped, not on the author of the
   message carrying the button. Every test therefore drives the real callback
   entry points (`cb_try_start` / `cb_try_answer` / `cb_try_stop`) with a message
   authored by the BOT — which is what production always hands them, since the
   button lives on a message the bot itself sent. An earlier version of these
   tests called `start_quiz_try` directly with an admin-authored fake message: a
   shape that never occurs in production, which is exactly how the identity bug
   slipped through green tests.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import func, select

from database.models import QuizAnswer
from handlers import quiz as quiz_handlers
from services import quiz_service

ADMIN_ID = 42
BOT_ID = 777  # the bot authors every message that carries a quiz_try button


class _FakeBot:
    async def send_message(self, *a, **kw):  # pragma: no cover - not exercised
        raise AssertionError("the test run must not send group messages")


class _FakeMessage:
    """A message as the bot sent it: `from_user` is the BOT, like in production."""

    def __init__(self, user_id=BOT_ID):
        self.bot = _FakeBot()
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=ADMIN_ID, type="private")
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


async def _start(message, session, quiz_id):
    """Enter the test run the way production does: a tap on the bot's own message."""
    await quiz_handlers.cb_try_start(_FakeCallback(f"quiz_try:start:{quiz_id}", message), session)


async def _answer_current(message, session, *, choice=0):
    """Tap one of the answer buttons the flow last offered. Returns the callback."""
    cbs = [c for c in _cbs(message.replies[-1][1]) if c.startswith("quiz_try:ans:")]
    assert cbs, "the test run should be offering answer buttons"
    cb = _FakeCallback(cbs[choice], message)
    await quiz_handlers.cb_try_answer(cb, session)
    return cb


def _try_state(quiz_id: int):
    return quiz_handlers._TRY.get((quiz_id, ADMIN_ID))


class TestRunIsKeyedOnTheAdmin:
    """Regression: the run used to be stored under `message.from_user.id`, which for
    a bot-sent message is the BOT — so the lookup on answer (keyed on the tapping
    admin) always missed and every answer was refused with "Prova scaduta"."""

    def teardown_method(self):
        quiz_handlers._TRY.clear()

    async def test_answering_right_after_start_is_accepted(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()

        await _start(message, session, quiz.id)
        cb = await _answer_current(message, session)

        assert not any("scaduta" in (a or "") for a in cb.answers)
        # The flow moved on to question 2 instead of stalling.
        assert "Domanda 2/2" in message.replies[-1][0]

    async def test_run_is_stored_under_the_admin_not_the_message_author(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()

        await _start(message, session, quiz.id)

        assert (quiz.id, ADMIN_ID) in quiz_handlers._TRY
        assert (quiz.id, BOT_ID) not in quiz_handlers._TRY

    async def test_stop_then_restart_then_answer(self, session):
        """The exact flow reported: exit the run, reopen it from the quiz, answer."""
        quiz = await _make_quiz(session)
        message = _FakeMessage()

        await _start(message, session, quiz.id)
        await quiz_handlers.cb_try_stop(_FakeCallback(f"quiz_try:stop:{quiz.id}", message))
        await _start(message, session, quiz.id)
        cb = await _answer_current(message, session)

        assert not any("scaduta" in (a or "") for a in cb.answers)
        assert await _answer_count(session, quiz.id) == 0

    async def test_stop_leaves_no_orphan_entry_at_all(self, session):
        """Not just "no entry for the admin": the whole map must be clean, or the
        run stored under the wrong key would leak until the process restarts."""
        quiz = await _make_quiz(session)
        message = _FakeMessage()

        await _start(message, session, quiz.id)
        await quiz_handlers.cb_try_stop(_FakeCallback(f"quiz_try:stop:{quiz.id}", message))

        assert quiz_handlers._TRY == {}

    async def test_a_second_admin_gets_an_independent_run(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()
        other_admin = 99

        await _start(message, session, quiz.id)
        await quiz_handlers.cb_try_start(
            _FakeCallback(f"quiz_try:start:{quiz.id}", message, user_id=other_admin), session
        )
        # Only the second admin answers; the first one's progress must not move.
        cbs = [c for c in _cbs(message.replies[-1][1]) if c.startswith("quiz_try:ans:")]
        await quiz_handlers.cb_try_answer(
            _FakeCallback(cbs[0], message, user_id=other_admin), session
        )

        assert quiz_handlers._TRY[(quiz.id, ADMIN_ID)].index == 0
        assert quiz_handlers._TRY[(quiz.id, other_admin)].index == 1


class TestRunLeavesNoTrace:
    def teardown_method(self):
        quiz_handlers._TRY.clear()

    async def test_full_run_records_nothing_in_db(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()

        await _start(message, session, quiz.id)
        for _ in range(2):
            await _answer_current(message, session)

        assert await _answer_count(session, quiz.id) == 0
        # ...and the quiz is still ready to be started for real.
        refreshed = await quiz_service.get_quiz(session, quiz.id)
        assert refreshed.status == "ready"

    async def test_run_does_not_reach_the_podium(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()

        await _start(message, session, quiz.id)
        for _ in range(2):
            await _answer_current(message, session)

        assert await quiz_service.podium(session, quiz.id) == []
        assert await quiz_service.answered_count(session, quiz.id, ADMIN_ID) == 0

    async def test_final_screen_reports_the_score(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()

        await _start(message, session, quiz.id)
        for _ in range(2):
            await _answer_current(message, session)

        final = message.replies[-1][0]
        assert "Prova completata" in final
        assert "Nessun dato è stato salvato" in final

    async def test_every_message_is_marked_as_a_test(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()

        await _start(message, session, quiz.id)

        assert "MODALITÀ PROVA" in message.replies[0][0]
        assert "🧪" in message.replies[1][0]


class TestRunGuards:
    def teardown_method(self):
        quiz_handlers._TRY.clear()

    async def test_refused_for_a_running_quiz(self, session):
        quiz = await _make_quiz(session, status="running")
        message = _FakeMessage()

        await _start(message, session, quiz.id)

        assert "solo per un quiz" in message.replies[-1][0]
        assert _try_state(quiz.id) is None

    async def test_stop_clears_the_run(self, session):
        quiz = await _make_quiz(session)
        message = _FakeMessage()

        await _start(message, session, quiz.id)
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
