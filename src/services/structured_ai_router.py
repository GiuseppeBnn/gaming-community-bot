"""Ordered structured-provider routing with deadline, breaker and safe audit."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
import logging
import time
from typing import Any, Generic, TypeVar, cast

from config_data.config import settings
from services import ai_budget
from services.structured_ai import (
    GeminiStructuredProvider,
    GroqStructuredProvider,
    OpenRouterStructuredProvider,
    ProviderName,
    StructuredAIError,
    StructuredAIErrorKind,
    StructuredAIProvider,
    StructuredProviderResult,
    StructuredRequest,
)

log = logging.getLogger(__name__)

T = TypeVar("T")
Clock = Callable[[], float]

_TRANSIENT_BREAKER_KINDS = frozenset({
    StructuredAIErrorKind.rate_limit,
    StructuredAIErrorKind.timeout,
    StructuredAIErrorKind.network,
    StructuredAIErrorKind.server,
})
_LONG_BREAKER_KINDS = frozenset({
    StructuredAIErrorKind.missing_key,
    StructuredAIErrorKind.authentication,
    StructuredAIErrorKind.configuration,
    StructuredAIErrorKind.quota,
    StructuredAIErrorKind.budget_exhausted,
    StructuredAIErrorKind.budget_unavailable,
})


@dataclass(frozen=True, slots=True)
class ProviderAttemptRecord:
    session_id: int | None
    operation: str
    provider: ProviderName
    model: str
    prompt_version: str
    schema_version: str
    outcome: str
    error_kind: str | None
    latency_ms: int
    usage: ai_budget.UsageMetrics
    cost_microusd: int | None


@dataclass(frozen=True, slots=True)
class RoutedStructuredResult(Generic[T]):
    value: T
    provider: ProviderName
    model: str
    attempts: tuple[ProviderAttemptRecord, ...]


Recorder = Callable[[ProviderAttemptRecord], Awaitable[None]]


def _breaker_delay(error: StructuredAIError) -> float | None:
    if error.kind in _TRANSIENT_BREAKER_KINDS:
        default = 60
    elif error.kind in _LONG_BREAKER_KINDS:
        default = 900
    else:
        return None
    if error.retry_after_seconds is not None:
        return float(error.retry_after_seconds)
    return float(default)


class _CircuitBreaker:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._open_until: dict[tuple[ProviderName, str], float] = {}

    def is_open(self, provider: ProviderName, model: str) -> bool:
        key = (provider, model)
        deadline = self._open_until.get(key)
        if deadline is None:
            return False
        if self._clock() >= deadline:
            self._open_until.pop(key, None)
            return False
        return True

    def observe(
        self, provider: ProviderName, model: str, error: StructuredAIError,
    ) -> None:
        delay = _breaker_delay(error)
        if delay is not None:
            self._open_until[(provider, model)] = self._clock() + delay


async def _default_recorder(record: ProviderAttemptRecord) -> None:
    from services.ai_provider_audit import record_provider_attempt

    await record_provider_attempt(record)


class StructuredAIRouter:
    def __init__(
        self,
        *,
        providers: Sequence[StructuredAIProvider],
        deadline_seconds: float,
        recorder: Recorder | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._providers = tuple(providers)
        self._deadline_seconds = deadline_seconds
        self._recorder = recorder or _default_recorder
        self._clock = clock
        self._breaker = _CircuitBreaker(clock)

    @property
    def providers(self) -> tuple[StructuredAIProvider, ...]:
        return self._providers

    @staticmethod
    def _latency_ms(started: float, finished: float) -> int:
        return max(0, int((finished - started) * 1000))

    def _failed_record(
        self,
        session_id: int | None,
        request: StructuredRequest,
        provider: StructuredAIProvider,
        error: StructuredAIError,
        started: float,
    ) -> ProviderAttemptRecord:
        return ProviderAttemptRecord(
            session_id=session_id,
            operation=request.operation,
            provider=provider.name,
            model=provider.model,
            prompt_version=request.prompt_version,
            schema_version=request.schema_version,
            outcome="failure",
            error_kind=error.kind.value,
            latency_ms=self._latency_ms(started, self._clock()),
            usage=ai_budget.UsageMetrics(),
            cost_microusd=None,
        )

    def _invalid_record(
        self,
        session_id: int | None,
        request: StructuredRequest,
        result: StructuredProviderResult,
        error: TypeError | ValueError,
        started: float,
    ) -> ProviderAttemptRecord:
        kind = (
            StructuredAIErrorKind.invalid_enum
            if isinstance(error, ValueError)
            else StructuredAIErrorKind.invalid_schema
        )
        return ProviderAttemptRecord(
            session_id=session_id,
            operation=request.operation,
            provider=result.provider,
            model=result.model,
            prompt_version=request.prompt_version,
            schema_version=request.schema_version,
            outcome=kind.value,
            error_kind=kind.value,
            latency_ms=self._latency_ms(started, self._clock()),
            usage=result.usage,
            cost_microusd=result.cost_microusd,
        )

    def _success_record(
        self,
        session_id: int | None,
        request: StructuredRequest,
        result: StructuredProviderResult,
        started: float,
    ) -> ProviderAttemptRecord:
        return ProviderAttemptRecord(
            session_id=session_id,
            operation=request.operation,
            provider=result.provider,
            model=result.model,
            prompt_version=request.prompt_version,
            schema_version=request.schema_version,
            outcome="success",
            error_kind=None,
            latency_ms=self._latency_ms(started, self._clock()),
            usage=result.usage,
            cost_microusd=result.cost_microusd,
        )

    async def _record_best_effort(
        self, record: ProviderAttemptRecord, *, enabled: bool,
    ) -> None:
        if not enabled:
            return
        try:
            await self._recorder(record)
        except Exception:
            log.error(
                "Structured AI audit failed provider=%s outcome=%s",
                record.provider,
                record.outcome,
            )

    async def generate(
        self,
        request: StructuredRequest,
        *,
        session_id: int | None,
        validate: Callable[[dict[str, Any]], T],
        audit: bool = True,
    ) -> RoutedStructuredResult[T]:
        route_started = self._clock()
        attempts: list[ProviderAttemptRecord] = []
        for provider in self._providers:
            if not provider.configured:
                continue
            if self._breaker.is_open(provider.name, provider.model):
                continue
            remaining = self._deadline_seconds - (self._clock() - route_started)
            if remaining <= 0:
                raise StructuredAIError(
                    "structured provider deadline exhausted",
                    kind=StructuredAIErrorKind.deadline,
                )
            attempt_started = self._clock()
            try:
                async with asyncio.timeout(min(provider.timeout_seconds, remaining)):
                    raw = await provider.generate_json(request)
            except TimeoutError:
                error = StructuredAIError(
                    "structured provider timed out",
                    kind=StructuredAIErrorKind.timeout,
                    provider=provider.name,
                )
                record = self._failed_record(
                    session_id, request, provider, error, attempt_started,
                )
                attempts.append(record)
                self._breaker.observe(provider.name, provider.model, error)
                await self._record_best_effort(record, enabled=audit)
                continue
            except StructuredAIError as error:
                record = self._failed_record(
                    session_id, request, provider, error, attempt_started,
                )
                attempts.append(record)
                self._breaker.observe(provider.name, provider.model, error)
                await self._record_best_effort(record, enabled=audit)
                continue
            try:
                value = validate(raw.value)
            except StructuredAIError as error:
                record = self._failed_record(
                    session_id, request, provider, error, attempt_started,
                )
                attempts.append(record)
                self._breaker.observe(provider.name, provider.model, error)
                await self._record_best_effort(record, enabled=audit)
                continue
            except (TypeError, ValueError) as error:
                record = self._invalid_record(
                    session_id, request, raw, error, attempt_started,
                )
                attempts.append(record)
                await self._record_best_effort(record, enabled=audit)
                continue
            record = self._success_record(
                session_id, request, raw, attempt_started,
            )
            attempts.append(record)
            await self._record_best_effort(record, enabled=audit)
            return RoutedStructuredResult(
                value, raw.provider, raw.model, tuple(attempts),
            )
        if self._clock() - route_started >= self._deadline_seconds:
            raise StructuredAIError(
                "structured provider deadline exhausted",
                kind=StructuredAIErrorKind.deadline,
            )
        raise StructuredAIError(
            "all structured providers unavailable",
            kind=StructuredAIErrorKind.providers_unavailable,
        )


@lru_cache(maxsize=1)
def get_twenty_questions_router() -> StructuredAIRouter:
    providers: dict[ProviderName, StructuredAIProvider] = {
        "gemini": cast(StructuredAIProvider, GeminiStructuredProvider()),
        "groq": cast(StructuredAIProvider, GroqStructuredProvider()),
        "openrouter": cast(StructuredAIProvider, OpenRouterStructuredProvider()),
    }
    order = cast(
        tuple[ProviderName, ...],
        tuple(settings.twentyq_provider_order.split(",")),
    )
    return StructuredAIRouter(
        providers=tuple(providers[name] for name in order),
        deadline_seconds=settings.twentyq_provider_deadline_seconds,
    )


def has_configured_twenty_questions_provider() -> bool:
    return any(
        provider.configured for provider in get_twenty_questions_router().providers
    )


def _reset_twenty_questions_router_for_tests() -> None:
    """Drop the cached router so tests never share breaker state."""
    get_twenty_questions_router.cache_clear()
