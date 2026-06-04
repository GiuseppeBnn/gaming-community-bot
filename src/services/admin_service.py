"""
Admin service — DB-side operations for the admin toolset.

Covers advanced currency ops, user dossiers/stats, the warn/strike system and
the admin audit log. Follows STEERING §5: these functions never commit — the
calling handler owns the transaction (currency ops reuse economy_service which
also records ledger entries).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import (
    AdminAction,
    BettingEvent,
    LedgerEntry,
    TransactionType,
    User,
    UserBet,
    Wallet,
    Warning,
)
from exceptions.economy import WalletNotFoundError
from services import economy_service


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

async def set_balance(session: AsyncSession, tg_id: int, target: int) -> tuple[int, int]:
    """Set a wallet to an exact balance via a credit/debit delta.

    Returns (old_balance, new_balance). Records a ledger entry for the delta.
    Does NOT commit.
    """
    if target < 0:
        raise ValueError("Il saldo non può essere negativo.")

    result = await session.execute(select(Wallet).where(Wallet.tg_id == tg_id))
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise WalletNotFoundError(tg_id)

    old = wallet.coins
    delta = target - old
    if delta > 0:
        await economy_service.credit(
            session, tg_id, delta, TransactionType.admin_credit, "Saldo impostato da admin"
        )
    elif delta < 0:
        await economy_service.debit(
            session, tg_id, -delta, TransactionType.admin_debit, "Saldo impostato da admin"
        )
    return old, target


async def mass_credit(
    session: AsyncSession, amount: int, description: str
) -> int:
    """Credit `amount` coins to every wallet (airdrop). Returns user count.

    Bulk-updates wallets in one statement and records one ledger entry per
    user for audit accuracy. Does NOT commit.
    """
    if amount <= 0:
        raise ValueError("L'importo deve essere positivo.")

    tg_ids = list((await session.execute(select(Wallet.tg_id))).scalars().all())
    if not tg_ids:
        return 0

    await session.execute(update(Wallet).values(coins=Wallet.coins + amount))
    session.add_all(
        LedgerEntry(
            to_tg_id=tg_id,
            amount=amount,
            tx_type=TransactionType.admin_credit.value,
            description=description[:512],
        )
        for tg_id in tg_ids
    )
    return len(tg_ids)


# ---------------------------------------------------------------------------
# Dossier / stats
# ---------------------------------------------------------------------------

@dataclass
class Dossier:
    user: User
    coins: int
    badge_count: int
    bet_count: int
    active_warnings: int


async def get_dossier(session: AsyncSession, tg_id: int) -> Dossier | None:
    result = await session.execute(
        select(User)
        .where(User.tg_id == tg_id)
        .options(selectinload(User.wallet), selectinload(User.badges))
    )
    user = result.scalar_one_or_none()
    if user is None:
        return None

    bet_count = (
        await session.execute(
            select(func.count()).select_from(UserBet).where(UserBet.user_tg_id == tg_id)
        )
    ).scalar_one()
    warns = await active_warning_count(session, tg_id)

    return Dossier(
        user=user,
        coins=user.wallet.coins if user.wallet else 0,
        badge_count=len(user.badges),
        bet_count=bet_count,
        active_warnings=warns,
    )


async def search_users(session: AsyncSession, query: str, limit: int = 15) -> list[User]:
    pattern = f"%{query}%"
    result = await session.execute(
        select(User)
        .where(User.username.ilike(pattern) | User.full_name.ilike(pattern))
        .order_by(User.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_users(
    session: AsyncSession, offset: int = 0, limit: int = 8
) -> list[tuple[User, int]]:
    """Users (newest first) with their balance, for the dashboard user picker."""
    result = await session.execute(
        select(User, func.coalesce(Wallet.coins, 0))
        .join(Wallet, Wallet.tg_id == User.tg_id, isouter=True)
        .order_by(User.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return [(row[0], row[1]) for row in result.all()]


async def count_users(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(User))).scalar_one()


async def leaderboard(session: AsyncSession, limit: int = 10) -> list[tuple[User, int]]:
    result = await session.execute(
        select(User, Wallet.coins)
        .join(Wallet, Wallet.tg_id == User.tg_id)
        .order_by(Wallet.coins.desc())
        .limit(limit)
    )
    return [(row[0], row[1]) for row in result.all()]


@dataclass
class EconomyStats:
    total_users: int
    total_coins: int
    avg_coins: int
    active_events: int
    active_warnings: int


async def economy_stats(session: AsyncSession) -> EconomyStats:
    total_users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    total_coins = (await session.execute(select(func.coalesce(func.sum(Wallet.coins), 0)))).scalar_one()
    active_events = (
        await session.execute(
            select(func.count()).select_from(BettingEvent).where(BettingEvent.status.in_(("open", "locked")))
        )
    ).scalar_one()
    active_warnings = (
        await session.execute(
            select(func.count()).select_from(Warning).where(Warning.active.is_(True))
        )
    ).scalar_one()
    avg = int(total_coins / total_users) if total_users else 0
    return EconomyStats(
        total_users=total_users,
        total_coins=int(total_coins),
        avg_coins=avg,
        active_events=active_events,
        active_warnings=active_warnings,
    )


# ---------------------------------------------------------------------------
# Warn / strike system
# ---------------------------------------------------------------------------

async def add_warning(
    session: AsyncSession,
    user_tg_id: int,
    group_id: int,
    issued_by_tg_id: int,
    reason: str | None,
) -> int:
    """Add an active warning. Returns the new count of active warnings. No commit."""
    session.add(
        Warning(
            user_tg_id=user_tg_id,
            group_id=group_id,
            issued_by_tg_id=issued_by_tg_id,
            reason=(reason or None) and reason[:256],
        )
    )
    await session.flush()
    return await active_warning_count(session, user_tg_id)


async def active_warnings(session: AsyncSession, user_tg_id: int) -> list[Warning]:
    result = await session.execute(
        select(Warning)
        .where(Warning.user_tg_id == user_tg_id, Warning.active.is_(True))
        .order_by(Warning.created_at.desc(), Warning.id.desc())
    )
    return list(result.scalars().all())


async def active_warning_count(session: AsyncSession, user_tg_id: int) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Warning)
            .where(Warning.user_tg_id == user_tg_id, Warning.active.is_(True))
        )
    ).scalar_one()


async def clear_warnings(
    session: AsyncSession, user_tg_id: int, count: int | None = None
) -> int:
    """Deactivate warnings (most recent first). count=None clears all.
    Returns how many were deactivated. No commit.
    """
    warns = await active_warnings(session, user_tg_id)
    to_clear = warns if count is None else warns[:count]
    for w in to_clear:
        w.active = False
    return len(to_clear)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

async def log_action(
    session: AsyncSession,
    admin_tg_id: int,
    action_type: str,
    target_tg_id: int | None = None,
    group_id: int | None = None,
    amount: int | None = None,
    detail: str | None = None,
) -> None:
    """Record an admin action for the audit trail. No commit."""
    session.add(
        AdminAction(
            admin_tg_id=admin_tg_id,
            action_type=action_type[:32],
            target_tg_id=target_tg_id,
            group_id=group_id,
            amount=amount,
            detail=(detail or None) and detail[:512],
        )
    )


async def recent_actions(
    session: AsyncSession, target_tg_id: int | None = None, limit: int = 15
) -> list[AdminAction]:
    stmt = select(AdminAction)
    if target_tg_id is not None:
        stmt = stmt.where(AdminAction.target_tg_id == target_tg_id)
    stmt = stmt.order_by(AdminAction.created_at.desc(), AdminAction.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())
