"""Real-PostgreSQL races for Alduino terminal settlement."""

from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update

from database.models import (
    AIGameRewardAllocation,
    AIGameRewardSettlement,
    AIGameSession,
    AIGameTurn,
    BettingOption,
    LedgerEntry,
    TwentyQuestionsGame,
    User,
    Wallet,
)
from services import ai_game_service, bet_service
from services.ai_game_types import (
    FinishReason,
    QuestionVerdict,
    TurnKind,
    TurnOutcome,
    TurnRejectReason,
)
from services.twenty_questions_catalog import GameDossier


pytestmark = pytest.mark.pg

TARGET = GameDossier(
    "portal_2",
    "Portal 2",
    ("portal two",),
    "Puzzle cooperativo con portali nei laboratori Aperture Science.",
)
_BASE_TURNS = (
    (10, "question", "Domanda 10?", '{"verdetto":"si"}'),
    (20, "question", "Domanda 20?", '{"verdetto":"no"}'),
    (20, "guess", "Half-Life 2", '{"correct":false}'),
)
_WINNING_TURN = (10, "guess", "Portal 2", '{"correct":true}')


def _rewards():
    """Keep the initial RED in test execution instead of failing collection."""
    return importlib.import_module("services.ai_game_rewards")


def _first_non_cancelled_error(results: tuple[Any, ...]) -> BaseException | None:
    """Return a task/rollback failure, excluding cancellation requested by cleanup."""
    return next(
        (
            result for result in results
            if isinstance(result, BaseException)
            and not isinstance(result, asyncio.CancelledError)
        ),
        None,
    )


async def _cleanup_actor_race(
    tasks: tuple[asyncio.Task[Any], ...],
    actor_sessions: tuple[Any, ...],
    *,
    finish_before_cancel: tuple[asyncio.Task[Any], ...] = (),
) -> BaseException | None:
    """Drain race workers and roll back their sessions without masking a primary failure."""
    if finish_before_cancel:
        await asyncio.wait(finish_before_cancel, timeout=3)
    for task in tasks:
        if not task.done():
            task.cancel()
    cleanup_error: BaseException | None = None
    try:
        task_results = tuple(await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=3,
        ))
    except BaseException as exc:
        cleanup_error = exc
    else:
        cleanup_error = _first_non_cancelled_error(task_results)
    try:
        rollback_results = tuple(await asyncio.wait_for(
            asyncio.gather(
                *(db_session.rollback() for db_session in actor_sessions),
                return_exceptions=True,
            ),
            timeout=3,
        ))
    except BaseException as exc:
        if cleanup_error is None:
            cleanup_error = exc
    else:
        if cleanup_error is None:
            cleanup_error = _first_non_cancelled_error(rollback_results)
    return cleanup_error


@asynccontextmanager
async def _managed_actor_race(
    tasks: tuple[asyncio.Task[Any], ...],
    actor_sessions: tuple[Any, ...],
):
    """Ensure every explicit actor task and session is drained on both race paths."""
    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error = await _cleanup_actor_race(tasks, actor_sessions)
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


async def _run_actor_race(
    tasks: tuple[asyncio.Task[Any], ...],
    actor_sessions: tuple[Any, ...],
) -> tuple[Any, ...]:
    """Run explicit concurrent actors and preserve their error over cleanup failures."""
    async with _managed_actor_race(tasks, actor_sessions):
        async with asyncio.timeout(10):
            return tuple(await asyncio.gather(*tasks))


async def _release_root_and_cleanup(
    release_root: asyncio.Event,
    lock_task: asyncio.Task[Any],
    action_task: asyncio.Task[Any] | None,
    actor_sessions: tuple[Any, ...],
) -> BaseException | None:
    """Unblock root-lock tests, then use the shared bounded actor cleanup."""
    release_root.set()
    tasks = (lock_task,) if action_task is None else (lock_task, action_task)
    return await _cleanup_actor_race(
        tasks, actor_sessions, finish_before_cancel=(lock_task,),
    )


@asynccontextmanager
async def _managed_root_lock_race(
    release_root: asyncio.Event,
    lock_task: asyncio.Task[Any],
    action_task: list[asyncio.Task[Any] | None],
    actor_sessions: tuple[Any, ...],
):
    """Release a held root and drain actors without replacing the body failure."""
    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error = await _release_root_and_cleanup(
            release_root, lock_task, action_task[0], actor_sessions,
        )
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


async def test_pg_root_lock_cleanup_surfaces_unawaited_locker_failure(pg_sessions):
    """A successful body must not conceal a completed root-lock worker failure."""
    released = asyncio.Event()
    root_locked = asyncio.Event()

    class ActorSession:
        async def rollback(self) -> None:
            return None

    async def fail_after_release() -> None:
        root_locked.set()
        await released.wait()
        raise RuntimeError("locker failed after release")

    action_task: list[asyncio.Task[Any] | None] = [None]
    lock_task = asyncio.create_task(fail_after_release())
    with pytest.raises(RuntimeError, match="locker failed after release"):
        async with _managed_root_lock_race(
            released, lock_task, action_task, (ActorSession(),),
        ):
            await asyncio.wait_for(root_locked.wait(), timeout=3)


async def _setup_running(
    pg_sessions,
    monkeypatch,
    *,
    turns: tuple[tuple[int, str, str, str], ...] = _BASE_TURNS + (_WINNING_TURN,),
    users: tuple[int, ...] = (10, 20),
    seed_accounts: bool = True,
    now: datetime | None = None,
    duration_seconds: int = 43_200,
    max_coins_per_participant: int = 100,
    title: str = "Concorrenza",
) -> int:
    monkeypatch.setattr(ai_game_service.settings, "twentyq_v2_enabled", True)
    monkeypatch.setattr(
        ai_game_service, "has_configured_twenty_questions_provider", lambda: True,
    )
    async with pg_sessions() as setup:
        if seed_accounts:
            for tg_id in users:
                setup.add(User(tg_id=tg_id, full_name=f"User {tg_id}"))
                setup.add(Wallet(tg_id=tg_id, coins=0))
        created = await ai_game_service.create_twenty_questions(
            setup,
            creator_tg_id=9,
            title=title,
            duration_seconds=duration_seconds,
            expires_at=None,
            max_coins_per_participant=max_coins_per_participant,
            target=TARGET,
        )
        started = await ai_game_service.start(
            setup, created.session_id, group_id=-1001, now=now,
        )
        assert started.started
        setup.add_all([
            AIGameTurn(
                session_id=created.session_id,
                turn_no=turn_no,
                user_tg_id=user_tg_id,
                kind=kind,
                input_text=input_text,
                output_json=output_json,
                normalized_input_hash=f"{turn_no:064x}",
            )
            for turn_no, (user_tg_id, kind, input_text, output_json) in enumerate(
                turns, start=1,
            )
        ])
        await setup.execute(
            update(AIGameSession)
            .where(AIGameSession.id == created.session_id)
            .values(next_turn_no=len(turns) + 1)
        )
        await setup.execute(
            update(TwentyQuestionsGame)
            .where(TwentyQuestionsGame.session_id == created.session_id)
            .values(
                questions_used=sum(
                    kind == TurnKind.question.value for _, kind, _, _ in turns
                ),
                guesses_used=sum(
                    kind == TurnKind.guess.value for _, kind, _, _ in turns
                ),
            )
        )
        await setup.commit()
        return created.session_id


async def _append_winning_turn(session, session_id: int) -> None:
    session.add(AIGameTurn(
        session_id=session_id,
        turn_no=4,
        user_tg_id=10,
        kind="guess",
        input_text="Portal 2",
        output_json='{"correct":true}',
        normalized_input_hash="4" * 64,
    ))
    await session.execute(
        update(AIGameSession)
        .where(AIGameSession.id == session_id)
        .values(next_turn_no=5)
    )
    await session.flush()


def _synchronize_terminal_claim(monkeypatch, sessions) -> None:
    """Bring independent workers to the same root-CAS boundary before either runs it."""
    barrier = asyncio.Barrier(len(sessions))
    for db_session in sessions:
        original_execute = db_session.execute
        state = {"waited": False}

        async def execute(statement, *args, _original=original_execute, _state=state, **kwargs):
            table = getattr(statement, "table", None)
            if (
                not _state["waited"]
                and getattr(statement, "is_update", False)
                and getattr(table, "name", None) == AIGameSession.__tablename__
            ):
                _state["waited"] = True
                await barrier.wait()
            return await _original(statement, *args, **kwargs)

        monkeypatch.setattr(db_session, "execute", execute)


