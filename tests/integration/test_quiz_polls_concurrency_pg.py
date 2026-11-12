"""Real SQL races for private quiz delivery, answers and published podiums."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from database.models import QuizAnswer, QuizPoll
from services import quiz_poll_service as polls, quiz_service, schedule_service
from tests.quiz_support import make_quiz

pytestmark = pytest.mark.pg


async def setup(pg_sessions, *, expired=False):
    async with pg_sessions() as session:
        quiz = await make_quiz(session, count=1, limit=5)
        delivery, _ = await polls.claim(session, quiz, 42, 42)
        await session.commit()
        stamp = schedule_service.utcnow() - timedelta(seconds=10 if expired else 0)
        await polls.publish(
            session,
            delivery,
            poll_id="native-pg",
            message_id=1,
            shown_at=stamp,
            expires_at=stamp + timedelta(seconds=5),
        )
        await session.commit()
        return quiz.id, delivery


async def test_simultaneous_starts_send_only_one_poll(pg_sessions):
    async with pg_sessions() as session:
        quiz = await make_quiz(session, count=1)
        quiz_id = quiz.id

    async def claim():
        async with pg_sessions() as session:
            quiz = await quiz_service.get_quiz(session, quiz_id)
            delivery, owned = await polls.claim(session, quiz, 42, 42)
            await session.commit()
            return delivery.token, owned

    claims = await asyncio.wait_for(asyncio.gather(*(claim() for _ in range(6))), timeout=10)
    assert sum(owned for _, owned in claims) == 1
    assert len({token for token, _ in claims}) == 1
    async with pg_sessions() as session:
        assert (await session.execute(select(func.count()).select_from(QuizPoll))).scalar_one() == 1


async def test_duplicate_votes_and_expiry_admit_exactly_one_answer(pg_sessions):
    quiz_id, delivery = await setup(pg_sessions, expired=True)

    async def consume(choice):
        async with pg_sessions() as session:
            admitted = await polls.answer(session, delivery, choice)
            await session.commit()
            return admitted

    admitted = await asyncio.wait_for(
        asyncio.gather(consume(0), consume(0), consume(None), consume(None)),
        timeout=10,
    )
    assert sum(admitted) == 1
    async with pg_sessions() as session:
        assert await quiz_service.answered_count(session, quiz_id, 42) == 1
        assert (await session.execute(select(QuizPoll.state))).scalar_one() == "answered"


async def test_no_answer_can_land_after_the_podium_closes(pg_sessions):
    quiz_id, delivery = await setup(pg_sessions)

    async def vote():
        async with pg_sessions() as session:
            admitted = await polls.answer(session, delivery, 0)
            await session.commit()
            return admitted

    async def close():
        async with pg_sessions() as session:
            assert await quiz_service.claim_close(session, quiz_id) is None
            # The exact podium snapshot paid by the closer must remain unchanged.
            podium_count = await quiz_service.answered_count(session, quiz_id, 42)
            await session.commit()
            return podium_count

    admitted, paid_count = await asyncio.wait_for(asyncio.gather(vote(), close()), timeout=10)
    assert int(admitted) == paid_count
    async with pg_sessions() as session:
        assert not await polls.answer(session, delivery, 0)
        assert await quiz_service.answered_count(session, quiz_id, 42) == paid_count


async def test_stale_identity_maps_do_not_authorize_a_second_vote(pg_sessions):
    quiz_id, delivery = await setup(pg_sessions)
    async with pg_sessions() as stale:
        held_poll = (await stale.execute(select(QuizPoll))).scalar_one()
        held_quiz = await quiz_service.get_quiz(stale, quiz_id)
        async with pg_sessions() as winner:
            assert await polls.answer(winner, delivery, 0)
            await winner.commit()
        assert held_poll.state == "open" and held_quiz.status == "running"
        assert not await polls.answer(stale, delivery, 1)
        await stale.commit()
    async with pg_sessions() as session:
        stored = (await session.execute(select(QuizAnswer))).scalar_one()
        assert stored.selected_option_id == 0 and stored.is_correct


async def test_reset_revokes_inflight_send_and_all_old_poll_tokens(pg_sessions):
    quiz_id, delivery = await setup(pg_sessions)
    async with pg_sessions() as session:
        await quiz_service.claim_close(session, quiz_id)
        assert await quiz_service.reset_quiz(session, quiz_id)
        await quiz_service.set_status(session, quiz_id, "running")
        await session.commit()
    async with pg_sessions() as old_sender:
        assert not await polls.publish(
            old_sender,
            delivery,
            poll_id="late",
            message_id=2,
            shown_at=schedule_service.utcnow(),
            expires_at=None,
        )
        assert not await polls.answer(old_sender, delivery, 0)
        await old_sender.commit()
    async with pg_sessions() as session:
        quiz = await quiz_service.get_quiz(session, quiz_id)
        fresh, owned = await polls.claim(session, quiz, 42, 42)
        assert owned and fresh.token != delivery.token
        assert await quiz_service.answered_count(session, quiz_id, 42) == 0


async def test_delete_and_vote_use_same_lock_order_without_deadlock(pg_sessions):
    quiz_id, delivery = await setup(pg_sessions)

    async def vote():
        async with pg_sessions() as session:
            await polls.answer(session, delivery, 0)
            await session.commit()

    async def remove():
        async with pg_sessions() as session:
            assert await quiz_service.delete_quiz(session, quiz_id)
            await session.commit()

    await asyncio.wait_for(asyncio.gather(vote(), remove()), timeout=10)
    async with pg_sessions() as session:
        assert await quiz_service.get_quiz(session, quiz_id) is None
        assert (await session.execute(select(func.count()).select_from(QuizPoll))).scalar_one() == 0
        assert await quiz_service.answered_count(session, quiz_id, 42) == 0
