from __future__ import annotations

import asyncio
from decimal import Decimal

import aiohttp
from aioresponses import aioresponses
import pytest

from services import ai_budget, ai_service


@pytest.fixture
def openrouter(monkeypatch):
    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "or-test")
    monkeypatch.setattr(ai_service.settings, "openrouter_url", "https://openrouter.test/chat")
    monkeypatch.setattr(ai_service.settings, "openrouter_app_name", "Test Alduino")
    monkeypatch.setattr(ai_service.settings, "openrouter_timeout_seconds", 2)
    monkeypatch.setattr(ai_service.settings, "openrouter_max_prompt_price", Decimal("0.25"))
    monkeypatch.setattr(ai_service.settings, "openrouter_max_completion_price", Decimal("0.60"))
    settlements = []

    async def reserve(**kwargs):
        reserve.kwargs = kwargs
        return ai_budget.Reservation("req", "2026-08", 999)

    async def settle(reservation, **kwargs):
        settlements.append((reservation, kwargs))

    monkeypatch.setattr(ai_service.ai_budget, "reserve", reserve)
    monkeypatch.setattr(ai_service.ai_budget, "settle", settle)
    return reserve, settlements


def _response(content: str = "Risposta", *, cost=0.000123):
    return {
        "model": "deepseek/deepseek-v4-flash-0731",
        "choices": [{"message": {"content": content}}],
        "usage": {
            "cost": cost,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 10},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


async def test_openrouter_route_is_private_price_bounded_and_accounted(openrouter):
    reserve, settlements = openrouter
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, payload=_response())
        result = await ai_service.generate_openrouter_completion(
            "persona",
            "gruppo",
            280,
            feature="alduino_chat",
            models=("deepseek/a", "deepseek/b"),
            require_zdr=True,
        )

    assert result == "Risposta"
    assert reserve.kwargs["feature"] == "alduino_chat"
    request = next(iter(mocked.requests.values()))[0]
    sent = request.kwargs["json"]
    assert sent["models"] == ["deepseek/a", "deepseek/b"]
    assert sent["provider"] == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
        "sort": "price",
        "max_price": {"prompt": 0.25, "completion": 0.6},
        "zdr": True,
    }
    assert sent["reasoning"] == {"effort": "none", "exclude": True}
    assert request.kwargs["headers"]["X-Title"] == "Test Alduino"
    assert settlements[0][1]["status"] == "completed"
    assert settlements[0][1]["actual_microusd"] == 123
    metrics = settlements[0][1]["metrics"]
    assert metrics == ai_budget.UsageMetrics(
        "deepseek/deepseek-v4-flash-0731", 100, 20, 2, 10,
    )


async def test_one_shot_route_denies_training_without_forcing_zdr(openrouter):
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, payload=_response())
        await ai_service.generate_openrouter_completion(
            "s", "u", feature="entertainment", models=("qwen/qwen3.7-flash",),
            require_zdr=False,
        )
    sent = next(iter(mocked.requests.values()))[0].kwargs["json"]
    assert "zdr" not in sent["provider"]
    assert sent["provider"]["data_collection"] == "deny"


async def test_known_http_failure_is_not_charged(openrouter):
    _reserve, settlements = openrouter
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, status=429, body="slow down")
        with pytest.raises(ai_service.AIServiceError, match="status 429"):
            await ai_service.generate_openrouter_completion(
                "s", "u", feature="fun", models=("qwen/a",), require_zdr=False,
            )
    assert settlements[0][1]["status"] == "failed"
    assert settlements[0][1]["actual_microusd"] == 0


@pytest.mark.parametrize("error", [TimeoutError(), aiohttp.ClientConnectionError("down")])
async def test_ambiguous_transport_failure_charges_reservation(openrouter, error):
    _reserve, settlements = openrouter
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, exception=error)
        with pytest.raises(ai_service.AIServiceError, match="OpenRouter"):
            await ai_service.generate_openrouter_completion(
                "s", "u", feature="chat", models=("deepseek/a",), require_zdr=True,
            )
    assert settlements[0][1] == {
        "status": "uncertain",
        "actual_microusd": None,
        "metrics": ai_budget.UsageMetrics(),
    }


