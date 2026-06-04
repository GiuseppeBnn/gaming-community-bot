"""Integration tests for services/shop_service.py (cosmetic shop)."""

from __future__ import annotations

import pytest

import services.shop_service as shop_svc
from database.models import ShopPurchase, TransactionType
from exceptions.economy import InsufficientFundsError
from services import economy_service
from services.catalog_loader import CosmeticItem


def _item(key="tag_pro", price=1000):
    return CosmeticItem(key=key, name="Tag PRO", tag_text="PRO", emoji="🎮", price=price)


# ---------------------------------------------------------------------------
# Catalog has no moderation/mute items anymore
# ---------------------------------------------------------------------------

class TestCatalogIsCosmetic:
    def test_no_mute_items(self):
        keys = set(shop_svc.get_cosmetics().keys())
        assert not any("mute" in k for k in keys)

    def test_all_entries_are_cosmetics(self):
        for item in shop_svc.get_cosmetics().values():
            assert isinstance(item, CosmeticItem)
            assert item.price >= 0
            assert item.tag_text


# ---------------------------------------------------------------------------
# Purchasing a cosmetic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestBuyCosmetic:
    async def test_purchase_debits_and_applies_tag(self, session, user_factory):
        await user_factory(tg_id=1, coins=5000)
        item = _item(price=1000)

        await economy_service.debit(
            session, 1, item.price, TransactionType.shop_purchase, f"Acquisto: {item.name}"
        )
        await shop_svc.record_purchase(session, 1, item.key, item.price)
        await shop_svc.apply_cosmetic(session, 1, item)
        await session.commit()

        assert await economy_service.get_balance(session, 1) == 4000
        assert await shop_svc.has_cosmetic(session, 1, item.key) is True
        from sqlalchemy import select
        from database.models import User
        user = (await session.execute(select(User).where(User.tg_id == 1))).scalar_one()
        assert user.cosmetic_tag == "🎮 PRO"

    async def test_has_cosmetic_false_before_purchase(self, session, user_factory):
        await user_factory(tg_id=2, coins=100)
        assert await shop_svc.has_cosmetic(session, 2, "tag_pro") is False

    async def test_record_purchase_marks_success(self, session, user_factory):
        await user_factory(tg_id=3, coins=100)
        purchase = await shop_svc.record_purchase(session, 3, "tag_pro", 50)
        await session.commit()
        assert isinstance(purchase, ShopPurchase)
        assert purchase.success is True

    async def test_insufficient_funds_raises(self, session, user_factory):
        await user_factory(tg_id=4, coins=10)
        with pytest.raises(InsufficientFundsError):
            await economy_service.debit(
                session, 4, 1000, TransactionType.shop_purchase, "Acquisto"
            )

    async def test_format_tag(self):
        assert shop_svc.format_tag(_item()) == "🎮 PRO"
