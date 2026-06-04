"""Tests for exceptions/economy.py — attributes and formatted messages."""

from __future__ import annotations

import pytest

from exceptions.economy import (
    AlreadyBetError,
    BettingClosedError,
    DailyAlreadyClaimedError,
    EventAlreadySettledError,
    EventNotFoundError,
    InsufficientFundsError,
    SelfTransferError,
    WalletNotFoundError,
)


class TestInsufficientFundsError:
    def test_attributes(self):
        err = InsufficientFundsError(balance=50, required=200)
        assert err.balance == 50
        assert err.required == 200

    def test_message_contains_values(self):
        err = InsufficientFundsError(balance=50, required=200)
        msg = str(err)
        assert "50" in msg
        assert "200" in msg

    def test_is_exception(self):
        with pytest.raises(InsufficientFundsError):
            raise InsufficientFundsError(0, 1)


class TestWalletNotFoundError:
    def test_attribute(self):
        err = WalletNotFoundError(tg_id=999)
        assert err.tg_id == 999

    def test_message_contains_id(self):
        err = WalletNotFoundError(tg_id=12345)
        assert "12345" in str(err)


class TestDailyAlreadyClaimedError:
    def test_attribute(self):
        err = DailyAlreadyClaimedError(hours_remaining=5.5)
        assert err.hours_remaining == pytest.approx(5.5)

    def test_message_contains_hours(self):
        err = DailyAlreadyClaimedError(hours_remaining=3.25)
        assert "3.2" in str(err)  # formatted to 1 decimal place

    def test_fractional_hours(self):
        err = DailyAlreadyClaimedError(hours_remaining=0.1)
        assert err.hours_remaining == pytest.approx(0.1)


class TestSelfTransferError:
    def test_is_exception(self):
        with pytest.raises(SelfTransferError):
            raise SelfTransferError()

    def test_message_not_empty(self):
        err = SelfTransferError()
        assert len(str(err)) > 0


class TestAlreadyBetError:
    def test_attributes(self):
        err = AlreadyBetError(user_tg_id=111, event_id=5)
        assert err.user_tg_id == 111
        assert err.event_id == 5

    def test_message_contains_ids(self):
        err = AlreadyBetError(user_tg_id=111, event_id=5)
        msg = str(err)
        assert "111" in msg
        assert "5" in msg


class TestBettingClosedError:
    def test_attributes(self):
        err = BettingClosedError(event_id=3, status="locked")
        assert err.event_id == 3
        assert err.status == "locked"

    def test_message(self):
        err = BettingClosedError(event_id=3, status="locked")
        msg = str(err)
        assert "3" in msg
        assert "locked" in msg


class TestEventNotFoundError:
    def test_attribute(self):
        err = EventNotFoundError(event_id=99)
        assert err.event_id == 99

    def test_message(self):
        err = EventNotFoundError(event_id=99)
        assert "99" in str(err)


class TestEventAlreadySettledError:
    def test_attributes(self):
        err = EventAlreadySettledError(event_id=7, status="resolved")
        assert err.event_id == 7
        assert err.status == "resolved"

    def test_message(self):
        err = EventAlreadySettledError(event_id=7, status="resolved")
        msg = str(err)
        assert "7" in msg
        assert "resolved" in msg
