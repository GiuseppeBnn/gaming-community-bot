"""
Consumables — coins buy **repeatable** food/drink items from the Locanda menu
(``data/consumables.csv`` via ``catalog_loader``).

Unlike cosmetics (own-once flair), consumables can be bought any number of times:
each purchase spends CoInn, logs a ``ShopPurchase`` row and adds to the buyer's
**pantry** (an inventory derived from those rows — no extra column on ``User``).
They grant **no Telegram permission and no gameplay effect**; their purchase counts
feed the "Locanda menu" trophies (``item_purchases`` / ``category_purchases`` /
``shop_purchases``).

Keys are namespaced ``cons_*`` so they never collide with the ``tag_*`` cosmetic
keys in the shared ``shop_purchases.item_key`` column.

No-commit convention: callers own the transaction.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ShopPurchase
from services import catalog_loader
from services.catalog_loader import ConsumableCategory, ConsumableItem


def get_consumables() -> dict[str, ConsumableItem]:
    """The current consumable catalog (loaded from CSV at startup, else defaults)."""
    return catalog_loader.get_consumables()


def get_item(item_key: str) -> ConsumableItem | None:
    return catalog_loader.get_consumables().get(item_key)


def get_categories() -> list[ConsumableCategory]:
    """Categories that actually contain at least one consumable, in display order."""
    cats = catalog_loader.get_consumable_categories()
    present = {item.category for item in catalog_loader.get_consumables().values()}
    return sorted(
        (c for k, c in cats.items() if k in present),
        key=lambda c: (c.order, c.key),
    )


def get_category(category_key: str) -> ConsumableCategory | None:
    return catalog_loader.get_consumable_categories().get(category_key)


def items_in_category(category_key: str) -> list[ConsumableItem]:
    """Consumables belonging to a category, in catalog (insertion) order."""
    return [
        item for item in catalog_loader.get_consumables().values()
        if item.category == category_key
    ]


async def record_consumption(
    session: AsyncSession, user_tg_id: int, item: ConsumableItem, cost: int
) -> ShopPurchase:
    """Log a (repeatable) consumable purchase. Does NOT commit."""
    purchase = ShopPurchase(
        user_tg_id=user_tg_id,
        item_key=item.key,
        group_id=0,
        target_tg_id=None,
        cost=cost,
        success=True,
    )
    session.add(purchase)
    return purchase


async def purchase_counts(session: AsyncSession, user_tg_id: int) -> dict[str, int]:
    """How many times the user has bought each **consumable** (item_key → count).

    One GROUP BY query, filtered to the current consumable keys so cosmetic
    purchases in the same table are never counted."""
    keys = list(catalog_loader.get_consumables().keys())
    if not keys:
        return {}
    result = await session.execute(
        select(ShopPurchase.item_key, func.count())
        .where(
            ShopPurchase.user_tg_id == user_tg_id,
            ShopPurchase.success == True,  # noqa: E712
            ShopPurchase.item_key.in_(keys),
        )
        .group_by(ShopPurchase.item_key)
    )
    return {row[0]: int(row[1]) for row in result.all()}


def category_total(counts: dict[str, int], category_key: str) -> int:
    """Sum of purchase counts across all items of a category (from a counts map)."""
    return sum(
        n for key, n in counts.items()
        if (item := get_item(key)) is not None and item.category == category_key
    )


def total_purchases(counts: dict[str, int]) -> int:
    """Total consumable purchases across the whole menu (from a counts map)."""
    return sum(counts.values())


async def inventory(
    session: AsyncSession, user_tg_id: int
) -> list[tuple[ConsumableItem, int]]:
    """The user's pantry: owned consumables with quantity > 0, in catalog order."""
    counts = await purchase_counts(session, user_tg_id)
    out: list[tuple[ConsumableItem, int]] = []
    for key, item in catalog_loader.get_consumables().items():
        qty = counts.get(key, 0)
        if qty > 0:
            out.append((item, qty))
    return out
