"""Locanda stock and personal gifts; no FSM, gameplay buffs or AI calls."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from exceptions.economy import InsufficientFundsError, WalletNotFoundError
from handlers._mentions import mention_many
from handlers._privacy import redirect_to_private
from handlers._trophy_announce import announce_trophies
from handlers.callbacks import ShopCb, TavernCb
from services import badge_service, consumable_service, group_registry, tavern_service as service
from utils import cooldown
from utils.daytime import utc_now
from utils.text import esc

router = Router()
log = logging.getLogger(__name__)
_PAGE_SIZE = 8


def _button(builder: InlineKeyboardBuilder, text: str, action: str, offer_id: int = 0, ref: int = 0) -> None:
    builder.button(text=text, callback_data=TavernCb(action=action, offer_id=offer_id, ref=ref).pack())


def _home(builder: InlineKeyboardBuilder) -> InlineKeyboardMarkup:
    builder.button(text="🔙 Locanda", callback_data=ShopCb(action="home").pack())
    builder.adjust(1)
    return builder.as_markup()


async def _member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(group_registry.get_group_id(), user_id, request_timeout=10)
        return member.status in {"creator", "administrator", "member"} or (
            member.status == "restricted" and member.is_member
        )
    except Exception:
        # Membership verification is fail-closed; no provider bodies in logs.
        return False


@router.message(Command("offri"))
async def cmd_offri(message: Message, db_session: AsyncSession) -> None:
    group_id = group_registry.get_group_id()
    target = message.reply_to_message.from_user if message.reply_to_message else None
    if (
        message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
        or message.chat.id != group_id or not group_id
    ):
        await message.answer("🎁 Nel gruppo, rispondi al messaggio di un amico con /offri.")
        return
    if (
        message.sender_chat or not target or target.is_bot
        or message.reply_to_message.sender_chat or target.id == message.from_user.id
    ):
        await message.reply("🎁 Rispondi al messaggio di un altro membro con /offri (non bot o messaggi anonimi).")
        return
    if not await cooldown.guard(message, "tavern_offer", 10):
        return
    try:
        offer = await service.create_offer(
            db_session, message.from_user.id, target.id, group_id, message.message_id,
        )
        await db_session.commit()
    except service.TavernError as exc:
        await db_session.rollback()
        await message.reply(str(exc))
        return
    await redirect_to_private(
        message, f"offri_{offer.token}", "🎁 Scegli cosa offrire",
        notice="🎁 Scegli e conferma in privato: non è stato speso nulla. L'invito dura 10 minuti.",
    )


async def show_offer(message: Message, db_session: AsyncSession, offer: service.Offer, *, edit: bool = False) -> None:
    names = await mention_many(db_session, [offer.recipient_tg_id])
    target = names[offer.recipient_tg_id]
    builder = InlineKeyboardBuilder()
    if offer.status == "delivered":
        item = consumable_service.get_item(offer.item_key) if offer.item_key else None
        label = f"{item.emoji} {esc(item.name)}" if item else "un oggetto"
        text = f"🎁 Hai offerto {label} a {target}.\nIl regalo è già nella sua dispensa."
        if offer.message_id is None:
            _button(builder, "📣 Pubblica nel gruppo", "publish", offer.id)
    elif offer.status == "cancelled" or utc_now() >= offer.expires_at:
        text = "Questa offerta è scaduta o annullata. Nel gruppo puoi crearne un'altra con /offri."
    elif offer.status == "prepared":
        item = consumable_service.get_item(offer.item_key) if offer.item_key else None
        label = f"{item.emoji} {esc(item.name)}" if item else "Oggetto non più disponibile"
        source = f"Costo: <b>{offer.cost:,} CoInn</b>." if offer.source_purchase_id is None else "Dalla tua dispensa: <b>nessuna nuova spesa</b>."
        text = f"🎁 Offri <b>{label}</b> a {target}?\n\n{source}\nIl destinatario può conservarlo o consumarlo."
        _button(builder, "✅ Conferma offerta", "confirm", offer.id)
        _button(builder, "✖ Annulla", "cancel", offer.id)
    else:
        text = f"🎁 Cosa vuoi offrire a {target}?\nScegli un oggetto, poi conferma. Nessun addebito ora."
        _button(builder, "🍖 Compra dal menù", "menu", offer.id)
        _button(builder, "🎒 Dalla mia dispensa", "stock", offer.id)
        _button(builder, "✖ Annulla", "cancel", offer.id)
    markup = _home(builder)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def start_offer_private(message: Message, db_session: AsyncSession, token: str) -> None:
    if message.chat.type != ChatType.PRIVATE:
        await redirect_to_private(message, f"offri_{token}")
        return
    try:
        offer = await service.find_offer(db_session, token, message.from_user.id)
        service.authorize_sender(offer, message.from_user.id, group_registry.get_group_id())
    except service.TavernError as exc:
        await message.answer(str(exc))
        return
    await show_offer(message, db_session, offer)


async def show_pantry(message: Message, db_session: AsyncSession, user_id: int, page: int = 0) -> None:
    summary = await consumable_service.stock_summary(db_session, user_id)
    stock = {key: count for key, (count, _) in summary.items()}
    units = {key: unit for key, (_, unit) in summary.items()}
    items = [item for item in consumable_service.get_consumables().values() if stock.get(item.key)]
    pages = max(1, (len(items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    builder = InlineKeyboardBuilder()
    lines = ["🎒 <b>La tua Dispensa</b>\n"]
    for item in items[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
        lines.append(f"{item.emoji} <b>{esc(item.name)}</b> ×{stock[item.key]}")
        _button(builder, f"🍽 Consuma {item.name}", "consume", ref=units[item.key])
    if not items:
        lines.append("<i>È vuota! Passa dal 🍖 Menù della Locanda per riempirla.</i>")
    else:
        lines.append("\nOgni consumazione usa una sola unità. Nessun bonus ai giochi o ai premi.")
    if pages > 1:
        lines.append(f"\nPagina {page + 1}/{pages}")
        if page:
            _button(builder, "⬅ Precedenti", "pantry", ref=page - 1)
        if page + 1 < pages:
            _button(builder, "Successivi ➡", "pantry", ref=page + 1)
    builder.button(text="🍖 Menù della Locanda", callback_data=ShopCb(action="menu").pack())
    await message.edit_text("\n".join(lines), reply_markup=_home(builder))


async def _choices(callback: CallbackQuery, db_session: AsyncSession, offer: service.Offer, action: str, page: int) -> None:
    if offer.status != "draft" or utc_now() >= offer.expires_at:
        await show_offer(callback.message, db_session, offer, edit=True)
        return
    units = await consumable_service.stock_units(db_session, callback.from_user.id) if action == "stock" else {}
    entries = [
        (index, item) for index, item in enumerate(consumable_service.get_consumables().values())
        if action == "menu" or item.key in units
    ]
    pages = max(1, (len(entries) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    builder = InlineKeyboardBuilder()
    lines = ["🎁 Scegli cosa offrire; seguirà una conferma.\n"]
    for index, item in entries[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
        label = f"{item.emoji} {item.name}"
        if action == "menu":
            label += f" — {item.price:,} 🪙"
        lines.append(esc(label))
        _button(builder, label, "choose_buy" if action == "menu" else "choose_stock",
                offer.id, index if action == "menu" else units[item.key])
    if not entries:
        lines.append("La tua dispensa è vuota.")
    if page:
        _button(builder, "⬅ Precedenti", action, offer.id, page - 1)
    if page + 1 < pages:
        _button(builder, "Successivi ➡", action, offer.id, page + 1)
    _button(builder, "🔙 Offerta", "open", offer.id)
    await callback.message.edit_text("\n".join(lines), reply_markup=_home(builder))


async def publish_offer(callback: CallbackQuery, db_session: AsyncSession, offer: service.Offer) -> bool:
    claimed = await service.claim_publication(db_session, offer.id, callback.from_user.id, group_registry.get_group_id())
    if not claimed:
        await db_session.commit()
        return False
    offer = await service.get_offer(db_session, offer.id)
    names = await mention_many(db_session, [offer.sender_tg_id, offer.recipient_tg_id])
    item = consumable_service.get_item(offer.item_key) if offer.item_key else None
    label = f"{item.emoji} {esc(item.name)}" if item else "un oggetto"
    text = f"🎁 {names[offer.sender_tg_id]} offre <b>{label}</b> a {names[offer.recipient_tg_id]}!"
    text += "\nPuoi consumarlo ora o conservarlo nella tua dispensa."
    builder = InlineKeyboardBuilder()
    _button(builder, "🍻 Brindiamo!", "drink", offer.id, offer.purchase_id or 0)
    _button(builder, "🎒 Lo conservo", "keep", offer.id)
    await db_session.commit()  # No payment/inventory transaction across Telegram.
    message_id = None
    try:
        sent = await group_registry.send_group_message(
            callback.bot, db_session, text, reply_markup=builder.adjust(1).as_markup(),
            request_timeout=10,
        )
        message_id = sent.message_id
    except Exception as exc:
        log.warning("Tavern gift announcement failed offer_id=%s kind=%s", offer.id, type(exc).__name__)
    await service.finish_publication(db_session, offer.id, offer.publish_lease_until, message_id)
    await db_session.commit()
    return message_id is not None


@router.callback_query(TavernCb.filter(F.action.in_({"offers", "pantry", "consume"})), F.message.chat.type == ChatType.PRIVATE)
async def cb_personal(callback: CallbackQuery, callback_data: TavernCb, db_session: AsyncSession) -> None:
    user_id = callback.from_user.id
    if callback_data.action == "consume":
        key = await consumable_service.consume_unit(db_session, callback_data.ref, user_id)
        if key is None:
            await db_session.rollback()
            await callback.answer("Oggetto già consumato o non più nella tua dispensa.", show_alert=True)
            return
        await db_session.commit()
        await callback.answer("🍽 Consumato! Acquisti e trofei restano invariati.")
        await show_pantry(callback.message, db_session, user_id)
        return
    if callback_data.action == "pantry":
        await show_pantry(callback.message, db_session, user_id, callback_data.ref)
    else:
        offers = await service.recent_offers(db_session, user_id)
        builder = InlineKeyboardBuilder()
        for offer in offers:
            status = {"draft": "da scegliere", "prepared": "da confermare", "cancelled": "annullata", "delivered": "consegnata"}[offer.status]
            if offer.status in {"draft", "prepared"} and utc_now() >= offer.expires_at:
                status = "scaduta"
            _button(builder, f"🎁 Offerta #{offer.id} · {status}", "open", offer.id)
        await callback.message.edit_text(
            "🎁 <b>Le mie offerte</b>\n\nUltime 10. Per iniziare, rispondi a un amico nel gruppo con /offri.",
            reply_markup=_home(builder),
        )
    await callback.answer()


@router.callback_query(TavernCb.filter(F.action.in_({"open", "menu", "stock", "choose_buy", "choose_stock", "confirm", "cancel", "publish"})), F.message.chat.type == ChatType.PRIVATE)
async def cb_offer(callback: CallbackQuery, callback_data: TavernCb, db_session: AsyncSession) -> None:
    data = callback_data
    group_id = group_registry.get_group_id()
    acknowledged = False
    try:
        offer = await service.get_offer(db_session, data.offer_id)
        service.authorize_sender(offer, callback.from_user.id, group_id)
        if data.action in {"menu", "stock"}:
            await _choices(callback, db_session, offer, data.action, data.ref)
        elif data.action in {"choose_buy", "choose_stock"}:
            if offer.status == "draft":
                items = list(consumable_service.get_consumables().values())
                key = items[data.ref].key if data.action == "choose_buy" and 0 <= data.ref < len(items) else None
                offer = await service.prepare_offer(
                    db_session, offer.id, callback.from_user.id, group_id,
                    item_key=key, source_purchase_id=data.ref if data.action == "choose_stock" else None,
                )
                await db_session.commit()
            await show_offer(callback.message, db_session, offer, edit=True)
        elif data.action == "cancel":
            await service.cancel_offer(db_session, offer.id, callback.from_user.id, group_id)
            await db_session.commit()
            await show_offer(callback.message, db_session, await service.get_offer(db_session, offer.id), edit=True)
        elif data.action == "confirm":
            await db_session.commit()  # Release reads before membership network calls.
            # Callback answers expire sooner than slow membership checks can.
            # Acknowledge before any network/payment; subsequent feedback is DM.
            await callback.answer("Verifico l'offerta…")
            acknowledged = True
            if not await _member(callback.bot, offer.sender_tg_id) or not await _member(callback.bot, offer.recipient_tg_id):
                raise service.TavernError("Non posso verificare che entrambi siate ancora nel gruppo. Nessun nuovo addebito.")
            offer, delivered = await service.deliver_offer(db_session, offer.id, callback.from_user.id, group_id)
            newly = []
            if delivered and offer.source_purchase_id is None:
                newly = await badge_service.check_and_award_milestones(db_session, offer.sender_tg_id)
            await db_session.commit()
            if newly:
                await announce_trophies(callback.bot, db_session, offer.sender_tg_id, newly)
            await publish_offer(callback, db_session, offer)
            await show_offer(callback.message, db_session, await service.get_offer(db_session, offer.id), edit=True)
            return
        elif data.action == "publish":
            if offer.status != "delivered":
                raise service.TavernError("Conferma prima l'offerta.")
            await db_session.commit()
            await callback.answer("Provo a pubblicare l'offerta, senza nuovi addebiti.")
            await publish_offer(callback, db_session, offer)
            await show_offer(callback.message, db_session, await service.get_offer(db_session, offer.id), edit=True)
            return
        else:
            await show_offer(callback.message, db_session, offer, edit=True)
        await callback.answer()
    except (service.TavernError, InsufficientFundsError, WalletNotFoundError) as exc:
        await db_session.rollback()
        if acknowledged:
            await callback.message.answer(str(exc))
        else:
            await callback.answer(str(exc), show_alert=True)


@router.callback_query(TavernCb.filter(F.action.in_({"drink", "keep"})))
async def cb_receive(callback: CallbackQuery, callback_data: TavernCb, db_session: AsyncSession) -> None:
    data = callback_data
    try:
        offer = await service.get_offer(db_session, data.offer_id)
        if (
            offer.status != "delivered" or callback.from_user.id != offer.recipient_tg_id
            or callback.message.chat.id != group_registry.get_group_id()
            or offer.group_id != group_registry.get_group_id()
            or offer.message_id != callback.message.message_id
        ):
            raise service.TavernError("Può scegliere soltanto il destinatario di questa offerta.")
        if data.action == "keep":
            await callback.answer("🎒 È già nella tua dispensa: puoi consumarlo quando vuoi.", show_alert=True)
            await callback.message.edit_reply_markup(reply_markup=None)
            return
        if data.ref != offer.purchase_id:
            raise service.TavernError("Questo bottone non corrisponde al regalo.")
        key = await consumable_service.consume_unit(db_session, data.ref, callback.from_user.id)
        if key is None:
            raise service.TavernError("Oggetto già consumato o non più nella tua dispensa.")
        await db_session.commit()
        await callback.answer("🍻 Alla salute!")
        await callback.message.edit_reply_markup(reply_markup=None)
    except service.TavernError as exc:
        await db_session.rollback()
        await callback.answer(str(exc), show_alert=True)
