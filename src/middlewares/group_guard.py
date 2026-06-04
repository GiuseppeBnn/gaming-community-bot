"""
Ensures that only members of the configured Telegram group can interact with
the bot in private chat.

Membership is checked via get_chat_member and cached per-user for
GROUP_MEMBER_CACHE_TTL seconds to avoid hammering the Telegram API.

Group updates (messages/callbacks posted inside the group itself) are always
allowed through — the group itself is the authoritative context.

If GROUP_ID is 0 (not configured) the middleware is fully bypassed.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from config_data.config import settings

_GROUP_MEMBER_CACHE_TTL = 300  # seconds

_cache: dict[int, tuple[bool, float]] = {}

_NON_MEMBER_STATUSES = {"left", "kicked", "banned"}


class GroupMemberMiddleware(BaseMiddleware):
    """Rejects private-chat updates from users who are not group members."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Disabled when no group is configured
        if settings.group_id == 0:
            return await handler(event, data)

        chat_type = _chat_type(event)

        # Only gate private chats; group/supergroup updates pass freely
        if chat_type != "private":
            return await handler(event, data)

        tg_user: User | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        bot: Bot = data["bot"]
        if not await _is_group_member(bot, tg_user.id):
            await _reject(event)
            return

        return await handler(event, data)


def _chat_type(event: TelegramObject) -> str | None:
    if isinstance(event, Message):
        return event.chat.type
    if isinstance(event, CallbackQuery) and event.message:
        return event.message.chat.type
    return None


async def _is_group_member(bot: Bot, user_id: int) -> bool:
    now = time.monotonic()
    cached = _cache.get(user_id)
    if cached is not None:
        is_member, ts = cached
        if now - ts < _GROUP_MEMBER_CACHE_TTL:
            return is_member

    try:
        member = await bot.get_chat_member(settings.group_id, user_id)
        is_member = member.status not in _NON_MEMBER_STATUSES
    except Exception:
        # On API error (e.g. bot removed from group) fail open to avoid locking out users
        is_member = True

    _cache[user_id] = (is_member, now)
    return is_member


def invalidate_cache(user_id: int) -> None:
    """Call this when a user joins or leaves the group."""
    _cache.pop(user_id, None)


async def _reject(event: TelegramObject) -> None:
    msg = (
        "⛔ <b>Accesso negato.</b>\n\n"
        "Devi essere membro del gruppo per usare questo bot."
    )
    if isinstance(event, Message):
        await event.answer(msg)
    elif isinstance(event, CallbackQuery):
        await event.answer("⛔ Devi essere membro del gruppo.", show_alert=True)
