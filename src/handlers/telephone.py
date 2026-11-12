"""Telegram orchestration for the private, restart-safe image relay."""

from __future__ import annotations

import asyncio
import logging
import traceback
from weakref import WeakValueDictionary

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    InputMediaPhoto,
    Message,
    ReplyParameters,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.callbacks import EventCb, TelephoneCb
from filters.admin_filter import IsAdminCallbackFilter
from services import (
    admin_service,
    ai_budget,
    group_registry,
    image_service,
    telephone_service as game,
)
from utils.text import esc

router = Router()
log = logging.getLogger(__name__)
# Presentation only, never the claim/money authority. Weak entries cannot grow
# forever; SQL fences remain authoritative across processes and restarts.
_card_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_REASONS = {
    "disabled": "La staffetta non è abilitata.",
    "closed": "Questa staffetta è terminata o scaduta.",
    "busy": "Qualcuno ha appena preso la staffetta. Riprova più tardi!",
    "seen": "Hai già visto un passaggio: in questa catena si partecipa una sola volta.",
    "resume": "La staffetta è già tua: rispondi al messaggio privato con l'immagine o la richiesta iniziale. Il tempo non riparte.",
    "retry": "Il tuo passaggio è in pausa. Puoi riprovare una sola volta o lasciare la staffetta; non ci sono tentativi automatici.",
}


def _retry_keyboard(game_id: int):
    b = InlineKeyboardBuilder()
    b.button(
        text="🎨 Riprova una volta",
        callback_data=TelephoneCb(action="retry", game_id=game_id).pack(),
    )
    b.button(
        text="Lascia la staffetta",
        callback_data=TelephoneCb(action="leave", game_id=game_id).pack(),
    )
    b.adjust(1)
    return b.as_markup()


async def _member(bot, user_id: int) -> bool:
    """Fail closed for private game material, including transient Telegram errors."""
    group_id = group_registry.get_group_id()
    if not group_id:
        return False
    try:
        member = await bot.get_chat_member(group_id, user_id, request_timeout=10)
        return member.status in ("member", "administrator", "creator") or (
            member.status == "restricted" and member.is_member
        )
    except Exception as exc:  # noqa: BLE001 — no secret delivered on an uncertain membership
        log.info("Telephone membership unavailable error=%s", type(exc).__name__)
        return False


def _gallery(view: game.View, number: int):
    stage = view.stages[number - 1]
    ending = (
        "🏁 Catena completa!"
        if view.reason == "completed"
        else "🏁 Staffetta chiusa: ecco la catena raccolta."
    )
    caption = (
        f"📞 <b>Telefono senza fili #{view.id}</b>\n{ending}\n\n"
        f"<b>{number}/{len(view.stages)}</b> · {esc(stage.author)}\n"
        f"{esc(stage.description)}\n\nSfoglia per vedere come è cambiata la scena."
    )
    b = InlineKeyboardBuilder()
    if number > 1:
        b.button(
            text="⬅️ Prima",
            callback_data=TelephoneCb(action="view", game_id=view.id, stage=number - 1).pack(),
        )
    if number < len(view.stages):
        b.button(
            text="Dopo ➡️",
            callback_data=TelephoneCb(action="view", game_id=view.id, stage=number + 1).pack(),
        )
    b.adjust(2)
    return InputMediaPhoto(media=stage.file_id, caption=caption, parse_mode="HTML"), b.as_markup()


