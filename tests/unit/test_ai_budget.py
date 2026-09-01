from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker

from services import ai_budget
from database.models import AIUsageLog


def test_period_and_currency_conversion_are_deterministic():
    assert ai_budget.current_period(datetime(2026, 8, 31, 23, tzinfo=UTC)) == "2026-08"
    assert ai_budget.usd_to_microusd(Decimal("5.009999")) == 5_009_999


def test_token_and_price_estimates_are_conservative():
    tokens = ai_budget.estimate_input_tokens("a", "🐲")
    assert tokens == 1 + len("🐲".encode()) + ai_budget._TOKEN_OVERHEAD
    assert ai_budget.estimate_cost_microusd(
        10_000,
        1_000,
        max_prompt_price=Decimal("0.03"),
        max_completion_price=Decimal("0.13"),
    ) == 430


async def test_feature_spend_isolated_to_run_and_charges_reserved_rows(engine, monkeypatch):
    """Filtering only by shared lane would include a concurrent game's spend in an eval report."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(ai_budget, "async_session_maker", factory)
    async with factory.begin() as session:
        session.add_all([
            AIUsageLog(
                request_id="a" * 36,
                period="2026-09",
                feature="eval-twentyq-a1b2c3d4e5f6",
                provider="openrouter",
                requested_model="test",
                status="completed",
                reserved_microusd=50,
                actual_microusd=19,
            ),
            AIUsageLog(
                request_id="b" * 36,
                period="2026-09",
                feature="eval-twentyq-a1b2c3d4e5f6",
                provider="openrouter",
                requested_model="test",
                status="reserved",
                reserved_microusd=13,
            ),
            AIUsageLog(
                request_id="c" * 36,
                period="2026-09",
                feature="twentyq_question",
                provider="openrouter",
                requested_model="test",
                status="completed",
                reserved_microusd=900,
                actual_microusd=900,
            ),
            AIUsageLog(
                request_id="d" * 36,
                period="2026-10",
                feature="eval-twentyq-a1b2c3d4e5f6",
                provider="openrouter",
                requested_model="test",
                status="completed",
                reserved_microusd=9,
                actual_microusd=5,
            ),
        ])

    assert await ai_budget.feature_spend_microusd("eval-twentyq-a1b2c3d4e5f6") == 37
    assert await ai_budget.feature_spend_microusd("eval-twentyq-a1b2c3d4e5f6", "2026-09") == 32
