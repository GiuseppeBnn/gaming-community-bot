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

from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers import event_types
from handlers._privacy import redirect_to_private
from handlers.callbacks import AdminCb, EventCb, PollCreateCb
from handlers.event_types import edit_or_send
from keyboards.common_kb import confirm_cancel_kb
from services import group_registry, poll_service
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
    "askclose": ("close", "chiudere ora (pubblica il podio)", "🏁 Sì, chiudi"),
    "askdel": ("del", "eliminare <b>definitivamente</b>", "🗑️ Sì, elimina"),
    # «e premi» diceva il falso: i premi già pagati restano pagati, e alla chiusura
    # successiva il montepremi viene erogato di nuovo per intero. È voluto — una
    # riproposizione è un evento nuovo — quindi è il testo che va detto com'è.
    "askreset": ("reset", "riproporre (azzera le risposte e ripaga il montepremi intero)",
                 "🔁 Sì, riproponi"),
}


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
    if res.ok:
        await db_session.commit()  # spec mutated but never commits (STEERING §5)
    await callback.answer(res.message, show_alert=res.alert)
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
        await callback.answer()
        return
    if res.ok:
        await db_session.commit()
    await callback.answer(res.message, show_alert=res.alert)
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
    delete = getattr(et, "delete", None) if et is not None else None
    if delete is None:
        await callback.answer()
        return
    res = await delete(db_session, item_id)
    if res.ok:
        await db_session.commit()  # spec mutated but never commits (STEERING §5)
    await callback.answer(res.message, show_alert=res.alert)
    await et.render_list(callback.message, db_session)  # item is gone → back to list


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
    reset = getattr(et, "reset", None) if et is not None else None
    if reset is None:
        await callback.answer()
        return
    res = await reset(db_session, item_id)
    if res is None:  # type isn't re-runnable
        await callback.answer()
        return
    if res.ok:
        await db_session.commit()
    await callback.answer(res.message, show_alert=res.alert)
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
    await state.update_data(pt_question=q)
    await state.set_state(PollTemplateStates.options)
    await message.answer(
        f"Invia le <b>opzioni</b>, una per riga (da {_MIN_OPTIONS} a {_MAX_OPTIONS}):",
        reply_markup=_pt_cancel_kb(),
    )


@router.message(PollTemplateStates.options, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_pt_options(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    options = _parse_options(message.text)
    if options is None:
        await message.answer(
            f"⚠️ Servono da {_MIN_OPTIONS} a {_MAX_OPTIONS} opzioni (≤100 caratteri, una per riga).",
            reply_markup=_pt_cancel_kb(),
        )
        return
    data = await state.get_data()
    poll = await poll_service.create_template(
        db_session, message.from_user.id, data["pt_question"], options,
        group_registry.get_group_id() or None,
    )
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ <b>Sondaggio #{poll.id} creato!</b>\n\n"
        f"❓ {esc(poll.question)}\n\n"
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