def _synchronize_first_turn_claim(monkeypatch, sessions) -> None:
    """Release independent workers at the first lease boundary, not a timing guess."""
    barrier = asyncio.Barrier(len(sessions))
    for db_session in sessions:
        original_execute = db_session.execute
        state = {"waited": False}

        async def execute(statement, *args, _original=original_execute, _state=state, **kwargs):
            table = getattr(statement, "table", None)
            locks_root = (
                getattr(statement, "is_select", False)
                and getattr(statement, "_for_update_arg", None) is not None
            )
            if (
                not _state["waited"]
                and (
                    locks_root
                    or (
                        getattr(statement, "is_update", False)
                        and getattr(table, "name", None) == AIGameSession.__tablename__
                    )
                )
            ):
                _state["waited"] = True
                await barrier.wait()
            return await _original(statement, *args, **kwargs)

        monkeypatch.setattr(db_session, "execute", execute)


def _synchronize_first_account_lock(monkeypatch, rewards, sessions):
    """Hold each actor immediately before its first real User -> Wallet lock helper call."""
    original = rewards._lock_and_validate_users_then_wallets
    actor_positions = {id(db_session): index for index, db_session in enumerate(sessions)}
    reached = tuple(asyncio.Event() for _ in sessions)
    all_reached = asyncio.Event()
    release = asyncio.Event()
    entered: set[int] = set()

    async def synchronized_lock(db_session, participants):
        position = actor_positions.get(id(db_session))
        if position is None or position in entered:
            return await original(db_session, participants)
        entered.add(position)
        reached[position].set()
        if len(entered) == len(sessions):
            all_reached.set()
        await asyncio.wait_for(release.wait(), timeout=3)
        return await original(db_session, participants)

    monkeypatch.setattr(rewards, "_lock_and_validate_users_then_wallets", synchronized_lock)
    return reached, all_reached, release


def _targets_table(statement, table_name: str) -> bool:
    """Recognise the real SQLAlchemy statement that reaches one mapped table."""
    if getattr(getattr(statement, "table", None), "name", None) == table_name:
        return True
    get_final_froms = getattr(statement, "get_final_froms", None)
    return bool(get_final_froms) and any(
        getattr(table, "name", None) == table_name for table in get_final_froms()
    )


async def _settlement_facts(pg_sessions, session_id: int) -> dict[str, object]:
    async with pg_sessions() as observe:
        root = (await observe.execute(select(
            AIGameSession.status,
            AIGameSession.finish_reason,
            AIGameSession.next_turn_no,
        ).where(AIGameSession.id == session_id))).one()
        winner = (await observe.execute(select(TwentyQuestionsGame.winner_tg_id).where(
            TwentyQuestionsGame.session_id == session_id,
        ))).scalar_one()
        settlement = (await observe.execute(select(
            AIGameRewardSettlement.status,
            AIGameRewardSettlement.finish_reason,
            AIGameRewardSettlement.participant_count,
            AIGameRewardSettlement.question_count,
            AIGameRewardSettlement.wrong_guess_count,
            AIGameRewardSettlement.base_amount,
            AIGameRewardSettlement.penalty_amount,
            AIGameRewardSettlement.computed_pool,
            AIGameRewardSettlement.paid_pool,
            AIGameRewardSettlement.share,
            AIGameRewardSettlement.remainder,
        ).where(AIGameRewardSettlement.session_id == session_id))).one()
        allocations = (await observe.execute(
            select(func.count()).select_from(AIGameRewardAllocation).where(
                AIGameRewardAllocation.session_id == session_id,
            )
        )).scalar_one()
        ledger = (await observe.execute(select(func.count()).select_from(LedgerEntry))).scalar_one()
        turns = (await observe.execute(
            select(func.count()).select_from(AIGameTurn).where(AIGameTurn.session_id == session_id)
        )).scalar_one()
        wallets = dict((await observe.execute(select(Wallet.tg_id, Wallet.coins))).all())
        xp = dict((await observe.execute(select(User.tg_id, User.xp))).all())
    return {
        "root": root,
        "winner": winner,
        "settlement": settlement,
        "allocations": int(allocations),
        "ledger": int(ledger),
        "turns": int(turns),
        "wallets": wallets,
        "xp": xp,
    }


async def test_pg_terminalize_two_victories_claim_once_and_pay_once(
    pg_sessions, monkeypatch,
):
    """Without the root CAS, two workers can create duplicate allocations and credits."""
    rewards = _rewards()
    session_id = await _setup_running(pg_sessions, monkeypatch)

    async with pg_sessions() as first, pg_sessions() as second:
        _synchronize_terminal_claim(monkeypatch, (first, second))

        async def run(db_session):
            result = await rewards.terminalize(
                db_session,
                session_id=session_id,
                reason=FinishReason.victory,
                winner_tg_id=10,
                now=datetime(2026, 8, 23, 12, 0),
            )
            await db_session.commit()
            return result

        first_result, second_result = await _run_actor_race(
            (
                asyncio.create_task(run(first)),
                asyncio.create_task(run(second)),
            ),
            (first, second),
        )

    assert sorted(result.transitioned for result in (first_result, second_result)) == [False, True]
    assert all(result.finish_reason is FinishReason.victory for result in (
        first_result, second_result,
    ))
    facts = await _settlement_facts(pg_sessions, session_id)
    assert facts == {
        "root": ("finished", "victory", 5),
        "winner": 10,
        "settlement": ("settled", "victory", 2, 2, 1, 200, 32, 168, 168, 84, 0),
        "allocations": 2,
        "ledger": 2,
        "turns": 4,
        "wallets": {10: 84, 20: 84},
        "xp": {10: 10, 20: 10},
    }
    async with pg_sessions() as observe:
        allocated_coins, allocated_xp = (await observe.execute(select(
            func.sum(AIGameRewardAllocation.coins),
            func.sum(AIGameRewardAllocation.xp),
        ).where(AIGameRewardAllocation.session_id == session_id))).one()
        ledger_coins = (await observe.execute(select(func.sum(LedgerEntry.amount)))).scalar_one()
    assert (allocated_coins, ledger_coins, allocated_xp) == (168, 168, 20)


@pytest.mark.parametrize(
    "competing_reason", (FinishReason.expired, FinishReason.admin_closed),
)
async def test_pg_terminalize_victory_vs_terminal_close_persists_one_reason_and_one_reward_set(
    pg_sessions, monkeypatch, competing_reason,
):
    """Without replay loading the losing closer could overwrite the winner's terminal fact."""
    rewards = _rewards()
    session_id = await _setup_running(pg_sessions, monkeypatch)

    async with pg_sessions() as victor, pg_sessions() as expirer:
        _synchronize_terminal_claim(monkeypatch, (victor, expirer))

        async def run_victory():
            result = await rewards.terminalize(
                victor,
                session_id=session_id,
                reason=FinishReason.victory,
                winner_tg_id=10,
            )
            await victor.commit()
            return result

        async def run_competing_close():
            result = await rewards.terminalize(
                expirer,
                session_id=session_id,
                reason=competing_reason,
            )
            await expirer.commit()
            return result

        victory, competing_close = await _run_actor_race(
            (
                asyncio.create_task(run_victory()),
                asyncio.create_task(run_competing_close()),
            ),
            (victor, expirer),
        )

    assert sorted(result.transitioned for result in (victory, competing_close)) == [False, True]
    assert victory.finish_reason is competing_close.finish_reason
    assert victory.finish_reason in (FinishReason.victory, competing_reason)
    facts = await _settlement_facts(pg_sessions, session_id)
    reason = facts["root"][1]
    assert reason == facts["settlement"][1] == victory.finish_reason.value
    assert facts["allocations"] == 2
    assert facts["turns"] == 4
    assert facts["xp"] == {10: 10, 20: 10}
    assert facts["winner"] == (10 if victory.finish_reason is FinishReason.victory else None)
    if victory.finish_reason is FinishReason.victory:
        assert (facts["settlement"], facts["ledger"], facts["wallets"]) == (
            ("settled", "victory", 2, 2, 1, 200, 32, 168, 168, 84, 0),
            2,
            {10: 84, 20: 84},
        )
    else:
        assert (facts["settlement"], facts["ledger"], facts["wallets"]) == (
            ("settled", competing_reason.value, 2, 2, 1, 200, 32, 168, 0, 84, 0),
            0,
            {10: 0, 20: 0},
        )


