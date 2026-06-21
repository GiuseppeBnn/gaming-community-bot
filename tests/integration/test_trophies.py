"""Integration tests for the trophy system (rarity, xp condition, CSV upsert)."""

from __future__ import annotations

import pytest

import services.badge_service as badge_svc
from services import catalog_loader, consumable_service, progress_service, shop_service

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
# xp / level unlock conditions
# ---------------------------------------------------------------------------

class TestXpCondition:
    async def test_unlocks_xp_trophy_at_threshold(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("xp_t", condition_type="xp", condition_value=500),
        ])
        await user_factory(tg_id=1, coins=0, xp=600)
        earned = await badge_svc.check_and_award_milestones(session, 1)
        assert "xp_t" in {b.slug for b in earned}

    async def test_no_xp_trophy_below_threshold(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("xp_t", condition_type="xp", condition_value=500),
        ])
        await user_factory(tg_id=2, coins=0, xp=499)
        earned = await badge_svc.check_and_award_milestones(session, 2)
        assert "xp_t" not in {b.slug for b in earned}


class TestLevelCondition:
    async def test_level_uses_xp_curve(self, session, user_factory):
        from services import xp_service
        await badge_svc.sync_trophies(session, [
            _row("lv3", condition_type="level", condition_value=3),
        ])
        # Just below level 3 → locked; at the level-3 XP threshold → unlocked.
        below = xp_service.xp_to_reach_level(3) - 1
        await user_factory(tg_id=1, coins=0, xp=below)
        assert await badge_svc.check_and_award_milestones(session, 1) == []
        await user_factory(tg_id=2, coins=0, xp=xp_service.xp_to_reach_level(3))
        earned = await badge_svc.check_and_award_milestones(session, 2)
        assert "lv3" in {b.slug for b in earned}


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


# ---------------------------------------------------------------------------
# Event-count conditions (generic per-user progress log: trivia last/sub30…)
# ---------------------------------------------------------------------------

class TestEventCondition:
    async def test_event_count_unlocks_and_is_idempotent(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("last2", condition_type="event_count", condition_value=2,
                 condition_param=progress_service.TRIVIA_LAST_PLACE),
        ])
        await user_factory(tg_id=1)
        # Re-processing the same source event (e.g. re-closing a quiz) counts once.
        await progress_service.record_event(session, 1, progress_service.TRIVIA_LAST_PLACE, 99)
        await session.commit()
        assert await progress_service.record_event(
            session, 1, progress_service.TRIVIA_LAST_PLACE, 99
        ) is None  # already recorded → no-op
        await session.commit()
        assert await badge_svc.check_and_award_milestones(session, 1) == []
        # A second distinct event reaches the threshold of 2.
        await progress_service.record_event(session, 1, progress_service.TRIVIA_LAST_PLACE, 100)
        await session.commit()
        earned = await badge_svc.check_and_award_milestones(session, 1)
        assert "last2" in {b.slug for b in earned}

    async def test_event_metrics_are_independent(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("sub1", condition_type="event_count", condition_value=1,
                 condition_param=progress_service.TRIVIA_SUB30),
        ])
        await user_factory(tg_id=1)
        # A last-place event must not satisfy a sub-30s trophy.
        await progress_service.record_event(session, 1, progress_service.TRIVIA_LAST_PLACE, 1)
        await session.commit()
        assert await badge_svc.check_and_award_milestones(session, 1) == []
        await progress_service.record_event(session, 1, progress_service.TRIVIA_SUB30, 1)
        await session.commit()
        earned = await badge_svc.check_and_award_milestones(session, 1)
        assert "sub1" in {b.slug for b in earned}


# ---------------------------------------------------------------------------
# "Own everything" conditions: catalog_complete + all_trophies (platinum)
# ---------------------------------------------------------------------------

class TestCatalogComplete:
    async def test_needs_all_consumables_and_cosmetics(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("collezionista", rarity="gold", condition_type="catalog_complete"),
        ])
        await user_factory(tg_id=1, coins=0)
        # Buy every consumable but own no cosmetic → still locked.
        for key in catalog_loader.get_consumables():
            await _buy(session, 1, key)
        assert await badge_svc.check_and_award_milestones(session, 1) == []
        # Own every cosmetic too → unlocks.
        for key, item in catalog_loader.get_cosmetics().items():
            await shop_service.record_purchase(session, 1, key, item.price)
        await session.commit()
        earned = await badge_svc.check_and_award_milestones(session, 1)
        assert "collezionista" in {b.slug for b in earned}


class TestAllTrophies:
    async def test_platinum_unlocks_after_all_auto_trophies(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("a", condition_type="xp", condition_value=10),
            _row("b", condition_type="xp", condition_value=20),
            _row("manual", condition_type=None),  # never auto-earned (e.g. Discord)
            _row("plat", rarity="platinum", condition_type="all_trophies"),
        ])
        await user_factory(tg_id=1, coins=0, xp=25)
        # Earns a + b, and the platinum in the SAME pass (fixpoint). The manual
        # trophy is excluded from the requirement, so the platinum is obtainable.
        earned = {b.slug for b in await badge_svc.check_and_award_milestones(session, 1)}
        assert earned == {"a", "b", "plat"}

    async def test_platinum_locked_while_one_missing(self, session, user_factory):
        await badge_svc.sync_trophies(session, [
            _row("a", condition_type="xp", condition_value=10),
            _row("b", condition_type="xp", condition_value=100),
            _row("plat", rarity="platinum", condition_type="all_trophies"),
        ])
        await user_factory(tg_id=1, coins=0, xp=50)  # earns a, not b
        earned = {b.slug for b in await badge_svc.check_and_award_milestones(session, 1)}
        assert earned == {"a"}


# ---------------------------------------------------------------------------
# Catalog reconciliation: prune trophies dropped from the catalog
# ---------------------------------------------------------------------------

class TestPrune:
    async def test_dropped_trophy_and_its_user_badges_removed(self, session, user_factory):
        await badge_svc.sync_trophies(session, [_row("keep"), _row("drop")])
        await user_factory(tg_id=1)
        await badge_svc.award_badge(session, 1, "drop")
        await session.commit()
        assert "drop" in {b.slug for b in await badge_svc.get_all_badges(session)}
        assert {ub.badge.slug for ub in await badge_svc.get_user_badges(session, 1)} == {"drop"}

        # Re-sync without "drop" → it is pruned, along with its user_badges row.
        await badge_svc.sync_trophies(session, [_row("keep")])
        slugs = {b.slug for b in await badge_svc.get_all_badges(session)}
        assert slugs == {"keep"}
        assert await badge_svc.get_user_badges(session, 1) == []
