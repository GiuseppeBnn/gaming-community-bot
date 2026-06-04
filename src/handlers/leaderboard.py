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

from services import admin_service, badge_service, xp_service

router = Router()

_MEDALS = ("🥇", "🥈", "🥉")
_BOARDS = ("coins", "xp", "trofei")
_TABS = (("💰 Ricchezza", "coins"), ("⚡ XP", "xp"), ("🏆 Trofei", "trofei"))


def _name(user) -> str:
    return f"@{user.username}" if user.username else user.full_name


def _kb(active: str):
    b = InlineKeyboardBuilder()
    for label, key in _TABS:
        b.button(text=f"• {label} •" if key == active else label, callback_data=f"lead:{key}")
    b.button(text="✖ Chiudi", callback_data="lead:close")
    b.adjust(3, 1)
    return b.as_markup()


async def render_board(db_session: AsyncSession, board: str) -> str:
    if board == "xp":
        rows = await xp_service.leaderboard_xp(db_session)
        title, unit = "⚡ <b>Classifica XP</b>", "XP"
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
        rank = _MEDALS[i] if i < 3 else f"{i + 1}."
        lines.append(f"{rank} {_name(user)} — <b>{value:,} {unit}</b>")
    return "\n".join(lines)


@router.message(Command("classifiche"))
async def cmd_classifiche(message: Message, db_session: AsyncSession) -> None:
    text = await render_board(db_session, "coins")
    await message.answer(text, reply_markup=_kb("coins"))


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
