"""Diagnostic instrument for the identity-map staleness hazard on the money path.

**These tests do not test a fix — there is none yet.** They measure, per site,
whether the hazard is real, so the fix can target what is actually broken.

## The hazard, precisely

`src/database/connection.py` builds sessions with `expire_on_commit=False`. When a
row is already loaded in a session's identity map and a service then runs
`select(...).with_for_update()`, Postgres **does** take the row lock, but SQLAlchemy
returns the cached instance with its stale column values. A check-then-write can
then pass twice.

## What this file discovered, and it narrows the blast radius a lot

The identity map holds **weak** references. `DbSessionMiddleware._upsert_user()`
loads `User` and `Wallet` but keeps neither, so both are garbage collected and the
identity map ends up empty. **The middleware does not poison the session** — the
opposite of what the roadmap assumed. Measured:

    only _upsert_user (nothing holds the object) → FOR UPDATE reads xp=999 (fresh)
    a caller holds the loaded User in a variable → FOR UPDATE reads xp=0   (STALE)

So the hazard needs a caller that **holds the entity** across a service call that
locks the same row. Reading a value off it and discarding the object (as
`shop._balance` does) is safe. That makes this a per-flow question, not a global one:

  * `handlers/economy.cmd_daily` → calls `claim_daily` with nothing preloaded: safe.
  * `handlers/admin_betting.cb_admin_confirm_resolve` → calls `resolve_event`
    directly: safe.
  * `handlers/event_types/bet_type._auto_lock` → binds
    `event = await get_event_detail(...)`, checks `event.status`, then calls
    `lock_event(...)` while still holding `event`: **the reachable one.**

`TestMiddlewareDoesNotPoisonTheSession` below pins the weak-reference property, so
if anyone ever makes the middleware retain the user (e.g. stashing it in `data`),
the hazard goes global and that test says so.

## Why a real Postgres

`SELECT ... FOR UPDATE` is a no-op on SQLite, and the in-memory engine uses
StaticPool, which hands every session the same DBAPI connection — hence the same
transaction. Two sessions racing is not expressible there at all.

## Interleaving shapes, both deadlock-proof by construction

**Shape S (sequenced)** — for stale decisions: A loads and holds, B does the whole
operation and commits, then A operates. Nothing blocks (A's plain SELECT takes no
row lock under READ COMMITTED, and B is already committed). No sleeps, no timing.

**Shape G (gather)** — for lost updates: N tasks, each with its own session,
serialising on the row lock. The fixture sets `lock_timeout=5s`, so a self-deadlock
fails fast instead of hanging CI.
"""

from __future__ import annotations

import asyncio
import gc
import types
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from database.models import (
    BetStatus,
    BettingEvent,
    EventStatus,
    LedgerEntry,
    Quiz,
    TransactionType,
    User,
    UserBet,
    Wallet,
)
from exceptions.economy import (
    DailyAlreadyClaimedError,
    EventAlreadySettledError,
    InsufficientFundsError,
)
from middlewares.db_middleware import _upsert_user
from services import bet_service as bet_svc
from services import economy_service as eco
from services import quiz_service as quiz_svc
from services import xp_service
from services.xp_service import XpSource
from utils import daytime

pytestmark = [pytest.mark.pg]


# ---------------------------------------------------------------------------
# Reading DB truth
# ---------------------------------------------------------------------------

async def truth(sessions, stmt):
    """Run `stmt` in a fresh session and return the scalar.

    Must not reuse a racing session: `expire_on_commit=False` means a participant
    hands back its own cached copy, and an *entity* select is served from the
    identity map. Fresh session + column select is the only way to read what is
    actually committed.
    """
    async with sessions() as s:
        return (await s.execute(stmt)).scalar_one_or_none()


def coins_of(tg_id: int):
    return select(Wallet.coins).where(Wallet.tg_id == tg_id)


def user_col(tg_id: int, column):
    return select(column).where(User.tg_id == tg_id)


async def assert_ledger_balanced(sessions, tg_id: int, initial: int) -> None:
    """The wallet must equal the starting balance plus every ledger movement.

    Catches any lost update in one assertion: if a concurrent write vanished, the
    wallet and the ledger disagree — coins were created or destroyed.
    """
    balance = await truth(sessions, coins_of(tg_id))
    moved = await truth(
        sessions,
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            (LedgerEntry.from_tg_id == tg_id) | (LedgerEntry.to_tg_id == tg_id)
        ),
    )
    assert balance == initial + moved, (
        f"wallet={balance} but initial={initial} + ledger={moved} = {initial + moved} "
        "— coins were created or destroyed"
    )


