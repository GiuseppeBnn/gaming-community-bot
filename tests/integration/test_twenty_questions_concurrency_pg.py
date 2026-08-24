"""Real-PostgreSQL races for Alduino terminal settlement."""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime

import pytest
from sqlalchemy import func, select, update

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
from services.ai_game_types import FinishReason
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


async def _setup_running(
    pg_sessions,
    monkeypatch,
    *,
    turns: tuple[tuple[int, str, str, str], ...] = _BASE_TURNS + (_WINNING_TURN,),
) -> int:
    monkeypatch.setattr(ai_game_service.settings, "twentyq_v2_enabled", True)
    monkeypatch.setattr(
        ai_game_service, "has_configured_twenty_questions_provider", lambda: True,
    )
    async with pg_sessions() as setup:
        for tg_id in (10, 20):
            setup.add(User(tg_id=tg_id, full_name=f"User {tg_id}"))
            setup.add(Wallet(tg_id=tg_id, coins=0))
        created = await ai_game_service.create_twenty_questions(
            setup,
            creator_tg_id=9,
            title="Concorrenza",
            duration_seconds=43_200,
            expires_at=None,
            max_coins_per_participant=100,
            target=TARGET,
        )
        started = await ai_game_service.start(setup, created.session_id, group_id=-1001)
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

        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(run(first), run(second)),
            timeout=10,
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


async def test_pg_terminalize_victory_vs_expired_persists_one_reason_and_one_reward_set(
    pg_sessions, monkeypatch,
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

        async def run_expired():
            result = await rewards.terminalize(
                expirer,
                session_id=session_id,
                reason=FinishReason.expired,
            )
            await expirer.commit()
            return result

        victory, expired = await asyncio.wait_for(
            asyncio.gather(run_victory(), run_expired()),
            timeout=10,
        )

    assert sorted(result.transitioned for result in (victory, expired)) == [False, True]
    assert victory.finish_reason is expired.finish_reason
    assert victory.finish_reason in (FinishReason.victory, FinishReason.expired)
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
            ("settled", "expired", 2, 2, 1, 200, 32, 168, 0, 84, 0),
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

        first_replay, second_replay = await asyncio.wait_for(
            asyncio.gather(replay(one), replay(two)),
            timeout=10,
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
            await observer_checked.wait()
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
            await observer_checked.wait()
            return result

        monkeypatch.setattr(rewards.xp_service, "grant_xp", fail_second)

    async def fail_and_rollback():
        async with pg_sessions() as failing:
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
        await first_write.wait()
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
        await rolled_back.wait()
        after = await _settlement_facts(pg_sessions, session_id)
        assert after == before

    await asyncio.wait_for(
        asyncio.gather(fail_and_rollback(), observe_uncommitted_then_rolled_back()),
        timeout=10,
    )


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
