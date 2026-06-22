from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.catalog_loader import ConsumableCategory, ConsumableItem, CosmeticItem

_CLOSE_TEXT = "✖ Chiudi"
_CLOSE_CB = "shop:close"
_HOME_TEXT = "🔙 Locanda"
_HOME_CB = "shop:home"


def get_locanda_home_kb(has_cosmetics: bool, has_menu: bool) -> InlineKeyboardMarkup:
    """The Locanda landing: pick a section (cosmetics / menu), tags, pantry."""
    builder = InlineKeyboardBuilder()
    if has_cosmetics:
        builder.button(text="🏷️ Personalizzazioni", callback_data="shop:list")
    if has_menu:
        builder.button(text="🍖 Menù della Locanda", callback_data="shop:menu")
    builder.button(text="🎨 I miei tag", callback_data="shop:tags")
    builder.button(text="🎒 Dispensa", callback_data="shop:pantry")
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_shop_catalog_kb(
    items: list[CosmeticItem], balance: int, owned: set[str]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in items:
        if item.key in owned:
            builder.button(
                text=f"🎁 {item.emoji} {item.name} — posseduto",
                callback_data="shop:owned",
            )
        else:
            mark = "✅" if balance >= item.price else "❌"
            builder.button(
                text=f"{mark} {item.emoji} {item.name} — {item.price} 🪙",
                callback_data=f"shop:buy:{item.key}",
            )
    builder.button(text="🎨 I miei tag", callback_data="shop:tags")
    builder.button(text=_HOME_TEXT, callback_data=_HOME_CB)
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_tag_switcher_kb(
    items: list[tuple[str, str, bool]], max_active: int
) -> InlineKeyboardMarkup:
    """Toggle keyboard for owned tags. `items` = (key, label, is_active)."""
    builder = InlineKeyboardBuilder()
    for key, label, active in items:
        builder.button(
            text=f"{'✅' if active else '⭕'} {label}",
            callback_data=f"shop:tag:{key}",
        )
    builder.button(text="🏷️ Personalizzazioni", callback_data="shop:list")
    builder.button(text=_HOME_TEXT, callback_data=_HOME_CB)
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_shop_confirm_kb(item_key: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Acquista", callback_data=f"shop:exec:{item_key}")
    builder.button(text="🔙 Indietro", callback_data="shop:list")
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Consumables (the Locanda menu)
# ---------------------------------------------------------------------------

def get_menu_categories_kb(
    categories: list[ConsumableCategory],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=f"{cat.emoji} {cat.name}", callback_data=f"shop:cat:{cat.key}")
    builder.button(text=_HOME_TEXT, callback_data=_HOME_CB)
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_menu_items_kb(
    items: list[ConsumableItem], balance: int, counts: dict[str, int]
) -> InlineKeyboardMarkup:
    """Items of one category. `counts` maps item_key → how many the user owns."""
    builder = InlineKeyboardBuilder()
    for item in items:
        mark = "✅" if balance >= item.price else "❌"
        qty = counts.get(item.key, 0)
        owned = f" ·{qty}" if qty else ""
        builder.button(
            text=f"{mark} {item.emoji} {item.name} — {item.price} 🪙{owned}",
            callback_data=f"shop:cbuy:{item.key}",
        )
    builder.button(text="🍖 Menù", callback_data="shop:menu")
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_consumable_confirm_kb(item_key: str, category_key: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Acquista", callback_data=f"shop:cexec:{item_key}")
    builder.button(text="🔙 Indietro", callback_data=f"shop:cat:{category_key}")
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()


def get_pantry_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🍖 Menù della Locanda", callback_data="shop:menu")
    builder.button(text=_HOME_TEXT, callback_data=_HOME_CB)
    builder.button(text=_CLOSE_TEXT, callback_data=_CLOSE_CB)
    builder.adjust(1)
    return builder.as_markup()
