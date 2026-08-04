"""
Keyboards for the button-driven admin dashboard (``AdminCb``, prefix ``adm``).

`AdminCb` always carries ``action``, ``key`` and ``item_id``. Optional values
keep their empty separators: ``users`` packs as ``adm:users::<page>`` and the
per-user ``act``/``ask``/``do`` actions pack their verb in ``key`` and Telegram
id in ``item_id``.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from handlers.callbacks import AdminCb, EventCb

PAGE_SIZE = 8


def home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 Statistiche", callback_data=AdminCb(action="stats").pack())
    b.button(text="🏆 Classifica", callback_data=AdminCb(action="lead").pack())
    b.button(text="🎬 Eventi", callback_data=EventCb(action="home").pack())
    b.button(text="👥 Utenti", callback_data=AdminCb(action="users", item_id=0).pack())
    b.button(text="💰 Economia", callback_data=AdminCb(action="econ").pack())
    b.button(text="🧾 Audit", callback_data=AdminCb(action="audit").pack())
    b.button(text="❓ Comandi", callback_data=AdminCb(action="help").pack())
    b.button(text="✖ Chiudi", callback_data=AdminCb(action="close").pack())
    b.adjust(2, 1, 2, 2, 1)
    return b.as_markup()


def back_home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Home", callback_data=AdminCb(action="home").pack())
    b.button(text="✖ Chiudi", callback_data=AdminCb(action="close").pack())
    b.adjust(2)
    return b.as_markup()


def econ_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎁 Airdrop monete", callback_data=AdminCb(action="airdrop").pack())
    b.button(text="⚡ Airdrop XP", callback_data=AdminCb(action="xpairdrop").pack())
    b.button(text="👤 Gestisci un utente", callback_data=AdminCb(action="users", item_id=0).pack())
    b.button(text="⬅️ Home", callback_data=AdminCb(action="home").pack())
    b.adjust(1)
    return b.as_markup()


def lead_kb(active: str) -> InlineKeyboardMarkup:
    """Leaderboard switcher inside the dashboard (coins · xp · trofei)."""
    b = InlineKeyboardBuilder()
    for label, key in (("💰 Ricchezza", "coins"), ("⚡ XP", "xp"), ("🏆 Trofei", "trofei")):
        b.button(
            text=f"• {label} •" if key == active else label,
            callback_data=AdminCb(action="lead_board", key=key).pack(),
        )
    b.button(text="⬅️ Home", callback_data=AdminCb(action="home").pack())
    b.adjust(3, 1)
    return b.as_markup()


def users_kb(users, page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Paginated user picker. `users` is a list of (User, coins) tuples."""
    b = InlineKeyboardBuilder()
    for user, coins in users:
        name = f"@{user.username}" if user.username else user.full_name[:24]
        b.button(
            text=f"👤 {name} · {coins:,}🪙",
            callback_data=AdminCb(action="user", item_id=user.tg_id).pack(),
        )
    b.button(text="🔍 Cerca", callback_data=AdminCb(action="search").pack())
    nav = []
    if page > 0:
        nav.append(("⬅️ Prec", AdminCb(action="users", item_id=page - 1).pack()))
    if has_next:
        nav.append(("Succ ➡️", AdminCb(action="users", item_id=page + 1).pack()))
    for text, cb in nav:
        b.button(text=text, callback_data=cb)
    b.button(text="🏠 Home", callback_data=AdminCb(action="home").pack())
    # rows: one per user, then [cerca], optional [prev/next], then [home]
    layout = [1] * len(users) + [1] + ([len(nav)] if nav else []) + [1]
    b.adjust(*layout)
    return b.as_markup()


def user_detail_kb(tg_id: int, group_enabled: bool) -> InlineKeyboardMarkup:
    """Per-user action buttons. Moderation actions require a configured group."""
    b = InlineKeyboardBuilder()
    b.button(text="💰 Credita", callback_data=AdminCb(action="act", key="credit", item_id=tg_id).pack())
    b.button(text="🔻 Addebita", callback_data=AdminCb(action="act", key="debit", item_id=tg_id).pack())
    b.button(text="⚖️ Set saldo", callback_data=AdminCb(action="act", key="setbal", item_id=tg_id).pack())
    b.button(text="⚡ Dai XP", callback_data=AdminCb(action="act", key="xpgrant", item_id=tg_id).pack())
    b.button(text="⚡ Set XP", callback_data=AdminCb(action="act", key="xpset", item_id=tg_id).pack())
    counts = [3, 2]
    if group_enabled:
        b.button(text="⛔ Ban", callback_data=AdminCb(action="ask", key="ban", item_id=tg_id).pack())
        b.button(text="👢 Kick", callback_data=AdminCb(action="ask", key="kick", item_id=tg_id).pack())
        b.button(text="✅ Sban", callback_data=AdminCb(action="do", key="sban", item_id=tg_id).pack())
        b.button(text="🔇 Mute", callback_data=AdminCb(action="act", key="mute", item_id=tg_id).pack())
        b.button(text="🔊 Unmute", callback_data=AdminCb(action="do", key="unmute", item_id=tg_id).pack())
        b.button(text="⚠️ Warn", callback_data=AdminCb(action="act", key="warn", item_id=tg_id).pack())
        b.button(text="♻️ Unwarn", callback_data=AdminCb(action="do", key="unwarn", item_id=tg_id).pack())
        counts += [2, 3, 2]
    b.button(text="⬅️ Lista", callback_data=AdminCb(action="users", item_id=0).pack())
    b.button(text="✖ Chiudi", callback_data=AdminCb(action="close").pack())
    counts += [2]
    b.adjust(*counts)
    return b.as_markup()


def confirm_kb(action: str, tg_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Conferma", callback_data=AdminCb(action="do", key=action, item_id=tg_id).pack())
    b.button(text="⬅️ Annulla", callback_data=AdminCb(action="user", item_id=tg_id).pack())
    b.adjust(2)
    return b.as_markup()


def cancel_to_user_kb(tg_id: int) -> InlineKeyboardMarkup:
    """Cancel an in-progress input and go back to the user detail."""
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Annulla", callback_data=AdminCb(action="user", item_id=tg_id).pack())
    return b.as_markup()


def skip_or_cancel_reason_kb(tg_id: int) -> InlineKeyboardMarkup:
    """For the warn flow: skip the (optional) reason or cancel."""
    b = InlineKeyboardBuilder()
    b.button(text="⏭️ Senza motivo", callback_data=AdminCb(action="do", key="warn", item_id=tg_id).pack())
    b.button(text="⬅️ Annulla", callback_data=AdminCb(action="user", item_id=tg_id).pack())
    b.adjust(2)
    return b.as_markup()


def cancel_to_home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Annulla", callback_data=AdminCb(action="home").pack())
    return b.as_markup()
