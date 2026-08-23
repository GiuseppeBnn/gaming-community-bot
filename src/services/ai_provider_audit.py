"""Short, prompt-free persistence for structured provider attempts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from database.connection import async_session_maker
from database.models import AIGameProviderAttempt

if TYPE_CHECKING:
    from services.structured_ai_router import ProviderAttemptRecord

_BIGINT_MAX = 2**63 - 1
_ALLOWED_PROVIDERS = frozenset({"gemini", "groq", "openrouter"})
_ALLOWED_OUTCOMES = frozenset({
    "success", "failure", "missing_key", "authentication", "configuration",
    "quota", "rate_limit", "timeout", "network", "server", "refusal",
    "empty_output", "malformed_json", "invalid_schema", "invalid_enum",
    "output_limit", "budget_exhausted", "budget_unavailable", "deadline",
    "providers_unavailable",
})


def _bounded_counter(value: int | None) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return min(_BIGINT_MAX, value)


def _safe_text(value: object, maximum: int) -> str:
    if type(value) is not str:
        return ""
    return "".join(character for character in value if character.isprintable())[:maximum]


def _safe_provider(value: object) -> str:
    return value if type(value) is str and value in _ALLOWED_PROVIDERS else "unknown"


def _safe_outcome(value: object) -> str:
    return value if type(value) is str and value in _ALLOWED_OUTCOMES else "failure"


async def record_provider_attempt(record: ProviderAttemptRecord) -> None:
    """Commit one operational attempt independently from the handler session."""
    if record.session_id is None:
        return
    usage = record.usage
    async with async_session_maker.begin() as session:
        session.add(AIGameProviderAttempt(
            session_id=record.session_id,
            operation=_safe_text(record.operation, 32),
            provider=_safe_provider(record.provider),
            model=_safe_text(record.model, 128),
            prompt_version=_safe_text(record.prompt_version, 32),
            schema_version=_safe_text(record.schema_version, 32),
            outcome=_safe_outcome(record.outcome),
            error_class=_safe_text(record.error_kind, 128) or None,
            latency_ms=_bounded_counter(record.latency_ms),
            prompt_tokens=_bounded_counter(usage.prompt_tokens),
            completion_tokens=_bounded_counter(usage.completion_tokens),
            reasoning_tokens=_bounded_counter(usage.reasoning_tokens),
            cached_tokens=_bounded_counter(usage.cached_tokens),
            cost_microusd=_bounded_counter(record.cost_microusd),
        ))
