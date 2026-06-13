"""Integration tests for the betting hardening (Phase E):
no betting on a locked event, and the atomic total_wagered increment.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import services.bet_service as bet_svc
from database.models import BettingEvent, BettingOption
from exceptions.economy import BettingClosedError


async def _create_event(session, creator_tg_id: int = 1) -> BettingEvent:
    event = await bet_svc.create_event(
        session,
        creator_tg_id=creator_tg_id,
        title="Test Event",
        description="d",
        options=[{"label": "A"}, {"label": "B"}],
    )
    await session.commit()
    result = await session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event.id)
        .options(selectinload(BettingEvent.options))
    )
    return result.scalar_one()


class TestBetOnLockedEvent:
    async def test_place_bet_rejected_after_lock(self, session, user_factory):
        await user_factory(tg_id=1, coins=1000)
        event = await _create_event(session, 1)
        await bet_svc.lock_event(session, event.id)
        await session.commit()

        with pytest.raises(BettingClosedError):
            await bet_svc.place_bet(session, 1, event.id, event.options[0].id, 100)


class TestTotalWageredAtomic:
    async def test_concurrent_style_bets_accumulate(self, session, user_factory):
        await user_factory(tg_id=1, coins=1000)
        await user_factory(tg_id=2, coins=1000)
        event = await _create_event(session, 1)
        opt_id = event.options[0].id

        await bet_svc.place_bet(session, 1, event.id, opt_id, 100)
        await session.commit()
        await bet_svc.place_bet(session, 2, event.id, opt_id, 150)
        await session.commit()

        # The atomic UPDATE bypasses the ORM identity map → expire to read DB truth.
        session.expire_all()
        row = (
            await session.execute(select(BettingOption).where(BettingOption.id == opt_id))
        ).scalar_one()
        assert row.total_wagered == 250