async def test_shutdown_cancellation_is_charged_then_propagated(openrouter):
    _reserve, settlements = openrouter
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, exception=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await ai_service.generate_openrouter_completion(
                "s", "u", feature="chat", models=("deepseek/a",), require_zdr=True,
            )
    assert settlements[0][1]["status"] == "uncertain"
    assert settlements[0][1]["actual_microusd"] is None


async def test_malformed_or_think_only_answer_is_settled(openrouter):
    _reserve, settlements = openrouter
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, payload=_response("<think>secret</think>"))
        with pytest.raises(ai_service.AIServiceError, match="malformed"):
            await ai_service.generate_openrouter_completion(
                "s", "u", feature="chat", models=("deepseek/a",), require_zdr=True,
            )
    assert settlements[0][1]["status"] == "malformed"
    assert settlements[0][1]["actual_microusd"] == 123


async def test_budget_failure_blocks_before_http(monkeypatch, openrouter):
    async def blocked(**kwargs):
        raise ai_budget.AIBudgetExceeded("full")

    monkeypatch.setattr(ai_service.ai_budget, "reserve", blocked)
    with pytest.raises(ai_service.AIServiceError, match="budget"):
        await ai_service.generate_openrouter_completion(
            "s", "u", feature="chat", models=("deepseek/a",), require_zdr=True,
        )


async def test_missing_key_empty_route_and_unavailable_budget_are_normalized(
    monkeypatch, openrouter,
):
    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "")
    with pytest.raises(ai_service.AIServiceError, match="missing OpenRouter"):
        await ai_service.generate_openrouter_completion(
            "s", "u", feature="chat", models=("deepseek/a",), require_zdr=True,
        )

    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "key")
    with pytest.raises(ai_service.AIServiceError, match="empty model"):
        await ai_service.generate_openrouter_completion(
            "s", "u", feature="chat", models=(), require_zdr=True,
        )

    async def unavailable(**kwargs):
        raise ai_budget.AIBudgetError("db")

    monkeypatch.setattr(ai_service.ai_budget, "reserve", unavailable)
    with pytest.raises(ai_service.AIServiceError, match="budget unavailable"):
        await ai_service.generate_openrouter_completion(
            "s", "u", feature="chat", models=("deepseek/a",), require_zdr=True,
        )


async def test_accounting_failure_after_success_keeps_answer(monkeypatch, openrouter):
    async def broken(*args, **kwargs):
        raise ai_budget.AIBudgetError("db")

    monkeypatch.setattr(ai_service.ai_budget, "settle", broken)
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, payload=_response())
        assert await ai_service.generate_openrouter_completion(
            "s", "u", feature="chat", models=("deepseek/a",), require_zdr=True,
        ) == "Risposta"


async def test_entertainment_dispatch_uses_configured_order(monkeypatch):
    captured = {}

    async def route(*args, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(ai_service.settings, "ai_entertainment_provider", "openrouter")
    monkeypatch.setattr(ai_service.settings, "openrouter_fun_models", " qwen/a, qwen/a, deep/b ")
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", route)
    assert await ai_service.generate_completion("s", "u", 12, temperature=0.4) == "ok"
    assert captured["models"] == ("qwen/a", "deep/b")
    assert captured["feature"] == "entertainment"
    assert captured["require_zdr"] is False


def test_invalid_model_and_usage_shapes_are_safe():
    with pytest.raises(ai_service.AIServiceError, match="empty model"):
        ai_service.parse_model_list(" , ")
    assert ai_service._openrouter_usage([]) == (ai_budget.UsageMetrics(), None)
    metrics, cost = ai_service._openrouter_usage({
        "model": 42,
        "usage": {"cost": "nan", "prompt_tokens": True, "completion_tokens": -1},
    })
    assert metrics == ai_budget.UsageMetrics()
    assert cost is None


@pytest.mark.parametrize("raw_cost", ["1e999999999", "9223372036854.775808"])
def test_usage_rejects_values_that_cannot_fit_accounting_columns(raw_cost):
    metrics, cost = ai_service._openrouter_usage({
        "usage": {
            "cost": raw_cost,
            "prompt_tokens": 2**31,
            "completion_tokens": 2**63,
            "reasoning_tokens": 10**100,
            "prompt_tokens_details": {"cached_tokens": 2**31},
        },
    })

    assert metrics == ai_budget.UsageMetrics()
    assert cost is None
