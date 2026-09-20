"""Unit tests for services/xp_service.py — rank derivation + daily cap logic."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from database.models import User
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

    def test_progress_bar_fills_toward_next_level(self):
        # Regression (note.txt): the bar fills xp_into_level / xp_for_next, where
        # xp_for_next is the FULL cost of the level. The old bug used
        # xp_into_level / (xp_into_level + xp_for_next), which kept the bar stuck
        # around half and made the profile show a denominator like "143/295" that
        # then *shrank* at the next level. Here it must reach (near) full instead.
        floor = xp_service.xp_to_reach_level(4)
        cost = xp_service._level_cost(4)
        assert xp_service.progress_bar(xp_service.level_for_xp(floor), width=6) == "▱" * 6
        near_top = xp_service.level_for_xp(floor + cost - 1)  # one XP short of level 5
        assert xp_service.progress_bar(near_top, width=6).count("▰") >= 5

    def test_level_cost_strictly_increases(self):
        # The "level 4 needs 295, level 5 needs less" report was a display bug, not the
        # curve: each level's real cost is strictly greater than the previous one.
        costs = [xp_service._level_cost(n) for n in range(1, 12)]
        assert all(b > a for a, b in zip(costs, costs[1:], strict=False))


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

    async def test_twenty_questions_reward_is_uncapped(self, session, user_factory):
        await user_factory(tg_id=5)
        res = await xp_service.grant_xp(session, 5, 300, XpSource.twentyq, capped=False)
        await session.commit()

        assert res.granted == 300
        assert (await _xp(session, 5)) == 300

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

    async def test_airdrop_keeps_xp_granted_while_it_runs(self, session, user_factory):
        """An airdrop must **add**, and adding is not the same as writing a total.

        `airdrop_xp` touches every registered user, so the gap between reading the
        table and writing it back is as wide as the table. A quiz closing in that gap
        grants XP through `grant_xp`, which emits `xp = xp + n` in SQL. If the airdrop
        then stores an absolute total computed from what it read *beforehand*, that
        grant is gone — no error, no trace, just a player whose podium XP evaporated.

        One session is enough to reproduce it, and that is the interesting part: the
        identity map serves entity selects from cache, so a write this session did not
        make through the ORM is exactly as invisible to it as another transaction's
        commit would be. The statement below is what `grant_xp` emits, verbatim.
        """
        user, _ = await user_factory(tg_id=32, xp=0)

        await session.execute(
            update(User)
            .where(User.tg_id == 32)
            .values(xp=User.xp + 500)
            .execution_options(synchronize_session=False)
        )
        assert user.xp == 0, "precondition: the loaded instance has not seen the grant"

        count = await xp_service.airdrop_xp(session, 100)
        await session.commit()

        assert count == 1
        assert (await _xp(session, 32)) == 600, "the 500 XP granted meanwhile was lost"

    async def test_airdrop_updates_every_tier_it_crosses(self, session, user_factory):
        """One airdrop, two users, two different destination tiers.

        `User.rank_slug` is a cached string derived from XP, and the airdrop writes it
        grouped by destination tier instead of user by user. Grouping is exactly the
        kind of thing that works by accident when everybody lands in the same bucket,
        so this pins two buckets. Thresholds come from the curve itself rather than
        being hard-coded, so retuning `xp_level_base` cannot make this test lie.
        """
        step = xp_service.xp_to_reach_level(6)  # level 6 = Iniziato
        await user_factory(tg_id=40, xp=0)
        await user_factory(tg_id=41, xp=xp_service.xp_to_reach_level(11) - step)

        await xp_service.airdrop_xp(session, step)
        await session.commit()

        assert (await _rank_slug(session, 40)) == "iniziato"
        assert (await _rank_slug(session, 41)) == "esperto"


async def _xp(session, tg_id):
    """The XP the database actually holds.

    Selects the **column**, not the `User` entity, on purpose: an entity select can
    be served from the identity map, so it would happily report the value the test
    already had in memory instead of the one that was written (STEERING §5). Every
    XP assertion in this file goes through here for that reason.
    """
    from sqlalchemy import select

    from database.models import User
    return (
        await session.execute(select(User.xp).where(User.tg_id == tg_id))
    ).scalar_one()


async def _rank_slug(session, tg_id):
    """The stored tier, read as a column for the same reason as `_xp`."""
    from sqlalchemy import select

    from database.models import User
    return (
        await session.execute(select(User.rank_slug).where(User.tg_id == tg_id))
    ).scalar_one()