# ---------------------------------------------------------------------------
# Creating the hazard the way a handler does
# ---------------------------------------------------------------------------

def _tg_user(tg_id: int, username: str = "testuser"):
    """The minimal aiogram User shape `_upsert_user` touches."""
    return types.SimpleNamespace(
        id=tg_id,
        is_bot=False,
        username=username,
        full_name=f"Test {username}",
        first_name=f"Test {username}",
    )


async def load_and_hold(session, tg_id: int) -> tuple[User, Wallet]:
    """Load User + Wallet and RETURN them, so the caller keeps them alive.

    Returning is the whole point: the identity map is weak-referencing, so a helper
    that loaded and discarded them would leave the session clean and the test would
    measure nothing. The caller must bind the result for the duration of the race.
    """
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one()
    wallet = (
        await session.execute(select(Wallet).where(Wallet.tg_id == tg_id))
    ).scalar_one()
    return user, wallet


# ===========================================================================
# The property that keeps the blast radius small
# ===========================================================================

class TestMiddlewareDoesNotPoisonTheSession:
    async def test_upsert_user_leaves_no_entities_in_the_identity_map(
        self, pg_sessions, pg_user_factory
    ):
        """`_upsert_user` must not retain the User/Wallet it loads.

        This is what keeps the staleness hazard confined to a few handler flows
        instead of applying to all ~190 handlers. If someone later caches the user
        (e.g. `data["db_user"] = user`), every locking read in every service becomes
        vulnerable — and this test is the tripwire.
        """
        await pg_user_factory(tg_id=1, coins=100)

        async with pg_sessions() as s:
            await _upsert_user(s, _tg_user(1))
            gc.collect()
            live = [st.class_.__name__ for st in s.identity_map.all_states()]

        assert live == [], (
            f"the middleware now retains {live} — the identity-map staleness hazard "
            "just became global; see this module's docstring"
        )

    async def test_a_held_entity_does_go_stale(self, pg_sessions, pg_user_factory):
        """The other half of the finding: when a caller *does* hold the row, the
        lock does not protect it. This is the mechanism every xfail below relies on,
        so it is asserted directly rather than inferred."""
        await pg_user_factory(tg_id=1, coins=100)

        async with pg_sessions() as sa:
            held_user, _held_wallet = await load_and_hold(sa, 1)

            async with pg_sessions() as sb:
                other = (
                    await sb.execute(select(User).where(User.tg_id == 1))
                ).scalar_one()
                other.xp = 999
                await sb.commit()

            relocked = (
                await sa.execute(
                    select(User).where(User.tg_id == 1).with_for_update()
                )
            ).scalar_one()
            assert relocked is held_user
            assert relocked.xp == 0, "unexpectedly fresh — re-read this module's docstring"


# ===========================================================================
# /daily
# ===========================================================================

class TestDailyClaim:
    @pytest.mark.xfail(
        strict=True,
        reason="claim_daily's FOR UPDATE returns the held User with a stale "
               "last_daily_claim, so the window check passes twice",
    )
    async def test_double_claim_is_refused(self, pg_sessions, pg_user_factory):
        await pg_user_factory(tg_id=1, coins=0)

        async with pg_sessions() as sa:
            _held = await load_and_hold(sa, 1)  # cache: last_daily_claim=None

            async with pg_sessions() as sb:
                await eco.claim_daily(sb, 1)
                await sb.commit()

            with pytest.raises(DailyAlreadyClaimedError):
                await eco.claim_daily(sa, 1)
                await sa.commit()

        await assert_ledger_balanced(pg_sessions, 1, initial=0)

    @pytest.mark.xfail(
        strict=True, reason="A recomputes the streak from its stale copy and writes it back"
    )
    async def test_streak_is_not_double_counted(self, pg_sessions, pg_user_factory):
        yesterday = daytime.utc_now() - timedelta(days=1)
        await pg_user_factory(tg_id=1, coins=0, last_daily_claim=yesterday, daily_streak=5)

        async with pg_sessions() as sa:
            _held = await load_and_hold(sa, 1)

            async with pg_sessions() as sb:
                await eco.claim_daily(sb, 1)
                await sb.commit()

            try:
                await eco.claim_daily(sa, 1)
                await sa.commit()
            except DailyAlreadyClaimedError:
                pass

        assert await truth(pg_sessions, user_col(1, User.daily_streak)) == 6
        await assert_ledger_balanced(pg_sessions, 1, initial=0)


