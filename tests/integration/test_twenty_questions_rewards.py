"""Terminal reward contracts for Alduino's v2 secret game."""

from __future__ import annotations

import importlib
from datetime import datetime
from typing import cast

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import (
    AIGameRewardAllocation,
    AIGameRewardSettlement,
    AIGameSession,
    AIGameTurn,
    LedgerEntry,
    ScheduledTask,
    TransactionType,
    TwentyQuestionsGame,
    User,
    Wallet,
)
from services import ai_game_service
from services.ai_game_types import FinishReason, SettlementFinishReason, TerminalAllocation
from services.twenty_questions_catalog import GameDossier
from services.xp_service import XpSource
from utils import daytime


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
    """Import at test execution so the first RED is a failed behavior, not collection."""
    return importlib.import_module("services.ai_game_rewards")


async def _running_game(
    session,
    monkeypatch,
    *,
    users: tuple[int, ...] = (10, 20),
    turns: tuple[tuple[int, str, str, str], ...] = _BASE_TURNS + (_WINNING_TURN,),
    max_coins_per_participant: int = 100,
) -> int:
    monkeypatch.setattr(ai_game_service.settings, "twentyq_v2_enabled", True)
    monkeypatch.setattr(
        ai_game_service, "has_configured_twenty_questions_provider", lambda: True,
    )
    for tg_id in users:
        session.add(User(tg_id=tg_id, full_name=f"User {tg_id}"))
        session.add(Wallet(tg_id=tg_id, coins=0))
    created = await ai_game_service.create_twenty_questions(
        session,
        creator_tg_id=9,
        title="Premi",
        duration_seconds=43_200,
        expires_at=None,
        max_coins_per_participant=max_coins_per_participant,
        target=TARGET,
    )
    started = await ai_game_service.start(session, created.session_id, group_id=-1001)
    assert started.started
    session.add_all([
        AIGameTurn(
            session_id=created.session_id,
            turn_no=turn_no,
            user_tg_id=user_tg_id,
            kind=kind,
            input_text=input_text,
            output_json=output_json,
            normalized_input_hash=f"{turn_no:064x}",
        )
        for turn_no, (user_tg_id, kind, input_text, output_json) in enumerate(turns, start=1)
    ])
    await session.execute(
        update(AIGameSession)
        .where(AIGameSession.id == created.session_id)
        .values(next_turn_no=len(turns) + 1)
    )
    # The terminalizer deliberately consumes only turns the caller has already
    # persisted in this transaction, including an eventual winning guess.
    await session.flush()
    return created.session_id


async def _add_winning_turn(session, session_id: int) -> None:
    """Add the caller-owned winning turn after a committed running baseline."""
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


async def _counts(session, session_id: int) -> tuple[int, int, int]:
    allocations = (await session.execute(
        select(func.count()).select_from(AIGameRewardAllocation).where(
            AIGameRewardAllocation.session_id == session_id,
        )
    )).scalar_one()
    ledger = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one()
    turns = (await session.execute(
        select(func.count()).select_from(AIGameTurn).where(AIGameTurn.session_id == session_id)
    )).scalar_one()
    return int(allocations), int(ledger), int(turns)


async def _wallets_and_xp(session, users: tuple[int, ...]) -> tuple[dict[int, int], dict[int, int]]:
    wallets = dict((await session.execute(
        select(Wallet.tg_id, Wallet.coins).where(Wallet.tg_id.in_(users))
    )).all())
    xp = dict((await session.execute(
        select(User.tg_id, User.xp).where(User.tg_id.in_(users))
    )).all())
    return wallets, xp