async def test_pg_concurrent_replay_reconstructs_one_immutable_settlement(
    pg_sessions, monkeypatch,
):
    """Without immutable replay both terminal callers could create a second payout."""
    rewards = _rewards()
    session_id = await _setup_running(pg_sessions, monkeypatch)
    async with pg_sessions() as setup:
        first = await rewards.terminalize(
            setup,
            session_id=session_id,
            reason=FinishReason.victory,
            winner_tg_id=10,
        )
        await setup.commit()

    async with pg_sessions() as one, pg_sessions() as two:
        _synchronize_terminal_claim(monkeypatch, (one, two))

        async def replay(db_session):
            result = await rewards.terminalize(
                db_session,
                session_id=session_id,
                reason=FinishReason.expired,
            )
            await db_session.commit()
            return result

        first_replay, second_replay = await _run_actor_race(
            (
                asyncio.create_task(replay(one)),
                asyncio.create_task(replay(two)),
            ),
            (one, two),
        )

    assert first.transitioned
    assert not first_replay.transitioned and not second_replay.transitioned
    assert first_replay.finish_reason is second_replay.finish_reason is FinishReason.victory
    assert first_replay.reward == second_replay.reward == first.reward
    assert first_replay.allocations == second_replay.allocations == first.allocations
    facts = await _settlement_facts(pg_sessions, session_id)
    assert facts == {
        "root": ("finished", "victory", 5),
        "winner": 10,
        "settlement": ("settled", "victory", 2, 2, 1, 200, 32, 168, 168, 84, 0),
        "allocations": 2,
        "ledger": 2,
        "turns": 4,
        "wallets": {10: 84, 20: 84},
        "xp": {10: 10, 20: 10},
    }
    async with pg_sessions() as observe:
        allocated_coins, allocated_xp = (await observe.execute(select(
            func.sum(AIGameRewardAllocation.coins),
            func.sum(AIGameRewardAllocation.xp),
        ).where(AIGameRewardAllocation.session_id == session_id))).one()
        ledger_coins = (await observe.execute(select(func.sum(LedgerEntry.amount)))).scalar_one()
    assert (allocated_coins, ledger_coins, allocated_xp) == (168, 168, 20)