# ===========================================================================
# debit
# ===========================================================================

class TestDebit:
    @pytest.mark.xfail(
        strict=True,
        reason="_get_wallet(for_update=True) returns the held Wallet, so the "
               "balance check runs against a cached number and coins are minted",
    )
    async def test_cannot_overdraw_from_a_stale_balance(self, pg_sessions, pg_user_factory):
        await pg_user_factory(tg_id=1, coins=100)

        async with pg_sessions() as sa:
            _held = await load_and_hold(sa, 1)  # cache: coins=100

            async with pg_sessions() as sb:
                await eco.debit(sb, 1, 100, TransactionType.shop_purchase, "B spends it all")
                await sb.commit()

            with pytest.raises(InsufficientFundsError):
                await eco.debit(sa, 1, 100, TransactionType.shop_purchase, "A spends it again")
                await sa.commit()

        assert await truth(pg_sessions, coins_of(1)) == 0
        await assert_ledger_balanced(pg_sessions, 1, initial=100)

    @pytest.mark.xfail(
        strict=True,
        reason="all 20 tasks load coins=100 before any of them commits, so each one "
               "computes 100-10 under its own lock: 20 debits land, 100 coins minted",
    )
    async def test_concurrent_debits_do_not_lose_updates(self, pg_sessions, pg_user_factory):
        """Shape G, each task holding its own loaded Wallet: 20 × debit(10) on a
        100-coin wallet, exactly 10 may succeed.

        Measured, and it corrected an assumption: loading inside each task does *not*
        make the cache safe. `gather` starts all 20 before any commits, so every task
        reads 100, and the row lock faithfully serialises 20 writes of `100 - 10`.
        The lock works; the arithmetic base is stale.
        """
        await pg_user_factory(tg_id=1, coins=100)
        ok = 0

        async def spend():
            nonlocal ok
            async with pg_sessions() as s:
                _held = await load_and_hold(s, 1)
                try:
                    await eco.debit(s, 1, 10, TransactionType.shop_purchase, "concurrent")
                    await s.commit()
                    ok += 1
                except InsufficientFundsError:
                    await s.rollback()

        await asyncio.gather(*(spend() for _ in range(20)))

        assert ok == 10, f"{ok} debits of 10 succeeded on a 100-coin wallet"
        assert await truth(pg_sessions, coins_of(1)) == 0
        await assert_ledger_balanced(pg_sessions, 1, initial=100)


# ===========================================================================
# transfer
# ===========================================================================

class TestTransfer:
    @pytest.mark.xfail(
        strict=True, reason="transfer's pre-check reads the held Wallet's stale coins"
    )
    async def test_cannot_overdraw_from_a_stale_balance(self, pg_sessions, pg_user_factory):
        await pg_user_factory(tg_id=1, coins=100, username="alice")
        await pg_user_factory(tg_id=2, coins=0, username="bob")
        await pg_user_factory(tg_id=3, coins=0, username="carol")

        async with pg_sessions() as sa:
            _held = await load_and_hold(sa, 1)

            async with pg_sessions() as sb:
                await eco.transfer(sb, 1, 3, 100, from_name="alice", to_name="carol")
                await sb.commit()

            with pytest.raises(InsufficientFundsError):
                await eco.transfer(sa, 1, 2, 100, from_name="alice", to_name="bob")
                await sa.commit()

        assert await truth(pg_sessions, coins_of(1)) == 0
        await assert_ledger_balanced(pg_sessions, 1, initial=100)

    async def test_concurrent_transfers_conserve_coins(self, pg_sessions, pg_user_factory):
        """Shape G: 10 × transfer(1→2, 10). Expected green — kept as a guard."""
        await pg_user_factory(tg_id=1, coins=1000, username="alice")
        await pg_user_factory(tg_id=2, coins=1000, username="bob")

        async def send():
            async with pg_sessions() as s:
                await eco.transfer(s, 1, 2, 10, from_name="alice", to_name="bob")
                await s.commit()

        await asyncio.gather(*(send() for _ in range(10)))

        assert await truth(pg_sessions, coins_of(1)) == 900
        assert await truth(pg_sessions, coins_of(2)) == 1100
        assert await truth(pg_sessions, user_col(1, User.transfers_made)) == 10

    async def test_opposite_transfers_do_not_deadlock(self, pg_sessions, pg_user_factory):
        """Regression guard, expected green.

        `transfer` pre-locks both wallets in ascending tg_id order precisely so two
        opposite transfers cannot deadlock. A future rewrite that drops that ordering
        fails here (deadlock or 5s lock_timeout) instead of in production.
        """
        await pg_user_factory(tg_id=1, coins=1000, username="alice")
        await pg_user_factory(tg_id=2, coins=1000, username="bob")

        async def move(src: int, dst: int):
            async with pg_sessions() as s:
                await eco.transfer(s, src, dst, 10)
                await s.commit()

        await asyncio.gather(move(1, 2), move(2, 1))

        assert await truth(pg_sessions, coins_of(1)) == 1000
        assert await truth(pg_sessions, coins_of(2)) == 1000


