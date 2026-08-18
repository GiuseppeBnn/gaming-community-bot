from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from services import ai_budget


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
