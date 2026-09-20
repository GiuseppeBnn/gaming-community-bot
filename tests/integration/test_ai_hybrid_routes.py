from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aioresponses import aioresponses
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import AIUsageLog
from services import ai_budget, ai_service, alduino_chat
from services.alduino_chat import AlduinoAIError, DialogueTurn, GeneratedReply


@pytest.fixture
def hybrid_db(engine, monkeypatch):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(ai_budget, "async_session_maker", factory)
    monkeypatch.setattr(ai_budget.settings, "ai_monthly_budget_usd", Decimal("5"))
    monkeypatch.setattr(ai_budget.settings, "openrouter_other_budget_usd", Decimal("1"))
    monkeypatch.setattr(ai_service.settings, "alduino_provider", "auto")
    monkeypatch.setattr(ai_service.settings, "alduino_fallback_to_groq", True)
    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(ai_service.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(ai_service.settings, "openrouter_chat_models", "z-ai/glm-5.3-flash")
    monkeypatch.setattr(ai_service.settings, "openrouter_url", "https://openrouter.test/chat")
    return factory


async def test_free_chat_succeeds_even_when_paid_budget_is_off(hybrid_db, monkeypatch):
    monkeypatch.setattr(ai_budget.settings, "openrouter_other_budget_usd", Decimal("0"))
    monkeypatch.setattr(alduino_chat, "_gemini_reply", AsyncMock(return_value=GeneratedReply("ok", "gemini")))
    paid = AsyncMock()
    monkeypatch.setattr(alduino_chat, "_openrouter_reply", paid)
    assert await alduino_chat.generate_reply(system_prompt="s", current="u") == GeneratedReply("ok", "gemini")
    paid.assert_not_awaited()
    assert await ai_budget.snapshot() is None
    async with hybrid_db() as session:
        assert (await session.execute(select(AIUsageLog))).scalars().all() == []


async def test_glm_fallback_keeps_branch_zdr_and_persistent_cost_accounting(hybrid_db, monkeypatch):
    for provider in ("gemini", "groq"):
        monkeypatch.setattr(alduino_chat, f"_{provider}_reply", AsyncMock(side_effect=AlduinoAIError("down")))
    parent = SimpleNamespace(
        provider="gemini", provider_interaction_id="old-interaction",
        history_json=alduino_chat.encode_history((DialogueTurn("Mario gioca a Doom", "ricordo"),)),
    )
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, payload={
            "model": "z-ai/glm-5.3-flash",
            "choices": [{"message": {"content": "<think>invisible</think>Doom, ricordo."}}],
            "usage": {"cost": .0001, "prompt_tokens": 100, "completion_tokens": 910,
                      "completion_tokens_details": {"reasoning_tokens": 900}},
        })
        reply = await alduino_chat.generate_reply(
            system_prompt="persona", current="cosa gioca Mario?", parent=parent,
            live_context="evento aperto", group_context="Mario: Doom",
        )
    assert reply == GeneratedReply("Doom, ricordo.", "openrouter")
    sent = next(iter(mocked.requests.values()))[0].kwargs["json"]
    assert sent["models"] == ["z-ai/glm-5.3-flash"]
    assert sent["reasoning"]["effort"] == "low"
    assert sent["provider"]["zdr"] and sent["provider"]["data_collection"] == "deny"
    assert "sort" not in sent["provider"]
    assert "Mario: Doom" in sent["messages"][1]["content"]
    assert "UTENTE: Mario gioca a Doom" in sent["messages"][1]["content"]
    snapshot = await ai_budget.snapshot()
    assert snapshot.spent_microusd == 100 and snapshot.reserved_microusd == 0
    async with hybrid_db() as session:
        row = (await session.execute(select(AIUsageLog))).scalar_one()
        assert row.requested_model == row.actual_model == "z-ai/glm-5.3-flash"
        assert row.reasoning_tokens == 900
        assert row.feature == "alduino_chat"
    lane = await ai_budget.feature_snapshot("openrouter_other")
    assert lane.spent_microusd == 100 and lane.reserved_microusd == 0


async def test_exhausted_paid_lane_blocks_glm_before_network(hybrid_db, monkeypatch):
    monkeypatch.setattr(ai_budget.settings, "openrouter_other_budget_usd", Decimal("0"))
    for provider in ("gemini", "groq"):
        monkeypatch.setattr(alduino_chat, f"_{provider}_reply", AsyncMock(side_effect=AlduinoAIError("down")))
    # Any accidental POST would be recorded by aioresponses and fail this check.
    with aioresponses() as mocked:
        with pytest.raises(AlduinoAIError, match="chat providers unavailable"):
            await alduino_chat.generate_reply(system_prompt="s", current="u")
        assert not mocked.requests


async def test_prompt_cache_still_generates_fresh_answers_and_reserves_full_cost(hybrid_db):
    with aioresponses() as mocked:
        for text, cached, cost in (("prima", 0, .0001), ("seconda", 90, .00001)):
            mocked.post(ai_service.settings.openrouter_url, payload={
                "model": "z-ai/glm-5.3-flash",
                "choices": [{"message": {"content": text}}],
                "usage": {"cost": cost, "prompt_tokens": 100, "completion_tokens": 10,
                          "prompt_tokens_details": {"cached_tokens": cached}},
            })
        answers = [await ai_service.generate_openrouter_completion(
            "s", "stesso input", 280, feature="cache_contract_test",
            models=("z-ai/glm-5.3-flash",), require_zdr=True,
        ) for _ in range(2)]
        calls = next(iter(mocked.requests.values()))
    assert answers == ["prima", "seconda"] and len(calls) == 2
    first, second = (call.kwargs["json"] for call in calls)
    assert first == second
    assert first["provider"]["zdr"] and first["max_tokens"] == 1304
    async with hybrid_db() as session:
        rows = (await session.execute(select(AIUsageLog).order_by(AIUsageLog.cached_tokens))).scalars().all()
        assert len({row.request_id for row in rows}) == 2
        assert [row.cached_tokens for row in rows] == [0, 90]
        assert [row.actual_microusd for row in rows] == [100, 10]
        assert rows[0].reserved_microusd == rows[1].reserved_microusd > 100
    snapshot = await ai_budget.snapshot()
    assert snapshot.spent_microusd == 110 and snapshot.reserved_microusd == 0
