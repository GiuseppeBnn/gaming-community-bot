from aiogram import Router
from aiogram.filters.command import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from handlers._privacy import redirect_to_private
from services import badge_service, xp_service
from services.badge_service import RARITY_LABELS, RARITY_ORDER
from utils.text import chunk_blocks, esc

router = Router()


def _rarity_key(badge) -> tuple[int, int]:
    return (RARITY_ORDER.get(badge.rarity, 0), badge.id)


@router.message(Command("trofei", "traguardi"))
async def cmd_traguardi(message: Message, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "trofei", "🏆 Vedi i tuoi trofei"):
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
    prog = xp_service.level_for_xp(xp)
    rank = xp_service.rank_for_level(prog.level)

    if not all_badges:
        await message.answer("🏆 Nessun trofeo disponibile al momento.")
        return

    header = f"🏆 <b>I tuoi Trofei</b> ({len(earned_ids)}/{len(all_badges)})"
    rank_txt = f" · {rank.emoji} {esc(rank.name)}" if rank is not None else ""
    header += f"\n⚡ <b>Livello {prog.level}</b>{rank_txt}"
    if user:
        from services.shop_service import render_active_tags
        tags = render_active_tags(user)
        if tags:
            header += f"\n🏷️ <b>Tag:</b> {esc(tags)}"
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
            elif badge.hidden:
                # Secret trophy: masked until earned (name + requirement hidden).
                lines.append("🔒 <i>??? — Trofeo nascosto</i>")
            else:
                # Locked: show the plain-Italian unlock requirement (same wording
                # as /catalogo_badge) so the two screens stay consistent.
                cond = badge_service.describe_condition(
                    badge.condition_type, badge.condition_value, badge.condition_param
                )
                cond_txt = f"\n   <i>Come sbloccarlo: {esc(cond)}</i>" if cond else ""
                lines.append(f"🔒 <i>{esc(badge.name)}</i> — {esc(badge.description)}{cond_txt}")

    if not earned_ids:
        lines.append(
            "\n<i>Non hai ancora sbloccato nessun trofeo.</i>\n"
            "Partecipa a quiz ed eventi, accumula XP e CoInn!"
        )

    # The full catalogue (40+ trophies, each with its unlock hint) exceeds
    # Telegram's 4096-char limit, so split across messages on line boundaries.
    for chunk in chunk_blocks(lines, sep="\n"):
        await message.answer(chunk)


@router.message(Command("catalogo_badge"))
async def cmd_catalogo_badge(message: Message, db_session: AsyncSession) -> None:
    all_badges = await badge_service.get_all_badges(db_session)

    if not all_badges:
        await message.answer("📚 Catalogo trofei vuoto.")
        return

    # Need the caller's earned set so secret trophies they own are shown in full.
    user_badges = await badge_service.get_user_badges(db_session, message.from_user.id)
    earned_ids = {ub.badge_id for ub in user_badges}

    lines = ["📚 <b>Catalogo Trofei</b>\n"]
    for badge in sorted(all_badges, key=_rarity_key):
        rarity = RARITY_LABELS.get(badge.rarity, badge.rarity.title())
        if badge.hidden and badge.id not in earned_ids:
            # Secret trophy: masked until earned (only its rarity is revealed).
            lines.append(f"❓ <b>???</b> · {rarity}\n   <i>Trofeo nascosto</i>")
            continue
        # Plain-Italian unlock requirement instead of the dev-jargon "type ≥ value"
        # (shared wording with /trofei via badge_service.describe_condition).
        cond_text = badge_service.describe_condition(
            badge.condition_type, badge.condition_value, badge.condition_param
        )
        cond = f"\n   <i>Come sbloccarlo: {esc(cond_text)}</i>" if cond_text else ""
        lines.append(
            f"{badge.icon_emoji} <b>{esc(badge.name)}</b> · {rarity}\n"
            f"   {esc(badge.description)}{cond}"
        )

    for chunk in chunk_blocks(lines, sep="\n\n"):
        await message.answer(chunk)
