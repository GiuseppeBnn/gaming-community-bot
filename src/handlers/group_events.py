"""
Group lifecycle handlers — keep the bot's view of the group in sync.

These exist because of a subtle, high-impact bug: making a basic group public
converts it into a supergroup with a BRAND-NEW chat id. The id in the .env then
goes stale and every admin/membership check silently breaks. We listen for the
migration service messages (and the Bot API error, in group_registry) and update
the runtime effective group id, persisted so it survives a restart.

We also invalidate the membership / admin caches on the relevant events so a
promotion, demotion, join or leave is reflected well before the 300s TTL.

Note: chat_member updates are only delivered while the bot is a group admin —
otherwise invalidation degrades gracefully to the cache TTL.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from filters.admin_filter import invalidate_admin_cache
from middlewares import group_guard
from services import group_registry

log = logging.getLogger(__name__)
router = Router()

_ADMIN_STATUSES = {"administrator", "creator"}


async def _apply_migration(session: AsyncSession, old_id: int, new_id: int) -> None:
    await group_registry.record_migration(session, old_id, new_id)
    await session.commit()
    invalidate_admin_cache()
    group_guard.invalidate_all()


@router.message(F.migrate_to_chat_id)
async def on_migrate_to(message: Message, db_session: AsyncSession) -> None:
    """Service message in the OLD chat: it now points at the new supergroup."""
    if message.chat.id != group_registry.get_group_id():
        return
    await _apply_migration(db_session, message.chat.id, message.migrate_to_chat_id)


@router.message(F.migrate_from_chat_id)
async def on_migrate_from(message: Message, db_session: AsyncSession) -> None:
    """Service message in the NEW supergroup (covers the case where the bot only
    sees this half of the migration pair)."""
    if message.migrate_from_chat_id != group_registry.get_group_id():
        return
    await _apply_migration(db_session, message.migrate_from_chat_id, message.chat.id)


@router.chat_member()
async def on_chat_member(event: ChatMemberUpdated) -> None:
    """A user's membership/role changed: refresh the relevant caches."""
    if event.chat.id != group_registry.get_group_id():
        return
    group_guard.invalidate_cache(event.new_chat_member.user.id)
    statuses = {event.old_chat_member.status, event.new_chat_member.status}
    if statuses & _ADMIN_STATUSES:
        invalidate_admin_cache()


@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    """The bot's own status changed (added/removed/promoted): the admin set may
    have changed, so drop the cached set."""
    invalidate_admin_cache()
