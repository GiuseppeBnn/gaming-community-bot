"""Integration tests for the trophy system (rarity, xp condition, CSV upsert)."""

from __future__ import annotations

import pytest

import services.badge_service as badge_svc
from services import consumable_service, progress_service

pytestmark = pytest.mark.asyncio


def _row(slug, **over):
    base = {
        "slug": slug, "name": slug.title(), "description": "d", "icon_emoji": "🏅",
        "category": "g", "rarity": "bronze", "xp_reward": 0,
        "condition_type": None, "condition_value": None, "condition_param": None,
    }
    base.update(over)
    return base


async def _buy(session, tg_id, item_key, times=1):
    item = consumable_service.get_item(item_key)
    for _ in range(times):
        await consumable_service.record_consumption(session, tg_id, item, item.price)
    await session.commit()


# ---------------------------------------------------------------------------
# xp unlock condition
# ---------------------------------------------------------------------------

class TestXpCondition:
    async def test_unlocks_xp_trophy_at_threshold(self, seeded_session, user_factory):
        await user_factory(tg_id=1, coins=0, xp=600)
        earned = await badge_svc.check_and_award_milestones(seeded_session, 1)
        assert "xp_500" in {b.slug for b in earned}

    async def test_no_xp_trophy_below_threshold(self, seeded_session, user_factory):
        await user_factory(tg_id=2, coins=0, xp=499)
        earned = await badge_svc.check_and_award_milestones(seeded_session, 2)
        assert "xp_500" not in {b.slug for b in earned}


# ---------------------------------------------------------------------------
# sync_trophies upsert
# ---------------------------------------------------------------------------

class TestSyncUpsert:
    async def test_insert_then_update_in_place(self, session):
        await badge_svc.sync_trophies(session, [_row("x", name="X", rarity="bronze")])
        await badge_svc.sync_trophies(session, [_row("x", name="X2", rarity="gold", xp_reward=5)])

        badges = [b for b in await badge_svc.get_all_badges(session) if b.slug == "x"]
        assert len(badges) == 1
        assert badges[0].name == "X2"
        assert badges[0].rarity == "gold"
        assert badges[0].xp_reward == 5

    async def test_returns_count(self, session):
        n = await badge_svc.sync_trophies(session, [_row("a"), _row("b")])
        assert n == 2


# ---------------------------------------------------------------------------
# trophy leaderboard
# ---------------------------------------------------------------------------

class TestLeaderboardTrophies:
    async def test_orders_by_trophy_count(self, seeded_session, user_factory):
        await user_factory(tg_id=10, xp=10)
        await user_factory(tg_id=11, xp=10)
        await badge_svc.award_badge(seeded_session, 10, "first_steps")
        await badge_svc.award_badge(seeded_session, 11, "first_steps")
        await badge_svc.award_badge(seeded_session, 11, "generous")
        await seeded_session.commit()

        rows = await badge_svc.leaderboard_trophies(seeded_session)
        assert rows[0][0].tg_id == 11
        assert rows[0][1] == 2

    async def test_excludes_users_without_trophies(self, seeded_session, user_factory):
        await user_factory(tg_id=20, xp=10)
        rows = await badge_svc.leaderboard_trophies(seeded_session)
        assert all(r[0].tg_id != 20 for r in rows)


# ---------------------------------------------------------------------------
# Consumable-driven conditions
# ---------------------------------------------------------------------------

class TestPurchaseConditions:
    async def test_item_purchases_unlocks(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("t_pizza", condition_type="item_purchases", condition_value=2,
                 condition_param="cons_pizza_pacman"),
        ])
        await user_factory(tg_id=1)
        await _buy(session, 1, "cons_pizza_pacman", times=1)
        assert await badge_svc.check_and_award_milestones(session, 1) == []
        await _buy(session, 1, "cons_pizza_pacman", times=1)  # now 2 total
        earned = await badge_svc.check_and_award_milestones(session, 1)
        assert {b.slug for b in earned} == {"t_pizza"}

    async def test_category_purchases_aggregate(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("menu_bevande", condition_type="category_purchases", condition_value=2,
                 condition_param="bevande"),
        ])
        await user_factory(tg_id=1)
        await _buy(session, 1, "cons_nuka_cola")
        await _buy(session, 1, "cons_latte_mandorla")
        earned = await badge_svc.check_and_award_milestones(session, 1)
        assert "menu_bevande" in {b.slug for b in earned}

    async def test_shop_purchases_total(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("spender", condition_type="shop_purchases", condition_value=3),
        ])
        await user_factory(tg_id=1)
        await _buy(session, 1, "cons_pizza_pacman")
        await _buy(session, 1, "cons_nuka_cola")
        assert await badge_svc.check_and_award_milestones(session, 1) == []
        await _buy(session, 1, "cons_super_fungo")
        earned = await badge_svc.check_and_award_milestones(session, 1)
        assert "spender" in {b.slug for b in earned}


# ---------------------------------------------------------------------------
# Podium conditions
# ---------------------------------------------------------------------------

class TestPodiumConditions:
    async def test_podium_count_unlocks(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("podio1", condition_type="podium_count", condition_value=1,
                 condition_param="trivia"),
        ])
        await user_factory(tg_id=1)
        await progress_service.record_podium(session, 1, "trivia", 2)
        await session.commit()
        earned = await badge_svc.check_and_award_milestones(session, 1)
        assert "podio1" in {b.slug for b in earned}

    async def test_first_place_count_needs_rank_one(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("first1", condition_type="first_place_count", condition_value=1,
                 condition_param="trivia"),
        ])
        await user_factory(tg_id=1)
        await progress_service.record_podium(session, 1, "trivia", 2)  # not a 1st
        await session.commit()
        assert await badge_svc.check_and_award_milestones(session, 1) == []
        await progress_service.record_podium(session, 1, "trivia", 1)
        await session.commit()
        earned = await badge_svc.check_and_award_milestones(session, 1)
        assert "first1" in {b.slug for b in earned}


# ---------------------------------------------------------------------------
# Collection trophies (fixpoint within a single call)
# ---------------------------------------------------------------------------

class TestCollectionFixpoint:
    async def test_collection_unlocks_same_call_as_last_prereq(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("a", condition_type="item_purchases", condition_value=1,
                 condition_param="cons_pizza_pacman"),
            _row("b", condition_type="item_purchases", condition_value=1,
                 condition_param="cons_nuka_cola"),
            _row("coll", rarity="gold", condition_type="collection", condition_param="a;b"),
        ])
        await user_factory(tg_id=1)
        await _buy(session, 1, "cons_pizza_pacman")
        await _buy(session, 1, "cons_nuka_cola")
        earned = {b.slug for b in await badge_svc.check_and_award_milestones(session, 1)}
        assert earned == {"a", "b", "coll"}

    async def test_collection_locked_until_all_prereqs(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("a", condition_type="item_purchases", condition_value=1,
                 condition_param="cons_pizza_pacman"),
            _row("b", condition_type="item_purchases", condition_value=1,
                 condition_param="cons_nuka_cola"),
            _row("coll", rarity="gold", condition_type="collection", condition_param="a;b"),
        ])
        await user_factory(tg_id=1)
        await _buy(session, 1, "cons_pizza_pacman")  # only one prereq
        earned = {b.slug for b in await badge_svc.check_and_award_milestones(session, 1)}
        assert earned == {"a"}
        # Buying the second prereq later unlocks both b and the collection.
        await _buy(session, 1, "cons_nuka_cola")
        earned = {b.slug for b in await badge_svc.check_and_award_milestones(session, 1)}
        assert earned == {"b", "coll"}
