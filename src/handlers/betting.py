"""
Betting handlers.

Group vs private split:
  /scommesse      — private only; in group redirects with a deep-link button (the list
                    marks which events the caller already bet on = personal data)
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
from aiogram.utils.keyboard import InlineKeyboardBuilder
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
    get_options_keyboard,
)
from config_data.config import settings
from handlers._privacy import redirect_to_private
from handlers._trophy_announce import announce_trophies
from keyboards.common_kb import confirm_cancel_kb
from services import badge_service, bet_service, group_registry, schedule_service
from utils import cooldown
from utils.text import esc

router = Router()

# Upper bound on a single bet (mirrors economy_service._MAX_TRANSFER). Besides
# being a sane gameplay limit, it keeps the confirm button's callback_data well
# within Telegram's 64-byte cap — an unbounded amount would overflow it and make
# the API reject the keyboard.
MAX_BET = 1_000_000


class BetCreationStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_options = State()
    waiting_for_window = State()          # pick the betting-window duration (preset)
    waiting_for_window_custom = State()   # or type a custom duration (30m/2h/1d)


# Betting-window presets offered at creation (label, seconds). 0 / illimitata is a
# separate button; a custom value is parsed via schedule_service.parse_duration.
_WINDOW_PRESETS: list[tuple[str, int]] = [
    ("15m", 15 * 60),
    ("30m", 30 * 60),
    ("1h", 60 * 60),
    ("3h", 3 * 60 * 60),
]


def _window_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for label, sec in _WINDOW_PRESETS:
        b.button(text=f"⏱️ {label}", callback_data=f"bet:win:{sec}")
    b.button(text="✏️ Personalizzata", callback_data="bet:win:custom")
    b.button(text="♾️ Illimitata", callback_data="bet:win:0")
    b.button(text="❌ Annulla", callback_data="bet:cancel_creation")
    b.adjust(2, 2, 1, 1, 1)
    return b.as_markup()


def _window_label(window_seconds: int | None) -> str:
    """Human label for the chosen betting window (used in confirmations)."""
    if not window_seconds:
        return "♾️ illimitata (chiusura manuale)"
    minutes = window_seconds // 60
    return f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def _deadline_block(event: BettingEvent) -> str:
    """A ready-to-embed «⏳ Chiude alle …» line for the user's bet view (empty when
    the window is illimitata), so a player knows the cutoff before betting."""
    if event.closes_at is None:
        return ""
    when = schedule_service.to_local(event.closes_at).strftime("%d/%m %H:%M")
    return f"⏳ Chiude alle <b>{when}</b>\n\n"


class BetCustomAmountState(StatesGroup):
    waiting_for_amount = State()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def parse_bet_amount(raw: str) -> tuple[int | None, str | None]:
    """Validate a user-typed bet amount. Returns ``(amount, None)`` on success or
    ``(None, error_message)`` for non-numeric, non-positive, or over-cap input.

    Pure (no I/O) so it's unit-testable and shared by every custom-amount entry."""
    try:
        amount = int((raw or "").strip())
    except ValueError:
        return None, "⚠️ Inserisci solo un numero intero, es: <code>150</code>"
    if amount <= 0:
        return None, "⚠️ L'importo deve essere positivo."
    if amount > MAX_BET:
        return None, f"⚠️ Importo massimo per una scommessa: <b>{MAX_BET:,} 🪙</b>."
    return amount, None


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
    # The list marks the events this user already bet on («✅ Hai già scommesso»),
    # which is personal data: answering in the group would show everyone what the
    # caller has (or hasn't) played. Redirect to private like /classifiche (§9).
    if await redirect_to_private(
        message,
        "scommesse",
        "🎲 Vedi le scommesse",
        notice="🔒 Le scommesse mostrano le tue puntate: continua in chat privata.",
    ):
        return
    await show_events_private(message, db_session, state)


async def show_events_private(
    message: Message, db_session: AsyncSession, state: FSMContext
) -> None:
    """The private open-events list — shared by /scommesse and the `scommesse` deep-link."""
    events = await bet_service.get_open_events(db_session)

    if not events:
        await message.answer(
            "🎲 <b>Nessuna scommessa aperta al momento.</b>\n\n"
            "<i>Torna più tardi o chiedi a un admin di crearne una!</i>"
        )
        return

    event_ids = [e.id for e in events]
    placed_ids = await bet_service.get_user_placed_event_ids(db_session, message.from_user.id, event_ids)

    await _clear_active_bet_msg(message.bot, message.chat.id, state)
    sent = await message.answer(
        f"🎲 <b>{len(events)} scommess{'a aperta' if len(events) == 1 else 'e aperte'}</b>\n\n"
        "Seleziona un evento:",
        reply_markup=get_events_keyboard(events, placed_ids),
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
        "<b>Step 1/4</b> — Invia il titolo dell'evento (max 200 caratteri):",
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
        "<b>Step 2/4</b> — Invia una breve descrizione (max 500 caratteri):",
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
        "<b>Step 3/4</b> — Invia le opzioni, <b>una per riga</b> (min 2, max 8).\n\n"
        "Esempio:\n<code>Squadra A\nSquadra B\nPareggio</code>",
        reply_markup=_cancel_creation_kb(),
    )


