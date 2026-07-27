"""Integration tests for services/badge_service.py."""

from __future__ import annotations


import services.badge_service as badge_svc
from database.models import Badge
from services import catalog_loader

_DEFAULT_COUNT = len(catalog_loader.DEFAULT_TROPHIES)


# ---------------------------------------------------------------------------
# seed_badges (trophy catalog sync)
# ---------------------------------------------------------------------------

class TestSeedBadges:
    async def test_creates_all_default_trophies(self, session):
        await badge_svc.seed_badges(session)

        from sqlalchemy import select
        badges = list((await session.execute(select(Badge))).scalars())
        assert len(badges) == _DEFAULT_COUNT

    async def test_idempotent_second_call(self, session):
        await badge_svc.seed_badges(session)
        await badge_svc.seed_badges(session)

        from sqlalchemy import select
        badges = list((await session.execute(select(Badge))).scalars())
        assert len(badges) == _DEFAULT_COUNT

    async def test_all_expected_slugs_present(self, session):
        await badge_svc.seed_badges(session)
        badges = await badge_svc.get_all_badges(session)
        slugs = {b.slug for b in badges}
        assert {
            "first_steps", "rich_1k", "rich_10k",
            "streak_7", "streak_30", "bet_winner", "generous",
        } <= slugs

    async def test_every_trophy_has_a_rarity(self, session):
        await badge_svc.seed_badges(session)
        for badge in await badge_svc.get_all_badges(session):
            assert badge.rarity in ("bronze", "silver", "gold", "platinum")
            assert badge.xp_reward >= 0


# ---------------------------------------------------------------------------
# award_badge
# ---------------------------------------------------------------------------

class TestAwardBadge:
    async def test_grants_new_badge(self, seeded_session, user_factory):
        await user_factory(tg_id=100)
        ub, is_new = await badge_svc.award_badge(seeded_session, 100, "first_steps")
        assert is_new is True
        assert ub is not None

    async def test_idempotent_second_award(self, seeded_session, user_factory):
        await user_factory(tg_id=101)
        _, is_new_1 = await badge_svc.award_badge(seeded_session, 101, "first_steps")
        await seeded_session.commit()
        _, is_new_2 = await badge_svc.award_badge(seeded_session, 101, "first_steps")
        assert is_new_1 is True
        assert is_new_2 is False

    async def test_unknown_slug_returns_none_false(self, seeded_session, user_factory):
        await user_factory(tg_id=102)
        result, is_new = await badge_svc.award_badge(seeded_session, 102, "nonexistent")
        assert result is None
        assert is_new is False


# ---------------------------------------------------------------------------
# check_and_award_milestones
# ---------------------------------------------------------------------------

class TestCheckAndAwardMilestones:
    async def test_no_badges_when_conditions_unmet(self, seeded_session, user_factory):
        await user_factory(tg_id=200, coins=0)
        earned = await badge_svc.check_and_award_milestones(seeded_session, 200)
        assert earned == []

    async def test_awards_first_steps_when_onboarding_completed(self, seeded_session, user_factory):
        user, _ = await user_factory(tg_id=201, coins=0)
        user.onboarding_completed = True
        await seeded_session.commit()

        earned = await badge_svc.check_and_award_milestones(seeded_session, 201)
        assert any(b.slug == "first_steps" for b in earned)

    async def test_awards_rich_1k_at_1000_coins(self, seeded_session, user_factory):
        await user_factory(tg_id=202, coins=1000)
        earned = await badge_svc.check_and_award_milestones(seeded_session, 202)
        assert any(b.slug == "rich_1k" for b in earned)

    async def test_awards_both_rich_badges_at_10k(self, seeded_session, user_factory):
        await user_factory(tg_id=203, coins=10000)
        earned = await badge_svc.check_and_award_milestones(seeded_session, 203)
        slugs = {b.slug for b in earned}
        assert "rich_1k" in slugs
        assert "rich_10k" in slugs

    async def test_awards_streak_7(self, seeded_session, user_factory):
        await user_factory(tg_id=204, coins=0, daily_streak=7)
        earned = await badge_svc.check_and_award_milestones(seeded_session, 204)
        assert any(b.slug == "streak_7" for b in earned)

    async def test_awards_both_streak_badges_at_30(self, seeded_session, user_factory):
        await user_factory(tg_id=205, coins=0, daily_streak=30)
        earned = await badge_svc.check_and_award_milestones(seeded_session, 205)
        slugs = {b.slug for b in earned}
        assert "streak_7" in slugs
        assert "streak_30" in slugs

    async def test_awards_bet_winner(self, seeded_session, user_factory):
        await user_factory(tg_id=206, coins=0, bets_won=1)
        earned = await badge_svc.check_and_award_milestones(seeded_session, 206)
        assert any(b.slug == "bet_winner" for b in earned)

    async def test_awards_generous(self, seeded_session, user_factory):
        await user_factory(tg_id=207, coins=0, transfers_made=1)
        earned = await badge_svc.check_and_award_milestones(seeded_session, 207)
        assert any(b.slug == "generous" for b in earned)

    async def test_does_not_re_award_already_earned_badges(self, seeded_session, user_factory):
        await user_factory(tg_id=208, coins=0, bets_won=1)
        first = await badge_svc.check_and_award_milestones(seeded_session, 208)
        await seeded_session.commit()
        second = await badge_svc.check_and_award_milestones(seeded_session, 208)

        first_slugs = {b.slug for b in first}
        second_slugs = {b.slug for b in second}
        assert first_slugs & second_slugs == set()

    async def test_returns_empty_for_nonexistent_user(self, seeded_session):
        result = await badge_svc.check_and_award_milestones(seeded_session, 99999)
        assert result == []

    async def test_balance_just_below_threshold_awards_nothing(self, seeded_session, user_factory):
        await user_factory(tg_id=209, coins=999)
        earned = await badge_svc.check_and_award_milestones(seeded_session, 209)
        slugs = {b.slug for b in earned}
        assert "rich_1k" not in slugs


# ---------------------------------------------------------------------------
# get_user_badges
# ---------------------------------------------------------------------------

class TestGetUserBadges:
    async def test_returns_badges_with_badge_relationship_loaded(self, seeded_session, user_factory):
        await user_factory(tg_id=300, coins=0, bets_won=1)
        await badge_svc.check_and_award_milestones(seeded_session, 300)
        await seeded_session.commit()

        user_badges = await badge_svc.get_user_badges(seeded_session, 300)
        assert len(user_badges) >= 1
        for ub in user_badges:
            assert ub.badge is not None
            assert isinstance(ub.badge.slug, str)

    async def test_returns_empty_for_user_with_no_badges(self, seeded_session, user_factory):
        await user_factory(tg_id=301, coins=0)
        result = await badge_svc.get_user_badges(seeded_session, 301)
        assert result == []


# ---------------------------------------------------------------------------
# get_all_badges
# ---------------------------------------------------------------------------

class TestGetAllBadges:
    async def test_returns_all_seeded_badges_ordered_by_id(self, seeded_session):
        badges = await badge_svc.get_all_badges(seeded_session)
        assert len(badges) == _DEFAULT_COUNT
        ids = [b.id for b in badges]
        assert ids == sorted(ids)

    async def test_returns_empty_before_seeding(self, session):
        badges = await badge_svc.get_all_badges(session)
        assert badges == []
