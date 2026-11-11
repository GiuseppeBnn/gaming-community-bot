from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from config_data.config import Settings
from services import ai_service
from scripts import eval_text_ai
from tests.unit.test_twenty_questions_view import NOW, _view
from utils.twenty_questions_view import render_live_card, render_public_help


async def test_light_commands_select_cheaper_route_without_reasoning_allowance(monkeypatch):
    paid = AsyncMock(return_value="battuta")
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", paid)
    monkeypatch.setattr(ai_service.settings, "openrouter_light_models", "inclusionai/ling-3.0-flash")
    assert await ai_service.generate_completion("s", "u", 160, tier="light") == "battuta"
    assert paid.call_args.kwargs["models"] == ("inclusionai/ling-3.0-flash",)
    assert paid.call_args.kwargs["feature"] == "entertainment_light"
    assert ai_service.openrouter_generation_policy(("inclusionai/ling-3.0-flash",), 160) == (
        {"effort": "none", "exclude": True}, 160,
    )


@pytest.mark.parametrize("effort", ["low", "high"])
def test_chat_reasoning_is_independent_from_judges_and_fun(monkeypatch, effort):
    monkeypatch.setattr(ai_service.settings, "openrouter_chat_reasoning_effort", effort)
    monkeypatch.setattr(ai_service.settings, "openrouter_chat_reasoning_token_allowance", 2048)
    monkeypatch.setattr(ai_service.settings, "openrouter_reasoning_token_allowance", 1024)
    chat = ai_service.openrouter_generation_policy(("z-ai/glm-5.3-flash",), 280, profile="chat")
    ordinary = ai_service.openrouter_generation_policy(("z-ai/glm-5.3-flash",), 32)
    assert chat == ({"effort": effort, "exclude": True}, 2328)
    assert ordinary == ({"effort": "low", "exclude": True}, 1056)
    assert ai_service.openrouter_generation_policy(("inclusionai/ling-3.0-flash",), 160, profile="chat")[1] == 160


def test_defaults_and_glm_unsupported_effort_are_validated():
    configured = Settings(bot_token="x", _env_file=None)
    assert configured.ai_cooldown_seconds == 10
    assert configured.openrouter_chat_reasoning_effort == "high"
    with pytest.raises(ValueError):
        Settings(bot_token="x", _env_file=None, openrouter_chat_reasoning_effort="medium")


def test_card_has_no_stale_countdown_and_instructions_are_explicit():
    view = _view()
    first = render_live_card(view, now=NOW)
    assert first == render_live_card(view, now=NOW + timedelta(hours=1))
    assert "Tempo residuo" not in first and "FORSE" not in first.upper()
    assert "SÌ</b> o <b>NO" in first and "NON LO SO" in first
    assert "Scade:" in first
    assert "Scade:" not in render_live_card(replace(view, expires_at=None), now=NOW)
    help_text = render_public_help(view.policy)
    assert "SÌ</b> o <b>NO" in help_text and "NON LO SO" in help_text
    assert "Esempio:" not in help_text
    assert len(first) < 1200 and len(help_text) < 1100


def test_eval_routes_and_preflight_follow_the_actual_profiles(monkeypatch):
    cases = eval_text_ai.build_cases()
    for case in cases:
        route = eval_text_ai._case_models(case, ("standard",), ("chat",), ("light",))
        expected = "chat" if case.command == "alduino" else "light" if case.command in {"maestro", "drama", "insulta"} else "standard"
        assert route == (expected,)
    seen = []

    def policy(models, tokens, *, profile):
        seen.append(profile)
        return {}, tokens

    monkeypatch.setattr(ai_service, "openrouter_generation_policy", policy)
    assert eval_text_ai.run_estimate(cases, ("standard",), ("chat",), ("light",)) > 0
    assert seen == ["default"] * 7 + ["chat"]
