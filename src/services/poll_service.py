"""
Poll templates — pre-created polls (question + options) an admin can start in the
group immediately or schedule, mirroring how quizzes are pre-created.

No-commit convention: callers own the transaction (STEERING §5).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import PollTemplate


def _now() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


async def create_template(
    session: AsyncSession,
    creator_tg_id: int,
    question: str,
    options: list[str],
    group_id: int | None = None,
) -> PollTemplate:
    poll = PollTemplate(
        question=question[:300],
        options_json=json.dumps(options, ensure_ascii=False),
        creator_tg_id=creator_tg_id,
        status="ready",
        group_id=group_id,
    )
    session.add(poll)
    await session.flush()
    return poll


def options_of(poll: PollTemplate) -> list[str]:
    try:
        data = json.loads(poll.options_json)
        return [str(o) for o in data] if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


async def get(session: AsyncSession, poll_id: int) -> PollTemplate | None:
    return (
        await session.execute(select(PollTemplate).where(PollTemplate.id == poll_id))
    ).scalar_one_or_none()


async def list_ready(session: AsyncSession) -> list[PollTemplate]:
    result = await session.execute(
        select(PollTemplate)
        .where(PollTemplate.status == "ready")
        .order_by(PollTemplate.created_at.desc())
    )
    return list(result.scalars().all())


async def mark_used(session: AsyncSession, poll_id: int) -> None:
    poll = await get(session, poll_id)
    if poll is not None:
        poll.status = "used"
        poll.used_at = _now()
