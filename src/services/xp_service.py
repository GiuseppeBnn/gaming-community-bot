"""
XP progression — the **single** place that mutates ``User.xp``.

XP is a merit metric, kept separate from coins:
  * **Event XP** (uncapped): quizzes and direct admin grants/airdrops. Curated by
    admins, so not farmable.
  * **Participation XP** (capped): small amounts from /daily and winning a bet,
    bounded by a per-user **daily cap** (``settings.xp_daily_participation_cap``)
    enforced server-side — so users can't farm XP from random actions.

Ranks (cosmetic titles) are *derived* from XP via the CSV-loaded registry in
``catalog_loader``; ``User.rank_slug`` caches the last rank seen to detect rank-ups.

No-commit convention: callers own the transaction (same as the other services).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
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
    new_rank: Rank | None   # set only when this grant promoted the user to a higher rank


def rank_for_xp(xp: int) -> Rank | None:
    """Highest rank whose ``min_xp`` is ≤ ``xp`` (ranks are loaded sorted ascending)."""
    current: Rank | None = None
    for rank in catalog_loader.get_ranks():
        if xp >= rank.min_xp:
            current = rank
        else:
            break
    return current


def _apply_rank(user: User, old_xp: int) -> Rank | None:
    """Refresh ``user.rank_slug`` from current XP; return the rank if it's a promotion."""
    old_rank = rank_for_xp(old_xp)
    new_rank = rank_for_xp(user.xp)
    if new_rank is not None:
        user.rank_slug = new_rank.slug
    promoted = (
        new_rank is not None
        and (old_rank is None or new_rank.min_xp > old_rank.min_xp)
    )
    return new_rank if promoted else None


async def _get_user(session: AsyncSession, tg_id: int) -> User | None:
    result = await session.execute(select(User).where(User.tg_id == tg_id))
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

    user = await _get_user(session, tg_id)
    if user is None:
        return XpGrantResult(granted=0, capped=False, new_rank=None)

    was_capped = False
    granted = amount

    if capped:
        today = date.today().isoformat()
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
    log.debug("XP +%d (%s) → %s = %d", granted, source.value, tg_id, user.xp)
    return XpGrantResult(granted=granted, capped=was_capped, new_rank=new_rank)


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
