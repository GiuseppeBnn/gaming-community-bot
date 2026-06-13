"""
Shared helper to keep personal data out of group chats.

Commands that reveal a single user's private info (balance, history, profile,
trophies, shop with its balance) must not answer in the group where everyone can
read them. `redirect_to_private` replies with a deep-link button that opens the
bot's private chat on the right screen, reusing the existing ``?start=<payload>``
dispatch in common.cmd_start.
"""

from __future__ import annotations

from aiogram.enums import ChatType
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message


async def redirect_to_private(
    message: Message,
    payload: str,
    button_text: str = "🔓 Apri in privato",
    *,
    notice: str = "🔒 Questo comando mostra dati personali: continua in chat privata.",
) -> bool:
    """If `message` is not in a private chat, reply with a t.me deep-link button
    and return True (the caller must then return). In private chat: return False.

    `notice` overrides the default reply text (e.g. for admin-only commands that
    aren't about personal data but must still stay out of the group).
    """
    if message.chat.type == ChatType.PRIVATE:
        return False
    bot_info = await message.bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=button_text,
            url=f"https://t.me/{bot_info.username}?start={payload}",
        )
    ]])
    await message.reply(notice, reply_markup=kb)
    return True