async def _assert_unsettled_terminal_footprint(session, session_id: int) -> None:
    root = (await session.execute(select(
        AIGameSession.status,
        AIGameSession.finish_reason,
        AIGameSession.finished_at,
        AIGameSession.pending_token,
        AIGameSession.pending_since,
        AIGameSession.pending_user_tg_id,
        AIGameSession.pending_kind,
        AIGameSession.next_turn_no,
    ).where(AIGameSession.id == session_id))).one()
    assert root == ("running", None, None, None, None, None, None, 4)
    assert (await session.execute(select(TwentyQuestionsGame.winner_tg_id).where(
        TwentyQuestionsGame.session_id == session_id,
    ))).scalar_one() is None
    settlement = (await session.execute(select(
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
        AIGameRewardSettlement.settled_at,
    ).where(AIGameRewardSettlement.session_id == session_id))).one()
    assert settlement == ("pending", None, 0, 0, 0, 0, 0, 0, 0, 0, 0, None)
    assert await _counts(session, session_id) == (0, 0, 3)
    wallets, xp = await _wallets_and_xp(session, (10, 20))
    assert wallets == {10: 0, 20: 0}
    assert xp == {10: 0, 20: 0}


async def test_victory_pays_equal_coins_and_uncapped_xp_once(session, monkeypatch):
    """Would fail if terminal CAS, equal rewards, or replay reconstruction regressed."""
    rewards = _rewards()
    session_id = await _running_game(session, monkeypatch)
    distractor_session_id = await _running_game(
        session,
        monkeypatch,
        users=(30,),
        turns=(
            (30, "question", "Domanda estranea?", '{"verdetto":"si"}'),
            (30, "guess", "Half-Life 2", '{"correct":false}'),
        ),
    )
    await session.execute(
        update(User)
        .where(User.tg_id.in_((10, 20)))
        .values(
            xp_today=1_000_000,
            xp_today_date=daytime.local_today().isoformat(),
        )
    )
    original_grant = rewards.xp_service.grant_xp

    async def terminal_xp_grant(session, tg_id, amount, source, *, capped):
        assert source is XpSource.twentyq
        assert not capped
        return await original_grant(session, tg_id, amount, source, capped=capped)

    monkeypatch.setattr(rewards.xp_service, "grant_xp", terminal_xp_grant)
    now = datetime(2026, 8, 23, 12, 0)

    result = await rewards.terminalize(
        session,
        session_id=session_id,
        reason=FinishReason.victory,
        winner_tg_id=10,
        now=now,
    )
    await session.commit()

    assert result.transitioned
    assert (result.session_id, result.finish_reason, result.group_id, result.title) == (
        session_id,
        FinishReason.victory,
        -1001,
        "Premi",
    )
    assert (result.answer, result.winner_tg_id, result.anchor_message_id) == ("Portal 2", 10, None)
    assert result.reward.settlement_status == "settled"
    assert (
        result.reward.participant_count,
        result.reward.question_count,
        result.reward.wrong_guess_count,
        result.reward.base_amount,
        result.reward.penalty_amount,
        result.reward.computed_pool,
        result.reward.paid_pool,
        result.reward.share,
        result.reward.remainder,
    ) == (2, 2, 1, 200, 32, 168, 168, 84, 0)
    assert result.allocations == (
        TerminalAllocation(user_tg_id=10, coins=84, xp=10),
        TerminalAllocation(user_tg_id=20, coins=84, xp=10),
    )
    root = (await session.execute(select(
        AIGameSession.status,
        AIGameSession.finish_reason,
        AIGameSession.finished_at,
        AIGameSession.pending_token,
        AIGameSession.pending_since,
        AIGameSession.pending_user_tg_id,
        AIGameSession.pending_kind,
    ).where(AIGameSession.id == session_id))).one()
    assert root == ("finished", "victory", now, None, None, None, None)
    assert (await session.execute(select(TwentyQuestionsGame.winner_tg_id).where(
        TwentyQuestionsGame.session_id == session_id,
    ))).scalar_one() == 10
    wallets, xp = await _wallets_and_xp(session, (10, 20, 30))
    assert wallets == {10: 84, 20: 84, 30: 0}
    assert xp == {10: 10, 20: 10, 30: 0}
    assert (await session.execute(select(func.count()).select_from(AIGameTurn).where(
        AIGameTurn.session_id == session_id,
    ))).scalar_one() == 4
    assert (await session.execute(select(func.count()).select_from(AIGameTurn).where(
        AIGameTurn.session_id == distractor_session_id,
    ))).scalar_one() == 2
    assert (await session.execute(select(func.count()).select_from(AIGameRewardAllocation).where(
        AIGameRewardAllocation.session_id == distractor_session_id,
    ))).scalar_one() == 0
    xp_today = dict((await session.execute(
        select(User.tg_id, User.xp_today).where(User.tg_id.in_((10, 20)))
    )).all())
    assert xp_today == {10: 1_000_000, 20: 1_000_000}
    ledger = (await session.execute(select(LedgerEntry).order_by(LedgerEntry.to_tg_id))).scalars().all()
    assert len(ledger) == 2
    assert all(row.tx_type == TransactionType.ai_game_reward.value for row in ledger)
    assert all(row.reference_id is None for row in ledger)

    replay = await rewards.terminalize(
        session,
        session_id=session_id,
        reason=FinishReason.victory,
        winner_tg_id=10,
    )
    await session.commit()
    assert not replay.transitioned
    assert replay.finish_reason is FinishReason.victory
    assert replay.reward == result.reward
    assert replay.allocations == result.allocations
    assert await _counts(session, session_id) == (2, 2, 4)


