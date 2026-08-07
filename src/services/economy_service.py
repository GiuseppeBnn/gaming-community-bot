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


async def _get_wallet(session: AsyncSession, tg_id: int) -> Wallet:
    """The wallet **instance**, for its existence check and to refresh afterwards.

    Deliberately not used to *read* the balance: an entity select can be served
    from the session's identity map, so `wallet.coins` is only trustworthy right
    after a `session.refresh`. To read a balance use `get_balance`, or
    `lock_balance` when the value has to stay valid for a following write.
    """
    result = await session.execute(select(Wallet).where(Wallet.tg_id == tg_id))
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise WalletNotFoundError(tg_id)
    return wallet


async def _balance(session: AsyncSession, tg_id: int, *, lock: bool) -> int:
    """The wallet's **committed** balance, straight from the database.

    Selects the column, not the `Wallet` entity, and that is the whole point: an
    entity select is served from the identity map, so a caller still holding the
    row would get its stale copy — and a `FOR UPDATE` on top of that would take a
    real lock while protecting a number that is already wrong. A column select
    can never be answered from the cache.

    With `lock=True` the row is locked until the caller's transaction ends, so the
    value stays valid for a check-then-write. No-op on SQLite (dev/tests), real on
    Postgres (prod).
    """
    stmt = select(Wallet.coins).where(Wallet.tg_id == tg_id)
    if lock:
        stmt = stmt.with_for_update()
    balance = (await session.execute(stmt)).scalar_one_or_none()
    if balance is None:
        raise WalletNotFoundError(tg_id)
    return balance


async def lock_balance(session: AsyncSession, tg_id: int) -> int:
    """Lock a wallet row and return its committed balance.

    For the one operation that cannot be expressed as a single relative UPDATE:
    setting an *absolute* balance needs the current value, and the lock is what
    keeps that value valid until the write lands (see `admin_service.set_balance`).
    Everything else should use SQL-side arithmetic instead of locking.
    """
    return await _balance(session, tg_id, lock=True)


async def _add_coins(session: AsyncSession, tg_id: int, delta: int) -> int:
    """Apply `delta` with SQL-side arithmetic. Returns the number of rows changed.

    `coins = coins + :delta` is computed by the database from the row's current
    value, so concurrent movements add up instead of overwriting each other. A
    negative `delta` also refuses to overdraw, in the same statement — which is
    what makes the balance check atomic rather than a read-then-write.

    `synchronize_session=False` is not an optimisation: the default tries to keep
    in-session objects in step and would write back a value derived from their
    *stale* copy (measured: cache=100, DB=500, `coins - 10` leaves DB=490 and
    cache=90). The caller refreshes instead.
    """
    stmt = update(Wallet).where(Wallet.tg_id == tg_id)
    if delta < 0:
        stmt = stmt.where(Wallet.coins >= -delta)
    result = await session.execute(
        stmt.values(coins=Wallet.coins + delta).execution_options(
            synchronize_session=False
        )
    )
    return result.rowcount or 0


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
    wallet = await _get_wallet(session, tg_id)
    await _add_coins(session, tg_id, amount)
    # The instance must reflect the write: callers (and tests) read the new
    # balance off it, and leaving it stale would just move the bug one level up.
    await session.refresh(wallet, ["coins"])
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
    wallet = await _get_wallet(session, tg_id)
    changed = await _add_coins(session, tg_id, -amount)
    await session.refresh(wallet, ["coins"])
    if not changed:
        # The wallet exists (checked above), so the only way to match no row is
        # the `coins >= amount` guard. The refresh above means the balance quoted
        # in the error is the real one, not the cached one.
        raise InsufficientFundsError(balance=wallet.coins, required=amount)
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
    return await _balance(session, tg_id, lock=False)


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

    # Lock both wallets in a deterministic order (ascending tg_id) so two opposite
    # transfers on the same pair can't deadlock — a transfer touches two rows, and
    # that is the one thing SQL-side arithmetic alone cannot make safe. Also the
    # existence check for both sides, before anything moves.
    first, second = sorted((from_tg_id, to_tg_id))
    await lock_balance(session, first)
    await lock_balance(session, second)

    # No balance pre-check here: `debit` refuses an overdraw in the same statement
    # that performs it, and it runs first, so an insufficient balance still raises
    # before anything moves — with the real number rather than a cached one.
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

    # Increment transfers_made for badge tracking. SQL-side, so concurrent
    # transfers from the same sender each add one instead of overwriting.
    from_user = (
        await session.execute(select(User).where(User.tg_id == from_tg_id))
    ).scalar_one_or_none()
    if from_user is not None:
        await session.execute(
            update(User)
            .where(User.tg_id == from_tg_id)
            .values(transfers_made=User.transfers_made + 1)
            .execution_options(synchronize_session=False)
        )
        await session.refresh(from_user, ["transfers_made"])


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
