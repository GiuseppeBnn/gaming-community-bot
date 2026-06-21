"""Unit tests for handlers.betting.parse_bet_amount — custom bet-amount input.

The custom-amount path previously only checked ``> 0``; a pathological value would
overflow the confirm button's callback_data (Telegram caps it at 64 bytes). The
validator now rejects non-numeric / non-positive / over-cap input.
"""

from __future__ import annotations

from handlers import betting
from keyboards.betting_kb import get_confirm_bet_keyboard


class TestParseBetAmount:
    def test_valid_amount(self):
        amount, err = betting.parse_bet_amount("150")
        assert amount == 150
        assert err is None

    def test_strips_whitespace(self):
        amount, err = betting.parse_bet_amount("  42 ")
        assert amount == 42 and err is None

    def test_non_numeric_rejected(self):
        amount, err = betting.parse_bet_amount("tanto")
        assert amount is None and err is not None

    def test_empty_rejected(self):
        amount, err = betting.parse_bet_amount("")
        assert amount is None and err is not None

    def test_zero_and_negative_rejected(self):
        for raw in ("0", "-5"):
            amount, err = betting.parse_bet_amount(raw)
            assert amount is None and err is not None

    def test_over_cap_rejected(self):
        amount, err = betting.parse_bet_amount(str(betting.MAX_BET + 1))
        assert amount is None and err is not None

    def test_at_cap_accepted(self):
        amount, err = betting.parse_bet_amount(str(betting.MAX_BET))
        assert amount == betting.MAX_BET and err is None

    def test_huge_string_rejected_without_crash(self):
        # A 30-digit number parses as an int but is far over the cap — must be
        # rejected, never propagated into a keyboard's callback_data.
        amount, err = betting.parse_bet_amount("9" * 30)
        assert amount is None and err is not None


def test_confirm_callback_data_within_telegram_limit_at_cap():
    # The whole point of the cap: the confirm button's callback_data stays within
    # Telegram's 64-byte limit even with large event/option ids and the max amount.
    kb = get_confirm_bet_keyboard(999_999_999, 999_999_999, betting.MAX_BET)
    cb = kb.inline_keyboard[0][0].callback_data
    assert cb.startswith("bet_confirm:")
    assert len(cb.encode("utf-8")) <= 64
