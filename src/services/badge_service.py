from __future__ import annotations

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import Badge, User, UserBadge
from services import (
    catalog_loader,
    consumable_service,
    progress_service,
    shop_service,
    xp_service,
)

log = logging.getLogger(__name__)

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
    "xp_reward", "condition_type", "condition_value", "condition_param", "hidden",
)

# Plain-text "how to unlock" templates for the **un-parametrized counter**
# conditions. Keeps the dev-jargon "type ≥ value" out of the user-facing trophy
# screens; shared by /catalogo_badge and /trofei so they render identically.
_CONDITION_TEMPLATES = {
    "onboarding": "Completa la registrazione",
    "balance": "Raggiungi {v:,} CoInn",
    "daily_streak": "Riscuoti il /daily per {v} giorni di fila",
    "bets_won": "Vinci {v} scommesse",
    "transfers_made": "Invia {v} trasferimenti",
    "xp": "Accumula {v:,} XP",
    "level": "Raggiungi il livello {v}",
}


def _times(value: int) -> str:
    return "almeno 1 volta" if value <= 1 else f"{value} volte"


def describe_condition(
    condition_type: str | None,
    condition_value: int | None,
    condition_param: str | None = None,
) -> str | None:
    """Plain-Italian unlock requirement for a trophy, or None if it has no machine
    condition (manually awarded). Pure presentation — no DB, no escaping (values are
    numeric / curated catalog names; the caller escapes any surrounding user text).
    Resolves item/category/game names from the in-memory registries."""
    if not condition_type:
        return None
    v = condition_value or 0

    template = _CONDITION_TEMPLATES.get(condition_type)
    if template is not None:
        return template.format(v=v)

    if condition_type == "item_purchases":
        item = consumable_service.get_item(condition_param or "")
        name = item.name if item else "questo oggetto"
        return f"Acquista {name} {_times(v)}"
    if condition_type == "category_purchases":
        cat = consumable_service.get_category(condition_param or "")
        name = cat.name if cat else "questa categoria"
        return f"Acquista {v} consumabili — {name}"
    if condition_type == "shop_purchases":
        return f"Acquista {v} consumabili nella Locanda"
    if condition_type == "event_count":
        when = "per la prima volta" if v <= 1 else f"{v} volte"
        template = progress_service.EVENT_LABELS.get(condition_param or "")
        if template is None:  # unknown/missing metric → generic fallback (never None)
            return f"Completa l'obiettivo {when}"
        return template.format(when=when)
    if condition_type == "catalog_complete":
        return "Possiedi ogni oggetto del negozio: consumabili e cosmetici"
    if condition_type == "all_trophies":
        return "Sblocca tutti gli altri trofei"
    if condition_type == "podium_count":
        game = progress_service.GAME_LABELS.get(condition_param or "", "un gioco")
        when = "per la prima volta" if v <= 1 else f"{v} volte"
        return f"Raggiungi il podio {when} nel {game}"
    if condition_type == "first_place_count":
        game = progress_service.GAME_LABELS.get(condition_param or "", "un gioco")
        when = "per la prima volta" if v <= 1 else f"{v} volte"
        return f"Arriva 1° {when} nel {game}"
    if condition_type == "collection":
        n = len([s for s in (condition_param or "").split(";") if s])
        return f"Sblocca tutti i {n} trofei richiesti" if n else "Sblocca i trofei richiesti"
    return None


