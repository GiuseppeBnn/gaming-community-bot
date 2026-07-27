"""Playing a quiz in private — `start_quiz_session` and `cb_quiz_answer`.

This is the flow every participant actually sees, and it is the one place in the bot
where **order is enforced**: a player may only answer their current question, in the
order that was shuffled for them, and only once. Everything downstream depends on
that — the podium ranks by correct answers and then by the time each player took, so
an answer accepted out of order, or twice, corrupts a ranking that pays out coins.

`tests/unit/test_quiz_timer.py` covers the countdown in isolation. What is covered
here is the interactive path: what a player is allowed to do, what they are told, and
what ends up recorded.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy import func, select

from database.models import QuizAnswer
from handlers.quiz import play as qz
from services import quiz_service

PLAYER = 10
ADMIN_ID = 1


class _FakeBot:
    id = 999_999

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.edits: list[str] = []
        self._next_id = 100

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.messages.append((chat_id, text))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.edits.append(text)

    async def get_me(self):
        return SimpleNamespace(username="testbot")

    @property
    def said(self) -> str:
        return "\n".join(t for _, t in self.messages)


class _FakeMessage:
    def __init__(self, bot=None, user_id: int = PLAYER) -> None:
        self.bot = bot or _FakeBot()
        self.from_user = SimpleNamespace(id=user_id, username=f"u{user_id}",
                                         full_name=f"User {user_id}")
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.texts: list[str] = []
        self.message_id = 55

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        return SimpleNamespace(message_id=len(self.texts))

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)

    @property
    def said(self) -> str:
        return "\n".join(self.texts)


class _FakeCallback:
    def __init__(self, data: str, *, bot=None, user_id: int = PLAYER) -> None:
        self.data = data
        self.message = _FakeMessage(bot=bot, user_id=user_id)
        self.bot = self.message.bot
        self.from_user = SimpleNamespace(id=user_id, username=f"u{user_id}",
                                         full_name=f"User {user_id}")
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    @property
    def said(self) -> str:
        return "\n".join(t for t, _ in self.answers if t)


async def _running_quiz(session, user_factory, *, n_questions: int = 2, time_limit: int = 0):
    await user_factory(tg_id=ADMIN_ID, username="admin")
    await user_factory(tg_id=PLAYER, username=f"u{PLAYER}", coins=0)
    quiz = await quiz_service.create_quiz(session, ADMIN_ID, "Capitali", "Geo")
    questions = []
    for i in range(n_questions):
        questions.append(
            await quiz_service.add_question(
                session, quiz.id, f"Domanda {i}?", ["Giusta", "Sbagliata"],
                correct_option_id=0, explanation="Perché sì" if i == 0 else None,
                time_limit_seconds=time_limit,
            )
        )
    await quiz_service.set_status(session, quiz.id, "running")
    await session.commit()
    return quiz, questions


async def _answers(session, quiz_id: int, tg_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(QuizAnswer).where(
                QuizAnswer.quiz_id == quiz_id, QuizAnswer.user_tg_id == tg_id
            )
        )
    ).scalar_one()


class TestStartingASession:
    async def test_starting_sends_the_rules_and_the_first_question(
        self, session, user_factory
    ):
        quiz, _ = await _running_quiz(session, user_factory)
        message = _FakeMessage()

        await qz.start_quiz_session(message, session, quiz.id)

        assert "Capitali" in message.said
        assert "senza limite" in message.said or "Nessun limite" in message.said
        assert "Domanda 1/2" in message.bot.said

    async def test_a_timed_quiz_announces_the_limit(self, session, user_factory):
        quiz, _ = await _running_quiz(session, user_factory, time_limit=30)
        message = _FakeMessage()

        await qz.start_quiz_session(message, session, quiz.id)

        assert "30 second" in message.said
        qz._forget_play(quiz.id, PLAYER)  # stop the countdown this armed

    async def test_a_quiz_that_is_not_running_cannot_be_played(
        self, session, user_factory
    ):
        quiz, _ = await _running_quiz(session, user_factory)

        await quiz_service.set_status(session, quiz.id, "finished")
        await session.commit()
        finished = _FakeMessage()
        await qz.start_quiz_session(finished, session, quiz.id)
        assert "già terminato" in finished.said
        assert finished.bot.messages == []

        await quiz_service.set_status(session, quiz.id, "ready")
        await session.commit()
        not_started = _FakeMessage()
        await qz.start_quiz_session(not_started, session, quiz.id)
        assert "non è ancora iniziato" in not_started.said

    async def test_a_missing_quiz_is_reported(self, session):
        message = _FakeMessage()
        await qz.start_quiz_session(message, session, 999999)
        assert "non trovato" in message.said

    async def test_resuming_picks_up_where_the_player_left_off(
        self, session, user_factory
    ):
        """Deep links are re-openable and the bot restarts; a resume must not replay
        question 1, which the player has already answered."""
        quiz, questions = await _running_quiz(session, user_factory, n_questions=3)
        await quiz_service.record_answer(session, quiz.id, questions[0].id, PLAYER, 0)
        await session.commit()

        message = _FakeMessage()
        await qz.start_quiz_session(message, session, quiz.id)

        assert "Domanda 2/3" in message.bot.said

    async def test_a_player_who_finished_is_told_their_score_not_replayed(
        self, session, user_factory
    ):
        quiz, questions = await _running_quiz(session, user_factory)
        for q in questions:
            await quiz_service.record_answer(session, quiz.id, q.id, PLAYER, 0)
        await session.commit()

        message = _FakeMessage()
        await qz.start_quiz_session(message, session, quiz.id)

        assert "2/2" in message.said
        assert message.bot.messages == [], "a completed quiz was served again"


class TestAnswering:
    async def _start(self, session, user_factory, **kw):
        quiz, questions = await _running_quiz(session, user_factory, **kw)
        await qz.start_quiz_session(_FakeMessage(), session, quiz.id)
        return quiz, questions

    async def test_a_correct_answer_is_recorded_and_confirmed(
        self, session, user_factory
    ):
        quiz, questions = await self._start(session, user_factory)
        cb = _FakeCallback(f"quiz_ans:{quiz.id}:{questions[0].id}:0")

        await qz.cb_quiz_answer(cb, session)

        assert await _answers(session, quiz.id, PLAYER) == 1
        assert "Esatto" in cb.message.said
        assert "Perché sì" in cb.message.said, "the explanation was not shown"

    async def test_a_wrong_answer_names_the_right_one(self, session, user_factory):
        quiz, questions = await self._start(session, user_factory)
        cb = _FakeCallback(f"quiz_ans:{quiz.id}:{questions[0].id}:1")

        await qz.cb_quiz_answer(cb, session)

        assert "Sbagliato" in cb.message.said
        assert "Giusta" in cb.message.said

    async def test_answering_the_same_question_twice_is_refused(
        self, session, user_factory
    ):
        """The podium ranks by correct answers, so a second answer to the same
        question is a second chance nobody else gets."""
        quiz, questions = await self._start(session, user_factory)
        data = f"quiz_ans:{quiz.id}:{questions[0].id}:0"

        await qz.cb_quiz_answer(_FakeCallback(data), session)
        second = _FakeCallback(data)
        await qz.cb_quiz_answer(second, session)

        assert await _answers(session, quiz.id, PLAYER) == 1
        assert "già risposto" in second.said

    async def test_skipping_ahead_is_refused(self, session, user_factory):
        """Only the current question is answerable. Without this a player could open
        the second question's keyboard from an older message and answer out of order,
        which breaks both the sequence and the per-question timing."""
        quiz, questions = await self._start(session, user_factory)
        cb = _FakeCallback(f"quiz_ans:{quiz.id}:{questions[1].id}:0")

        await qz.cb_quiz_answer(cb, session)

        assert await _answers(session, quiz.id, PLAYER) == 0
        assert "Rispondi prima" in cb.said

    async def test_finishing_sends_the_wrap_up_with_the_score(
        self, session, user_factory
    ):
        quiz, questions = await self._start(session, user_factory)

        await qz.cb_quiz_answer(
            _FakeCallback(f"quiz_ans:{quiz.id}:{questions[0].id}:0"), session
        )
        last = _FakeCallback(f"quiz_ans:{quiz.id}:{questions[1].id}:1")
        await qz.cb_quiz_answer(last, session)

        assert await _answers(session, quiz.id, PLAYER) == 2
        assert "Quiz completato" in last.message.bot.said
        assert "1/2" in last.message.bot.said

    async def test_a_malformed_callback_is_refused(self, session, user_factory):
        await _running_quiz(session, user_factory)

        for data in ("quiz_ans:", "quiz_ans:a:b:c", "quiz_ans:1:2"):
            cb = _FakeCallback(data)
            await qz.cb_quiz_answer(cb, session)
            assert "non validi" in cb.said, data

    async def test_answering_a_closed_quiz_is_refused(self, session, user_factory):
        """The buttons stay in the chat after the admin closes the quiz."""
        quiz, questions = await self._start(session, user_factory)
        await quiz_service.set_status(session, quiz.id, "finished")
        await session.commit()

        cb = _FakeCallback(f"quiz_ans:{quiz.id}:{questions[0].id}:0")
        await qz.cb_quiz_answer(cb, session)

        assert await _answers(session, quiz.id, PLAYER) == 0
        assert "non è più disponibile" in cb.said

    async def test_an_unknown_question_or_option_is_refused(
        self, session, user_factory
    ):
        quiz, questions = await self._start(session, user_factory)

        ghost = _FakeCallback(f"quiz_ans:{quiz.id}:999999:0")
        await qz.cb_quiz_answer(ghost, session)
        assert "Domanda non valida" in ghost.said

        out_of_range = _FakeCallback(f"quiz_ans:{quiz.id}:{questions[0].id}:9")
        await qz.cb_quiz_answer(out_of_range, session)
        assert "Opzione non valida" in out_of_range.said

        assert await _answers(session, quiz.id, PLAYER) == 0

    async def test_an_uneditable_message_still_gets_the_feedback(
        self, session, user_factory
    ):
        """Telegram refuses edits on messages older than 48h. The player must still be
        told whether they were right, as a new message."""
        quiz, questions = await self._start(session, user_factory)
        cb = _FakeCallback(f"quiz_ans:{quiz.id}:{questions[0].id}:0")

        async def refuse_edit(text, reply_markup=None, **kw):
            raise RuntimeError("Bad Request: message can't be edited")

        cb.message.edit_text = refuse_edit

        await qz.cb_quiz_answer(cb, session)

        assert await _answers(session, quiz.id, PLAYER) == 1
        assert "Esatto" in cb.message.said


class TestTimingAndCleanup:
    async def test_answering_stops_the_countdown(self, session, user_factory):
        """If the timer survived the answer it would fire later, find the question
        already answered, and — depending on the ordering — mark the *next* one wrong.
        """
        quiz, questions = await _running_quiz(session, user_factory, time_limit=300)
        await qz.start_quiz_session(_FakeMessage(), session, quiz.id)
        ctx = qz._PLAY[qz._play_key(quiz.id, PLAYER)]
        assert ctx.timer is not None and not ctx.timer.done()

        await qz.cb_quiz_answer(
            _FakeCallback(f"quiz_ans:{quiz.id}:{questions[0].id}:0"), session
        )
        await asyncio.sleep(0)  # let the cancellation land

        assert ctx.timer.cancelled() or ctx.timer.done()
        qz._forget_play(quiz.id, PLAYER)

    async def test_the_response_time_is_recorded_and_capped_by_the_limit(
        self, session, user_factory
    ):
        """Completion time is the podium's tie-break, so it has to be measured — and
        capped at the limit, or a player whose client was slow to deliver the
        expiry would rank below someone who actually took longer."""
        quiz, questions = await _running_quiz(session, user_factory, time_limit=5)
        await qz.start_quiz_session(_FakeMessage(), session, quiz.id)

        ctx = qz._PLAY[qz._play_key(quiz.id, PLAYER)]
        ctx.shown_at -= 3600  # pretend an hour passed

        ms = qz._response_ms(quiz.id, PLAYER, questions[0].id, 5)
        assert ms == 5000, "the response time was not capped at the limit"

        qz._forget_play(quiz.id, PLAYER)

    async def test_the_response_time_is_zero_without_a_context(self, session, user_factory):
        """After a restart the in-memory context is gone. Zero is the honest answer;
        anything else would invent a ranking."""
        quiz, questions = await _running_quiz(session, user_factory)

        assert qz._response_ms(quiz.id, PLAYER, questions[0].id, 0) == 0

    async def test_finishing_forgets_the_play_context(self, session, user_factory):
        """The context dict is process-global, so anything left in it is a leak that
        grows with every quiz played."""
        quiz, questions = await _running_quiz(session, user_factory)
        await qz.start_quiz_session(_FakeMessage(), session, quiz.id)
        assert qz._play_key(quiz.id, PLAYER) in qz._PLAY

        for q in questions:
            await qz.cb_quiz_answer(
                _FakeCallback(f"quiz_ans:{quiz.id}:{q.id}:0"), session
            )

        assert qz._play_key(quiz.id, PLAYER) not in qz._PLAY
