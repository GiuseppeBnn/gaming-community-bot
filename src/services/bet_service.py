from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import (
    BetStatus,
    BettingEvent,
    BettingOption,
    EventStatus,
    TransactionType,
    User,
    UserBet,
)
from config_data.config import settings
from exceptions.economy import (
    AlreadyBetError,
    BettingClosedError,
    EventAlreadySettledError,
    EventNotFoundError,
)
from services import economy_service, xp_service
from services.xp_service import XpSource


async def create_event(
    session: AsyncSession,
    creator_tg_id: int,
    title: str,
    description: str,
    options: list[dict],
    status: str = EventStatus.open.value,
) -> BettingEvent:
    """Create a betting event. Defaults to ``open`` (the community flow); pass
    ``status=EventStatus.draft.value`` to pre-create one for the Events hub, to be
    activated/scheduled later."""
    event = BettingEvent(
        title=title,
        description=description,
        creator_tg_id=creator_tg_id,
        status=status,
    )
    session.add(event)
    await session.flush()

    for opt in options:
        session.add(
            BettingOption(
                event_id=event.id,
                label=opt["label"],
                odds_multiplier=float(opt.get("odds", 2.0)),
            )
        )
    return event


async def activate_event(session: AsyncSession, event_id: int) -> BettingEvent:
    """Transition a ``draft`` event to ``open`` so members can bet. Idempotent for
    an already-open event; raises if it was locked/resolved/cancelled."""
    event = (
        await session.execute(select(BettingEvent).where(BettingEvent.id == event_id))
    ).scalar_one_or_none()
    if event is None:
        raise EventNotFoundError(event_id)
    if event.status == EventStatus.open.value:
        return event
    if event.status != EventStatus.draft.value:
        raise EventAlreadySettledError(event_id, event.status)
    event.status = EventStatus.open.value
    return event


async def list_drafts(session: AsyncSession) -> list[BettingEvent]:
    """Pre-created (not yet opened) events, for the Events hub."""
    result = await session.execute(
        select(BettingEvent)
        .where(BettingEvent.status == EventStatus.draft.value)
        .options(selectinload(BettingEvent.options))
        .order_by(BettingEvent.created_at.desc())
    )
    return list(result.scalars().all())


async def place_bet(
    session: AsyncSession,
    user_tg_id: int,
    event_id: int,
    option_id: int,
    amount: int,
) -> UserBet:
    # Lock the event row so its status can't flip to locked/resolved between the
    # check below and the debit (no betting on an event that's being closed).
    event_result = await session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event_id)
        .options(selectinload(BettingEvent.options))
        .with_for_update()
    )
    event = event_result.scalar_one_or_none()
    if event is None:
        raise EventNotFoundError(event_id)
    if event.status != EventStatus.open.value:
        raise BettingClosedError(event_id, event.status)

    option = next((o for o in event.options if o.id == option_id), None)
    if option is None:
        raise EventNotFoundError(event_id)

    await economy_service.debit(
        session,
        user_tg_id,
        amount,
        TransactionType.bet_placed,
        f"Scommessa sull'evento #{event_id}: {event.title}",
        reference_id=event_id,
    )

    # Atomic increment so concurrent bets on the same option can't lose updates.
    await session.execute(
        update(BettingOption)
        .where(BettingOption.id == option_id)
        .values(total_wagered=BettingOption.total_wagered + amount)
    )
    # potential_win is a snapshot at bet-time; actual payout uses proportional distribution.
    potential_win = int(amount * option.odds_multiplier)

    bet = UserBet(
        user_tg_id=user_tg_id,
        event_id=event_id,
        option_id=option_id,
        amount=amount,
        potential_win=potential_win,
        status=BetStatus.pending.value,
    )
    session.add(bet)

    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AlreadyBetError(user_tg_id, event_id)

    return bet


async def lock_event(session: AsyncSession, event_id: int) -> BettingEvent:
    result = await session.execute(
        select(BettingEvent).where(BettingEvent.id == event_id).with_for_update()
    )
    event = result.scalar_one_or_none()
    if event is None:
        raise EventNotFoundError(event_id)
    if event.status in (EventStatus.resolved.value, EventStatus.cancelled.value):
        raise EventAlreadySettledError(event_id, event.status)
    if event.status == EventStatus.locked.value:
        return event  # idempotent

    event.status = EventStatus.locked.value
    event.locked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return event