async def publish_card(bot, session: AsyncSession, game_id: int, number: int = 1) -> None:
    lock = _card_locks.setdefault(game_id, asyncio.Lock())
    async with lock:
        view = await game.get_view(session, game_id)
        await session.rollback()  # never hold a DB transaction over Telegram
        if view is None or not view.group_id or view.status == "ready":
            return
        anchor = view.anchor
        b = InlineKeyboardBuilder()
        if view.status == "running":
            me = await bot.get_me()
            b.button(
                text="📞 Prendo la staffetta",
                url=f"https://t.me/{me.username}?start=telefono_{game_id}",
            )
            activity = (
                "⏸️ Immagine non riuscita: passaggio recuperabile."
                if view.kind == "retry"
                else "🎨 Sto creando l'immagine."
                if view.kind == "generating"
                else (
                    "✍️ Un partecipante ha la staffetta." if view.token else "🟢 Passaggio libero!"
                )
            )
            text = (
                f"📞 <b>Telefono senza fili #{game_id}</b>\n\n"
                f"Passaggio <b>{view.stage}/{game.STAGES}</b> · {activity}\n\n"
                "Il primo inventa una scena; gli altri vedono solo l'ultima immagine "
                "e la descrivono in privato. L'AI ridisegna alla cieca.\n"
                "4 persone diverse · 150 caratteri · 2 minuti per scrivere.\n"
                "Alla fine sveliamo la catena! La partita si chiude entro 30 minuti."
            )
        else:
            text = f"📞 <b>Telefono senza fili #{game_id}</b>\nStaffetta chiusa senza immagini: riproviamo un'altra volta!"
        try:
            if view.status == "finished" and view.stages and view.anchor:
                media, kb = _gallery(view, max(1, min(number, len(view.stages))))
                await bot.edit_message_media(
                    chat_id=view.group_id, message_id=view.anchor, media=media, reply_markup=kb
                )
            elif view.anchor:
                await bot.edit_message_text(
                    chat_id=view.group_id,
                    message_id=view.anchor,
                    text=text,
                    reply_markup=b.as_markup(),
                    parse_mode="HTML",
                )
            else:
                # Initial publication has the usual Telegram send/DB-save crash
                # window. Later progress/reveal edits the same durable message.
                message = await bot.send_message(
                    view.group_id, text, reply_markup=b.as_markup(), parse_mode="HTML"
                )
                await game.set_anchor(session, game_id, message.message_id)
                await session.commit()
                anchor = message.message_id
                if view.status == "finished" and view.stages:
                    media, kb = _gallery(view, 1)
                    await bot.edit_message_media(
                        chat_id=view.group_id,
                        message_id=message.message_id,
                        media=media,
                        reply_markup=kb,
                    )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        if view.status == "finished" and not view.announced:
            claimed = await game.claim_reveal_notice(session, game_id)
            await session.commit()
            if claimed:
                # Best-effort, at-most-once notification. A crash here may
                # lose the ping, but cannot spam it on every gallery click.
                await bot.send_message(
                    view.group_id,
                    "📞 Staffetta conclusa! Il risultato è nella card qui sopra: sfoglia la catena 👆"
                    if view.stages
                    else "📞 Staffetta chiusa senza immagini. Riproviamo un'altra volta!",
                    reply_parameters=ReplyParameters(
                        message_id=anchor, allow_sending_without_reply=True
                    ),
                )


async def _refresh(bot, session: AsyncSession, game_id: int) -> None:
    try:
        await publish_card(bot, session, game_id)
    except Exception as exc:  # noqa: BLE001 — Telegram cannot roll back a durable turn
        await session.rollback()
        log.warning("Telephone card unavailable game=%s error=%s", game_id, type(exc).__name__)


async def start_creation(
    message: Message, state: FSMContext, creator_id: int, session: AsyncSession
) -> None:
    await state.clear()
    try:
        game_id = await game.create(session, creator_id)
    except ValueError as exc:
        await session.rollback()
        await message.answer(esc(str(exc)))
        return
    await admin_service.log_action(
        session, creator_id, "telephone_create", detail=f"game={game_id}"
    )
    await session.commit()
    b = InlineKeyboardBuilder()
    b.button(
        text="📞 Apri la staffetta",
        callback_data=EventCb(action="item", task_type=game.GAME_TYPE, item_id=game_id).pack(),
    )
    await message.answer(
        "📞 Staffetta pronta: 4 immagini, 4 persone, massimo 30 minuti. Nessun premio in CoInn/XP. Avviala o programmala dagli Eventi.",
        reply_markup=b.as_markup(),
    )


