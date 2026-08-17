from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import AIUsageLog
from services import ai_budget


@pytest.fixture
def budget_db(engine, monkeypatch):
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False,
    )
    monkeypatch.setattr(ai_budget, "async_session_maker", factory)
    monkeypatch.setattr(ai_budget.settings, "ai_monthly_budget_usd", Decimal("0.001"))
    monkeypatch.setattr(ai_budget.settings, "openrouter_max_prompt_price", Decimal("0.25"))
    monkeypatch.setattr(ai_budget.settings, "openrouter_max_completion_price", Decimal("0.60"))
    return factory


async def _reserve():
    return await ai_budget.reserve(
        feature="chat",
        provider="openrouter",
        requested_model="deepseek/a",
        system_prompt="s",
        user_text="u",
        max_output_tokens=10,
    )


async def test_reservation_settlement_and_snapshot_are_persistent(budget_db):
    reservation = await _reserve()
    before = await ai_budget.snapshot()
    assert before is not None
    assert before.spent_microusd == 0
    assert before.reserved_microusd == reservation.estimated_microusd

    await ai_budget.settle(
        reservation,
        status="completed",
        actual_microusd=7,
        metrics=ai_budget.UsageMetrics("deepseek/actual", 10, 2, 0, 4),
    )
    # Idempotent: a repeated callback cannot charge twice.
    await ai_budget.settle(reservation, status="completed", actual_microusd=999)
    after = await ai_budget.snapshot()
    assert after is not None
    assert after.spent_microusd == 7 and after.reserved_microusd == 0
    async with budget_db() as session:
        row = (await session.execute(select(AIUsageLog))).scalar_one()
    assert row.actual_model == "deepseek/actual"
    assert row.prompt_tokens == 10 and row.cached_tokens == 4
    assert row.status == "completed" and row.actual_microusd == 7


async def test_unknown_period_has_no_snapshot(budget_db):
    assert await ai_budget.snapshot("1999-01") is None


async def test_unknown_outcome_is_charged_and_hard_cap_blocks_next_call(budget_db):
    reservation = await _reserve()
    await ai_budget.settle(reservation, status="uncertain", actual_microusd=None)
    with pytest.raises(ai_budget.AIBudgetExceeded):
        # A deliberately huge request cannot be reserved after the first charge.
        await ai_budget.reserve(
            feature="chat",
            provider="openrouter",
            requested_model="deepseek/a",
            system_prompt="x" * 10_000,
            user_text="u",
            max_output_tokens=1000,
        )


async def test_zero_cap_explicitly_disables_local_ledger(monkeypatch):
    monkeypatch.setattr(ai_budget.settings, "ai_monthly_budget_usd", Decimal("0"))
    reservation = await _reserve()
    assert reservation.tracked is False
    await ai_budget.settle(reservation, status="completed", actual_microusd=10)
