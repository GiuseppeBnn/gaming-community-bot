"""Small reusable free-to-paid failover boundary for text workloads."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import logging
import time
from typing import Generic, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


class ProviderUnavailable(RuntimeError):
    """An expected provider failure, safe to handle through failover."""


@dataclass(frozen=True, slots=True)
class ProviderAttempt(Generic[T]):
    name: str
    model: str
    generate: Callable[[], Awaitable[T]]
    timeout_seconds: float


class TextAIRouter:
    """Bound total latency and briefly skip a failing provider/model per workload."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._open_until: dict[tuple[str, str, str], float] = {}

    async def generate(
        self, attempts: Sequence[ProviderAttempt[T]], *, workload: str,
        deadline_seconds: float,
    ) -> T:
        deadline = self._clock() + deadline_seconds
        last_error: ProviderUnavailable | None = None
        # Keys come only from configured workloads/models, never Telegram users.
        self._open_until = {
            key: until for key, until in self._open_until.items()
            if until > self._clock()
        }
        for attempt in attempts:
            key = (workload, attempt.name, attempt.model)
            if key in self._open_until:
                continue
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(min(attempt.timeout_seconds, remaining)):
                    return await attempt.generate()
            except TimeoutError:
                last_error = ProviderUnavailable("provider timeout")
            except ProviderUnavailable as exc:
                last_error = exc
            # Cancellation and programming errors must propagate, never trigger
            # an extra paid call. Log only configured metadata, not error bodies.
            self._open_until[key] = self._clock() + 60
            log.warning("AI provider unavailable workload=%s provider=%s", workload, attempt.name)
        raise ProviderUnavailable("all text providers unavailable") from last_error

    def reset(self) -> None:
        self._open_until.clear()


text_router = TextAIRouter()
