"""Unit tests for services/xp_service.py — rank derivation + daily cap logic."""

from __future__ import annotations

import pytest

from services import catalog_loader, xp_service
from services.catalog_loader import Rank
from services.xp_service import XpSource


@pytest.fixture(autouse=True)
def default_ranks():
    """Ensure the default rank registry is loaded (some tests mutate it)."""
    catalog_loader._ranks = list(catalog_loader.DEFAULT_RANKS)
    yield
    catalog_loader._ranks = list(catalog_loader.DEFAULT_RANKS)


# ---------------------------------------------------------------------------
# Levels (geometric curve) + tier derivation (pure)
# ---------------------------------------------------------------------------

class TestLevels:
    def test_zero_xp_is_level_one(self):
        p = xp_service.level_for_xp(0)
        assert p.level == 1
        assert p.xp_into_level == 0
        assert p.floor_xp == 0
        assert p.xp_for_next == xp_service._level_cost(1)

    def test_reach_level_roundtrip(self):
        # The exact cumulative XP for level L resolves back to L; one short → L-1.
        for L in (1, 2, 6, 11, 20, 30):
            xp = xp_service.xp_to_reach_level(L)
            assert xp_service.level_for_xp(xp).level == L
            if L > 1:
                assert xp_service.level_for_xp(xp - 1).level == L - 1

    def test_progress_within_level(self):
        floor = xp_service.xp_to_reach_level(3)
        cost = xp_service._level_cost(3)
        p = xp_service.level_for_xp(floor + cost - 1)  # one XP short of level 4
        assert p.level == 3
        assert p.xp_into_level == cost - 1
        assert p.xp_for_next == cost

    def test_level_monotonic_in_xp(self):
        last = 0
        for xp in range(0, 30000, 91):
            lvl = xp_service.level_for_xp(xp).level
            assert lvl >= last
            last = lvl

    def test_progress_bar_bounds(self):
        empty = xp_service.progress_bar(xp_service.level_for_xp(0), width=6)
        assert empty == "▱" * 6
        bar = xp_service.progress_bar(xp_service.level_for_xp(150), width=6)
        assert len(bar) == 6 and set(bar) <= {"▰", "▱"}


# ---------------------------------------------------------------------------
# rank_for_xp / rank_for_level (named tiers keyed by level)
# ---------------------------------------------------------------------------

class TestRankForXp:
    def test_zero_xp_is_first_rank(self):
        assert xp_service.rank_for_xp(0).slug == "novizio"

    def test_picks_highest_tier_reached(self):
        # Default tiers start at levels 1/6/11/16/21/26.
        assert xp_service.rank_for_level(5).slug == "novizio"
        assert xp_service.rank_for_level(6).slug == "iniziato"
        assert xp_service.rank_for_level(11).slug == "esperto"
        assert xp_service.rank_for_level(999).slug == "leggenda"
        # Via XP: just below the level-6 threshold is still novizio.
        boundary = xp_service.xp_to_reach_level(6)
        assert xp_service.rank_for_xp(boundary).slug == "iniziato"
        assert xp_service.rank_for_xp(boundary - 1).slug == "novizio"

    def test_none_when_below_first_tier(self):
        catalog_loader._ranks = [Rank("vet", "Vet", "🎖️", 5)]  # tier starts at level 5
        assert xp_service.rank_for_level(4) is None
        assert xp_service.rank_for_level(5).slug == "vet"

    def test_monotonic_non_decreasing(self):
        last = -1
        for xp in range(0, 30000, 137):
            rank = xp_service.rank_for_xp(xp)
            assert rank is not None
            assert rank.min_level >= last
            last = rank.min_level


# ---------------------------------------------------------------------------
# grant_xp — uncapped (events)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGrantUncapped:
    async def test_grants_full_amount(self, session, user_factory):
        await user_factory(tg_id=1)
        res = await xp_service.grant_xp(session, 1, 300, XpSource.quiz, capped=False)
        await session.commit()
        assert res.granted == 300
        assert (await _xp(session, 1)) == 300

    async def test_rank_up_reported_on_promotion(self, session, user_factory):
        await user_factory(tg_id=2)
        target = xp_service.xp_to_reach_level(6)  # first XP that reaches the iniziato tier
        res = await xp_service.grant_xp(session, 2, target, XpSource.quiz, capped=False)
        assert res.new_rank is not None
        assert res.new_rank.slug == "iniziato"
        assert res.leveled_up is True
        assert res.new_level == 6

    async def test_no_rank_up_within_same_rank(self, session, user_factory):
        await user_factory(tg_id=3)
        res = await xp_service.grant_xp(session, 3, 10, XpSource.quiz, capped=False)
        assert res.new_rank is None  # still novizio, still level 1
        assert res.leveled_up is False

    async def test_level_up_without_tier_up(self, session, user_factory):
        await user_factory(tg_id=4)
        res = await xp_service.grant_xp(
            session, 4, xp_service.xp_to_reach_level(2), XpSource.quiz, capped=False
        )
        assert res.leveled_up is True
        assert res.new_level == 2
        assert res.new_rank is None  # crossed a level but still the novizio tier

    async def test_unknown_user_is_noop(self, session):
        res = await xp_service.grant_xp(session, 999, 100, XpSource.quiz, capped=False)
        assert res.granted == 0


# ---------------------------------------------------------------------------
# grant_xp — capped (participation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGrantCapped:
    async def test_respects_daily_cap(self, session, user_factory, monkeypatch):
        from config_data.config import settings
        monkeypatch.setattr(settings, "xp_daily_participation_cap", 50)
        await user_factory(tg_id=10)

        r1 = await xp_service.grant_xp(session, 10, 40, XpSource.daily, capped=True)
        r2 = await xp_service.grant_xp(session, 10, 40, XpSource.daily, capped=True)
        await session.commit()

        assert r1.granted == 40
        assert r2.granted == 10        # clipped to remaining cap
        assert r2.capped is True
        assert (await _xp(session, 10)) == 50

    async def test_cap_resets_on_new_day(self, session, user_factory, monkeypatch):
        from config_data.config import settings
        monkeypatch.setattr(settings, "xp_daily_participation_cap", 50)
        await user_factory(tg_id=11, xp_today=50, xp_today_date="2000-01-01")
        res = await xp_service.grant_xp(session, 11, 30, XpSource.daily, capped=True)
        assert res.granted == 30       # yesterday's counter reset

    async def test_zero_amount_noop(self, session, user_factory):
        await user_factory(tg_id=12)
        res = await xp_service.grant_xp(session, 12, 0, XpSource.daily, capped=True)
        assert res.granted == 0


# ---------------------------------------------------------------------------
# set_xp / airdrop_xp (admin)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAdminXp:
    async def test_set_xp_absolute(self, session, user_factory):
        await user_factory(tg_id=20, xp=999)
        new = await xp_service.set_xp(session, 20, 100)
        await session.commit()
        assert new == 100
        assert (await _xp(session, 20)) == 100

    async def test_set_xp_clamps_negative(self, session, user_factory):
        await user_factory(tg_id=21, xp=50)
        new = await xp_service.set_xp(session, 21, -5)
        assert new == 0

    async def test_airdrop_adds_to_all(self, session, user_factory):
        await user_factory(tg_id=30, xp=0)
        await user_factory(tg_id=31, xp=100)
        count = await xp_service.airdrop_xp(session, 50)
        await session.commit()
        assert count == 2
        assert (await _xp(session, 30)) == 50
        assert (await _xp(session, 31)) == 150


async def _xp(session, tg_id):
    from sqlalchemy import select
    from database.models import User
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one()
    return user.xp
