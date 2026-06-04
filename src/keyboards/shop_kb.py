from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.catalog_loader import CosmeticItem

_CLOSE_TEXT = "✖ Chiudi"
_CLOSE_CB = "shop:close"


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
