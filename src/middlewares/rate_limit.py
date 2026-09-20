"""
Simple sliding-window rate limiter.

Allows at most MAX_CALLS events per user within WINDOW_SECONDS.
Applies to both private and group contexts so no single user can flood
the bot regardless of where they interact.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    Message,
    TelegramObject,
    User,
)

MAX_CALLS = 12
WINDOW_SECONDS = 10.0
# Inline queries fire on every keystroke while the user types "@bot ..." — a much
# higher volume than commands. Give them their own, wider budget, and drop silently
# on overflow: there is no chat message to reply to.
INLINE_MAX_CALLS = 40
INLINE_WINDOW_SECONDS = 10.0
# Sweep the whole dict every N calls so idle users' (now-empty) entries are
# reclaimed — otherwise the dict grows once per unique user, forever.
_CLEANUP_EVERY = 512


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._timestamps: dict[int, list[float]] = defaultdict(list)
        self._inline_timestamps: dict[int, list[float]] = defaultdict(list)
        self._op_count = 0

    def _evict_from(
        self,
        dct: dict[int, list[float]],
        user_id: int,
        now: float,
        window: float,
    ) -> list[float]:
        """Drop this user's timestamps outside the window; remove the key entirely
        when it empties so inactive users don't linger in the dict."""
        kept = [t for t in dct[user_id] if now - t < window]
        if kept:
            dct[user_id] = kept
        else:
            dct.pop(user_id, None)
        return kept

    def _evict(
        self,
        user_id: int,
        now: float,
        window: float = WINDOW_SECONDS,
    ) -> list[float]:
        return self._evict_from(self._timestamps, user_id, now, window)

    def _sweep_from(self, dct: dict[int, list[float]], now: float, window: float) -> None:
        for uid in list(dct):
            if not [t for t in dct[uid] if now - t < window]:
                dct.pop(uid, None)

    def _sweep(self, now: float, window: float = WINDOW_SECONDS) -> None:
        self._sweep_from(self._timestamps, now, window)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        is_inline = isinstance(event, InlineQuery)
        window = INLINE_WINDOW_SECONDS if is_inline else WINDOW_SECONDS
        timestamps = self._inline_timestamps if is_inline else self._timestamps
        limit = INLINE_MAX_CALLS if is_inline else MAX_CALLS

        now = time.monotonic()
        self._op_count += 1
        if self._op_count >= _CLEANUP_EVERY:
            self._op_count = 0
            sweep_now = time.monotonic()
            self._sweep(sweep_now, WINDOW_SECONDS)
            self._sweep_from(self._inline_timestamps, sweep_now, INLINE_WINDOW_SECONDS)

        kept = self._evict_from(timestamps, tg_user.id, now, window)

        if len(kept) >= limit:
            if isinstance(event, InlineQuery):
                return  # silent: no chat message to answer to
            if isinstance(event, Message):
                await event.answer("⚠️ Stai inviando troppi comandi. Aspetta qualche secondo.")
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    "⚠️ Troppo veloce! Aspetta qualche secondo.", show_alert=True
                )
            return

        timestamps[tg_user.id].append(now)
        return await handler(event, data)
