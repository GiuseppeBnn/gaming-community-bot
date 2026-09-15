from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from aioresponses import aioresponses
import pytest

from services import ai_service, alduino_chat
from services.alduino_chat import AlduinoAIError, DialogueTurn, GeneratedReply


@pytest.fixture
def hybrid(monkeypatch):
    monkeypatch.setattr(ai_service.settings, "ai_entertainment_provider", "auto")
    monkeypatch.setattr(ai_service.settings, "alduino_provider", "auto")
    monkeypatch.setattr(ai_service.settings, "alduino_fallback_to_groq", True)
    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(ai_service.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(ai_service.settings, "openrouter_chat_models", "z-ai/glm-5.3-flash")
    monkeypatch.setattr(ai_service.settings, "openrouter_fun_models", "z-ai/glm-5.3-flash")


@pytest.mark.parametrize("winner", ["gemini", "groq", "openrouter"])
async def test_chat_free_first_and_group_context_only_on_zdr(hybrid, monkeypatch, winner):
    mocks = {}
    for name in ("gemini", "groq", "openrouter"):
        mock = AsyncMock(
            return_value=GeneratedReply("ok", name),
            side_effect=None if name == winner else AlduinoAIError("down"),
        )
        monkeypatch.setattr(alduino_chat, f"_{name}_reply", mock)
        mocks[name] = mock
    parent = SimpleNamespace(
        provider="openrouter", provider_interaction_id=None,
        history_json=alduino_chat.encode_history((DialogueTurn("prima", "risposta"),)),
    )
    assert await alduino_chat.generate_reply(
        system_prompt="persona", current="dopo", parent=parent,
        live_context="evento", group_context="ambientale", quoted_bot_text="citato",
    ) == GeneratedReply("ok", winner)
    for name in ("gemini", "groq", "openrouter"):
        if mocks[name].await_count:
            sent = mocks[name].call_args.kwargs
            assert sent["history"] == (DialogueTurn("prima", "risposta"),)
            assert sent["live_context"] == "evento"
            assert sent["quoted_bot_text"] == "citato"
            assert sent["group_context"] == ("ambientale" if name == "openrouter" else "")
    if winner == "gemini":
        mocks["groq"].assert_not_awaited()
    if winner != "openrouter":
        mocks["openrouter"].assert_not_awaited()


async def test_chat_can_skip_groq_and_paid_without_key(hybrid, monkeypatch):
    monkeypatch.setattr(ai_service.settings, "alduino_fallback_to_groq", False)
    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "")
    monkeypatch.setattr(alduino_chat, "_gemini_reply", AsyncMock(side_effect=AlduinoAIError()))
    groq, paid = AsyncMock(), AsyncMock()
    monkeypatch.setattr(alduino_chat, "_groq_reply", groq)
    monkeypatch.setattr(alduino_chat, "_openrouter_reply", paid)
    with pytest.raises(AlduinoAIError, match="chat providers unavailable"):
        await alduino_chat.generate_reply(system_prompt="s", current="u")
    groq.assert_not_awaited()
    paid.assert_not_awaited()


@pytest.mark.parametrize("free_ok", [True, False])
async def test_fun_preserves_prompt_temperature_caps_and_free_priority(
    hybrid, monkeypatch, free_ok,
):
    free = AsyncMock(return_value="free", side_effect=None if free_ok else ai_service.AIServiceError())
    paid = AsyncMock(return_value="p" * 2000)
    monkeypatch.setattr(ai_service, "generate_groq_completion", free)
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", paid)
    result = await ai_service.generate_completion("persona", "contenuto", 160, temperature=.4)
    free.assert_awaited_once_with("persona", "contenuto", 160, temperature=.4)
    if free_ok:
        assert result == "free"
        paid.assert_not_awaited()
    else:
        assert len(result) == 600 and result.endswith("…")
        paid.assert_awaited_once_with(
            "persona", "contenuto", 160, temperature=.4, feature="entertainment",
            models=("z-ai/glm-5.3-flash",), require_zdr=False,
        )


async def test_all_fun_providers_down_is_normalized(hybrid, monkeypatch):
    for name in ("generate_groq_completion", "generate_openrouter_completion"):
        monkeypatch.setattr(ai_service, name, AsyncMock(side_effect=ai_service.AIServiceError()))
    with pytest.raises(ai_service.AIServiceError, match="entertainment providers unavailable"):
        await ai_service.generate_completion("s", "u")


async def test_explicit_free_fun_mode_never_calls_paid(hybrid, monkeypatch):
    monkeypatch.setattr(ai_service.settings, "ai_entertainment_provider", "groq")
    free = AsyncMock(return_value="free")
    paid = AsyncMock()
    monkeypatch.setattr(ai_service, "generate_groq_completion", free)
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", paid)
    assert await ai_service.generate_completion("s", "u", 160) == "free"
    paid.assert_not_awaited()


def test_glm_policy_leaves_reasoning_room_and_rejects_mixed_routes(hybrid, monkeypatch):
    monkeypatch.setattr(ai_service.settings, "openrouter_reasoning_token_allowance", 1024)
    assert ai_service.openrouter_generation_policy(("z-ai/glm-5.3-flash",), 280) == (
        {"effort": "low", "exclude": True}, 1304,
    )
    with pytest.raises(ai_service.AIServiceError, match="mixed reasoning"):
        ai_service.openrouter_generation_policy(("z-ai/glm-5.3-flash", "deepseek/other"), 280)


def test_paid_route_must_follow_free_in_game_configuration():
    from config_data.config import Settings

    with pytest.raises(ValueError, match="paid OpenRouter must follow"):
        Settings(bot_token="x", _env_file=None, twentyq_provider_order="openrouter,gemini")


async def test_glm_chat_reserves_reasoning_and_sends_low(hybrid, monkeypatch):
    from services import ai_budget

    reserve = AsyncMock(return_value=ai_budget.Reservation("r", "2026-09", "chat", "other", 100))
    settle = AsyncMock()
    monkeypatch.setattr(ai_budget, "reserve", reserve)
    monkeypatch.setattr(ai_budget, "settle", settle)
    monkeypatch.setattr(ai_service.settings, "openrouter_reasoning_token_allowance", 1024)
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
    assert sent["max_tokens"] == reserve.call_args.kwargs["max_output_tokens"] == 1304
    assert sent["reasoning"] == {"effort": "low", "exclude": True}
    assert sent["provider"]["zdr"] is True
    assert sent["provider"]["sort"] == "latency"
    assert sent["provider"]["max_price"] == {"prompt": .25, "completion": .6}
    assert settle.call_args.kwargs["metrics"].reasoning_tokens == 90
    assert settle.call_args.kwargs["actual_microusd"] == 100
