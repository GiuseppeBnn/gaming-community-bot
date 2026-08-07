from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import BettingEvent, BettingOption, EventStatus
from handlers.callbacks import AdminBetCb

_CLOSE_TEXT = "✖ Chiudi"
_CLOSE_CB = AdminBetCb(action="close").pack()


def get_admin_events_keyboard(events: list[BettingEvent]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for event in events:
        total = sum(o.total_wagered for o in event.options)
        bets_count = len(event.user_bets)
        icon = "🟢" if event.status == EventStatus.open.value else "🔒"
        builder.button(
            text=f"{icon} #{event.id} {event.title[:26]} · {bets_count}bet · {total}🪙",
            callback_data=AdminBetCb(action="event", event_id=event.id).pack(),
        )
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_admin_event_menu_keyboard(event_id: int, status: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if status == EventStatus.open.value:
        builder.button(
            text="🔒 Blocca scommesse",
            callback_data=AdminBetCb(action="lock", event_id=event_id).pack(),
        )
    builder.button(
        text="🏁 Dichiara vincitore",
        callback_data=AdminBetCb(action="resolve", event_id=event_id).pack(),
    )
    builder.button(
        text="❌ Annulla e rimborsa tutto",
        callback_data=AdminBetCb(action="cancel", event_id=event_id).pack(),
    )
    builder.button(text="🔙 Indietro", callback_data=AdminBetCb(action="list").pack())
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_admin_resolve_options_keyboard(
    event_id: int, options: list[BettingOption]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for opt in options:
        builder.button(
            text=f"🏆 {opt.label} — {opt.total_wagered} 🪙",
            callback_data=AdminBetCb(
                action="pick_winner", event_id=event_id, option_id=opt.id
            ).pack(),
        )
    builder.button(
        text="🔙 Indietro", callback_data=AdminBetCb(action="event", event_id=event_id).pack()
    )
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_admin_confirm_resolve_keyboard(event_id: int, option_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Conferma e distribuisci",
        callback_data=AdminBetCb(
            action="confirm_resolve", event_id=event_id, option_id=option_id
        ).pack(),
    )
    builder.button(
        text="🔙 Indietro",
        callback_data=AdminBetCb(action="resolve", event_id=event_id).pack(),
    )
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_admin_confirm_lock_keyboard(event_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔒 Sì, blocca",
        callback_data=AdminBetCb(action="confirm_lock", event_id=event_id).pack(),
    )
    builder.button(
        text="🔙 Indietro", callback_data=AdminBetCb(action="event", event_id=event_id).pack()
    )
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_admin_confirm_cancel_keyboard(event_id: int, pending_count: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"❌ Sì, annulla e rimborsa {pending_count} scommesse",
        callback_data=AdminBetCb(action="confirm_cancel", event_id=event_id).pack(),
    )
    builder.button(
        text="🔙 Indietro", callback_data=AdminBetCb(action="event", event_id=event_id).pack()
    )
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()
