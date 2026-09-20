"""Integration tests for services/admin_service.py (in-memory SQLite).

Service functions never commit — the test commits after calling them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import services.admin_service as admin
from database.models import LedgerEntry, TransactionType, User, Wallet
from exceptions.economy import WalletNotFoundError


# ---------------------------------------------------------------------------
# set_balance
# ---------------------------------------------------------------------------

class TestSetBalance:
    async def test_increases_with_credit(self, session, user_factory):
        _, wallet = await user_factory(tg_id=1, coins=100)
        old, new = await admin.set_balance(session, 1, 500)
        await session.commit()
        assert (old, new) == (100, 500)
        assert wallet.coins == 500

    async def test_decreases_with_debit(self, session, user_factory):
        _, wallet = await user_factory(tg_id=1, coins=500)
        old, new = await admin.set_balance(session, 1, 200)
        await session.commit()
        assert (old, new) == (500, 200)
        assert wallet.coins == 200

    async def test_same_balance_no_ledger(self, session, user_factory):
        await user_factory(tg_id=1, coins=300)
        await admin.set_balance(session, 1, 300)
        await session.commit()
        entries = list((await session.execute(select(LedgerEntry))).scalars())
        assert entries == []

    async def test_negative_target_raises(self, session, user_factory):
        await user_factory(tg_id=1, coins=300)
        with pytest.raises(ValueError):
            await admin.set_balance(session, 1, -5)

    async def test_missing_wallet_raises(self, session):
        with pytest.raises(WalletNotFoundError):
            await admin.set_balance(session, 999, 100)


# ---------------------------------------------------------------------------
# mass_credit
# ---------------------------------------------------------------------------

class TestMassCredit:
    async def test_credits_everyone(self, session, user_factory):
        _, w1 = await user_factory(tg_id=1, coins=0)
        _, w2 = await user_factory(tg_id=2, coins=50)
        count = await admin.mass_credit(session, 100, "airdrop")
        await session.commit()
        assert count == 2
        assert w1.coins == 100
        assert w2.coins == 150

    async def test_one_ledger_per_user(self, session, user_factory):
        await user_factory(tg_id=1, coins=0)
        await user_factory(tg_id=2, coins=0)
        await admin.mass_credit(session, 10, "airdrop")
        await session.commit()
        entries = list((await session.execute(select(LedgerEntry))).scalars())
        assert len(entries) == 2
        assert all(e.tx_type == TransactionType.admin_credit.value for e in entries)

    async def test_no_users_returns_zero(self, session):
        assert await admin.mass_credit(session, 10, "x") == 0

    async def test_non_positive_raises(self, session, user_factory):
        await user_factory(tg_id=1, coins=0)
        with pytest.raises(ValueError):
            await admin.mass_credit(session, 0, "x")


# ---------------------------------------------------------------------------
# Warn system
# ---------------------------------------------------------------------------

class TestWarnings:
    async def test_add_returns_active_count(self, session, user_factory):
        await user_factory(tg_id=1)
        assert await admin.add_warning(session, 1, -100, 9, "spam") == 1
        assert await admin.add_warning(session, 1, -100, 9, "again") == 2
        await session.commit()

    async def test_clear_one_most_recent(self, session, user_factory):
        await user_factory(tg_id=1)
        await admin.add_warning(session, 1, -100, 9, "first")
        await admin.add_warning(session, 1, -100, 9, "second")
        cleared = await admin.clear_warnings(session, 1, count=1)
        await session.commit()
        assert cleared == 1
        remaining = await admin.active_warnings(session, 1)
        assert len(remaining) == 1
        assert remaining[0].reason == "first"

    async def test_clear_all(self, session, user_factory):
        await user_factory(tg_id=1)
        await admin.add_warning(session, 1, -100, 9, None)
        await admin.add_warning(session, 1, -100, 9, None)
        assert await admin.clear_warnings(session, 1) == 2
        await session.commit()
        assert await admin.active_warning_count(session, 1) == 0


# ---------------------------------------------------------------------------
# Dossier / stats / leaderboard / search
# ---------------------------------------------------------------------------

class TestDossierAndStats:
    async def test_dossier(self, session, user_factory):
        await user_factory(tg_id=1, coins=777, username="neo")
        await admin.add_warning(session, 1, -100, 9, "x")
        await session.commit()
        d = await admin.get_dossier(session, 1)
        assert d is not None
        assert d.coins == 777
        assert d.active_warnings == 1

    async def test_dossier_missing(self, session):
        assert await admin.get_dossier(session, 404) is None

    async def test_leaderboard_orders_by_coins(self, session, user_factory):
        await user_factory(tg_id=1, coins=100)
        await user_factory(tg_id=2, coins=900)
        await user_factory(tg_id=3, coins=500)
        board = await admin.leaderboard(session, limit=10)
        assert [coins for _, coins in board] == [900, 500, 100]

    async def test_search_by_username(self, session, user_factory):
        await user_factory(tg_id=1, username="mario_rossi")
        await user_factory(tg_id=2, username="luigi")
        found = await admin.search_users(session, "mario")
        assert [u.tg_id for u in found] == [1]

    async def test_resolve_usernames_exact_case_insensitive_with_missing(
        self, session, user_factory
    ):
        await user_factory(tg_id=1, username="Mario")
        await user_factory(tg_id=2, username="luigi")
        # '@' optional, case-insensitive, exact only; blanks and duplicates dropped;
        # a partial ('mari') must NOT match — this pays out, no fuzzy matching.
        found, missing = await admin.resolve_usernames(
            session, ["@mario", "LUIGI", "luigi", "", "  ", "mari", "@ghost"]
        )
        assert sorted(u.tg_id for u in found) == [1, 2]
        assert missing == ["mari", "ghost"]

    async def test_resolve_usernames_empty_input(self, session):
        assert await admin.resolve_usernames(session, ["", "  ", "@"]) == ([], [])

    async def test_get_users_by_ids_preserves_order_and_drops_unknown(
        self, session, user_factory
    ):
        await user_factory(tg_id=1, username="a")
        await user_factory(tg_id=2, username="b")
        found = await admin.get_users_by_ids(session, [2, 404, 1])
        assert [u.tg_id for u in found] == [2, 1]

    async def test_get_users_by_ids_empty(self, session):
        assert await admin.get_users_by_ids(session, []) == []

    async def test_economy_stats(self, session, user_factory):
        await user_factory(tg_id=1, coins=100)
        await user_factory(tg_id=2, coins=300)
        s = await admin.economy_stats(session)
        assert s.total_users == 2
        assert s.total_coins == 400
        assert s.avg_coins == 200


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class TestAudit:
    async def test_log_and_recent(self, session):
        await admin.log_action(session, 9, "ban", target_tg_id=1, detail="spam")
        await admin.log_action(session, 9, "mute", target_tg_id=2, amount=600)
        await session.commit()
        actions = await admin.recent_actions(session)
        assert {a.action_type for a in actions} == {"ban", "mute"}

    async def test_recent_filtered_by_target(self, session):
        await admin.log_action(session, 9, "ban", target_tg_id=1)
        await admin.log_action(session, 9, "warn", target_tg_id=2)
        await session.commit()
        actions = await admin.recent_actions(session, target_tg_id=2)
        assert len(actions) == 1
        assert actions[0].target_tg_id == 2


# ---------------------------------------------------------------------------
# set_user_banned (bot-level ban flag, with stub-row upsert)
# ---------------------------------------------------------------------------

class TestSetUserBanned:
    async def test_sets_flag_on_existing_user(self, session, user_factory):
        await user_factory(tg_id=1, coins=0)
        assert await admin.set_user_banned(session, 1, True) is True
        await session.commit()
        assert await session.scalar(select(User.is_banned).where(User.tg_id == 1)) is True

    async def test_ban_without_row_creates_banned_stub(self, session):
        # Banning a user the bot has never seen must still stick: a stub row is
        # created, otherwise their first update would upsert an unbanned row.
        assert await admin.set_user_banned(session, 777, True) is True
        await session.commit()
        user = await session.get(User, 777)
        assert user is not None and user.is_banned is True
        # A wallet is materialised too, so later admin/economy reads don't break.
        assert await session.scalar(select(Wallet.tg_id).where(Wallet.tg_id == 777)) == 777

    async def test_clear_without_row_is_noop(self, session):
        assert await admin.set_user_banned(session, 888, False) is False
        await session.commit()
        assert await session.get(User, 888) is None  # no row materialised for a clear