@pytest.mark.parametrize("failure_at", ("credit", "grant"))
async def test_pg_kth_payout_failure_rolls_back_for_an_independent_observer(
    pg_sessions, monkeypatch, failure_at,
):
    """Without caller rollback, an observer would see a winning turn or partial reward."""
    rewards = _rewards()
    session_id = await _setup_running(pg_sessions, monkeypatch, turns=_BASE_TURNS)

    class PayoutFault(RuntimeError):
        pass

    first_write = asyncio.Event()
    observer_checked = asyncio.Event()
    rolled_back = asyncio.Event()
    calls = 0
    if failure_at == "credit":
        original = rewards.economy_service.credit

        async def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PayoutFault("second credit")
            result = await original(*args, **kwargs)
            first_write.set()
            await asyncio.wait_for(observer_checked.wait(), timeout=3)
            return result

        monkeypatch.setattr(rewards.economy_service, "credit", fail_second)
    else:
        original = rewards.xp_service.grant_xp

        async def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PayoutFault("second grant")
            result = await original(*args, **kwargs)
            first_write.set()
            await asyncio.wait_for(observer_checked.wait(), timeout=3)
            return result

        monkeypatch.setattr(rewards.xp_service, "grant_xp", fail_second)

    async with pg_sessions() as failing:

        async def fail_and_rollback():
            await _append_winning_turn(failing, session_id)
            with pytest.raises(PayoutFault):
                await rewards.terminalize(
                    failing,
                    session_id=session_id,
                    reason=FinishReason.victory,
                    winner_tg_id=10,
                )
            await failing.rollback()
            rolled_back.set()

        async def observe_uncommitted_then_rolled_back():
            await asyncio.wait_for(first_write.wait(), timeout=3)
            before = await _settlement_facts(pg_sessions, session_id)
            assert before == {
                "root": ("running", None, 4),
                "winner": None,
                "settlement": ("pending", None, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                "allocations": 0,
                "ledger": 0,
                "turns": 3,
                "wallets": {10: 0, 20: 0},
                "xp": {10: 0, 20: 0},
            }
            observer_checked.set()
            await asyncio.wait_for(rolled_back.wait(), timeout=3)
            after = await _settlement_facts(pg_sessions, session_id)
            assert after == before

        try:
            await _run_actor_race(
                (
                    asyncio.create_task(fail_and_rollback()),
                    asyncio.create_task(observe_uncommitted_then_rolled_back()),
                ),
                (failing,),
            )
        finally:
            if failure_at == "credit":
                monkeypatch.setattr(rewards.economy_service, "credit", original)
            else:
                monkeypatch.setattr(rewards.xp_service, "grant_xp", original)
    if failure_at == "credit":
        assert rewards.economy_service.credit is original
    else:
        assert rewards.xp_service.grant_xp is original

    async with pg_sessions() as retry:
        await _append_winning_turn(retry, session_id)
        retried = await rewards.terminalize(
            retry,
            session_id=session_id,
            reason=FinishReason.victory,
            winner_tg_id=10,
        )
        await retry.commit()
    assert retried.transitioned
    assert await _settlement_facts(pg_sessions, session_id) == {
        "root": ("finished", "victory", 5),
        "winner": 10,
        "settlement": ("settled", "victory", 2, 2, 1, 200, 32, 168, 168, 84, 0),
        "allocations": 2,
        "ledger": 2,
        "turns": 4,
        "wallets": {10: 84, 20: 84},
        "xp": {10: 10, 20: 10},
    }


@pytest.mark.parametrize("missing", ("user", "wallet"))
async def test_pg_missing_participant_account_rolls_back_without_partial_payment(
    pg_sessions, monkeypatch, missing,
):
    """Account prevalidation must fail before any credit and leave terminalization retryable."""
    rewards = _rewards()
    session_id = await _setup_running(pg_sessions, monkeypatch)
    async with pg_sessions() as remove_account:
        if missing == "user":
            await remove_account.execute(delete(User).where(User.tg_id == 20))
        else:
            await remove_account.execute(delete(Wallet).where(Wallet.tg_id == 20))
        await remove_account.commit()

    original_credit = rewards.economy_service.credit

    async def credit_must_not_run(*args, **kwargs):
        raise AssertionError("participant accounts must be validated before the first credit")

    monkeypatch.setattr(rewards.economy_service, "credit", credit_must_not_run)
    async with pg_sessions() as failing:
        with pytest.raises(rewards.RewardSettlementError):
            await rewards.terminalize(
                failing,
                session_id=session_id,
                reason=FinishReason.victory,
                winner_tg_id=10,
            )
        await failing.rollback()
    monkeypatch.setattr(rewards.economy_service, "credit", original_credit)

    async with pg_sessions() as observe:
        root = (await observe.execute(select(
            AIGameSession.status, AIGameSession.finish_reason,
        ).where(AIGameSession.id == session_id))).one()
        settlement = (await observe.execute(select(
            AIGameRewardSettlement.status, AIGameRewardSettlement.finish_reason,
        ).where(AIGameRewardSettlement.session_id == session_id))).one()
        allocations = (await observe.execute(
            select(func.count()).select_from(AIGameRewardAllocation).where(
                AIGameRewardAllocation.session_id == session_id,
            )
        )).scalar_one()
        ledger = (await observe.execute(select(func.count()).select_from(LedgerEntry))).scalar_one()
        wallets = dict((await observe.execute(select(Wallet.tg_id, Wallet.coins))).all())
        xp = dict((await observe.execute(select(User.tg_id, User.xp))).all())
    assert root == ("running", None)
    assert settlement == ("pending", None)
    assert (allocations, ledger) == (0, 0)
    if missing == "user":
        assert (wallets, xp) == ({10: 0}, {10: 0})
    else:
        assert (wallets, xp) == ({10: 0}, {10: 0, 20: 0})

    async with pg_sessions() as restore_account:
        if missing == "user":
            restore_account.add(User(tg_id=20, full_name="User 20"))
        restore_account.add(Wallet(tg_id=20, coins=0))
        await restore_account.commit()
    async with pg_sessions() as retry:
        retried = await rewards.terminalize(
            retry,
            session_id=session_id,
            reason=FinishReason.victory,
            winner_tg_id=10,
        )
        await retry.commit()
    assert retried.transitioned
    assert await _settlement_facts(pg_sessions, session_id) == {
        "root": ("finished", "victory", 5),
        "winner": 10,
        "settlement": ("settled", "victory", 2, 2, 1, 200, 32, 168, 168, 84, 0),
        "allocations": 2,
        "ledger": 2,
        "turns": 4,
        "wallets": {10: 84, 20: 84},
        "xp": {10: 10, 20: 10},
    }


async def test_pg_terminalize_and_place_bet_acquire_user_before_wallet(
    pg_sessions, monkeypatch,
):
    """Old place_bet took Wallet then its XP UPDATE(User), deadlocking terminalize."""
    rewards = _rewards()
    monkeypatch.setattr(bet_service.settings, "xp_per_bet_placed", 10)
    session_id = await _setup_running(pg_sessions, monkeypatch)
    async with pg_sessions() as setup:
        await setup.execute(update(Wallet).where(Wallet.tg_id == 10).values(coins=1_000))
        event = await bet_service.create_event(
            setup,
            creator_tg_id=10,
            title="Ordine lock",
            description="interleaving reale",
            options=[{"label": "A"}],
        )
        await setup.flush()
        option_id = (await setup.execute(
            select(BettingOption.id).where(BettingOption.event_id == event.id)
        )).scalar_one()
        await setup.commit()

    terminalizer_has_user = asyncio.Event()
    bet_touches_user = asyncio.Event()
    async with pg_sessions() as terminalizer, pg_sessions() as bettor:
        terminal_execute = terminalizer.execute

        async def terminal_execute_with_barrier(statement, *args, **kwargs):
            result = await terminal_execute(statement, *args, **kwargs)
            if (
                _targets_table(statement, User.__tablename__)
                and getattr(statement, "_for_update_arg", None) is not None
                and not terminalizer_has_user.is_set()
            ):
                terminalizer_has_user.set()
                await asyncio.wait_for(bet_touches_user.wait(), timeout=3)
            return result

        bet_execute = bettor.execute

        async def bet_execute_with_signal(statement, *args, **kwargs):
            if _targets_table(statement, User.__tablename__):
                bet_touches_user.set()
            return await bet_execute(statement, *args, **kwargs)

        monkeypatch.setattr(terminalizer, "execute", terminal_execute_with_barrier)
        monkeypatch.setattr(bettor, "execute", bet_execute_with_signal)

        async def settle():
            result = await rewards.terminalize(
                terminalizer,
                session_id=session_id,
                reason=FinishReason.victory,
                winner_tg_id=10,
            )
            await terminalizer.commit()
            return result

        async def bet():
            result = await bet_service.place_bet(
                bettor,
                user_tg_id=10,
                event_id=event.id,
                option_id=option_id,
                amount=100,
            )
            await bettor.commit()
            return result

        tasks = (asyncio.create_task(settle()), asyncio.create_task(bet()))
        outcomes: list[object] = []
        try:
            done, pending = await asyncio.wait(tasks, timeout=10)
            if pending:
                outcomes.append("timed out waiting for the coordinated lock order")
                for task in pending:
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in tasks:
                try:
                    outcomes.append(task.result())
                except BaseException as exc:  # the old order is expected to deadlock
                    outcomes.append(exc)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await terminalizer.rollback()
            await bettor.rollback()

    assert terminalizer_has_user.is_set()
    assert bet_touches_user.is_set()
    assert len(outcomes) == 2 and all(
        not isinstance(outcome, BaseException) and not isinstance(outcome, str)
        for outcome in outcomes
    ), f"wallet-before-user deadlocked the real service paths: {outcomes!r}"
    settled, placed = outcomes
    assert settled.transitioned
    assert placed.user_tg_id == 10
    facts = await _settlement_facts(pg_sessions, session_id)
    assert facts["wallets"] == {10: 984, 20: 84}
    assert facts["xp"] == {10: 20, 20: 10}


async def test_pg_overlapping_game_settlements_with_reversed_participants_do_not_deadlock(
    pg_sessions, monkeypatch,
):
    """Two roots sharing accounts must serialize User then Wallet locks by ascending tg_id."""
    rewards = _rewards()
    first_session_id = await _setup_running(
        pg_sessions,
        monkeypatch,
        title="Prima gara",
    )
    second_session_id = await _setup_running(
        pg_sessions,
        monkeypatch,
        title="Seconda gara",
        seed_accounts=False,
        turns=(
            (20, TurnKind.question.value, "Domanda 20?", '{"verdetto":"si"}'),
            (10, TurnKind.question.value, "Domanda 10?", '{"verdetto":"no"}'),
            (10, TurnKind.guess.value, "Half-Life 2", '{"correct":false}'),
            (20, TurnKind.guess.value, "Portal 2", '{"correct":true}'),
        ),
    )
    async with pg_sessions() as first, pg_sessions() as second:
        reached_account_lock, all_reached_account_lock, release_account_lock = (
            _synchronize_first_account_lock(monkeypatch, rewards, (first, second))
        )

        async def settle(db_session, session_id: int, winner_tg_id: int):
            result = await rewards.terminalize(
                db_session,
                session_id=session_id,
                reason=FinishReason.victory,
                winner_tg_id=winner_tg_id,
            )
            await db_session.commit()
            return result

        tasks = (
            asyncio.create_task(settle(first, first_session_id, 10)),
            asyncio.create_task(settle(second, second_session_id, 20)),
        )
        async with _managed_actor_race(tasks, (first, second)):
            await asyncio.wait_for(all_reached_account_lock.wait(), timeout=3)
            assert all(event.is_set() for event in reached_account_lock)
            release_account_lock.set()
            first_result, second_result = await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=10,
            )

    assert first_result.transitioned and second_result.transitioned
    async with pg_sessions() as observe:
        roots = (await observe.execute(select(
            AIGameSession.id, AIGameSession.status, AIGameSession.finish_reason,
        ).where(AIGameSession.id.in_((first_session_id, second_session_id))).order_by(
            AIGameSession.id.asc(),
        ))).all()
        settlements = (await observe.execute(select(
            AIGameRewardSettlement.session_id,
            AIGameRewardSettlement.status,
            AIGameRewardSettlement.finish_reason,
        ).where(
            AIGameRewardSettlement.session_id.in_((first_session_id, second_session_id)),
        ).order_by(AIGameRewardSettlement.session_id.asc()))).all()
        settlement_count = (await observe.execute(
            select(func.count()).select_from(AIGameRewardSettlement).where(
                AIGameRewardSettlement.session_id.in_((first_session_id, second_session_id)),
            )
        )).scalar_one()
        allocations = (await observe.execute(select(
            AIGameRewardAllocation.session_id,
            AIGameRewardAllocation.user_tg_id,
            AIGameRewardAllocation.coins,
            AIGameRewardAllocation.xp,
        ).where(
            AIGameRewardAllocation.session_id.in_((first_session_id, second_session_id)),
        ).order_by(
            AIGameRewardAllocation.session_id.asc(),
            AIGameRewardAllocation.user_tg_id.asc(),
        ))).all()
        ledger = (await observe.execute(select(
            LedgerEntry.description, LedgerEntry.to_tg_id, LedgerEntry.amount,
        ).where(
            LedgerEntry.description.in_((
                f"Premio gioco segreto di Alduino #{first_session_id}",
                f"Premio gioco segreto di Alduino #{second_session_id}",
            )),
        ).order_by(LedgerEntry.description.asc(), LedgerEntry.to_tg_id.asc()))).all()
        wallets = dict((await observe.execute(select(Wallet.tg_id, Wallet.coins))).all())
        xp = dict((await observe.execute(select(User.tg_id, User.xp))).all())
    assert roots == [
        (first_session_id, "finished", "victory"),
        (second_session_id, "finished", "victory"),
    ]
    assert settlements == [
        (first_session_id, "settled", "victory"),
        (second_session_id, "settled", "victory"),
    ]
    assert settlement_count == 2
    assert allocations == [
        (first_session_id, 10, 84, 10),
        (first_session_id, 20, 84, 10),
        (second_session_id, 10, 84, 10),
        (second_session_id, 20, 84, 10),
    ]
    assert ledger == [
        (f"Premio gioco segreto di Alduino #{first_session_id}", 10, 84),
        (f"Premio gioco segreto di Alduino #{first_session_id}", 20, 84),
        (f"Premio gioco segreto di Alduino #{second_session_id}", 10, 84),
        (f"Premio gioco segreto di Alduino #{second_session_id}", 20, 84),
    ]
    assert wallets == {10: 168, 20: 168}
    assert xp == {10: 20, 20: 20}


