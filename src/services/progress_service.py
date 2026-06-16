"""
Progress — community-game podium tracking for trophies.

Records a podium finish (rank 1–3) per user per game event into ``game_podiums``,
and exposes batch counts for the trophy engine (``podium_count`` /
``first_place_count`` conditions). The only game wired today is the quiz
(``trivia``); ``guess`` / ``sound`` are forward-declared so their future handlers
just call ``record_podium`` and their trophies light up automatically.

No-commit convention: callers own the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import GamePodium

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
