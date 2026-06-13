"""Integration: total-state export → import roundtrip preserves every value."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database.models import Base, LedgerEntry, User, Wallet
from services.backup import state_export
from services.backup.state_export import StateExportError


async def _fresh_db():
    """A second, independent in-memory DB to import into (the migration target)."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    return engine, factory


async def test_roundtrip_preserves_values(tmp_path, session, user_factory):
    await user_factory(111, username="alice", coins=12_345_678_900, xp=999, daily_streak=7)
    session.add(LedgerEntry(to_tg_id=111, amount=12_345_678_900, tx_type="deposit", description="seed"))
    await session.commit()

    snapshot = await state_export.export_state(session, tmp_path)
    assert snapshot.exists()
    assert snapshot.with_name(snapshot.name + ".sha256").exists()
    assert (tmp_path / "state-latest.jsonl.gz").exists()

    engine2, factory2 = await _fresh_db()
    try:
        async with factory2() as s2:
            report = await state_export.import_state(s2, snapshot, mode="empty")
            await s2.commit()

            assert report.tables.get("users") == 1
            assert report.tables.get("wallets") == 1
            assert report.tables.get("ledger") == 1

            user = (await s2.execute(select(User).where(User.tg_id == 111))).scalar_one()
            wallet = (await s2.execute(select(Wallet).where(Wallet.tg_id == 111))).scalar_one()
            assert user.username == "alice"
            assert user.xp == 999
            assert user.daily_streak == 7
            assert wallet.coins == 12_345_678_900  # BigInteger survives the roundtrip

            ledger = (await s2.execute(select(LedgerEntry))).scalars().all()
            assert len(ledger) == 1
            assert ledger[0].amount == 12_345_678_900
    finally:
        await engine2.dispose()


async def test_import_refuses_nonempty_db(tmp_path, session, user_factory):
    await user_factory(1, coins=5)
    snapshot = await state_export.export_state(session, tmp_path)
    # The session's DB already has rows → 'empty' mode must refuse.
    with pytest.raises(StateExportError):
        await state_export.import_state(session, snapshot, mode="empty")


async def test_import_rejects_corrupt_snapshot(tmp_path, session, user_factory):
    await user_factory(1, coins=5)
    snapshot = await state_export.export_state(session, tmp_path)
    with open(snapshot, "ab") as f:
        f.write(b"tampered")

    engine2, factory2 = await _fresh_db()
    try:
        async with factory2() as s2:
            with pytest.raises(StateExportError):
                await state_export.import_state(s2, snapshot, mode="empty")
    finally:
        await engine2.dispose()
