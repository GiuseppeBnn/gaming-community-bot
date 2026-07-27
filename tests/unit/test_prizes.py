"""The shared prize schedule — podium + linearly decreasing consolation.

These two functions were extracted from `quiz_service` so the guess games can
use the same schedule without importing one game from another. The re-export
test is the one that matters: it is what makes the move a *move* and not a fork,
and it is what would fail if someone later deleted the alias.
"""

from __future__ import annotations

import pytest

from services import prizes, quiz_service


class TestReExport:
    def test_quiz_service_still_exposes_them(self):
        """Every existing caller says `quiz_service.consolation_amounts`."""
        assert quiz_service.consolation_amounts is prizes.consolation_amounts
        assert quiz_service.participation_floor is prizes.participation_floor


class TestConsolationAmounts:
    def test_no_finishers_gets_an_empty_schedule(self):
        assert prizes.consolation_amounts(0, 100, 20) == []

    def test_a_single_finisher_gets_the_top_amount(self):
        assert prizes.consolation_amounts(1, 100, 20) == [100]

    def test_the_schedule_decreases_from_top_to_floor(self):
        got = prizes.consolation_amounts(5, 100, 20)
        assert got[0] == 100 and got[-1] == 20
        assert got == sorted(got, reverse=True)

    def test_nobody_ever_gets_less_than_the_floor(self):
        assert all(c >= 20 for c in prizes.consolation_amounts(9, 100, 20))

    def test_a_zero_top_pays_nobody(self):
        """A quiz with no consolation prize must not pay a floor to anyone."""
        assert prizes.consolation_amounts(4, 0, 20) == [0, 0, 0, 0]

    def test_a_floor_above_the_top_is_clamped_to_the_top(self):
        assert prizes.consolation_amounts(3, 50, 999) == [50, 50, 50]

    def test_a_negative_floor_never_pays_a_negative_prize(self):
        assert all(c >= 0 for c in prizes.consolation_amounts(4, 100, -50))


class TestParticipationFloor:
    def test_no_consolation_means_no_floor(self):
        assert prizes.participation_floor(0) == 0

    def test_a_negative_consolation_means_no_floor(self):
        assert prizes.participation_floor(-10) == 0

    def test_the_floor_never_exceeds_the_consolation(self):
        assert prizes.participation_floor(1) <= 1

    @pytest.mark.parametrize("consolation", [10, 100, 1000])
    def test_the_floor_is_a_fraction_of_the_consolation(self, consolation):
        assert 0 < prizes.participation_floor(consolation) <= consolation
