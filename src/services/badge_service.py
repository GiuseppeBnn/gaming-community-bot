from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Badge, User, UserBadge
from services import catalog_loader

BADGE_FIRST_STEPS = "first_steps"

# Display order for PlayStation-style rarity tiers (low → high).
RARITY_ORDER = {"bronze": 0, "silver": 1, "gold": 2, "platinum": 3}
RARITY_LABELS = {
    "bronze": "🥉 Bronzo",
    "silver": "🥈 Argento",
    "gold": "🥇 Oro",
    "platinum": "💠 Platino",
}

# Fields synced from the catalog onto an existing Badge row (slug is the identity).
_SYNC_FIELDS = (
    "name", "description", "icon_emoji", "category", "rarity",
    "xp_reward", "condition_type", "condition_value",
)

# Human-readable "how to unlock" text per condition type (the 6 supported in
# catalog_loader.TROPHY_CONDITIONS / check_and_award_milestones). Keeps the
# dev-jargon "type ≥ value" out of the user-facing trophy screens; shared by
# /catalogo_badge and /traguardi so the two render conditions identically.
_CONDITION_TEMPLATES = {
    "onboarding": "Completa la registrazione",
    "balance": "Raggiungi {v:,} CoInn",
    "daily_streak": "Riscuoti il /daily per {v} giorni di fila",
    "bets_won": "Vinci {v} scommesse",
    "transfers_made": "Invia {v} trasferimenti",
    "xp": "Accumula {v:,} XP",
}


def describe_condition(condition_type: str | None, condition_value: int | None) -> str | None:
    """Plain-Italian unlock requirement for a trophy, or None if it has no
    machine condition (manually awarded). Pure presentation — no DB, no escaping
    (the value is numeric; the caller escapes any surrounding user text)."""
    if not condition_type:
        return None
    template = _CONDITION_TEMPLATES.get(condition_type)
    if template is None:
        return None
    return template.format(v=condition_value or 0)


async def sync_trophies(session: AsyncSession, rows: list[dict] | None = None) -> int:
    """Upsert the trophy catalog (CSV-driven, falling back to built-in defaults).

    Inserts missing trophies and refreshes the display/condition fields of existing
    ones, keyed by ``slug``. Returns the number of catalog entries. Commits.
    """
    if rows is None:
        rows = catalog_loader.load_trophies()
    for entry in rows:
        result = await session.execute(select(Badge).where(Badge.slug == entry["slug"]))
        badge = result.scalar_one_or_none()
        if badge is None:
            session.add(Badge(**entry))
        else:
            for field in _SYNC_FIELDS:
                if field in entry:
                    setattr(badge, field, entry[field])
    await session.commit()
    return len(rows)


async def seed_badges(session: AsyncSession) -> None:
    """Back-compat entry point used at startup / in tests — syncs the trophy catalog."""
    await sync_trophies(session)


async def award_badge(
    session: AsyncSession,
    user_tg_id: int,
    slug: str,
) -> tuple[UserBadge | None, bool]:
    """Award a specific badge by slug. Returns (user_badge, is_new).
    If the user already has it, returns (existing, False).
    Does NOT commit — caller handles commit.
    """
    badge_result = await session.execute(select(Badge).where(Badge.slug == slug))
    badge = badge_result.scalar_one_or_none()
    if badge is None:
        return None, False

    existing_result = await session.execute(
        select(UserBadge).where(
            UserBadge.user_tg_id == user_tg_id,
            UserBadge.badge_id == badge.id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        return existing, False

    ub = UserBadge(user_tg_id=user_tg_id, badge_id=badge.id)
    session.add(ub)
    return ub, True


async def check_and_award_milestones(
    session: AsyncSession,
    user_tg_id: int,
) -> list[Badge]:
    """Check all milestone conditions for the user and award any newly earned badges.
    Returns the list of newly earned Badge objects.
    Does NOT commit — caller handles commit.
    """
    user_result = await session.execute(
        select(User)
        .where(User.tg_id == user_tg_id)
        .options(selectinload(User.wallet), selectinload(User.badges))
    )
    user = user_result.scalar_one_or_none()
    if user is None or user.wallet is None:
        return []

    earned_badge_ids = {ub.badge_id for ub in user.badges}
    all_badges_result = await session.execute(select(Badge))
    all_badges = list(all_badges_result.scalars().all())

    newly_earned: list[Badge] = []

    for badge in all_badges:
        if badge.id in earned_badge_ids:
            continue
        if badge.condition_type is None or badge.condition_value is None:
            continue

        ct = badge.condition_type
        cv = badge.condition_value
        earned = False

        if ct == "onboarding" and user.onboarding_completed:
            earned = True
        elif ct == "balance" and user.wallet.coins >= cv:
            earned = True
        elif ct == "daily_streak" and user.daily_streak >= cv:
            earned = True
        elif ct == "bets_won" and user.bets_won >= cv:
            earned = True
        elif ct == "transfers_made" and user.transfers_made >= cv:
            earned = True
        elif ct == "xp" and user.xp >= cv:
            earned = True

        if earned:
            ub = UserBadge(user_tg_id=user_tg_id, badge_id=badge.id)
            session.add(ub)
            newly_earned.append(badge)

    return newly_earned


async def get_user_badges(
    session: AsyncSession,
    user_tg_id: int,
) -> list[UserBadge]:
    result = await session.execute(
        select(UserBadge)
        .where(UserBadge.user_tg_id == user_tg_id)
        .options(selectinload(UserBadge.badge))
    )
    return list(result.scalars().all())


async def get_all_badges(session: AsyncSession) -> list[Badge]:
    result = await session.execute(select(Badge).order_by(Badge.id))
    return list(result.scalars().all())


async def leaderboard_trophies(
    session: AsyncSession, limit: int = 10
) -> list[tuple[User, int]]:
    """Top users by number of trophies earned (tie-break: XP). Excludes 0-trophy users."""
    count = func.count(UserBadge.id)
    result = await session.execute(
        select(User, count)
        .join(UserBadge, UserBadge.user_tg_id == User.tg_id)
        .group_by(User.tg_id)
        .order_by(count.desc(), User.xp.desc())
        .limit(limit)
    )
    return [(row[0], row[1]) for row in result.all()]
