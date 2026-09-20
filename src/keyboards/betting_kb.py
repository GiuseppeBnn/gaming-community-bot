from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import BettingEvent, BettingOption
from handlers.callbacks import BetAmountCb, BetCb, BetConfirmCb, BetCustomCb, BetEventCb, BetOptionCb

PRESET_AMOUNTS = [50, 100, 250, 500]

_CLOSE_TEXT = "✖ Chiudi"
_CLOSE_CB = BetCb(action="close").pack()


# NOTE: the former `get_group_events_keyboard` (URL buttons posted in the group)
# was removed: it marked «✅ Hai già scommesso» per event based on the *caller's*
# bets, inside a message everyone in the group could read. /scommesse is now
# private-only and redirects from the group (§9).


def get_events_keyboard(
    events: list[BettingEvent], placed_ids: set[int] = frozenset()
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for event in events:
        total = sum(o.total_wagered for o in event.options)
        prefix = "✅ " if event.id in placed_ids else ""
        builder.button(
            text=f"{prefix}#{event.id} {event.title[:30]} — {total} 🪙",
            callback_data=BetEventCb(action="view", event_id=event.id).pack(),
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
            callback_data=BetOptionCb(action="pick", event_id=event_id, option_id=opt.id).pack(),
        )
    builder.button(text="🔙 Indietro", callback_data=BetCb(action="back").pack())
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
            callback_data=BetAmountCb(
                action="pick", event_id=event_id, option_id=option_id, amount=amount
            ).pack(),
        )
    builder.button(
        text="✏️ Importo personalizzato",
        callback_data=BetCustomCb(action="open", event_id=event_id, option_id=option_id).pack(),
    )
    builder.button(text="🔙 Indietro", callback_data=BetEventCb(action="view", event_id=event_id).pack())
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
        callback_data=BetConfirmCb(
            action="place", event_id=event_id, option_id=option_id, amount=amount
        ).pack(),
    )
    builder.button(
        text="🔙 Cambia importo",
        callback_data=BetOptionCb(action="pick", event_id=event_id, option_id=option_id).pack(),
    )
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()