# ===========================================================================
# daily XP cap
# ===========================================================================

class TestDailyXpCap:
    @pytest.mark.xfail(
        strict=True,
        reason="grant_xp's capped path reads xp_today off the held User, so the "
               "daily cap can be exceeded and one grant is lost",
    )
    async def test_cap_is_not_bypassed(self, pg_sessions, pg_user_factory, monkeypatch):
        monkeypatch.setattr(xp_service.settings, "xp_daily_participation_cap", 50)
        await pg_user_factory(tg_id=1, coins=0)

        async with pg_sessions() as sa:
            _held = await load_and_hold(sa, 1)  # cache: xp_today=0

            async with pg_sessions() as sb:
                await xp_service.grant_xp(sb, 1, 40, XpSource.daily, capped=True)
                await sb.commit()

            result = await xp_service.grant_xp(sa, 1, 40, XpSource.daily, capped=True)
            await sa.commit()

        assert result.granted == 10, f"granted {result.granted}: the 50 XP cap was bypassed"
        assert await truth(pg_sessions, user_col(1, User.xp)) == 50
        assert await truth(pg_sessions, user_col(1, User.xp_today)) == 50


# ===========================================================================
# betting lifecycle
# ===========================================================================

async def _open_event(session, creator: int = 1) -> tuple[int, int]:
    """Create an open event; return (event_id, first_option_id).

    Returns ids rather than the instance, and re-selects with `selectinload` the way
    tests/integration/test_bet_locking.py does — accessing `event.options` after the
    commit would lazy-load and raise MissingGreenlet.
    """
    event = await bet_svc.create_event(
        session,
        creator_tg_id=creator,
        title="Race",
        description="d",
        options=[{"label": "A"}, {"label": "B"}],
    )
    await session.commit()
    loaded = (
        await session.execute(
            select(BettingEvent)
            .where(BettingEvent.id == event.id)
            .options(selectinload(BettingEvent.options))
        )
    ).scalar_one()
    return loaded.id, loaded.options[0].id


