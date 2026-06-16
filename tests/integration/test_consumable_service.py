"""Integration tests for services/consumable_service.py (pantry + counts)."""

from __future__ import annotations

import pytest

from services import consumable_service

pytestmark = pytest.mark.asyncio


def _item(key: str):
    item = consumable_service.get_item(key)
    assert item is not None, f"default consumable {key} missing"
    return item


class TestRecordAndCount:
    async def test_repeatable_purchase_counts(self, session, user_factory):
        await user_factory(tg_id=1)
        pizza = _item("cons_pizza_pacman")
        for _ in range(3):
            await consumable_service.record_consumption(session, 1, pizza, pizza.price)
        await session.commit()

        counts = await consumable_service.purchase_counts(session, 1)
        assert counts["cons_pizza_pacman"] == 3
        assert consumable_service.total_purchases(counts) == 3

    async def test_counts_isolated_per_user(self, session, user_factory):
        await user_factory(tg_id=1)
        await user_factory(tg_id=2)
        pizza = _item("cons_pizza_pacman")
        await consumable_service.record_consumption(session, 1, pizza, pizza.price)
        await session.commit()

        assert (await consumable_service.purchase_counts(session, 2)) == {}


class TestCategoryAggregation:
    async def test_category_total_sums_items(self, session, user_factory):
        await user_factory(tg_id=1)
        # Two different bevande consumables.
        for key in ("cons_nuka_cola", "cons_latte_mandorla"):
            item = _item(key)
            await consumable_service.record_consumption(session, 1, item, item.price)
        await session.commit()

        counts = await consumable_service.purchase_counts(session, 1)
        assert consumable_service.category_total(counts, "bevande") == 2
        assert consumable_service.category_total(counts, "dessert") == 0


class TestInventory:
    async def test_inventory_only_owned(self, session, user_factory):
        await user_factory(tg_id=1)
        gelato = _item("cons_gelato_sale_marino")
        await consumable_service.record_consumption(session, 1, gelato, gelato.price)
        await consumable_service.record_consumption(session, 1, gelato, gelato.price)
        await session.commit()

        inv = await consumable_service.inventory(session, 1)
        assert len(inv) == 1
        item, qty = inv[0]
        assert item.key == "cons_gelato_sale_marino"
        assert qty == 2

    async def test_empty_inventory(self, session, user_factory):
        await user_factory(tg_id=1)
        assert await consumable_service.inventory(session, 1) == []
