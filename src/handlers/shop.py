"""
Shop handler — spend CoInn on **cosmetic** customizations (tags/titles).

Opens **everywhere** (private or group): /negozio shows the catalog inline; there is
no group context to resolve and no deep-link dance.

Purchase flow (all callbacks act on the *clicking* user only):
  shop:list          → (re)show catalog
  shop:buy:<key>     → confirm screen (price + tag preview)
  shop:exec:<key>    → idempotency + balance check, debit, apply tag, record
  shop:owned         → alert: already owned
  shop:close         → delete panel

Security: cosmetics are in-bot flair only (no Telegram permissions). Purchases are
idempotent and always debit/apply to ``callback.from_user.id`` → no grief / no escalation.
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
from keyboards.shop_kb import get_shop_catalog_kb, get_shop_confirm_kb, get_tag_switcher_kb
from services import economy_service, shop_service
from utils import cooldown
from utils.text import esc

log = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# /negozio command (private only — the catalog shows the caller's balance)
# ---------------------------------------------------------------------------

@router.message(Command("negozio"))
async def cmd_negozio(message: Message, db_session: AsyncSession) -> None:
    # In a group the catalog would leak the opener's balance to everyone, and
    # the inline buttons act on whoever clicks → send a private deep-link instead.
    if await redirect_to_private(message, f"shop_{message.chat.id}", "🛒 Apri il negozio"):
        return
    if not await cooldown.guard(message, "negozio", settings.command_cooldown_seconds):
        return
    await _show_catalog(message, db_session)


async def start_shop_private(
    message: Message,
    state: FSMContext,
    group_id: int,
    db_session: AsyncSession,
) -> None:
    """Back-compat entry for the legacy ``?start=shop_<group_id>`` deep-link."""
    await _show_catalog(message, db_session)


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


def _catalog_text(balance: int, has_items: bool) -> str:
    if not has_items:
        return (
            "🛒 <b>Negozio</b>\n\n"
            f"🪙 Il tuo saldo: <b>{balance:,} CoInn</b>\n\n"
            "<i>Nessuna personalizzazione disponibile al momento.</i>"
        )
    return (
        "🛒 <b>Negozio — Personalizzazioni</b>\n\n"
        f"🪙 Il tuo saldo: <b>{balance:,} CoInn</b>\n\n"
        "Compra un <b>tag</b> da mostrare sul tuo profilo.\n"
        "✅ = acquistabile · ❌ = saldo insufficiente · 🎁 = già posseduto"
    )


async def _show_catalog(message: Message, db_session: AsyncSession) -> None:
    tg_id = message.from_user.id
    balance = await _balance(db_session, tg_id)
    items = list(shop_service.get_cosmetics().values())
    owned = await _owned_keys(db_session, tg_id, [i.key for i in items])
    await message.answer(
        _catalog_text(balance, bool(items)),
        reply_markup=get_shop_catalog_kb(items, balance, owned),
    )


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "shop:list")
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


@router.callback_query(F.data == "shop:owned")
async def cb_shop_owned(callback: CallbackQuery) -> None:
    await callback.answer("🎁 Possiedi già questa personalizzazione.", show_alert=True)


@router.callback_query(F.data.startswith("shop:buy:"))
async def cb_shop_buy(callback: CallbackQuery, db_session: AsyncSession) -> None:
    item_key = callback.data[len("shop:buy:"):]
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


@router.callback_query(F.data.startswith("shop:exec:"))
async def cb_shop_execute(callback: CallbackQuery, db_session: AsyncSession) -> None:
    item_key = callback.data[len("shop:exec:"):]
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


@router.callback_query(F.data == "shop:tags")
async def cb_shop_tags(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await _show_tag_switcher(callback.message, db_session, callback.from_user.id, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:tag:"))
async def cb_shop_toggle_tag(callback: CallbackQuery, db_session: AsyncSession) -> None:
    item_key = callback.data[len("shop:tag:"):]
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


@router.callback_query(F.data == "shop:close")
async def cb_shop_close(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
