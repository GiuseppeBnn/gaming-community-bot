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
from middlewares import ban_guard, group_guard
from services import admin_service, group_registry

log = logging.getLogger(__name__)
router = Router()

_ADMIN_STATUSES = {"administrator", "creator"}
# Bot API status for a banned user (ChatMemberBanned).
_BANNED_STATUS = "kicked"


def _initiated_by_bot(event: ChatMemberUpdated) -> bool:
    """True when the membership change was performed BY the bot itself (i.e. via
    /ban, /kick, /sban) — those paths already manage ``is_banned`` directly, so we
    must not re-derive it from the resulting chat_member update (it would, e.g.,
    re-ban a /kick)."""
    return (
        event.from_user is not None
        and event.bot is not None
        and event.from_user.id == event.bot.id
    )


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
async def on_chat_member(event: ChatMemberUpdated, db_session: AsyncSession) -> None:
    """A user's membership/role changed: refresh the relevant caches and keep the
    bot-level ban in sync with native (Telegram-UI) moderation."""
    if event.chat.id != group_registry.get_group_id():
        return
    user_id = event.new_chat_member.user.id
    group_guard.invalidate_cache(user_id)
    old, new = event.old_chat_member.status, event.new_chat_member.status
    if {old, new} & _ADMIN_STATUSES:
        invalidate_admin_cache()

    # A user banned/unbanned outside the bot (Telegram UI, by a human admin) must
    # also be cut off from / restored to the bot in private — else a natively-banned
    # user keeps using the bot in DM. Bot-initiated changes are skipped (their own
    # handlers own is_banned, and a /kick transits through "kicked" transiently).
    if old == new or _initiated_by_bot(event):
        return
    if new == _BANNED_STATUS:
        if await admin_service.set_user_banned(db_session, user_id, True):
            await db_session.commit()
            ban_guard.invalidate(user_id)
    elif old == _BANNED_STATUS:
        if await admin_service.set_user_banned(db_session, user_id, False):
            await db_session.commit()
            ban_guard.invalidate(user_id)


@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    """The bot's own status changed (added/removed/promoted): the admin set may
    have changed, so drop the cached set."""
    invalidate_admin_cache()
