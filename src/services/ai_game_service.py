"""Persistent runtime primitives shared by AI-assisted game strategies.

No function commits. The handler commits the short claim transaction before an
external AI call, then completes or releases it in a second transaction.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, overload, cast

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import (
    AIGameCatalogDraw,
    AIGameCatalogEntry,
    AIGameRewardSettlement,
    AIGameSession,
    AIGameTurn,
    ScheduledTask,
    TwentyQuestionsGame,
)
from services import ai_game_rewards, schedule_service
from services.ai_game_types import (
    CreatedGame,
    FinishReason,
    GameCreationError,
    GameView,
    PersonalQuota,
    QuestionClaim,
    QuestionStartResult,
    QuestionContextTurn,
    QuestionVerdict,
    RewardProjection,
    SettlementFinishReason,
    StartGameResult,
    StartRejectReason,
    TerminalResult,
    TurnKind,
    TurnOutcome,
    TurnRejectReason,
    TurnResult,
    TurnView,
    TwentyQuestionsPolicy,
)
from services.guess_judge import normalize
from services.structured_ai import StructuredAIError, StructuredAIProvider
from services.structured_ai_router import (
    RoutedStructuredResult,
    StructuredAIRouter,
    get_twenty_questions_router,
    has_configured_twenty_questions_provider,
)
from services.twenty_questions_ai import (
    build_question_request,
    configured_context_limits,
    parse_question_verdict,
    select_question_context,
)
from services.twenty_questions_catalog import GameDossier, all_games
from services.twenty_questions_rules import (
    compute_reward_projection,
    looks_like_direct_guess,
    normalize_turn_input,
    normalized_input_hash,
    v2_policy,
)

GAME_TYPE = "twentyq"
log = logging.getLogger(__name__)

_V2_RULES_VERSION = 2
_MAX_TURN_INPUT_CHARS = 500
_QUESTION_CONTEXT_CANDIDATES = 96
_QUESTION_CONTEXT_MAX_TURNS = 24
_QUESTION_CONTEXT_MAX_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class GameSnapshot:
    session: AIGameSession
    game: TwentyQuestionsGame
    turns: tuple[AIGameTurn, ...]

    @property
    def questions_left(self) -> int:
        return max(0, (self.game.question_limit or 0) - self.game.questions_used)

    @property
    def guesses_left(self) -> int:
        return max(0, (self.game.guess_limit or 0) - self.game.guesses_used)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value: datetime | None) -> datetime | None:
    """Normalize aware caller values to the database's naive-UTC convention."""
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


async def create_twenty_questions(
    session: AsyncSession, *, creator_tg_id: int, title: str,
    duration_seconds: int | None, expires_at: datetime | None,
    max_coins_per_participant: int,
    target: GameDossier | None = None,
) -> CreatedGame:
    expires_at = _naive_utc(expires_at)
    if not settings.twentyq_v2_enabled:
        raise GameCreationError("feature_disabled")
    if (duration_seconds is None) == (expires_at is None):
        raise GameCreationError("invalid_policy")
    if not 1 <= max_coins_per_participant <= settings.twentyq_max_coins_per_participant:
        raise GameCreationError("invalid_policy")
    policy = v2_policy(max_coins_per_participant)
    # Production uses PostgreSQL. Serialize the very short draw transaction so
    # two simultaneous admin creations cannot select from the same ledger state.
    # SQLite tests/development already serialize writes on their single connection.
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text(
            "LOCK TABLE ai_game_catalog_draws IN SHARE ROW EXCLUSIVE MODE",
        ))
    await _bootstrap_draw_history(session)
    target = target or await _select_target(session)
    session.add(AIGameCatalogDraw(
        game_type=GAME_TYPE, catalog_key=target.key,
    ))
    root = AIGameSession(
        game_type=GAME_TYPE, title=title[:256], creator_tg_id=creator_tg_id,
        status="ready", duration_seconds=duration_seconds, expires_at=expires_at,
    )
    session.add(root)
    await session.flush()
    session.add(TwentyQuestionsGame(
        session_id=root.id, catalog_key=target.key, answer=target.title,
        aliases_json=json.dumps(target.aliases, ensure_ascii=False),
        dossier_json=json.dumps(target.dossier, ensure_ascii=False),
        rules_version=policy.version, question_limit=None, guess_limit=None,
        questions_per_user=policy.questions_per_user,
        guesses_per_user=policy.guesses_per_user,
    ))
    session.add(AIGameRewardSettlement(
        session_id=root.id,
        policy_version=policy.version,
        max_coins_per_participant=policy.max_coins_per_participant,
        minimum_bps=policy.minimum_bps,
        question_penalty_bps=policy.question_penalty_bps,
        wrong_guess_penalty_bps=policy.wrong_guess_penalty_bps,
        xp_per_participant=policy.xp_per_participant,
        status="pending",
    ))
    await session.flush()
    return CreatedGame(root.id, root.title)


async def _bootstrap_draw_history(session: AsyncSession) -> None:
    """Seed the new draw ledger once from games created before it existed."""
    has_draws = (await session.execute(
        select(AIGameCatalogDraw.id)
        .where(AIGameCatalogDraw.game_type == GAME_TYPE)
        .limit(1)
    )).scalar_one_or_none()
    if has_draws is not None:
        return
    previous_keys = (await session.execute(
        select(TwentyQuestionsGame.catalog_key)
        .join(AIGameSession, AIGameSession.id == TwentyQuestionsGame.session_id)
        .where(AIGameSession.game_type == GAME_TYPE)
        .order_by(AIGameSession.id.asc())
    )).scalars().all()
    session.add_all([
        AIGameCatalogDraw(game_type=GAME_TYPE, catalog_key=key)
        for key in previous_keys
    ])
    await session.flush()


