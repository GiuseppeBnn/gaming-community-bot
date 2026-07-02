"""Integration tests for services/bet_service.py."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import services.bet_service as bet_svc
from database.models import (
    BettingEvent,
    BettingOption,
    BetStatus,
    EventStatus,
    ScheduledTask,
    User,
    UserBet,
)
from exceptions.economy import (
    AlreadyBetError,
    BettingClosedError,
    EventNotFoundError,
    InsufficientFundsError,
)
from exceptions.economy import EventAlreadySettledError
from services import schedule_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_event(session, creator_tg_id: int = 1) -> BettingEvent:
    event = await bet_svc.create_event(
        session,
        creator_tg_id=creator_tg_id,
        title="Test Event",
        description="A test betting event",
        options=[
            {"label": "Option A", "odds": 2.0},
            {"label": "Option B", "odds": 3.0},
        ],
    )
    await session.commit()
    # Reload options eagerly to avoid async lazy-load errors when tests access event.options.
    # NOTE: do NOT selectinload user_bets here — pre-loading an empty collection marks it
    # as "loaded" in SQLAlchemy's identity map, causing resolve_event's selectinload to be
    # skipped (cache hit with empty list), which makes pending_bets appear empty.
    result = await session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event.id)
        .options(selectinload(BettingEvent.options))
    )
    return result.scalar_one()


async def _place_bet_for(session, user_tg_id: int, event: BettingEvent, option_index: int = 0, amount: int = 100):
    option = event.options[option_index]
    bet = await bet_svc.place_bet(
        session,
        user_tg_id=user_tg_id,
        event_id=event.id,
        option_id=option.id,
        amount=amount,
    )
    await session.commit()
    return bet


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------

class TestCreateEvent:
    async def test_creates_event_with_options(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _create_event(session, creator_tg_id=1)

        assert event.id is not None
        assert event.title == "Test Event"
        assert event.status == EventStatus.open.value
        assert len(event.options) == 2

    async def test_option_labels_correct(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _create_event(session, creator_tg_id=1)

        labels = {o.label for o in event.options}
        assert labels == {"Option A", "Option B"}

    async def test_option_odds_stored(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _create_event(session, creator_tg_id=1)

        opt_a = next(o for o in event.options if o.label == "Option A")
        assert opt_a.odds_multiplier == pytest.approx(2.0)

    async def test_initial_total_wagered_is_zero(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _create_event(session, creator_tg_id=1)

        for opt in event.options:
            assert opt.total_wagered == 0


# ---------------------------------------------------------------------------
# place_bet
# ---------------------------------------------------------------------------

class TestPlaceBet:
    async def test_creates_user_bet(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        event = await _create_event(session, creator_tg_id=1)

        bet = await _place_bet_for(session, user_tg_id=1, event=event, amount=100)

        assert bet.id is not None
        assert bet.user_tg_id == 1
        assert bet.amount == 100
        assert bet.status == BetStatus.pending.value

    async def test_debits_user_wallet(self, session, user_factory):
        _, wallet = await user_factory(tg_id=1, coins=500)
        event = await _create_event(session, creator_tg_id=1)

        await _place_bet_for(session, user_tg_id=1, event=event, amount=100)

        assert wallet.coins == 400

    async def test_increments_option_total_wagered(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        event = await _create_event(session, creator_tg_id=1)

        opt = event.options[0]
        await _place_bet_for(session, user_tg_id=1, event=event, option_index=0, amount=250)

        assert opt.total_wagered == 250

    async def test_raises_event_not_found(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)

        with pytest.raises(EventNotFoundError):
            await bet_svc.place_bet(session, 1, event_id=9999, option_id=1, amount=100)

    async def test_raises_betting_closed_when_locked(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        event = await _create_event(session, creator_tg_id=1)
        event_id = event.id
        opt_id = event.options[0].id
        await bet_svc.lock_event(session, event_id)
        await session.commit()

        with pytest.raises(BettingClosedError):
            await bet_svc.place_bet(session, 1, event_id, opt_id, 100)

    async def test_raises_already_bet_on_duplicate(self, session, user_factory):
        await user_factory(tg_id=1, coins=1000)
        event = await _create_event(session, creator_tg_id=1)
        event_id = event.id
        opt_id = event.options[0].id
        await _place_bet_for(session, user_tg_id=1, event=event, amount=100)

        # place_bet triggers session.rollback() on IntegrityError, which expires all
        # objects in the session — capture plain-int ids before the call.
        with pytest.raises(AlreadyBetError) as exc:
            await bet_svc.place_bet(session, 1, event_id, opt_id, 100)

        assert exc.value.user_tg_id == 1
        assert exc.value.event_id == event_id

    async def test_raises_insufficient_funds(self, session, user_factory):
        await user_factory(tg_id=1, coins=10)
        event = await _create_event(session, creator_tg_id=1)
        event_id = event.id
        opt_id = event.options[0].id

        with pytest.raises(InsufficientFundsError):
            await bet_svc.place_bet(session, 1, event_id, opt_id, 100)

    async def test_multiple_users_can_bet_same_event(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        await user_factory(tg_id=2, coins=500)
        event = await _create_event(session, creator_tg_id=1)

        await _place_bet_for(session, user_tg_id=1, event=event, option_index=0, amount=100)
        await _place_bet_for(session, user_tg_id=2, event=event, option_index=1, amount=200)

        result = await session.execute(select(UserBet).where(UserBet.event_id == event.id))
        bets = list(result.scalars())
        assert len(bets) == 2


# ---------------------------------------------------------------------------
# lock_event
# ---------------------------------------------------------------------------

class TestLockEvent:
    async def test_changes_status_to_locked(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _create_event(session, 1)

        await bet_svc.lock_event(session, event.id)
        await session.commit()

        assert event.status == EventStatus.locked.value
        assert event.locked_at is not None

    async def test_idempotent_lock(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _create_event(session, 1)

        result1 = await bet_svc.lock_event(session, event.id)
        await session.commit()
        result2 = await bet_svc.lock_event(session, event.id)  # should not raise

        assert result2.status == EventStatus.locked.value

    async def test_raises_event_not_found(self, session):
        with pytest.raises(EventNotFoundError):
            await bet_svc.lock_event(session, 9999)

    async def test_raises_already_settled_when_resolved(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        event = await _create_event(session, 1)
        await _place_bet_for(session, 1, event, option_index=0, amount=100)
        await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        with pytest.raises(EventAlreadySettledError):
            await bet_svc.lock_event(session, event.id)


# ---------------------------------------------------------------------------
# resolve_event
# ---------------------------------------------------------------------------

class TestResolveEvent:
    async def test_winning_bets_credited(self, session, user_factory):
        _, w1 = await user_factory(tg_id=1, coins=1000)
        _, w2 = await user_factory(tg_id=2, coins=1000)
        event = await _create_event(session, creator_tg_id=1)

        opt_a = event.options[0]
        opt_b = event.options[1]

        await _place_bet_for(session, 1, event, option_index=0, amount=500)
        await _place_bet_for(session, 2, event, option_index=1, amount=500)
        # Pot = 1000, winner is option A (user 1 bet 500)

        result = await bet_svc.resolve_event(session, event.id, opt_a.id)
        await session.commit()

        assert result["total_pot"] == 1000
        assert result["winners"] == 1
        assert w1.coins == 500 + 1000  # initial 1000 - bet 500 + win 1000

    async def test_losing_bets_marked_lost(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        await user_factory(tg_id=2, coins=500)
        event = await _create_event(session, 1)

        await _place_bet_for(session, 1, event, option_index=0, amount=100)
        await _place_bet_for(session, 2, event, option_index=1, amount=200)

        await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        result = await session.execute(
            select(UserBet).where(UserBet.user_tg_id == 2)
        )
        loser_bet = result.scalar_one()
        assert loser_bet.status == BetStatus.lost.value

    async def test_winner_bets_marked_won(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        event = await _create_event(session, 1)
        await _place_bet_for(session, 1, event, option_index=0, amount=100)

        await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        result = await session.execute(select(UserBet).where(UserBet.user_tg_id == 1))
        bet = result.scalar_one()
        assert bet.status == BetStatus.won.value

    async def test_proportional_payout_multiple_winners(self, session, user_factory):
        _, w1 = await user_factory(tg_id=1, coins=1000)
        _, w2 = await user_factory(tg_id=2, coins=1000)
        await user_factory(tg_id=3, coins=600)
        event = await _create_event(session, 1)

        opt_win = event.options[0]
        # User 1 bets 400 on winner, user 2 bets 200 on winner, user 3 bets 600 on loser
        await _place_bet_for(session, 1, event, option_index=0, amount=400)
        await _place_bet_for(session, 2, event, option_index=0, amount=200)
        await _place_bet_for(session, 3, event, option_index=1, amount=600)

        result = await bet_svc.resolve_event(session, event.id, opt_win.id)
        await session.commit()

        assert result["total_pot"] == 1200
        assert result["total_distributed"] == 1200

        # User 1 gets ~667 (400/600 * 1200), user 2 gets ~533 (200/600 * 1200)
        total_out = sum(w["payout"] for w in result["winners_data"])
        assert total_out == 1200

    async def test_increments_bets_won_counter(self, session, user_factory):
        user1, _ = await user_factory(tg_id=1, coins=500)
        await user_factory(tg_id=2, coins=500)
        event = await _create_event(session, 1)

        await _place_bet_for(session, 1, event, option_index=0, amount=100)
        await _place_bet_for(session, 2, event, option_index=1, amount=100)

        assert user1.bets_won == 0
        await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        assert user1.bets_won == 1

    async def test_loser_bets_won_not_incremented(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        user2, _ = await user_factory(tg_id=2, coins=500)
        event = await _create_event(session, 1)

        await _place_bet_for(session, 1, event, option_index=0, amount=100)
        await _place_bet_for(session, 2, event, option_index=1, amount=100)

        await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        assert user2.bets_won == 0

    async def test_raises_already_settled_on_double_resolve(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        event = await _create_event(session, 1)
        await _place_bet_for(session, 1, event, option_index=0, amount=100)

        await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        with pytest.raises(EventAlreadySettledError):
            await bet_svc.resolve_event(session, event.id, event.options[0].id)

    async def test_event_with_no_bets_distributes_zero(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _create_event(session, 1)

        result = await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        assert result["total_pot"] == 0
        assert result["total_distributed"] == 0
        assert result["winners"] == 0


# ---------------------------------------------------------------------------
# cancel_event
# ---------------------------------------------------------------------------

class TestCancelEvent:
    async def test_refunds_all_pending_bets(self, session, user_factory):
        _, w1 = await user_factory(tg_id=1, coins=500)
        _, w2 = await user_factory(tg_id=2, coins=500)
        event = await _create_event(session, 1)

        await _place_bet_for(session, 1, event, option_index=0, amount=200)
        await _place_bet_for(session, 2, event, option_index=1, amount=100)

        result = await bet_svc.cancel_event(session, event.id)
        await session.commit()

        assert result["refunded"] == 2
        assert w1.coins == 500   # all coins returned
        assert w2.coins == 500

    async def test_marks_bets_as_refunded(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        event = await _create_event(session, 1)
        await _place_bet_for(session, 1, event, option_index=0, amount=100)

        await bet_svc.cancel_event(session, event.id)
        await session.commit()

        result = await session.execute(select(UserBet).where(UserBet.user_tg_id == 1))
        bet = result.scalar_one()
        assert bet.status == BetStatus.refunded.value

    async def test_event_status_set_to_cancelled(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _create_event(session, 1)
        await bet_svc.cancel_event(session, event.id)
        await session.commit()

        assert event.status == EventStatus.cancelled.value

    async def test_raises_event_not_found(self, session):
        with pytest.raises(EventNotFoundError):
            await bet_svc.cancel_event(session, 9999)

    async def test_raises_already_settled_on_double_cancel(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _create_event(session, 1)
        await bet_svc.cancel_event(session, event.id)
        await session.commit()

        with pytest.raises(EventAlreadySettledError):
            await bet_svc.cancel_event(session, event.id)

    async def test_cancel_after_resolve_raises(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        event = await _create_event(session, 1)
        await _place_bet_for(session, 1, event, option_index=0, amount=100)
        await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        with pytest.raises(EventAlreadySettledError):
            await bet_svc.cancel_event(session, event.id)


# ---------------------------------------------------------------------------
# get_open_events / get_all_active_events / get_event_detail
# ---------------------------------------------------------------------------

class TestEventQueries:
    async def test_get_open_events_returns_only_open(self, session, user_factory):
        await user_factory(tg_id=1, coins=200)
        ev1 = await _create_event(session, 1)
        ev2 = await _create_event(session, 1)
        # Lock one event
        await bet_svc.lock_event(session, ev2.id)
        await session.commit()

        open_events = await bet_svc.get_open_events(session)
        ids = {e.id for e in open_events}
        assert ev1.id in ids
        assert ev2.id not in ids

    async def test_get_open_events_empty_when_none(self, session):
        result = await bet_svc.get_open_events(session)
        assert result == []

    async def test_get_all_active_events_includes_locked(self, session, user_factory):
        await user_factory(tg_id=1, coins=200)
        ev1 = await _create_event(session, 1)
        ev2 = await _create_event(session, 1)
        await bet_svc.lock_event(session, ev2.id)
        await session.commit()

        active = await bet_svc.get_all_active_events(session)
        ids = {e.id for e in active}
        assert ev1.id in ids
        assert ev2.id in ids

    async def test_get_all_active_events_excludes_resolved(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        ev1 = await _create_event(session, 1)
        ev2 = await _create_event(session, 1)
        await _place_bet_for(session, 1, ev1, option_index=0, amount=100)
        await bet_svc.resolve_event(session, ev1.id, ev1.options[0].id)
        await session.commit()

        active = await bet_svc.get_all_active_events(session)
        ids = {e.id for e in active}
        assert ev1.id not in ids
        assert ev2.id in ids

    async def test_get_event_detail_returns_event_with_relations(self, session, user_factory):
        await user_factory(tg_id=1, coins=500)
        event = await _create_event(session, 1)
        await _place_bet_for(session, 1, event, option_index=0, amount=100)

        detail = await bet_svc.get_event_detail(session, event.id)
        assert detail is not None
        assert len(detail.options) == 2
        assert len(detail.user_bets) == 1

    async def test_get_event_detail_returns_none_for_unknown(self, session):
        result = await bet_svc.get_event_detail(session, 9999)
        assert result is None


# ---------------------------------------------------------------------------
# Event XP — participation on placement + win bonus, both uncapped (note.txt)
# ---------------------------------------------------------------------------

class TestBetXp:
    async def _xp(self, session, uid):
        return (await session.execute(select(User.xp).where(User.tg_id == uid))).scalar_one()

    async def test_placing_a_bet_grants_participation_xp(self, session, user_factory, monkeypatch):
        monkeypatch.setattr(bet_svc.settings, "xp_per_bet_placed", 10)
        await user_factory(tg_id=1, coins=500, xp=0)
        event = await _create_event(session, 1)
        await _place_bet_for(session, 1, event, option_index=0, amount=100)
        assert await self._xp(session, 1) == 10

    async def test_win_adds_bonus_xp_and_is_uncapped(self, session, user_factory, monkeypatch):
        monkeypatch.setattr(bet_svc.settings, "xp_per_bet_placed", 10)
        monkeypatch.setattr(bet_svc.settings, "xp_per_bet_won", 25)
        # A tiny daily cap must NOT clip event XP — proves the win bonus is uncapped now.
        monkeypatch.setattr(bet_svc.settings, "xp_daily_participation_cap", 5)
        await user_factory(tg_id=1, coins=1000, xp=0)
        await user_factory(tg_id=2, coins=1000, xp=0)
        event = await _create_event(session, 1)
        await _place_bet_for(session, 1, event, option_index=0, amount=500)
        await _place_bet_for(session, 2, event, option_index=1, amount=500)

        await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        assert await self._xp(session, 1) == 35  # 10 placed + 25 won, not clipped by cap=5
        assert await self._xp(session, 2) == 10  # loser keeps only the participation XP


# ---------------------------------------------------------------------------
# Betting window (timed auto-close)
# ---------------------------------------------------------------------------

async def _open_event_with_window(session, window_seconds, *, status=EventStatus.open.value):
    event = await bet_svc.create_event(
        session,
        creator_tg_id=1,
        title="Timed",
        description="d",
        options=[{"label": "A"}, {"label": "B"}],
        status=status,
        window_seconds=window_seconds,
    )
    await session.commit()
    return event


class TestBettingWindow:
    async def test_open_with_window_arms_deadline(self, session, user_factory):
        await user_factory(tg_id=1)
        before = schedule_service.utcnow()
        event = await _open_event_with_window(session, 900)  # 15 min

        assert event.betting_window_seconds == 900
        assert event.closes_at is not None
        assert timedelta(minutes=14) < event.closes_at - before < timedelta(minutes=16)

    async def test_unlimited_window_leaves_no_deadline(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _open_event_with_window(session, None)
        assert event.closes_at is None

    async def test_draft_arms_only_on_activation(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _open_event_with_window(session, 1800, status=EventStatus.draft.value)
        assert event.closes_at is None  # still a draft, window not started

        activated = await bet_svc.activate_event(session, event.id)
        await session.commit()
        assert activated.closes_at is not None

    async def test_schedule_close_creates_pending_lock_task(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _open_event_with_window(session, 900)

        task = await bet_svc.schedule_close(session, event, created_by_tg_id=1, group_id=123)
        await session.commit()

        assert task is not None
        assert task.task_type == "bet"
        assert task.ref_id == event.id
        assert task.status == "pending"
        assert task.run_at == event.closes_at
        assert schedule_service.task_payload(task) == {"action": "lock"}

    async def test_schedule_close_noop_for_unlimited(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _open_event_with_window(session, None)
        assert await bet_svc.schedule_close(session, event, 1, 123) is None

    async def test_place_bet_rejected_after_deadline(self, session, user_factory):
        await user_factory(tg_id=1, coins=1000)
        event = await _open_event_with_window(session, 900)
        # Force the window shut without changing status (scheduler tick not yet fired).
        event.closes_at = schedule_service.utcnow() - timedelta(seconds=1)
        await session.commit()

        option = (
            await session.execute(select(BettingOption).where(BettingOption.event_id == event.id))
        ).scalars().first()
        with pytest.raises(BettingClosedError):
            await bet_svc.place_bet(session, 1, event.id, option.id, 100)

    async def test_cancel_pending_close_cancels_lock_task(self, session, user_factory):
        await user_factory(tg_id=1)
        event = await _open_event_with_window(session, 900)
        task = await bet_svc.schedule_close(session, event, 1, 123)
        await session.commit()
        task_id = task.id

        await bet_svc.cancel_pending_close(session, event.id)
        await session.commit()

        session.expire_all()
        reloaded = (
            await session.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
        ).scalar_one()
        assert reloaded.status == "cancelled"