async def sync_trophies(session: AsyncSession, rows: list[dict] | None = None) -> int:
    """Reconcile the trophy catalog to the canonical source (CSV-driven, falling
    back to built-in defaults) — the catalog is the single source of truth.

    Inserts missing trophies, refreshes the display/condition fields of existing
    ones (keyed by ``slug``), and **prunes** any trophy whose slug is no longer in
    the catalog, cascading its ``user_badges``. Returns the number of catalog
    entries. Commits.
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

    # Prune trophies dropped from the catalog so the DB matches it exactly. Flush
    # first (autoflush is off) so freshly-inserted slugs are visible to the query.
    await session.flush()
    canonical = {entry["slug"] for entry in rows}
    existing = (await session.execute(select(Badge.id, Badge.slug))).all()
    stale_ids = [bid for bid, slug in existing if slug not in canonical]
    if stale_ids:
        # Delete the user_badges explicitly (DB-agnostic — don't rely on ON DELETE
        # CASCADE, which SQLite enforces only with PRAGMA foreign_keys=ON).
        await session.execute(delete(UserBadge).where(UserBadge.badge_id.in_(stale_ids)))
        await session.execute(delete(Badge).where(Badge.id.in_(stale_ids)))
        stale_slugs = sorted(slug for _, slug in existing if slug not in canonical)
        log.info("Pruned %d trofei fuori catalogo: %s", len(stale_ids), ", ".join(stale_slugs))

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


# Condition families → which aggregate query (if any) they need. Drives the lazy
# computation: a query runs only when a candidate badge actually needs it.
# Counter conditions read User/Wallet directly (``level`` is derived from XP).
_COUNTER_CONDITIONS = frozenset(
    {"onboarding", "balance", "daily_streak", "bets_won", "transfers_made", "xp", "level"}
)
_PURCHASE_CONDITIONS = frozenset({"item_purchases", "category_purchases", "shop_purchases"})
_PODIUM_CONDITIONS = frozenset({"podium_count", "first_place_count"})
_EVENT_CONDITIONS = frozenset({"event_count"})


def _counter_met(user: User, ct: str, cv: int) -> bool:
    if ct == "onboarding":
        return user.onboarding_completed
    if ct == "balance":
        return user.wallet is not None and user.wallet.coins >= cv
    if ct == "daily_streak":
        return user.daily_streak >= cv
    if ct == "bets_won":
        return user.bets_won >= cv
    if ct == "transfers_made":
        return user.transfers_made >= cv
    if ct == "xp":
        return user.xp >= cv
    if ct == "level":
        return xp_service.level_for_xp(user.xp).level >= cv
    return False


async def check_and_award_milestones(
    session: AsyncSession,
    user_tg_id: int,
) -> list[Badge]:
    """Check every trophy condition for the user and award the newly earned ones.

    Data-driven (no per-type ``if/elif`` sprawl outside the small dispatch helpers):
    counter conditions read User/Wallet; ``item/category/shop_purchases`` read the
    consumable purchase counts; ``podium_count/first_place_count`` read the game
    podium tallies; ``collection`` unlocks on owning other trophies. Aggregates are
    computed **once and only if needed**. Returns the newly earned Badge objects.
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

    all_badges = list((await session.execute(select(Badge))).scalars().all())
    earned_ids = {ub.badge_id for ub in user.badges}
    earned_slugs = {b.slug for b in all_badges if b.id in earned_ids}

    candidates = [
        b for b in all_badges if b.id not in earned_ids and b.condition_type is not None
    ]
    if not candidates:
        return []
    present_types = {b.condition_type for b in candidates}

    # Lazy aggregates — one query each, only when a candidate needs it.
    # ``catalog_complete`` needs the consumable counts too (every item bought ≥1).
    needs_counts = present_types & (_PURCHASE_CONDITIONS | {"catalog_complete"})
    counts = (
        await consumable_service.purchase_counts(session, user_tg_id)
        if needs_counts else {}
    )
    podium = (
        await progress_service.podium_counts(session, user_tg_id)
        if present_types & _PODIUM_CONDITIONS else {}
    )
    events = (
        await progress_service.event_counts(session, user_tg_id)
        if present_types & _EVENT_CONDITIONS else {}
    )
    cosmetics_owned = (
        await shop_service.owned_cosmetic_keys(session, user_tg_id)
        if "catalog_complete" in present_types else set()
    )

    def _meets(badge: Badge) -> bool:
        ct, cv, cp = badge.condition_type, badge.condition_value, badge.condition_param
        if ct in _COUNTER_CONDITIONS:
            return cv is not None and _counter_met(user, ct, cv)
        if ct == "item_purchases":
            return cv is not None and cp is not None and counts.get(cp, 0) >= cv
        if ct == "category_purchases":
            return (
                cv is not None and cp is not None
                and consumable_service.category_total(counts, cp) >= cv
            )
        if ct == "shop_purchases":
            return cv is not None and consumable_service.total_purchases(counts) >= cv
        if ct == "event_count":
            return cv is not None and bool(cp) and events.get(cp, 0) >= cv
        if ct == "catalog_complete":
            consumables = catalog_loader.get_consumables()
            cosmetics = catalog_loader.get_cosmetics()
            return (
                bool(consumables) and bool(cosmetics)
                and all(counts.get(k, 0) >= 1 for k in consumables)
                and all(k in cosmetics_owned for k in cosmetics)
            )
        if ct == "all_trophies":
            # Own every *other* auto-conditioned trophy. Manual trophies (no
            # condition_type, e.g. the Discord placeholder) are excluded, so the
            # platinum stays obtainable. The fixpoint loop lets it unlock in the
            # same pass as the last collection it depends on.
            required = {
                b.slug for b in all_badges if b.condition_type and b.slug != badge.slug
            }
            return bool(required) and required <= earned_slugs
        if ct == "podium_count":
            tally = podium.get(cp or "any", progress_service.PodiumTally())
            return cv is not None and tally.podiums >= cv
        if ct == "first_place_count":
            tally = podium.get(cp or "any", progress_service.PodiumTally())
            return cv is not None and tally.first_places >= cv
        if ct == "collection":
            required = [s for s in (cp or "").split(";") if s]
            return bool(required) and all(s in earned_slugs for s in required)
        return False

    newly_earned: list[Badge] = []
    pending = list(candidates)
    # Fixpoint: a `collection` can unlock in the same call as the last prerequisite
    # it depends on (awarded earlier this pass). Counter/purchase/podium conditions
    # don't change between passes, so they settle on the first pass.
    for _ in range(len(candidates) + 1):
        progressed = False
        remaining: list[Badge] = []
        for badge in pending:
            if _meets(badge):
                session.add(UserBadge(user_tg_id=user_tg_id, badge_id=badge.id))
                newly_earned.append(badge)
                earned_slugs.add(badge.slug)
                progressed = True
            else:
                remaining.append(badge)
        pending = remaining
        if not progressed or not pending:
            break

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
