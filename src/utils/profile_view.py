"""Shared profile card text (HTML). One code path for `/profilo` and the inline
user picker, so the two can never drift apart. Presentation only: every
user-controlled string goes through `utils.text.esc` here (rule 20)."""

from __future__ import annotations

from database.models import User
from services import xp_service
from services.catalog_loader import ConsumableItem
from services.shop_service import render_active_tags
from utils.text import esc


def profile_text(user: User, pantry: list[tuple[ConsumableItem, int]] | None = None) -> str:
    """Render the full profile card.

    `user` must be loaded with `selectinload(User.wallet)` and
    `selectinload(User.badges)`. `pantry` is the `consumable_service.inventory`
    result; when `None` the pantry section is omitted (inline cards: querying it
    for 20 results per keystroke would be 20 DB round-trips).
    """
    username_display = f"@{esc(user.username)}" if user.username else "N/D"
    badge_count = len(user.badges)
    # Precondition (see docstring): callers eager-load wallet via selectinload and
    # registered users always have a wallet row, so this can never be None here.
    assert user.wallet is not None
    prog = xp_service.level_for_xp(user.xp)
    rank = xp_service.rank_for_level(prog.level)
    rank_txt = f" · {rank.emoji} {esc(rank.name)}" if rank else ""
    level_line = (
        f"⚡ <b>Livello {prog.level}</b>{rank_txt}\n"
        f"   {xp_service.progress_bar(prog)} "
        f"{prog.xp_into_level:,}/{prog.xp_for_next:,} XP\n"
    )
    tags = render_active_tags(user)
    tag_line = f"🏷️ <b>Tag:</b> {esc(tags)}\n" if tags else ""
    title = esc(user.full_name)
    if tags:
        title = f"{esc(tags)} · {title}"

    pantry_line = ""
    if pantry:
        shown = " · ".join(f"{item.emoji} ×{qty}" for item, qty in pantry[:6])
        more = " …" if len(pantry) > 6 else ""
        pantry_line = f"🎒 <b>Dispensa:</b> {shown}{more}\n"

    return (
        f"🎮 <b>{title}</b>\n\n"
        f"🔖 <b>Username:</b> {username_display}\n\n"
        f"{tag_line}"
        f"{level_line}"
        f"💰 <b>CoInn:</b> <b>{user.wallet.coins:,} 🪙</b>\n"
        f"🏆 <b>Trofei:</b> {badge_count}\n"
        f"{pantry_line}".rstrip("\n")
    )
