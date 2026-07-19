"""Integration tests for services/economy_service.py.

Each test gets a fresh in-memory SQLite DB (via the `session` + `engine` fixtures
from conftest.py). Service functions never commit — the test commits after calling them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import services.economy_service as eco
from config_data.config import settings
from database.models import LedgerEntry, TransactionType
from exceptions.economy import (
    DailyAlreadyClaimedError,
    InsufficientFundsError,
    SelfTransferError,
    WalletNotFoundError,
)
from utils import daytime

_ROME = ZoneInfo("Europe/Rome")


def _utc_of(local: str) -> datetime:
    """Rome wall-clock ``"YYYY-MM-DD HH:MM"`` → naive UTC (the storage format).

    The /daily rules are about *local* midnight, so the tests are written in the
    wall-clock terms a member actually experiences and converted here.
    """
    return (
        datetime.strptime(local, "%Y-%m-%d %H:%M")
        .replace(tzinfo=_ROME)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


# ---------------------------------------------------------------------------
# credit
# ---------------------------------------------------------------------------

class TestCredit:
    async def test_increases_wallet_balance(self, session, user_factory):
        _, wallet = await user_factory(tg_id=1, coins=0)

        await eco.credit(session, 1, 500, TransactionType.deposit, "test")
        await session.commit()

        assert wallet.coins == 500

    async def test_creates_ledger_entry(self, session, user_factory):
        await user_factory(tg_id=1, coins=0)

        await eco.credit(session, 1, 200, TransactionType.admin_credit, "bonus")
        await session.commit()

        from sqlalchemy import select
        entries = list((await session.execute(select(LedgerEntry))).scalars())
        assert len(entries) == 1
        assert entries[0].to_tg_id == 1
        assert entries[0].amount == 200
        assert entries[0].tx_type == TransactionType.admin_credit.value

    async def test_ledger_truncates_long_description(self, session, user_factory):
        await user_factory(tg_id=1, coins=0)
        long_desc = "x" * 600

        await eco.credit(session, 1, 1, TransactionType.deposit, long_desc)
        await session.commit()

        from sqlalchemy import select
        entry = (await session.execute(select(LedgerEntry))).scalar_one()
        assert len(entry.description) == 512

    async def test_raises_wallet_not_found(self, session):
        with pytest.raises(WalletNotFoundError):
            await eco.credit(session, 9999, 100, TransactionType.deposit, "x")

    async def test_accumulates_multiple_credits(self, session, user_factory):
        _, wallet = await user_factory(tg_id=1, coins=100)

        await eco.credit(session, 1, 50, TransactionType.deposit, "a")
        await eco.credit(session, 1, 50, TransactionType.deposit, "b")
        await session.commit()

        assert wallet.coins == 200


# ---------------------------------------------------------------------------
# debit
# ---------------------------------------------------------------------------

class TestDebit:
    async def test_decreases_wallet_balance(self, session, user_factory):
        _, wallet = await user_factory(tg_id=1, coins=1000)

        await eco.debit(session, 1, 300, TransactionType.withdrawal, "test")
        await session.commit()

        assert wallet.coins == 700

    async def test_creates_negative_ledger_entry(self, session, user_factory):
        await user_factory(tg_id=1, coins=1000)

        await eco.debit(session, 1, 400, TransactionType.bet_placed, "bet")
        await session.commit()

        from sqlalchemy import select
        entry = (await session.execute(select(LedgerEntry))).scalar_one()
        assert entry.from_tg_id == 1
        assert entry.amount == -400

    async def test_raises_insufficient_funds(self, session, user_factory):
        await user_factory(tg_id=1, coins=50)

        with pytest.raises(InsufficientFundsError) as exc:
            await eco.debit(session, 1, 100, TransactionType.withdrawal, "x")

        assert exc.value.balance == 50
        assert exc.value.required == 100

    async def test_exact_balance_is_allowed(self, session, user_factory):
        _, wallet = await user_factory(tg_id=1, coins=100)

        await eco.debit(session, 1, 100, TransactionType.withdrawal, "x")
        await session.commit()

        assert wallet.coins == 0

    async def test_raises_wallet_not_found(self, session):
        with pytest.raises(WalletNotFoundError):
            await eco.debit(session, 9999, 50, TransactionType.withdrawal, "x")


# ---------------------------------------------------------------------------
# get_balance
# ---------------------------------------------------------------------------

class TestGetBalance:
    async def test_returns_current_balance(self, session, user_factory):
        await user_factory(tg_id=1, coins=750)
        assert await eco.get_balance(session, 1) == 750

    async def test_raises_wallet_not_found_for_unknown_user(self, session):
        with pytest.raises(WalletNotFoundError):
            await eco.get_balance(session, 9999)

    async def test_reflects_credit(self, session, user_factory):
        await user_factory(tg_id=1, coins=0)
        await eco.credit(session, 1, 300, TransactionType.deposit, "x")
        await session.commit()
        assert await eco.get_balance(session, 1) == 300


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------

class TestTransfer:
    async def test_moves_coins_between_wallets(self, session, user_factory):
        _, w1 = await user_factory(tg_id=1, coins=1000)
        _, w2 = await user_factory(tg_id=2, coins=0)

        await eco.transfer(session, from_tg_id=1, to_tg_id=2, amount=400)
        await session.commit()

        assert w1.coins == 600
        assert w2.coins == 400

    async def test_creates_two_ledger_entries(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        await user_factory(tg_id=2, coins=0)

        await eco.transfer(session, 1, 2, 100)
        await session.commit()

        from sqlalchemy import select
        entries = list((await session.execute(select(LedgerEntry))).scalars())
        assert len(entries) == 2
        types = {e.tx_type for e in entries}
        assert TransactionType.transfer_out.value in types
        assert TransactionType.transfer_in.value in types

    async def test_increments_transfers_made(self, session, user_factory):
        user1, _ = await user_factory(tg_id=1, coins=500)
        await user_factory(tg_id=2, coins=0)

        await eco.transfer(session, 1, 2, 100)
        await session.commit()

        assert user1.transfers_made == 1

    async def test_names_appear_in_ledger_descriptions(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        await user_factory(tg_id=2, coins=0)

        await eco.transfer(
            session, 1, 2, 100, from_name="@mittente", to_name="@destinatario"
        )
        await session.commit()

        from sqlalchemy import select
        entries = list((await session.execute(select(LedgerEntry))).scalars())
        by_type = {e.tx_type: e.description for e in entries}
        assert by_type[TransactionType.transfer_out.value] == "Trasferimento a @destinatario"
        assert by_type[TransactionType.transfer_in.value] == "Trasferimento da @mittente"

    async def test_falls_back_to_ids_without_names(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        await user_factory(tg_id=2, coins=0)

        await eco.transfer(session, 1, 2, 100)  # no names supplied
        await session.commit()

        from sqlalchemy import select
        entries = list((await session.execute(select(LedgerEntry))).scalars())
        by_type = {e.tx_type: e.description for e in entries}
        assert by_type[TransactionType.transfer_out.value] == "Trasferimento a 2"
        assert by_type[TransactionType.transfer_in.value] == "Trasferimento da 1"

    async def test_raises_self_transfer_error(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)

        with pytest.raises(SelfTransferError):
            await eco.transfer(session, 1, 1, 100)

    async def test_raises_insufficient_funds(self, session, user_factory):
        await user_factory(tg_id=1, coins=50)
        await user_factory(tg_id=2, coins=0)

        with pytest.raises(InsufficientFundsError):
            await eco.transfer(session, 1, 2, 100)

    async def test_raises_wallet_not_found_for_unknown_recipient(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)

        with pytest.raises(WalletNotFoundError):
            await eco.transfer(session, 1, 9999, 100)

    async def test_raises_value_error_for_zero_amount(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        await user_factory(tg_id=2, coins=0)

        with pytest.raises(ValueError):
            await eco.transfer(session, 1, 2, 0)

    async def test_raises_value_error_for_over_max(self, session, user_factory):
        await user_factory(tg_id=1, coins=2_000_000)
        await user_factory(tg_id=2, coins=0)

        with pytest.raises(ValueError):
            await eco.transfer(session, 1, 2, 1_000_001)


# ---------------------------------------------------------------------------
# claim_daily
# ---------------------------------------------------------------------------

class TestClaimDaily:
    @pytest.fixture(autouse=True)
    def _pin_env(self, monkeypatch):
        """Hermetic: never let a local .env change the window under test."""
        monkeypatch.setattr(settings, "scheduler_timezone", "Europe/Rome")
        monkeypatch.setattr(settings, "daily_min_hours", 6)

    @pytest.fixture
    def freeze_now(self, monkeypatch):
        """Pin ``daytime.utc_now()`` to a Rome wall-clock instant."""
        def _freeze(local: str) -> None:
            monkeypatch.setattr(daytime, "utc_now", lambda: _utc_of(local))
        return _freeze

    async def test_first_claim_credits_reward(self, session, user_factory):
        _, wallet = await user_factory(tg_id=1, coins=0)

        reward, streak = await eco.claim_daily(session, 1)
        await session.commit()

        assert reward == settings.daily_reward_coins
        assert wallet.coins == settings.daily_reward_coins

    async def test_first_claim_sets_streak_to_one(self, session, user_factory):
        user, _ = await user_factory(tg_id=1, coins=0)

        _, streak = await eco.claim_daily(session, 1)
        assert streak == 1
        assert user.daily_streak == 1

    # -- claim window: one per LOCAL calendar day AND >= daily_min_hours apart --

    async def test_same_day_retry_blocked_even_past_the_min_gap(
        self, session, user_factory, freeze_now
    ):
        """Regression guard for the "claim every 6h" bug.

        The minimum gap must never be a *second way in*: here 7h50 have elapsed
        (well past the 6h gap) but it is still the same calendar day → blocked.
        Had the two rules been ORed instead of ANDed, this would pass.
        """
        user, _ = await user_factory(tg_id=1, coins=0)
        user.last_daily_claim = _utc_of("2026-06-15 00:10")
        await session.commit()
        freeze_now("2026-06-15 08:00")

        with pytest.raises(DailyAlreadyClaimedError):
            await eco.claim_daily(session, 1)

    async def test_blocked_just_after_midnight_when_min_gap_not_elapsed(
        self, session, user_factory, freeze_now
    ):
        """A 23:00 claim must not be followed by another one at 00:30."""
        user, _ = await user_factory(tg_id=1, coins=0)
        user.last_daily_claim = _utc_of("2026-06-15 23:00")
        await session.commit()
        freeze_now("2026-06-16 00:30")

        with pytest.raises(DailyAlreadyClaimedError) as exc:
            await eco.claim_daily(session, 1)

        # Next slot is 05:00 (23:00 + 6h), i.e. 4h30 away — not "24h - elapsed".
        assert exc.value.seconds_remaining == int(timedelta(hours=4, minutes=30).total_seconds())

    async def test_allowed_after_midnight_once_min_gap_elapsed(
        self, session, user_factory, freeze_now
    ):
        user, wallet = await user_factory(tg_id=1, coins=0)
        user.last_daily_claim = _utc_of("2026-06-15 23:00")
        await session.commit()
        freeze_now("2026-06-16 05:00")

        reward, _ = await eco.claim_daily(session, 1)
        await session.commit()
        assert wallet.coins == reward

    async def test_allowed_right_after_midnight_when_previous_claim_was_early(
        self, session, user_factory, freeze_now
    ):
        """Claimed at 08:00, so by 00:01 the next day both rules are satisfied."""
        user, _ = await user_factory(tg_id=1, coins=0)
        user.last_daily_claim = _utc_of("2026-06-15 08:00")
        await session.commit()
        freeze_now("2026-06-16 00:01")

        reward, _ = await eco.claim_daily(session, 1)
        assert reward == settings.daily_reward_coins

    async def test_midnight_boundary_is_local_not_utc(
        self, session, user_factory, freeze_now
    ):
        """00:30 local on 16 June is still 22:30 UTC on the 15th (CEST, +2).

        A UTC-based day comparison would see the same UTC day as the 08:00 claim
        and refuse; the local-midnight rule correctly allows it.
        """
        user, _ = await user_factory(tg_id=1, coins=0)
        user.last_daily_claim = _utc_of("2026-06-15 08:00")
        user.daily_streak = 2
        await session.commit()

        now = _utc_of("2026-06-16 00:30")
        # Same UTC day (both fall on the 15th) → a UTC rule would refuse...
        assert user.last_daily_claim.date() == now.date()
        # ...but they are different LOCAL days, which is what must count.
        assert daytime.local_day(user.last_daily_claim) != daytime.local_day(now)

        freeze_now("2026-06-16 00:30")
        _, streak = await eco.claim_daily(session, 1)
        assert streak == 3

    async def test_min_gap_can_never_cost_a_day(self):
        """The latest possible claim still leaves the whole next day available."""
        latest = _utc_of("2026-06-15 23:59")
        next_allowed = max(
            daytime.next_local_midnight(latest),
            latest + timedelta(hours=settings.daily_min_hours),
        )
        assert daytime.local_day(next_allowed) == daytime.local_day(latest) + timedelta(days=1)

    # -- streak: continues only if the previous claim was YESTERDAY --

    async def test_streak_increments_on_consecutive_days(
        self, session, user_factory, freeze_now
    ):
        user, _ = await user_factory(tg_id=1, coins=0)
        user.last_daily_claim = _utc_of("2026-06-15 10:00")
        user.daily_streak = 3
        await session.commit()
        freeze_now("2026-06-16 10:00")

        _, streak = await eco.claim_daily(session, 1)
        assert streak == 4
        assert user.daily_streak == 4

    async def test_streak_resets_when_a_day_is_skipped(
        self, session, user_factory, freeze_now
    ):
        """Missed the whole 16th → the streak is gone, exactly as intended."""
        user, _ = await user_factory(tg_id=1, coins=0)
        user.last_daily_claim = _utc_of("2026-06-15 10:00")
        user.daily_streak = 5
        await session.commit()
        freeze_now("2026-06-17 10:00")

        _, streak = await eco.claim_daily(session, 1)
        assert streak == 1

    async def test_raises_wallet_not_found_for_unknown_user(self, session):
        with pytest.raises(WalletNotFoundError):
            await eco.claim_daily(session, 9999)

    async def test_claim_exactly_at_cooldown_boundary_passes(self, session, user_factory):
        user, _ = await user_factory(tg_id=1, coins=0)
        # Set last claim to exactly 24h + 1 second ago
        user.last_daily_claim = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(hours=24, seconds=1)
        await session.commit()

        reward, _ = await eco.claim_daily(session, 1)
        assert reward == settings.daily_reward_coins


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------

class TestGetHistory:
    async def test_returns_entries_for_user(self, session, user_factory):
        await user_factory(tg_id=1, coins=1000)

        await eco.credit(session, 1, 100, TransactionType.deposit, "a")
        await eco.credit(session, 1, 200, TransactionType.deposit, "b")
        await session.commit()

        history = await eco.get_history(session, 1)
        assert len(history) == 2

    async def test_returns_empty_for_no_transactions(self, session, user_factory):
        await user_factory(tg_id=1, coins=0)
        history = await eco.get_history(session, 1)
        assert history == []

    async def test_ordered_by_most_recent_first(self, session, user_factory):
        await user_factory(tg_id=1, coins=1000)

        # Both credits happen within the same test run; SQLite's func.now() is
        # second-level precision, so we rely on the secondary sort by id.desc()
        # to guarantee that the second insert (higher id) comes first.
        await eco.credit(session, 1, 50, TransactionType.deposit, "first")
        await eco.credit(session, 1, 99, TransactionType.deposit, "second")
        await session.commit()

        history = await eco.get_history(session, 1)
        # Most recent by id comes first
        assert history[0].amount == 99
        assert history[1].amount == 50

    async def test_respects_limit(self, session, user_factory):
        await user_factory(tg_id=1, coins=5000)
        for i in range(30):
            await eco.credit(session, 1, 10, TransactionType.deposit, f"tx{i}")
        await session.commit()

        history = await eco.get_history(session, 1, limit=5)
        assert len(history) == 5

    async def test_cap_at_50_regardless_of_limit(self, session, user_factory):
        await user_factory(tg_id=1, coins=10_000)
        for i in range(60):
            await eco.credit(session, 1, 1, TransactionType.deposit, f"tx{i}")
        await session.commit()

        history = await eco.get_history(session, 1, limit=100)
        assert len(history) == 50

    async def test_includes_debit_entries(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        await eco.debit(session, 1, 100, TransactionType.withdrawal, "spent")
        await session.commit()

        history = await eco.get_history(session, 1)
        assert any(e.from_tg_id == 1 for e in history)
