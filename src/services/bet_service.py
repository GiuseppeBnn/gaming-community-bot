from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    ScheduledTask,
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
from services import economy_service, schedule_service, xp_service
from services.xp_service import XpSource


async def create_event(
    session: AsyncSession,
    creator_tg_id: int,
    title: str,
    description: str,
    options: list[dict],
    status: str = EventStatus.open.value,
    window_seconds: int | None = None,
) -> BettingEvent:
    """Create a betting event. Defaults to ``open`` (the community flow); pass
    ``status=EventStatus.draft.value`` to pre-create one for the Events hub, to be
    activated/scheduled later.

    ``window_seconds`` is the betting window chosen at creation (``None``/0 =
    illimitata, only manual lock). If the event is created already ``open``, the
    close deadline is armed here; a draft arms it later, at ``activate_event``."""
    event = BettingEvent(
        title=title,
        description=description,
        creator_tg_id=creator_tg_id,
        status=status,
        betting_window_seconds=window_seconds,
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
    if status == EventStatus.open.value:
        arm_close(event)
    return event


def arm_close(event: BettingEvent) -> None:
    """Arm the auto-close deadline for a freshly-opened event:
    ``closes_at = now + betting_window_seconds``. A NULL/0 window means illimitata
    (no auto-close), so ``closes_at`` is left unset. Pure helper (no DB access)."""
    window = event.betting_window_seconds
    if window and window > 0:
        event.closes_at = schedule_service.utcnow() + timedelta(seconds=window)


async def schedule_close(
    session: AsyncSession,
    event: BettingEvent,
    created_by_tg_id: int,
    group_id: int | None,
) -> ScheduledTask | None:
    """If the event has an armed deadline, schedule its auto-lock. Reuses
    ``task_type="bet"`` with payload ``{"action": "lock"}`` so the scheduler
    dispatches it through the same ``BetType`` spec — no new task type, no per-type
    branching in the loop (STEERING §18.2). Returns the task, or ``None`` if the
    window is illimitata. No commit (§5)."""
    if event.closes_at is None:
        return None
    return await schedule_service.schedule_task(
        session,
        task_type="bet",
        run_at=event.closes_at,
        created_by_tg_id=created_by_tg_id,
        group_id=group_id,
        ref_id=event.id,
        payload={"action": "lock"},
    )


async def cancel_pending_close(session: AsyncSession, event_id: int) -> None:
    """Cancel any pending auto-lock task for this event (the admin locked/resolved/
    cancelled it by hand before the window elapsed), so the scheduler doesn't later
    fire a no-op and DM the admin about a skipped task. No commit (§5)."""
    result = await session.execute(
        select(ScheduledTask).where(
            ScheduledTask.task_type == "bet",
            ScheduledTask.ref_id == event_id,
            ScheduledTask.status == "pending",
        )
    )
    for task in result.scalars().all():
        if schedule_service.task_payload(task).get("action") == "lock":
            task.status = "cancelled"


async def activate_event(session: AsyncSession, event_id: int) -> BettingEvent:
    """Transition a ``draft`` event to ``open`` so members can bet. Idempotent for
    an already-open event; raises if it was locked/resolved/cancelled. Arms the
    auto-close deadline on the draft→open transition."""
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
    arm_close(event)
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
    # Betting window: reject once the deadline has passed even if the scheduler's
    # auto-lock tick (~poll interval) hasn't flipped the status to `locked` yet, so
    # the window is honoured to the second on the user's side.
    if event.closes_at is not None and schedule_service.utcnow() >= event.closes_at:
        raise BettingClosedError(event_id, "closed")

    option = next((o for o in event.options if o.id == option_id), None)
    if option is None:
        raise EventNotFoundError(event_id)

    # The event lock above comes first; then every account effect follows the
    # shared User → Wallet order. Keep debit's validation/error behavior intact
    # for non-positive amounts by letting it reject before any account lock.
    if amount > 0:
        await economy_service.lock_users_then_wallets(session, (user_tg_id,))
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
        # `from None`: the IntegrityError is the *expected* signal of the unique
        # constraint firing, not an error while handling one — don't chain it.
        raise AlreadyBetError(user_tg_id, event_id) from None

    # Event XP for participating (placing a bet), uncapped — once per event, since a
    # second bet on the same event raises AlreadyBetError above. The (larger) win
    # bonus is paid later, only to winners, in resolve_event. The account rows
    # were already locked User → Wallet before the debit above.
    if settings.xp_per_bet_placed > 0:
        await xp_service.grant_xp(
            session, user_tg_id, settings.xp_per_bet_placed,
            XpSource.bet_placed, capped=False,
        )

    return bet


async def lock_event(session: AsyncSession, event_id: int) -> BettingEvent:
    """Close betting on an event. Idempotent: locking an already-locked event is a
    no-op, settling one is an error.

    The transition is a conditional UPDATE rather than a read-check-write, because
    the caller usually *is* holding the event: the scheduler's auto-lock binds it
    via `get_event_detail` and then calls this (`handlers/event_types/bet_type`).
    A locking read would hand back that same held instance with its old status and
    both overlapping locks would proceed.
    """
    changed = (
        await session.execute(
            update(BettingEvent)
            .where(
                BettingEvent.id == event_id,
                BettingEvent.status == EventStatus.open.value,
            )
            .values(
                status=EventStatus.locked.value,
                locked_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            .execution_options(synchronize_session=False)
        )
    ).rowcount or 0

    event = (
        await session.execute(select(BettingEvent).where(BettingEvent.id == event_id))
    ).scalar_one_or_none()
    if event is None:
        raise EventNotFoundError(event_id)
    # The instance may predate the UPDATE — the caller's, or ours from a moment ago.
    await session.refresh(event, ["status", "locked_at"])

    if not changed and event.status != EventStatus.locked.value:
        raise EventAlreadySettledError(event_id, event.status)
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
    event = (
        await session.execute(
            select(BettingEvent).where(BettingEvent.id == event_id).with_for_update()
        )
    ).scalar_one_or_none()
    if event is None:
        raise EventNotFoundError(event_id)
    await session.refresh(event, ["status"])

    settled = (EventStatus.resolved.value, EventStatus.cancelled.value)
    if event.status in settled:
        raise EventAlreadySettledError(event_id, event.status)

    winning_option = (
        await session.execute(
            select(BettingOption).where(
                BettingOption.id == winning_option_id,
                BettingOption.event_id == event_id,
            )
        )
    ).scalar_one_or_none()
    if winning_option is None:
        raise EventNotFoundError(event_id)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # The status check above is friendly-error handling; *this* is the guard. A
    # concurrent resolution that got there first leaves this matching no row, so
    # the pot cannot be paid out twice.
    resolved = (
        await session.execute(
            update(BettingEvent)
            .where(BettingEvent.id == event_id, BettingEvent.status.not_in(settled))
            .values(
                status=EventStatus.resolved.value,
                resolution_option_id=winning_option_id,
                resolved_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    ).rowcount or 0
    if not resolved:
        await session.refresh(event, ["status"])
        raise EventAlreadySettledError(event_id, event.status)
    await session.refresh(event, ["status", "resolution_option_id", "resolved_at"])

    # Queried, not read off `event.user_bets`: a caller holding the event holds its
    # loaded collection too, and a bet placed after that snapshot would be debited
    # and then never settled.
    pending_bets = list(
        (
            await session.execute(
                select(UserBet).where(
                    UserBet.event_id == event_id,
                    UserBet.status == BetStatus.pending.value,
                )
            )
        )
        .scalars()
        .all()
    )
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
            # Event XP bonus for a winning bet (uncapped — participation XP was already
            # paid at placement). The User row is already locked above for bets_won.
            await xp_service.grant_xp(
                session, bet.user_tg_id, settings.xp_per_bet_won,
                XpSource.bet_won, capped=False,
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


async def get_user_placed_event_ids(
    session: AsyncSession, tg_id: int, event_ids: list[int]
) -> set[int]:
    """Return the subset of event_ids on which tg_id has a pending bet."""
    if not event_ids:
        return set()
    result = await session.execute(
        select(UserBet.event_id).where(
            UserBet.user_tg_id == tg_id,
            UserBet.event_id.in_(event_ids),
            UserBet.status == BetStatus.pending.value,
        )
    )
    return set(result.scalars().all())


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
