"""Tests for bet_service.compute_payout_preview — pure function, no DB needed."""

from __future__ import annotations

from types import SimpleNamespace


from services.bet_service import compute_payout_preview


def _make_bet(user_tg_id: int, option_id: int, amount: int, status: str = "pending"):
    return SimpleNamespace(
        user_tg_id=user_tg_id,
        option_id=option_id,
        amount=amount,
        status=status,
    )


def _make_event(bets: list):
    return SimpleNamespace(user_bets=bets)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestNoWinners:
    def test_no_bets_at_all(self):
        event = _make_event([])
        result = compute_payout_preview(event, winning_option_id=1)
        assert result["total_pot"] == 0
        assert result["winning_count"] == 0
        assert result["losing_count"] == 0
        assert result["previews"] == []

    def test_all_bets_on_losing_side(self):
        event = _make_event([
            _make_bet(user_tg_id=1, option_id=2, amount=100),
            _make_bet(user_tg_id=2, option_id=2, amount=200),
        ])
        result = compute_payout_preview(event, winning_option_id=1)
        assert result["total_pot"] == 300
        assert result["winning_count"] == 0
        assert result["losing_count"] == 2
        assert result["previews"] == []

    def test_settled_bets_excluded(self):
        event = _make_event([
            _make_bet(user_tg_id=1, option_id=1, amount=100, status="won"),
        ])
        result = compute_payout_preview(event, winning_option_id=1)
        assert result["total_pot"] == 0
        assert result["winning_count"] == 0


# ---------------------------------------------------------------------------
# Single winner
# ---------------------------------------------------------------------------

class TestSingleWinner:
    def test_sole_winner_takes_entire_pot(self):
        event = _make_event([
            _make_bet(user_tg_id=1, option_id=1, amount=300),
            _make_bet(user_tg_id=2, option_id=2, amount=700),
        ])
        result = compute_payout_preview(event, winning_option_id=1)
        assert result["total_pot"] == 1000
        assert result["winning_count"] == 1
        assert result["losing_count"] == 1
        assert len(result["previews"]) == 1
        assert result["previews"][0]["payout"] == 1000

    def test_only_winners_no_losers(self):
        event = _make_event([
            _make_bet(user_tg_id=1, option_id=1, amount=500),
        ])
        result = compute_payout_preview(event, winning_option_id=1)
        assert result["previews"][0]["payout"] == 500


# ---------------------------------------------------------------------------
# Multiple winners — proportional split
# ---------------------------------------------------------------------------

class TestMultipleWinners:
    def test_equal_bets_split_evenly(self):
        event = _make_event([
            _make_bet(user_tg_id=1, option_id=1, amount=100),
            _make_bet(user_tg_id=2, option_id=1, amount=100),
            _make_bet(user_tg_id=3, option_id=2, amount=200),  # loser
        ])
        result = compute_payout_preview(event, winning_option_id=1)
        pot = 400
        # Both winners bet 100 (equal), so each gets 200
        payouts = sorted(p["payout"] for p in result["previews"])
        assert sum(payouts) == pot
        assert payouts[0] == 200
        assert payouts[1] == 200

    def test_proportional_split(self):
        # Winner pool: 600 (400 + 200), total pot: 1000
        event = _make_event([
            _make_bet(user_tg_id=1, option_id=1, amount=400),
            _make_bet(user_tg_id=2, option_id=1, amount=200),
            _make_bet(user_tg_id=3, option_id=2, amount=400),  # loser
        ])
        result = compute_payout_preview(event, winning_option_id=1)
        payouts = {p["tg_id"]: p["payout"] for p in result["previews"]}
        assert sum(payouts.values()) == 1000

        # User 1 bet 400/600 = 2/3 of pool → ~667
        # User 2 bet 200/600 = 1/3 of pool → ~333
        assert payouts[1] + payouts[2] == 1000
        assert payouts[1] > payouts[2]

    def test_total_always_equals_pot(self):
        for amounts in [[50, 100, 250], [1, 2, 3, 4], [999, 1], [333, 333, 334]]:
            winning_amounts = amounts[:-1]
            losing_amount = amounts[-1]
            bets = [_make_bet(i + 1, 1, a) for i, a in enumerate(winning_amounts)]
            bets.append(_make_bet(99, 2, losing_amount))
            event = _make_event(bets)
            result = compute_payout_preview(event, winning_option_id=1)
            assert sum(p["payout"] for p in result["previews"]) == sum(amounts)

    def test_biggest_winner_gets_rounding_leftover(self):
        # 1000 pot, winning pool = 3 (1+1+1), each should get 333 → total 999
        # biggest winner (all equal, first in sort) gets the leftover 1
        event = _make_event([
            _make_bet(user_tg_id=1, option_id=1, amount=1),
            _make_bet(user_tg_id=2, option_id=1, amount=1),
            _make_bet(user_tg_id=3, option_id=1, amount=1),
            _make_bet(user_tg_id=4, option_id=2, amount=997),  # loser
        ])
        result = compute_payout_preview(event, winning_option_id=1)
        total_payout = sum(p["payout"] for p in result["previews"])
        assert total_payout == 1000


# ---------------------------------------------------------------------------
# Preview structure
# ---------------------------------------------------------------------------

class TestPreviewStructure:
    def test_preview_dict_has_required_keys(self):
        event = _make_event([
            _make_bet(user_tg_id=5, option_id=1, amount=100),
        ])
        result = compute_payout_preview(event, winning_option_id=1)
        preview = result["previews"][0]
        assert "tg_id" in preview
        assert "bet_amount" in preview
        assert "payout" in preview

    def test_result_dict_has_required_keys(self):
        event = _make_event([])
        result = compute_payout_preview(event, winning_option_id=1)
        assert "total_pot" in result
        assert "winning_count" in result
        assert "losing_count" in result
        assert "winning_pool" in result
        assert "previews" in result
