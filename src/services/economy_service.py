from __future__ import annotations

import math
from datetime import timedelta

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import LedgerEntry, TransactionType, User, Wallet
from exceptions.economy import (
    DailyAlreadyClaimedError,
    InsufficientFundsError,
    SelfTransferError,
    WalletNotFoundError,
)
from utils import daytime

_MIN_TRANSFER = 1
_MAX_TRANSFER = 1_000_000


async def _get_wallet(
    session: AsyncSession, tg_id: int, *, for_update: bool = False
) -> Wallet:
    stmt = select(Wallet).where(Wallet.tg_id == tg_id)
    if for_update:
        # Row-level lock so concurrent debits/credits serialize on Postgres and
        # can't double-spend. No-op on SQLite (dev/tests), real on Postgres (prod).
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise WalletNotFoundError(tg_id)
    return wallet


async def credit(
    session: AsyncSession,
    tg_id: int,
    amount: int,
    tx_type: TransactionType,
    description: str,
    reference_id: int | None = None,
) -> None:
    """Add `amount` coins to `tg_id`'s wallet and record a ledger entry.
    Does NOT commit — caller is responsible for the commit.
    """
    if amount <= 0:
        raise ValueError("L'importo da accreditare deve essere positivo.")
    wallet = await _get_wallet(session, tg_id, for_update=True)
    wallet.coins += amount
    session.add(
        LedgerEntry(
            to_tg_id=tg_id,
            amount=amount,
            tx_type=tx_type.value,
            description=description[:512],
            reference_id=reference_id,
        )
    )


async def debit(
    session: AsyncSession,
    tg_id: int,
    amount: int,
    tx_type: TransactionType,
    description: str,
    reference_id: int | None = None,
) -> None:
    """Subtract `amount` coins from `tg_id`'s wallet and record a ledger entry.
    Raises InsufficientFundsError if balance is too low.
    Does NOT commit — caller is responsible for the commit.
    """
    if amount <= 0:
        raise ValueError("L'importo da addebitare deve essere positivo.")
    wallet = await _get_wallet(session, tg_id, for_update=True)
    if wallet.coins < amount:
        raise InsufficientFundsError(balance=wallet.coins, required=amount)
    wallet.coins -= amount
    session.add(
        LedgerEntry(
            from_tg_id=tg_id,
            amount=-amount,
            tx_type=tx_type.value,
            description=description[:512],
            reference_id=reference_id,
        )
    )


async def get_balance(session: AsyncSession, tg_id: int) -> int:
    wallet = await _get_wallet(session, tg_id)
    return wallet.coins


async def transfer(
    session: AsyncSession,
    from_tg_id: int,
    to_tg_id: int,
    amount: int,
    from_name: str | None = None,
    to_name: str | None = None,
) -> None:
    """Transfer `amount` coins between two users.
    Raises SelfTransferError, InsufficientFundsError, or WalletNotFoundError.
    Does NOT commit — caller is responsible for the commit.

    `from_name`/`to_name` are display labels stored in the two ledger entries so
    the counterparty shows up by name in /storico (a transfer's counterparty is
    not in a column — `transfer_out` only has `from_tg_id`, `transfer_in` only
    `to_tg_id`). Falls back to the numeric id when a name is not supplied.
    """
    if from_tg_id == to_tg_id:
        raise SelfTransferError()
    if not (_MIN_TRANSFER <= amount <= _MAX_TRANSFER):
        raise ValueError(
            f"Importo non valido (min {_MIN_TRANSFER}, max {_MAX_TRANSFER:,})."
        )

    # Pre-lock both wallets in a deterministic order (ascending tg_id) so two
    # opposite transfers on the same pair can't deadlock. The debit/credit below
    # re-select FOR UPDATE, but the locks are already held by this transaction.
    first, second = sorted((from_tg_id, to_tg_id))
    w_first = await _get_wallet(session, first, for_update=True)
    w_second = await _get_wallet(session, second, for_update=True)
    from_wallet = w_first if first == from_tg_id else w_second

    if from_wallet.coins < amount:
        raise InsufficientFundsError(balance=from_wallet.coins, required=amount)

    await debit(
        session, from_tg_id, amount,
        TransactionType.transfer_out,
        f"Trasferimento a {to_name or to_tg_id}",
    )
    await credit(
        session, to_tg_id, amount,
        TransactionType.transfer_in,
        f"Trasferimento da {from_name or from_tg_id}",
    )

    # Increment transfers_made counter for badge tracking (lock the row so the
    # counter can't be lost under concurrent transfers from the same sender).
    from_user_result = await session.execute(
        select(User).where(User.tg_id == from_tg_id).with_for_update()
    )
    from_user = from_user_result.scalar_one_or_none()
    if from_user is not None:
        from_user.transfers_made += 1


