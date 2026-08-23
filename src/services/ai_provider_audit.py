"""Short, prompt-free persistence for structured provider attempts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from database.connection import async_session_maker
from database.models import AIGameProviderAttempt

if TYPE_CHECKING:
    from services.structured_ai_router import ProviderAttemptRecord

_BIGINT_MAX = 2**63 - 1


def _bounded_counter(value: int | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    return min(_BIGINT_MAX, max(0, value))


async def record_provider_attempt(record: ProviderAttemptRecord) -> None:
    """Commit one operational attempt independently from the handler session."""
    if record.session_id is None:
        return
    usage = record.usage
    async with async_session_maker.begin() as session:
        session.add(AIGameProviderAttempt(
            session_id=record.session_id,
            operation=record.operation[:32],
            provider=record.provider[:32],
            model=record.model[:128],
            prompt_version=record.prompt_version[:32],
            schema_version=record.schema_version[:32],
            outcome=record.outcome[:16],
            error_class=(record.error_kind or "")[:128] or None,
            latency_ms=_bounded_counter(record.latency_ms),
            prompt_tokens=_bounded_counter(usage.prompt_tokens),
            completion_tokens=_bounded_counter(usage.completion_tokens),
            reasoning_tokens=_bounded_counter(usage.reasoning_tokens),
            cached_tokens=_bounded_counter(usage.cached_tokens),
            cost_microusd=_bounded_counter(record.cost_microusd),
        ))
