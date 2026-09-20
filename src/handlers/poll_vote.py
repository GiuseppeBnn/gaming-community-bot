"""Tracks votes on a rewarded/closable poll via ``poll_answer`` updates.

A native Telegram poll tells us the per-option counts when it is stopped, but
never who voted. A **non-anonymous** poll instead emits a ``poll_answer`` update
per voter, and recording those is the only way to pay the participation prize to
the people who actually took part (STEERING §18.2).

This is the one public (non-admin) piece of the poll feature: voters are ordinary
group members, so the handler is deliberately **not** on the admin-gated events
router. Registering a ``poll_answer`` handler is also what makes
``dp.resolve_used_update_types()`` subscribe the bot to that update type.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import PollAnswer
from sqlalchemy.ext.asyncio import AsyncSession

from services import poll_service

log = logging.getLogger(__name__)
router = Router()


@router.poll_answer()
async def on_poll_answer(poll_answer: PollAnswer, db_session: AsyncSession) -> None:
    # `user` is None for anonymous channel votes (voter_chat); nothing to pay there.
    user = poll_answer.user
    if user is None or user.is_bot:
        return
    poll = await poll_service.get_by_tg_poll_id(db_session, poll_answer.poll_id)
    # Only track polls we still own and that are live: a stale poll_id (finished /
    # deleted / a poll we never created) is simply ignored.
    if poll is None or poll.status != "running":
        return
    await poll_service.record_vote(
        db_session, poll.id, user.id, list(poll_answer.option_ids)
    )
    await db_session.commit()
