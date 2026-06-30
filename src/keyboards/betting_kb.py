from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import BettingEvent, BettingOption

PRESET_AMOUNTS = [50, 100, 250, 500]

_CLOSE_TEXT = "✖ Chiudi"
_CLOSE_CB = "bet:close"


def get_group_events_keyboard(events: list[BettingEvent], bot_username: str) -> InlineKeyboardMarkup:
    """URL buttons for group messages — tap opens private chat deep-link."""
    builder = InlineKeyboardBuilder()
    for event in events:
        builder.button(
            text=f"🎲 Scommetti su #{event.id} {event.title[:22]}",
            url=f"https://t.me/{bot_username}?start=bet_{event.id}",
        )
    builder.adjust(1)
    return builder.as_markup()


def get_events_keyboard(
    events: list[BettingEvent], placed_ids: set[int] = frozenset()
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for event in events:
        total = sum(o.total_wagered for o in event.options)
        prefix = "✅ " if event.id in placed_ids else ""
        builder.button(
            text=f"{prefix}#{event.id} {event.title[:30]} — {total} 🪙",
            callback_data=f"event:view:{event.id}",
        )
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_options_keyboard(
    event_id: int, options: list[BettingOption]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for opt in options:
        builder.button(
            text=f"{opt.label} — {opt.total_wagered} 🪙",
            callback_data=f"bet_option:{event_id}:{opt.id}",
        )
    builder.button(text="🔙 Indietro", callback_data="bet:back")
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_amount_keyboard(
    event_id: int, option_id: int, user_balance: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    affordable = [a for a in PRESET_AMOUNTS if a <= user_balance]
    for amount in affordable:
        builder.button(
            text=f"{amount} 🪙",
            callback_data=f"bet_amount:{event_id}:{option_id}:{amount}",
        )
    builder.button(
        text="✏️ Importo personalizzato",
        callback_data=f"bet_custom:{event_id}:{option_id}",
    )
    builder.button(text="🔙 Indietro", callback_data=f"event:view:{event_id}")
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    n = len(affordable)
    if n >= 2:
        builder.adjust(2, 1, 1, 1)
    else:
        builder.adjust(1)
    return builder.as_markup()


def get_confirm_bet_keyboard(
    event_id: int, option_id: int, amount: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ Conferma {amount} 🪙",
        callback_data=f"bet_confirm:{event_id}:{option_id}:{amount}",
    )
    builder.button(
        text="🔙 Cambia importo",
        callback_data=f"bet_option:{event_id}:{option_id}",
    )
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()
