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


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._timestamps: dict[int, list[float]] = defaultdict(list)

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
        # Evict timestamps outside the window
        self._timestamps[tg_user.id] = [
            t for t in self._timestamps[tg_user.id] if now - t < WINDOW_SECONDS
        ]

        if len(self._timestamps[tg_user.id]) >= MAX_CALLS:
            if isinstance(event, Message):
                await event.answer("⚠️ Stai inviando troppi comandi. Aspetta qualche secondo.")
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    "⚠️ Troppo veloce! Aspetta qualche secondo.", show_alert=True
                )
            return

        self._timestamps[tg_user.id].append(now)
        return await handler(event, data)
