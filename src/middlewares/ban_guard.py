"""
Bot-level ban enforcement.

A user flagged ``is_banned`` (set by /ban, the warn auto-ban and the admin
dashboard; cleared by /sban) is **dead to the bot**: every update from them —
in the group AND in private — is dropped **silently** (no reply at all). Their
data is never touched, so /sban fully restores them.

The flag is read through the request's DB session and cached per-user in memory.
The cache is invalidated explicitly on ban/sban (so the change takes effect on
the very next update); a short TTL is only a safety net. Runs AFTER
``DbSessionMiddleware`` (needs ``db_session``) and BEFORE the group-membership
guard.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User as DbUser

_BAN_CACHE_TTL = 300  # seconds (safety net; ban/sban invalidate explicitly)
_CACHE_PRUNE_THRESHOLD = 4096

# user_id -> (is_banned, monotonic_ts)
_cache: dict[int, tuple[bool, float]] = {}


class BannedUserMiddleware(BaseMiddleware):
    """Silently drops every update from a bot-banned user."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        session: AsyncSession | None = data.get("db_session")
        if tg_user is None or session is None:
            return await handler(event, data)

        if await _is_banned(session, tg_user.id):
            return  # silent: never reply, anywhere

        return await handler(event, data)


def _prune_expired(now: float) -> None:
    if len(_cache) <= _CACHE_PRUNE_THRESHOLD:
        return
    for uid in [u for u, (_b, ts) in _cache.items() if now - ts >= _BAN_CACHE_TTL]:
        del _cache[uid]


async def _is_banned(session: AsyncSession, user_id: int) -> bool:
    now = time.monotonic()
    cached = _cache.get(user_id)
    if cached is not None and now - cached[1] < _BAN_CACHE_TTL:
        return cached[0]

    banned = bool(
        await session.scalar(select(DbUser.is_banned).where(DbUser.tg_id == user_id))
    )
    _prune_expired(now)
    _cache[user_id] = (banned, now)
    return banned


def invalidate(user_id: int) -> None:
    """Drop the cached ban state for a user — call right after /ban or /sban so
    the new state applies on their next update."""
    _cache.pop(user_id, None)


def invalidate_all() -> None:
    """Clear the whole ban cache (test helper / mass changes)."""
    _cache.clear()
