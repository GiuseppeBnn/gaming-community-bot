"""Atomic terminal settlement for Alduino's v2 secret game."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    AIGameRewardAllocation,
    AIGameRewardSettlement,
    AIGameSession,
    AIGameTurn,
    TransactionType,
    TwentyQuestionsGame,
    User,
    Wallet,
)
from services import economy_service, xp_service
from services.ai_game_types import (
    FinishReason,
    RewardSummary,
    SettlementFinishReason,
    TerminalAllocation,
    TerminalResult,
    TurnKind,
    TwentyQuestionsPolicy,
)
from services.twenty_questions_rules import compute_reward_projection
from services.xp_service import XpSource


class RewardSettlementError(RuntimeError):
    """The terminal settlement cannot safely complete in the caller transaction."""


@dataclass(frozen=True, slots=True)
class _PendingSettlement:
    title: str
    group_id: int | None
    anchor_message_id: int | None
    answer: str
    winner_tg_id: int | None
    questions_per_user: int | None
    guesses_per_user: int | None
    policy_version: int
    max_coins_per_participant: int
    minimum_bps: int
    question_penalty_bps: int
    wrong_guess_penalty_bps: int
    xp_per_participant: int


_TERMINAL_REASONS = frozenset((
    FinishReason.victory,
    FinishReason.expired,
    FinishReason.admin_closed,
))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _terminal_reason(reason: SettlementFinishReason) -> FinishReason:
    """Validate the runtime enum boundary before this service mutates anything."""
    try:
        typed_reason = FinishReason(reason)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid AI-game settlement reason") from exc
    if typed_reason not in _TERMINAL_REASONS:
        raise ValueError("legacy AI-game finish reasons cannot settle rewards")
    return typed_reason


async def _load_pending_settlement(
    session: AsyncSession, session_id: int,
) -> _PendingSettlement:
    """Lock terminal event rows before participant account rows are touched."""
    row = (await session.execute(
        select(
            AIGameSession.title,
            AIGameSession.group_id,
            AIGameSession.anchor_message_id,
            TwentyQuestionsGame.answer,
            TwentyQuestionsGame.winner_tg_id,
            TwentyQuestionsGame.questions_per_user,
            TwentyQuestionsGame.guesses_per_user,
            AIGameRewardSettlement.policy_version,
            AIGameRewardSettlement.max_coins_per_participant,
            AIGameRewardSettlement.minimum_bps,
            AIGameRewardSettlement.question_penalty_bps,
            AIGameRewardSettlement.wrong_guess_penalty_bps,
            AIGameRewardSettlement.xp_per_participant,
        )
        .join(TwentyQuestionsGame, TwentyQuestionsGame.session_id == AIGameSession.id)
        .join(
            AIGameRewardSettlement,
            AIGameRewardSettlement.session_id == AIGameSession.id,
        )
        .where(
            AIGameSession.id == session_id,
            TwentyQuestionsGame.rules_version == 2,
            AIGameRewardSettlement.status == "pending",
        )
        .with_for_update()
    )).one_or_none()
    if row is None:
        raise RewardSettlementError(f"v2 game {session_id} has no pending reward settlement")
    return _PendingSettlement(*row)


async def _lock_and_validate_users_then_wallets(
    session: AsyncSession, participants: tuple[int, ...],
) -> None:
    """Prevalidate and lock the account rows in the global terminal-payout order."""
    user_ids = tuple((await session.execute(
        select(User.tg_id)
        .where(User.tg_id.in_(participants))
        .order_by(User.tg_id.asc())
        .with_for_update()
    )).scalars().all())
    if user_ids != participants:
        missing = sorted(set(participants) - set(user_ids))
        raise RewardSettlementError(f"missing User rows for AI-game participants: {missing}")

    wallet_ids = tuple((await session.execute(
        select(Wallet.tg_id)
        .where(Wallet.tg_id.in_(participants))
        .order_by(Wallet.tg_id.asc())
        .with_for_update()
    )).scalars().all())
    if wallet_ids != participants:
        missing = sorted(set(participants) - set(wallet_ids))
        raise RewardSettlementError(f"missing Wallet rows for AI-game participants: {missing}")


def _wrong_guess(kind: str, output_json: str) -> bool:
    if kind != TurnKind.guess.value:
        return False
    try:
        payload = json.loads(output_json)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and not bool(payload.get("correct", False))


async def _load_terminal_result(
    session: AsyncSession, session_id: int, *, transitioned: bool,
) -> TerminalResult:
    """Rebuild the public immutable result from committed-or-pending persistence."""
    row = (await session.execute(
        select(
            AIGameSession.status,
            AIGameSession.finish_reason,
            AIGameSession.group_id,
            AIGameSession.anchor_message_id,
            AIGameSession.title,
            TwentyQuestionsGame.answer,
            TwentyQuestionsGame.winner_tg_id,
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
        )
        .join(TwentyQuestionsGame, TwentyQuestionsGame.session_id == AIGameSession.id)
        .join(
            AIGameRewardSettlement,
            AIGameRewardSettlement.session_id == AIGameSession.id,
        )
        .where(AIGameSession.id == session_id)
    )).one_or_none()
    if row is None:
        raise RewardSettlementError(f"AI game {session_id} has no terminal settlement")
    (
        status,
        root_reason,
        group_id,
        anchor_message_id,
        title,
        answer,
        winner_tg_id,
        settlement_status,
        settlement_reason,
        participant_count,
        question_count,
        wrong_guess_count,
        base_amount,
        penalty_amount,
        computed_pool,
        paid_pool,
        share,
        remainder,
    ) = row
    try:
        typed_root_reason = FinishReason(root_reason)
        typed_settlement_reason = FinishReason(settlement_reason)
    except (TypeError, ValueError) as exc:
        raise RewardSettlementError(f"AI game {session_id} has an invalid terminal reason") from exc
    if (
        status != "finished"
        or typed_root_reason not in _TERMINAL_REASONS
        or typed_settlement_reason is not typed_root_reason
        or settlement_status not in ("settled", "void")
    ):
        raise RewardSettlementError(f"AI game {session_id} is not terminally settled")

    allocations = tuple(
        TerminalAllocation(user_tg_id=user_tg_id, coins=coins, xp=xp)
        for user_tg_id, coins, xp in (await session.execute(
            select(
                AIGameRewardAllocation.user_tg_id,
                AIGameRewardAllocation.coins,
                AIGameRewardAllocation.xp,
            )
            .where(AIGameRewardAllocation.session_id == session_id)
            .order_by(AIGameRewardAllocation.user_tg_id.asc())
        )).all()
    )
    return TerminalResult(
        session_id=session_id,
        transitioned=transitioned,
        finish_reason=typed_root_reason,
        group_id=group_id,
        anchor_message_id=anchor_message_id,
        title=title,
        answer=answer,
        winner_tg_id=winner_tg_id,
        reward=RewardSummary(
            settlement_status=cast(Literal["settled", "void"], settlement_status),
            participant_count=participant_count,
            question_count=question_count,
            wrong_guess_count=wrong_guess_count,
            base_amount=base_amount,
            penalty_amount=penalty_amount,
            computed_pool=computed_pool,
            paid_pool=paid_pool,
            share=share,
            remainder=remainder,
        ),
        allocations=allocations,
    )


async def terminalize(
    session: AsyncSession,
    *,
    session_id: int,
    reason: SettlementFinishReason,
    winner_tg_id: int | None = None,
    now: datetime | None = None,
) -> TerminalResult:
    """Claim and settle one v2 terminal outcome without committing.

    For a victory the caller must have added and flushed the winning turn in this
    same transaction before entering this function. Any exception is deliberately
    propagated so the caller can roll back the claim, turn, allocations, money,
    wallets, and XP together.
    """
    typed_reason = _terminal_reason(reason)
    if typed_reason is FinishReason.victory and winner_tg_id is None:
        raise ValueError("a victory settlement requires a winner_tg_id")
    if typed_reason is not FinishReason.victory and winner_tg_id is not None:
        raise ValueError("only a victory settlement may include a winner_tg_id")
    finished_at = _naive_utc(now) or _now()

    pending_v2 = select(AIGameRewardSettlement.session_id).join(
        TwentyQuestionsGame,
        TwentyQuestionsGame.session_id == AIGameRewardSettlement.session_id,
    ).where(
        AIGameRewardSettlement.session_id == AIGameSession.id,
        AIGameRewardSettlement.status == "pending",
        TwentyQuestionsGame.rules_version == 2,
    ).exists()
    claim = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == session_id,
            AIGameSession.status == "running",
            pending_v2,
        )
        .values(
            status="finished",
            finish_reason=typed_reason.value,
            finished_at=finished_at,
            pending_token=None,
            pending_since=None,
            pending_user_tg_id=None,
            pending_kind=None,
        )
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        return await _load_terminal_result(session, session_id, transitioned=False)

    if typed_reason is FinishReason.victory:
        winner = await session.execute(
            update(TwentyQuestionsGame)
            .where(
                TwentyQuestionsGame.session_id == session_id,
                TwentyQuestionsGame.rules_version == 2,
            )
            .values(winner_tg_id=winner_tg_id)
            .execution_options(synchronize_session=False)
        )
        if winner.rowcount != 1:
            raise RewardSettlementError(f"v2 game {session_id} is missing its strategy row")

    pending = await _load_pending_settlement(session, session_id)
    rows = (await session.execute(
        select(AIGameTurn.user_tg_id, AIGameTurn.kind, AIGameTurn.output_json)
        .where(AIGameTurn.session_id == session_id)
        .order_by(AIGameTurn.turn_no.asc())
    )).all()
    participants = tuple(sorted({user_tg_id for user_tg_id, _, _ in rows}))
    if not participants:
        voided = await session.execute(
            update(AIGameRewardSettlement)
            .where(
                AIGameRewardSettlement.session_id == session_id,
                AIGameRewardSettlement.status == "pending",
            )
            .values(
                status="void",
                finish_reason=typed_reason.value,
                participant_count=0,
                question_count=0,
                wrong_guess_count=0,
                base_amount=0,
                penalty_amount=0,
                computed_pool=0,
                paid_pool=0,
                share=0,
                remainder=0,
                settled_at=finished_at,
            )
            .execution_options(synchronize_session=False)
        )
        if voided.rowcount != 1:
            raise RewardSettlementError(f"v2 game {session_id} lost its pending settlement")
        return await _load_terminal_result(session, session_id, transitioned=True)

    policy = TwentyQuestionsPolicy(
        version=pending.policy_version,
        questions_per_user=pending.questions_per_user or 0,
        guesses_per_user=pending.guesses_per_user or 0,
        max_coins_per_participant=pending.max_coins_per_participant,
        minimum_bps=pending.minimum_bps,
        question_penalty_bps=pending.question_penalty_bps,
        wrong_guess_penalty_bps=pending.wrong_guess_penalty_bps,
        xp_per_participant=pending.xp_per_participant,
    )
    projection = compute_reward_projection(
        policy,
        participants=len(participants),
        questions=sum(kind == TurnKind.question.value for _, kind, _ in rows),
        wrong_guesses=sum(_wrong_guess(kind, output_json) for _, kind, output_json in rows),
    )
    await _lock_and_validate_users_then_wallets(session, participants)
    coins = projection.share if typed_reason is FinishReason.victory else 0
    session.add_all([
        AIGameRewardAllocation(
            session_id=session_id,
            user_tg_id=tg_id,
            coins=coins,
            xp=policy.xp_per_participant,
        )
        for tg_id in participants
    ])
    await session.flush()
    for tg_id in participants:
        if coins:
            await economy_service.credit(
                session,
                tg_id,
                coins,
                TransactionType.ai_game_reward,
                f"Premio gioco segreto di Alduino #{session_id}",
                reference_id=None,
            )
        granted = await xp_service.grant_xp(
            session,
            tg_id,
            policy.xp_per_participant,
            XpSource.twentyq,
            capped=False,
        )
        if granted.granted != policy.xp_per_participant:
            raise RewardSettlementError("XP grant did not match allocation")

    settled = await session.execute(
        update(AIGameRewardSettlement)
        .where(
            AIGameRewardSettlement.session_id == session_id,
            AIGameRewardSettlement.status == "pending",
        )
        .values(
            status="settled",
            finish_reason=typed_reason.value,
            participant_count=projection.participant_count,
            question_count=projection.question_count,
            wrong_guess_count=projection.wrong_guess_count,
            base_amount=projection.base_amount,
            penalty_amount=projection.penalty_amount,
            computed_pool=projection.computed_pool,
            paid_pool=projection.computed_pool if typed_reason is FinishReason.victory else 0,
            share=projection.share,
            remainder=projection.remainder,
            settled_at=finished_at,
        )
        .execution_options(synchronize_session=False)
    )
    if settled.rowcount != 1:
        raise RewardSettlementError(f"v2 game {session_id} lost its pending settlement")
    return await _load_terminal_result(session, session_id, transitioned=True)