async def test_victory_records_pool_but_never_assigns_the_integer_remainder(session, monkeypatch):
    """Would fail if the last participant receives the remainder instead of an equal share."""
    rewards = _rewards()
    session_id = await _running_game(
        session,
        monkeypatch,
        users=(10, 20, 30),
        turns=_BASE_TURNS + ((30, "question", "Domanda 30?", '{"verdetto":"si"}'), _WINNING_TURN),
    )

    result = await rewards.terminalize(
        session,
        session_id=session_id,
        reason=FinishReason.victory,
        winner_tg_id=10,
    )
    await session.commit()

    assert (
        result.reward.computed_pool,
        result.reward.paid_pool,
        result.reward.share,
        result.reward.remainder,
    ) == (262, 262, 87, 1)
    assert {allocation.coins for allocation in result.allocations} == {87}
    assert sum(allocation.coins for allocation in result.allocations) == 261
    wallets, _ = await _wallets_and_xp(session, (10, 20, 30))
    assert wallets == {10: 87, 20: 87, 30: 87}


@pytest.mark.parametrize("reason", (FinishReason.expired, FinishReason.admin_closed))
async def test_non_victory_terminal_reasons_grant_only_participation_xp(
    session, monkeypatch, reason,
):
    """Would fail if expiry/administrative closure mints victory coins or drops participation XP."""
    rewards = _rewards()
    session_id = await _running_game(session, monkeypatch, turns=_BASE_TURNS)

    result = await rewards.terminalize(session, session_id=session_id, reason=reason)
    await session.commit()

    assert result.transitioned and result.finish_reason is reason
    assert result.reward.settlement_status == "settled"
    assert (
        result.reward.participant_count,
        result.reward.question_count,
        result.reward.wrong_guess_count,
        result.reward.computed_pool,
        result.reward.paid_pool,
        result.reward.share,
    ) == (2, 2, 1, 168, 0, 84)
    assert result.allocations == (
        TerminalAllocation(user_tg_id=10, coins=0, xp=10),
        TerminalAllocation(user_tg_id=20, coins=0, xp=10),
    )
    wallets, xp = await _wallets_and_xp(session, (10, 20))
    assert wallets == {10: 0, 20: 0}
    assert xp == {10: 10, 20: 10}
    assert (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one() == 0


async def test_empty_game_voids_without_running_the_reward_formula(session, monkeypatch):
    """Would fail if a no-participant terminalization calls policy math or emits a payout row."""
    rewards = _rewards()
    session_id = await _running_game(session, monkeypatch, users=(), turns=())
    now = datetime(2026, 8, 23, 12, 0)

    def formula_must_not_run(*args, **kwargs):
        raise AssertionError("empty terminalization must not compute a reward projection")

    monkeypatch.setattr(rewards, "compute_reward_projection", formula_must_not_run)
    result = await rewards.terminalize(
        session,
        session_id=session_id,
        reason=FinishReason.expired,
        now=now,
    )
    await session.commit()

    assert result.transitioned
    assert result.reward.settlement_status == "void"
    assert (
        result.reward.participant_count,
        result.reward.question_count,
        result.reward.wrong_guess_count,
        result.reward.base_amount,
        result.reward.penalty_amount,
        result.reward.computed_pool,
        result.reward.paid_pool,
        result.reward.share,
        result.reward.remainder,
    ) == (0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert result.allocations == ()
    root = (await session.execute(select(
        AIGameSession.status,
        AIGameSession.finish_reason,
        AIGameSession.finished_at,
    ).where(AIGameSession.id == session_id))).one()
    assert root == ("finished", "expired", now)
    settlement = (await session.execute(select(
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
        AIGameRewardSettlement.settled_at,
    ).where(AIGameRewardSettlement.session_id == session_id))).one()
    assert settlement == ("void", "expired", 0, 0, 0, 0, 0, 0, 0, 0, 0, now)
    assert await _counts(session, session_id) == (0, 0, 0)


async def test_zero_coin_share_still_records_uncapped_xp_allocation(session, monkeypatch):
    """Would fail if a zero-value victory allocation skips participation XP or calls credit(0)."""
    rewards = _rewards()
    session_id = await _running_game(
        session,
        monkeypatch,
        users=(10,),
        turns=((10, "guess", "Portal 2", '{"correct":true}'),),
        max_coins_per_participant=1,
    )
    await session.execute(
        update(AIGameRewardSettlement)
        .where(AIGameRewardSettlement.session_id == session_id)
        .values(max_coins_per_participant=0)
    )

    result = await rewards.terminalize(
        session,
        session_id=session_id,
        reason=FinishReason.victory,
        winner_tg_id=10,
    )
    await session.commit()

    assert result.reward.share == 0
    assert result.allocations == (TerminalAllocation(user_tg_id=10, coins=0, xp=10),)
    wallets, xp = await _wallets_and_xp(session, (10,))
    assert wallets == {10: 0}
    assert xp == {10: 10}
    assert (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one() == 0


async def test_legacy_finish_reason_is_rejected_before_any_mutation(session, engine, monkeypatch):
    """Would fail if a historical view-only reason could claim or settle a v2 game."""
    rewards = _rewards()
    session_id = await _running_game(session, monkeypatch, turns=_BASE_TURNS)
    await session.commit()

    with pytest.raises(ValueError):
        await rewards.terminalize(
            session,
            session_id=session_id,
            reason=cast(SettlementFinishReason, FinishReason.legacy),
        )

    assert not session.new
    assert not session.dirty
    await _assert_unsettled_terminal_footprint(session, session_id)
    await session.rollback()
    observer_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False,
    )
    async with observer_factory() as observer:
        await _assert_unsettled_terminal_footprint(observer, session_id)


@pytest.mark.parametrize("missing", ("user", "wallet"))
async def test_incomplete_participant_accounts_fail_before_payout_and_remain_retryable(
    session, monkeypatch, missing,
):
    """Would fail if a late missing account permits an earlier participant to be paid."""
    rewards = _rewards()
    users = (10,) if missing == "user" else (10, 20)
    session_id = await _running_game(session, monkeypatch, users=users)
    await session.commit()
    if missing == "wallet":
        await session.execute(delete(Wallet).where(Wallet.tg_id == 20))
        await session.commit()

    original_credit = rewards.economy_service.credit

    async def credit_must_not_run(*args, **kwargs):
        raise AssertionError("account validation must finish before the first credit")

    monkeypatch.setattr(rewards.economy_service, "credit", credit_must_not_run)
    with pytest.raises(rewards.RewardSettlementError):
        await rewards.terminalize(
            session,
            session_id=session_id,
            reason=FinishReason.victory,
            winner_tg_id=10,
        )
    await session.rollback()
    monkeypatch.setattr(rewards.economy_service, "credit", original_credit)

    root = (await session.execute(select(
        AIGameSession.status, AIGameSession.finish_reason,
    ).where(AIGameSession.id == session_id))).one()
    assert root == ("running", None)
    assert await _counts(session, session_id) == (0, 0, 4)
    existing_users = (10,) if missing == "user" else (10, 20)
    wallets, xp = await _wallets_and_xp(session, existing_users)
    assert wallets == {10: 0}
    assert xp == ({10: 0} if missing == "user" else {10: 0, 20: 0})

    if missing == "user":
        session.add(User(tg_id=20, full_name="User 20"))
        session.add(Wallet(tg_id=20, coins=0))
    else:
        session.add(Wallet(tg_id=20, coins=0))
    await session.flush()
    retried = await rewards.terminalize(
        session,
        session_id=session_id,
        reason=FinishReason.victory,
        winner_tg_id=10,
    )
    await session.commit()
    assert retried.transitioned
    wallets, xp = await _wallets_and_xp(session, (10, 20))
    assert wallets == {10: 84, 20: 84}
    assert xp == {10: 10, 20: 10}


@pytest.mark.parametrize("failure_at", ("credit", "grant"))
async def test_payout_failure_rolls_back_caller_owned_winning_turn_and_retries(
    session, monkeypatch, failure_at,
):
    """Would fail if a caught payout error leaves a terminal claim, turn, money, or XP behind."""
    rewards = _rewards()
    session_id = await _running_game(session, monkeypatch, turns=_BASE_TURNS)
    await session.commit()
    await _add_winning_turn(session, session_id)

    class PayoutFault(RuntimeError):
        pass

    calls = 0
    if failure_at == "credit":
        original = rewards.economy_service.credit

        async def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PayoutFault("second credit")
            return await original(*args, **kwargs)

        monkeypatch.setattr(rewards.economy_service, "credit", fail_second)
    else:
        original = rewards.xp_service.grant_xp

        async def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PayoutFault("second XP grant")
            return await original(*args, **kwargs)

        monkeypatch.setattr(rewards.xp_service, "grant_xp", fail_second)

    with pytest.raises(PayoutFault):
        await rewards.terminalize(
            session,
            session_id=session_id,
            reason=FinishReason.victory,
            winner_tg_id=10,
        )
    await session.rollback()

    root = (await session.execute(select(
        AIGameSession.status,
        AIGameSession.finish_reason,
        AIGameSession.next_turn_no,
    ).where(AIGameSession.id == session_id))).one()
    assert root == ("running", None, 4)
    assert (await session.execute(select(TwentyQuestionsGame.winner_tg_id).where(
        TwentyQuestionsGame.session_id == session_id,
    ))).scalar_one() is None
    assert await _counts(session, session_id) == (0, 0, 3)
    wallets, xp = await _wallets_and_xp(session, (10, 20))
    assert wallets == {10: 0, 20: 0}
    assert xp == {10: 0, 20: 0}

    if failure_at == "credit":
        monkeypatch.setattr(rewards.economy_service, "credit", original)
    else:
        monkeypatch.setattr(rewards.xp_service, "grant_xp", original)
    await _add_winning_turn(session, session_id)
    retry = await rewards.terminalize(
        session,
        session_id=session_id,
        reason=FinishReason.victory,
        winner_tg_id=10,
    )
    await session.commit()
    assert retry.transitioned
    assert await _counts(session, session_id) == (2, 2, 4)


async def test_game_service_exposes_terminalization_facade(session, monkeypatch):
    """Would fail if callers had to import the reward engine and reintroduced an import cycle."""
    session_id = await _running_game(session, monkeypatch, turns=_BASE_TURNS)

    result = await ai_game_service.terminalize(
        session,
        session_id=session_id,
        reason=FinishReason.expired,
    )

    assert result.transitioned
    assert result.finish_reason is FinishReason.expired


@pytest.mark.parametrize(
    ("reason", "winner_tg_id"),
    [
        (FinishReason.victory, 10),
        (FinishReason.expired, None),
        (FinishReason.admin_closed, None),
    ],
)
async def test_terminalization_cancels_only_pending_tasks_for_its_game(
    session, monkeypatch, reason, winner_tg_id,
):
    """Without this cancellation, stale start/close/expiry tasks can mutate a finished game."""
    session_id = await _running_game(session, monkeypatch)
    now = datetime(2026, 8, 23, 12, 0)
    initial_expiry_id = (await session.execute(
        select(ScheduledTask.id).where(
            ScheduledTask.task_type == "twentyq",
            ScheduledTask.ref_id == session_id,
            ScheduledTask.status == "pending",
        )
    )).scalar_one()
    own_expiry = ScheduledTask(
        task_type="twentyq", ref_id=session_id, run_at=now, status="pending",
        created_by_tg_id=9, payload_json='{"internal":true,"action":"expire"}',
    )
    own_close = ScheduledTask(
        task_type="twentyq", ref_id=session_id, run_at=now, status="pending",
        created_by_tg_id=9, payload_json='{"action":"close"}',
    )
    other_game = ScheduledTask(
        task_type="twentyq", ref_id=session_id + 1, run_at=now, status="pending",
        created_by_tg_id=9, payload_json='{"internal":true,"action":"expire"}',
    )
    other_type = ScheduledTask(
        task_type="quiz", ref_id=session_id, run_at=now, status="pending",
        created_by_tg_id=9, payload_json='{"action":"close"}',
    )
    session.add_all((own_expiry, own_close, other_game, other_type))
    await session.flush()

    result = await ai_game_service.terminalize(
        session, session_id=session_id, reason=reason, winner_tg_id=winner_tg_id, now=now,
    )
    await session.commit()

    own_ids = (initial_expiry_id, own_expiry.id, own_close.id, other_game.id, other_type.id)
    statuses = dict((await session.execute(
        select(ScheduledTask.id, ScheduledTask.status).where(ScheduledTask.id.in_(own_ids))
    )).all())
    assert result.transitioned
    assert statuses == {
        initial_expiry_id: "cancelled",
        own_expiry.id: "cancelled",
        own_close.id: "cancelled",
        other_game.id: "pending",
        other_type.id: "pending",
    }


async def test_terminalization_rollback_restores_pending_task_cancellation(session, monkeypatch):
    """Task cancellation is part of settlement atomicity, not cleanup after a commit."""
    rewards = _rewards()
    session_id = await _running_game(session, monkeypatch)
    expiry = ScheduledTask(
        task_type="twentyq", ref_id=session_id, run_at=datetime(2026, 8, 23, 12, 0),
        status="pending", created_by_tg_id=9,
        payload_json='{"internal":true,"action":"expire"}',
    )
    session.add(expiry)
    await session.commit()
    expiry_id = expiry.id
    cancellations = []
    original_cancel = rewards.schedule_service.cancel_pending_for_ref

    async def capture_cancel(*args, **kwargs):
        cancelled = await original_cancel(*args, **kwargs)
        cancellations.append(cancelled)
        return cancelled

    async def fail_credit(*args, **kwargs):
        raise RuntimeError("credit fails after terminal claim")

    monkeypatch.setattr(rewards.schedule_service, "cancel_pending_for_ref", capture_cancel)
    monkeypatch.setattr(rewards.economy_service, "credit", fail_credit)
    with pytest.raises(RuntimeError, match="credit fails"):
        await ai_game_service.terminalize(
            session, session_id=session_id, reason=FinishReason.victory, winner_tg_id=10,
        )
    await session.rollback()

    stored = await session.get(ScheduledTask, expiry_id)
    assert cancellations == [2]
    assert stored is not None and stored.status == "pending"


async def test_terminalization_replay_does_not_recancel_tasks(session, monkeypatch):
    """A losing terminal CAS is an immutable replay, never a second cleanup pass."""
    session_id = await _running_game(session, monkeypatch)
    task = ScheduledTask(
        task_type="twentyq", ref_id=session_id, run_at=datetime(2026, 8, 23, 12, 0),
        status="pending", created_by_tg_id=9,
        payload_json='{"internal":true,"action":"expire"}',
    )
    session.add(task)
    await session.flush()
    task_id = task.id
    first = await ai_game_service.terminalize(
        session, session_id=session_id, reason=FinishReason.expired,
    )
    await session.commit()
    assert first.transitioned

    await session.execute(
        update(ScheduledTask).where(ScheduledTask.id == task_id).values(status="pending")
    )
    await session.commit()
    replay = await ai_game_service.terminalize(
        session, session_id=session_id, reason=FinishReason.expired,
    )
    await session.commit()

    stored = await session.get(ScheduledTask, task_id)
    assert not replay.transitioned
    assert stored is not None and stored.status == "pending"