async def _balanced_target(
    session: AsyncSession, catalog: tuple[GameDossier, ...],
) -> GameDossier:
    """Draw among the least-used catalog entries, avoiding the previous draw.

    With sequential creation (the bot's normal handler path), every catalog item
    is selected once before any item starts a new cycle. The append-only history
    survives session deletion and adapts automatically when catalog keys change.
    """
    if not catalog:
        raise ValueError("twenty questions catalog is empty")
    count_rows = (await session.execute(
        select(AIGameCatalogDraw.catalog_key, func.count(AIGameCatalogDraw.id))
        .where(AIGameCatalogDraw.game_type == GAME_TYPE)
        .group_by(AIGameCatalogDraw.catalog_key)
    )).all()
    counts: dict[str, int] = {key: count for key, count in count_rows}
    minimum = min(counts.get(game.key, 0) for game in catalog)
    candidates = [game for game in catalog if counts.get(game.key, 0) == minimum]
    last_key = (await session.execute(
        select(AIGameCatalogDraw.catalog_key)
        .where(AIGameCatalogDraw.game_type == GAME_TYPE)
        .order_by(AIGameCatalogDraw.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if len(candidates) > 1:
        candidates = [game for game in candidates if game.key != last_key]
    return secrets.choice(candidates)


async def _select_target(session: AsyncSession) -> GameDossier:
    """Pick from the qualified external cache, or from the built-in fallback."""
    draw_count = func.count(AIGameCatalogDraw.id)
    rows = (await session.execute(
        select(AIGameCatalogEntry.catalog_key, draw_count)
        .outerjoin(AIGameCatalogDraw, and_(
            AIGameCatalogDraw.game_type == GAME_TYPE,
            AIGameCatalogDraw.catalog_key == AIGameCatalogEntry.catalog_key,
        ))
        .where(
            AIGameCatalogEntry.game_type == GAME_TYPE,
            AIGameCatalogEntry.active.is_(True),
        )
        .group_by(AIGameCatalogEntry.catalog_key)
    )).all()
    if not rows:
        return await _balanced_target(session, all_games())

    counts = {key: count for key, count in rows}
    minimum = min(counts.values())
    candidates = [key for key, count in counts.items() if count == minimum]
    last_key = (await session.execute(
        select(AIGameCatalogDraw.catalog_key)
        .where(AIGameCatalogDraw.game_type == GAME_TYPE)
        .order_by(AIGameCatalogDraw.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if len(candidates) > 1:
        candidates = [key for key in candidates if key != last_key]
    selected_key = secrets.choice(candidates)
    entry = (await session.execute(select(AIGameCatalogEntry).where(
        AIGameCatalogEntry.game_type == GAME_TYPE,
        AIGameCatalogEntry.catalog_key == selected_key,
        AIGameCatalogEntry.active.is_(True),
    ))).scalar_one()
    try:
        aliases = json.loads(entry.aliases_json)
        dossier = json.loads(entry.dossier_json)["facts"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"corrupt cached catalog entry {selected_key}") from exc
    if not isinstance(aliases, list) or not isinstance(dossier, str):
        raise RuntimeError(f"corrupt cached catalog entry {selected_key}")
    return GameDossier(
        key=entry.catalog_key, title=entry.title,
        aliases=tuple(str(alias) for alias in aliases), dossier=dossier,
    )


async def get_snapshot(session: AsyncSession, session_id: int) -> GameSnapshot | None:
    pair = (await session.execute(
        select(AIGameSession, TwentyQuestionsGame)
        .join(TwentyQuestionsGame, TwentyQuestionsGame.session_id == AIGameSession.id)
        .where(AIGameSession.id == session_id, AIGameSession.game_type == GAME_TYPE)
    )).one_or_none()
    if pair is None:
        return None
    # The legacy handler keeps entity snapshots across its short Telegram
    # transactions. Refresh the compatibility entities explicitly; the v2
    # presenter path below remains scalar-only and never uses live ORM rows.
    await session.refresh(pair[0])
    await session.refresh(pair[1])
    turns = tuple((await session.execute(
        select(AIGameTurn).where(AIGameTurn.session_id == session_id)
        .order_by(AIGameTurn.turn_no.asc())
    )).scalars().all())
    return GameSnapshot(pair[0], pair[1], turns)


async def list_manageable(
    session: AsyncSession, *, finished_limit: int = 10,
) -> list[AIGameSession]:
    rows = list((await session.execute(
        select(AIGameSession)
        .where(
            AIGameSession.game_type == GAME_TYPE,
            AIGameSession.status.in_(("ready", "running", "finished")),
            AIGameSession.archived_at.is_(None),
        )
        .order_by(AIGameSession.created_at.desc(), AIGameSession.id.desc())
    )).scalars().all())
    active = [row for row in rows if row.status in ("running", "ready")]
    active.sort(key=lambda row: 0 if row.status == "running" else 1)
    return active + [row for row in rows if row.status == "finished"][:finished_limit]


async def list_ready(session: AsyncSession) -> list[AIGameSession]:
    return list((await session.execute(
        select(AIGameSession).where(
            AIGameSession.game_type == GAME_TYPE, AIGameSession.status == "ready",
            AIGameSession.archived_at.is_(None),
        ).order_by(AIGameSession.created_at.desc(), AIGameSession.id.desc())
    )).scalars().all())


async def find_by_anchor(
    session: AsyncSession, group_id: int, anchor_message_id: int,
) -> GameSnapshot | None:
    root_id = (await session.execute(
        select(AIGameSession.id).where(
            AIGameSession.game_type == GAME_TYPE,
            AIGameSession.group_id == group_id,
            AIGameSession.anchor_message_id == anchor_message_id,
            AIGameSession.status == "running",
        )
    )).scalar_one_or_none()
    return await get_snapshot(session, root_id) if root_id is not None else None


async def _legacy_start(
    session: AsyncSession, session_id: int, *, group_id: int, anchor_message_id: int,
) -> bool:
    legacy_game = select(TwentyQuestionsGame.session_id).where(
        TwentyQuestionsGame.session_id == AIGameSession.id,
        TwentyQuestionsGame.rules_version == 1,
    ).exists()
    result = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == session_id,
            AIGameSession.status == "ready",
            legacy_game,
        )
        .values(
            status="running", group_id=group_id, anchor_message_id=anchor_message_id,
            started_at=_now(),
        ).execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


@overload
async def start(
    session: AsyncSession,
    session_id: int,
    *,
    group_id: int,
    now: datetime | None = None,
) -> StartGameResult: ...


@overload
async def start(
    session: AsyncSession,
    session_id: int,
    *,
    group_id: int,
    anchor_message_id: int,
) -> bool: ...


async def start(
    session: AsyncSession,
    session_id: int,
    *,
    group_id: int,
    anchor_message_id: int | None = None,
    now: datetime | None = None,
) -> StartGameResult | bool:
    """Start a v2 game without network I/O, retaining the legacy eager adapter.

    The anchor-bearing call is deliberately restricted to version-one rows: the
    old event adapter announces first, while a v2 caller commits this transition
    before publishing and installs the resulting anchor with a separate CAS.
    """
    if anchor_message_id is not None:
        return await _legacy_start(
            session, session_id, group_id=group_id, anchor_message_id=anchor_message_id,
        )

    started_at = _naive_utc(now) or _now()
    if not has_configured_twenty_questions_provider():
        return StartGameResult(False, StartRejectReason.providers_unavailable, None)

    lifecycle = (await session.execute(
        select(
            AIGameSession.duration_seconds,
            AIGameSession.expires_at,
            AIGameSession.creator_tg_id,
        )
        .join(TwentyQuestionsGame, TwentyQuestionsGame.session_id == AIGameSession.id)
        .where(
            AIGameSession.id == session_id,
            AIGameSession.game_type == GAME_TYPE,
            TwentyQuestionsGame.rules_version == 2,
        )
    )).one_or_none()
    if lifecycle is None:
        return StartGameResult(False, StartRejectReason.not_ready, None)
    duration_seconds, absolute_expiry, creator_tg_id = lifecycle
    absolute_expiry = _naive_utc(absolute_expiry)
    if duration_seconds is None and absolute_expiry is not None and absolute_expiry <= started_at:
        return StartGameResult(False, StartRejectReason.absolute_expiry_elapsed, None)
    if duration_seconds is None and absolute_expiry is None:
        return StartGameResult(False, StartRejectReason.not_ready, None)
    computed_expiry = (
        started_at + timedelta(seconds=duration_seconds)
        if duration_seconds is not None else absolute_expiry
    )
    assert computed_expiry is not None
    v2_game = select(TwentyQuestionsGame.session_id).where(
        TwentyQuestionsGame.session_id == AIGameSession.id,
        TwentyQuestionsGame.rules_version == 2,
    ).exists()
    conditions = [
        AIGameSession.id == session_id,
        AIGameSession.status == "ready",
        v2_game,
    ]
    if duration_seconds is None:
        conditions.append(AIGameSession.expires_at > started_at)
    else:
        conditions.extend((
            AIGameSession.duration_seconds == duration_seconds,
            AIGameSession.expires_at.is_(None),
        ))
    transitioned = await session.execute(
        update(AIGameSession)
        .where(*conditions)
        .values(
            status="running",
            group_id=group_id,
            anchor_message_id=None,
            started_at=started_at,
            expires_at=computed_expiry,
        )
        .execution_options(synchronize_session=False)
    )
    if transitioned.rowcount != 1:
        return StartGameResult(False, StartRejectReason.not_ready, None)
    await schedule_service.schedule_task(
        session,
        GAME_TYPE,
        computed_expiry,
        created_by_tg_id=creator_tg_id,
        group_id=group_id,
        ref_id=session_id,
        payload={"action": "expire", "internal": True},
    )
    return StartGameResult(True, None, computed_expiry)


async def move_anchor(
    session: AsyncSession, session_id: int, anchor_message_id: int,
) -> None:
    await session.execute(
        update(AIGameSession)
        .where(AIGameSession.id == session_id)
        .values(anchor_message_id=anchor_message_id)
        .execution_options(synchronize_session=False)
    )


async def move_anchor_if_current(
    session: AsyncSession,
    session_id: int,
    *,
    expected_message_id: int | None,
    new_message_id: int,
) -> bool:
    expected = (
        AIGameSession.anchor_message_id.is_(None)
        if expected_message_id is None
        else AIGameSession.anchor_message_id == expected_message_id
    )
    moved = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == session_id,
            AIGameSession.game_type == GAME_TYPE,
            expected,
        )
        .values(anchor_message_id=new_message_id)
        .execution_options(synchronize_session=False)
    )
    return moved.rowcount == 1


async def finish(session: AsyncSession, session_id: int) -> bool:
    result = await session.execute(
        update(AIGameSession)
        .where(AIGameSession.id == session_id, AIGameSession.status == "running")
        .values(
            status="finished",
            finished_at=_now(),
            pending_token=None,
            pending_since=None,
            pending_user_tg_id=None,
            pending_kind=None,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def terminalize(
    session: AsyncSession,
    *,
    session_id: int,
    reason: SettlementFinishReason,
    winner_tg_id: int | None = None,
    now: datetime | None = None,
) -> TerminalResult:
    """Facade for v2 terminal rewards; the reward service owns the transaction work."""
    return await ai_game_rewards.terminalize(
        session,
        session_id=session_id,
        reason=reason,
        winner_tg_id=winner_tg_id,
        now=now,
    )


async def archive_game(session: AsyncSession, session_id: int) -> bool:
    v2_game = select(TwentyQuestionsGame.session_id).where(
        TwentyQuestionsGame.session_id == AIGameSession.id,
        TwentyQuestionsGame.rules_version == 2,
    ).exists()
    archived = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == session_id,
            AIGameSession.game_type == GAME_TYPE,
            AIGameSession.status == "finished",
            AIGameSession.archived_at.is_(None),
            v2_game,
        )
        .values(archived_at=_now())
        .execution_options(synchronize_session=False)
    )
    return archived.rowcount == 1


async def delete_game(session: AsyncSession, session_id: int) -> bool:
    # Lock the aggregate root before touching dependent policy/strategy rows.
    # PostgreSQL keeps this lock to the caller's commit, serializing a concurrent
    # v2 start; SQLite's single writer already gives the equivalent test/dev path.
    await session.execute(
        select(AIGameSession.id)
        .where(AIGameSession.id == session_id, AIGameSession.game_type == GAME_TYPE)
        .with_for_update()
    )
    v2_eligible = select(AIGameSession.id).join(
        TwentyQuestionsGame,
        TwentyQuestionsGame.session_id == AIGameSession.id,
    ).where(
        AIGameSession.id == session_id,
        AIGameSession.game_type == GAME_TYPE,
        TwentyQuestionsGame.rules_version == 2,
        AIGameSession.status == "ready",
    )
    legacy_eligible = select(AIGameSession.id).join(
        TwentyQuestionsGame,
        TwentyQuestionsGame.session_id == AIGameSession.id,
    ).where(
        AIGameSession.id == session_id,
        AIGameSession.game_type == GAME_TYPE,
        TwentyQuestionsGame.rules_version == 1,
        AIGameSession.status != "running",
    )
    eligible = v2_eligible.union(legacy_eligible)
    # PostgreSQL enforces ON DELETE CASCADE; SQLite test/developer databases do
    # not enable FK pragmas globally. Delete children explicitly so both engines
    # preserve the same invariant and a reused SQLite PK cannot hit an orphan.
    await session.execute(delete(AIGameTurn).where(
        AIGameTurn.session_id.in_(eligible),
    ))
    # A v2 settlement uses RESTRICT so its immutable policy cannot disappear
    # accidentally. A ready draft is the one explicit exception.
    await session.execute(delete(AIGameRewardSettlement).where(
        AIGameRewardSettlement.session_id.in_(v2_eligible),
    ))
    await session.execute(delete(ScheduledTask).where(
        ScheduledTask.task_type == GAME_TYPE,
        ScheduledTask.ref_id.in_(v2_eligible),
    ))
    await session.execute(delete(TwentyQuestionsGame).where(
        TwentyQuestionsGame.session_id.in_(eligible),
    ))
    result = await session.execute(
        delete(AIGameSession).where(
            AIGameSession.id == session_id,
            AIGameSession.game_type == GAME_TYPE,
            or_(
                AIGameSession.status == "ready",
                and_(
                    AIGameSession.status != "running",
                    ~select(TwentyQuestionsGame.session_id).where(
                        TwentyQuestionsGame.session_id == AIGameSession.id,
                        TwentyQuestionsGame.rules_version == 2,
                    ).exists(),
                ),
            ),
        )
    )
    return result.rowcount == 1


def _legacy_view_policy(
    question_limit: int | None,
    guess_limit: int | None,
    questions_per_user: int | None,
    guesses_per_user: int | None,
) -> TwentyQuestionsPolicy:
    return TwentyQuestionsPolicy(
        version=1,
        questions_per_user=questions_per_user or question_limit or 0,
        guesses_per_user=guesses_per_user or guess_limit or 0,
        max_coins_per_participant=0,
        minimum_bps=0,
        question_penalty_bps=0,
        wrong_guess_penalty_bps=0,
        xp_per_participant=0,
    )


def _turn_view(
    turn_no: int,
    user_tg_id: int,
    kind_value: str,
    input_text: str,
    output_json: str,
) -> TurnView:
    try:
        output = json.loads(output_json)
    except (TypeError, ValueError):
        output = {}
    try:
        kind = TurnKind(kind_value)
    except ValueError:
        kind = TurnKind.question
    verdict: QuestionVerdict | None = None
    if kind is TurnKind.question:
        try:
            verdict = QuestionVerdict(output.get("verdetto"))
        except (AttributeError, TypeError, ValueError):
            pass
    correct = output.get("correct") if kind is TurnKind.guess else None
    return TurnView(
        turn_no=turn_no,
        user_tg_id=user_tg_id,
        kind=kind,
        input_text=input_text,
        verdict=verdict,
        correct=correct if isinstance(correct, bool) else None,
    )


async def get_game_view(
    session: AsyncSession, session_id: int, *, recent_turns: int = 6,
) -> GameView | None:
    """Return a small immutable presenter view without exposing a live secret."""
    row = (await session.execute(
        select(
            AIGameSession.id,
            AIGameSession.title,
            AIGameSession.status,
            AIGameSession.group_id,
            AIGameSession.anchor_message_id,
            AIGameSession.expires_at,
            AIGameSession.finish_reason,
            TwentyQuestionsGame.rules_version,
            TwentyQuestionsGame.question_limit,
            TwentyQuestionsGame.guess_limit,
            TwentyQuestionsGame.questions_per_user,
            TwentyQuestionsGame.guesses_per_user,
            TwentyQuestionsGame.answer,
            TwentyQuestionsGame.winner_tg_id,
            AIGameRewardSettlement.policy_version,
            AIGameRewardSettlement.max_coins_per_participant,
            AIGameRewardSettlement.minimum_bps,
            AIGameRewardSettlement.question_penalty_bps,
            AIGameRewardSettlement.wrong_guess_penalty_bps,
            AIGameRewardSettlement.xp_per_participant,
        )
        .join(TwentyQuestionsGame, TwentyQuestionsGame.session_id == AIGameSession.id)
        .outerjoin(
            AIGameRewardSettlement,
            AIGameRewardSettlement.session_id == AIGameSession.id,
        )
        .where(AIGameSession.id == session_id, AIGameSession.game_type == GAME_TYPE)
    )).one_or_none()
    if row is None:
        return None
    (
        root_id, title, status, group_id, anchor_message_id, expires_at, finish_reason,
        rules_version, question_limit, guess_limit, questions_per_user, guesses_per_user,
        answer, winner_tg_id, policy_version, max_coins, minimum_bps, question_penalty_bps,
        wrong_guess_penalty_bps, xp_per_participant,
    ) = row
    if rules_version == 2:
        if (
            policy_version is None or max_coins is None or minimum_bps is None
            or question_penalty_bps is None or wrong_guess_penalty_bps is None
            or xp_per_participant is None
        ):
            raise RuntimeError(f"v2 game {root_id} is missing its settlement policy")
        policy = TwentyQuestionsPolicy(
            version=policy_version,
            questions_per_user=questions_per_user or 0,
            guesses_per_user=guesses_per_user or 0,
            max_coins_per_participant=max_coins,
            minimum_bps=minimum_bps,
            question_penalty_bps=question_penalty_bps,
            wrong_guess_penalty_bps=wrong_guess_penalty_bps,
            xp_per_participant=xp_per_participant,
        )
    else:
        policy = _legacy_view_policy(
            question_limit, guess_limit, questions_per_user, guesses_per_user,
        )
    compact_turn_output = func.replace(
        func.replace(
            func.replace(func.replace(AIGameTurn.output_json, " ", ""), "\n", ""),
            "\r",
            "",
        ),
        "\t",
        "",
    )
    counts = (await session.execute(
        select(
            func.count(func.distinct(AIGameTurn.user_tg_id)),
            func.count(AIGameTurn.id).filter(AIGameTurn.kind == TurnKind.question.value),
            func.count(AIGameTurn.id).filter(and_(
                AIGameTurn.kind == TurnKind.guess.value,
                compact_turn_output.like('%"correct":false%'),
            )),
        ).where(AIGameTurn.session_id == root_id)
    )).one()
    participant_count, question_count, wrong_guess_count = (int(value or 0) for value in counts)
    max_recent = max(0, min(recent_turns, 6))
    recent_rows = (await session.execute(
        select(
            AIGameTurn.turn_no,
            AIGameTurn.user_tg_id,
            AIGameTurn.kind,
            AIGameTurn.input_text,
            AIGameTurn.output_json,
        )
        .where(AIGameTurn.session_id == root_id)
        .order_by(AIGameTurn.turn_no.desc())
        .limit(max_recent)
    )).all()
    recent = tuple(_turn_view(*turn) for turn in reversed(recent_rows))
    projection: RewardProjection = compute_reward_projection(
        policy,
        participants=participant_count,
        questions=question_count,
        wrong_guesses=wrong_guess_count,
    )
    try:
        typed_finish_reason = FinishReason(finish_reason) if finish_reason is not None else None
    except ValueError:
        typed_finish_reason = None
    return GameView(
        session_id=root_id,
        title=title,
        status=status,
        group_id=group_id,
        anchor_message_id=anchor_message_id,
        expires_at=expires_at,
        finish_reason=typed_finish_reason,
        policy=policy,
        projection=projection,
        participant_count=participant_count,
        question_count=question_count,
        wrong_guess_count=wrong_guess_count,
        recent_turns=recent,
        revealed_answer=answer if status == "finished" else None,
        winner_tg_id=winner_tg_id,
    )


def _quota_subquery(session_id: int, user_tg_id: int, kind: TurnKind):
    """Return the authoritative per-actor ledger count for a single turn kind."""
    return select(func.count(AIGameTurn.id)).where(
        AIGameTurn.session_id == session_id,
        AIGameTurn.user_tg_id == user_tg_id,
        AIGameTurn.kind == kind.value,
    ).scalar_subquery()


def _v2_limit_subquery(session_id: int, kind: TurnKind):
    column = (
        TwentyQuestionsGame.questions_per_user
        if kind is TurnKind.question else TwentyQuestionsGame.guesses_per_user
    )
    return select(column).where(
        TwentyQuestionsGame.session_id == session_id,
        TwentyQuestionsGame.rules_version == _V2_RULES_VERSION,
    ).scalar_subquery()


async def get_personal_quota(
    session: AsyncSession, session_id: int, user_tg_id: int,
) -> PersonalQuota:
    """Project one user's v2 allowance from the append-only turn ledger."""
    questions_used = _quota_subquery(session_id, user_tg_id, TurnKind.question)
    guesses_used = _quota_subquery(session_id, user_tg_id, TurnKind.guess)
    row = (await session.execute(select(
        TwentyQuestionsGame.rules_version,
        TwentyQuestionsGame.question_limit,
        TwentyQuestionsGame.guess_limit,
        TwentyQuestionsGame.questions_per_user,
        TwentyQuestionsGame.guesses_per_user,
        questions_used,
        guesses_used,
    ).join(
        AIGameSession, AIGameSession.id == TwentyQuestionsGame.session_id,
    ).where(
        TwentyQuestionsGame.session_id == session_id,
        AIGameSession.game_type == GAME_TYPE,
    ))).one_or_none()
    if row is None:
        return PersonalQuota(0, 0, 0, 0, False)
    (
        rules_version,
        question_limit,
        guess_limit,
        questions_per_user,
        guesses_per_user,
        question_count,
        guess_count,
    ) = row
    if rules_version == _V2_RULES_VERSION:
        question_cap = questions_per_user or 0
        guess_cap = guesses_per_user or 0
    else:
        # This public projection is harmless for historical rows. The v1 write
        # APIs below remain explicitly global and never use it as their cap.
        question_cap = questions_per_user or question_limit or 0
        guess_cap = guesses_per_user or guess_limit or 0
    used_questions = int(question_count or 0)
    used_guesses = int(guess_count or 0)
    return PersonalQuota(
        questions_used=used_questions,
        questions_left=max(0, question_cap - used_questions),
        guesses_used=used_guesses,
        guesses_left=max(0, guess_cap - used_guesses),
        participant=bool(used_questions or used_guesses),
    )


async def _v2_state(
    session: AsyncSession, session_id: int,
) -> tuple[str, datetime | None, str | None, datetime | None] | None:
    row = (await session.execute(select(
        AIGameSession.status,
        AIGameSession.expires_at,
        AIGameSession.pending_token,
        AIGameSession.pending_since,
    ).join(
        TwentyQuestionsGame, TwentyQuestionsGame.session_id == AIGameSession.id,
    ).where(
        AIGameSession.id == session_id,
        AIGameSession.game_type == GAME_TYPE,
        TwentyQuestionsGame.rules_version == _V2_RULES_VERSION,
    ))).one_or_none()
    if row is None:
        return None
    status, expires_at, pending_token, pending_since = row
    return status, expires_at, pending_token, pending_since


async def _terminalize_if_due(
    session: AsyncSession, session_id: int, now: datetime | None,
) -> TerminalResult | None:
    """Settle expiry before accepting a new v2 action or a late completion."""
    current = _naive_utc(now) or _now()
    state = await _v2_state(session, session_id)
    if state is None:
        return None
    status, expires_at, _, _ = state
    if status != "running" or expires_at is None or expires_at > current:
        return None
    return await terminalize(
        session,
        session_id=session_id,
        reason=FinishReason.expired,
        now=current,
    )


async def _lock_v2_action(
    session: AsyncSession, session_id: int, now: datetime | None,
) -> tuple[datetime, TerminalResult | None]:
    """Linearize a v2 action at the root lock and resolve expiry there.

    An explicit ``now`` is a caller-provided logical clock for deterministic
    tests. The normal runtime path samples ``_now()`` only after waiting for
    the root lock, so a preflight timestamp cannot survive an arbitrary wait.
    """
    await session.execute(select(AIGameSession.id).where(
        AIGameSession.id == session_id,
    ).with_for_update())
    action_now = _naive_utc(now) or _now()
    return action_now, await _terminalize_if_due(session, session_id, action_now)


async def _find_normalized_turn(
    session: AsyncSession, session_id: int, kind: TurnKind, digest: str,
) -> AIGameTurn | None:
    return (await session.execute(select(AIGameTurn).where(
        AIGameTurn.session_id == session_id,
        AIGameTurn.kind == kind.value,
        AIGameTurn.normalized_input_hash == digest,
    ))).scalar_one_or_none()


def _same_normalized_input(
    turn: AIGameTurn, normalized: str, *, session_id: int, kind: TurnKind, digest: str,
) -> bool:
    """Defend the digest index by treating a raw-input mismatch as a collision."""
    if normalize_turn_input(turn.input_text) == normalized:
        return True
    log.warning(
        "AI-game turn hash collision session_id=%s kind=%s digest=%s",
        session_id,
        kind.value,
        digest,
    )
    return False


def _cached_question_verdict(turn: AIGameTurn) -> QuestionVerdict | None:
    try:
        payload = json.loads(turn.output_json)
        return QuestionVerdict(payload["verdetto"])
    except (KeyError, TypeError, ValueError):
        return None


async def _load_question_context(
    session: AsyncSession, session_id: int, current_question: str,
) -> tuple[QuestionContextTurn, ...]:
    """Read a bounded candidate window before the pure context selector runs."""
    rows = (await session.execute(select(
        AIGameTurn.turn_no,
        AIGameTurn.normalized_input_hash,
        AIGameTurn.input_text,
        AIGameTurn.output_json,
    ).where(
        AIGameTurn.session_id == session_id,
        AIGameTurn.kind == TurnKind.question.value,
    ).order_by(AIGameTurn.turn_no.desc()).limit(_QUESTION_CONTEXT_CANDIDATES))).all()
    candidates: list[QuestionContextTurn] = []
    for turn_no, digest, question, output_json in rows:
        try:
            verdict = QuestionVerdict(json.loads(output_json)["verdetto"])
        except (KeyError, TypeError, ValueError):
            continue
        candidates.append(QuestionContextTurn(
            turn_no=turn_no,
            normalized_hash=digest,
            question=question,
            verdict=verdict,
        ))
    configured_turns, configured_chars = configured_context_limits()
    return select_question_context(
        candidates,
        current_question,
        max_turns=min(configured_turns, _QUESTION_CONTEXT_MAX_TURNS),
        max_chars=min(configured_chars, _QUESTION_CONTEXT_MAX_CHARS),
    )


async def _claim_v2_turn(
    session: AsyncSession,
    *,
    session_id: int,
    user_tg_id: int,
    kind: TurnKind,
    now: datetime,
) -> str | None:
    """Claim under the root lock already held by ``_lock_v2_action``."""
    token = str(uuid.uuid4())
    # PostgreSQL fixes an UPDATE snapshot before it waits for a row lock. The
    # caller's separate root lock gives this CAS a fresh snapshot for its ledger
    # aggregate while also establishing the action's expiry linearization point.
    stale = now - timedelta(seconds=settings.ai_game_claim_timeout_seconds)
    used = _quota_subquery(session_id, user_tg_id, kind)
    limit = _v2_limit_subquery(session_id, kind)
    v2_game = select(TwentyQuestionsGame.session_id).where(
        TwentyQuestionsGame.session_id == AIGameSession.id,
        TwentyQuestionsGame.rules_version == _V2_RULES_VERSION,
    ).exists()
    claimed = await session.execute(update(AIGameSession).where(
        AIGameSession.id == session_id,
        AIGameSession.status == "running",
        AIGameSession.expires_at > now,
        v2_game,
        or_(
            AIGameSession.pending_token.is_(None),
            AIGameSession.pending_since.is_(None),
            AIGameSession.pending_since < stale,
        ),
        used < limit,
    ).values(
        pending_token=token,
        pending_since=now,
        pending_user_tg_id=user_tg_id,
        pending_kind=kind.value,
    ).execution_options(synchronize_session=False))
    return token if claimed.rowcount == 1 else None


async def _failed_v2_claim(
    session: AsyncSession,
    *,
    session_id: int,
    user_tg_id: int,
    kind: TurnKind,
    now: datetime,
) -> tuple[TurnRejectReason, PersonalQuota, TerminalResult | None]:
    """Classify a failed CAS from fresh persisted state, never stale ORM rows."""
    terminal = await _terminalize_if_due(session, session_id, now)
    quota = await get_personal_quota(session, session_id, user_tg_id)
    if terminal is not None:
        return TurnRejectReason.expired, quota, terminal
    state = await _v2_state(session, session_id)
    if state is None or state[0] != "running":
        return TurnRejectReason.closed, quota, None
    left = quota.questions_left if kind is TurnKind.question else quota.guesses_left
    if left == 0:
        return (
            TurnRejectReason.question_quota if kind is TurnKind.question
            else TurnRejectReason.guess_quota,
            quota,
            None,
        )
    pending_token, pending_since = state[2:]
    stale = now - timedelta(seconds=settings.ai_game_claim_timeout_seconds)
    if pending_token is not None and (pending_since is None or pending_since >= stale):
        return TurnRejectReason.busy, quota, None
    # A fresh contender can win the CAS immediately after this read. It is a
    # lease contention either way, and intentionally exposes no actor details.
    return TurnRejectReason.busy, quota, None


async def _release_v2_claim(
    session: AsyncSession, claim: QuestionClaim,
) -> bool:
    released = await session.execute(update(AIGameSession).where(
        AIGameSession.id == claim.session_id,
        AIGameSession.status == "running",
        AIGameSession.pending_token == claim.token,
        AIGameSession.pending_user_tg_id == claim.user_tg_id,
        AIGameSession.pending_kind == claim.kind.value,
    ).values(
        pending_token=None,
        pending_since=None,
        pending_user_tg_id=None,
        pending_kind=None,
    ).execution_options(synchronize_session=False))
    return released.rowcount == 1


async def _turn_no_for_v2_claim(
    session: AsyncSession,
    *,
    session_id: int,
    token: str,
    user_tg_id: int,
    kind: TurnKind,
) -> int | None:
    return (await session.execute(select(AIGameSession.next_turn_no).where(
        AIGameSession.id == session_id,
        AIGameSession.status == "running",
        AIGameSession.pending_token == token,
        AIGameSession.pending_user_tg_id == user_tg_id,
        AIGameSession.pending_kind == kind.value,
    ))).scalar_one_or_none()


async def _append_v2_turn(
    session: AsyncSession,
    *,
    session_id: int,
    token: str,
    user_tg_id: int,
    kind: TurnKind,
    input_text: str,
    digest: str,
    output_json: str,
    now: datetime,
) -> bool | None:
    """Append one turn under the root lock held by ``_lock_v2_action``.

    ``None`` means a uniqueness race was safely rolled back and should be
    translated to a typed dedupe/collision result by the public caller.
    """
    turn_no = await _turn_no_for_v2_claim(
        session,
        session_id=session_id,
        token=token,
        user_tg_id=user_tg_id,
        kind=kind,
    )
    if turn_no is None:
        return False
    projection_column = (
        TwentyQuestionsGame.questions_used
        if kind is TurnKind.question else TwentyQuestionsGame.guesses_used
    )
    try:
        async with session.begin_nested():
            released = await session.execute(update(AIGameSession).where(
                AIGameSession.id == session_id,
                AIGameSession.status == "running",
                AIGameSession.expires_at > now,
                AIGameSession.pending_token == token,
                AIGameSession.pending_user_tg_id == user_tg_id,
                AIGameSession.pending_kind == kind.value,
            ).values(
                next_turn_no=AIGameSession.next_turn_no + 1,
                pending_token=None,
                pending_since=None,
                pending_user_tg_id=None,
                pending_kind=None,
            ).execution_options(synchronize_session=False))
            if released.rowcount != 1:
                return False
            projected = await session.execute(update(TwentyQuestionsGame).where(
                TwentyQuestionsGame.session_id == session_id,
                TwentyQuestionsGame.rules_version == _V2_RULES_VERSION,
            ).values(
                **{projection_column.key: projection_column + 1},
            ).execution_options(synchronize_session=False))
            if projected.rowcount != 1:
                raise RuntimeError(f"v2 game {session_id} lost its strategy projection")
            session.add(AIGameTurn(
                session_id=session_id,
                turn_no=turn_no,
                user_tg_id=user_tg_id,
                kind=kind.value,
                input_text=input_text,
                output_json=output_json,
                normalized_input_hash=digest,
            ))
            await session.flush()
    except IntegrityError:
        await session.execute(update(AIGameSession).where(
            AIGameSession.id == session_id,
            AIGameSession.status == "running",
            AIGameSession.pending_token == token,
            AIGameSession.pending_user_tg_id == user_tg_id,
            AIGameSession.pending_kind == kind.value,
        ).values(
            pending_token=None,
            pending_since=None,
            pending_user_tg_id=None,
            pending_kind=None,
        ).execution_options(synchronize_session=False))
        return None
    return True


async def begin_question(
    session: AsyncSession,
    *,
    session_id: int,
    user_tg_id: int,
    question: str,
    now: datetime | None = None,
) -> QuestionStartResult:
    """Claim a short v2 question lease without calling an AI provider."""
    current = _naive_utc(now) or _now()
    normalized = normalize_turn_input(question)
    terminal = await _terminalize_if_due(session, session_id, current)
    if terminal is not None:
        return QuestionStartResult(
            session_id,
            TurnOutcome.rejected,
            TurnRejectReason.expired,
            await get_personal_quota(session, session_id, user_tg_id),
            terminal=terminal,
        )
    state = await _v2_state(session, session_id)
    if state is None or state[0] != "running":
        return QuestionStartResult(
            session_id,
            TurnOutcome.rejected,
            TurnRejectReason.closed,
            await get_personal_quota(session, session_id, user_tg_id),
        )
    if not normalized or len(question) > _MAX_TURN_INPUT_CHARS:
        return QuestionStartResult(
            session_id,
            TurnOutcome.rejected,
            TurnRejectReason.invalid_input,
            await get_personal_quota(session, session_id, user_tg_id),
        )
    quota = await get_personal_quota(session, session_id, user_tg_id)
    digest = normalized_input_hash(normalized)
    duplicate = await _find_normalized_turn(session, session_id, TurnKind.question, digest)
    if duplicate is not None:
        if not _same_normalized_input(
            duplicate, normalized, session_id=session_id, kind=TurnKind.question, digest=digest,
        ):
            return QuestionStartResult(
                session_id, TurnOutcome.rejected, TurnRejectReason.hash_collision, quota,
            )
        verdict = _cached_question_verdict(duplicate)
        if verdict is None:
            return QuestionStartResult(
                session_id, TurnOutcome.rejected, TurnRejectReason.hash_collision, quota,
            )
        return QuestionStartResult(
            session_id, TurnOutcome.reused, None, quota, cached_verdict=verdict,
        )
    if looks_like_direct_guess(question):
        return QuestionStartResult(
            session_id,
            TurnOutcome.rejected,
            TurnRejectReason.answer_confirmation_required,
            quota,
        )
    action_now, terminal = await _lock_v2_action(session, session_id, now)
    if terminal is not None:
        return QuestionStartResult(
            session_id,
            TurnOutcome.rejected,
            TurnRejectReason.expired,
            await get_personal_quota(session, session_id, user_tg_id),
            terminal=terminal,
        )
    token = await _claim_v2_turn(
        session,
        session_id=session_id,
        user_tg_id=user_tg_id,
        kind=TurnKind.question,
        now=action_now,
    )
    if token is None:
        reason, quota, terminal = await _failed_v2_claim(
            session,
            session_id=session_id,
            user_tg_id=user_tg_id,
            kind=TurnKind.question,
            now=action_now,
        )
        return QuestionStartResult(
            session_id, TurnOutcome.rejected, reason, quota, terminal=terminal,
        )
    dossier_json = (await session.execute(select(TwentyQuestionsGame.dossier_json).where(
        TwentyQuestionsGame.session_id == session_id,
        TwentyQuestionsGame.rules_version == _V2_RULES_VERSION,
    ))).scalar_one_or_none()
    if dossier_json is None:
        claim = QuestionClaim(
            session_id, token, user_tg_id, question, normalized, digest, "{}", (),
        )
        await _release_v2_claim(session, claim)
        return QuestionStartResult(
            session_id, TurnOutcome.rejected, TurnRejectReason.closed, quota,
        )
    context = await _load_question_context(session, session_id, question)
    claim = QuestionClaim(
        session_id=session_id,
        token=token,
        user_tg_id=user_tg_id,
        input_text=question,
        normalized_text=normalized,
        normalized_hash=digest,
        dossier_json=dossier_json,
        context=context,
        kind=TurnKind.question,
    )
    return QuestionStartResult(
        session_id, TurnOutcome.claimed, None, quota, claim=claim,
    )


async def _question_duplicate_result(
    session: AsyncSession, claim: QuestionClaim,
) -> TurnResult:
    quota = await get_personal_quota(session, claim.session_id, claim.user_tg_id)
    duplicate = await _find_normalized_turn(
        session, claim.session_id, TurnKind.question, claim.normalized_hash,
    )
    if duplicate is not None:
        if not _same_normalized_input(
            duplicate,
            claim.normalized_text,
            session_id=claim.session_id,
            kind=TurnKind.question,
            digest=claim.normalized_hash,
        ):
            return TurnResult(
                claim.session_id,
                TurnOutcome.rejected,
                TurnRejectReason.hash_collision,
                quota,
            )
        verdict = _cached_question_verdict(duplicate)
        if verdict is not None:
            return TurnResult(
                claim.session_id, TurnOutcome.reused, None, quota, verdict=verdict,
            )
    return TurnResult(
        claim.session_id, TurnOutcome.rejected, TurnRejectReason.lost_claim, quota,
    )


async def complete_question(
    session: AsyncSession,
    *,
    claim: QuestionClaim,
    verdict: QuestionVerdict,
    now: datetime | None = None,
) -> TurnResult:
    """Persist a classified v2 question only while its exact lease is current."""
    current = _naive_utc(now) or _now()
    terminal = await _terminalize_if_due(session, claim.session_id, current)
    if terminal is not None:
        return TurnResult(
            claim.session_id,
            TurnOutcome.rejected,
            TurnRejectReason.expired,
            await get_personal_quota(session, claim.session_id, claim.user_tg_id),
            terminal=terminal,
        )
    if claim.kind is not TurnKind.question:
        return TurnResult(
            claim.session_id,
            TurnOutcome.rejected,
            TurnRejectReason.lost_claim,
            await get_personal_quota(session, claim.session_id, claim.user_tg_id),
        )
    action_now, terminal = await _lock_v2_action(session, claim.session_id, now)
    if terminal is not None:
        return TurnResult(
            claim.session_id,
            TurnOutcome.rejected,
            TurnRejectReason.expired,
            await get_personal_quota(session, claim.session_id, claim.user_tg_id),
            terminal=terminal,
        )
    if verdict is QuestionVerdict.usa_risposta:
        released = await _release_v2_claim(session, claim)
        return TurnResult(
            claim.session_id,
            TurnOutcome.rejected,
            (
                TurnRejectReason.answer_confirmation_required
                if released else TurnRejectReason.lost_claim
            ),
            await get_personal_quota(session, claim.session_id, claim.user_tg_id),
        )
    appended = await _append_v2_turn(
        session,
        session_id=claim.session_id,
        token=claim.token,
        user_tg_id=claim.user_tg_id,
        kind=TurnKind.question,
        input_text=claim.input_text,
        digest=claim.normalized_hash,
        output_json=json.dumps({"verdetto": verdict.value}, ensure_ascii=False),
        now=action_now,
    )
    if appended is None:
        return await _question_duplicate_result(session, claim)
    quota = await get_personal_quota(session, claim.session_id, claim.user_tg_id)
    if not appended:
        return TurnResult(
            claim.session_id, TurnOutcome.rejected, TurnRejectReason.lost_claim, quota,
        )
    return TurnResult(
        claim.session_id, TurnOutcome.recorded, None, quota, verdict=verdict,
    )


async def abandon_claim(
    session: AsyncSession,
    *,
    claim: QuestionClaim,
    reason: TurnRejectReason,
) -> TurnResult:
    """Release a failed provider claim; no failure path consumes a question."""
    if claim.kind is not TurnKind.question:
        return TurnResult(
            claim.session_id,
            TurnOutcome.rejected,
            TurnRejectReason.lost_claim,
            await get_personal_quota(session, claim.session_id, claim.user_tg_id),
        )
    released = await _release_v2_claim(session, claim)
    quota = await get_personal_quota(session, claim.session_id, claim.user_tg_id)
    return TurnResult(
        claim.session_id,
        TurnOutcome.rejected,
        reason if released else TurnRejectReason.lost_claim,
        quota,
    )


def _guess_is_correct_values(canonical: str, aliases_json: str, answer: str) -> bool:
    try:
        aliases = json.loads(aliases_json)
    except (ValueError, TypeError):
        aliases = []
    accepted = [canonical, *(aliases if isinstance(aliases, list) else [])]
    candidate = normalize(answer)
    return bool(candidate) and candidate in {normalize(str(value)) for value in accepted}


async def _guess_duplicate_result(
    session: AsyncSession,
    *,
    session_id: int,
    user_tg_id: int,
    normalized: str,
    digest: str,
) -> TurnResult:
    quota = await get_personal_quota(session, session_id, user_tg_id)
    duplicate = await _find_normalized_turn(session, session_id, TurnKind.guess, digest)
    if duplicate is not None:
        if not _same_normalized_input(
            duplicate,
            normalized,
            session_id=session_id,
            kind=TurnKind.guess,
            digest=digest,
        ):
            return TurnResult(
                session_id, TurnOutcome.rejected, TurnRejectReason.hash_collision, quota,
            )
        return TurnResult(
            session_id, TurnOutcome.rejected, TurnRejectReason.duplicate_guess, quota,
        )
    return TurnResult(
        session_id, TurnOutcome.rejected, TurnRejectReason.lost_claim, quota,
    )


async def submit_guess(
    session: AsyncSession,
    *,
    session_id: int,
    user_tg_id: int,
    answer: str,
    now: datetime | None = None,
) -> TurnResult:
    """Append a locally judged v2 guess; callers never supply correctness."""
    current = _naive_utc(now) or _now()
    normalized = normalize_turn_input(answer)
    terminal = await _terminalize_if_due(session, session_id, current)
    if terminal is not None:
        return TurnResult(
            session_id,
            TurnOutcome.rejected,
            TurnRejectReason.expired,
            await get_personal_quota(session, session_id, user_tg_id),
            terminal=terminal,
        )
    state = await _v2_state(session, session_id)
    if state is None or state[0] != "running":
        return TurnResult(
            session_id,
            TurnOutcome.rejected,
            TurnRejectReason.closed,
            await get_personal_quota(session, session_id, user_tg_id),
        )
    if not normalized or len(answer) > _MAX_TURN_INPUT_CHARS:
        return TurnResult(
            session_id,
            TurnOutcome.rejected,
            TurnRejectReason.invalid_input,
            await get_personal_quota(session, session_id, user_tg_id),
        )
    quota = await get_personal_quota(session, session_id, user_tg_id)
    digest = normalized_input_hash(normalized)
    duplicate = await _find_normalized_turn(session, session_id, TurnKind.guess, digest)
    if duplicate is not None:
        return await _guess_duplicate_result(
            session,
            session_id=session_id,
            user_tg_id=user_tg_id,
            normalized=normalized,
            digest=digest,
        )
    action_now, terminal = await _lock_v2_action(session, session_id, now)
    if terminal is not None:
        return TurnResult(
            session_id,
            TurnOutcome.rejected,
            TurnRejectReason.expired,
            await get_personal_quota(session, session_id, user_tg_id),
            terminal=terminal,
        )
    token = await _claim_v2_turn(
        session,
        session_id=session_id,
        user_tg_id=user_tg_id,
        kind=TurnKind.guess,
        now=action_now,
    )
    if token is None:
        reason, quota, terminal = await _failed_v2_claim(
            session,
            session_id=session_id,
            user_tg_id=user_tg_id,
            kind=TurnKind.guess,
            now=action_now,
        )
        return TurnResult(session_id, TurnOutcome.rejected, reason, quota, terminal=terminal)
    target = (await session.execute(select(
        TwentyQuestionsGame.answer, TwentyQuestionsGame.aliases_json,
    ).where(
        TwentyQuestionsGame.session_id == session_id,
        TwentyQuestionsGame.rules_version == _V2_RULES_VERSION,
    ))).one_or_none()
    if target is None:
        await _release_v2_claim(session, QuestionClaim(
            session_id, token, user_tg_id, answer, normalized, digest, "{}", (),
            kind=TurnKind.guess,
        ))
        return TurnResult(session_id, TurnOutcome.rejected, TurnRejectReason.closed, quota)
    canonical, aliases_json = target
    correct = _guess_is_correct_values(canonical, aliases_json, answer)
    appended = await _append_v2_turn(
        session,
        session_id=session_id,
        token=token,
        user_tg_id=user_tg_id,
        kind=TurnKind.guess,
        input_text=answer,
        digest=digest,
        output_json=json.dumps({"correct": correct}),
        now=action_now,
    )
    if appended is None:
        return await _guess_duplicate_result(
            session,
            session_id=session_id,
            user_tg_id=user_tg_id,
            normalized=normalized,
            digest=digest,
        )
    quota = await get_personal_quota(session, session_id, user_tg_id)
    if not appended:
        return TurnResult(
            session_id, TurnOutcome.rejected, TurnRejectReason.lost_claim, quota,
        )
    if not correct:
        return TurnResult(session_id, TurnOutcome.recorded, None, quota, correct=False)
    # The turn is already flushed by _append_v2_turn. terminalize owns the
    # running→finished CAS and settlement in this same caller transaction.
    terminal = await terminalize(
        session,
        session_id=session_id,
        reason=FinishReason.victory,
        winner_tg_id=user_tg_id,
        now=action_now,
    )
    return TurnResult(
        session_id,
        TurnOutcome.recorded,
        None,
        await get_personal_quota(session, session_id, user_tg_id),
        correct=True,
        terminal=terminal,
    )


async def claim_turn(session: AsyncSession, session_id: int) -> str | None:
    """Legacy v1 lease adapter; v2 callers use begin_question/submit_guess."""
    token = str(uuid.uuid4())
    now = _now()
    stale = now - timedelta(seconds=settings.ai_game_claim_timeout_seconds)
    legacy_game = select(TwentyQuestionsGame.session_id).where(
        TwentyQuestionsGame.session_id == AIGameSession.id,
        TwentyQuestionsGame.rules_version == 1,
    ).exists()
    result = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == session_id,
            AIGameSession.status == "running",
            legacy_game,
            or_(AIGameSession.pending_token.is_(None), AIGameSession.pending_since < stale),
        )
        .values(
            pending_token=token,
            pending_since=now,
            pending_user_tg_id=None,
            pending_kind=None,
        )
        .execution_options(synchronize_session=False)
    )
    return token if result.rowcount == 1 else None


async def release_turn(session: AsyncSession, session_id: int, token: str) -> None:
    """Release only historical v1 leases; v2 requires an owned typed claim."""
    legacy_game = select(TwentyQuestionsGame.session_id).where(
        TwentyQuestionsGame.session_id == AIGameSession.id,
        TwentyQuestionsGame.rules_version == 1,
    ).exists()
    await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == session_id,
            AIGameSession.pending_token == token,
            legacy_game,
        )
        .values(
            pending_token=None,
            pending_since=None,
            pending_user_tg_id=None,
            pending_kind=None,
        )
        .execution_options(synchronize_session=False)
    )


