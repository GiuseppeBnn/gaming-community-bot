"""Durable native-quiz delivery and exactly-once answer admission.

Services flush, callers commit. Lock order is always Quiz -> QuizPoll; the same
quiz lock serializes admission against close/reset. No Telegram calls live here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Quiz, QuizPoll
from services import group_registry, quiz_service, schedule_service

DELIVERY_SECONDS = 60
EXPIRY_GRACE_SECONDS = 3


@dataclass(frozen=True)
class Delivery:
    quiz_id: int
    question_id: int
    user_tg_id: int
    token: str
    state: str
    option_order_json: str
    chat_id: int
    message_id: int | None
    tg_poll_id: str | None
    shown_at: datetime
    expires_at: datetime | None
    lease_until: datetime

    @property
    def option_order(self) -> list[int]:
        return json.loads(self.option_order_json)


async def get(session: AsyncSession, **key) -> Delivery | None:
    """Explicit columns: a stale ORM identity map can never authorize a vote."""
    row = (
        (
            await session.execute(
                select(
                    *(getattr(QuizPoll, name) for name in Delivery.__dataclass_fields__)
                ).filter_by(**key)
            )
        )
        .mappings()
        .one_or_none()
    )
    return Delivery(**row) if row is not None else None


async def _running(session: AsyncSession, quiz_id: int) -> bool:
    return (
        await session.execute(select(Quiz.status).where(Quiz.id == quiz_id).with_for_update())
    ).scalar_one_or_none() == "running"


async def claim(
    session: AsyncSession,
    quiz,
    user_id: int,
    chat_id: int,
) -> tuple[Delivery | None, bool]:
    """Return (delivery, owned_send). Resumes neither reshuffle nor reset clocks."""
    if chat_id != user_id or not await _running(session, quiz.id):
        return None, False
    done = await quiz_service.answered_count(session, quiz.id, user_id)
    order = quiz_service.user_question_order(quiz, user_id)
    if done >= len(order):
        return None, False
    question = order[done]
    old = await get(session, question_id=question.id, user_tg_id=user_id)
    now = schedule_service.utcnow()
    if old is not None and (old.state != "sending" or old.lease_until > now):
        return old, False
    values = dict(
        token=uuid4().hex,
        state="sending",
        shown_at=now,
        lease_until=now + timedelta(seconds=DELIVERY_SECONDS),
        option_order_json=json.dumps(
            [real_id for real_id, _ in quiz_service.user_option_order(quiz, question, user_id)]
        ),
    )
    if old is None:
        session.add(
            QuizPoll(
                quiz_id=quiz.id,
                question_id=question.id,
                user_tg_id=user_id,
                chat_id=chat_id,
                **values,
            )
        )
        await session.flush()
    else:
        await session.execute(
            update(QuizPoll)
            .where(QuizPoll.token == old.token, QuizPoll.state == "sending")
            .values(**values)
            .execution_options(synchronize_session=False)
        )
    return await get(session, token=values["token"]), True


async def publish(
    session: AsyncSession,
    delivery: Delivery,
    *,
    poll_id: str,
    message_id: int,
    shown_at: datetime,
    expires_at: datetime | None,
) -> bool:
    """Attach the send result only if this delivery still owns the current run."""
    if not await _running(session, delivery.quiz_id):
        return False
    result = await session.execute(
        update(QuizPoll)
        .where(QuizPoll.token == delivery.token, QuizPoll.state == "sending")
        .values(
            state="open",
            tg_poll_id=poll_id,
            message_id=message_id,
            shown_at=shown_at,
            expires_at=expires_at,
        )
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return False
    if expires_at is not None:
        await schedule_service.schedule_task(
            session,
            "quiz",
            expires_at + timedelta(seconds=EXPIRY_GRACE_SECONDS),
            created_by_tg_id=0,
            group_id=group_registry.get_group_id() or None,
            ref_id=delivery.quiz_id,
            payload={"action": "expire", "internal": True, "poll_token": delivery.token},
        )
    return True


async def release_rejected(session: AsyncSession, token: str) -> None:
    """Only definite Telegram rejections may be retried immediately."""
    await session.execute(
        delete(QuizPoll)
        .where(QuizPoll.token == token, QuizPoll.state == "sending")
        .execution_options(synchronize_session=False)
    )


async def answer(
    session: AsyncSession,
    delivery: Delivery,
    option_id: int | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Consume one current delivery and reuse the existing answer/score recorder.

    None means timeout, accepted only after the original deadline. Telegram
    enforces the vote deadline; queued vote updates get a small expiry grace.
    """
    if not await _running(session, delivery.quiz_id):
        return False
    current = await get(session, token=delivery.token)
    if current is None or current.state != "open":
        return False
    now = now or schedule_service.utcnow()
    quiz = await quiz_service.get_quiz(session, current.quiz_id)
    assert quiz is not None  # the parent row is locked by _running
    order = quiz_service.user_question_order(quiz, current.user_tg_id)
    done = await quiz_service.answered_count(session, quiz.id, current.user_tg_id)
    if done >= len(order) or order[done].id != current.question_id:
        return False
    if option_id is None:
        if current.expires_at is None or now < current.expires_at:
            return False
        selected = -1
    elif 0 <= option_id < len(current.option_order):
        selected = current.option_order[option_id]
    else:
        return False
    ms = max(0, round((now - current.shown_at).total_seconds() * 1000))
    if current.expires_at is not None:
        limit_ms = order[done].open_period * 1000
        ms = limit_ms if option_id is None else min(ms, limit_ms)
    result = await session.execute(
        update(QuizPoll)
        .where(QuizPoll.token == current.token, QuizPoll.state == "open")
        .values(state="answered")
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return False
    outcome = await quiz_service.record_answer(
        session,
        quiz.id,
        current.question_id,
        current.user_tg_id,
        selected,
        response_ms=ms,
    )
    return outcome is not None and outcome.recorded
