from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from services import ai_budget


@pytest.mark.pg
async def test_two_paid_calls_cannot_both_cross_the_monthly_cap(
    pg_sessions, monkeypatch,
):
    monkeypatch.setattr(ai_budget, "async_session_maker", pg_sessions)
    monkeypatch.setattr(ai_budget.settings, "openrouter_max_prompt_price", Decimal("0.25"))
    monkeypatch.setattr(ai_budget.settings, "openrouter_max_completion_price", Decimal("0.60"))
    estimate = ai_budget.estimate_cost_microusd(
        ai_budget.estimate_input_tokens("s", "u"),
        10,
        max_prompt_price=Decimal("0.25"),
        max_completion_price=Decimal("0.60"),
    )
    monkeypatch.setattr(
        ai_budget.settings, "ai_monthly_budget_usd", Decimal(estimate) / Decimal("1000000"),
    )

    async def attempt():
        return await ai_budget.reserve(
            feature="race",
            provider="openrouter",
            requested_model="deepseek/a",
            system_prompt="s",
            user_text="u",
            max_output_tokens=10,
        )

    outcomes = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert sum(isinstance(item, ai_budget.Reservation) for item in outcomes) == 1
    assert sum(isinstance(item, ai_budget.AIBudgetExceeded) for item in outcomes) == 1
