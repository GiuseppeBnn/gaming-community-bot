from __future__ import annotations

from argparse import Namespace
from unittest.mock import AsyncMock

import pytest

from scripts import eval_text_ai
from services import ai_budget, ai_service


def args(**kwargs):
    return Namespace(provider="openrouter", allow_paid_openrouter=True, max_cost_usd="0.02", **kwargs)


def test_cases_use_real_prompts_and_include_a_synthetic_context_followup():
    cases = eval_text_ai.build_cases()
    assert [case.command for case in cases] == [
        "maestro", "complotto", "difendi", "accusa", "drama", "dialetto", "insulta", "alduino",
    ]
    assert "CONVERSAZIONE RECENTE DEL GRUPPO" in cases[-1].user
    assert "CONVERSAZIONE RECENTE>>>" in cases[-1].user
    assert "Monster Hunter" in cases[-1].user


@pytest.mark.parametrize("limit", ["0", "-1", "nan", "Infinity", "0.051", "0.000001"])
async def test_paid_eval_rejects_invalid_or_insufficient_limit_before_network(monkeypatch, limit):
    from database import connection

    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "test-key")
    create = AsyncMock()
    paid = AsyncMock()
    monkeypatch.setattr(connection, "create_tables", create)
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", paid)
    configured = args()
    configured.max_cost_usd = limit
    with pytest.raises(ValueError):
        await eval_text_ai.run(configured)
    create.assert_not_awaited()
    paid.assert_not_awaited()


async def test_paid_eval_requires_explicit_opt_in_and_key(monkeypatch):
    configured = args()
    configured.allow_paid_openrouter = False
    with pytest.raises(ValueError, match="requires"):
        await eval_text_ai.run(configured)
    configured.allow_paid_openrouter = True
    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "")
    with pytest.raises(ValueError, match="missing"):
        await eval_text_ai.run(configured)


async def test_paid_eval_accounts_one_run_and_continues_after_provider_failure(monkeypatch, capsys):
    from database import connection

    monkeypatch.setattr(ai_service.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(connection, "create_tables", AsyncMock())
    paid = AsyncMock(side_effect=[ai_service.AIServiceError("secret")] + ["ok"] * 7)
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", paid)
    spend = AsyncMock(return_value=100)
    monkeypatch.setattr(ai_budget, "feature_spend_microusd", spend)
    assert not await eval_text_ai.run(args())
    assert paid.await_count == 8
    features = {call.kwargs["feature"] for call in paid.call_args_list}
    assert len(features) == 1
    spend.assert_awaited_once_with(next(iter(features)))
    assert all(call.kwargs["require_zdr"] for call in paid.call_args_list)
    output = capsys.readouterr().out
    assert "secret" not in output and '"charged_usd": "0.0001"' in output


async def test_free_eval_never_touches_paid_network_or_budget(monkeypatch):
    monkeypatch.setattr(ai_service.settings, "groq_api_key", "test-key")
    free = AsyncMock(return_value="ok")
    paid, spend = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ai_service, "generate_groq_completion", free)
    monkeypatch.setattr(ai_service, "generate_openrouter_completion", paid)
    monkeypatch.setattr(ai_budget, "feature_spend_microusd", spend)
    configured = args()
    configured.provider = "groq"
    assert await eval_text_ai.run(configured)
    assert free.await_count == 8
    paid.assert_not_awaited()
    spend.assert_not_awaited()
