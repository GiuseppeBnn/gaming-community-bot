"""Real SQL coverage for gifts, virtual legacy stock and immutable purchase history."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from database.models import LedgerEntry, ShopPurchase, TavernItem, TavernOffer, User, Wallet
from exceptions.economy import InsufficientFundsError
from services import catalog_loader, consumable_service as stock, tavern_service as gifts
from utils.daytime import utc_now

pytestmark = pytest.mark.asyncio
GROUP = -100123
KEY = "cons_nuka_cola"


@pytest.fixture
async def accounts(user_factory):
    for uid in (1, 2, 3):
        await user_factory(tg_id=uid, coins=2000)


async def draft(session, message_id=10, sender=1, target=2):
    offer = await gifts.create_offer(session, sender, target, GROUP, message_id)
    await session.commit()
    return offer


async def prepared(session, message_id=10, sender=1, target=2, unit=None):
    offer = await draft(session, message_id, sender, target)
    offer = await gifts.prepare_offer(
        session, offer.id, sender, GROUP,
        item_key=KEY if unit is None else None, source_purchase_id=unit,
    )
    await session.commit()
    return offer


async def legacy(session, user=1, key=KEY):
    item = stock.get_item(key)
    purchase = await stock.record_consumption(session, user, item, item.price)
    await session.commit()
    return purchase.id


async def test_legacy_stock_lazy_consumption_and_trophy_counts(session, accounts):
    first, second = await legacy(session), await legacy(session)
    assert (await session.execute(select(func.count()).select_from(TavernItem))).scalar_one() == 0
    assert await stock.stock_summary(session, 1) == {KEY: (2, first)}
    assert await stock.consume_unit(session, first, 1) == KEY
    await session.commit()
    assert await stock.consume_unit(session, first, 1) is None
    assert await stock.stock_units(session, 1) == {KEY: second}
    assert await stock.purchase_counts(session, 1) == {KEY: 2}
    assert await stock.stock_counts(session, 1) == {KEY: 1}
    assert (await stock.inventory(session, 1))[0][1] == 1
    assert await stock.consume_unit(session, second, 2) is None
    assert await stock.consume_unit(session, -1, 1) is None
    assert not await stock.move_unit(session, second, 1, 1)
    assert not await stock.move_unit(session, -1, 1, 2)


async def test_failed_and_cosmetic_purchases_never_become_consumables(session, accounts):
    for key, success in ((KEY, False), ("tag_memelord", True), ("removed_item", True)):
        row = ShopPurchase(user_tg_id=1, item_key=key, group_id=0, cost=1, success=success)
        session.add(row)
        await session.flush()
        assert await stock.consume_unit(session, row.id, 1) is None
    await session.commit()
    assert await stock.inventory(session, 1) == []


async def test_transfer_and_consume_cannot_duplicate_a_unit(session, accounts):
    unit = await legacy(session)
    assert await stock.move_unit(session, unit, 1, 2)
    assert not await stock.move_unit(session, unit, 1, 3)
    assert await stock.consume_unit(session, unit, 1) is None
    assert await stock.stock_counts(session, 2) == {KEY: 1}
    assert await stock.consume_unit(session, unit, 2) == KEY
    assert not await stock.move_unit(session, unit, 2, 3)
    assert await stock.stock_counts(session, 2) == {}
    assert await stock.purchase_counts(session, 1) == {KEY: 1}
    assert await stock.purchase_counts(session, 2) == {}


async def test_draft_deduplication_personal_token_and_limits(session, accounts):
    offer = await draft(session)
    assert (await draft(session)).id == offer.id
    assert (await gifts.find_offer(session, offer.token, 1)).id == offer.id
    for token, uid in ((offer.token, 2), ("missing", 1)):
        with pytest.raises(gifts.TavernError):
            await gifts.find_offer(session, token, uid)
    for uid, group in ((2, GROUP), (1, 0), (1, -999)):
        with pytest.raises(gifts.TavernError):
            gifts.authorize_sender(offer, uid, group)
    with pytest.raises(gifts.TavernError):
        await gifts.get_offer(session, -1)
    for mid in range(11, 15):
        await draft(session, mid)
    with pytest.raises(gifts.TavernError, match="5 offerte"):
        await draft(session, 15)
    assert len(await gifts.recent_offers(session, 1)) == 5
    assert await gifts.recent_offers(session, 2) == []
    assert await gifts.cancel_offer(session, offer.id, 1, GROUP)
    assert not await gifts.cancel_offer(session, offer.id, 1, GROUP)
    assert await draft(session, 15)


@pytest.mark.parametrize("sender,target,group", [(1, 1, GROUP), (1, 2, 0), (99, 2, GROUP), (1, 99, GROUP)])
async def test_invalid_draft_targets(session, accounts, sender, target, group):
    with pytest.raises(gifts.TavernError):
        await gifts.create_offer(session, sender, target, group, 5)


async def test_selection_is_frozen_and_requires_live_owned_stock(session, accounts):
    offer = await draft(session)
    for kwargs in ({}, {"item_key": KEY, "source_purchase_id": 1}, {"item_key": "missing"}, {"source_purchase_id": 99}):
        with pytest.raises(gifts.TavernError):
            await gifts.prepare_offer(session, offer.id, 1, GROUP, **kwargs)
    offer = await gifts.prepare_offer(session, offer.id, 1, GROUP, item_key=KEY)
    await session.commit()
    assert offer.cost == 250 and offer.item_key == KEY
    with pytest.raises(gifts.TavernError):
        await gifts.prepare_offer(session, offer.id, 1, GROUP, item_key="cons_super_fungo")
    with pytest.raises(gifts.TavernError):
        await gifts.deliver_offer(session, offer.id, 2, GROUP)


async def test_buy_gift_is_one_atomic_payment_and_durable_receipt(session, accounts):
    offer = await prepared(session)
    delivered, changed = await gifts.deliver_offer(session, offer.id, 1, GROUP)
    await session.commit()
    again, changed_again = await gifts.deliver_offer(session, offer.id, 1, GROUP)
    assert changed and not changed_again and again == delivered
    assert delivered.status == "delivered" and delivered.purchase_id is not None
    assert await stock.stock_counts(session, 1) == {}
    assert await stock.stock_counts(session, 2) == {KEY: 1}
    assert await stock.purchase_counts(session, 1) == {KEY: 1}
    assert await stock.purchase_counts(session, 2) == {}
    assert (await session.execute(select(Wallet.coins).where(Wallet.tg_id == 1))).scalar_one() == 1750
    assert (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one() == 1


async def test_pantry_gift_costs_nothing_and_cannot_give_away_spent_stock(session, accounts):
    unit = await legacy(session)
    offer = await prepared(session, unit=unit)
    assert offer.cost == 0
    await gifts.deliver_offer(session, offer.id, 1, GROUP)
    await session.commit()
    assert (await session.execute(select(Wallet.coins).where(Wallet.tg_id == 1))).scalar_one() == 2000
    assert (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one() == 0
    unit2 = await legacy(session)
    other = await prepared(session, 11, unit=unit2, sender=1, target=3)
    await stock.consume_unit(session, unit2, 1)
    await session.commit()
    # Move outside the cooldown to exercise the stale-unit guard itself.
    await session.execute(update(TavernOffer).where(TavernOffer.id == offer.id).values(delivered_at=utc_now() - timedelta(minutes=2)))
    await session.commit()
    with pytest.raises(gifts.TavernError, match="nessun addebito"):
        await gifts.deliver_offer(session, other.id, 1, GROUP)
    await session.rollback()
    assert (await gifts.get_offer(session, other.id)).status == "prepared"


async def test_cooldown_survives_new_drafts(session, accounts):
    offer = await prepared(session)
    await gifts.deliver_offer(session, offer.id, 1, GROUP)
    await session.commit()
    other = await prepared(session, 11, target=3)
    with pytest.raises(gifts.TavernError, match="minuto"):
        await gifts.deliver_offer(session, other.id, 1, GROUP)
    await session.rollback()
    assert await stock.stock_counts(session, 3) == {}


async def test_insufficient_balance_rolls_back_every_effect(session, accounts):
    offer = await prepared(session)
    await session.execute(update(Wallet).where(Wallet.tg_id == 1).values(coins=1))
    await session.commit()
    with pytest.raises(InsufficientFundsError):
        await gifts.deliver_offer(session, offer.id, 1, GROUP)
    await session.rollback()
    assert (await gifts.get_offer(session, offer.id)).status == "prepared"
    assert await stock.stock_counts(session, 2) == {}
    assert await stock.purchase_counts(session, 1) == {}


async def test_failures_after_debit_are_caller_owned_and_roll_back(session, accounts, monkeypatch):
    offer = await prepared(session)
    monkeypatch.setattr(stock, "move_unit", AsyncMock(return_value=False))
    with pytest.raises(gifts.TavernError):
        await gifts.deliver_offer(session, offer.id, 1, GROUP)
    await session.rollback()
    assert (await session.execute(select(Wallet.coins).where(Wallet.tg_id == 1))).scalar_one() == 2000
    assert await stock.purchase_counts(session, 1) == {}
    assert (await gifts.get_offer(session, offer.id)).status == "prepared"


async def test_expiry_cancellation_catalog_changes_and_missing_recipient(session, accounts, monkeypatch):
    offer = await prepared(session)
    expired = utc_now() - timedelta(seconds=1)
    await session.execute(update(TavernOffer).where(TavernOffer.id == offer.id).values(expires_at=expired))
    await session.commit()
    with pytest.raises(gifts.TavernError, match="scaduta"):
        await gifts.deliver_offer(session, offer.id, 1, GROUP)
    other = await prepared(session, 11)
    item = stock.get_item(KEY)
    menu = dict(stock.get_consumables())
    monkeypatch.setattr(catalog_loader, "_consumables", {**menu, KEY: replace(item, price=item.price + 1)})
    with pytest.raises(gifts.TavernError, match="prezzo"):
        await gifts.deliver_offer(session, other.id, 1, GROUP)
    monkeypatch.setattr(catalog_loader, "_consumables", {})
    with pytest.raises(gifts.TavernError, match="menù"):
        await gifts.deliver_offer(session, other.id, 1, GROUP)
    monkeypatch.setattr(catalog_loader, "_consumables", menu)
    # Remove only the User row in SQLite to exercise missing-account validation.
    from sqlalchemy import delete
    await session.execute(delete(User).where(User.tg_id == 2))
    await session.commit()
    with pytest.raises(gifts.TavernError, match="registrato"):
        await gifts.deliver_offer(session, other.id, 1, GROUP)


async def test_expiration_is_rechecked_after_lock_wait(session, accounts, monkeypatch):
    offer = await prepared(session)
    original = gifts.utc_now
    async def delayed_lock(*args):
        monkeypatch.setattr(gifts, "utc_now", lambda: offer.expires_at)
    monkeypatch.setattr(gifts.economy_service, "lock_users_then_wallets", delayed_lock)
    with pytest.raises(gifts.TavernError, match="scaduta"):
        await gifts.deliver_offer(session, offer.id, 1, GROUP)
    monkeypatch.setattr(gifts, "utc_now", original)
    assert await stock.purchase_counts(session, 1) == {}


async def test_publication_lease_fencing_and_retry_never_redeliver(session, accounts):
    offer = await prepared(session)
    await gifts.deliver_offer(session, offer.id, 1, GROUP)
    assert not await gifts.claim_publication(session, offer.id, 2, GROUP)
    assert await gifts.claim_publication(session, offer.id, 1, GROUP)
    lease = (await gifts.get_offer(session, offer.id)).publish_lease_until
    assert not await gifts.claim_publication(session, offer.id, 1, GROUP)
    await gifts.finish_publication(session, offer.id, lease + timedelta(seconds=1), 900)
    assert (await gifts.get_offer(session, offer.id)).message_id is None
    await gifts.finish_publication(session, offer.id, lease, None)
    assert await gifts.claim_publication(session, offer.id, 1, GROUP)
    lease = (await gifts.get_offer(session, offer.id)).publish_lease_until
    await gifts.finish_publication(session, offer.id, lease, 901)
    assert not await gifts.claim_publication(session, offer.id, 1, GROUP)
    assert (await gifts.get_offer(session, offer.id)).message_id == 901


async def test_export_restore_preserves_spent_stock_and_gift_receipts(session, accounts, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from database.models import Base
    from services.backup.state_export import export_state, import_state

    old_unit = await legacy(session)
    await stock.consume_unit(session, old_unit, 1)
    await session.commit()
    offer = await prepared(session)
    await gifts.deliver_offer(session, offer.id, 1, GROUP)
    await session.commit()
    snapshot = await export_state(session, tmp_path)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as restored:
            report = await import_state(restored, snapshot)
            await restored.commit()
            assert report.tables["tavern_items"] == 2
            assert report.tables["tavern_offers"] == 1
            assert await stock.stock_counts(restored, 1) == {}
            assert await stock.stock_counts(restored, 2) == {KEY: 1}
            receipt = await gifts.find_offer(restored, offer.token, 1)
            assert receipt.status == "delivered"
            assert await stock.consume_unit(restored, old_unit, 1) is None
            assert (await gifts.deliver_offer(restored, receipt.id, 1, GROUP))[1] is False
    finally:
        await engine.dispose()
