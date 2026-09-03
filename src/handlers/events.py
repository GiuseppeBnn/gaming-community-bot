"""
Events hub — one place to manage the registered event types (quiz · sondaggi ·
scommesse · …) under a shared model: each is **pre-created**, then either
**started now** in the group or **scheduled**, exactly like quizzes already worked.

Every callback dispatches through the event-type registry
(``handlers.event_types``) — there is no per-type ``if/elif`` here, so a new type
appears in the hub the moment it is registered, with no edits to this file.

Callback payloads are typed (``handlers.callbacks.EventCb``, prefix ``ev``; well
within Telegram's 64-byte limit):
  action="home"                      → the events hub (one button per registered type)
  action="list", task_type           → list pre-created items of a type
  action="item", task_type, item_id  → manage one item (avvia ora / programma)
  action="start", task_type, item_id → start it now in the group
  action="sched"/"sched_close", task_type, item_id
                                      → schedule it (hands off to handlers.schedule);
                                        "sched_close" pins the action instead of
                                        asking «avvio o chiusura?»
  action="close", task_type, item_id → close a running item (e.g. publish a quiz podium)
  action="new", task_type            → create a new item of a type
``handlers.callbacks.PollCreateCb`` (prefix ``evpt``) is a family of its own for the
poll-template cancel triangle: action="cancel"[_yes|_no] (with confirm).

Admin-only throughout (IsAdminFilter / IsAdminCallbackFilter). Reuses the existing
quiz, betting and scheduling services/handlers — no duplicated business logic.
"""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters.command import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers import event_types
from handlers._privacy import redirect_to_private
from handlers.callbacks import AdminCb, EventCb, PollCreateCb
from handlers.event_types import StartResult, edit_or_send
from keyboards.common_kb import confirm_cancel_kb
from services import group_registry, poll_service, schedule_service
from utils.text import esc

log = logging.getLogger(__name__)
router = Router()
# Admin-only router: gate every message/callback handler so no FSM-state-only
# handler can be driven by a user who has lost admin (STEERING §8).
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminCallbackFilter())

_MIN_OPTIONS, _MAX_OPTIONS = 2, 10


class PollTemplateStates(StatesGroup):
    question = State()
    options = State()
    description = State()   # optional free text (or ⏭️ Salta)
    prize_choice = State()  # menu: default / custom / none
    prize_coins = State()   # custom: CoInn per voter
    prize_xp = State()      # custom: XP per voter
    close_choice = State()  # menu: no auto-close / schedule one
    close_at = State()      # absolute AAAA-MM-GG HH:MM


#: A bare relative token (30m/2h/1d): refused for the poll's auto-close date. The
#: poll is not running yet, so "from now" vs "from start" is ambiguous — only an
#: absolute instant is unambiguous here (same rule as the guess auto-close).
_REL_TOKEN_RE = re.compile(r"^\d+\s*[mhd]$", re.IGNORECASE)
#: Sanity bound on a per-voter prize, so a fat-fingered amount can't mint millions.
_MAX_PRIZE = 1_000_000
#: Telegram caps a native poll question at 300 chars, and a native poll has no
#: separate description field — the description is concatenated into the question
#: (``poll_service.render_question``). So question + description must fit within
#: this, validated at the description step and re-asked until it does.
_POLL_QUESTION_MAX = 300
_DESC_SEP = "\n\n"


def _poll_length_overflow(question: str, description: str) -> int:
    """Chars by which ``question`` + ``description`` would exceed Telegram's
    300-char poll question. 0 means it fits; an empty description always fits."""
    if not description:
        return 0
    total = len(question) + len(_DESC_SEP) + len(description)
    return max(0, total - _POLL_QUESTION_MAX)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _hub_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    types = event_types.all_types()
    for et in types:
        b.button(text=et.hub_label, callback_data=EventCb(action="list", task_type=et.key).pack())
    b.button(text="⬅️ Dashboard", callback_data=AdminCb(action="home").pack())
    # Event labels are descriptive (and one is deliberately long): one button
    # per row preserves their full text instead of squeezing six tiny columns.
    b.adjust(1)
    return b.as_markup()


def _item_kb(task_type: str, item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Avvia ora",
             callback_data=EventCb(action="start", task_type=task_type, item_id=item_id).pack())
    b.button(text="🗓️ Programma",
             callback_data=EventCb(action="sched", task_type=task_type, item_id=item_id).pack())
    b.button(text="⬅️ Indietro",
             callback_data=EventCb(action="list", task_type=task_type).pack())
    b.adjust(2, 1)
    return b.as_markup()


