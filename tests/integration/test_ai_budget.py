from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import AIBudgetPeriod, AIFeatureBudgetPeriod, AIUsageLog
from services import ai_budget


@pytest.fixture
def budget_db(engine, monkeypatch):
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False,
    )
    monkeypatch.setattr(ai_budget, "async_session_maker", factory)
    monkeypatch.setattr(ai_budget.settings, "ai_monthly_budget_usd", Decimal("0.001"))
    monkeypatch.setattr(
        ai_budget.settings, "twentyq_openrouter_budget_usd", Decimal("0.001"),
    )
    monkeypatch.setattr(
        ai_budget.settings, "openrouter_other_budget_usd", Decimal("0.001"),
    )
    monkeypatch.setattr(ai_budget.settings, "openrouter_max_prompt_price", Decimal("0.25"))
    monkeypatch.setattr(ai_budget.settings, "openrouter_max_completion_price", Decimal("0.60"))
    return factory


async def _reserve(
    *, feature: str = "chat", budget_lane: str = "openrouter_other",
):
    return await ai_budget.reserve(
        feature=feature,
        budget_lane=budget_lane,
        provider="openrouter",
        requested_model="deepseek/a",
        system_prompt="s",
        user_text="u",
        max_output_tokens=10,
    )


async def test_reservation_settlement_and_snapshot_are_persistent(budget_db):
    reservation = await _reserve(feature="alduino_chat")
    before = await ai_budget.snapshot()
    lane_before = await ai_budget.feature_snapshot("openrouter_other")
    assert before is not None
    assert lane_before is not None
    assert reservation.feature == "alduino_chat"
    assert reservation.budget_lane == "openrouter_other"
    assert before.spent_microusd == 0
    assert before.reserved_microusd == reservation.estimated_microusd
    assert lane_before.spent_microusd == 0
    assert lane_before.reserved_microusd == reservation.estimated_microusd

    await ai_budget.settle(
        reservation,
        status="completed",
        actual_microusd=7,
        metrics=ai_budget.UsageMetrics("deepseek/actual", 10, 2, 0, 4),
    )
    # Idempotent: a repeated callback cannot charge twice.
    await ai_budget.settle(reservation, status="completed", actual_microusd=999)
    after = await ai_budget.snapshot()
    lane_after = await ai_budget.feature_snapshot("openrouter_other")
    assert after is not None
    assert lane_after is not None
    assert after.spent_microusd == 7 and after.reserved_microusd == 0
    assert lane_after.spent_microusd == 7 and lane_after.reserved_microusd == 0
    async with budget_db() as session:
        row = (await session.execute(select(AIUsageLog))).scalar_one()
    assert row.feature == "alduino_chat"
    assert row.actual_model == "deepseek/actual"
    assert row.prompt_tokens == 10 and row.cached_tokens == 4
    assert row.status == "completed" and row.actual_microusd == 7


async def test_unknown_period_has_no_snapshot(budget_db):
    assert await ai_budget.snapshot("1999-01") is None
    assert await ai_budget.feature_snapshot("twentyq", "1999-01") is None


async def test_unknown_outcome_is_charged_and_hard_cap_blocks_next_call(budget_db):
    reservation = await _reserve()
    await ai_budget.settle(reservation, status="uncertain", actual_microusd=None)
    global_state = await ai_budget.snapshot()
    lane_state = await ai_budget.feature_snapshot("openrouter_other")
    assert global_state is not None
    assert lane_state is not None
    assert global_state.spent_microusd == reservation.estimated_microusd
    assert global_state.reserved_microusd == 0
    assert lane_state.spent_microusd == reservation.estimated_microusd
    assert lane_state.reserved_microusd == 0
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