@router.callback_query(
    TelephoneCb.filter(F.action == "create"),
    IsAdminCallbackFilter(),
    F.message.chat.type == "private",
)
async def create(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await callback.answer()
    await start_creation(callback.message, state, callback.from_user.id, db_session)


async def join(message: Message, state: FSMContext, session: AsyncSession, game_id: int) -> None:
    if message.chat.type != "private":
        await session.rollback()
        return
    if await state.get_state():
        await session.rollback()
        await message.answer(
            "Concludi o annulla prima l'altra operazione, poi riapri la staffetta."
        )
        return
    await session.rollback()
    if not await _member(message.bot, message.from_user.id):
        await message.answer("Non riesco a verificare l'accesso al gruppo. Riprova più tardi.")
        return
    c, reason = await game.claim(
        session,
        game_id,
        message.from_user.id,
        message.from_user.full_name,
        group_registry.get_group_id(),
    )
    await session.commit()
    if c is None:
        await message.answer(
            _REASONS[reason], reply_markup=_retry_keyboard(game_id) if reason == "retry" else None
        )
        return
    prompt = (
        "📞 Inventa una scena buffa in massimo 150 caratteri."
        if c.stage == 1
        else "📞 Descrivi SOLO quello che vedi in massimo 150 caratteri: il prossimo vedrà il disegno ricreato dalla tua descrizione."
    ) + "\nHai 2 minuti. Rispondi a questo messaggio. Non condividere il passaggio nel gruppo!"
    try:
        if c.previous_file_id:
            sent = await message.bot.send_photo(
                c.user_id,
                c.previous_file_id,
                caption=prompt,
                reply_markup=ForceReply(selective=True),
                protect_content=True,
                request_timeout=20,
            )
        else:
            sent = await message.bot.send_message(
                c.user_id,
                prompt,
                reply_markup=ForceReply(selective=True),
                protect_content=True,
                request_timeout=20,
            )
        ok = await game.delivered(session, c, sent.message_id)
        await session.commit()
        if not ok:
            await message.answer(
                "Il passaggio è scaduto durante la consegna: non inviare la descrizione."
            )
    except Exception as exc:  # noqa: BLE001 — retain the seen visit, release only this token
        await session.rollback()
        await game.release(session, c.game_id, c.token)
        await session.commit()
        log.warning("Telephone delivery failed game=%s error=%s", game_id, type(exc).__name__)
    await _refresh(message.bot, session, game_id)


@router.message(
    F.chat.type == "private", StateFilter(None), F.reply_to_message, F.text, ~F.text.startswith("/")
)
async def reply(message: Message, db_session: AsyncSession) -> None:
    c = await game.reply_claim(
        db_session, message.from_user.id, message.reply_to_message.message_id
    )
    if c is None:
        await db_session.rollback()
        raise SkipHandler
    description = message.text.strip()
    if not 1 <= len(description) <= 150:
        await db_session.rollback()
        await message.answer("Usa da 1 a 150 caratteri e rispondi al messaggio del passaggio.")
        return
    await db_session.rollback()
    if not await _member(message.bot, message.from_user.id):
        await message.answer("Accesso al gruppo non verificato: nessuna immagine generata.")
        return
    view = await game.begin_generation(db_session, c, description)
    await db_session.commit()
    if view is None:
        await message.answer(
            "Passaggio già inviato, scaduto o chiuso: nessuna nuova immagine generata."
        )
        return
    await _run_generation(message, db_session, c, view, description)


async def _run_generation(
    message: Message,
    db_session: AsyncSession,
    c: game.Claim,
    view: game.View,
    description: str,
) -> None:
    try:
        await message.answer(
            "🎨 Ricevuto! Creo l'immagine, poi libero la staffetta.", request_timeout=15
        )
        image = await image_service.generate(
            description, model=view.model, provider=view.provider, cap_microusd=view.cap
        )
        sent = await message.bot.send_photo(
            c.user_id,
            BufferedInputFile(image, filename="staffetta.png"),
            caption="🎨 Ecco il tuo passaggio! Non condividerlo prima del finale.",
            protect_content=True,
            request_timeout=20,
        )
        await game.complete(db_session, c, sent.photo[-1].file_id)
        await db_session.commit()
    except Exception as exc:  # noqa: BLE001 — no blind paid retry; reveal any completed prefix
        await db_session.rollback()
        recoverable = isinstance(exc, image_service.ImageError) and exc.retryable
        paused = recoverable and await game.pause_generation(db_session, c)
        if not paused:
            await game.finish(db_session, c.game_id, "image_error")
        await db_session.commit()
        frames = traceback.extract_tb(exc.__traceback__)
        location = f"{frames[-1].name}:{frames[-1].lineno}" if frames else "unknown"
        log.warning(
            "Telephone generation stopped game=%s error=%s stage=%s kind=%s http_status=%s paused=%s location=%s",
            c.game_id,
            type(exc).__name__,
            getattr(exc, "stage", None),
            getattr(exc, "kind", None),
            getattr(exc, "http_status", None),
            paused,
            location,
        )
        if paused:
            reason = (
                "Il provider immagini non ha risposto in tempo."
                if exc.kind == "timeout"
                else "Il servizio immagini ha avuto un problema temporaneo."
            )
            await message.answer(
                f"⏸️ {reason} La staffetta è salva.\n"
                f"Puoi confermare un solo altro tentativo (prenotazione massima ${view.cap / 1_000_000:.3f}), "
                "oppure lasciare il passaggio a un'altra persona. Tra 2 minuti torna libero. "
                "Non ho ritentato automaticamente.",
                reply_markup=_retry_keyboard(c.game_id),
            )
        elif isinstance(exc, ai_budget.AIBudgetError):
            await message.answer(
                "Il budget immagini è esaurito o non verificabile. Chiudo la staffetta conservando i passaggi già raccolti, senza altre chiamate a pagamento."
            )
        else:
            await message.answer(
                "Non sono riuscito a completare l'immagine. Chiudo la staffetta mostrando gli eventuali passaggi già raccolti, senza ritentare a pagamento."
            )
    await _refresh(message.bot, db_session, c.game_id)


@router.callback_query(
    TelephoneCb.filter(F.action.in_({"retry", "leave"})), F.message.chat.type == "private"
)
async def retry_control(
    callback: CallbackQuery,
    callback_data: TelephoneCb,
    state: FSMContext,
    db_session: AsyncSession,
) -> None:
    await db_session.rollback()
    if await state.get_state():
        await callback.answer("Concludi prima l'altra operazione in corso.", show_alert=True)
        return
    if callback_data.action == "leave":
        ok = await game.abandon_retry(db_session, callback_data.game_id, callback.from_user.id)
        await db_session.commit()
        await callback.answer("Staffetta liberata." if ok else "Passaggio già ripreso o scaduto.")
        await _refresh(callback.bot, db_session, callback_data.game_id)
        return
    if not await _member(callback.bot, callback.from_user.id):
        await callback.answer("Accesso al gruppo non verificato.", show_alert=True)
        return
    result = await game.retry_generation(db_session, callback_data.game_id, callback.from_user.id)
    await db_session.commit()
    if result is None:
        await callback.answer("Tentativo già avviato, esaurito o scaduto.")
        return
    await callback.answer()
    c, view, description = result
    await _run_generation(callback.message, db_session, c, view, description)


@router.callback_query(TelephoneCb.filter(F.action == "view"))
async def gallery(
    callback: CallbackQuery, callback_data: TelephoneCb, db_session: AsyncSession
) -> None:
    view = await game.get_view(db_session, callback_data.game_id)
    await db_session.rollback()
    if (
        view is None
        or view.status != "finished"
        or not view.stages
        or callback.message is None
        or callback.message.chat.id != view.group_id
        or callback.message.message_id != view.anchor
        or not 1 <= callback_data.stage <= len(view.stages)
    ):
        await callback.answer("Galleria non disponibile.")
        return
    await callback.answer()
    await publish_card(callback.bot, db_session, view.id, callback_data.stage)
