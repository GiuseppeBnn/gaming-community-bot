"""Integration tests for services/quiz_service.py (private, podium model)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

import services.quiz_service as qz
from database.models import LedgerEntry, QuizAnswer, TransactionType, User, Wallet


async def _quiz(session, prize=0, n_questions=1, correct=0):
    quiz = await qz.create_quiz(session, 9, "Capitali", "Geo", prize)
    questions = []
    for i in range(n_questions):
        q = await qz.add_question(
            session, quiz.id, f"Domanda {i}", ["Roma", "Milano", "Napoli"],
            correct_option_id=correct, explanation="È Roma",
        )
        questions.append(q)
    await session.commit()
    return quiz, questions


class TestCreateAddQuestion:
    async def test_options_round_trip(self, session):
        _, qs = await _quiz(session)
        assert qz.question_options(qs[0]) == ["Roma", "Milano", "Napoli"]
        assert qs[0].position == 0
        assert qs[0].open_period == 0  # no timer

    async def test_positions_increment(self, session):
        _, qs = await _quiz(session, n_questions=3)
        assert [q.position for q in qs] == [0, 1, 2]

    async def test_time_limit_applied_to_questions(self, session):
        quiz = await qz.create_quiz(session, 9, "T", "d")
        q = await qz.add_question(
            session, quiz.id, "Q", ["a", "b"], correct_option_id=0,
            explanation=None, time_limit_seconds=45,
        )
        await session.commit()
        assert q.open_period == 45
        full = await qz.get_quiz(session, quiz.id)
        assert qz.time_limit_seconds(full) == 45

    async def test_time_limit_seconds_zero_without_questions(self, session):
        quiz = await qz.create_quiz(session, 9, "T", "d")
        await session.commit()
        full = await qz.get_quiz(session, quiz.id)
        assert qz.time_limit_seconds(full) == 0

    async def test_get_quiz_for_update_returns_quiz_with_questions(self, session):
        # The FOR UPDATE branch (used by close_quiz to serialize concurrent closes)
        # must still eager-load questions; the lock is a no-op on SQLite.
        quiz, qs = await _quiz(session, n_questions=2)
        got = await qz.get_quiz(session, quiz.id, for_update=True)
        assert got is not None and got.id == quiz.id
        assert len(got.questions) == len(qs)


class TestRecordAnswer:
    async def test_correct_and_dedup(self, session, user_factory):
        await user_factory(tg_id=1)
        quiz, qs = await _quiz(session, correct=0)
        out = await qz.record_answer(session, quiz.id, qs[0].id, 1, 0)
        await session.commit()
        assert out is not None and out.recorded is True and out.is_correct is True

        dup = await qz.record_answer(session, quiz.id, qs[0].id, 1, 2)
        assert dup.recorded is False  # already answered

        rows = (await session.execute(select(QuizAnswer))).scalars().all()
        assert len(rows) == 1 and rows[0].is_correct is True

    async def test_wrong_answer(self, session, user_factory):
        await user_factory(tg_id=2)
        quiz, qs = await _quiz(session, correct=0)
        out = await qz.record_answer(session, quiz.id, qs[0].id, 2, 2)
        await session.commit()
        assert out.recorded is True and out.is_correct is False
        assert out.correct_option_id == 0

    async def test_invalid_question(self, session):
        quiz, _ = await _quiz(session)
        assert await qz.record_answer(session, quiz.id, 99999, 1, 0) is None

    async def test_records_response_ms(self, session, user_factory):
        await user_factory(tg_id=3)
        quiz, qs = await _quiz(session, correct=0)
        await qz.record_answer(session, quiz.id, qs[0].id, 3, 0, response_ms=4200)
        await session.commit()
        row = (await session.execute(select(QuizAnswer))).scalars().one()
        assert row.response_ms == 4200

    async def test_timeout_sentinel_is_wrong(self, session, user_factory):
        # selected_option_id = -1 (no answer / timeout) is never correct.
        await user_factory(tg_id=4)
        quiz, qs = await _quiz(session, correct=0)
        out = await qz.record_answer(session, quiz.id, qs[0].id, 4, -1, response_ms=30000)
        await session.commit()
        assert out.recorded is True and out.is_correct is False


class TestProgress:
    async def test_answered_and_correct_counts(self, session, user_factory):
        await user_factory(tg_id=1)
        quiz, qs = await _quiz(session, n_questions=2, correct=0)
        await qz.record_answer(session, quiz.id, qs[0].id, 1, 0)  # correct
        await qz.record_answer(session, quiz.id, qs[1].id, 1, 1)  # wrong
        await session.commit()
        assert await qz.answered_count(session, quiz.id, 1) == 2
        assert await qz.correct_count(session, quiz.id, 1) == 1
        assert await qz.total_questions(session, quiz.id) == 2


class TestPodiumAndPrizes:
    async def _answer_all(self, session, quiz, qs, uid, n_correct, when=None):
        for i, q in enumerate(qs):
            selected = q.correct_option_id if i < n_correct else (q.correct_option_id + 1) % 3
            session.add(QuizAnswer(
                quiz_id=quiz.id, question_id=q.id, user_tg_id=uid,
                selected_option_id=selected, is_correct=(i < n_correct),
                response_ms=0, answered_at=when or datetime.utcnow(),
            ))
        await session.commit()

    async def test_podium_orders_by_correct_then_arrival(self, session):
        quiz, qs = await _quiz(session, n_questions=3, correct=0)
        t0 = datetime(2026, 5, 29, 12, 0, 0)
        # user 1: 3 correct, finishes later; user 2: 3 correct, finishes earlier; user 3: 2 correct.
        # response_ms is 0 for everyone here, so completion time ties → arrival (finished_at)
        # is the final tie-break and user 2 (earlier) beats user 1 on the 3-3 tie.
        await self._answer_all(session, quiz, qs, 1, 3, when=t0 + timedelta(minutes=5))
        await self._answer_all(session, quiz, qs, 2, 3, when=t0 + timedelta(minutes=1))
        await self._answer_all(session, quiz, qs, 3, 2, when=t0)
        board = await qz.podium(session, quiz.id)
        assert [r.user_tg_id for r in board] == [2, 1, 3]

    async def test_podium_tiebreak_by_completion_time(self, session):
        # Equal correct answers → the FASTER player (lower total response time) ranks
        # higher, even if they finished later in wall-clock arrival order.
        quiz, qs = await _quiz(session, n_questions=2, correct=0)
        t0 = datetime(2026, 5, 29, 12, 0, 0)
        for q in qs:
            # user 1: arrived first but slow (9s/question = 18s total)
            session.add(QuizAnswer(
                quiz_id=quiz.id, question_id=q.id, user_tg_id=1,
                selected_option_id=q.correct_option_id, is_correct=True,
                response_ms=9000, answered_at=t0,
            ))
            # user 2: arrived later but fast (1s/question = 2s total)
            session.add(QuizAnswer(
                quiz_id=quiz.id, question_id=q.id, user_tg_id=2,
                selected_option_id=q.correct_option_id, is_correct=True,
                response_ms=1000, answered_at=t0 + timedelta(minutes=5),
            ))
        await session.commit()
        board = await qz.podium(session, quiz.id)
        assert [r.user_tg_id for r in board] == [2, 1]  # faster on top despite later arrival
        assert board[0].completion_seconds == 2
        assert board[1].completion_seconds == 18

    async def test_completion_seconds_is_sum_of_response_times(self, session):
        # The completion time is the SUM of the user's per-question response times,
        # so it includes the first question (the 16s↔10s bug was dropping it).
        quiz, qs = await _quiz(session, n_questions=3, correct=0)
        quiz.started_at = datetime(2026, 1, 1, 0, 0, 0)
        for i, q in enumerate(qs):
            session.add(QuizAnswer(
                quiz_id=quiz.id, question_id=q.id, user_tg_id=1,
                selected_option_id=0, is_correct=True,
                response_ms=[6000, 6000, 4000][i],  # 6s + 6s + 4s = 16s
                answered_at=datetime(2026, 5, 1, 12, 0, 0),
            ))
        await session.commit()
        board = await qz.podium(session, quiz.id)
        assert len(board) == 1
        assert board[0].completion_seconds == 16
        assert await qz.user_completion_seconds(session, quiz.id, 1) == 16

    async def test_completion_seconds_shown_for_single_answer(self, session):
        # A single-question quiz now shows its time (the user's response_ms).
        quiz, qs = await _quiz(session, n_questions=1, correct=0)
        session.add(QuizAnswer(
            quiz_id=quiz.id, question_id=qs[0].id, user_tg_id=1,
            selected_option_id=0, is_correct=True, response_ms=12000,
            answered_at=datetime(2026, 5, 1, 12, 0, 0),
        ))
        await session.commit()
        board = await qz.podium(session, quiz.id)
        assert board[0].completion_seconds == 12
        assert await qz.user_completion_seconds(session, quiz.id, 1) == 12

    async def test_completion_seconds_none_without_answers(self, session):
        quiz, _ = await _quiz(session, n_questions=1, correct=0)
        assert await qz.user_completion_seconds(session, quiz.id, 1) is None

    async def test_podium_excludes_non_finishers(self, session):
        quiz, qs = await _quiz(session, n_questions=3, correct=0)
        # user 1 answered only 2/3 → not a finisher
        for q in qs[:2]:
            session.add(QuizAnswer(quiz_id=quiz.id, question_id=q.id, user_tg_id=1,
                                   selected_option_id=0, is_correct=True, response_ms=0))
        await session.commit()
        assert await qz.podium(session, quiz.id) == []

    async def test_award_prizes_split_top3(self, session, user_factory):
        for uid in (1, 2, 3):
            await user_factory(tg_id=uid, coins=0)
        quiz, qs = await _quiz(session, prize=1000, n_questions=2, correct=0)
        t0 = datetime(2026, 5, 29, 12, 0, 0)
        await self._answer_all(session, quiz, qs, 1, 2, when=t0 + timedelta(minutes=1))
        await self._answer_all(session, quiz, qs, 2, 2, when=t0 + timedelta(minutes=2))
        await self._answer_all(session, quiz, qs, 3, 1, when=t0 + timedelta(minutes=3))
        awards = await qz.award_prizes(session, quiz.id)
        await session.commit()

        coins = {uid: (await session.execute(select(Wallet.coins).where(Wallet.tg_id == uid))).scalar_one()
                 for uid in (1, 2, 3)}
        assert coins == {1: 500, 2: 300, 3: 200}  # 50/30/20 (1 fastest among 2-correct)
        assert sum(a.coins for a in awards) == 1000
        ledger = (await session.execute(select(LedgerEntry))).scalars().all()
        assert all(e.tx_type == TransactionType.quiz_reward.value for e in ledger)

    async def test_no_prize_when_zero_pool(self, session, user_factory):
        await user_factory(tg_id=1, coins=0)
        quiz, qs = await _quiz(session, prize=0, n_questions=1, correct=0)
        await self._answer_all(session, quiz, qs, 1, 1)
        assert await qz.award_prizes(session, quiz.id) == []


class TestQuizXp:
    """Event XP rewards participation, correctness, and the podium (note.txt)."""

    async def _answer_all(self, session, quiz, qs, uid, n_correct, when=None):
        for i, q in enumerate(qs):
            selected = q.correct_option_id if i < n_correct else (q.correct_option_id + 1) % 3
            session.add(QuizAnswer(
                quiz_id=quiz.id, question_id=q.id, user_tg_id=uid,
                selected_option_id=selected, is_correct=(i < n_correct),
                response_ms=1000, answered_at=when or datetime(2026, 5, 29, 12, 0, 0),
            ))
        await session.commit()

    async def _xp(self, session, uid):
        return (await session.execute(select(User.xp).where(User.tg_id == uid))).scalar_one()

    @staticmethod
    def _set_xp_settings(monkeypatch):
        from config_data.config import settings
        monkeypatch.setattr(settings, "quiz_xp_participation", 20)
        monkeypatch.setattr(settings, "quiz_xp_per_correct", 10)
        monkeypatch.setattr(settings, "quiz_xp_podium_first", 50)
        monkeypatch.setattr(settings, "quiz_xp_podium_second", 30)
        monkeypatch.setattr(settings, "quiz_xp_podium_third", 20)

    async def test_participation_correct_and_podium_bonus(self, session, user_factory, monkeypatch):
        self._set_xp_settings(monkeypatch)
        for uid in (1, 2, 3):
            await user_factory(tg_id=uid, xp=0)
        quiz, qs = await _quiz(session, n_questions=2, correct=0)
        t0 = datetime(2026, 5, 29, 12, 0, 0)
        await self._answer_all(session, quiz, qs, 1, 2, when=t0)                       # 1st
        await self._answer_all(session, quiz, qs, 2, 2, when=t0 + timedelta(minutes=1))  # 2nd (later)
        await self._answer_all(session, quiz, qs, 3, 0, when=t0 + timedelta(minutes=2))  # 3rd, 0 correct
        await qz.award_prizes(session, quiz.id)
        await session.commit()
        # 1st: 20 + 2*10 + 50 = 90 ; 2nd: 20 + 2*10 + 30 = 70 ; 3rd: 20 + 0 + 20 = 40
        assert await self._xp(session, 1) == 90
        assert await self._xp(session, 2) == 70
        assert await self._xp(session, 3) == 40

    async def test_participation_xp_even_without_finishing(self, session, user_factory, monkeypatch):
        self._set_xp_settings(monkeypatch)
        await user_factory(tg_id=1, xp=0)
        quiz, qs = await _quiz(session, n_questions=3, correct=0)
        # Answers only 1 of 3 questions → not a finisher (no podium, no prize) but participated.
        session.add(QuizAnswer(
            quiz_id=quiz.id, question_id=qs[0].id, user_tg_id=1,
            selected_option_id=qs[0].correct_option_id, is_correct=True, response_ms=0,
        ))
        await session.commit()
        awards = await qz.award_prizes(session, quiz.id)
        await session.commit()
        assert awards == []                       # not a finisher → no coin prize
        assert await self._xp(session, 1) == 30   # 20 participation + 10 for the one correct


async def _quiz_explicit(
    session, *, first=0, second=0, third=0, consolation=0, prize_min=0, n_questions=2
):
    quiz = await qz.create_quiz(
        session, 9, "Capitali", "Geo", 0,
        prize_first=first, prize_second=second, prize_third=third,
        prize_consolation=consolation, prize_min=prize_min,
    )
    qs = []
    for i in range(n_questions):
        q = await qz.add_question(
            session, quiz.id, f"Domanda {i}", ["Roma", "Milano", "Napoli"],
            correct_option_id=0, explanation=None,
        )
        qs.append(q)
    await session.commit()
    return quiz, qs


class TestExplicitPrizes:
    async def _finish(self, session, quiz, qs, uid, when, n_correct=None):
        n_correct = len(qs) if n_correct is None else n_correct
        for i, q in enumerate(qs):
            selected = q.correct_option_id if i < n_correct else (q.correct_option_id + 1) % 3
            session.add(QuizAnswer(
                quiz_id=quiz.id, question_id=q.id, user_tg_id=uid,
                selected_option_id=selected, is_correct=(i < n_correct),
                response_ms=0, answered_at=when,
            ))
        await session.commit()

    async def _coins(self, session, uid):
        return (await session.execute(
            select(Wallet.coins).where(Wallet.tg_id == uid)
        )).scalar_one()

    async def test_columns_persisted(self, session):
        quiz, _ = await _quiz_explicit(
            session, first=1000, second=500, third=250, consolation=100, prize_min=20
        )
        reloaded = await qz.get_quiz(session, quiz.id)
        assert (reloaded.prize_first, reloaded.prize_second, reloaded.prize_third) == (1000, 500, 250)
        assert reloaded.prize_consolation == 100 and reloaded.prize_min == 20
        assert qz.has_explicit_prizes(reloaded) is True

    async def test_podium_gets_explicit_amounts(self, session, user_factory):
        for uid in (1, 2, 3):
            await user_factory(tg_id=uid, coins=0)
        quiz, qs = await _quiz_explicit(session, first=1000, second=500, third=250)
        t0 = datetime(2026, 5, 29, 12, 0, 0)
        for uid in (1, 2, 3):
            await self._finish(session, quiz, qs, uid, t0 + timedelta(minutes=uid))
        await qz.award_prizes(session, quiz.id)
        await session.commit()
        assert {uid: await self._coins(session, uid) for uid in (1, 2, 3)} == {1: 1000, 2: 500, 3: 250}

    async def test_consolation_decreasing_with_floor(self, session, user_factory):
        for uid in range(1, 7):
            await user_factory(tg_id=uid, coins=0)
        quiz, qs = await _quiz_explicit(
            session, first=1000, second=500, third=250, consolation=100, prize_min=20
        )
        t0 = datetime(2026, 5, 29, 12, 0, 0)
        for uid in range(1, 7):  # all correct → ranked by arrival (uid order)
            await self._finish(session, quiz, qs, uid, t0 + timedelta(minutes=uid))
        await qz.award_prizes(session, quiz.id)
        await session.commit()
        # 4th, 5th, 6th get the linear consolation schedule [100, 60, 20]
        assert await self._coins(session, 4) == 100
        assert await self._coins(session, 5) == 60
        assert await self._coins(session, 6) == 20
        # podium still gets the explicit amounts
        assert await self._coins(session, 1) == 1000

    async def test_single_non_podium_gets_consolation_top(self, session, user_factory):
        for uid in range(1, 5):
            await user_factory(tg_id=uid, coins=0)
        quiz, qs = await _quiz_explicit(session, first=1000, consolation=100, prize_min=20)
        t0 = datetime(2026, 5, 29, 12, 0, 0)
        for uid in range(1, 5):
            await self._finish(session, quiz, qs, uid, t0 + timedelta(minutes=uid))
        await qz.award_prizes(session, quiz.id)
        await session.commit()
        assert await self._coins(session, 4) == 100  # sole non-podium finisher → 4th-place value

    async def test_non_finishers_excluded(self, session, user_factory):
        for uid in (1, 2):
            await user_factory(tg_id=uid, coins=0)
        quiz, qs = await _quiz_explicit(session, first=1000, consolation=100, prize_min=20, n_questions=3)
        t0 = datetime(2026, 5, 29, 12, 0, 0)
        await self._finish(session, quiz, qs, 1, t0 + timedelta(minutes=1))  # finisher
        # user 2 answers only 2/3 → not a finisher, gets no prize
        for q in qs[:2]:
            session.add(QuizAnswer(quiz_id=quiz.id, question_id=q.id, user_tg_id=2,
                                   selected_option_id=0, is_correct=True, response_ms=0,
                                   answered_at=t0))
        await session.commit()
        await qz.award_prizes(session, quiz.id)
        await session.commit()
        assert await self._coins(session, 1) == 1000
        assert await self._coins(session, 2) == 0

    async def test_zero_consolation_pays_only_podium(self, session, user_factory):
        for uid in range(1, 5):
            await user_factory(tg_id=uid, coins=0)
        quiz, qs = await _quiz_explicit(session, first=1000, consolation=0, prize_min=0)
        t0 = datetime(2026, 5, 29, 12, 0, 0)
        for uid in range(1, 5):
            await self._finish(session, quiz, qs, uid, t0 + timedelta(minutes=uid))
        awards = await qz.award_prizes(session, quiz.id)
        await session.commit()
        assert await self._coins(session, 1) == 1000
        assert await self._coins(session, 4) == 0
        assert all(a.kind == "podium" for a in awards)


class TestCloseQuizTriviaEvents:
    """close_quiz wiring: it records the new Trivia progress events (last place /
    completed under 30 s) for the trophy engine, beyond the top-3 podium."""

    async def _answer(self, session, quiz, q, uid, response_ms, correct=True):
        await qz.record_answer(
            session, quiz.id, q.id, uid,
            selected_option_id=(0 if correct else 1), response_ms=response_ms,
        )

    async def test_records_last_place_and_sub30(self, session, user_factory):
        from unittest.mock import AsyncMock

        from handlers.quiz import close_quiz
        from services import group_registry, progress_service

        group_registry.set_runtime_group_id(0)  # no group → no broadcast
        try:
            for uid in (1, 2):
                await user_factory(tg_id=uid, coins=0)
            quiz, qs = await _quiz(session, n_questions=1, correct=0)
            await self._answer(session, quiz, qs[0], 1, response_ms=1_000)   # fast → 1st
            await self._answer(session, quiz, qs[0], 2, response_ms=2_000)   # slower → last
            await qz.set_status(session, quiz.id, "running")
            await session.commit()

            ok, _ = await close_quiz(AsyncMock(), session, quiz.id)
            assert ok

            c1 = await progress_service.event_counts(session, 1)
            c2 = await progress_service.event_counts(session, 2)
            # Both finished under 30 s; only the runner-up is "last place".
            assert c1.get(progress_service.TRIVIA_SUB30) == 1
            assert progress_service.TRIVIA_LAST_PLACE not in c1
            assert c2.get(progress_service.TRIVIA_SUB30) == 1
            assert c2.get(progress_service.TRIVIA_LAST_PLACE) == 1
        finally:
            group_registry.set_runtime_group_id(None)

    async def test_single_finisher_is_not_last_place(self, session, user_factory):
        from unittest.mock import AsyncMock

        from handlers.quiz import close_quiz
        from services import group_registry, progress_service

        group_registry.set_runtime_group_id(0)
        try:
            await user_factory(tg_id=1, coins=0)
            quiz, qs = await _quiz(session, n_questions=1, correct=0)
            await self._answer(session, quiz, qs[0], 1, response_ms=45_000)  # over 30 s
            await qz.set_status(session, quiz.id, "running")
            await session.commit()

            ok, _ = await close_quiz(AsyncMock(), session, quiz.id)
            assert ok
            counts = await progress_service.event_counts(session, 1)
            # Solo player: not "last", and 45 s is not under 30 s → no events.
            assert counts == {}
        finally:
            group_registry.set_runtime_group_id(None)