@pytest.mark.parametrize(
    ("global_cap", "twentyq_cap", "other_cap", "lane"),
    [
        (Decimal("0"), Decimal("0"), Decimal("0"), "twentyq"),
        (Decimal("5"), Decimal("0"), Decimal("1"), "twentyq"),
        (Decimal("5"), Decimal("4"), Decimal("0"), "openrouter_other"),
    ],
)
async def test_zero_cap_disables_paid_lane_before_usage_log(
    budget_db, monkeypatch, global_cap, twentyq_cap, other_cap, lane,
):
    monkeypatch.setattr(ai_budget.settings, "ai_monthly_budget_usd", global_cap)
    monkeypatch.setattr(ai_budget.settings, "twentyq_openrouter_budget_usd", twentyq_cap)
    monkeypatch.setattr(ai_budget.settings, "openrouter_other_budget_usd", other_cap)
    with pytest.raises(ai_budget.AIBudgetExceeded):
        await _reserve(
            feature="twentyq_question" if lane == "twentyq" else "alduino_chat",
            budget_lane=lane,
        )
    async with budget_db() as session:
        count = (await session.execute(select(func.count()).select_from(AIUsageLog))).scalar_one()
    assert count == 0


async def test_lane_rejection_rolls_back_the_global_reservation(budget_db, monkeypatch):
    estimate = ai_budget.estimate_cost_microusd(
        ai_budget.estimate_input_tokens("s", "u"),
        10,
        max_prompt_price=Decimal("0.25"),
        max_completion_price=Decimal("0.60"),
    )
    monkeypatch.setattr(
        ai_budget.settings,
        "ai_monthly_budget_usd",
        Decimal(estimate) / Decimal("1000000"),
    )
    monkeypatch.setattr(
        ai_budget.settings, "twentyq_openrouter_budget_usd", Decimal("0.000001"),
    )
    async with budget_db.begin() as session:
        session.add(AIBudgetPeriod(
            period=ai_budget.current_period(), cap_microusd=estimate,
        ))
        session.add(AIFeatureBudgetPeriod(
            period=ai_budget.current_period(), feature="twentyq", cap_microusd=1,
        ))

    with pytest.raises(ai_budget.AIBudgetExceeded):
        await _reserve(feature="twentyq_question", budget_lane="twentyq")

    async with budget_db() as session:
        global_row = await session.get(AIBudgetPeriod, ai_budget.current_period())
        lane_row = await session.get(
            AIFeatureBudgetPeriod, (ai_budget.current_period(), "twentyq"),
        )
        count = (await session.execute(select(func.count()).select_from(AIUsageLog))).scalar_one()
    assert global_row is not None and global_row.reserved_microusd == 0
    assert lane_row is not None and lane_row.reserved_microusd == 0
    assert count == 0


async def test_configured_caps_are_refreshed_on_each_reservation(budget_db, monkeypatch):
    first = await _reserve()
    await ai_budget.settle(first, status="failed", actual_microusd=0)
    exact_cap = Decimal(first.estimated_microusd) / Decimal("1000000")
    monkeypatch.setattr(ai_budget.settings, "ai_monthly_budget_usd", exact_cap)
    monkeypatch.setattr(ai_budget.settings, "openrouter_other_budget_usd", exact_cap)

    second = await _reserve()
    global_state = await ai_budget.snapshot()
    lane_state = await ai_budget.feature_snapshot("openrouter_other")

    assert global_state is not None and global_state.cap_microusd == second.estimated_microusd
    assert lane_state is not None and lane_state.cap_microusd == second.estimated_microusd
    with pytest.raises(ai_budget.AIBudgetExceeded):
        await _reserve()


async def test_budget_lanes_have_distinct_snapshots(budget_db):
    twentyq = await _reserve(feature="twentyq_question", budget_lane="twentyq")
    other = await _reserve(feature="alduino_chat")

    twentyq_state = await ai_budget.feature_snapshot("twentyq")
    other_state = await ai_budget.feature_snapshot("openrouter_other")

    assert twentyq_state is not None
    assert other_state is not None
    assert twentyq_state.reserved_microusd == twentyq.estimated_microusd
    assert other_state.reserved_microusd == other.estimated_microusd
