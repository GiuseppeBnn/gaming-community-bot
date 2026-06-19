"""
XP progression — the **single** place that mutates ``User.xp``.

XP is a merit metric, kept separate from coins:
  * **Event XP** (uncapped): quizzes and direct admin grants/airdrops. Curated by
    admins, so not farmable.
  * **Participation XP** (capped): small amounts from /daily and winning a bet,
    bounded by a per-user **daily cap** (``settings.xp_daily_participation_cap``)
    enforced server-side — so users can't farm XP from random actions.

Progression is shown as a **level** (GTA-style): XP maps to a numeric level via a
geometric curve (``settings.xp_level_base`` / ``xp_level_growth``), and the
CSV-loaded named tiers in ``catalog_loader`` (Novizio→Leggenda) map to **level
ranges** (``Rank.min_level``). ``User.rank_slug`` caches the last tier seen to
detect tier-ups; the finer-grained level-up is derived on the fly from XP.

No-commit convention: callers own the transaction (same as the other services).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import User
from services import catalog_loader
from services.catalog_loader import Rank

log = logging.getLogger(__name__)


class XpSource(str, Enum):
    quiz = "quiz"               # event, uncapped
    daily = "daily"             # participation, capped
    bet_won = "bet_won"         # participation, capped
    admin_grant = "admin_grant"  # event, uncapped
    admin_airdrop = "admin_airdrop"  # event, uncapped


@dataclass
class XpGrantResult:
    granted: int            # XP actually added (may be < requested if capped)
    capped: bool            # True when the daily cap clipped the amount
    new_rank: Rank | None   # set only when this grant promoted the user to a higher tier
    new_level: int = 1      # the user's level after the grant
    leveled_up: bool = False  # True when this grant raised the numeric level


@dataclass(frozen=True)
class LevelProgress:
    level: int            # current level (≥ 1)
    xp_into_level: int    # XP accumulated within the current level
    xp_for_next: int      # XP cost to reach the next level
    floor_xp: int         # cumulative XP needed to have reached `level`


def _level_cost(n: int) -> int:
    """XP needed to go from level ``n`` to ``n+1`` (geometric, +growth% each level)."""
    return round(settings.xp_level_base * settings.xp_level_growth ** (n - 1))


def level_for_xp(xp: int) -> LevelProgress:
    """Map a total XP value to its level + progress within that level.

    Iterative (one step per level): the geometric curve grows fast, so even very
    large XP totals resolve in a few dozen iterations — no closed form needed
    (per-level rounding would make a logarithmic inverse imprecise anyway).
    """
    xp = max(0, xp)
    level = 1
    floor = 0
    cost = _level_cost(1)
    while floor + cost <= xp:
        floor += cost
        level += 1
        cost = _level_cost(level)
    return LevelProgress(level=level, xp_into_level=xp - floor, xp_for_next=cost, floor_xp=floor)


def xp_to_reach_level(level: int) -> int:
    """Cumulative XP needed to reach ``level`` (level 1 = 0). Inverse of ``level_for_xp``."""
    total = 0
    for n in range(1, max(1, level)):
        total += _level_cost(n)
    return total


def progress_bar(prog: LevelProgress, width: int = 6) -> str:
    """A plain ``▰▰▰▱▱▱`` bar for the progress within the current level.

    Pure (no HTML / no user data) — safe to interpolate without escaping.
    """
    span = prog.xp_into_level + prog.xp_for_next
    filled = 0 if span <= 0 else round(width * prog.xp_into_level / span)
    filled = max(0, min(width, filled))
    return "▰" * filled + "▱" * (width - filled)


def rank_for_level(level: int) -> Rank | None:
    """Highest named tier whose ``min_level`` is ≤ ``level`` (ranks sorted ascending)."""
    current: Rank | None = None
    for rank in catalog_loader.get_ranks():
        if level >= rank.min_level:
            current = rank
        else:
            break
    return current


def rank_for_xp(xp: int) -> Rank | None:
    """The named tier for a given total XP (via the user's level)."""
    return rank_for_level(level_for_xp(xp).level)


def _apply_rank(user: User, old_xp: int) -> Rank | None:
    """Refresh ``user.rank_slug`` from current XP; return the tier if it's a promotion."""
    old_rank = rank_for_xp(old_xp)
    new_rank = rank_for_xp(user.xp)
    if new_rank is not None:
        user.rank_slug = new_rank.slug
    promoted = (
        new_rank is not None
        and (old_rank is None or new_rank.min_level > old_rank.min_level)
    )
    return new_rank if promoted else None


async def _get_user(
    session: AsyncSession, tg_id: int, *, for_update: bool = False
) -> User | None:
    stmt = select(User).where(User.tg_id == tg_id)
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def grant_xp(
    session: AsyncSession,
    tg_id: int,
    amount: int,
    source: XpSource,
    *,
    capped: bool,
) -> XpGrantResult:
    """Add XP to a user. ``capped`` participation XP respects the daily cap.
    Does NOT commit — caller handles the commit.
    """
    if amount <= 0:
        return XpGrantResult(granted=0, capped=False, new_rank=None)

    # Only the capped path does a read-modify-write on the daily counter that
    # needs serializing; uncapped grants skip the lock (keeps the quiz
    # award_prizes wallet→xp ordering deadlock-free).
    user = await _get_user(session, tg_id, for_update=capped)
    if user is None:
        return XpGrantResult(granted=0, capped=False, new_rank=None)

    was_capped = False
    granted = amount

    if capped:
        today = datetime.now(timezone.utc).date().isoformat()
        if user.xp_today_date != today:
            user.xp_today = 0
            user.xp_today_date = today
        cap = settings.xp_daily_participation_cap
        remaining = max(0, cap - user.xp_today)
        granted = min(amount, remaining)
        was_capped = granted < amount
        user.xp_today += granted

    if granted <= 0:
        return XpGrantResult(granted=0, capped=was_capped, new_rank=None)

    old_xp = user.xp
    user.xp += granted
    new_rank = _apply_rank(user, old_xp)
    old_level = level_for_xp(old_xp).level
    new_level = level_for_xp(user.xp).level
    log.debug("XP +%d (%s) → %s = %d", granted, source.value, tg_id, user.xp)
    return XpGrantResult(
        granted=granted, capped=was_capped, new_rank=new_rank,
        new_level=new_level, leveled_up=new_level > old_level,
    )


async def set_xp(session: AsyncSession, tg_id: int, value: int) -> int:
    """Admin: set a user's XP to an absolute value (≥ 0). Returns the new XP.
    Does NOT commit — caller handles the commit.
    """
    user = await _get_user(session, tg_id)
    if user is None:
        return 0
    old_xp = user.xp
    user.xp = max(0, value)
    _apply_rank(user, old_xp)
    return user.xp


async def airdrop_xp(session: AsyncSession, amount: int) -> int:
    """Admin: add ``amount`` XP to every registered user. Returns the number of users.
    Does NOT commit — caller handles the commit.
    """
    if amount <= 0:
        return 0
    result = await session.execute(select(User))
    users = list(result.scalars().all())
    for user in users:
        old_xp = user.xp
        user.xp += amount
        _apply_rank(user, old_xp)
    return len(users)


async def leaderboard_xp(session: AsyncSession, limit: int = 10) -> list[tuple[User, int]]:
    result = await session.execute(
        select(User).order_by(User.xp.desc(), User.tg_id.asc()).limit(limit)
    )
    return [(user, user.xp) for user in result.scalars().all()]
