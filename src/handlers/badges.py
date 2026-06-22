from aiogram import Router
from aiogram.filters.command import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from handlers._privacy import redirect_to_private
from services import badge_service, xp_service
from services.badge_service import RARITY_HEADERS, RARITY_LABELS, RARITY_ORDER
from utils.text import chunk_blocks, esc

router = Router()

# Drawn between rarity tiers to mirror note.txt's layout; precedes every section
# header except the first.
_TIER_SEP = "————————————————"


def _rarity_key(badge) -> tuple[int, int]:
    return (RARITY_ORDER.get(badge.rarity, 0), badge.id)


def _trophy_block(badge, *, earned: bool) -> str:
    """One trophy entry: ``<status> <name> — <description>`` (no per-trophy emoji
    or rarity label — those live in the section header). A secret trophy the caller
    hasn't earned is masked; once earned it renders in full like any other."""
    if badge.hidden and not earned:
        return "🔒 <i>??? — Trofeo nascosto</i>"
    # The curated description is the canonical unlock text (note.txt). Fall back to
    # the generated requirement only if a trophy left it blank, so an entry is never
    # shown without its goal.
    desc = (badge.description or "").strip() or (
        badge_service.describe_condition(
            badge.condition_type, badge.condition_value, badge.condition_param
        )
        or ""
    )
    tail = f" — {esc(desc)}" if desc else ""
    if earned:
        return f"✅ <b>{esc(badge.name)}</b>{tail}"
    return f"🔒 <i>{esc(badge.name)}</i>{tail}"


def _grouped_trophy_blocks(badges, earned_ids: set[int]) -> list[str]:
    """Trophy entries grouped by rarity (platinum → bronze) under note.txt-style
    section headers. Each entry is a standalone block so ``chunk_blocks`` packs them
    into Telegram-sized messages with a blank line between trophies."""
    by_rarity: dict[str, list] = {}
    for badge in sorted(badges, key=_rarity_key):
        by_rarity.setdefault(badge.rarity, []).append(badge)

    blocks: list[str] = []
    first = True
    for rarity in sorted(by_rarity, key=lambda r: RARITY_ORDER.get(r, 0), reverse=True):
        header = RARITY_HEADERS.get(rarity, RARITY_LABELS.get(rarity, rarity.title()))
        # Glue the separator to the header (single newline) so a chunk break can't
        # strand one without the other.
        blocks.append(header if first else f"{_TIER_SEP}\n{header}")
        first = False
        for badge in by_rarity[rarity]:
            blocks.append(_trophy_block(badge, earned=badge.id in earned_ids))
    return blocks


@router.message(Command("trofei", "traguardi"))
async def cmd_traguardi(message: Message, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "trofei", "🏆 Vedi i tuoi trofei"):
        return
    await show_traguardi(message, db_session)


async def show_traguardi(message: Message, db_session: AsyncSession) -> None:
    """Render the caller's *unlocked* trophies (private chat only). The full list —
    locked entries included — lives in /catalogo_trofei; this personal screen shows
    only what the user has actually earned (secret trophies revealed once owned)."""
    user_badges = await badge_service.get_user_badges(db_session, message.from_user.id)
    all_badges = await badge_service.get_all_badges(db_session)

    if not all_badges:
        await message.answer("🏆 Nessun trofeo disponibile al momento.")
        return

    # Keep only earned trophies that still exist in the catalog (a pruned trophy
    # leaves no stale row here), so the count and the list can never disagree.
    earned_ids = {ub.badge_id for ub in user_badges}
    earned_badges = [b for b in all_badges if b.id in earned_ids]

    user_result = await db_session.execute(
        select(User).where(User.tg_id == message.from_user.id)
    )
    user = user_result.scalar_one_or_none()
    xp = user.xp if user else 0
    prog = xp_service.level_for_xp(xp)
    rank = xp_service.rank_for_level(prog.level)

    header = f"🏆 <b>I tuoi Trofei</b> ({len(earned_badges)}/{len(all_badges)})"
    rank_txt = f" · {rank.emoji} {esc(rank.name)}" if rank is not None else ""
    header += f"\n⚡ <b>Livello {prog.level}</b>{rank_txt}"
    if user:
        from services.shop_service import render_active_tags
        tags = render_active_tags(user)
        if tags:
            header += f"\n🏷️ <b>Tag:</b> {esc(tags)}"

    if earned_badges:
        blocks = [header, *_grouped_trophy_blocks(earned_badges, earned_ids)]
    else:
        blocks = [
            header,
            "<i>Non hai ancora sbloccato nessun trofeo.</i>\n"
            "Partecipa a quiz ed eventi, accumula XP e CoInn!\n"
            "Sfoglia /catalogo_trofei per scoprirli tutti.",
        ]

    # Earned trophies can still exceed Telegram's 4096-char limit, so split across
    # messages on block boundaries, a blank line between each entry.
    for chunk in chunk_blocks(blocks, sep="\n\n"):
        await message.answer(chunk)


@router.message(Command("catalogo_trofei", "catalogo_badge"))
async def cmd_catalogo_trofei(message: Message, db_session: AsyncSession) -> None:
    all_badges = await badge_service.get_all_badges(db_session)

    if not all_badges:
        await message.answer("📚 Catalogo trofei vuoto.")
        return

    # Public catalog: identical for everyone, so secret trophies stay masked here
    # (their details are revealed only in the personal /trofei once earned).
    blocks = ["📚 <b>Catalogo Trofei</b>", *_grouped_trophy_blocks(all_badges, set())]
    for chunk in chunk_blocks(blocks, sep="\n\n"):
        await message.answer(chunk)
