"""
Betting handlers.

Group vs private split:
  /scommesse      — works everywhere; in group shows URL buttons, in private shows callbacks
  /crea_scommessa — in group sends a deep-link button to private; in private starts FSM

State naming is explicit: BetCreationStates and BetCustomAmountState are separate
StatesGroups so they can never collide.

Active-message tracking:
  FSM data key `bet_active_msg_id` holds the message_id of the one interactive
  betting message open for a user. New entry-points delete the old tracked message
  before sending a fresh one — never more than one live prompt per user.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters.command import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database.models import BettingEvent, EventStatus, Wallet
from exceptions.economy import (
    AlreadyBetError,
    BettingClosedError,
    EventNotFoundError,
    InsufficientFundsError,
)
from keyboards.betting_kb import (
    get_amount_keyboard,
    get_confirm_bet_keyboard,
    get_events_keyboard,
    get_group_events_keyboard,
    get_options_keyboard,
)
from config_data.config import settings
from keyboards.common_kb import confirm_cancel_kb
from services import badge_service, bet_service
from utils import cooldown
from utils.text import esc

router = Router()


class BetCreationStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_options = State()


class BetCustomAmountState(StatesGroup):
    waiting_for_amount = State()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cancel_creation_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Annulla", callback_data="bet:cancel_creation")
    ]])


async def _clear_active_bet_msg(bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    msg_id = data.get("bet_active_msg_id")
    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        await state.update_data(bet_active_msg_id=None)


# ---------------------------------------------------------------------------
# /scommesse
# ---------------------------------------------------------------------------

@router.message(Command("scommesse"))
async def cmd_scommesse(message: Message, db_session: AsyncSession, state: FSMContext) -> None:
    events = await bet_service.get_open_events(db_session)

    if not events:
        await message.answer(
            "🎲 <b>Nessuna scommessa aperta al momento.</b>\n\n"
            "<i>Torna più tardi o chiedi a un admin di crearne una!</i>"
        )
        return

    if message.chat.type != "private":
        bot_info = await message.bot.get_me()
        sent = await message.answer(
            f"🎲 <b>{len(events)} scommess{'a aperta' if len(events) == 1 else 'e aperte'}</b>\n\n"
            "Tocca per scommettere in privato:",
            reply_markup=get_group_events_keyboard(events, bot_info.username),
        )
        return

    await _clear_active_bet_msg(message.bot, message.chat.id, state)
    sent = await message.answer(
        f"🎲 <b>{len(events)} scommess{'a aperta' if len(events) == 1 else 'e aperte'}</b>\n\n"
        "Seleziona un evento:",
        reply_markup=get_events_keyboard(events),
    )
    await state.update_data(bet_active_msg_id=sent.message_id)


# ---------------------------------------------------------------------------
# /crea_scommessa + FSM creation
# ---------------------------------------------------------------------------

@router.message(Command("crea_scommessa"))
async def cmd_crea_scommessa(message: Message, state: FSMContext) -> None:
    if message.chat.type != "private":
        bot_info = await message.bot.get_me()
        await message.answer(
            "🎲 Crea la scommessa in chat privata:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="➡️ Crea scommessa",
                    url=f"https://t.me/{bot_info.username}?start=create_bet",
                )
            ]]),
        )
        return

    if not await cooldown.guard(
        message, "event_create", settings.event_create_cooldown_seconds, exempt_admin=False
    ):
        return
    await state.clear()
    await state.set_state(BetCreationStates.waiting_for_title)
    await message.answer(
        "🎲 <b>Crea una nuova scommessa</b>\n\n"
        "<b>Step 1/3</b> — Invia il titolo dell'evento (max 200 caratteri):",
        reply_markup=_cancel_creation_kb(),
    )


@router.callback_query(F.data == "bet:cancel_creation")
async def cb_cancel_creation(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() is None:
        await callback.answer()
        return
    await callback.message.answer(
        "⚠️ Sicuro di voler annullare la creazione della scommessa? I dati inseriti andranno persi.",
        reply_markup=confirm_cancel_kb("bet:cancel_yes", "bet:cancel_no"),
    )
    await callback.answer()


@router.callback_query(F.data == "bet:cancel_yes")
async def cb_cancel_creation_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Creazione scommessa annullata.")
    await callback.answer()


@router.callback_query(F.data == "bet:cancel_no")
async def cb_cancel_creation_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("▶️ Ok, continua pure da dove eri rimasto.")
    await callback.answer()


@router.message(BetCreationStates.waiting_for_title, ~F.text.startswith("/"))
async def fsm_bet_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:200]
    if len(title) < 4:
        await message.answer("⚠️ Il titolo deve avere almeno 4 caratteri.", reply_markup=_cancel_creation_kb())
        return
    await state.update_data(title=title)
    await state.set_state(BetCreationStates.waiting_for_description)
    await message.answer(
        f"✅ Titolo: <b>{esc(title)}</b>\n\n"
        "<b>Step 2/3</b> — Invia una breve descrizione (max 500 caratteri):",
        reply_markup=_cancel_creation_kb(),
    )


@router.message(BetCreationStates.waiting_for_description, ~F.text.startswith("/"))
async def fsm_bet_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()[:500]
    if len(description) < 4:
        await message.answer("⚠️ La descrizione deve avere almeno 4 caratteri.", reply_markup=_cancel_creation_kb())
        return
    await state.update_data(description=description)
    await state.set_state(BetCreationStates.waiting_for_options)
    await message.answer(
        "<b>Step 3/3</b> — Invia le opzioni, <b>una per riga</b> (min 2, max 8).\n\n"
        "Esempio:\n<code>Squadra A\nSquadra B\nPareggio</code>",
        reply_markup=_cancel_creation_kb(),
    )


@router.message(BetCreationStates.waiting_for_options, ~F.text.startswith("/"))
async def fsm_bet_options(
    message: Message, state: FSMContext, db_session: AsyncSession
) -> None:
    raw = (message.text or "").strip()
    options = [o.strip() for o in raw.splitlines() if o.strip()]
    if len(options) < 2:
        await message.answer("⚠️ Servono almeno 2 opzioni (una per riga).", reply_markup=_cancel_creation_kb())
        return
    if len(options) > 8:
        await message.answer("⚠️ Massimo 8 opzioni.", reply_markup=_cancel_creation_kb())
        return
    for opt in options:
        if len(opt) > 100:
            await message.answer("⚠️ Ogni opzione deve essere max 100 caratteri.", reply_markup=_cancel_creation_kb())
            return

    data = await state.get_data()
    title = data["title"]
    description = data["description"]
    as_draft = bool(data.get("bet_as_draft", False))

    event = await bet_service.create_event(
        db_session,
        creator_tg_id=message.from_user.id,
        title=title,
        description=description,
        options=[{"label": o} for o in options],
        status=EventStatus.draft.value if as_draft else EventStatus.open.value,
    )
    await db_session.commit()
    await state.clear()

    opts_text = "\n".join(f"• {esc(o)}" for o in options)
    if as_draft:
        await message.answer(
            f"✅ <b>Scommessa creata in bozza!</b>\n\n"
            f"<b>#{event.id} {esc(title)}</b>\n"
            f"{esc(description)}\n\n"
            f"<b>Opzioni:</b>\n{opts_text}\n\n"
            f"Avviala subito o programmala dagli 🎬 Eventi (/admin → Eventi → Scommesse)."
        )
    else:
        await message.answer(
            f"✅ <b>Scommessa creata!</b>\n\n"
            f"<b>#{event.id} {esc(title)}</b>\n"
            f"{esc(description)}\n\n"
            f"<b>Opzioni:</b>\n{opts_text}\n\n"
            f"Usa /scommesse per vederla."
        )


# ---------------------------------------------------------------------------
# Entry-points from deep-links
# ---------------------------------------------------------------------------

async def start_bet_creation(
    message: Message, state: FSMContext, as_draft: bool = False
) -> None:
    """Start the bet-creation FSM. ``as_draft`` (Events hub) creates a draft to be
    activated/scheduled later; otherwise (deep-link/community) it opens directly."""
    await state.clear()
    await state.update_data(bet_as_draft=as_draft)
    await state.set_state(BetCreationStates.waiting_for_title)
    intro = (
        "🎲 <b>Crea una scommessa</b> <i>(bozza, da avviare o programmare)</i>"
        if as_draft else "🎲 <b>Crea una nuova scommessa</b>"
    )
    await message.answer(
        f"{intro}\n\n"
        "<b>Step 1/3</b> — Invia il titolo dell'evento (max 200 caratteri):",
        reply_markup=_cancel_creation_kb(),
    )


async def start_bet_view(
    message: Message, db_session: AsyncSession, event_id: int, state: FSMContext
) -> None:
    """Called from common.cmd_start with payload=bet_<event_id>."""
    result = await db_session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event_id)
        .options(selectinload(BettingEvent.options))
    )
    event = result.scalar_one_or_none()
    if event is None or event.status != "open":
        await message.answer("⚠️ Scommessa non trovata o non più disponibile.")
        return

    await _clear_active_bet_msg(message.bot, message.chat.id, state)
    total = sum(o.total_wagered for o in event.options)
    sent = await message.answer(
        f"🎲 <b>#{event.id} {esc(event.title)}</b>\n\n"
        f"<i>{esc(event.description)}</i>\n\n"
        f"💰 Pool totale: <b>{total} 🪙</b>\n\n"
        "Scegli un'opzione:",
        reply_markup=get_options_keyboard(event_id, event.options),
    )
    await state.update_data(bet_active_msg_id=sent.message_id)


async def start_custom_amount(
    message: Message, state: FSMContext, event_id: int, option_id: int
) -> None:
    """Called from common.cmd_start with payload=bet_custom_<e>_<o>."""
    await _clear_active_bet_msg(message.bot, message.chat.id, state)
    await state.update_data(custom_bet_event=event_id, custom_bet_option=option_id)
    await state.set_state(BetCustomAmountState.waiting_for_amount)
    sent = await message.answer(
        "✏️ Inserisci l'importo che vuoi scommettere (in Aldueuri):"
    )
    await state.update_data(bet_active_msg_id=sent.message_id)


# ---------------------------------------------------------------------------
# Callbacks: event browsing
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("event:view:"))
async def cb_event_view(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext) -> None:
    try:
        event_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback.answer("⚠️ ID non valido.", show_alert=True)
        return

    result = await db_session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event_id)
        .options(selectinload(BettingEvent.options))
    )
    event = result.scalar_one_or_none()
    if event is None:
        await callback.answer("⚠️ Evento non trovato.", show_alert=True)
        return
    if event.status != "open":
        await callback.message.edit_text(
            f"🔒 <b>#{event.id} {esc(event.title)}</b>\n\n"
            f"<i>Questa scommessa non accetta più puntate.</i>"
        )
        await callback.answer()
        return

    total = sum(o.total_wagered for o in event.options)
    await callback.message.edit_text(
        f"🎲 <b>#{event.id} {esc(event.title)}</b>\n\n"
        f"<i>{esc(event.description)}</i>\n\n"
        f"💰 Pool totale: <b>{total} 🪙</b>\n\n"
        "Scegli un'opzione:",
        reply_markup=get_options_keyboard(event_id, event.options),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bet_option:"))
async def cb_bet_option(
    callback: CallbackQuery, db_session: AsyncSession
) -> None:
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("⚠️ Dati non validi.", show_alert=True)
        return
    try:
        event_id = int(parts[1])
        option_id = int(parts[2])
    except ValueError:
        await callback.answer("⚠️ ID non valido.", show_alert=True)
        return

    event_result = await db_session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event_id)
        .options(selectinload(BettingEvent.options))
    )
    event = event_result.scalar_one_or_none()
    if event is None or event.status != "open":
        await callback.answer("⚠️ Evento non disponibile.", show_alert=True)
        return

    wallet_result = await db_session.execute(
        select(Wallet).where(Wallet.tg_id == callback.from_user.id)
    )
    wallet = wallet_result.scalar_one_or_none()
    balance = wallet.coins if wallet else 0

    option = next((o for o in event.options if o.id == option_id), None)
    if option is None:
        await callback.answer("⚠️ Opzione non trovata.", show_alert=True)
        return

    await callback.message.edit_text(
        f"🎲 <b>#{event.id} {esc(event.title)}</b>\n\n"
        f"✅ Opzione scelta: <b>{esc(option.label)}</b>\n"
        f"💰 Pool su questa opzione: <b>{option.total_wagered} 🪙</b>\n\n"
        f"🪙 Il tuo saldo: <b>{balance} 🪙</b>\n\n"
        "Quanto vuoi puntare?",
        reply_markup=get_amount_keyboard(event_id, option_id, balance),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bet_amount:"))
async def cb_bet_amount(
    callback: CallbackQuery, db_session: AsyncSession, state: FSMContext
) -> None:
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("⚠️ Dati non validi.", show_alert=True)
        return
    try:
        event_id = int(parts[1])
        option_id = int(parts[2])
        amount = int(parts[3])
    except ValueError:
        await callback.answer("⚠️ Importo non valido.", show_alert=True)
        return

    if amount <= 0:
        await callback.answer("⚠️ L'importo deve essere positivo.", show_alert=True)
        return

    await _show_confirm(callback, db_session, event_id, option_id, amount)


async def _show_confirm(
    callback: CallbackQuery,
    db_session: AsyncSession,
    event_id: int,
    option_id: int,
    amount: int,
) -> None:
    result = await db_session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event_id)
        .options(selectinload(BettingEvent.options))
    )
    event = result.scalar_one_or_none()
    if event is None or event.status != "open":
        await callback.answer("⚠️ Evento non disponibile.", show_alert=True)
        return

    option = next((o for o in event.options if o.id == option_id), None)
    if option is None:
        await callback.answer("⚠️ Opzione non trovata.", show_alert=True)
        return

    # Proportional payout estimate
    total_pool = sum(o.total_wagered for o in event.options) + amount
    winning_pool_after = option.total_wagered + amount
    estimated = (
        int((amount / winning_pool_after) * total_pool)
        if winning_pool_after > 0
        else 0
    )

    await callback.message.edit_text(
        f"🎲 <b>#{event.id} {esc(event.title)}</b>\n\n"
        f"✅ Opzione: <b>{esc(option.label)}</b>\n"
        f"💸 Puntata: <b>{amount} 🪙</b>\n\n"
        f"📈 Stima payout attuale: ~<b>{estimated} 🪙</b>\n"
        f"<i>Il payout finale dipende da quanti altri scommettono (stile Twitch).</i>\n\n"
        "Confermi?",
        reply_markup=get_confirm_bet_keyboard(event_id, option_id, amount),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bet_custom:"))
async def cb_bet_custom(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("⚠️ Dati non validi.", show_alert=True)
        return
    try:
        event_id = int(parts[1])
        option_id = int(parts[2])
    except ValueError:
        await callback.answer("⚠️ ID non valido.", show_alert=True)
        return

    await state.update_data(custom_bet_event=event_id, custom_bet_option=option_id)
    await state.update_data(bet_active_msg_id=callback.message.message_id)
    await state.set_state(BetCustomAmountState.waiting_for_amount)
    await callback.message.edit_text(
        "✏️ Inserisci l'importo che vuoi scommettere (in Aldueuri):\n\n"
        "<i>Invia solo il numero, es: <code>150</code></i>"
    )
    await callback.answer()


@router.message(BetCustomAmountState.waiting_for_amount, ~F.text.startswith("/"))
async def fsm_custom_amount(
    message: Message, state: FSMContext, db_session: AsyncSession
) -> None:
    raw = (message.text or "").strip()
    try:
        amount = int(raw)
    except ValueError:
        await message.answer("⚠️ Inserisci solo un numero intero, es: <code>150</code>")
        return

    if amount <= 0:
        await message.answer("⚠️ L'importo deve essere positivo.")
        return

    data = await state.get_data()
    event_id = data.get("custom_bet_event")
    option_id = data.get("custom_bet_option")
    if event_id is None or option_id is None:
        await state.clear()
        await message.answer("⚠️ Sessione scaduta. Usa /scommesse per ricominciare.")
        return

    # Delete the FSM prompt message
    await _clear_active_bet_msg(message.bot, message.chat.id, state)

    result = await db_session.execute(
        select(BettingEvent)
        .where(BettingEvent.id == event_id)
        .options(selectinload(BettingEvent.options))
    )
    event = result.scalar_one_or_none()
    if event is None or event.status != "open":
        await state.clear()
        await message.answer("⚠️ Evento non disponibile. Usa /scommesse per ricominciare.")
        return

    option = next((o for o in event.options if o.id == option_id), None)
    if option is None:
        await state.clear()
        await message.answer("⚠️ Opzione non trovata. Usa /scommesse per ricominciare.")
        return

    # Show confirm screen
    total_pool = sum(o.total_wagered for o in event.options) + amount
    winning_pool_after = option.total_wagered + amount
    estimated = int((amount / winning_pool_after) * total_pool) if winning_pool_after > 0 else 0

    sent = await message.answer(
        f"🎲 <b>#{event.id} {esc(event.title)}</b>\n\n"
        f"✅ Opzione: <b>{esc(option.label)}</b>\n"
        f"💸 Puntata: <b>{amount} 🪙</b>\n\n"
        f"📈 Stima payout attuale: ~<b>{estimated} 🪙</b>\n"
        f"<i>Il payout finale dipende da quanti altri scommettono (stile Twitch).</i>\n\n"
        "Confermi?",
        reply_markup=get_confirm_bet_keyboard(event_id, option_id, amount),
    )
    await state.update_data(bet_active_msg_id=sent.message_id)
    await state.clear()


@router.callback_query(F.data.startswith("bet_confirm:"))
async def cb_bet_confirm(
    callback: CallbackQuery, db_session: AsyncSession, state: FSMContext
) -> None:
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("⚠️ Dati non validi.", show_alert=True)
        return
    try:
        event_id = int(parts[1])
        option_id = int(parts[2])
        amount = int(parts[3])
    except ValueError:
        await callback.answer("⚠️ Importo non valido.", show_alert=True)
        return

    try:
        await bet_service.place_bet(
            db_session, callback.from_user.id, event_id, option_id, amount
        )
        await db_session.commit()
    except AlreadyBetError:
        await callback.answer(
            "⚠️ Hai già scommesso su questo evento.", show_alert=True
        )
        return
    except BettingClosedError:
        await callback.answer("⚠️ Questa scommessa non accetta più puntate.", show_alert=True)
        return
    except EventNotFoundError:
        await callback.answer("⚠️ Evento non trovato.", show_alert=True)
        return
    except InsufficientFundsError as e:
        await callback.answer(
            f"⚠️ Saldo insufficiente: hai {e.balance} 🪙, servono {e.required} 🪙.",
            show_alert=True,
        )
        return

    newly_earned = await badge_service.check_and_award_milestones(
        db_session, callback.from_user.id
    )
    if newly_earned:
        await db_session.commit()

    text = (
        f"✅ <b>Scommessa piazzata!</b>\n\n"
        f"💸 Hai puntato <b>{amount} 🪙</b>.\n"
        f"<i>Ti notificheremo quando l'evento viene risolto.</i>"
    )
    if newly_earned:
        badges_text = ", ".join(f"{b.icon_emoji} {esc(b.name)}" for b in newly_earned)
        text += f"\n\n🏅 <b>Badge sbloccato:</b> {badges_text}!"

    await callback.message.edit_text(text)
    await state.update_data(bet_active_msg_id=None)
    await callback.answer("✅ Scommessa registrata!")


@router.callback_query(F.data == "bet:back")
async def cb_bet_back(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext) -> None:
    events = await bet_service.get_open_events(db_session)
    if not events:
        await callback.message.edit_text("🎲 Nessuna scommessa aperta.")
        await callback.answer()
        return
    await callback.message.edit_text(
        f"🎲 <b>{len(events)} scommess{'a aperta' if len(events) == 1 else 'e aperte'}</b>\n\n"
        "Seleziona un evento:",
        reply_markup=get_events_keyboard(events),
    )
    await callback.answer()


@router.callback_query(F.data == "bet:close")
async def cb_bet_close(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        await callback.message.delete()
    except Exception:
        pass
    await state.update_data(bet_active_msg_id=None)
    await callback.answer()
