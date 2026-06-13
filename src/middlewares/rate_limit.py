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
from aiogram.types import CallbackQuery, Message, TelegramObject, User

MAX_CALLS = 12
WINDOW_SECONDS = 10.0
# Sweep the whole dict every N calls so idle users' (now-empty) entries are
# reclaimed — otherwise the dict grows once per unique user, forever.
_CLEANUP_EVERY = 512


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._timestamps: dict[int, list[float]] = defaultdict(list)
        self._op_count = 0

    def _evict(self, user_id: int, now: float) -> list[float]:
        """Drop this user's timestamps outside the window; remove the key entirely
        when it empties so inactive users don't linger in the dict."""
        kept = [t for t in self._timestamps[user_id] if now - t < WINDOW_SECONDS]
        if kept:
            self._timestamps[user_id] = kept
        else:
            self._timestamps.pop(user_id, None)
        return kept

    def _sweep(self, now: float) -> None:
        for uid in list(self._timestamps):
            if not [t for t in self._timestamps[uid] if now - t < WINDOW_SECONDS]:
                self._timestamps.pop(uid, None)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        now = time.monotonic()
        self._op_count += 1
        if self._op_count >= _CLEANUP_EVERY:
            self._op_count = 0
            self._sweep(now)

        kept = self._evict(tg_user.id, now)

        if len(kept) >= MAX_CALLS:
            if isinstance(event, Message):
                await event.answer("⚠️ Stai inviando troppi comandi. Aspetta qualche secondo.")
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    "⚠️ Troppo veloce! Aspetta qualche secondo.", show_alert=True
                )
            return

        self._timestamps[tg_user.id].append(now)
        return await handler(event, data)
