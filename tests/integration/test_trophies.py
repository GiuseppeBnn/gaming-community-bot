"""Integration tests for the trophy system (rarity, xp condition, CSV upsert)."""

from __future__ import annotations

import pytest

import services.badge_service as badge_svc

pytestmark = pytest.mark.asyncio


def _row(slug, **over):
    base = {
        "slug": slug, "name": slug.title(), "description": "d", "icon_emoji": "🏅",
        "category": "g", "rarity": "bronze", "xp_reward": 0,
        "condition_type": None, "condition_value": None,
    }
    base.update(over)
    return base


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