async def claim_daily(
    session: AsyncSession,
    tg_id: int,
) -> tuple[int, int]:
    """Award the daily reward. Returns (reward_amount, new_streak).
    Raises DailyAlreadyClaimedError if the claim window is not open yet.
    Does NOT commit — caller is responsible for the commit.

    Window: the reward resets at **local midnight** (one claim per calendar day)
    AND at least `settings.daily_min_hours` must have elapsed since the previous
    claim — the second rule exists only to stop a 23:59 claim from being followed
    by another at 00:01.

    Both rules are expressed as a single `next_allowed` threshold rather than two
    booleans: an OR there would let people claim every N hours, so the AND is made
    structural (a max() of the two instants) and cannot be written wrong.

    ## Why one UPDATE instead of read-check-write

    The check and the write are a single conditional UPDATE, so two concurrent
    /daily calls cannot both pass: the second one matches zero rows. Locking the
    row and deciding in Python is *not* enough here — see `_locked_balance` for
    why the lock alone does not protect a value read through the ORM.

    The two window rules are rephrased as fixed instants so SQL can evaluate them
    against the stored column, which is the whole reason this fits in one
    statement (a threshold derived from `last` itself could not):

        now >= next_local_midnight(last)  ⟺  last < <today's local midnight>
        now >= last + daily_min_hours     ⟺  last <= now - daily_min_hours
    """
    # Loaded up front for two reasons: it is the existence check, and it is the
    # instance the caller may still be holding — the refresh below is what stops
    # it from lying after the UPDATE (§5).
    user = (
        await session.execute(select(User).where(User.tg_id == tg_id))
    ).scalar_one_or_none()
    if user is None:
        raise WalletNotFoundError(tg_id)

    now = daytime.utc_now()
    today = daytime.local_day(now)
    today_opened = daytime.local_midnight(today)
    yesterday_opened = daytime.local_midnight(today - timedelta(days=1))
    min_gap_cutoff = now - timedelta(hours=settings.daily_min_hours)

    result = await session.execute(
        update(User)
        .where(
            User.tg_id == tg_id,
            or_(
                User.last_daily_claim.is_(None),  # never claimed
                and_(
                    User.last_daily_claim < today_opened,      # new calendar day
                    User.last_daily_claim <= min_gap_cutoff,   # min gap
                ),
            ),
        )
        .values(
            last_daily_claim=now,
            # SET expressions see the row's *previous* values, so the streak is
            # continued or reset from the stored claim without reading it first.
            # The WHERE above already excludes anything from today, so "not older
            # than yesterday's opening" means exactly "claimed yesterday".
            daily_streak=case(
                (User.last_daily_claim >= yesterday_opened, User.daily_streak + 1),
                else_=1,
            ),
        )
        .execution_options(synchronize_session=False)
    )

    # Whatever the outcome, the in-session copy must stop lying: it is what the
    # error path reads to size the countdown, what this function returns as the
    # streak, and what the caller sees if it kept a reference.
    await session.refresh(user, ["last_daily_claim", "daily_streak"])

    if not (result.rowcount or 0):
        last = user.last_daily_claim
        next_allowed = (
            max(
                daytime.next_local_midnight(last),
                last + timedelta(hours=settings.daily_min_hours),
            )
            if last is not None
            # Unreachable: a NULL last_daily_claim always matches the WHERE above,
            # so a non-match means the row exists and was claimed too recently.
            else now
        )
        raise DailyAlreadyClaimedError(
            seconds_remaining=max(0, math.ceil((next_allowed - now).total_seconds()))
        )

    new_streak = user.daily_streak
    reward = settings.daily_reward_coins
    await credit(
        session, tg_id, reward,
        TransactionType.daily_reward,
        "Premio giornaliero",
    )

    return reward, new_streak


async def get_history(
    session: AsyncSession,
    tg_id: int,
    limit: int = 20,
) -> list[LedgerEntry]:
    result = await session.execute(
        select(LedgerEntry)
        .where(
            (LedgerEntry.from_tg_id == tg_id) | (LedgerEntry.to_tg_id == tg_id)
        )
        .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
        .limit(min(limit, 50))
    )
    return list(result.scalars().all())
