"""
Progress — community-game podium + generic event tracking for trophies.

Records a podium finish (rank 1–3) per user per game event into ``game_podiums``,
and exposes batch counts for the trophy engine (``podium_count`` /
``first_place_count`` conditions). The only game wired today is the quiz
(``trivia``); ``guess`` / ``sound`` are forward-declared so their future handlers
just call ``record_podium`` and their trophies light up automatically.

It also keeps a **generic per-user event log** (``user_progress_events``) for the
``event_count`` trophy condition: a "did action X in event Y" row, counted by
``metric_key``. This powers the "finished last" / "completed under 30 s" Trivia
trophies and any future "do X N times" trophy — adding one needs only a
``record_event`` call at the action site plus a CSV row, no schema change.

No-commit convention: callers own the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import GamePodium, UserProgressEvent

# Event metric keys recorded into ``user_progress_events`` (condition_param of the
# ``event_count`` trophies). Kept here so the recorder and the catalog never drift.
TRIVIA_LAST_PLACE = "trivia_last_place"
TRIVIA_SUB30 = "trivia_sub30"

# Human-readable phrasing per metric key, with a ``{when}`` slot filled by
# badge_service.describe_condition (e.g. "per la prima volta" / "10 volte").
EVENT_LABELS = {
    TRIVIA_LAST_PLACE: "Arriva ultimo {when} nel Trivia Nerd",
    TRIVIA_SUB30: "Completa il Trivia Nerd sotto i 30s {when}",
}

# Known game keys. Only ``trivia`` produces rows today; the others are reserved so
# trophy conditions/labels can reference them before the games ship.
GAME_KEYS = ("trivia", "guess", "sound")

# Human-readable game names (used by badge_service.describe_condition).
GAME_LABELS = {
    "trivia": "Trivia Nerd",
    "guess": "Guess The Game",
    "sound": "Sound Quest",
}


@dataclass(frozen=True)
class PodiumTally:
    podiums: int = 0      # total podium finishes (rank 1–3)
    first_places: int = 0  # finishes at rank 1


async def record_podium(
    session: AsyncSession,
    user_tg_id: int,
    game_key: str,
    rank: int,
    ref_id: int | None = None,
) -> GamePodium:
    """Record a single podium finish. Does NOT commit.

    Callers record once per (user, game event) for each top-3 finisher, so the
    row count is the authoritative podium tally."""
    row = GamePodium(
        user_tg_id=user_tg_id,
        game_key=game_key,
        rank=rank,
        ref_id=ref_id,
    )
    session.add(row)
    return row


async def podium_counts(session: AsyncSession, user_tg_id: int) -> dict[str, PodiumTally]:
    """Per-game tally (total podiums + first places) plus an aggregate ``"any"``
    key spanning every game — feeds the trophy engine in one query."""
    result = await session.execute(
        select(
            GamePodium.game_key,
            func.count(),
            func.sum(func.cast(GamePodium.rank == 1, Integer)),
        )
        .where(GamePodium.user_tg_id == user_tg_id)
        .group_by(GamePodium.game_key)
    )
    out: dict[str, PodiumTally] = {}
    total_podiums = total_firsts = 0
    for game_key, podiums, firsts in result.all():
        podiums = int(podiums or 0)
        firsts = int(firsts or 0)
        out[game_key] = PodiumTally(podiums=podiums, first_places=firsts)
        total_podiums += podiums
        total_firsts += firsts
    out["any"] = PodiumTally(podiums=total_podiums, first_places=total_firsts)
    return out


async def record_event(
    session: AsyncSession,
    user_tg_id: int,
    metric_key: str,
    ref_id: int | None = None,
) -> UserProgressEvent | None:
    """Record one generic progress event. Does NOT commit.

    Idempotent on ``(user, metric_key, ref_id)``: re-processing the same source
    event (e.g. closing a quiz twice) does not double-count. Returns the new row,
    or ``None`` if an identical event was already recorded."""
    existing = await session.execute(
        select(UserProgressEvent.id).where(
            UserProgressEvent.user_tg_id == user_tg_id,
            UserProgressEvent.metric_key == metric_key,
            UserProgressEvent.ref_id == ref_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return None
    row = UserProgressEvent(user_tg_id=user_tg_id, metric_key=metric_key, ref_id=ref_id)
    session.add(row)
    return row


async def event_counts(session: AsyncSession, user_tg_id: int) -> dict[str, int]:
    """Per-metric event tally for a user — one query, feeds the trophy engine
    (``event_count`` condition) like ``podium_counts`` does for podiums."""
    result = await session.execute(
        select(UserProgressEvent.metric_key, func.count())
        .where(UserProgressEvent.user_tg_id == user_tg_id)
        .group_by(UserProgressEvent.metric_key)
    )
    return {metric_key: int(count or 0) for metric_key, count in result.all()}