@router.message(BetCreationStates.waiting_for_options, ~F.text.startswith("/"))
async def fsm_bet_options(message: Message, state: FSMContext) -> None:
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

    # Options are stashed; the event is created only after the window step, so the
    # betting deadline is baked in from the start (draft carries it to activation).
    await state.update_data(options=options)
    await state.set_state(BetCreationStates.waiting_for_window)
    opts_text = "\n".join(f"• {esc(o)}" for o in options)
    await message.answer(
        f"✅ Opzioni:\n{opts_text}\n\n"
        f"<b>Step 4/4</b> — Per quanto tempo si potrà scommettere <b>dopo l'avvio</b>?\n"
        f"Scegli un preset, «Personalizzata» (<code>30m</code>/<code>2h</code>/<code>1d</code>) "
        f"o «Illimitata» (chiudi tu a mano, come prima).",
        reply_markup=_window_kb(),
    )


@router.callback_query(BetCreationStates.waiting_for_window, F.data.startswith("bet:win:"))
async def cb_bet_window(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    raw = callback.data.split(":")[2]
    if raw == "custom":
        await state.set_state(BetCreationStates.waiting_for_window_custom)
        await callback.message.edit_text(
            "✏️ Invia la durata della finestra: <code>30m</code>, <code>2h</code> oppure <code>1d</code>.",
            reply_markup=_cancel_creation_kb(),
        )
        await callback.answer()
        return
    sec = int(raw)  # 0 = illimitata
    await _finalize_bet_creation(callback.message, state, db_session, sec or None, callback.from_user.id)
    await callback.answer()


@router.message(BetCreationStates.waiting_for_window_custom, ~F.text.startswith("/"))
async def fsm_bet_window_custom(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    try:
        sec = schedule_service.parse_duration(message.text or "")
    except ValueError as e:
        await message.answer(f"⚠️ {e}", reply_markup=_cancel_creation_kb())
        return
    await _finalize_bet_creation(message, state, db_session, sec, message.from_user.id)


async def _finalize_bet_creation(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    window_seconds: int | None,
    creator_id: int,
) -> None:
    """Create the event (draft or open) with the chosen betting window, then confirm.
    ``message`` is the surface to reply on (a callback's message or the user's text);
    ``creator_id`` is passed explicitly since a callback's message is authored by the bot."""
    data = await state.get_data()
    title = data["title"]
    description = data["description"]
    options = data["options"]
    as_draft = bool(data.get("bet_as_draft", False))

    event = await bet_service.create_event(
        db_session,
        creator_tg_id=creator_id,
        title=title,
        description=description,
        options=[{"label": o} for o in options],
        status=EventStatus.draft.value if as_draft else EventStatus.open.value,
        window_seconds=window_seconds,
    )
    # Direct-open (community) path: the event is already open → arm+schedule its
    # auto-lock now. A draft arms/schedules later, when it's activated from the hub.
    if not as_draft:
        await bet_service.schedule_close(
            db_session, event, creator_id, group_registry.get_group_id() or None
        )
    await db_session.commit()
    await state.clear()

    opts_text = "\n".join(f"• {esc(o)}" for o in options)
    window_line = _window_label(window_seconds)
    if as_draft:
        await message.answer(
            f"✅ <b>Scommessa creata in bozza!</b>\n\n"
            f"<b>#{event.id} {esc(title)}</b>\n"
            f"{esc(description)}\n\n"
            f"<b>Opzioni:</b>\n{opts_text}\n\n"
            f"⏳ Finestra puntate: <b>{window_line}</b>\n\n"
            f"Avviala subito o programmala dagli 🎬 Eventi (/admin → Eventi → Scommesse)."
        )
    else:
        await message.answer(
            f"✅ <b>Scommessa creata!</b>\n\n"
            f"<b>#{event.id} {esc(title)}</b>\n"
            f"{esc(description)}\n\n"
            f"<b>Opzioni:</b>\n{opts_text}\n\n"
            f"⏳ Finestra puntate: <b>{window_line}</b>\n\n"
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
        "<b>Step 1/4</b> — Invia il titolo dell'evento (max 200 caratteri):",
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
        f"{_deadline_block(event)}"
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
        "✏️ Inserisci l'importo che vuoi scommettere (in CoInn):"
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
        f"{_deadline_block(event)}"
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
        "✏️ Inserisci l'importo che vuoi scommettere (in CoInn):\n\n"
        "<i>Invia solo il numero, es: <code>150</code></i>"
    )
    await callback.answer()


@router.message(BetCustomAmountState.waiting_for_amount, ~F.text.startswith("/"))
async def fsm_custom_amount(
    message: Message, state: FSMContext, db_session: AsyncSession
) -> None:
    amount, err = parse_bet_amount(message.text or "")
    if err:
        await message.answer(err)
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
        # Announced in the group (tagging the user), not appended in private.
        await announce_trophies(callback.bot, db_session, callback.from_user.id, newly_earned)

    xp_line = (
        f"⚡ <b>+{settings.xp_per_bet_placed} XP</b> per la partecipazione "
        f"(altri se vinci!).\n"
        if settings.xp_per_bet_placed > 0
        else ""
    )
    await callback.message.edit_text(
        f"✅ <b>Scommessa piazzata!</b>\n\n"
        f"💸 Hai puntato <b>{amount} 🪙</b>.\n"
        f"{xp_line}"
        f"<i>Ti notificheremo quando l'evento viene risolto.</i>"
    )
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