async def resolve_event(
    session: AsyncSession, event_id: int, winning_option_id: int
) -> dict:
    """Proportional payout (Twitch-style): the entire pot is split among winners
    proportionally to their individual bet amount.

    Returns a dict with:
      winners          — number of winning bets
      total_pot        — total coins wagered on all sides
      total_distributed — coins actually paid out
      winning_option   — label of the winning option
      winners_data     — list of {tg_id, bet_amount, payout} for notifications
    """
    result = await session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event_id)
        .options(
            selectinload(BettingEvent.options),
            selectinload(BettingEvent.user_bets),
        )
        .with_for_update()
    )
    event = result.scalar_one_or_none()
    if event is None:
        raise EventNotFoundError(event_id)
    if event.status in (EventStatus.resolved.value, EventStatus.cancelled.value):
        raise EventAlreadySettledError(event_id, event.status)

    winning_option = next((o for o in event.options if o.id == winning_option_id), None)
    if winning_option is None:
        raise EventNotFoundError(event_id)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    event.status = EventStatus.resolved.value
    event.resolution_option_id = winning_option_id
    event.resolved_at = now

    pending_bets = [b for b in event.user_bets if b.status == BetStatus.pending.value]
    total_pot = sum(b.amount for b in pending_bets)

    winning_bets = [b for b in pending_bets if b.option_id == winning_option_id]
    total_winning_pool = sum(b.amount for b in winning_bets)

    # Compute floor payouts; give rounding leftover to the biggest winner.
    payout_map: dict[int, int] = {}
    total_distributed = 0

    if total_winning_pool > 0 and winning_bets:
        for bet in winning_bets:
            payout = int((bet.amount / total_winning_pool) * total_pot)
            payout_map[bet.id] = payout
            total_distributed += payout

        leftover = total_pot - total_distributed
        if leftover > 0:
            biggest = max(winning_bets, key=lambda b: b.amount)
            payout_map[biggest.id] += leftover
            total_distributed += leftover

    winners_data: list[dict] = []

    for bet in pending_bets:
        bet.settled_at = now
        if bet.option_id == winning_option_id:
            bet.status = BetStatus.won.value
            # Lock the user row and grant XP (User lock) BEFORE crediting the
            # wallet (Wallet lock) to keep the canonical User → Wallet lock order.
            user_result = await session.execute(
                select(User).where(User.tg_id == bet.user_tg_id).with_for_update()
            )
            winner_user = user_result.scalar_one_or_none()
            if winner_user is not None:
                winner_user.bets_won += 1
            # Capped participation XP for a winning bet (anti-farm via daily cap).
            await xp_service.grant_xp(
                session, bet.user_tg_id, settings.xp_per_bet_won,
                XpSource.bet_won, capped=True,
            )
            payout = payout_map.get(bet.id, 0)
            if payout > 0:
                await economy_service.credit(
                    session,
                    bet.user_tg_id,
                    payout,
                    TransactionType.bet_won,
                    f"Vittoria scommessa #{event_id}: {event.title}",
                    reference_id=event_id,
                )
            winners_data.append({
                "tg_id": bet.user_tg_id,
                "bet_amount": bet.amount,
                "payout": payout,
            })
        else:
            bet.status = BetStatus.lost.value

    return {
        "winners": len(winners_data),
        "total_pot": total_pot,
        "total_distributed": total_distributed,
        "winning_option": winning_option.label,
        "winners_data": winners_data,
    }


async def cancel_event(session: AsyncSession, event_id: int) -> dict:
    """Cancel an event and refund all pending bets.
    Returns {"refunded": count, "title": str, "refunds_data": [{tg_id, amount}]}
    """
    result = await session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event_id)
        .options(selectinload(BettingEvent.user_bets))
        .with_for_update()
    )
    event = result.scalar_one_or_none()
    if event is None:
        raise EventNotFoundError(event_id)
    if event.status in (EventStatus.resolved.value, EventStatus.cancelled.value):
        raise EventAlreadySettledError(event_id, event.status)

    event.status = EventStatus.cancelled.value
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    refunds_data: list[dict] = []

    for bet in event.user_bets:
        if bet.status == BetStatus.pending.value:
            bet.status = BetStatus.refunded.value
            bet.settled_at = now
            await economy_service.credit(
                session,
                bet.user_tg_id,
                bet.amount,
                TransactionType.bet_refund,
                f"Rimborso scommessa #{event_id}: {event.title} (annullata)",
                reference_id=event_id,
            )
            refunds_data.append({"tg_id": bet.user_tg_id, "amount": bet.amount})

    return {
        "refunded": len(refunds_data),
        "title": event.title,
        "refunds_data": refunds_data,
    }


async def get_open_events(session: AsyncSession) -> list[BettingEvent]:
    result = await session.execute(
        select(BettingEvent)
        .where(BettingEvent.status == EventStatus.open.value)
        .options(selectinload(BettingEvent.options))
        .order_by(BettingEvent.created_at.desc())
    )
    return list(result.scalars().all())


async def get_all_active_events(session: AsyncSession) -> list[BettingEvent]:
    """Returns open and locked events (not yet resolved or cancelled)."""
    result = await session.execute(
        select(BettingEvent)
        .where(
            BettingEvent.status.in_([EventStatus.open.value, EventStatus.locked.value])
        )
        .options(
            selectinload(BettingEvent.options),
            selectinload(BettingEvent.user_bets),
        )
        .order_by(BettingEvent.created_at.desc())
    )
    return list(result.scalars().all())


async def get_event_detail(session: AsyncSession, event_id: int) -> Optional[BettingEvent]:
    result = await session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event_id)
        .options(
            selectinload(BettingEvent.options),
            selectinload(BettingEvent.user_bets),
        )
    )
    return result.scalar_one_or_none()


def compute_payout_preview(event: BettingEvent, winning_option_id: int) -> dict:
    """Pure computation — no DB access. Used by admin confirm screen."""
    pending = [b for b in event.user_bets if b.status == "pending"]
    total_pot = sum(b.amount for b in pending)
    winning_bets = [b for b in pending if b.option_id == winning_option_id]
    losing_bets = [b for b in pending if b.option_id != winning_option_id]
    total_winning_pool = sum(b.amount for b in winning_bets)

    previews: list[dict] = []
    if total_winning_pool > 0:
        sorted_bets = sorted(winning_bets, key=lambda b: b.amount, reverse=True)
        for i, bet in enumerate(sorted_bets):
            if i == 0:
                remaining_others = sum(
                    int((b.amount / total_winning_pool) * total_pot)
                    for b in sorted_bets[1:]
                )
                payout = total_pot - remaining_others
            else:
                payout = int((bet.amount / total_winning_pool) * total_pot)
            previews.append({
                "tg_id": bet.user_tg_id,
                "bet_amount": bet.amount,
                "payout": payout,
            })

    return {
        "total_pot": total_pot,
        "winning_count": len(winning_bets),
        "losing_count": len(losing_bets),
        "winning_pool": total_winning_pool,
        "previews": previews,
    }
