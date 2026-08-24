"""Unit tests for the pure quiz-prize helpers (no DB needed)."""

from __future__ import annotations


from config_data.config import settings
from services.quiz_service import consolation_amounts, participation_floor


class TestConsolationAmounts:
    def test_empty_when_no_one(self):
        assert consolation_amounts(0, 100, 20) == []

    def test_single_finisher_gets_top(self):
        assert consolation_amounts(1, 100, 20) == [100]

    def test_zero_top_pays_nothing(self):
        assert consolation_amounts(5, 0, 20) == [0, 0, 0, 0, 0]

    def test_linear_descent_first_is_top_last_is_floor(self):
        amounts = consolation_amounts(3, 100, 20)
        assert amounts == [100, 60, 20]
        assert amounts[0] == 100
        assert amounts[-1] == 20

    def test_non_increasing(self):
        amounts = consolation_amounts(8, 500, 50)
        assert amounts == sorted(amounts, reverse=True)
        assert amounts[0] == 500
        assert amounts[-1] == 50

    def test_everyone_at_least_floor(self):
        amounts = consolation_amounts(10, 90, 30)
        assert all(a >= 30 for a in amounts)

    def test_floor_above_top_is_clamped(self):
        # Degenerate input: floor must never exceed top.
        amounts = consolation_amounts(3, 50, 200)
        assert all(a <= 50 for a in amounts)
        assert all(a >= 0 for a in amounts)


class TestParticipationFloor:
    def test_zero_consolation_zero_floor(self):
        assert participation_floor(0) == 0

    def test_ratio_applies_when_it_beats_the_floor_min(self):
        # ratio 0.2: round(200 * 0.2) = 40, above floor_min (25) → the ratio wins
        assert participation_floor(200) == 40

    def test_never_below_floor_min(self):
        # round(100 * 0.2) = 20 is below floor_min (25) → the floor_min binds
        assert participation_floor(100) == settings.quiz_participation_floor_min

    def test_never_above_consolation(self):
        assert participation_floor(2) <= 2
