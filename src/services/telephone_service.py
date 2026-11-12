"""Durable open relay. Services flush; Telegram callers own every commit.

The root row is the compare-and-swap fence for claims, delivery and paid jobs.
Visits are deliberately never released: seeing a picture spends participation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from uuid import uuid4

from sqlalchemy import and_, exists, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import AIGameSession, AIGameTurn, TelephoneGame, TelephoneVisit
from services import ai_budget, schedule_service

GAME_TYPE = "telephone"
STAGES = 4
CLAIM_SECONDS = 120
DELIVERY_SECONDS = 30
GENERATION_SECONDS = 180  # longer than the bounded provider + Telegram timeouts
DURATION_SECONDS = 1800


@dataclass(frozen=True, slots=True)
class Stage:
    number: int
    description: str
    file_id: str
    author: str


@dataclass(frozen=True, slots=True)
class View:
    id: int
    status: str
    group_id: int | None
    anchor: int | None
    stage: int
    token: str | None
    user_id: int | None
    kind: str | None
    since: datetime | None
    expires_at: datetime | None
    reason: str | None
    creator: int
    model: str
    provider: str
    cap: int
    announced: bool
    stages: tuple[Stage, ...]


@dataclass(frozen=True, slots=True)
class Claim:
    game_id: int
    token: str
    user_id: int
    stage: int
    previous_file_id: str | None


async def get_view(session: AsyncSession, game_id: int) -> View | None:
    # Explicit columns bypass SQLAlchemy's possibly stale identity map.
    r = (
        await session.execute(
            select(
                AIGameSession.id,
                AIGameSession.status,
                AIGameSession.group_id,
                AIGameSession.anchor_message_id,
                AIGameSession.next_turn_no,
                AIGameSession.pending_token,
                AIGameSession.pending_user_tg_id,
                AIGameSession.pending_kind,
                AIGameSession.pending_since,
                AIGameSession.expires_at,
                AIGameSession.finish_reason,
                AIGameSession.creator_tg_id,
                TelephoneGame.image_model,
                TelephoneGame.image_provider,
                TelephoneGame.image_cap_microusd,
                TelephoneGame.reveal_announced,
            )
            .join(TelephoneGame, TelephoneGame.session_id == AIGameSession.id)
            .where(AIGameSession.id == game_id, AIGameSession.game_type == GAME_TYPE)
        )
    ).one_or_none()
    if r is None:
        return None
    turns = (
        await session.execute(
            select(
                AIGameTurn.turn_no,
                AIGameTurn.input_text,
                AIGameTurn.output_json,
            )
            .where(AIGameTurn.session_id == game_id)
            .order_by(AIGameTurn.turn_no)
        )
    ).all()
    stages = tuple(
        Stage(n, desc, json.loads(out)["file_id"], json.loads(out)["author"])
        for n, desc, out in turns
    )
    return View(
        id=r.id,
        status=r.status,
        group_id=r.group_id,
        anchor=r.anchor_message_id,
        stage=r.next_turn_no,
        token=r.pending_token,
        user_id=r.pending_user_tg_id,
        kind=r.pending_kind,
        since=r.pending_since,
        expires_at=r.expires_at,
        reason=r.finish_reason,
        creator=r.creator_tg_id,
        model=r.image_model,
        provider=r.image_provider,
        cap=r.image_cap_microusd,
        announced=r.reveal_announced,
        stages=stages,
    )


async def list_games(session: AsyncSession) -> list[tuple[int, str]]:
    return list(
        (
            await session.execute(
                select(AIGameSession.id, AIGameSession.status)
                .where(AIGameSession.game_type == GAME_TYPE)
                .order_by(AIGameSession.id.desc())
                .limit(20)
            )
        )
        .tuples()
        .all()
    )


async def create(session: AsyncSession, creator: int) -> int:
    if not settings.telephone_enabled or not settings.openrouter_api_key:
        raise ValueError("Telefono senza fili non abilitato o OpenRouter non configurato.")
    root = AIGameSession(game_type=GAME_TYPE, title="Telefono senza fili", creator_tg_id=creator)
    session.add(root)
    await session.flush()
    session.add(
        TelephoneGame(
            session_id=root.id,
            image_model=settings.telephone_image_model,
            image_provider=settings.telephone_image_provider,
            image_cap_microusd=ai_budget.usd_to_microusd(settings.telephone_image_max_usd),
        )
    )
    await session.flush()
    return root.id


async def _wake(session: AsyncSession, view: View, when: datetime) -> None:
    await schedule_service.schedule_task(
        session,
        GAME_TYPE,
        when,
        view.creator,
        view.group_id,
        view.id,
        {"action": "expire", "internal": True},
    )


async def start(
    session: AsyncSession, game_id: int, group_id: int, *, now: datetime | None = None
) -> bool:
    if not settings.telephone_enabled or not group_id:
        return False
    now = now or schedule_service.utcnow()
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(1413827653, hashtext(:g))"), {"g": str(group_id)}
        )
    other = await session.scalar(
        select(AIGameSession.id)
        .where(
            AIGameSession.game_type == GAME_TYPE,
            AIGameSession.group_id == group_id,
            AIGameSession.status == "running",
            AIGameSession.id != game_id,
        )
        .limit(1)
    )
    if other is not None:
        return False
    result = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == game_id,
            AIGameSession.game_type == GAME_TYPE,
            AIGameSession.status == "ready",
        )
        .values(
            status="running",
            group_id=group_id,
            started_at=now,
            expires_at=now + timedelta(seconds=DURATION_SECONDS),
        )
        .execution_options(synchronize_session=False)
    )
    view = await get_view(session, game_id)
    if result.rowcount == 1 and view is not None:
        assert view.expires_at is not None
        await _wake(session, view, view.expires_at)
        return True
    return view is not None and view.status == "running" and view.group_id == group_id


def _lease_expired(now: datetime):
    return or_(
        and_(
            AIGameSession.pending_kind == "delivering",
            AIGameSession.pending_since <= now - timedelta(seconds=DELIVERY_SECONDS),
        ),
        and_(
            AIGameSession.pending_kind == "writing",
            AIGameSession.pending_since <= now - timedelta(seconds=CLAIM_SECONDS),
        ),
    )


async def claim(
    session: AsyncSession,
    game_id: int,
    user_id: int,
    display_name: str,
    group_id: int,
    *,
    now: datetime | None = None,
) -> tuple[Claim | None, str]:
    now = now or schedule_service.utcnow()
    if not settings.telephone_enabled:
        return None, "disabled"
    token = str(uuid4())
    unseen = ~exists(
        select(TelephoneVisit.user_tg_id)
        .where(
            TelephoneVisit.session_id == AIGameSession.id,
            TelephoneVisit.user_tg_id == user_id,
        )
        .correlate(AIGameSession)
    )
    result = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == game_id,
            AIGameSession.game_type == GAME_TYPE,
            AIGameSession.group_id == group_id,
            AIGameSession.status == "running",
            AIGameSession.next_turn_no.between(1, STAGES),
            AIGameSession.expires_at > now,
            unseen,
            or_(AIGameSession.pending_token.is_(None), _lease_expired(now)),
        )
        .values(
            pending_token=token,
            pending_user_tg_id=user_id,
            pending_kind="delivering",
            pending_since=now,
        )
        .execution_options(synchronize_session=False)
    )
    view = await get_view(session, game_id)
    if result.rowcount != 1:
        if (
            view is None
            or view.status != "running"
            or view.group_id != group_id
            or view.expires_at is None
            or view.expires_at <= now
        ):
            return None, "closed"
        if (
            view.user_id == user_id
            and view.token
            and view.kind == "writing"
            and view.since is not None
            and view.since > now - timedelta(seconds=CLAIM_SECONDS)
        ):
            return None, "resume"
        seen = await session.get(TelephoneVisit, (game_id, user_id))
        return None, "seen" if seen else "busy"
    assert view is not None
    session.add(
        TelephoneVisit(
            session_id=game_id,
            user_tg_id=user_id,
            stage=view.stage,
            token=token,
            display_name=display_name[:64],
        )
    )
    await session.flush()
    await _wake(session, view, now + timedelta(seconds=DELIVERY_SECONDS))
    return Claim(
        game_id, token, user_id, view.stage, view.stages[-1].file_id if view.stages else None
    ), "claimed"


async def delivered(
    session: AsyncSession, c: Claim, prompt_id: int, *, now: datetime | None = None
) -> bool:
    now = now or schedule_service.utcnow()
    result = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == c.game_id,
            AIGameSession.status == "running",
            AIGameSession.pending_token == c.token,
            AIGameSession.pending_kind == "delivering",
            AIGameSession.pending_since > now - timedelta(seconds=DELIVERY_SECONDS),
            AIGameSession.expires_at > now,
        )
        .values(pending_kind="writing", pending_since=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    await session.execute(
        update(TelephoneVisit)
        .where(TelephoneVisit.token == c.token)
        .values(prompt_message_id=prompt_id)
    )
    view = await get_view(session, c.game_id)
    assert view is not None
    await _wake(session, view, now + timedelta(seconds=CLAIM_SECONDS))
    return True


async def reply_claim(session: AsyncSession, user_id: int, prompt_id: int) -> Claim | None:
    row = (
        await session.execute(
            select(
                TelephoneVisit.session_id,
                TelephoneVisit.token,
                TelephoneVisit.stage,
            ).where(
                TelephoneVisit.user_tg_id == user_id, TelephoneVisit.prompt_message_id == prompt_id
            )
        )
    ).one_or_none()
    return Claim(row[0], row[1], user_id, row[2], None) if row else None


async def begin_generation(
    session: AsyncSession, c: Claim, description: str, *, now: datetime | None = None
) -> View | None:
    now = now or schedule_service.utcnow()
    description = description.strip()
    if not settings.telephone_enabled or not 1 <= len(description) <= 150:
        return None
    result = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == c.game_id,
            AIGameSession.status == "running",
            AIGameSession.pending_token == c.token,
            AIGameSession.pending_user_tg_id == c.user_id,
            AIGameSession.next_turn_no == c.stage,
            AIGameSession.pending_kind == "writing",
            AIGameSession.pending_since > now - timedelta(seconds=CLAIM_SECONDS),
            AIGameSession.expires_at > now,
        )
        .values(pending_kind="generating", pending_since=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return None
    await session.execute(
        update(TelephoneVisit)
        .where(TelephoneVisit.token == c.token)
        .values(description=description)
    )
    view = await get_view(session, c.game_id)
    assert view is not None
    await _wake(session, view, now + timedelta(seconds=GENERATION_SECONDS))
    return view


async def complete(
    session: AsyncSession, c: Claim, file_id: str, *, now: datetime | None = None
) -> bool:
    now = now or schedule_service.utcnow()
    result = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == c.game_id,
            AIGameSession.status == "running",
            AIGameSession.pending_token == c.token,
            AIGameSession.pending_kind == "generating",
            AIGameSession.next_turn_no == c.stage,
        )
        .values(
            next_turn_no=AIGameSession.next_turn_no + 1,
            pending_token=None,
            pending_user_tg_id=None,
            pending_kind=None,
            pending_since=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    visit = (
        await session.execute(
            select(TelephoneVisit.description, TelephoneVisit.display_name).where(
                TelephoneVisit.token == c.token
            )
        )
    ).one()
    session.add(
        AIGameTurn(
            session_id=c.game_id,
            turn_no=c.stage,
            user_tg_id=c.user_id,
            kind="image",
            input_text=visit.description,
            output_json=json.dumps({"file_id": file_id, "author": visit.display_name}),
        )
    )
    await session.flush()
    if c.stage == STAGES:
        await finish(session, c.game_id, "completed", now=now)
    else:
        await tick(session, c.game_id, now=now)
    return True


async def release(session: AsyncSession, game_id: int, token: str) -> bool:
    # Never release a paid job for someone else to retry it. On failure finish.
    result = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == game_id,
            AIGameSession.pending_token == token,
            AIGameSession.status == "running",
            AIGameSession.pending_kind.in_(("delivering", "writing")),
        )
        .values(pending_token=None, pending_user_tg_id=None, pending_kind=None, pending_since=None)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def finish(
    session: AsyncSession, game_id: int, reason: str, *, now: datetime | None = None
) -> bool:
    result = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == game_id,
            AIGameSession.game_type == GAME_TYPE,
            AIGameSession.status == "running",
        )
        .values(
            status="finished",
            finish_reason=reason,
            finished_at=now or schedule_service.utcnow(),
            pending_token=None,
            pending_user_tg_id=None,
            pending_kind=None,
            pending_since=None,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def tick(session: AsyncSession, game_id: int, *, now: datetime | None = None) -> bool:
    """Release expired writers, never re-run an ambiguous paid generation."""
    now = now or schedule_service.utcnow()
    view = await get_view(session, game_id)
    if view is None or view.status != "running":
        return False
    result = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == game_id,
            AIGameSession.status == "running",
            or_(
                and_(
                    AIGameSession.expires_at <= now,
                    AIGameSession.pending_kind.is_distinct_from("generating"),
                ),
                and_(
                    AIGameSession.pending_kind == "generating",
                    AIGameSession.pending_since <= now - timedelta(seconds=GENERATION_SECONDS),
                ),
            ),
        )
        .values(
            status="finished",
            finish_reason="expired",
            finished_at=now,
            pending_token=None,
            pending_user_tg_id=None,
            pending_kind=None,
            pending_since=None,
        )
        .execution_options(synchronize_session=False)
    )
    released = await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == game_id,
            AIGameSession.status == "running",
            _lease_expired(now),
        )
        .values(pending_token=None, pending_user_tg_id=None, pending_kind=None, pending_since=None)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1 or released.rowcount == 1


async def set_anchor(session: AsyncSession, game_id: int, message_id: int) -> None:
    await session.execute(
        update(AIGameSession)
        .where(
            AIGameSession.id == game_id,
            AIGameSession.anchor_message_id.is_(None),
        )
        .values(anchor_message_id=message_id)
        .execution_options(synchronize_session=False)
    )


async def claim_reveal_notice(session: AsyncSession, game_id: int) -> bool:
    terminal = exists(
        select(AIGameSession.id).where(
            AIGameSession.id == game_id,
            AIGameSession.status == "finished",
        )
    )
    result = await session.execute(
        update(TelephoneGame)
        .where(
            TelephoneGame.session_id == game_id,
            TelephoneGame.reveal_announced.is_(False),
            terminal,
        )
        .values(reveal_announced=True)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
