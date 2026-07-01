"""
Keyboards for the button-driven admin dashboard (callback namespace ``adm:*``).

Callback grammar (kept well within Telegram's 64-byte limit):
  adm:home · adm:close
  adm:stats · adm:lead · adm:audit · adm:help
  adm:bets · adm:econ · adm:airdrop · adm:search
  adm:users:<page> · adm:user:<tg_id>
  adm:act:<action>:<tg_id>   (input-driven: credit/debit/setbal/mute/warn)
  adm:ask:<action>:<tg_id>   (confirm step: ban/kick)
  adm:do:<action>:<tg_id>    (immediate: ban/kick/sban/unmute/unwarn)
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

PAGE_SIZE = 8


def home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 Statistiche", callback_data="adm:stats")
    b.button(text="🏆 Classifica", callback_data="adm:lead")
    b.button(text="🎬 Eventi", callback_data="ev:home")
    b.button(text="👥 Utenti", callback_data="adm:users:0")
    b.button(text="💰 Economia", callback_data="adm:econ")
    b.button(text="🧾 Audit", callback_data="adm:audit")
    b.button(text="❓ Comandi", callback_data="adm:help")
    b.button(text="✖ Chiudi", callback_data="adm:close")
    b.adjust(2, 1, 2, 2, 1)
    return b.as_markup()


def back_home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Home", callback_data="adm:home")
    b.button(text="✖ Chiudi", callback_data="adm:close")
    b.adjust(2)
    return b.as_markup()


def econ_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎁 Airdrop monete", callback_data="adm:airdrop")
    b.button(text="⚡ Airdrop XP", callback_data="adm:xpairdrop")
    b.button(text="👤 Gestisci un utente", callback_data="adm:users:0")
    b.button(text="⬅️ Home", callback_data="adm:home")
    b.adjust(1)
    return b.as_markup()


def lead_kb(active: str) -> InlineKeyboardMarkup:
    """Leaderboard switcher inside the dashboard (coins · xp · trofei)."""
    b = InlineKeyboardBuilder()
    for label, key in (("💰 Ricchezza", "coins"), ("⚡ XP", "xp"), ("🏆 Trofei", "trofei")):
        b.button(text=f"• {label} •" if key == active else label, callback_data=f"adm:lead:{key}")
    b.button(text="⬅️ Home", callback_data="adm:home")
    b.adjust(3, 1)
    return b.as_markup()


def users_kb(users, page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Paginated user picker. `users` is a list of (User, coins) tuples."""
    b = InlineKeyboardBuilder()
    for user, coins in users:
        name = f"@{user.username}" if user.username else user.full_name[:24]
        b.button(text=f"👤 {name} · {coins:,}🪙", callback_data=f"adm:user:{user.tg_id}")
    b.button(text="🔍 Cerca", callback_data="adm:search")
    nav = []
    if page > 0:
        nav.append(("⬅️ Prec", f"adm:users:{page - 1}"))
    if has_next:
        nav.append(("Succ ➡️", f"adm:users:{page + 1}"))
    for text, cb in nav:
        b.button(text=text, callback_data=cb)
    b.button(text="🏠 Home", callback_data="adm:home")
    # rows: one per user, then [cerca], optional [prev/next], then [home]
    layout = [1] * len(users) + [1] + ([len(nav)] if nav else []) + [1]
    b.adjust(*layout)
    return b.as_markup()


def user_detail_kb(tg_id: int, group_enabled: bool) -> InlineKeyboardMarkup:
    """Per-user action buttons. Moderation actions require a configured group."""
    b = InlineKeyboardBuilder()
    b.button(text="💰 Credita", callback_data=f"adm:act:credit:{tg_id}")
    b.button(text="🔻 Addebita", callback_data=f"adm:act:debit:{tg_id}")
    b.button(text="⚖️ Set saldo", callback_data=f"adm:act:setbal:{tg_id}")
    b.button(text="⚡ Dai XP", callback_data=f"adm:act:xpgrant:{tg_id}")
    b.button(text="⚡ Set XP", callback_data=f"adm:act:xpset:{tg_id}")
    counts = [3, 2]
    if group_enabled:
        b.button(text="⛔ Ban", callback_data=f"adm:ask:ban:{tg_id}")
        b.button(text="👢 Kick", callback_data=f"adm:ask:kick:{tg_id}")
        b.button(text="✅ Sban", callback_data=f"adm:do:sban:{tg_id}")
        b.button(text="🔇 Mute", callback_data=f"adm:act:mute:{tg_id}")
        b.button(text="🔊 Unmute", callback_data=f"adm:do:unmute:{tg_id}")
        b.button(text="⚠️ Warn", callback_data=f"adm:act:warn:{tg_id}")
        b.button(text="♻️ Unwarn", callback_data=f"adm:do:unwarn:{tg_id}")
        counts += [2, 3, 2]
    b.button(text="⬅️ Lista", callback_data="adm:users:0")
    b.button(text="✖ Chiudi", callback_data="adm:close")
    counts += [2]
    b.adjust(*counts)
    return b.as_markup()


def confirm_kb(action: str, tg_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Conferma", callback_data=f"adm:do:{action}:{tg_id}")
    b.button(text="⬅️ Annulla", callback_data=f"adm:user:{tg_id}")
    b.adjust(2)
    return b.as_markup()


def cancel_to_user_kb(tg_id: int) -> InlineKeyboardMarkup:
    """Cancel an in-progress input and go back to the user detail."""
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Annulla", callback_data=f"adm:user:{tg_id}")
    return b.as_markup()


def skip_or_cancel_reason_kb(tg_id: int) -> InlineKeyboardMarkup:
    """For the warn flow: skip the (optional) reason or cancel."""
    b = InlineKeyboardBuilder()
    b.button(text="⏭️ Senza motivo", callback_data=f"adm:do:warn:{tg_id}")
    b.button(text="⬅️ Annulla", callback_data=f"adm:user:{tg_id}")
    b.adjust(2)
    return b.as_markup()


def cancel_to_home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Annulla", callback_data="adm:home")
    return b.as_markup()