async def test_pg_zero_participant_terminalization_is_void(
    pg_sessions, monkeypatch,
):
    """A real PostgreSQL close with no valid turns creates neither allocation nor credit."""
    rewards = _rewards()
    session_id = await _setup_running(
        pg_sessions,
        monkeypatch,
        users=(),
        turns=(),
    )
    async with pg_sessions() as closer:
        closed = await rewards.terminalize(
            closer,
            session_id=session_id,
            reason=FinishReason.expired,
        )
        await closer.commit()
    assert closed.transitioned and closed.reward.settlement_status == "void"
    assert await _settlement_facts(pg_sessions, session_id) == {
        "root": ("finished", "expired", 1),
        "winner": None,
        "settlement": ("void", "expired", 0, 0, 0, 0, 0, 0, 0, 0, 0),
        "allocations": 0,
        "ledger": 0,
        "turns": 0,
        "wallets": {},
        "xp": {},
    }


async def test_pg_remainder_is_not_paid_to_any_participant(
    pg_sessions, monkeypatch,
):
    """PostgreSQL settlement preserves the deterministic integer remainder outside payouts."""
    rewards = _rewards()
    session_id = await _setup_running(
        pg_sessions,
        monkeypatch,
        users=(10, 20, 30),
        turns=_BASE_TURNS + (
            (30, TurnKind.question.value, "Domanda 30?", '{"verdetto":"si"}'),
            _WINNING_TURN,
        ),
    )
    async with pg_sessions() as closer:
        closed = await rewards.terminalize(
            closer,
            session_id=session_id,
            reason=FinishReason.victory,
            winner_tg_id=10,
        )
        await closer.commit()
    assert closed.transitioned
    assert await _settlement_facts(pg_sessions, session_id) == {
        "root": ("finished", "victory", 6),
        "winner": 10,
        "settlement": ("settled", "victory", 3, 3, 1, 300, 38, 262, 262, 87, 1),
        "allocations": 3,
        "ledger": 3,
        "turns": 5,
        "wallets": {10: 87, 20: 87, 30: 87},
        "xp": {10: 10, 20: 10, 30: 10},
    }


async def test_pg_admin_close_pays_participation_xp_without_coins(
    pg_sessions, monkeypatch,
):
    """Administrative closure uses the authoritative settlement and awards XP exactly once."""
    rewards = _rewards()
    session_id = await _setup_running(pg_sessions, monkeypatch, turns=_BASE_TURNS)
    async with pg_sessions() as closer:
        closed = await rewards.terminalize(
            closer,
            session_id=session_id,
            reason=FinishReason.admin_closed,
        )
        await closer.commit()
    assert closed.transitioned and closed.finish_reason is FinishReason.admin_closed
    assert await _settlement_facts(pg_sessions, session_id) == {
        "root": ("finished", "admin_closed", 4),
        "winner": None,
        "settlement": ("settled", "admin_closed", 2, 2, 1, 200, 32, 168, 0, 84, 0),
        "allocations": 2,
        "ledger": 0,
        "turns": 3,
        "wallets": {10: 0, 20: 0},
        "xp": {10: 10, 20: 10},
    }


async def test_pg_question_claim_after_root_lock_expiry_is_terminalized_before_lease(
    pg_sessions, monkeypatch,
):
    """Without a post-lock expiry check, a preflighted question gets a late lease."""
    started_at = datetime(2034, 1, 2, 12, 0)
    deadline = started_at + timedelta(seconds=60)
    clock = {"now": deadline - timedelta(seconds=1)}
    monkeypatch.setattr(ai_game_service, "_now", lambda: clock["now"])
    session_id = await _setup_running(
        pg_sessions,
        monkeypatch,
        turns=(),
        now=started_at,
        duration_seconds=60,
    )

    async with pg_sessions() as locker, pg_sessions() as worker:
        root_locked = asyncio.Event()
        worker_reached_claim_lock = asyncio.Event()
        release_root = asyncio.Event()
        original_execute = worker.execute

        async def hold_root() -> None:
            await locker.execute(select(AIGameSession.id).where(
                AIGameSession.id == session_id,
            ).with_for_update())
            root_locked.set()
            await asyncio.wait_for(release_root.wait(), timeout=3)
            await locker.commit()

        async def observe_claim_lock(statement, *args, **kwargs):
            if (
                getattr(statement, "is_select", False)
                and getattr(statement, "_for_update_arg", None) is not None
                and not worker_reached_claim_lock.is_set()
            ):
                worker_reached_claim_lock.set()
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(worker, "execute", observe_claim_lock)
        action_task: list[asyncio.Task[Any] | None] = [None]
        lock_task = asyncio.create_task(hold_root())
        async with _managed_root_lock_race(
            release_root, lock_task, action_task, (locker, worker),
        ):
            await asyncio.wait_for(root_locked.wait(), timeout=3)
            action_task[0] = asyncio.create_task(ai_game_service.begin_question(
                worker,
                session_id=session_id,
                user_tg_id=10,
                question="La richiesta era già oltre il preflight?",
            ))
            await asyncio.wait_for(worker_reached_claim_lock.wait(), timeout=3)
            clock["now"] = deadline + timedelta(seconds=1)
            release_root.set()
            result = await asyncio.wait_for(action_task[0], timeout=5)
            await worker.commit()

    assert (result.outcome, result.reason) == (
        TurnOutcome.rejected, TurnRejectReason.expired,
    )
    assert result.terminal is not None and result.terminal.finish_reason is FinishReason.expired
    async with pg_sessions() as observe:
        root = (await observe.execute(select(
            AIGameSession.status,
            AIGameSession.finish_reason,
            AIGameSession.pending_token,
            AIGameSession.pending_since,
            AIGameSession.pending_user_tg_id,
            AIGameSession.pending_kind,
        ).where(AIGameSession.id == session_id))).one()
        settlement = (await observe.execute(select(
            AIGameRewardSettlement.status,
            AIGameRewardSettlement.finish_reason,
        ).where(AIGameRewardSettlement.session_id == session_id))).one()
        turns = (await observe.execute(select(func.count()).select_from(AIGameTurn).where(
            AIGameTurn.session_id == session_id,
        ))).scalar_one()
        allocations = (await observe.execute(
            select(func.count()).select_from(AIGameRewardAllocation).where(
                AIGameRewardAllocation.session_id == session_id,
            )
        )).scalar_one()

    assert root == ("finished", "expired", None, None, None, None)
    assert settlement == ("void", "expired")
    assert (turns, allocations) == (0, 0)


