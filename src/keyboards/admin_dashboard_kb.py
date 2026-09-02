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
    b.button(text="🎯 Manda premi (a più utenti)", callback_data=AdminCb(action="massreward").pack())
    b.button(text="👤 Gestisci un utente", callback_data=AdminCb(action="users", item_id=0).pack())
    b.button(text="⬅️ Home", callback_data=AdminCb(action="home").pack())
    b.adjust(1)
    return b.as_markup()


def massreward_kb() -> InlineKeyboardMarkup:
    """Choose what to send in the multi-recipient reward flow: XP or CoInn."""
    b = InlineKeyboardBuilder()
    b.button(text="⚡ XP", callback_data=AdminCb(action="massreward", key="xp").pack())
    b.button(text="🪙 CoInn", callback_data=AdminCb(action="massreward", key="coins").pack())
    b.button(text="⬅️ Annulla", callback_data=AdminCb(action="econ").pack())
    b.adjust(2, 1)
    return b.as_markup()


def _picker_name(user) -> str:
    """Label for a user in the reward picker: @username, or the name truncated."""
    return f"@{user.username}" if user.username else user.full_name[:24]


def mass_picker_kb(
    users, selected_ids, page: int, has_next: bool, *, is_search: bool = False
) -> InlineKeyboardMarkup:
    """Member picker for the multi-recipient reward flow.

    `users` is a list of (User, coins). Already-selected members are marked with a
    ✅; tapping one toggles it via ``mrpick``. Search results (`is_search`) drop the
    page navigation and offer a way back to the full list instead.
    """
    b = InlineKeyboardBuilder()
    selected = set(selected_ids)
    for user, coins in users:
        mark = "✅ " if user.tg_id in selected else "👤 "
        b.button(
            text=f"{mark}{_picker_name(user)} · {coins:,}🪙",
            callback_data=AdminCb(action="mrpick", item_id=user.tg_id).pack(),
        )
    tools: list[tuple[str, str]] = [("🔍 Cerca", AdminCb(action="mrsearch").pack())]
    if is_search:
        tools.append(("📋 Lista completa", AdminCb(action="mrlist", item_id=0).pack()))
    for text, cb in tools:
        b.button(text=text, callback_data=cb)
    nav: list[tuple[str, str]] = []
    if not is_search and page > 0:
        nav.append(("⬅️ Prec", AdminCb(action="mrlist", item_id=page - 1).pack()))
    if not is_search and has_next:
        nav.append(("Succ ➡️", AdminCb(action="mrlist", item_id=page + 1).pack()))
    for text, cb in nav:
        b.button(text=text, callback_data=cb)
    if selected:
        b.button(
            text=f"✔️ Conferma selezione ({len(selected)})",
            callback_data=AdminCb(action="mrconfirm").pack(),
        )
    b.button(text="❌ Annulla", callback_data=AdminCb(action="home").pack())
    layout = [1] * len(users) + [len(tools)] + ([len(nav)] if nav else [])
    layout += ([1] if selected else []) + [1]
    b.adjust(*layout)
    return b.as_markup()


def mass_more_kb() -> InlineKeyboardMarkup:
    """After a member is picked: select another, or move on to the summary."""
    b = InlineKeyboardBuilder()
    b.button(text="➕ Sì, aggiungi", callback_data=AdminCb(action="mrmore", key="yes").pack())
    b.button(text="✔️ No, prosegui", callback_data=AdminCb(action="mrmore", key="no").pack())
    b.adjust(2)
    return b.as_markup()


def mass_confirm_kb() -> InlineKeyboardMarkup:
    """The final summary of a multi-recipient reward before it is paid out."""
    b = InlineKeyboardBuilder()
    b.button(text="✅ Conferma e manda", callback_data=AdminCb(action="mrsend").pack())
    b.button(text="➕ Aggiungi qualcuno", callback_data=AdminCb(action="mrlist", item_id=0).pack())
    b.button(text="➖ Rimuovi qualcuno", callback_data=AdminCb(action="mrremlist").pack())
    b.button(text="❌ Annulla tutto", callback_data=AdminCb(action="home").pack())
    b.adjust(1, 2, 1)
    return b.as_markup()


def mass_remove_kb(users) -> InlineKeyboardMarkup:
    """One button per selected member; tapping removes them (``mrunpick``)."""
    b = InlineKeyboardBuilder()
    for user in users:
        b.button(
            text=f"➖ {_picker_name(user)}",
            callback_data=AdminCb(action="mrunpick", item_id=user.tg_id).pack(),
        )
    b.button(text="⬅️ Torna al riepilogo", callback_data=AdminCb(action="mrconfirm").pack())
    b.adjust(*([1] * len(users) + [1]))
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
