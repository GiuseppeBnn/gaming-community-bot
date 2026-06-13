"""Small shared inline keyboards reused across handlers."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def confirm_cancel_kb(
    yes_cb: str,
    no_cb: str,
    *,
    yes_text: str = "✅ Sì, annulla",
    no_text: str = "↩️ No, continua",
) -> InlineKeyboardMarkup:
    """Yes/No keyboard for a "are you sure you want to cancel?" safety prompt."""
    b = InlineKeyboardBuilder()
    b.button(text=yes_text, callback_data=yes_cb)
    b.button(text=no_text, callback_data=no_cb)
    b.adjust(2)
    return b.as_markup()
