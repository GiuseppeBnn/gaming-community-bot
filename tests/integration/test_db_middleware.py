"""Integration tests for middlewares/db_middleware._upsert_user."""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy import select

from database.models import User, Wallet
from middlewares.db_middleware import _upsert_user


def _make_tg_user(user_id: int = 1, username: str = "alice", full_name: str = "Alice Smith"):
    tg = MagicMock()
    tg.id = user_id
    tg.username = username
    tg.full_name = full_name
    tg.first_name = full_name.split()[0]
    tg.is_bot = False
    return tg


class TestUpsertUser:
    async def test_creates_user_and_wallet_on_first_call(self, session):
        tg_user = _make_tg_user(user_id=100, username="alice")

        await _upsert_user(session, tg_user)

        user = (await session.execute(select(User).where(User.tg_id == 100))).scalar_one_or_none()
        wallet = (await session.execute(select(Wallet).where(Wallet.tg_id == 100))).scalar_one_or_none()

        assert user is not None
        assert user.username == "alice"
        assert wallet is not None
        assert wallet.coins == 0

    async def test_updates_username_on_change(self, session, user_factory):
        user, _ = await user_factory(tg_id=200, username="old_name")

        tg_user = _make_tg_user(user_id=200, username="new_name")
        await _upsert_user(session, tg_user)

        await session.refresh(user)
        assert user.username == "new_name"

    async def test_updates_full_name_on_change(self, session, user_factory):
        user, _ = await user_factory(tg_id=201, full_name="Old Name")

        tg_user = _make_tg_user(user_id=201, full_name="New Name")
        await _upsert_user(session, tg_user)

        await session.refresh(user)
        assert user.full_name == "New Name"

    async def test_no_update_when_nothing_changed(self, session, user_factory):
        user, _ = await user_factory(tg_id=202, username="same", full_name="Test same")

        tg_user = _make_tg_user(user_id=202, username="same", full_name="Test same")
        await _upsert_user(session, tg_user)  # should not raise or fail

        assert user.username == "same"

    async def test_creates_wallet_if_missing(self, session):
        # Create user without wallet (unusual but defensive code handles this)
        session.add(User(tg_id=300, username="u", full_name="U"))
        await session.commit()

        tg_user = _make_tg_user(user_id=300, username="u", full_name="U")
        await _upsert_user(session, tg_user)

        wallet = (await session.execute(select(Wallet).where(Wallet.tg_id == 300))).scalar_one_or_none()
        assert wallet is not None

    async def test_idempotent_multiple_calls(self, session):
        tg_user = _make_tg_user(user_id=400, username="bob")

        await _upsert_user(session, tg_user)
        await _upsert_user(session, tg_user)  # second call should not duplicate

        users = list((await session.execute(select(User).where(User.tg_id == 400))).scalars())
        wallets = list((await session.execute(select(Wallet).where(Wallet.tg_id == 400))).scalars())
        assert len(users) == 1
        assert len(wallets) == 1