async def test_pg_question_completion_after_root_lock_expiry_cannot_append(
    pg_sessions, monkeypatch,
):
    """Without a post-lock expiry check, a preflighted completion appends late."""
    started_at = datetime(2034, 1, 3, 12, 0)
    deadline = started_at + timedelta(seconds=60)
    clock = {"now": deadline - timedelta(seconds=1)}
    monkeypatch.setattr(ai_game_service, "_now", lambda: clock["now"])
    session_id = await _setup_running(
        pg_sessions,
        monkeypatch,
        turns=(),
        now=started_at,
        duration_seconds=60,
    )
    async with pg_sessions() as setup:
        started = await ai_game_service.begin_question(
            setup,
            session_id=session_id,
            user_tg_id=10,
            question="La completion ha oltrepassato il lock?",
        )
        await setup.commit()
    assert started.claim is not None

    async with pg_sessions() as locker, pg_sessions() as worker:
        root_locked = asyncio.Event()
        worker_reached_append_lock = asyncio.Event()
        release_root = asyncio.Event()
        original_execute = worker.execute

        async def hold_root() -> None:
            await locker.execute(select(AIGameSession.id).where(
                AIGameSession.id == session_id,
            ).with_for_update())
            root_locked.set()
            await asyncio.wait_for(release_root.wait(), timeout=3)
            await locker.commit()

        async def observe_append_lock(statement, *args, **kwargs):
            table = getattr(statement, "table", None)
            waits_for_root = (
                getattr(statement, "is_select", False)
                and getattr(statement, "_for_update_arg", None) is not None
            ) or (
                getattr(statement, "is_update", False)
                and getattr(table, "name", None) == AIGameSession.__tablename__
            )
            if waits_for_root and not worker_reached_append_lock.is_set():
                worker_reached_append_lock.set()
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(worker, "execute", observe_append_lock)
        action_task: list[asyncio.Task[Any] | None] = [None]
        lock_task = asyncio.create_task(hold_root())
        async with _managed_root_lock_race(
            release_root, lock_task, action_task, (locker, worker),
        ):
            await asyncio.wait_for(root_locked.wait(), timeout=3)
            action_task[0] = asyncio.create_task(ai_game_service.complete_question(
                worker,
                claim=started.claim,
                verdict=QuestionVerdict.si,
            ))
            await asyncio.wait_for(worker_reached_append_lock.wait(), timeout=3)
            clock["now"] = deadline + timedelta(seconds=1)
            release_root.set()
            result = await asyncio.wait_for(action_task[0], timeout=5)
            await worker.commit()

    assert (result.outcome, result.reason) == (
        TurnOutcome.rejected, TurnRejectReason.expired,
    )
    assert result.terminal is not None and result.terminal.finish_reason is FinishReason.expired
    async with pg_sessions() as observe:
        root = (await observe.execute(select(
            AIGameSession.status,
            AIGameSession.finish_reason,
            AIGameSession.pending_token,
            AIGameSession.pending_since,
            AIGameSession.pending_user_tg_id,
            AIGameSession.pending_kind,
        ).where(AIGameSession.id == session_id))).one()
        settlement = (await observe.execute(select(
            AIGameRewardSettlement.status,
            AIGameRewardSettlement.finish_reason,
        ).where(AIGameRewardSettlement.session_id == session_id))).one()
        turns = (await observe.execute(select(func.count()).select_from(AIGameTurn).where(
            AIGameTurn.session_id == session_id,
        ))).scalar_one()

    assert root == ("finished", "expired", None, None, None, None)
    assert settlement == ("void", "expired")
    assert turns == 0


async def test_pg_wrong_guess_after_root_lock_expiry_cannot_append(
    pg_sessions, monkeypatch,
):
    """Without a post-lock expiry check, a wrong guess is appended after deadline."""
    started_at = datetime(2034, 1, 4, 12, 0)
    deadline = started_at + timedelta(seconds=60)
    clock = {"now": deadline - timedelta(seconds=1)}
    monkeypatch.setattr(ai_game_service, "_now", lambda: clock["now"])
    session_id = await _setup_running(
        pg_sessions,
        monkeypatch,
        turns=(),
        now=started_at,
        duration_seconds=60,
    )

    async with pg_sessions() as locker, pg_sessions() as worker:
        root_locked = asyncio.Event()
        worker_reached_claim_lock = asyncio.Event()
        release_root = asyncio.Event()
        original_execute = worker.execute

        async def hold_root() -> None:
            await locker.execute(select(AIGameSession.id).where(
                AIGameSession.id == session_id,
            ).with_for_update())
            root_locked.set()
            await asyncio.wait_for(release_root.wait(), timeout=3)
            await locker.commit()

        async def observe_claim_lock(statement, *args, **kwargs):
            if (
                getattr(statement, "is_select", False)
                and getattr(statement, "_for_update_arg", None) is not None
                and not worker_reached_claim_lock.is_set()
            ):
                worker_reached_claim_lock.set()
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(worker, "execute", observe_claim_lock)
        action_task: list[asyncio.Task[Any] | None] = [None]
        lock_task = asyncio.create_task(hold_root())
        async with _managed_root_lock_race(
            release_root, lock_task, action_task, (locker, worker),
        ):
            await asyncio.wait_for(root_locked.wait(), timeout=3)
            action_task[0] = asyncio.create_task(ai_game_service.submit_guess(
                worker,
                session_id=session_id,
                user_tg_id=10,
                answer="Half-Life 2",
            ))
            await asyncio.wait_for(worker_reached_claim_lock.wait(), timeout=3)
            clock["now"] = deadline + timedelta(seconds=1)
            release_root.set()
            result = await asyncio.wait_for(action_task[0], timeout=5)
            await worker.commit()

    assert (result.outcome, result.reason) == (
        TurnOutcome.rejected, TurnRejectReason.expired,
    )
    assert result.terminal is not None and result.terminal.finish_reason is FinishReason.expired
    async with pg_sessions() as observe:
        root = (await observe.execute(select(
            AIGameSession.status,
            AIGameSession.finish_reason,
            AIGameSession.pending_token,
            AIGameSession.pending_since,
            AIGameSession.pending_user_tg_id,
            AIGameSession.pending_kind,
        ).where(AIGameSession.id == session_id))).one()
        settlement = (await observe.execute(select(
            AIGameRewardSettlement.status,
            AIGameRewardSettlement.finish_reason,
        ).where(AIGameRewardSettlement.session_id == session_id))).one()
        turns = (await observe.execute(select(func.count()).select_from(AIGameTurn).where(
            AIGameTurn.session_id == session_id,
        ))).scalar_one()

    assert root == ("finished", "expired", None, None, None, None)
    assert settlement == ("void", "expired")
    assert turns == 0


async def test_pg_winning_guess_after_root_lock_expiry_cannot_settle_victory(
    pg_sessions, monkeypatch,
):
    """Without a post-lock expiry check, a late alias guess pays a victory."""
    started_at = datetime(2034, 1, 5, 12, 0)
    deadline = started_at + timedelta(seconds=60)
    clock = {"now": deadline - timedelta(seconds=1)}
    monkeypatch.setattr(ai_game_service, "_now", lambda: clock["now"])
    session_id = await _setup_running(
        pg_sessions,
        monkeypatch,
        turns=(),
        now=started_at,
        duration_seconds=60,
    )

    async with pg_sessions() as locker, pg_sessions() as worker:
        root_locked = asyncio.Event()
        worker_reached_claim_lock = asyncio.Event()
        release_root = asyncio.Event()
        original_execute = worker.execute

        async def hold_root() -> None:
            await locker.execute(select(AIGameSession.id).where(
                AIGameSession.id == session_id,
            ).with_for_update())
            root_locked.set()
            await asyncio.wait_for(release_root.wait(), timeout=3)
            await locker.commit()

        async def observe_claim_lock(statement, *args, **kwargs):
            if (
                getattr(statement, "is_select", False)
                and getattr(statement, "_for_update_arg", None) is not None
                and not worker_reached_claim_lock.is_set()
            ):
                worker_reached_claim_lock.set()
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(worker, "execute", observe_claim_lock)
        action_task: list[asyncio.Task[Any] | None] = [None]
        lock_task = asyncio.create_task(hold_root())
        async with _managed_root_lock_race(
            release_root, lock_task, action_task, (locker, worker),
        ):
            await asyncio.wait_for(root_locked.wait(), timeout=3)
            action_task[0] = asyncio.create_task(ai_game_service.submit_guess(
                worker,
                session_id=session_id,
                user_tg_id=10,
                answer="portal two",
            ))
            await asyncio.wait_for(worker_reached_claim_lock.wait(), timeout=3)
            clock["now"] = deadline + timedelta(seconds=1)
            release_root.set()
            result = await asyncio.wait_for(action_task[0], timeout=5)
            await worker.commit()

    assert (result.outcome, result.reason) == (
        TurnOutcome.rejected, TurnRejectReason.expired,
    )
    assert result.terminal is not None and result.terminal.finish_reason is FinishReason.expired
    async with pg_sessions() as observe:
        root = (await observe.execute(select(
            AIGameSession.status,
            AIGameSession.finish_reason,
            AIGameSession.pending_token,
            AIGameSession.pending_since,
            AIGameSession.pending_user_tg_id,
            AIGameSession.pending_kind,
        ).where(AIGameSession.id == session_id))).one()
        settlement = (await observe.execute(select(
            AIGameRewardSettlement.status,
            AIGameRewardSettlement.finish_reason,
        ).where(AIGameRewardSettlement.session_id == session_id))).one()
        turns = (await observe.execute(select(func.count()).select_from(AIGameTurn).where(
            AIGameTurn.session_id == session_id,
        ))).scalar_one()
        allocations = (await observe.execute(
            select(func.count()).select_from(AIGameRewardAllocation).where(
                AIGameRewardAllocation.session_id == session_id,
            )
        )).scalar_one()
        ledger = (await observe.execute(
            select(func.count()).select_from(LedgerEntry)
        )).scalar_one()

    assert root == ("finished", "expired", None, None, None, None)
    assert settlement == ("void", "expired")
    assert (turns, allocations, ledger) == (0, 0, 0)


