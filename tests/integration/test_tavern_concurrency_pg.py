"""Independent PostgreSQL transactions: one unit/charge under competing clicks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from database.models import LedgerEntry, Wallet
from services import badge_service, consumable_service as stock, tavern_service as gifts

pytestmark = [pytest.mark.asyncio, pytest.mark.pg]
GROUP = -100123
KEY = "cons_nuka_cola"


async def race(*operations):
    # Drain every participant even when an assertion-worthy SQL error occurs;
    # otherwise gather's first exception leaves locks alive during fixture DDL.
    results = await asyncio.gather(*operations, return_exceptions=True)
    assert not any(isinstance(result, BaseException) for result in results), results
    return results


async def seed(pg_session, pg_user_factory, *, pantry=False, sender=1, target=2, mid=10):
    for uid in (1, 2, 3):
        await pg_user_factory(tg_id=uid, coins=2000)
    return await make_offer(pg_session, pantry=pantry, sender=sender, target=target, mid=mid)


async def make_offer(session, *, pantry=False, sender=1, target=2, mid=10):
    unit = None
    if pantry:
        unit = await stock.record_consumption(session, sender, stock.get_item(KEY), 250)
        await session.flush()
    row = await gifts.create_offer(session, sender, target, GROUP, mid)
    row = await gifts.prepare_offer(session, row.id, sender, GROUP,
                                    item_key=KEY if unit is None else None,
                                    source_purchase_id=unit.id if unit else None)
    await session.commit()
    return row


async def test_eight_confirmations_one_payment_and_one_gift(pg_session, pg_sessions, pg_user_factory):
    row = await seed(pg_session, pg_user_factory)
    async def click():
        async with pg_sessions() as session:
            _, delivered = await gifts.deliver_offer(session, row.id, 1, GROUP)
            await session.commit()
            return delivered
    assert sum(await race(*(click() for _ in range(8)))) == 1
    async with pg_sessions() as session:
        assert await stock.stock_counts(session, 2) == {KEY: 1}
        assert (await session.execute(select(Wallet.coins).where(Wallet.tg_id == 1))).scalar_one() == 1750
        assert (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one() == 1


async def test_legacy_materialization_and_consumption_happen_once(pg_session, pg_sessions, pg_user_factory):
    row = await seed(pg_session, pg_user_factory, pantry=True)
    async def consume():
        async with pg_sessions() as session:
            result = await stock.consume_unit(session, row.source_purchase_id, 1)
            await session.commit()
            return result
    results = await race(*(consume() for _ in range(8)))
    assert results.count(KEY) == 1 and results.count(None) == 7


async def test_consume_versus_gift_never_duplicates_stock(pg_session, pg_sessions, pg_user_factory):
    row = await seed(pg_session, pg_user_factory, pantry=True)
    async def consume():
        async with pg_sessions() as session:
            result = await stock.consume_unit(session, row.source_purchase_id, 1)
            await session.commit()
            return bool(result)
    async def deliver():
        async with pg_sessions() as session:
            try:
                _, changed = await gifts.deliver_offer(session, row.id, 1, GROUP)
                await session.commit()
                return changed
            except gifts.TavernError:
                await session.rollback()
                return False
    consumed, delivered = await race(consume(), deliver())
    assert int(consumed) + int(delivered) == 1
    async with pg_sessions() as session:
        assert await stock.stock_counts(session, 1) == {}
        assert await stock.stock_counts(session, 2) == ({KEY: 1} if delivered else {})


async def test_opposite_gifts_use_shared_account_lock_order(pg_session, pg_sessions, pg_user_factory):
    first = await seed(pg_session, pg_user_factory)
    second = await make_offer(pg_session, sender=2, target=1, mid=11)
    async def deliver(row):
        async with pg_sessions() as session:
            _, changed = await gifts.deliver_offer(session, row.id, row.sender_tg_id, GROUP)
            await session.commit()
            return changed
    assert await race(deliver(first), deliver(second)) == [True, True]


async def test_pending_limit_serializes_across_independent_commands(pg_session, pg_sessions, pg_user_factory):
    for uid in (1, 2):
        await pg_user_factory(tg_id=uid, coins=2000)
    async def create(mid):
        async with pg_sessions() as session:
            try:
                await gifts.create_offer(session, 1, 2, GROUP, mid)
                await session.commit()
                return True
            except gifts.TavernError:
                await session.rollback()
                return False
    assert sum(await race(*(create(mid) for mid in range(10)))) == 5


async def test_publication_has_one_live_publisher(pg_session, pg_sessions, pg_user_factory):
    row = await seed(pg_session, pg_user_factory)
    await gifts.deliver_offer(pg_session, row.id, 1, GROUP)
    await pg_session.commit()
    async def claim():
        async with pg_sessions() as session:
            result = await gifts.claim_publication(session, row.id, 1, GROUP)
            await session.commit()
            return result
    assert sum(await race(*(claim() for _ in range(8)))) == 1


@pytest.mark.parametrize("cosmetic", [False, True])
async def test_gifts_and_normal_shop_purchases_share_account_lock_order(
    pg_session, pg_sessions, pg_user_factory, cosmetic,
):
    from handlers import shop
    from handlers.callbacks import ShopCb

    row = await seed(pg_session, pg_user_factory)
    await pg_session.execute(update(Wallet).where(Wallet.tg_id == 1).values(coins=10000))
    await pg_session.commit()
    await badge_service.seed_badges(pg_session)
    key = "tag_memelord" if cosmetic else KEY
    data = ShopCb(action="exec" if cosmetic else "cexec", key=key)
    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=1), message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(), bot=SimpleNamespace(send_message=AsyncMock()),
    )
    async def normal_buy():
        async with pg_sessions() as session:
            handler = shop.cb_shop_execute if cosmetic else shop.cb_consumable_execute
            await handler(cb, data, session)
    async def gift():
        async with pg_sessions() as session:
            await gifts.deliver_offer(session, row.id, 1, GROUP)
            await badge_service.check_and_award_milestones(session, 1)
            await session.commit()
    await race(normal_buy(), gift())
    async with pg_sessions() as session:
        price = 2000 if cosmetic else 250
        assert (await session.execute(select(Wallet.coins).where(Wallet.tg_id == 1))).scalar_one() == 10000 - 250 - price
        assert await stock.stock_counts(session, 2) == {KEY: 1}
