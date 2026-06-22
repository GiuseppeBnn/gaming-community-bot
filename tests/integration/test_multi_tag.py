"""Integration tests for multiple active shop tags (switch + combine)."""

from __future__ import annotations

from sqlalchemy import select

import services.shop_service as shop_svc
from database.models import User


async def _own(session, tg_id: int, *keys: str) -> None:
    for k in keys:
        await shop_svc.record_purchase(session, tg_id, k, cost=0)
    await session.commit()


def _catalog_keys(n: int) -> list[str]:
    return list(shop_svc.get_cosmetics().keys())[:n]


async def _user(session, tg_id: int) -> User:
    return (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one()


async def test_toggle_requires_ownership(session, user_factory):
    await user_factory(1, "u1")
    key = _catalog_keys(1)[0]
    assert await shop_svc.toggle_tag(session, 1, key, max_active=3) == "notowned"


async def test_activate_and_combine_multiple(session, user_factory):
    await user_factory(1, "u1")
    k1, k2 = _catalog_keys(2)
    await _own(session, 1, k1, k2)

    assert await shop_svc.toggle_tag(session, 1, k1, max_active=3) == "activated"
    assert await shop_svc.toggle_tag(session, 1, k2, max_active=3) == "activated"
    await session.commit()

    user = await _user(session, 1)
    assert shop_svc.active_tag_keys(user) == [k1, k2]
    # Rendered flair concatenates both tags.
    rendered = shop_svc.render_active_tags(user)
    cats = shop_svc.get_cosmetics()
    assert shop_svc.format_tag(cats[k1]) in rendered
    assert shop_svc.format_tag(cats[k2]) in rendered


async def test_cap_blocks_beyond_limit(session, user_factory):
    await user_factory(1, "u1")
    k1, k2 = _catalog_keys(2)
    await _own(session, 1, k1, k2)
    await shop_svc.toggle_tag(session, 1, k1, max_active=1)
    # Second activation exceeds the cap of 1.
    assert await shop_svc.toggle_tag(session, 1, k2, max_active=1) == "cap"


async def test_deactivate_removes_tag(session, user_factory):
    await user_factory(1, "u1")
    k1 = _catalog_keys(1)[0]
    await _own(session, 1, k1)
    await shop_svc.toggle_tag(session, 1, k1, max_active=3)
    assert await shop_svc.toggle_tag(session, 1, k1, max_active=3) == "deactivated"
    await session.commit()
    user = await _user(session, 1)
    assert shop_svc.active_tag_keys(user) == []
    assert shop_svc.render_active_tags(user) == ""  # no stale legacy fallback


async def test_legacy_seed_migrates_single_tag(session, user_factory):
    await user_factory(1, "u1")
    k1 = _catalog_keys(1)[0]
    await _own(session, 1, k1)
    # Simulate a legacy user: cosmetic_tag set, no active_tags_json yet.
    user = await _user(session, 1)
    user.cosmetic_tag = shop_svc.format_tag(shop_svc.get_cosmetics()[k1])
    await session.commit()

    await shop_svc.ensure_active_seeded(session, 1)
    await session.commit()
    user = await _user(session, 1)
    assert shop_svc.active_tag_keys(user) == [k1]
