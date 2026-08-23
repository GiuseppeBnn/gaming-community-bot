"""Integration contracts for the prompt-free Alduino provider audit."""

from __future__ import annotations

from sqlalchemy import select

from database.models import AIGameProviderAttempt, AIGameSession


async def test_provider_attempt_persists_operational_metadata_without_conversation_data(session):
    game = AIGameSession(game_type="twentyq", title="Audit", creator_tg_id=1)
    session.add(game)
    await session.flush()
    session.add(
        AIGameProviderAttempt(
            session_id=game.id,
            operation="answer_question",
            provider="openrouter",
            model="openai/gpt-5",
            prompt_version="twentyq-v2",
            schema_version="1",
            outcome="success",
            error_class=None,
            latency_ms=315,
            prompt_tokens=120,
            completion_tokens=40,
            reasoning_tokens=15,
            cached_tokens=20,
            cost_microusd=457,
        )
    )
    await session.commit()

    attempt = (await session.execute(select(AIGameProviderAttempt))).scalar_one()
    assert (
        attempt.operation,
        attempt.provider,
        attempt.model,
        attempt.cost_microusd,
    ) == ("answer_question", "openrouter", "openai/gpt-5", 457)