class TestBettingLifecycle:
    @pytest.mark.xfail(
        strict=True,
        reason="_auto_lock holds the event across lock_event, whose FOR UPDATE then "
               "returns the stale status — two overlapping locks both proceed",
    )
    async def test_double_lock_is_refused_when_the_caller_holds_the_event(
        self, pg_sessions, pg_user_factory
    ):
        """Mirrors `handlers/event_types/bet_type._auto_lock`, the one flow found to
        hold an entity across a locking service call."""
        await pg_user_factory(tg_id=1, coins=1000, username="admin")
        async with pg_sessions() as setup:
            event_id, _ = await _open_event(setup, 1)

        async with pg_sessions() as sa:
            held = await bet_svc.get_event_detail(sa, event_id)  # bound, as _auto_lock does
            assert held is not None and held.status == EventStatus.open.value

            async with pg_sessions() as sb:
                await bet_svc.lock_event(sb, event_id)
                await sb.commit()

            with pytest.raises(EventAlreadySettledError):
                await bet_svc.lock_event(sa, event_id)
                await sa.commit()

    @pytest.mark.xfail(
        strict=True,
        reason="resolve_event walks the held event's cached (empty) user_bets, so a "
               "bet placed meanwhile is debited but never settled",
    )
    async def test_bet_placed_before_resolution_is_settled(self, pg_sessions, pg_user_factory):
        await pg_user_factory(tg_id=1, coins=1000, username="admin")
        await pg_user_factory(tg_id=2, coins=1000, username="punter")
        async with pg_sessions() as setup:
            event_id, opt_id = await _open_event(setup, 1)

        async with pg_sessions() as sa:
            held = await bet_svc.get_event_detail(sa, event_id)  # user_bets loaded: empty
            assert held is not None

            async with pg_sessions() as sb:
                await bet_svc.place_bet(sb, 2, event_id, opt_id, 100)
                await sb.commit()

            await bet_svc.resolve_event(sa, event_id, opt_id)
            await sa.commit()

        status = await truth(
            pg_sessions, select(UserBet.status).where(UserBet.user_tg_id == 2)
        )
        assert status != BetStatus.pending.value, "stake debited but never settled"

    async def test_double_resolution_pays_once_from_a_clean_session(
        self, pg_sessions, pg_user_factory
    ):
        """Mirrors `cb_admin_confirm_resolve`, which calls resolve_event with nothing
        preloaded. Expected green — this is the evidence that the admin resolve path
        is already safe, so the fix does not need to touch it.
        """
        await pg_user_factory(tg_id=1, coins=1000, username="admin")
        await pg_user_factory(tg_id=2, coins=1000, username="punter")
        async with pg_sessions() as setup:
            event_id, opt_id = await _open_event(setup, 1)
            await bet_svc.place_bet(setup, 2, event_id, opt_id, 100)
            await setup.commit()

        async with pg_sessions() as sa:
            async with pg_sessions() as sb:
                await bet_svc.resolve_event(sb, event_id, opt_id)
                await sb.commit()

            with pytest.raises(EventAlreadySettledError):
                await bet_svc.resolve_event(sa, event_id, opt_id)
                await sa.commit()

        assert await truth(pg_sessions, coins_of(2)) == 1000  # stake back, paid once
        assert await truth(pg_sessions, user_col(2, User.bets_won)) == 1


# ===========================================================================
# quiz close
# ===========================================================================

class TestQuizClose:
    @pytest.mark.xfail(
        strict=True,
        reason="get_quiz(for_update=True) returns the held quiz's stale status, and "
               "award_prizes has no idempotency guard of its own",
    )
    async def test_locking_read_sees_a_committed_close(self, pg_sessions, pg_user_factory):
        """`handlers.quiz.close_quiz` guards the payout with a status check read via
        `get_quiz(for_update=True)`. Tested at service level (the handler needs a bot).
        """
        await pg_user_factory(tg_id=1, coins=0, username="admin")
        await pg_user_factory(tg_id=2, coins=0, username="player")

        async with pg_sessions() as setup:
            quiz = await quiz_svc.create_quiz(
                setup, creator_tg_id=1, title="Q", description="d", prize_first=500
            )
            question = await quiz_svc.add_question(setup, quiz.id, "1+1?", ["2", "3"], 0, None)
            quiz_id, question_id = quiz.id, question.id
            await quiz_svc.set_status(setup, quiz_id, "running")
            await setup.commit()
            await quiz_svc.record_answer(setup, quiz_id, question_id, 2, 0)
            await setup.commit()

        async with pg_sessions() as sa:
            held = await quiz_svc.get_quiz(sa, quiz_id)  # bound: status=running
            assert held is not None and held.status == "running"

            async with pg_sessions() as sb:
                await quiz_svc.award_prizes(sb, quiz_id)
                await quiz_svc.set_status(sb, quiz_id, "finished")
                await sb.commit()

            observed = await quiz_svc.get_quiz(sa, quiz_id, for_update=True)
            assert observed is not None
            assert observed.status == "finished", (
                "the locking read returned a stale status — close_quiz would pay the "
                "prizes a second time"
            )

        assert await truth(pg_sessions, coins_of(2)) == 500
        assert await truth(pg_sessions, select(Quiz.status).where(Quiz.id == quiz_id)) == "finished"