async def test_pg_fifth_question_quota_claims_exactly_once(pg_sessions, monkeypatch):
    """Two workers at question five must not both pass a stale Python quota read."""
    turns = tuple(
        (10, TurnKind.question.value, f"Historic question {index}?", '{"verdetto":"si"}')
        for index in range(1, 5)
    )
    session_id = await _setup_running(pg_sessions, monkeypatch, turns=turns)

    async with pg_sessions() as first, pg_sessions() as second:
        _synchronize_first_turn_claim(monkeypatch, (first, second))

        async def claim_one(db_session, question: str):
            result = await ai_game_service.begin_question(
                db_session,
                session_id=session_id,
                user_tg_id=10,
                question=question,
            )
            await db_session.commit()
            return result

        first_result, second_result = await _run_actor_race(
            (
                asyncio.create_task(
                    claim_one(first, "Is the fifth question accepted first?"),
                ),
                asyncio.create_task(
                    claim_one(second, "Can another fifth question slip through?"),
                ),
            ),
            (first, second),
        )

    results = (first_result, second_result)
    assert sum(result.outcome is TurnOutcome.claimed for result in results) == 1
    assert sum(result.reason is TurnRejectReason.busy for result in results) == 1
    claim = next(result.claim for result in results if result.claim is not None)
    async with pg_sessions() as complete:
        recorded = await ai_game_service.complete_question(
            complete, claim=claim, verdict=QuestionVerdict.si,
        )
        await complete.commit()
    assert recorded.outcome is TurnOutcome.recorded

    async with pg_sessions() as observe:
        quota = await ai_game_service.get_personal_quota(observe, session_id, 10)
        sixth = await ai_game_service.begin_question(
            observe,
            session_id=session_id,
            user_tg_id=10,
            question="Does a sixth question exceed the personal quota?",
        )
        ledger = (await observe.execute(
            select(func.count()).select_from(AIGameTurn).where(
                AIGameTurn.session_id == session_id,
            )
        )).scalar_one()
        projection = (await observe.execute(select(
            TwentyQuestionsGame.questions_used,
            TwentyQuestionsGame.guesses_used,
        ).where(TwentyQuestionsGame.session_id == session_id))).one()
        lease = (await observe.execute(select(
            AIGameSession.pending_token,
            AIGameSession.pending_since,
            AIGameSession.pending_user_tg_id,
            AIGameSession.pending_kind,
        ).where(AIGameSession.id == session_id))).one()
    assert (
        quota.questions_used,
        quota.questions_left,
        quota.guesses_used,
        quota.guesses_left,
        ledger,
        projection,
        lease,
    ) == (5, 0, 0, 2, 5, (5, 0), (None, None, None, None))
    assert (sixth.outcome, sixth.reason) == (
        TurnOutcome.rejected, TurnRejectReason.question_quota,
    )


async def test_pg_second_guess_quota_is_atomic_across_independent_sessions(
    pg_sessions, monkeypatch,
):
    """Two second guesses must not both append after reading one old ledger row."""
    session_id = await _setup_running(
        pg_sessions,
        monkeypatch,
        turns=((10, TurnKind.guess.value, "First wrong title", '{"correct":false}'),),
    )
    async with pg_sessions() as first, pg_sessions() as second:
        _synchronize_first_turn_claim(monkeypatch, (first, second))

        async def guess_one(db_session, answer: str):
            result = await ai_game_service.submit_guess(
                db_session,
                session_id=session_id,
                user_tg_id=10,
                answer=answer,
            )
            await db_session.commit()
            return result

        first_result, second_result = await _run_actor_race(
            (
                asyncio.create_task(guess_one(first, "Second wrong title one")),
                asyncio.create_task(guess_one(second, "Second wrong title two")),
            ),
            (first, second),
        )

    assert sum(result.outcome is TurnOutcome.recorded for result in (
        first_result, second_result,
    )) == 1
    rejected = next(result for result in (first_result, second_result)
                    if result.outcome is TurnOutcome.rejected)
    assert rejected.reason is TurnRejectReason.guess_quota
    async with pg_sessions() as observe:
        quota = await ai_game_service.get_personal_quota(observe, session_id, 10)
        guess_ledger = (await observe.execute(select(func.count()).select_from(AIGameTurn).where(
            AIGameTurn.session_id == session_id,
            AIGameTurn.kind == TurnKind.guess.value,
        ))).scalar_one()
        ledger = (await observe.execute(
            select(func.count()).select_from(AIGameTurn).where(
                AIGameTurn.session_id == session_id,
            )
        )).scalar_one()
        projection = (await observe.execute(select(
            TwentyQuestionsGame.questions_used,
            TwentyQuestionsGame.guesses_used,
        ).where(TwentyQuestionsGame.session_id == session_id))).one()
        lease = (await observe.execute(select(
            AIGameSession.pending_token,
            AIGameSession.pending_since,
            AIGameSession.pending_user_tg_id,
            AIGameSession.pending_kind,
        ).where(AIGameSession.id == session_id))).one()
    assert (
        quota.questions_used,
        quota.questions_left,
        quota.guesses_used,
        quota.guesses_left,
        ledger,
        guess_ledger,
        projection,
        lease,
    ) == (0, 5, 2, 0, 2, 2, (0, 2), (None, None, None, None))


async def test_pg_duplicate_guess_hash_race_returns_free_typed_rejection(
    pg_sessions, monkeypatch,
):
    """A unique-index race must not poison the transaction or consume a second guess."""
    session_id = await _setup_running(pg_sessions, monkeypatch, turns=())
    async with pg_sessions() as first, pg_sessions() as second:
        _synchronize_first_turn_claim(monkeypatch, (first, second))

        async def same_guess(db_session, answer: str):
            result = await ai_game_service.submit_guess(
                db_session,
                session_id=session_id,
                user_tg_id=10,
                answer=answer,
            )
            await db_session.commit()
            return result

        first_result, second_result = await _run_actor_race(
            (
                asyncio.create_task(same_guess(first, "The identical wrong title")),
                asyncio.create_task(
                    same_guess(second, " the  identical wrong title! "),
                ),
            ),
            (first, second),
        )

    assert sum(result.outcome is TurnOutcome.recorded for result in (
        first_result, second_result,
    )) == 1
    duplicate = next(result for result in (first_result, second_result)
                     if result.outcome is TurnOutcome.rejected)
    assert duplicate.reason is TurnRejectReason.duplicate_guess
    async with pg_sessions() as observe:
        quota = await ai_game_service.get_personal_quota(observe, session_id, 10)
        turns = (await observe.execute(select(AIGameTurn).where(
            AIGameTurn.session_id == session_id,
        ))).scalars().all()
        pending = (await observe.execute(select(
            AIGameSession.pending_token,
            AIGameSession.pending_since,
            AIGameSession.pending_user_tg_id,
            AIGameSession.pending_kind,
        ).where(AIGameSession.id == session_id))).one()
    assert (quota.guesses_used, quota.guesses_left, len(turns)) == (1, 1, 1)
    assert pending == (None, None, None, None)


