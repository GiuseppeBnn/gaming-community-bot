"""Integration contracts for the prompt-free AI provider audit."""

from __future__ import annotations

from dataclasses import fields
import importlib
import inspect

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import AIGameProviderAttempt, AIGameSession
from services import ai_budget


def _modules():
    return (
        importlib.import_module("services.ai_provider_audit"),
        importlib.import_module("services.structured_ai_router"),
    )


def _factory(engine):
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False,
    )


async def _game_id(factory) -> int:
    async with factory.begin() as session:
        game = AIGameSession(game_type="twentyq", title="Audit", creator_tg_id=1)
        session.add(game)
        await session.flush()
        return game.id


def _record(router, session_id: int | None, **overrides):
    values = {
        "session_id": session_id,
        "operation": "answer_question",
        "provider": "openrouter",
        "model": "openai/gpt-5",
        "prompt_version": "twentyq-v2",
        "schema_version": "1",
        "outcome": "success",
        "error_kind": None,
        "latency_ms": 315,
        "usage": ai_budget.UsageMetrics("openai/gpt-5", 120, 40, 15, 20),
        "cost_microusd": 457,
    }
    values.update(overrides)
    return router.ProviderAttemptRecord(**values)


def test_recorder_signature_and_record_have_no_content_or_identity_fields():
    audit, router = _modules()
    forbidden = {
        "prompt", "dossier", "output", "user", "user_id", "user_tg_id",
        "group", "group_id", "username", "body", "input_text",
    }
    assert set(inspect.signature(audit.record_provider_attempt).parameters) == {"record"}
    assert forbidden.isdisjoint(field.name for field in fields(router.ProviderAttemptRecord))


async def test_provider_attempt_uses_own_transaction_and_persists_safe_metadata(
    engine, monkeypatch,
):
    audit, router = _modules()
    factory = _factory(engine)
    monkeypatch.setattr(audit, "async_session_maker", factory)
    game_id = await _game_id(factory)
    await audit.record_provider_attempt(_record(router, game_id))

    async with factory() as reader:
        attempt = (await reader.execute(select(AIGameProviderAttempt))).scalar_one()
    assert (
        attempt.session_id, attempt.operation, attempt.provider, attempt.model,
        attempt.prompt_version, attempt.schema_version, attempt.outcome,
        attempt.error_class, attempt.latency_ms, attempt.prompt_tokens,
        attempt.completion_tokens, attempt.reasoning_tokens, attempt.cached_tokens,
        attempt.cost_microusd,
    ) == (
        game_id, "answer_question", "openrouter", "openai/gpt-5",
        "twentyq-v2", "1", "success", None, 315, 120, 40, 15, 20, 457,
    )


async def test_session_id_none_does_not_open_a_transaction_or_insert(engine, monkeypatch):
    audit, router = _modules()

    class ForbiddenFactory:
        def begin(self):
            raise AssertionError("session_id=None must not touch the database")

    monkeypatch.setattr(audit, "async_session_maker", ForbiddenFactory())
    await audit.record_provider_attempt(_record(router, None))

    factory = _factory(engine)
    async with factory() as reader:
        count = await reader.scalar(select(func.count()).select_from(AIGameProviderAttempt))
    assert count == 0


async def test_recorder_truncates_strings_and_bounds_db_numeric_types(engine, monkeypatch):
    audit, router = _modules()
    factory = _factory(engine)
    monkeypatch.setattr(audit, "async_session_maker", factory)
    game_id = await _game_id(factory)
    maximum = 2**63 - 1
    record = _record(
        router, game_id, operation="o" * 100, model="m" * 300,
        prompt_version="p" * 100, schema_version="s" * 100,
        outcome="invalid_schema" * 10, error_kind="invalid_schema" * 20,
        latency_ms=2**100,
        usage=ai_budget.UsageMetrics("ignored", True, -4, 2**100, 5),
        cost_microusd=-10,
    )
    await audit.record_provider_attempt(record)

    async with factory() as reader:
        attempt = (await reader.execute(select(AIGameProviderAttempt))).scalar_one()
    assert (
        len(attempt.operation), len(attempt.model), len(attempt.prompt_version),
        len(attempt.schema_version), len(attempt.outcome), len(attempt.error_class),
    ) == (32, 128, 32, 32, 16, 128)
    assert attempt.latency_ms == maximum
    assert attempt.prompt_tokens is None
    assert attempt.completion_tokens == 0
    assert attempt.reasoning_tokens == maximum
    assert attempt.cached_tokens == 5
    assert attempt.cost_microusd == 0


async def test_each_recorder_call_creates_exactly_one_row(engine, monkeypatch):
    audit, router = _modules()
    factory = _factory(engine)
    monkeypatch.setattr(audit, "async_session_maker", factory)
    game_id = await _game_id(factory)
    await audit.record_provider_attempt(_record(router, game_id, outcome="failure"))
    await audit.record_provider_attempt(_record(router, game_id, outcome="success"))

    async with factory() as reader:
        count = await reader.scalar(select(func.count()).select_from(AIGameProviderAttempt))
    assert count == 2
