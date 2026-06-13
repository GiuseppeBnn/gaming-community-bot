"""Integration tests for the economy hardening (Phase E):
amount validation on credit/debit and the idempotent daily claim.

SQLite ignores FOR UPDATE, so these assert behaviour, not the lock itself (which
only has teeth on Postgres in production).
"""

from __future__ import annotations

import pytest

import services.economy_service as eco
from database.models import TransactionType
from exceptions.economy import DailyAlreadyClaimedError


class TestAmountValidation:
    async def test_credit_rejects_zero(self, session, user_factory):
        await user_factory(tg_id=1, coins=100)
        with pytest.raises(ValueError):
            await eco.credit(session, 1, 0, TransactionType.admin_credit, "x")

    async def test_credit_rejects_negative(self, session, user_factory):
        await user_factory(tg_id=1, coins=100)
        with pytest.raises(ValueError):
            await eco.credit(session, 1, -50, TransactionType.admin_credit, "x")

    async def test_debit_rejects_zero(self, session, user_factory):
        await user_factory(tg_id=1, coins=100)
        with pytest.raises(ValueError):
            await eco.debit(session, 1, 0, TransactionType.admin_debit, "x")

    async def test_debit_rejects_negative(self, session, user_factory):
        # A negative debit must NOT secretly credit the wallet.
        _user, wallet = await user_factory(tg_id=1, coins=100)
        with pytest.raises(ValueError):
            await eco.debit(session, 1, -50, TransactionType.admin_debit, "x")
        assert wallet.coins == 100

    async def test_positive_amounts_still_work(self, session, user_factory):
        _user, wallet = await user_factory(tg_id=1, coins=100)
        await eco.credit(session, 1, 25, TransactionType.admin_credit, "ok")
        await eco.debit(session, 1, 10, TransactionType.admin_debit, "ok")
        await session.commit()
        assert wallet.coins == 115


class TestDailyIdempotence:
    async def test_second_claim_is_rejected(self, session, user_factory):
        await user_factory(tg_id=1, coins=0)
        reward, streak = await eco.claim_daily(session, 1)
        await session.commit()
        assert reward > 0 and streak == 1

        with pytest.raises(DailyAlreadyClaimedError):
            await eco.claim_daily(session, 1)