@overload
async def classify_question(
    claim: QuestionClaim, router: StructuredAIRouter | None = None,
) -> RoutedStructuredResult[QuestionVerdict]: ...


@overload
async def classify_question(
    claim: GameSnapshot, router: str, provider: StructuredAIProvider,
) -> QuestionVerdict: ...


@overload
async def classify_question(
    *, snapshot: GameSnapshot, question: str, provider: StructuredAIProvider,
) -> QuestionVerdict: ...


async def classify_question(
    claim: QuestionClaim | GameSnapshot | None = None,
    router: StructuredAIRouter | str | None = None,
    provider: StructuredAIProvider | None = None,
    **legacy_kwargs: object,
) -> RoutedStructuredResult[QuestionVerdict] | QuestionVerdict:
    """Classify a v2 claim without DB I/O, retaining the v1 provider adapter."""
    if legacy_kwargs:
        legacy_snapshot = legacy_kwargs.pop("snapshot", None)
        legacy_question = legacy_kwargs.pop("question", None)
        if (
            legacy_kwargs
            or claim is not None
            or router is not None
            or legacy_snapshot is None
            or not isinstance(legacy_question, str)
        ):
            raise TypeError("legacy classification requires snapshot, question, and provider")
        # Historical callers supplied a lightweight snapshot-shaped object;
        # preserve that structural adapter instead of requiring an ORM DTO.
        claim = cast(GameSnapshot, legacy_snapshot)
        router = legacy_question
    if isinstance(claim, QuestionClaim):
        if isinstance(router, str) or provider is not None:
            raise TypeError("v2 question classification accepts only a claim and router")
        selected_router = (
            cast(StructuredAIRouter, router)
            if router is not None else get_twenty_questions_router()
        )
        request = build_question_request(
            dossier_json=claim.dossier_json,
            current_question=claim.input_text,
            context=claim.context,
        )
        return await selected_router.generate(
            request,
            session_id=claim.session_id,
            validate=parse_question_verdict,
        )

    if claim is None or not isinstance(router, str) or provider is None:
        raise TypeError("legacy classification requires snapshot, question, and provider")
    legacy_snapshot = cast(GameSnapshot, claim)
    legacy_question = router
    context: list[QuestionContextTurn] = []
    for turn in legacy_snapshot.turns:
        if turn.kind != TurnKind.question.value:
            continue
        try:
            verdict = QuestionVerdict(json.loads(turn.output_json).get("verdetto"))
        except (TypeError, ValueError):
            continue
        context.append(QuestionContextTurn(
            turn_no=turn.turn_no,
            normalized_hash=turn.normalized_input_hash,
            question=turn.input_text,
            verdict=verdict,
        ))
    request = build_question_request(
        dossier_json=legacy_snapshot.game.dossier_json,
        current_question=legacy_question,
        context=context,
    )
    result = await provider.generate_json(request)
    try:
        return parse_question_verdict(result.value)
    except ValueError as exc:
        raise StructuredAIError("invalid twenty questions verdict") from exc


