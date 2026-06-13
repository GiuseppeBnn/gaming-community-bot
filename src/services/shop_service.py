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

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
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


# ---------------------------------------------------------------------------
# Active tags (multiple simultaneously, switchable & combinable)
# ---------------------------------------------------------------------------

def active_tag_keys(user: User) -> list[str]:
    """Ordered list of the user's active tag catalog keys."""
    try:
        data = json.loads(user.active_tags_json) if user.active_tags_json else []
    except (ValueError, TypeError):
        return []
    return [str(k) for k in data] if isinstance(data, list) else []


def _set_active_tag_keys(user: User, keys: list[str]) -> None:
    user.active_tags_json = json.dumps(keys, ensure_ascii=False)
    # Keep the legacy single-tag column in sync (for any old reader). Computed
    # directly from the keys — NOT via render_active_tags, whose empty-list
    # fallback would otherwise make a just-cleared tag sticky.
    cats = catalog_loader.get_cosmetics()
    parts = [format_tag(cats[k]) for k in keys if k in cats]
    user.cosmetic_tag = " ".join(parts) if parts else None


def render_active_tags(user: User) -> str:
    """The flair string to show: active tags resolved via the catalog and joined.

    Falls back to the legacy single ``cosmetic_tag`` for users who never used the
    new switcher (so existing tags keep showing until they touch the new UI)."""
    cats = catalog_loader.get_cosmetics()
    keys = active_tag_keys(user)
    parts = [format_tag(cats[k]) for k in keys if k in cats]
    if parts:
        return " ".join(parts)
    return user.cosmetic_tag or ""


async def get_active_keys(session: AsyncSession, user_tg_id: int) -> list[str]:
    user = (await session.execute(select(User).where(User.tg_id == user_tg_id))).scalar_one_or_none()
    return active_tag_keys(user) if user else []


async def owned_tag_keys(session: AsyncSession, user_tg_id: int) -> list[str]:
    """Catalog keys the user owns (purchased), in catalog order."""
    owned: list[str] = []
    for key in catalog_loader.get_cosmetics():
        if await has_cosmetic(session, user_tg_id, key):
            owned.append(key)
    return owned


async def ensure_active_seeded(session: AsyncSession, user_tg_id: int) -> None:
    """Migrate a legacy single ``cosmetic_tag`` into the active list on first use
    of the switcher, so the displayed state matches reality. No commit."""
    user = (await session.execute(select(User).where(User.tg_id == user_tg_id))).scalar_one_or_none()
    if user is None or active_tag_keys(user) or not user.cosmetic_tag:
        return
    for key, item in catalog_loader.get_cosmetics().items():
        if format_tag(item) == user.cosmetic_tag and await has_cosmetic(session, user_tg_id, key):
            _set_active_tag_keys(user, [key])
            return


async def toggle_tag(
    session: AsyncSession, user_tg_id: int, item_key: str, max_active: int
) -> str:
    """Activate/deactivate an owned tag (respecting the cap). No commit.

    Returns one of: ``activated`` · ``deactivated`` · ``cap`` · ``notowned``.
    """
    user = (await session.execute(select(User).where(User.tg_id == user_tg_id))).scalar_one_or_none()
    if user is None:
        return "notowned"
    keys = active_tag_keys(user)
    if item_key in keys:
        keys.remove(item_key)
        _set_active_tag_keys(user, keys)
        return "deactivated"
    if not await has_cosmetic(session, user_tg_id, item_key):
        return "notowned"
    if len(keys) >= max_active:
        return "cap"
    keys.append(item_key)
    _set_active_tag_keys(user, keys)
    return "activated"


async def apply_cosmetic(session: AsyncSession, user_tg_id: int, item: CosmeticItem) -> None:
    """Apply a just-purchased cosmetic: auto-activate it if there's room under the
    cap, otherwise it's simply owned and can be activated later. Does NOT commit."""
    result = await session.execute(select(User).where(User.tg_id == user_tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        return
    keys = active_tag_keys(user)
    if item.key not in keys and len(keys) < settings.max_active_tags:
        keys.append(item.key)
        user.active_tags_json = json.dumps(keys, ensure_ascii=False)
    # Legacy single-tag field reflects the just-bought item for old readers/tests.
    user.cosmetic_tag = format_tag(item)
