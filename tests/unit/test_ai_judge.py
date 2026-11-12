"""Strict OpenRouter judge: one closed enum, metered and fail-closed."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import aiohttp
from aioresponses import aioresponses
import pytest

from services import ai_budget, ai_service


@pytest.fixture
def judge(monkeypatch):
    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(ai_service.settings, "openrouter_judge_model", "openai/gpt-oss-120b")
    reserve = AsyncMock(return_value=ai_budget.Reservation("r", "2026-11", 1000))
    settle = AsyncMock()
    monkeypatch.setattr(ai_budget, "reserve", reserve)
    monkeypatch.setattr(ai_budget, "settle", settle)
    return reserve, settle


def response(value):
    return {
        "model": "openai/gpt-oss-120b",
        "choices": [{"message": {"content": json.dumps({"verdetto": value})}, "finish_reason": "stop"}],
        "usage": {"cost": .00001, "prompt_tokens": 100, "completion_tokens": 50},
    }


@pytest.mark.parametrize("value", ["correct", "series", "wrong"])
async def test_enum_verdict_and_metered_strict_contract(judge, value):
    reserve, settle = judge
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, payload=response(value))
        assert await ai_service.judge_equivalence("regole", "titolo utente") == value
    request = next(iter(mocked.requests.values()))[0]
    sent = request.kwargs["json"]
    assert sent["model"] == "openai/gpt-oss-120b"
    assert sent["temperature"] == 0
    assert sent["response_format"] == {"type": "json_schema", "json_schema": ai_service.JUDGE_SCHEMA}
    assert sent["messages"] == [
        {"role": "system", "content": "regole"},
        {"role": "user", "content": "titolo utente"},
    ]
    assert sent["reasoning"] == {"effort": "low", "exclude": True}
    assert sent["max_tokens"] == 32 + ai_service.settings.openrouter_reasoning_token_allowance
    assert sent["provider"]["zdr"] is True
    assert sent["provider"]["require_parameters"] is True
    assert sent["provider"]["data_collection"] == "deny"
    assert sent["provider"]["allow_fallbacks"] is True
    assert "max_price" in sent["provider"]
    assert reserve.call_args.kwargs["budget_lane"] == "openrouter_other"
    assert reserve.call_args.kwargs["feature"] == "guess_judge"
    assert reserve.call_args.kwargs["max_output_tokens"] == sent["max_tokens"]
    assert settle.call_args.kwargs["actual_microusd"] == 10


@pytest.mark.parametrize("value", [0, 1, True, False, "true", "CORRECT", "Portal 2", None, {}, []])
async def test_unknown_or_non_string_is_never_a_verdict(judge, value):
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, payload=response(value))
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("s", "u")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
async def test_http_failure_never_retries_or_falls_back_to_free(judge, status):
    _, settle = judge
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, status=status)
        with pytest.raises(ai_service.AIServiceError) as error:
            await ai_service.judge_equivalence("s", "u")
        assert error.value.status == status
        assert sum(len(calls) for calls in mocked.requests.values()) == 1
    assert settle.call_args.kwargs["actual_microusd"] == 0


@pytest.mark.parametrize("failure", [TimeoutError(), aiohttp.ClientConnectionError()])
async def test_ambiguous_transport_retains_reservation_and_does_not_retry(judge, failure):
    _, settle = judge
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, exception=failure)
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("s", "u")
        assert sum(len(calls) for calls in mocked.requests.values()) == 1
    assert settle.call_args.kwargs["actual_microusd"] is None


@pytest.mark.parametrize("kind", [ai_budget.AIBudgetExceeded, ai_budget.AIBudgetError])
async def test_budget_blocks_before_network(judge, monkeypatch, kind):
    monkeypatch.setattr(ai_budget, "reserve", AsyncMock(side_effect=kind("blocked")))
    with aioresponses() as mocked:
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("s", "u")
        assert not mocked.requests


async def test_missing_key_never_uses_legacy_credentials(judge, monkeypatch):
    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "")
    monkeypatch.setattr(ai_service.settings, "groq_api_key", "legacy-key")
    with aioresponses() as mocked:
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("s", "u")
        assert not mocked.requests


async def test_judge_schema_has_no_answer_leak_field():
    assert ai_service.JUDGE_SCHEMA["schema"]["properties"] == {
        "verdetto": {"type": "string", "enum": ["correct", "series", "wrong"]}
    }
    assert ai_service.JUDGE_SCHEMA["schema"]["additionalProperties"] is False


async def test_shutdown_is_not_swallowed_or_retried(judge):
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, exception=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await ai_service.judge_equivalence("s", "u")
        assert sum(len(calls) for calls in mocked.requests.values()) == 1