async def test_pg_duplicate_question_hash_race_reuses_one_free_turn(
    pg_sessions, monkeypatch,
):
    """A normalized duplicate must leave one persisted question and later reuse it for free."""
    session_id = await _setup_running(pg_sessions, monkeypatch, turns=())
    async with pg_sessions() as first, pg_sessions() as second:
        _synchronize_first_turn_claim(monkeypatch, (first, second))

        async def start_one(db_session, question: str):
            result = await ai_game_service.begin_question(
                db_session,
                session_id=session_id,
                user_tg_id=10,
                question=question,
            )
            await db_session.commit()
            return result

        first_result, second_result = await _run_actor_race(
            (
                asyncio.create_task(start_one(first, "Il gioco usa portali?")),
                asyncio.create_task(start_one(second, "  il gioco   usa portali?! ")),
            ),
            (first, second),
        )

    assert sum(result.outcome is TurnOutcome.claimed for result in (
        first_result, second_result,
    )) == 1
    assert sum(result.reason is TurnRejectReason.busy for result in (
        first_result, second_result,
    )) == 1
    claim = next(result.claim for result in (first_result, second_result)
                 if result.claim is not None)
    async with pg_sessions() as complete:
        recorded = await ai_game_service.complete_question(
            complete, claim=claim, verdict=QuestionVerdict.si,
        )
        await complete.commit()
    assert recorded.outcome is TurnOutcome.recorded

    async with pg_sessions() as reuse:
        reused = await ai_game_service.begin_question(
            reuse,
            session_id=session_id,
            user_tg_id=10,
            question="IL GIOCO USA PORTALI.",
        )
        await reuse.commit()
    assert (reused.outcome, reused.cached_verdict) == (
        TurnOutcome.reused, QuestionVerdict.si,
    )
    assert (reused.quota.questions_used, reused.quota.questions_left) == (1, 4)

    async with pg_sessions() as observe:
        turns = (await observe.execute(select(func.count()).select_from(AIGameTurn).where(
            AIGameTurn.session_id == session_id,
            AIGameTurn.kind == TurnKind.question.value,
        ))).scalar_one()
        pending = (await observe.execute(select(
            AIGameSession.pending_token,
            AIGameSession.pending_since,
            AIGameSession.pending_user_tg_id,
            AIGameSession.pending_kind,
        ).where(AIGameSession.id == session_id))).one()
    assert (turns, pending) == (1, (None, None, None, None))


async def test_pg_completion_after_lease_recovery_cannot_append_stale_turn(
    pg_sessions, monkeypatch,
):
    """The old token must lose after another connection replaces every lease field."""
    monkeypatch.setattr(ai_game_service.settings, "ai_game_claim_timeout_seconds", 45)
    now = datetime(2026, 8, 23, 12, 0)
    session_id = await _setup_running(pg_sessions, monkeypatch, turns=(), now=now)
    async with pg_sessions() as original:
        first = await ai_game_service.begin_question(
            original,
            session_id=session_id,
            user_tg_id=10,
            question="Can the first lease become stale?",
            now=now,
        )
        await original.commit()
    assert first.claim is not None

    async with pg_sessions() as replacement:
        second = await ai_game_service.begin_question(
            replacement,
            session_id=session_id,
            user_tg_id=20,
            question="Can a replacement lease take ownership?",
            now=now.replace(second=46),
        )
        await replacement.commit()
    assert second.claim is not None

    async with pg_sessions() as stale_worker:
        stale = await ai_game_service.complete_question(
            stale_worker,
            claim=first.claim,
            verdict=QuestionVerdict.si,
            now=now + timedelta(seconds=47),
        )
        await stale_worker.commit()
    assert (stale.outcome, stale.reason) == (
        TurnOutcome.rejected, TurnRejectReason.lost_claim,
    )
    async with pg_sessions() as observe:
        fields = (await observe.execute(select(
            AIGameSession.pending_token,
            AIGameSession.pending_since,
            AIGameSession.pending_user_tg_id,
            AIGameSession.pending_kind,
        ).where(AIGameSession.id == session_id))).one()
        turns = (await observe.execute(select(func.count()).select_from(AIGameTurn).where(
            AIGameTurn.session_id == session_id,
        ))).scalar_one()
        original_quota = await ai_game_service.get_personal_quota(observe, session_id, 10)
        replacement_quota = await ai_game_service.get_personal_quota(observe, session_id, 20)
    assert fields == (
        second.claim.token,
        now + timedelta(seconds=46),
        20,
        TurnKind.question.value,
    )
    assert turns == 0
    assert (original_quota.questions_used, replacement_quota.questions_used) == (0, 0)


async def test_pg_completion_after_admin_close_keeps_stale_lease_inert(
    pg_sessions, monkeypatch,
):
    """A provider completion after authoritative closure cannot append or consume quota."""
    session_id = await _setup_running(pg_sessions, monkeypatch, turns=())
    async with pg_sessions() as claimant:
        started = await ai_game_service.begin_question(
            claimant,
            session_id=session_id,
            user_tg_id=10,
            question="La completion sopravvive alla chiusura?",
        )
        await claimant.commit()
    assert started.claim is not None

    async with pg_sessions() as closer:
        closed = await ai_game_service.terminalize(
            closer,
            session_id=session_id,
            reason=FinishReason.admin_closed,
        )
        await closer.commit()
    assert closed.transitioned and closed.finish_reason is FinishReason.admin_closed

    async with pg_sessions() as stale_worker:
        stale = await ai_game_service.complete_question(
            stale_worker,
            claim=started.claim,
            verdict=QuestionVerdict.si,
        )
        await stale_worker.commit()
    assert (stale.outcome, stale.reason) == (
        TurnOutcome.rejected, TurnRejectReason.lost_claim,
    )
    assert (stale.quota.questions_used, stale.quota.questions_left) == (0, 5)

    async with pg_sessions() as observe:
        root = (await observe.execute(select(
            AIGameSession.status,
            AIGameSession.finish_reason,
            AIGameSession.pending_token,
            AIGameSession.pending_since,
            AIGameSession.pending_user_tg_id,
            AIGameSession.pending_kind,
        ).where(AIGameSession.id == session_id))).one()
        settlement = (await observe.execute(select(
            AIGameRewardSettlement.status,
            AIGameRewardSettlement.finish_reason,
        ).where(AIGameRewardSettlement.session_id == session_id))).one()
        turns = (await observe.execute(select(func.count()).select_from(AIGameTurn).where(
            AIGameTurn.session_id == session_id,
        ))).scalar_one()
    assert root == ("finished", "admin_closed", None, None, None, None)
    assert settlement == ("void", "admin_closed")
    assert turns == 0


async def test_two_correct_aliases_create_one_winner_and_one_settlement(
    pg_sessions, monkeypatch,
):
    """Concurrent locally-correct aliases must settle one winner, one turn, and one payout."""
    session_id = await _setup_running(pg_sessions, monkeypatch, turns=())
    async with pg_sessions() as first, pg_sessions() as second:
        _synchronize_first_turn_claim(monkeypatch, (first, second))

        async def answer(db_session, user_tg_id: int, title: str):
            result = await ai_game_service.submit_guess(
                db_session,
                session_id=session_id,
                user_tg_id=user_tg_id,
                answer=title,
            )
            await db_session.commit()
            return result

        first_result, second_result = await _run_actor_race(
            (
                asyncio.create_task(answer(first, 10, "Portal 2")),
                asyncio.create_task(answer(second, 20, "portal two")),
            ),
            (first, second),
        )

    winner_result = next(result for result in (first_result, second_result)
                         if result.outcome is TurnOutcome.recorded)
    loser_result = next(result for result in (first_result, second_result)
                        if result.outcome is TurnOutcome.rejected)
    assert winner_result.correct is True
    assert loser_result.reason in (TurnRejectReason.closed, TurnRejectReason.lost_claim)
    facts = await _settlement_facts(pg_sessions, session_id)
    assert facts["root"] == ("finished", "victory", 2)
    assert facts["winner"] == winner_result.terminal.winner_tg_id
    assert facts["settlement"][:5] == ("settled", "victory", 1, 0, 0)
    assert facts["allocations"] == facts["ledger"] == facts["turns"] == 1
    winner_tg_id = winner_result.terminal.winner_tg_id
    assert facts["wallets"] == {
        10: 100 if winner_tg_id == 10 else 0,
        20: 100 if winner_tg_id == 20 else 0,
    }
    assert facts["xp"] == {
        10: 10 if winner_tg_id == 10 else 0,
        20: 10 if winner_tg_id == 20 else 0,
    }
    async with pg_sessions() as observe:
        allocation_coins, allocation_xp = (await observe.execute(select(
            func.sum(AIGameRewardAllocation.coins),
            func.sum(AIGameRewardAllocation.xp),
        ).where(AIGameRewardAllocation.session_id == session_id))).one()
        ledger_coins = (await observe.execute(select(
            func.sum(LedgerEntry.amount),
        ).where(
            LedgerEntry.description == f"Premio gioco segreto di Alduino #{session_id}",
        ))).scalar_one()
    assert (allocation_coins, ledger_coins, allocation_xp) == (100, 100, 10)
