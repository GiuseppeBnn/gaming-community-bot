from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import LedgerEntry, TransactionType, User, Wallet
from exceptions.economy import (
    DailyAlreadyClaimedError,
    InsufficientFundsError,
    SelfTransferError,
    WalletNotFoundError,
)

_DAILY_COOLDOWN = timedelta(hours=20)
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
    Raises DailyAlreadyClaimedError if called within the cooldown window.
    Does NOT commit — caller is responsible for the commit.
    """
    # Lock the user row: the read-check-write on last_daily_claim must be atomic
    # or two concurrent /daily calls could both pass the cooldown check.
    result = await session.execute(
        select(User).where(User.tg_id == tg_id).with_for_update()
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise WalletNotFoundError(tg_id)

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)

    if user.last_daily_claim is not None:
        last = user.last_daily_claim
        elapsed = now - last
        if elapsed < _DAILY_COOLDOWN:
            remaining_seconds = int((_DAILY_COOLDOWN - elapsed).total_seconds())
            raise DailyAlreadyClaimedError(seconds_remaining=remaining_seconds)

    # Determine new streak before updating last_daily_claim
    if user.last_daily_claim is not None and (now - user.last_daily_claim) < timedelta(hours=48):
        new_streak = (user.daily_streak or 0) + 1
    else:
        new_streak = 1

    user.last_daily_claim = now
    user.daily_streak = new_streak

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
