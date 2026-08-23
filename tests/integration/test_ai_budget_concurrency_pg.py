from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from database.models import AIBudgetPeriod, AIFeatureBudgetPeriod
from services import ai_budget


def _usd(microusd: int) -> Decimal:
    return Decimal(microusd) / Decimal("1000000")


async def _seed_periods(pg_sessions, *, global_cap: int, lane_caps: dict[str, int]) -> None:
    async with pg_sessions.begin() as session:
        session.add(AIBudgetPeriod(period=ai_budget.current_period(), cap_microusd=global_cap))
        session.add_all([
            AIFeatureBudgetPeriod(
                period=ai_budget.current_period(), feature=lane, cap_microusd=cap,
            )
            for lane, cap in lane_caps.items()
        ])


def _synchronize_updates(monkeypatch) -> None:
    barrier = asyncio.Barrier(2)
    original = ai_budget._ensure_periods

    async def synchronized(session, period, global_cap, lane, lane_cap):
        await original(session, period, global_cap, lane, lane_cap)
        await barrier.wait()

    monkeypatch.setattr(ai_budget, "_ensure_periods", synchronized)


@pytest.mark.pg
async def test_same_lane_requests_cannot_cross_the_lane_cap(
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
    await _seed_periods(
        pg_sessions, global_cap=estimate * 2, lane_caps={"twentyq": estimate},
    )
    monkeypatch.setattr(ai_budget.settings, "ai_monthly_budget_usd", _usd(estimate * 2))
    monkeypatch.setattr(
        ai_budget.settings, "twentyq_openrouter_budget_usd", _usd(estimate),
    )
    monkeypatch.setattr(
        ai_budget.settings, "openrouter_other_budget_usd", _usd(estimate),
    )
    _synchronize_updates(monkeypatch)

    async def attempt():
        return await ai_budget.reserve(
            feature="twentyq_question",
            budget_lane="twentyq",
            provider="openrouter",
            requested_model="deepseek/a",
            system_prompt="s",
            user_text="u",
            max_output_tokens=10,
        )

    outcomes = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert sum(isinstance(item, ai_budget.Reservation) for item in outcomes) == 1
    assert sum(isinstance(item, ai_budget.AIBudgetExceeded) for item in outcomes) == 1
    global_state = await ai_budget.snapshot()
    lane_state = await ai_budget.feature_snapshot("twentyq")
    assert global_state is not None and global_state.reserved_microusd == estimate
    assert lane_state is not None and lane_state.reserved_microusd == estimate


@pytest.mark.pg
async def test_different_lanes_compete_on_global_cap_without_deadlock(
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
    await _seed_periods(
        pg_sessions,
        global_cap=estimate,
        lane_caps={"twentyq": estimate, "openrouter_other": estimate},
    )
    monkeypatch.setattr(ai_budget.settings, "ai_monthly_budget_usd", _usd(estimate))
    monkeypatch.setattr(
        ai_budget.settings, "twentyq_openrouter_budget_usd", _usd(estimate),
    )
    monkeypatch.setattr(
        ai_budget.settings, "openrouter_other_budget_usd", _usd(estimate),
    )
    _synchronize_updates(monkeypatch)

    async def attempt(lane: str):
        return await ai_budget.reserve(
            feature="twentyq_question" if lane == "twentyq" else "alduino_chat",
            budget_lane=lane,
            provider="openrouter",
            requested_model="deepseek/a",
            system_prompt="s",
            user_text="u",
            max_output_tokens=10,
        )

    outcomes = await asyncio.gather(
        attempt("twentyq"), attempt("openrouter_other"), return_exceptions=True,
    )
    assert sum(isinstance(item, ai_budget.Reservation) for item in outcomes) == 1
    assert sum(isinstance(item, ai_budget.AIBudgetExceeded) for item in outcomes) == 1
    global_state = await ai_budget.snapshot()
    assert global_state is not None and global_state.reserved_microusd == estimate
