"""
Reusable per-(bucket, user) anti-spam cooldown.

Generalizes the in-memory throttle in ``handlers.fun_ai`` so any command can rate
itself without sharing the global 12-calls/10s rate-limit budget. Admins are
exempt by default. State is in-memory (resets on restart — fine for spam control)
and self-prunes so a busy group can't grow it without bound.

Usage in a handler::

    from utils import cooldown
    if not await cooldown.guard(message, "classifiche", settings.command_cooldown_seconds):
        return
"""

from __future__ import annotations

import time

from aiogram.types import Message

from filters.admin_filter import is_admin

# (bucket, user_id) → monotonic timestamp of last accepted use.
_store: dict[tuple[str, int], float] = {}
_PRUNE_THRESHOLD = 1024
_PRUNE_MAX_AGE = 3600.0  # entries older than this are dropped on prune


def _prune(now: float) -> None:
    if len(_store) <= _PRUNE_THRESHOLD:
        return
    for key, ts in [(k, t) for k, t in _store.items() if now - t >= _PRUNE_MAX_AGE]:
        _store.pop(key, None)


def remaining(bucket: str, user_id: int, seconds: float) -> float:
    """Seconds left on this user's cooldown for `bucket` (0.0 if ready now)."""
    last = _store.get((bucket, user_id))
    if last is None:
        return 0.0
    left = seconds - (time.monotonic() - last)
    return left if left > 0 else 0.0


def mark(bucket: str, user_id: int) -> None:
    """Record a use now (starts/refreshes the cooldown)."""
    now = time.monotonic()
    _store[(bucket, user_id)] = now
    _prune(now)


async def guard(
    message: Message,
    bucket: str,
    seconds: float,
    *,
    exempt_admin: bool = True,
    notice: str | None = None,
) -> bool:
    """True if the user may proceed (and records the use); else reply + False.

    `notice` may contain ``{s}`` for the remaining seconds. Admins bypass by
    default (``exempt_admin``).
    """
    uid = message.from_user.id if message.from_user else 0
    if exempt_admin and uid and await is_admin(message.bot, uid):
        return True
    left = remaining(bucket, uid, seconds)
    if left > 0:
        template = notice or "⏳ Vai più piano! Riprova tra {s}s."
        await message.reply(template.format(s=int(left) + 1))
        return False
    mark(bucket, uid)
    return True


def reset() -> None:
    """Clear all cooldowns — test helper."""
    _store.clear()
