"""
Shop — coins buy **cosmetic** customizations (tags/titles) from an admin-curated
catalog (``data/shop_cosmetics.csv`` via ``catalog_loader``).

Cosmetics are purely in-bot flair (shown on the profile): **no Telegram permissions**
are ever granted, so there is no privilege-escalation surface. Purchases are
**idempotent** (you either own a cosmetic or not) and always apply to the buyer
only — there is nothing a user can do here to affect another member.

No-commit convention: callers own the transaction.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ShopPurchase, User
from services import catalog_loader
from services.catalog_loader import CosmeticItem


def get_cosmetics() -> dict[str, CosmeticItem]:
    """The current cosmetic catalog (loaded from CSV at startup, else defaults)."""
    return catalog_loader.get_cosmetics()


def get_item(item_key: str) -> CosmeticItem | None:
    return catalog_loader.get_cosmetics().get(item_key)


def format_tag(item: CosmeticItem) -> str:
    """The flair string stored on the user's profile."""
    return f"{item.emoji} {item.tag_text}".strip()


async def has_cosmetic(session: AsyncSession, user_tg_id: int, item_key: str) -> bool:
    result = await session.execute(
        select(ShopPurchase.id)
        .where(
            ShopPurchase.user_tg_id == user_tg_id,
            ShopPurchase.item_key == item_key,
            ShopPurchase.success == True,  # noqa: E712
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def record_purchase(
    session: AsyncSession, user_tg_id: int, item_key: str, cost: int
) -> ShopPurchase:
    """Log a successful cosmetic purchase. Does NOT commit."""
    purchase = ShopPurchase(
        user_tg_id=user_tg_id,
        item_key=item_key,
        group_id=0,
        target_tg_id=None,
        cost=cost,
        success=True,
    )
    session.add(purchase)
    return purchase


async def apply_cosmetic(session: AsyncSession, user_tg_id: int, item: CosmeticItem) -> None:
    """Set the buyer's cosmetic tag. Does NOT commit."""
    result = await session.execute(select(User).where(User.tg_id == user_tg_id))
    user = result.scalar_one_or_none()
    if user is not None:
        user.cosmetic_tag = format_tag(item)
