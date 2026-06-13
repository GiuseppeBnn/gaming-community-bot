from aiogram import Router
from aiogram.filters.command import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from handlers._privacy import redirect_to_private
from services import badge_service, xp_service
from services.badge_service import RARITY_LABELS, RARITY_ORDER
from utils.text import esc

router = Router()


def _rarity_key(badge) -> tuple[int, int]:
    return (RARITY_ORDER.get(badge.rarity, 0), badge.id)


@router.message(Command("traguardi"))
async def cmd_traguardi(message: Message, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "traguardi", "🏆 Vedi i tuoi trofei"):
        return
    await show_traguardi(message, db_session)


async def show_traguardi(message: Message, db_session: AsyncSession) -> None:
    """Render the caller's trophies (private chat only)."""
    user_badges = await badge_service.get_user_badges(db_session, message.from_user.id)
    all_badges = await badge_service.get_all_badges(db_session)
    earned_ids = {ub.badge_id for ub in user_badges}

    user_result = await db_session.execute(
        select(User).where(User.tg_id == message.from_user.id)
    )
    user = user_result.scalar_one_or_none()
    xp = user.xp if user else 0
    rank = xp_service.rank_for_xp(xp)

    if not all_badges:
        await message.answer("🏆 Nessun trofeo disponibile al momento.")
        return

    header = f"🏆 <b>I tuoi Trofei</b> ({len(earned_ids)}/{len(all_badges)})"
    if rank is not None:
        header += f"\n{rank.emoji} <b>Rango:</b> {esc(rank.name)} · ⚡ <b>{xp} XP</b>"
    if user and user.cosmetic_tag:
        header += f"\n🏷️ <b>Tag:</b> {esc(user.cosmetic_tag)}"
    lines = [header]

    # Group by rarity tier (platinum → bronze for prestige first).
    by_rarity: dict[str, list] = {}
    for badge in sorted(all_badges, key=_rarity_key):
        by_rarity.setdefault(badge.rarity, []).append(badge)

    for rarity in sorted(by_rarity, key=lambda r: RARITY_ORDER.get(r, 0), reverse=True):
        lines.append(f"\n{RARITY_LABELS.get(rarity, rarity.title())}")
        for badge in by_rarity[rarity]:
            if badge.id in earned_ids:
                lines.append(f"{badge.icon_emoji} <b>{esc(badge.name)}</b> — {esc(badge.description)}")
            else:
                lines.append(f"🔒 <i>{esc(badge.name)}</i> — {esc(badge.description)}")

    if not earned_ids:
        lines.append(
            "\n<i>Non hai ancora sbloccato nessun trofeo.</i>\n"
            "Partecipa a quiz ed eventi, accumula XP e Aldueuri!"
        )

    await message.answer("\n".join(lines))


@router.message(Command("catalogo_badge"))
async def cmd_catalogo_badge(message: Message, db_session: AsyncSession) -> None:
    all_badges = await badge_service.get_all_badges(db_session)

    if not all_badges:
        await message.answer("📚 Catalogo trofei vuoto.")
        return

    lines = ["📚 <b>Catalogo Trofei</b>\n"]
    for badge in sorted(all_badges, key=_rarity_key):
        cond = ""
        if badge.condition_type and badge.condition_value is not None:
            cond = f"\n   <i>Condizione: {badge.condition_type} ≥ {badge.condition_value}</i>"
        rarity = RARITY_LABELS.get(badge.rarity, badge.rarity.title())
        lines.append(
            f"{badge.icon_emoji} <b>{esc(badge.name)}</b> · {rarity}\n"
            f"   {esc(badge.description)}{cond}"
        )

    await message.answer("\n\n".join(lines))
