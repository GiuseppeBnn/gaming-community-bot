"""Integration tests for services/progress_service.py (game podium tallies)."""

from __future__ import annotations

import pytest

from services import progress_service

pytestmark = pytest.mark.asyncio


class TestRecordPodium:
    async def test_counts_podiums_and_first_places(self, session, user_factory):
        await user_factory(tg_id=1)
        # Two trivia podiums (one a first place) + one guess first place.
        await progress_service.record_podium(session, 1, "trivia", 1, ref_id=10)
        await progress_service.record_podium(session, 1, "trivia", 3, ref_id=11)
        await progress_service.record_podium(session, 1, "guess", 1, ref_id=20)
        await session.commit()

        tallies = await progress_service.podium_counts(session, 1)
        assert tallies["trivia"].podiums == 2
        assert tallies["trivia"].first_places == 1
        assert tallies["guess"].podiums == 1
        assert tallies["guess"].first_places == 1

    async def test_any_aggregates_all_games(self, session, user_factory):
        await user_factory(tg_id=1)
        await progress_service.record_podium(session, 1, "trivia", 1)
        await progress_service.record_podium(session, 1, "guess", 2)
        await session.commit()

        tallies = await progress_service.podium_counts(session, 1)
        assert tallies["any"].podiums == 2
        assert tallies["any"].first_places == 1

    async def test_empty_user(self, session, user_factory):
        await user_factory(tg_id=1)
        tallies = await progress_service.podium_counts(session, 1)
        assert "any" in tallies
        assert tallies["any"].podiums == 0