async def show_hub(message: Message) -> None:
    await edit_or_send(
        message,
        "🎬 <b>Eventi</b>\n\n"
        "Crea quiz, sondaggi e scommesse, poi <b>avviali subito</b> nel gruppo "
        "oppure <b>programmali</b>.\n\nScegli un tipo:",
        _hub_kb(),
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

@router.message(Command("eventi"), IsAdminFilter())
async def cmd_eventi(message: Message, state: FSMContext) -> None:
    if await redirect_to_private(
        message, "eventi", "🎬 Apri gli Eventi", notice="🎬 Gli Eventi si gestiscono in chat privata."
    ):
        return
    await state.clear()
    await show_hub(message)


@router.callback_query(EventCb.filter(F.action == "home"), IsAdminCallbackFilter())
async def cb_hub(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_hub(callback.message)
    await callback.answer()


@router.callback_query(EventCb.filter(F.action == "list"), IsAdminCallbackFilter())
async def cb_list(callback: CallbackQuery, callback_data: EventCb, db_session: AsyncSession) -> None:
    task_type = callback_data.task_type
    if task_type is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    await et.render_list(callback.message, db_session)
    await callback.answer()


@router.callback_query(EventCb.filter(F.action == "item"), IsAdminCallbackFilter())
async def cb_item(callback: CallbackQuery, callback_data: EventCb, db_session: AsyncSession) -> None:
    task_type = callback_data.task_type
    item_id = callback_data.item_id
    if task_type is None or item_id is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    # Types that provide a detail/info screen (e.g. quiz) show it — tapping an item
    # never launches it. Types without one keep the generic "avvia/programma" screen.
    render_detail = getattr(et, "render_detail", None)
    if render_detail is not None:
        await render_detail(callback.message, db_session, item_id)
    else:
        await edit_or_send(
            callback.message,
            f"{et.hub_label} #{item_id}\n\nVuoi avviarlo subito nel gruppo o programmarlo?",
            _item_kb(task_type, item_id),
        )
    await callback.answer()


# ---------------------------------------------------------------------------
# Confirmation gate — every impactful action asks "sicuro?" first (no accidental
# start/close/delete). Yes routes to the executor callback; No back to the detail.
# ---------------------------------------------------------------------------

# action → (the action to run, prompt verb, yes-button label)
_CONFIRM: dict[str, tuple[str, str, str]] = {
    "askstart": ("start", "avviare subito nel gruppo", "▶️ Sì, avvia"),
    "askclose": ("close", "chiudere ora", "🏁 Sì, chiudi"),
    "askdel": ("del", "eliminare <b>definitivamente</b>", "🗑️ Sì, elimina"),
    "askarchive": ("archive", "archiviare/nascondere", "🗃️ Sì, archivia"),
    # «e premi» diceva il falso: i premi già pagati restano pagati, e alla chiusura
    # successiva il montepremi viene erogato di nuovo per intero. È voluto — una
    # riproposizione è un evento nuovo — quindi è il testo che va detto com'è.
    "askreset": ("reset", "riproporre (azzera le risposte e ripaga il montepremi intero)",
                 "🔁 Sì, riproponi"),
}


async def _commit_then_present(
    callback: CallbackQuery,
    db_session: AsyncSession,
    res: StartResult,
) -> bool:
    """Own a generic mutation transaction, then run its optional presentation hook.

    ``True`` means callers may redraw their normal screen. A failed commit has no
    durable state to present, while a failed hook happens *after* that state is
    durable and gets a recoverable message instead of a false rollback claim.
    """
    if not res.ok:
        await db_session.rollback()
        await callback.answer(res.message, show_alert=res.alert)
        return True
    try:
        await db_session.commit()
    except Exception as exc:  # noqa: BLE001 — caller owns this transaction boundary
        await db_session.rollback()
        log.warning("Event callback commit failed error=%s", type(exc).__name__)
        await callback.answer("⚠️ Stato non salvato. Riprova.", show_alert=True)
        return False
    if res.post_commit is not None:
        try:
            await res.post_commit()
        except Exception as exc:  # noqa: BLE001 — durable state is recoverable by republish
            await db_session.rollback()
            log.warning("Event post-commit hook failed error=%s", type(exc).__name__)
            await callback.answer(
                "⚠️ Stato salvato, ma la card va ripubblicata.", show_alert=True,
            )
            return True
    await callback.answer(res.message, show_alert=res.alert)
    return True


@router.callback_query(EventCb.filter(F.action.in_(_CONFIRM)), IsAdminCallbackFilter())
async def cb_confirm(callback: CallbackQuery, callback_data: EventCb) -> None:
    exec_action, verb, yes_text = _CONFIRM[callback_data.action]
    task_type = callback_data.task_type
    item_id = callback_data.item_id
    if task_type is None or item_id is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    await edit_or_send(
        callback.message,
        f"⚠️ Vuoi {verb} <b>{et.hub_label} #{item_id}</b>?",
        confirm_cancel_kb(
            EventCb(action=exec_action, task_type=task_type, item_id=item_id).pack(),
            EventCb(action="item", task_type=task_type, item_id=item_id).pack(),
            yes_text=yes_text,
            no_text="⬅️ No, indietro",
        ),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Start now / close — generic dispatch through the event-type registry
# ---------------------------------------------------------------------------

@router.callback_query(EventCb.filter(F.action == "start"), IsAdminCallbackFilter())
async def cb_start_now(
    callback: CallbackQuery, callback_data: EventCb, db_session: AsyncSession
) -> None:
    task_type = callback_data.task_type
    item_id = callback_data.item_id
    if task_type is None or item_id is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    res = await et.start_now(callback.bot, db_session, item_id)
    if await _commit_then_present(callback, db_session, res):
        await et.render_list(callback.message, db_session)


@router.callback_query(EventCb.filter(F.action == "close"), IsAdminCallbackFilter())
async def cb_close(
    callback: CallbackQuery, callback_data: EventCb, db_session: AsyncSession
) -> None:
    task_type = callback_data.task_type
    item_id = callback_data.item_id
    if task_type is None or item_id is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    res = await et.close_now(callback.bot, db_session, item_id)
    if res is None:  # type has no close action
        await db_session.rollback()
        await callback.answer()
        return
    if await _commit_then_present(callback, db_session, res):
        await et.render_list(callback.message, db_session)


@router.callback_query(EventCb.filter(F.action == "del"), IsAdminCallbackFilter())
async def cb_delete(
    callback: CallbackQuery, callback_data: EventCb, db_session: AsyncSession
) -> None:
    task_type = callback_data.task_type
    item_id = callback_data.item_id
    if task_type is None or item_id is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    delete = getattr(et, "delete", None)
    if delete is None:
        await callback.answer()
        return
    res = await delete(db_session, item_id)
    if await _commit_then_present(callback, db_session, res):
        await et.render_list(callback.message, db_session)  # item is gone → back to list


@router.callback_query(EventCb.filter(F.action == "archive"), IsAdminCallbackFilter())
async def cb_archive(
    callback: CallbackQuery, callback_data: EventCb, db_session: AsyncSession
) -> None:
    """Run an optional non-destructive archive capability without type branching."""
    task_type = callback_data.task_type
    item_id = callback_data.item_id
    if task_type is None or item_id is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    archive = getattr(et, "archive", None)
    if archive is None:
        await callback.answer()
        return
    res = await archive(db_session, item_id)
    if await _commit_then_present(callback, db_session, res):
        await et.render_list(callback.message, db_session)


@router.callback_query(EventCb.filter(F.action == "reset"), IsAdminCallbackFilter())
async def cb_reset(
    callback: CallbackQuery, callback_data: EventCb, db_session: AsyncSession
) -> None:
    task_type = callback_data.task_type
    item_id = callback_data.item_id
    if task_type is None or item_id is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    reset = getattr(et, "reset", None)
    if reset is None:
        await callback.answer()
        return
    res = await reset(db_session, item_id)
    if res is None:  # type isn't re-runnable
        await db_session.rollback()
        await callback.answer()
        return
    if not await _commit_then_present(callback, db_session, res):
        return
    # Still exists (now `ready` again) → refresh its detail if the type has one.
    render_detail = getattr(et, "render_detail", None)
    if render_detail is not None:
        await render_detail(callback.message, db_session, item_id)
    else:
        await et.render_list(callback.message, db_session)


# ---------------------------------------------------------------------------
# Schedule (hands off to handlers.schedule)
# ---------------------------------------------------------------------------

@router.callback_query(
    EventCb.filter(F.action.in_({"sched", "sched_close"})), IsAdminCallbackFilter()
)
async def cb_schedule(
    callback: CallbackQuery, callback_data: EventCb, state: FSMContext
) -> None:
    # "sched_close" pins what to schedule ("close"), used by the buttons on an item
    # that is already running — there, «avvio» is not one of the answers.
    action = "close" if callback_data.action == "sched_close" else None
    task_type = callback_data.task_type
    item_id = callback_data.item_id
    if task_type is None or item_id is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    from handlers.schedule import start_schedule_for
    await start_schedule_for(
        callback.message, state, task_type, item_id,
        f"{et.hub_label} #{item_id}", action,
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Create — generic dispatch; each type enters its own creation FSM
# ---------------------------------------------------------------------------

@router.callback_query(EventCb.filter(F.action == "new"), IsAdminCallbackFilter())
async def cb_new(callback: CallbackQuery, callback_data: EventCb, state: FSMContext) -> None:
    task_type = callback_data.task_type
    if task_type is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    await et.start_creation(callback.message, state, creator_id=callback.from_user.id)
    await callback.answer()


def _pt_cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Annulla", callback_data=PollCreateCb(action="cancel").pack())
    return b.as_markup()


def _parse_options(text: str | None) -> list[str] | None:
    options = [o.strip() for o in (text or "").splitlines() if o.strip()]
    if not (_MIN_OPTIONS <= len(options) <= _MAX_OPTIONS):
        return None
    if any(len(o) > 100 for o in options):
        return None
    return options


async def start_poll_creation(message: Message, state: FSMContext) -> None:
    """Enter the poll-template creation FSM (question → options). Shared by the
    Events hub «➕ Crea sondaggio» and the /sondaggio command — single canonical
    flow, no duplicated FSM."""
    await state.clear()
    await state.set_state(PollTemplateStates.question)
    await message.answer(
        "📊 <b>Nuovo sondaggio</b>\n\nInvia la <b>domanda</b>:", reply_markup=_pt_cancel_kb()
    )


@router.message(PollTemplateStates.question, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_pt_question(message: Message, state: FSMContext) -> None:
    q = (message.text or "").strip()[:300]
    if len(q) < 3:
        await message.answer("⚠️ Domanda troppo corta (min 3).", reply_markup=_pt_cancel_kb())
        return
    # The admin's id is captured here, from a real user message, so the final
    # create can run from a *callback* (e.g. «no prize», «no close») where
    # `from_user` would be the bot.
    await state.update_data(pt_question=q, pt_creator=message.from_user.id)
    await _ask_description(message, state)


# --- Optional description (right after the question) ----------------------

def _desc_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⏭️ Salta", callback_data=PollCreateCb(action="desc_skip").pack())
    b.button(text="❌ Annulla", callback_data=PollCreateCb(action="cancel").pack())
    b.adjust(2)
    return b.as_markup()


async def _ask_description(message: Message, state: FSMContext) -> None:
    await state.set_state(PollTemplateStates.description)
    await message.answer(
        "📝 Vuoi aggiungere una <b>descrizione</b>? Verrà mostrata <b>sotto la domanda</b>, "
        "nello stesso messaggio del sondaggio. Inviala ora, oppure salta.",
        reply_markup=_desc_kb(),
    )


@router.message(PollTemplateStates.description, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_pt_description(message: Message, state: FSMContext) -> None:
    desc = (message.text or "").strip()
    data = await state.get_data()
    over = _poll_length_overflow(data.get("pt_question", ""), desc)
    if over > 0:
        # Description goes inside the poll question (max 300), so title + description
        # must fit: reject and re-ask, like the trivia question length check.
        await message.answer(
            f"⚠️ Domanda e descrizione insieme superano di <b>{over}</b> caratteri il "
            f"limite di <b>{_POLL_QUESTION_MAX}</b> di un sondaggio Telegram.\n"
            "Invia una <b>descrizione più corta</b> (oppure salta).",
            reply_markup=_desc_kb(),
        )
        return
    await state.update_data(pt_description=desc or None)
    await _ask_options(message, state)


@router.callback_query(PollCreateCb.filter(F.action == "desc_skip"), IsAdminCallbackFilter())
async def cb_pt_desc_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(pt_description=None)
    await _ask_options(callback.message, state)
    await callback.answer()


# --- Options --------------------------------------------------------------

async def _ask_options(message: Message, state: FSMContext) -> None:
    await state.set_state(PollTemplateStates.options)
    await message.answer(
        f"Invia le <b>opzioni</b>, una per riga (da {_MIN_OPTIONS} a {_MAX_OPTIONS}):",
        reply_markup=_pt_cancel_kb(),
    )


@router.message(PollTemplateStates.options, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_pt_options(message: Message, state: FSMContext) -> None:
    options = _parse_options(message.text)
    if options is None:
        await message.answer(
            f"⚠️ Servono da {_MIN_OPTIONS} a {_MAX_OPTIONS} opzioni (≤100 caratteri, una per riga).",
            reply_markup=_pt_cancel_kb(),
        )
        return
    await state.update_data(pt_options=options)
    await _ask_prize(message, state)


# --- Optional prize -------------------------------------------------------

def _prize_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text=f"⚡ Premio consigliato ({settings.poll_reward_coins}🪙 + {settings.poll_reward_xp}⚡)",
        callback_data=PollCreateCb(action="prize_default").pack(),
    )
    b.button(text="✏️ Personalizza", callback_data=PollCreateCb(action="prize_custom").pack())
    b.button(text="🚫 Nessun premio", callback_data=PollCreateCb(action="prize_none").pack())
    b.button(text="❌ Annulla", callback_data=PollCreateCb(action="cancel").pack())
    b.adjust(1)
    return b.as_markup()


async def _ask_prize(message: Message, state: FSMContext) -> None:
    await state.set_state(PollTemplateStates.prize_choice)
    await message.answer(
        "🏆 <b>Premio ai votanti?</b>\n"
        "Un sondaggio non ha risposte giuste: il premio va a <b>ogni utente che vota</b>, "
        "pagato alla chiusura.",
        reply_markup=_prize_kb(),
    )


@router.callback_query(PollCreateCb.filter(F.action == "prize_default"), IsAdminCallbackFilter())
async def cb_pt_prize_default(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(
        pt_prize_coins=settings.poll_reward_coins, pt_prize_xp=settings.poll_reward_xp
    )
    await _ask_close(callback.message, state)
    await callback.answer()


@router.callback_query(PollCreateCb.filter(F.action == "prize_none"), IsAdminCallbackFilter())
async def cb_pt_prize_none(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(pt_prize_coins=0, pt_prize_xp=0)
    await _ask_close(callback.message, state)
    await callback.answer()


@router.callback_query(PollCreateCb.filter(F.action == "prize_custom"), IsAdminCallbackFilter())
async def cb_pt_prize_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PollTemplateStates.prize_coins)
    await callback.message.answer(
        "🪙 Quanti <b>CoInn</b> a ogni votante? (0 per nessuno)", reply_markup=_pt_cancel_kb()
    )
    await callback.answer()


def _parse_prize(text: str | None) -> int | None:
    raw = (text or "").strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value <= _MAX_PRIZE else None


@router.message(PollTemplateStates.prize_coins, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_pt_prize_coins(message: Message, state: FSMContext) -> None:
    value = _parse_prize(message.text)
    if value is None:
        await message.answer(
            f"⚠️ Inserisci un numero intero fra 0 e {_MAX_PRIZE}.", reply_markup=_pt_cancel_kb()
        )
        return
    await state.update_data(pt_prize_coins=value)
    await state.set_state(PollTemplateStates.prize_xp)
    await message.answer(
        "⚡ Quanti <b>XP</b> a ogni votante? (0 per nessuno)", reply_markup=_pt_cancel_kb()
    )


@router.message(PollTemplateStates.prize_xp, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_pt_prize_xp(message: Message, state: FSMContext) -> None:
    value = _parse_prize(message.text)
    if value is None:
        await message.answer(
            f"⚠️ Inserisci un numero intero fra 0 e {_MAX_PRIZE}.", reply_markup=_pt_cancel_kb()
        )
        return
    await state.update_data(pt_prize_xp=value)
    await _ask_close(message, state)


# --- Optional scheduled close --------------------------------------------

def _close_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚫 Nessuna chiusura automatica", callback_data=PollCreateCb(action="close_none").pack())
    b.button(text="🗓️ Programma chiusura", callback_data=PollCreateCb(action="close_set").pack())
    b.button(text="❌ Annulla", callback_data=PollCreateCb(action="cancel").pack())
    b.adjust(1)
    return b.as_markup()


async def _ask_close(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    has_prize = (data.get("pt_prize_coins", 0) or 0) > 0 or (data.get("pt_prize_xp", 0) or 0) > 0
    if has_prize:
        # A prize is paid AT the close, so a prized poll MUST have a close date —
        # there is no «nessuna chiusura» option here (the reward would never land).
        await state.set_state(PollTemplateStates.close_at)
        await message.answer(
            "🏆 <b>I premi si pagano alla chiusura</b>, quindi serve una <b>data</b>.\n"
            "All'orario scelto il bot chiude il sondaggio, annuncia l'opzione vincente e "
            "paga i votanti.\n\nInvia la data:\n<code>2026-05-30 18:00</code>",
            reply_markup=_pt_cancel_kb(),
        )
        return
    # No prize: a close date is OPTIONAL. Without it the poll is a plain, normal
    # Telegram poll (fire-and-forget). With it, the bot closes it and announces the
    # winning option (no payment).
    await state.set_state(PollTemplateStates.close_choice)
    await message.answer(
        "⏳ <b>Chiusura automatica?</b>\n"
        "Con una data il bot chiude il sondaggio e annuncia l'opzione vincente. "
        "Senza, resta un sondaggio normale.",
        reply_markup=_close_kb(),
    )


@router.callback_query(PollCreateCb.filter(F.action == "close_none"), IsAdminCallbackFilter())
async def cb_pt_close_none(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await state.update_data(pt_closes_at=None)
    await _finish_poll(callback.message, state, db_session)
    await callback.answer()


@router.callback_query(PollCreateCb.filter(F.action == "close_set"), IsAdminCallbackFilter())
async def cb_pt_close_set(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PollTemplateStates.close_at)
    await callback.message.answer(
        "🕒 Quando chiuderlo? Invia una <b>data assoluta</b>:\n"
        "<code>2026-05-30 18:00</code>",
        reply_markup=_pt_cancel_kb(),
    )
    await callback.answer()


@router.message(PollTemplateStates.close_at, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_pt_close_at(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    raw = (message.text or "").strip()
    if _REL_TOKEN_RE.match(raw):
        await message.answer(
            "⚠️ Per la chiusura usa una <b>data</b> assoluta (<code>AAAA-MM-GG HH:MM</code>), "
            "non una durata relativa.",
            reply_markup=_pt_cancel_kb(),
        )
        return
    try:
        closes_at = schedule_service.parse_run_at(raw)
    except ValueError as e:
        await message.answer(f"⚠️ {e}\n\nEsempio: <code>2026-05-30 18:00</code>", reply_markup=_pt_cancel_kb())
        return
    await state.update_data(pt_closes_at=closes_at.isoformat())
    await _finish_poll(message, state, db_session)


# --- Create ---------------------------------------------------------------

async def _finish_poll(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    from datetime import datetime

    data = await state.get_data()
    closes_raw = data.get("pt_closes_at")
    closes_at = datetime.fromisoformat(closes_raw) if closes_raw else None
    poll = await poll_service.create_template(
        db_session, data["pt_creator"], data["pt_question"], data["pt_options"],
        group_registry.get_group_id() or None,
        description=data.get("pt_description"),
        prize_coins=data.get("pt_prize_coins", 0),
        prize_xp=data.get("pt_prize_xp", 0),
        closes_at=closes_at,
    )
    await db_session.commit()
    await state.clear()
    prize_line = f"🏆 {poll_service.format_prize_summary(poll)}"
    close_line = (
        f"\n⏳ Chiusura: {schedule_service.to_local(poll.closes_at):%d/%m %H:%M}"
        if poll.closes_at is not None else ""
    )
    await message.answer(
        f"✅ <b>Sondaggio creato!</b>\n\n"
        f"❓ {esc(poll.question)}\n{prize_line}{close_line}\n\n"
        "Avvialo subito nel gruppo oppure programmalo:",
        reply_markup=_item_kb("poll", poll.id),
    )


@router.callback_query(PollCreateCb.filter(F.action == "cancel"), IsAdminCallbackFilter())
async def cb_pt_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() is None:
        await callback.answer()
        return
    await callback.message.answer(
        "⚠️ Sicuro di voler annullare il sondaggio? I dati inseriti andranno persi.",
        reply_markup=confirm_cancel_kb(
            PollCreateCb(action="cancel_yes").pack(), PollCreateCb(action="cancel_no").pack()
        ),
    )
    await callback.answer()


@router.callback_query(PollCreateCb.filter(F.action == "cancel_yes"), IsAdminCallbackFilter())
async def cb_pt_cancel_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Creazione sondaggio annullata.")
    await callback.answer()


@router.callback_query(PollCreateCb.filter(F.action == "cancel_no"), IsAdminCallbackFilter())
async def cb_pt_cancel_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("▶️ Ok, continua pure da dove eri rimasto.")
    await callback.answer()
