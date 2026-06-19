"""
User-facing leaderboards — /classifiche with an inline switcher (coins · XP · trofei).

Public command (callbacks under the ``lead:*`` namespace); each board reuses an
existing service query, so there is no duplicated ranking logic.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters.command import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from services import admin_service, badge_service, xp_service
from utils import cooldown
from utils.text import esc

router = Router()

_MEDALS = ("🥇", "🥈", "🥉")
_BOARDS = ("coins", "xp", "trofei")
_TABS = (("💰 Ricchezza", "coins"), ("⚡ XP", "xp"), ("🏆 Trofei", "trofei"))


def _name(user) -> str:
    from services.shop_service import render_active_tags
    base = f"@{esc(user.username)}" if user.username else esc(user.full_name)
    tags = render_active_tags(user)
    return f"{esc(tags)} {base}" if tags else base


def _kb(active: str):
    b = InlineKeyboardBuilder()
    for label, key in _TABS:
        b.button(text=f"• {label} •" if key == active else label, callback_data=f"lead:{key}")
    b.button(text="✖ Chiudi", callback_data="lead:close")
    b.adjust(3, 1)
    return b.as_markup()


async def render_board(db_session: AsyncSession, board: str) -> str:
    unit = ""
    if board == "xp":
        rows = await xp_service.leaderboard_xp(db_session)
        title = "⚡ <b>Classifica Livelli</b>"
    elif board == "trofei":
        rows = await badge_service.leaderboard_trophies(db_session)
        title, unit = "🏆 <b>Classifica Trofei</b>", "🏆"
    else:
        rows = await admin_service.leaderboard(db_session)
        title, unit = "💰 <b>Classifica Ricchezza</b>", "🪙"

    if not rows:
        return f"{title}\n\n<i>Nessun dato ancora.</i>"

    lines = [title, ""]
    for i, (user, value) in enumerate(rows):
        medal = _MEDALS[i] if i < 3 else f"{i + 1}."
        if board == "xp":
            prog = xp_service.level_for_xp(value)
            tier = xp_service.rank_for_level(prog.level)
            tier_txt = f" {tier.emoji}" if tier else ""
            val = f"<b>Livello {prog.level}</b>{tier_txt}"
        else:
            val = f"<b>{value:,} {unit}</b>"
        lines.append(f"{medal} {_name(user)} — {val}")
    return "\n".join(lines)


async def show_board_private(message: Message, db_session: AsyncSession) -> None:
    """Render the leaderboard with its inline switcher (used in private chat and
    from the ``?start=classifiche`` deep-link)."""
    if not await cooldown.guard(message, "classifiche", settings.command_cooldown_seconds):
        return
    text = await render_board(db_session, "coins")
    await message.answer(text, reply_markup=_kb("coins"))


@router.message(Command("classifiche"))
async def cmd_classifiche(message: Message, db_session: AsyncSession) -> None:
    # Interactive switcher + close button: in a group everyone sees it and anyone
    # could close it → always show it privately (deep-link from the group).
    from handlers._privacy import redirect_to_private
    if await redirect_to_private(message, "classifiche", "📊 Vedi le classifiche"):
        return
    await show_board_private(message, db_session)


@router.callback_query(F.data.startswith("lead:"))
async def cb_lead(callback: CallbackQuery, db_session: AsyncSession) -> None:
    board = callback.data[len("lead:"):]
    if board == "close":
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer()
        return
    if board not in _BOARDS:
        await callback.answer()
        return
    text = await render_board(db_session, board)
    try:
        await callback.message.edit_text(text, reply_markup=_kb(board))
    except Exception:
        pass
    await callback.answer()
