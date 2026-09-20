"""
Locanda handler — spend CoInn on **cosmetic** tags and on **consumable** menu
items (food/drink) at *La Locanda del Drago*.

Opens with ``/locanda`` (``/negozio`` kept as a back-compat alias). In a group the
catalog would leak the opener's balance and the inline buttons act on whoever
clicks → it redirects to private via the ``shop_<chat_id>`` deep-link.

Sections (all callbacks act on the *clicking* user only):
  shop:home          → landing (pick a section)
  shop:list          → 🏷️ cosmetics catalog
  shop:buy/exec/owned → cosmetic purchase (own-once, idempotent)
  shop:tags/tag      → tag switcher (combine owned tags)
  shop:menu          → 🍖 consumable categories
  shop:cat:<key>     → items of a category
  shop:cbuy/cexec    → consumable purchase (repeatable; logs to the pantry)
  shop:pantry        → 🎒 owned consumables with quantities
  shop:close         → delete panel

Security: nothing here grants Telegram permissions. Cosmetics are idempotent flair;
consumables are repeatable CoInn sinks that only feed the buyer's pantry/trophies.
Every debit/apply targets ``callback.from_user.id`` → no grief / no escalation.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters.command import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import TransactionType, Wallet
from exceptions.economy import InsufficientFundsError, WalletNotFoundError
from handlers._privacy import redirect_to_private
from handlers._trophy_announce import announce_trophies
from handlers.callbacks import ShopCb
from keyboards.shop_kb import (
    get_consumable_confirm_kb,
    get_locanda_home_kb,
    get_menu_categories_kb,
    get_menu_items_kb,
    get_pantry_kb,
    get_shop_catalog_kb,
    get_shop_confirm_kb,
    get_tag_switcher_kb,
)
from services import badge_service, consumable_service, economy_service, shop_service
from utils import cooldown
from utils.text import esc

log = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# /locanda command (redirects from group → private; the catalog shows the balance)
# ---------------------------------------------------------------------------

@router.message(Command("locanda", "negozio"))
async def cmd_locanda(message: Message, db_session: AsyncSession) -> None:
    # In a group the catalog would leak the opener's balance to everyone, and
    # the inline buttons act on whoever clicks → send a private deep-link instead.
    if await redirect_to_private(message, f"shop_{message.chat.id}", "🍺 Apri la Locanda"):
        return
    if not await cooldown.guard(message, "locanda", settings.command_cooldown_seconds):
        return
    await _show_home(message, db_session, message.from_user.id)


async def start_shop_private(
    message: Message,
    state: FSMContext,
    group_id: int,
    db_session: AsyncSession,
) -> None:
    """Back-compat entry for the legacy ``?start=shop_<group_id>`` deep-link."""
    await _show_home(message, db_session, message.from_user.id)


async def _balance(db_session: AsyncSession, tg_id: int) -> int:
    wallet_result = await db_session.execute(select(Wallet).where(Wallet.tg_id == tg_id))
    wallet = wallet_result.scalar_one_or_none()
    return wallet.coins if wallet else 0


async def _owned_keys(db_session: AsyncSession, tg_id: int, keys) -> set[str]:
    owned: set[str] = set()
    for key in keys:
        if await shop_service.has_cosmetic(db_session, tg_id, key):
            owned.add(key)
    return owned


def _home_text(balance: int) -> str:
    return (
        "🍺 <b>La Locanda del Drago</b>\n\n"
        f"🪙 Il tuo saldo: <b>{balance:,} CoInn</b>\n\n"
        "🏷️ <b>Personalizzazioni</b> — tag da sfoggiare sul profilo.\n"
        "🍖 <b>Menù della Locanda</b> — cibi e bevande da collezionare.\n"
        "🎒 <b>Dispensa</b> — cosa hai accumulato."
    )


async def _show_home(
    message: Message, db_session: AsyncSession, tg_id: int, *, edit: bool = False
) -> None:
    # ``tg_id`` is passed explicitly: on a callback, ``message`` is the bot-sent
    # panel whose ``from_user`` is the *bot*, not the buyer — reading the balance
    # from it would show 0. The caller always supplies the clicking user's id.
    balance = await _balance(db_session, tg_id)
    kb = get_locanda_home_kb(
        has_cosmetics=bool(shop_service.get_cosmetics()),
        has_menu=bool(consumable_service.get_consumables()),
    )
    text = _home_text(balance)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:  # noqa: BLE001
            pass
    await message.answer(text, reply_markup=kb)


def _catalog_text(balance: int, has_items: bool) -> str:
    if not has_items:
        return (
            "🏷️ <b>Personalizzazioni</b>\n\n"
            f"🪙 Il tuo saldo: <b>{balance:,} CoInn</b>\n\n"
            "<i>Nessuna personalizzazione disponibile al momento.</i>"
        )
    return (
        "🏷️ <b>Personalizzazioni</b>\n\n"
        f"🪙 Il tuo saldo: <b>{balance:,} CoInn</b>\n\n"
        "Compra un <b>tag</b> da mostrare sul tuo profilo.\n"
        "✅ = acquistabile · ❌ = saldo insufficiente · 🎁 = già posseduto"
    )


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

@router.callback_query(ShopCb.filter(F.action == "home"))
async def cb_shop_home(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await _show_home(callback.message, db_session, callback.from_user.id, edit=True)
    await callback.answer()


@router.callback_query(ShopCb.filter(F.action == "list"))
async def cb_shop_list(callback: CallbackQuery, db_session: AsyncSession) -> None:
    tg_id = callback.from_user.id
    balance = await _balance(db_session, tg_id)
    items = list(shop_service.get_cosmetics().values())
    owned = await _owned_keys(db_session, tg_id, [i.key for i in items])
    await callback.message.edit_text(
        _catalog_text(balance, bool(items)),
        reply_markup=get_shop_catalog_kb(items, balance, owned),
    )
    await callback.answer()


@router.callback_query(ShopCb.filter(F.action == "owned"))
async def cb_shop_owned(callback: CallbackQuery) -> None:
    await callback.answer("🎁 Possiedi già questa personalizzazione.", show_alert=True)


@router.callback_query(ShopCb.filter(F.action == "buy"))
async def cb_shop_buy(
    callback: CallbackQuery, callback_data: ShopCb, db_session: AsyncSession
) -> None:
    item_key = callback_data.key
    if item_key is None:
        await callback.answer()
        return
    item = shop_service.get_item(item_key)
    if item is None:
        await callback.answer("⚠️ Oggetto non disponibile.", show_alert=True)
        return

    if await shop_service.has_cosmetic(db_session, callback.from_user.id, item_key):
        await callback.answer("🎁 Possiedi già questa personalizzazione.", show_alert=True)
        return

    balance = await _balance(db_session, callback.from_user.id)
    if balance < item.price:
        await callback.answer(
            f"⚠️ Saldo insufficiente: hai {balance:,} 🪙, servono {item.price:,} 🪙.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        f"🛒 <b>{esc(item.name)}</b>\n\n"
        f"Tag mostrato sul profilo: <b>{esc(shop_service.format_tag(item))}</b>\n\n"
        f"💸 Costo: <b>{item.price:,} 🪙</b>\n"
        f"💰 Saldo: <b>{balance:,} 🪙</b>\n\n"
        f"Confermi l'acquisto?",
        reply_markup=get_shop_confirm_kb(item.key),
    )
    await callback.answer()


@router.callback_query(ShopCb.filter(F.action == "exec"))
async def cb_shop_execute(
    callback: CallbackQuery, callback_data: ShopCb, db_session: AsyncSession
) -> None:
    item_key = callback_data.key
    if item_key is None:
        await callback.answer()
        return
    item = shop_service.get_item(item_key)
    if item is None:
        await callback.answer("⚠️ Oggetto non disponibile.", show_alert=True)
        return

    tg_id = callback.from_user.id

    # Fast-path idempotency check (no lock). The authoritative re-check happens
    # under the wallet lock below to close the double-purchase race.
    if await shop_service.has_cosmetic(db_session, tg_id, item_key):
        await callback.answer("🎁 Possiedi già questa personalizzazione.", show_alert=True)
        return

    try:
        # Debit FIRST: this takes the wallet row lock, serializing concurrent
        # purchases of the same user.
        await economy_service.debit(
            db_session,
            tg_id,
            item.price,
            TransactionType.shop_purchase,
            f"Acquisto negozio: {item.name}",
        )
        # Re-check ownership under the lock: if a concurrent purchase already
        # applied this cosmetic, undo this debit and bail (never charge twice).
        if await shop_service.has_cosmetic(db_session, tg_id, item_key):
            await db_session.rollback()
            await callback.answer("🎁 Possiedi già questa personalizzazione.", show_alert=True)
            return
        await shop_service.record_purchase(db_session, tg_id, item_key, item.price)
        await shop_service.apply_cosmetic(db_session, tg_id, item)
        await db_session.commit()
    except InsufficientFundsError as e:
        await callback.answer(
            f"⚠️ Saldo insufficiente: hai {e.balance:,} 🪙, servono {e.required:,} 🪙.",
            show_alert=True,
        )
        return
    except WalletNotFoundError:
        await callback.answer("⚠️ Wallet non trovato. Usa /start per registrarti.", show_alert=True)
        return

    await callback.message.edit_text(
        f"✅ <b>Acquistato!</b>\n\n"
        f"🏷️ Il tuo nuovo tag: <b>{esc(shop_service.format_tag(item))}</b>\n"
        f"💸 Hai speso <b>{item.price:,} 🪙</b>.\n\n"
        f"Lo vedi sul tuo /profilo."
    )
    await callback.answer("🎉 Tag applicato!")


# ---------------------------------------------------------------------------
# Consumables — the Locanda menu (repeatable purchases, fill the pantry)
# ---------------------------------------------------------------------------

def _menu_text(balance: int) -> str:
    return (
        "🍖 <b>Menù della Locanda</b>\n\n"
        f"🪙 Il tuo saldo: <b>{balance:,} CoInn</b>\n\n"
        "Scegli una portata: ogni acquisto finisce nella tua 🎒 Dispensa e sblocca trofei."
    )


@router.callback_query(ShopCb.filter(F.action == "menu"))
async def cb_shop_menu(callback: CallbackQuery, db_session: AsyncSession) -> None:
    cats = consumable_service.get_categories()
    if not cats:
        await callback.answer("🍽️ Il menù è vuoto al momento.", show_alert=True)
        return
    balance = await _balance(db_session, callback.from_user.id)
    await callback.message.edit_text(
        _menu_text(balance), reply_markup=get_menu_categories_kb(cats)
    )
    await callback.answer()


@router.callback_query(ShopCb.filter(F.action == "cat"))
async def cb_shop_category(
    callback: CallbackQuery, callback_data: ShopCb, db_session: AsyncSession
) -> None:
    cat_key = callback_data.key
    if cat_key is None:
        await callback.answer()
        return
    cat = consumable_service.get_category(cat_key)
    items = consumable_service.items_in_category(cat_key)
    if cat is None or not items:
        await callback.answer("⚠️ Categoria non disponibile.", show_alert=True)
        return
    tg_id = callback.from_user.id
    balance = await _balance(db_session, tg_id)
    counts = await consumable_service.purchase_counts(db_session, tg_id)
    await callback.message.edit_text(
        f"{cat.emoji} <b>{esc(cat.name)}</b>\n\n"
        f"🪙 Il tuo saldo: <b>{balance:,} CoInn</b>\n\n"
        "✅ = acquistabile · ❌ = saldo insufficiente · ·N = quantità in dispensa",
        reply_markup=get_menu_items_kb(items, balance, counts),
    )
    await callback.answer()


@router.callback_query(ShopCb.filter(F.action == "cbuy"))
async def cb_consumable_buy(
    callback: CallbackQuery, callback_data: ShopCb, db_session: AsyncSession
) -> None:
    item_key = callback_data.key
    if item_key is None:
        await callback.answer()
        return
    item = consumable_service.get_item(item_key)
    if item is None:
        await callback.answer("⚠️ Oggetto non disponibile.", show_alert=True)
        return

    balance = await _balance(db_session, callback.from_user.id)
    if balance < item.price:
        await callback.answer(
            f"⚠️ Saldo insufficiente: hai {balance:,} 🪙, servono {item.price:,} 🪙.",
            show_alert=True,
        )
        return

    desc = f"<i>{esc(item.description)}</i>\n\n" if item.description else ""
    await callback.message.edit_text(
        f"🍽️ <b>{esc(item.name)}</b> {item.emoji}\n\n"
        f"{desc}"
        f"💸 Costo: <b>{item.price:,} 🪙</b>\n"
        f"💰 Saldo: <b>{balance:,} 🪙</b>\n\n"
        f"Confermi l'acquisto?",
        reply_markup=get_consumable_confirm_kb(item.key, item.category),
    )
    await callback.answer()


@router.callback_query(ShopCb.filter(F.action == "cexec"))
async def cb_consumable_execute(
    callback: CallbackQuery, callback_data: ShopCb, db_session: AsyncSession
) -> None:
    item_key = callback_data.key
    if item_key is None:
        await callback.answer()
        return
    item = consumable_service.get_item(item_key)
    if item is None:
        await callback.answer("⚠️ Oggetto non disponibile.", show_alert=True)
        return

    tg_id = callback.from_user.id
    try:
        # Debit takes the wallet row lock (serializes concurrent buys); consumables
        # are repeatable so there's no idempotency re-check.
        await economy_service.debit(
            db_session, tg_id, item.price, TransactionType.shop_purchase,
            f"Locanda: {item.name}",
        )
        await consumable_service.record_consumption(db_session, tg_id, item, item.price)
        # Flush so the just-added purchase is visible to the milestone count query
        # (autoflush is off → the SELECT would otherwise miss the pending INSERT).
        await db_session.flush()
        newly = await badge_service.check_and_award_milestones(db_session, tg_id)
        await db_session.commit()
    except InsufficientFundsError as e:
        await callback.answer(
            f"⚠️ Saldo insufficiente: hai {e.balance:,} 🪙, servono {e.required:,} 🪙.",
            show_alert=True,
        )
        return
    except WalletNotFoundError:
        await callback.answer("⚠️ Wallet non trovato. Usa /start per registrarti.", show_alert=True)
        return

    # Trophies are announced in the GROUP (tagging the user), not in private.
    await announce_trophies(callback.bot, db_session, tg_id, newly)

    text = (
        f"✅ <b>Gustato!</b> {item.emoji}\n\n"
        f"Hai acquistato <b>{esc(item.name)}</b> — finisce nella tua 🎒 Dispensa.\n"
        f"💸 Hai speso <b>{item.price:,} 🪙</b>."
    )
    await callback.message.edit_text(text, reply_markup=get_pantry_kb())
    await callback.answer("🎉 Acquistato!")


@router.callback_query(ShopCb.filter(F.action == "pantry"))
async def cb_shop_pantry(callback: CallbackQuery, db_session: AsyncSession) -> None:
    items = await consumable_service.inventory(db_session, callback.from_user.id)
    if not items:
        text = (
            "🎒 <b>La tua Dispensa</b>\n\n"
            "<i>È vuota! Passa dal 🍖 Menù della Locanda per riempirla.</i>"
        )
    else:
        lines = ["🎒 <b>La tua Dispensa</b>\n"]
        lines += [f"{it.emoji} <b>{esc(it.name)}</b> ×{qty}" for it, qty in items]
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=get_pantry_kb())
    await callback.answer()


# ---------------------------------------------------------------------------
# Tag switcher — activate/deactivate & combine owned tags
# ---------------------------------------------------------------------------

async def _show_tag_switcher(message: Message, db_session: AsyncSession, tg_id: int, *, edit: bool) -> None:
    # Lazily migrate a legacy single tag into the active list so the toggles
    # reflect what the user is actually wearing.
    await shop_service.ensure_active_seeded(db_session, tg_id)
    await db_session.commit()

    cats = shop_service.get_cosmetics()
    owned = await shop_service.owned_tag_keys(db_session, tg_id)
    active = set(await shop_service.get_active_keys(db_session, tg_id))
    items = [(k, shop_service.format_tag(cats[k]), k in active) for k in owned if k in cats]

    if not owned:
        text = (
            "🎨 <b>I tuoi tag</b>\n\n"
            "<i>Non possiedi ancora nessun tag. Compra una personalizzazione dal negozio!</i>"
        )
    else:
        text = (
            "🎨 <b>I tuoi tag</b>\n\n"
            f"Attiva o disattiva i tag posseduti — puoi <b>combinarne fino a {settings.max_active_tags}</b> "
            "insieme. ✅ = attivo, ⭕ = spento."
        )
    kb = get_tag_switcher_kb(items, settings.max_active_tags)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
        except Exception:  # noqa: BLE001
            await message.answer(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.callback_query(ShopCb.filter(F.action == "tags"))
async def cb_shop_tags(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await _show_tag_switcher(callback.message, db_session, callback.from_user.id, edit=True)
    await callback.answer()


@router.callback_query(ShopCb.filter(F.action == "tag"))
async def cb_shop_toggle_tag(
    callback: CallbackQuery, callback_data: ShopCb, db_session: AsyncSession
) -> None:
    item_key = callback_data.key
    if item_key is None:
        await callback.answer()
        return
    result = await shop_service.toggle_tag(
        db_session, callback.from_user.id, item_key, settings.max_active_tags
    )
    await db_session.commit()
    if result == "cap":
        await callback.answer(
            f"⚠️ Massimo {settings.max_active_tags} tag attivi insieme. Disattivane uno prima.",
            show_alert=True,
        )
    elif result == "notowned":
        await callback.answer("⚠️ Non possiedi questo tag.", show_alert=True)
    else:
        await callback.answer("✅ Tag attivato!" if result == "activated" else "⭕ Tag disattivato.")
    await _show_tag_switcher(callback.message, db_session, callback.from_user.id, edit=True)


@router.callback_query(ShopCb.filter(F.action == "close"))
async def cb_shop_close(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