async def _turn_no_for_token(
    session: AsyncSession, session_id: int, token: str,
) -> int | None:
    return (await session.execute(select(AIGameSession.next_turn_no).where(
        AIGameSession.id == session_id,
        AIGameSession.status == "running",
        AIGameSession.pending_token == token,
        select(TwentyQuestionsGame.session_id).where(
            TwentyQuestionsGame.session_id == AIGameSession.id,
            TwentyQuestionsGame.rules_version == 1,
        ).exists(),
    ))).scalar_one_or_none()


async def record_question(
    session: AsyncSession, *, session_id: int, token: str,
    user_tg_id: int, question: str, verdict: QuestionVerdict,
) -> bool:
    turn_no = await _turn_no_for_token(session, session_id, token)
    if turn_no is None:
        return False
    counter = await session.execute(
        update(TwentyQuestionsGame)
        .where(
            TwentyQuestionsGame.session_id == session_id,
            TwentyQuestionsGame.rules_version == 1,
            TwentyQuestionsGame.questions_used < TwentyQuestionsGame.question_limit,
        )
        .values(questions_used=TwentyQuestionsGame.questions_used + 1)
        .execution_options(synchronize_session=False)
    )
    if counter.rowcount != 1:
        await release_turn(session, session_id, token)
        return False
    root = await session.execute(
        update(AIGameSession)
        .where(AIGameSession.id == session_id, AIGameSession.pending_token == token)
        .values(
            pending_token=None, pending_since=None,
            next_turn_no=AIGameSession.next_turn_no + 1,
        ).execution_options(synchronize_session=False)
    )
    if root.rowcount != 1:
        raise RuntimeError("lost AI game claim")
    session.add(AIGameTurn(
        session_id=session_id, turn_no=turn_no, user_tg_id=user_tg_id,
        kind="question", input_text=question[:512],
        output_json=json.dumps({"verdetto": verdict.value}, ensure_ascii=False),
    ))
    await _finish_if_exhausted(session, session_id)
    return True


