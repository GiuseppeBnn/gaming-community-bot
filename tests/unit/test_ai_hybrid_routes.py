from __future__ import annotations

from unittest.mock import AsyncMock

from aioresponses import aioresponses
import pytest

from services import ai_service, alduino_chat
from services.alduino_chat import AlduinoAIError, GeneratedReply


@pytest.fixture
def hybrid(monkeypatch):
    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(ai_service.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(ai_service.settings, "openrouter_chat_models", "z-ai/glm-5.3-flash")
    monkeypatch.setattr(ai_service.settings, "openrouter_fun_models", "z-ai/glm-5.3-flash")


def test_glm_policy_leaves_reasoning_room_and_rejects_mixed_routes(hybrid, monkeypatch):
    monkeypatch.setattr(ai_service.settings, "openrouter_reasoning_token_allowance", 1024)
    assert ai_service.openrouter_generation_policy(("z-ai/glm-5.3-flash",), 280) == (
        {"effort": "low", "exclude": True}, 1304,
    )
    with pytest.raises(ai_service.AIServiceError, match="mixed reasoning"):
        ai_service.openrouter_generation_policy(("z-ai/glm-5.3-flash", "deepseek/other"), 280)


async def test_glm_chat_reserves_larger_reasoning_budget_and_sends_high(hybrid, monkeypatch):
    from services import ai_budget

    reserve = AsyncMock(return_value=ai_budget.Reservation("r", "2026-09", "chat", "other", 100))
    settle = AsyncMock()
    monkeypatch.setattr(ai_budget, "reserve", reserve)
    monkeypatch.setattr(ai_budget, "settle", settle)
    monkeypatch.setattr(ai_service.settings, "openrouter_reasoning_token_allowance", 1024)
    monkeypatch.setattr(ai_service.settings, "openrouter_chat_reasoning_token_allowance", 2048)
    monkeypatch.setattr(ai_service.settings, "openrouter_chat_reasoning_effort", "high")
    with aioresponses() as mocked:
        mocked.post(ai_service.settings.openrouter_url, payload={
            "model": "z-ai/glm-5.3-flash",
            "choices": [{"message": {"content": "<think>interno</think>Ciao."}}],
            "usage": {"cost": .0001, "completion_tokens": 100,
                      "completion_tokens_details": {"reasoning_tokens": 90}},
        })
        assert await ai_service.generate_openrouter_completion(
            "s", "u", 280, feature="alduino_chat",
            models=("z-ai/glm-5.3-flash",), require_zdr=True,
        ) == "Ciao."
    sent = next(iter(mocked.requests.values()))[0].kwargs["json"]
    assert sent["max_tokens"] == reserve.call_args.kwargs["max_output_tokens"] == 2328
    assert sent["reasoning"] == {"effort": "high", "exclude": True}
    assert sent["provider"]["zdr"] is True
    assert "sort" not in sent["provider"]
    assert sent["provider"]["max_price"] == {"prompt": .25, "completion": .6}
    assert settle.call_args.kwargs["metrics"].reasoning_tokens == 90
    assert settle.call_args.kwargs["actual_microusd"] == 100

@pytest.mark.parametrize("legacy_selector", ["auto", "groq", "gemini", "openrouter"])
async def test_legacy_chat_selector_never_reactivates_free_providers(hybrid, monkeypatch, legacy_selector):
    from config_data.config import Settings

    configured = Settings(bot_token="x", _env_file=None, alduino_provider=legacy_selector)
    monkeypatch.setattr(ai_service, "settings", configured)
    monkeypatch.setattr(alduino_chat, "settings", configured)
    free = AsyncMock(side_effect=AssertionError("free provider called"))
    paid = AsyncMock(return_value="OpenRouter")
    monkeypatch.setattr(ai_service, "generate_groq_completion", free)
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", paid)
    assert await alduino_chat.generate_reply(system_prompt="s", current="u") == GeneratedReply(
        "OpenRouter", "openrouter",
    )
    free.assert_not_awaited()
    paid.assert_awaited_once()


@pytest.mark.parametrize("legacy_selector", ["auto", "groq", "openrouter"])
async def test_fun_is_openrouter_only_and_preserves_parameters(hybrid, monkeypatch, legacy_selector):
    from config_data.config import Settings

    configured = Settings(bot_token="x", _env_file=None, ai_entertainment_provider=legacy_selector)
    monkeypatch.setattr(ai_service, "settings", configured)
    free = AsyncMock(side_effect=AssertionError("free provider called"))
    paid = AsyncMock(return_value="p" * 2000)
    monkeypatch.setattr(ai_service, "generate_groq_completion", free)
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", paid)
    reply = await ai_service.generate_completion("persona", "contenuto", 160, temperature=.4)
    assert len(reply) == 600 and reply.endswith("…")
    paid.assert_awaited_once_with(
        "persona", "contenuto", 160, temperature=.4, feature="entertainment",
        models=("z-ai/glm-5.3-flash",), require_zdr=False,
    )
    free.assert_not_awaited()


async def test_chat_failure_opens_breaker_without_extra_spending(hybrid, monkeypatch):
    paid = AsyncMock(side_effect=ai_service.AIServiceError("timeout"))
    free = AsyncMock(side_effect=AssertionError("free fallback"))
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", paid)
    monkeypatch.setattr(ai_service, "generate_groq_completion", free)
    for _ in range(2):
        with pytest.raises(AlduinoAIError, match="chat providers unavailable"):
            await alduino_chat.generate_reply(system_prompt="s", current="u")
    paid.assert_awaited_once()
    free.assert_not_awaited()


@pytest.mark.parametrize("field,value", [
    ("alduino_provider", "auto"), ("alduino_provider", "gemini"),
    ("ai_entertainment_provider", "groq"),
    ("twentyq_provider_order", "groq,gemini,openrouter"),
])
def test_old_environment_values_migrate_to_openrouter(field, value):
    from config_data.config import Settings

    configured = Settings(bot_token="x", _env_file=None, **{field: value})
    assert field not in type(configured).model_fields


@pytest.mark.parametrize("field", ["alduino_provider", "ai_entertainment_provider"])
def test_provider_typos_are_not_silently_accepted(field):
    from config_data.config import Settings

    with pytest.raises(ValueError, match="unknown AI provider"):
        Settings(bot_token="x", _env_file=None, **{field: "openroter"})


@pytest.mark.parametrize("model", ["z-ai/glm-5.3-flash", "z-ai/glm-5.3", "openai/gpt-oss-120b"])
def test_mandatory_reasoning_is_bounded_and_excluded(model):
    policy, tokens = ai_service.openrouter_generation_policy((model,), 32)
    assert policy == {"effort": "low", "exclude": True}
    assert tokens == 32 + ai_service.settings.openrouter_reasoning_token_allowance
