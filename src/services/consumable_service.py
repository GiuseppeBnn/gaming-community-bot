"""
Consumables — coins buy **repeatable** food/drink items from the Locanda menu
(``data/consumables.csv`` via ``catalog_loader``).

Unlike cosmetics (own-once flair), consumables can be bought any number of times:
each purchase spends CoInn, logs a ``ShopPurchase`` row and adds to the buyer's
**pantry**. Legacy purchases remain stock until first use; ``TavernItem`` tracks
transfers and consumption separately. No purchase history is deleted or rewritten.
They grant **no Telegram permission and no gameplay advantage**; their purchase counts
feed the "Locanda menu" trophies (``item_purchases`` / ``category_purchases`` /
``shop_purchases``).

Keys are namespaced ``cons_*`` so they never collide with the ``tag_*`` cosmetic
keys in the shared ``shop_purchases.item_key`` column.

No-commit convention: callers own the transaction.
"""

from __future__ import annotations

from sqlalchemy import exists, func, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ShopPurchase, TavernItem
from services import catalog_loader
from services.catalog_loader import ConsumableCategory, ConsumableItem
from utils.daytime import utc_now


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
    counts = await stock_counts(session, user_tg_id)
    out: list[tuple[ConsumableItem, int]] = []
    for key, item in catalog_loader.get_consumables().items():
        qty = counts.get(key, 0)
        if qty > 0:
            out.append((item, qty))
    return out


def _stock(user_tg_id: int):
    """Disjoint indexed paths for legacy and materialized stock.

    Filtering an outer join by COALESCE(owner, buyer) scans unrelated purchases.
    UNION ALL keeps the buyer/owner indexes useful even with a long shop history.
    """
    legacy = (
        select(ShopPurchase.id, ShopPurchase.item_key)
        .where(
            ShopPurchase.user_tg_id == user_tg_id,
            ShopPurchase.success.is_(True),
            ShopPurchase.item_key.in_(list(get_consumables())),
            ~exists().where(TavernItem.purchase_id == ShopPurchase.id),
        )
    )
    owned = (
        select(ShopPurchase.id, ShopPurchase.item_key)
        .join(TavernItem, TavernItem.purchase_id == ShopPurchase.id)
        .where(
            TavernItem.owner_tg_id == user_tg_id,
            TavernItem.consumed_at.is_(None),
            ShopPurchase.success.is_(True),
            ShopPurchase.item_key.in_(list(get_consumables())),
        )
    )
    return union_all(legacy, owned)


async def stock_counts(session: AsyncSession, user_tg_id: int) -> dict[str, int]:
    """Current quantities, independent from lifetime purchase/trophy counts."""
    return {key: count for key, (count, _) in (await stock_summary(session, user_tg_id)).items()}


async def stock_units(session: AsyncSession, user_tg_id: int) -> dict[str, int]:
    """One concrete available unit per item, keeping callback payloads bounded."""
    return {key: unit for key, (_, unit) in (await stock_summary(session, user_tg_id)).items()}


async def stock_summary(session: AsyncSession, user_tg_id: int) -> dict[str, tuple[int, int]]:
    """One aggregate query: item -> (quantity, oldest concrete available unit)."""
    stock = _stock(user_tg_id).subquery()
    rows = await session.execute(
        select(stock.c.item_key, func.count(), func.min(stock.c.id)).group_by(stock.c.item_key)
    )
    return {key: (int(count), int(unit)) for key, count, unit in rows}


async def lock_unit(session: AsyncSession, purchase_id: int) -> str | None:
    """Serialize on the immutable purchase BEFORE lazily materializing stock.

    All unit writers use this lock order (purchase -> item), including legacy
    units. Read columns rather than identity-map entities under FOR UPDATE.
    """
    purchase = (await session.execute(
        select(ShopPurchase.item_key, ShopPurchase.user_tg_id)
        .where(ShopPurchase.id == purchase_id, ShopPurchase.success.is_(True))
        .with_for_update()
    )).first()
    if purchase is None or get_item(purchase.item_key) is None:
        return None
    exists = (await session.execute(
        select(TavernItem.purchase_id).where(TavernItem.purchase_id == purchase_id)
    )).scalar_one_or_none()
    if exists is None:
        session.add(TavernItem(purchase_id=purchase_id, owner_tg_id=purchase.user_tg_id))
        await session.flush()
    return purchase.item_key


async def move_unit(
    session: AsyncSession, purchase_id: int, owner_tg_id: int, recipient_tg_id: int
) -> bool:
    """Transfer once, refusing spent/stale units. Caller owns the transaction."""
    if owner_tg_id == recipient_tg_id or await lock_unit(session, purchase_id) is None:
        return False
    result = await session.execute(
        update(TavernItem).where(
            TavernItem.purchase_id == purchase_id,
            TavernItem.owner_tg_id == owner_tg_id,
            TavernItem.consumed_at.is_(None),
        ).values(owner_tg_id=recipient_tg_id).execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def consume_unit(session: AsyncSession, purchase_id: int, user_tg_id: int) -> str | None:
    """Consume an exact unit once; returns its catalog key, never grants rewards."""
    key = await lock_unit(session, purchase_id)
    if key is None:
        return None
    result = await session.execute(
        update(TavernItem).where(
            TavernItem.purchase_id == purchase_id,
            TavernItem.owner_tg_id == user_tg_id,
            TavernItem.consumed_at.is_(None),
        ).values(consumed_at=utc_now()).execution_options(synchronize_session=False)
    )
    return key if result.rowcount else None