def guess_is_correct(game: TwentyQuestionsGame, answer: str) -> bool:
    return _guess_is_correct_values(game.answer, game.aliases_json, answer)


async def record_guess(
    session: AsyncSession, *, session_id: int, token: str,
    user_tg_id: int, answer: str, correct: bool,
) -> bool:
    turn_no = await _turn_no_for_token(session, session_id, token)
    if turn_no is None:
        return False
    counter = await session.execute(
        update(TwentyQuestionsGame)
        .where(
            TwentyQuestionsGame.session_id == session_id,
            TwentyQuestionsGame.rules_version == 1,
            TwentyQuestionsGame.guesses_used < TwentyQuestionsGame.guess_limit,
        )
        .values(
            guesses_used=TwentyQuestionsGame.guesses_used + 1,
            winner_tg_id=user_tg_id if correct else TwentyQuestionsGame.winner_tg_id,
        ).execution_options(synchronize_session=False)
    )
    if counter.rowcount != 1:
        await release_turn(session, session_id, token)
        return False
    root_values: dict[str, Any] = {
        "pending_token": None, "pending_since": None,
        "next_turn_no": AIGameSession.next_turn_no + 1,
    }
    if correct:
        root_values.update(status="finished", finished_at=_now())
    root = await session.execute(
        update(AIGameSession)
        .where(AIGameSession.id == session_id, AIGameSession.pending_token == token)
        .values(**root_values).execution_options(synchronize_session=False)
    )
    if root.rowcount != 1:
        raise RuntimeError("lost AI game claim")
    session.add(AIGameTurn(
        session_id=session_id, turn_no=turn_no, user_tg_id=user_tg_id,
        kind="guess", input_text=answer[:512],
        output_json=json.dumps({"correct": correct}),
    ))
    if not correct:
        await _finish_if_exhausted(session, session_id)
    return True


async def _finish_if_exhausted(session: AsyncSession, session_id: int) -> None:
    game = TwentyQuestionsGame
    exhausted = select(game.session_id).where(
        game.session_id == session_id,
        game.rules_version == 1,
        or_(game.questions_used >= game.question_limit, game.guesses_used >= game.guess_limit),
    ).exists()
    await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == session_id,
            AIGameSession.status == "running",
            exhausted,
        )
        .values(status="finished", finished_at=_now())
        .execution_options(synchronize_session=False)
    )
