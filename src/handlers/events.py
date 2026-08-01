"""
Events hub — one place to manage the registered event types (quiz · sondaggi ·
scommesse · …) under a shared model: each is **pre-created**, then either
**started now** in the group or **scheduled**, exactly like quizzes already worked.

Every callback dispatches through the event-type registry
(``handlers.event_types``) — there is no per-type ``if/elif`` here, so a new type
appears in the hub the moment it is registered, with no edits to this file.

Callback grammar (namespace ``ev:*``, well within Telegram's 64-byte limit):
  ev:home                      → the events hub (one button per registered type)
  ev:list:<type>               → list pre-created items of a type
  ev:item:<type>:<id>          → manage one item (avvia ora / programma)
  ev:start:<type>:<id>         → start it now in the group
  ev:sched:<type>:<id>[:close] → schedule it (hands off to handlers.schedule); the
                                 optional last segment pins the action instead of
                                 asking «avvio o chiusura?»
  ev:close:<type>:<id>         → close a running item (e.g. publish a quiz podium)
  ev:new:<type>                → create a new item of a type
  ev:pt:cancel[_yes|_no]       → cancel the poll-template creation (with confirm)

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
        b.button(text=et.hub_label, callback_data=f"ev:list:{et.key}")
    b.button(text="⬅️ Dashboard", callback_data="adm:home")
    b.adjust(len(types) or 1, 1)
    return b.as_markup()


def _item_kb(task_type: str, item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Avvia ora", callback_data=f"ev:start:{task_type}:{item_id}")
    b.button(text="🗓️ Programma", callback_data=f"ev:sched:{task_type}:{item_id}")
    b.button(text="⬅️ Indietro", callback_data=f"ev:list:{task_type}")
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


@router.callback_query(F.data == "ev:home", IsAdminCallbackFilter())
async def cb_hub(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_hub(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("ev:list:"), IsAdminCallbackFilter())
async def cb_list(callback: CallbackQuery, db_session: AsyncSession) -> None:
    et = event_types.get(callback.data.split(":")[2])
    if et is None:
        await callback.answer()
        return
    await et.render_list(callback.message, db_session)
    await callback.answer()


@router.callback_query(F.data.startswith("ev:item:"), IsAdminCallbackFilter())
async def cb_item(callback: CallbackQuery, db_session: AsyncSession) -> None:
    _, _, task_type, raw = callback.data.split(":")
    et = event_types.get(task_type)
    if et is None or not raw.isdigit():
        await callback.answer()
        return
    # Types that provide a detail/info screen (e.g. quiz) show it — tapping an item
    # never launches it. Types without one keep the generic "avvia/programma" screen.
    render_detail = getattr(et, "render_detail", None)
    if render_detail is not None:
        await render_detail(callback.message, db_session, int(raw))
    else:
        await edit_or_send(
            callback.message,
            f"{et.hub_label} #{raw}\n\nVuoi avviarlo subito nel gruppo o programmarlo?",
            _item_kb(task_type, int(raw)),
        )
    await callback.answer()


# ---------------------------------------------------------------------------
# Confirmation gate — every impactful action asks "sicuro?" first (no accidental
# start/close/delete). Yes routes to the executor callback; No back to the detail.
# ---------------------------------------------------------------------------

# action → (executor prefix, prompt verb, yes-button label)
_CONFIRM: dict[str, tuple[str, str, str]] = {
    "askstart": ("ev:start", "avviare subito nel gruppo", "▶️ Sì, avvia"),
    "askclose": ("ev:close", "chiudere ora (pubblica il podio)", "🏁 Sì, chiudi"),
    "askdel": ("ev:del", "eliminare <b>definitivamente</b>", "🗑️ Sì, elimina"),
    # «e premi» diceva il falso: i premi già pagati restano pagati, e alla chiusura
    # successiva il montepremi viene erogato di nuovo per intero. È voluto — una
    # riproposizione è un evento nuovo — quindi è il testo che va detto com'è.
    "askreset": ("ev:reset", "riproporre (azzera le risposte e ripaga il montepremi intero)",
                 "🔁 Sì, riproponi"),
}


@router.callback_query(F.data.startswith("ev:ask"), IsAdminCallbackFilter())
async def cb_confirm(callback: CallbackQuery) -> None:
    _, action, task_type, raw = callback.data.split(":")
    conf = _CONFIRM.get(action)
    et = event_types.get(task_type)
    if conf is None or et is None or not raw.isdigit():
        await callback.answer()
        return
    exec_prefix, verb, yes_text = conf
    await edit_or_send(
        callback.message,
        f"⚠️ Vuoi {verb} <b>{et.hub_label} #{raw}</b>?",
        confirm_cancel_kb(
            f"{exec_prefix}:{task_type}:{raw}",
            f"ev:item:{task_type}:{raw}",
            yes_text=yes_text,
            no_text="⬅️ No, indietro",
        ),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Start now / close — generic dispatch through the event-type registry
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("ev:start:"), IsAdminCallbackFilter())
async def cb_start_now(callback: CallbackQuery, db_session: AsyncSession) -> None:
    _, _, task_type, raw = callback.data.split(":")
    et = event_types.get(task_type)
    if et is None or not raw.isdigit():
        await callback.answer()
        return
    res = await et.start_now(callback.bot, db_session, int(raw))
    if res.ok:
        await db_session.commit()  # spec mutated but never commits (STEERING §5)
    await callback.answer(res.message, show_alert=res.alert)
    await et.render_list(callback.message, db_session)


@router.callback_query(F.data.startswith("ev:close:"), IsAdminCallbackFilter())
async def cb_close(callback: CallbackQuery, db_session: AsyncSession) -> None:
    _, _, task_type, raw = callback.data.split(":")
    et = event_types.get(task_type)
    if et is None or not raw.isdigit():
        await callback.answer()
        return
    res = await et.close_now(callback.bot, db_session, int(raw))
    if res is None:  # type has no close action
        await callback.answer()
        return
    if res.ok:
        await db_session.commit()
    await callback.answer(res.message, show_alert=res.alert)
    await et.render_list(callback.message, db_session)


@router.callback_query(F.data.startswith("ev:del:"), IsAdminCallbackFilter())
async def cb_delete(callback: CallbackQuery, db_session: AsyncSession) -> None:
    _, _, task_type, raw = callback.data.split(":")
    et = event_types.get(task_type)
    delete = getattr(et, "delete", None) if et is not None else None
    if delete is None or not raw.isdigit():
        await callback.answer()
        return
    res = await delete(db_session, int(raw))
    if res.ok:
        await db_session.commit()  # spec mutated but never commits (STEERING §5)
    await callback.answer(res.message, show_alert=res.alert)
    await et.render_list(callback.message, db_session)  # item is gone → back to list


@router.callback_query(F.data.startswith("ev:reset:"), IsAdminCallbackFilter())
async def cb_reset(callback: CallbackQuery, db_session: AsyncSession) -> None:
    _, _, task_type, raw = callback.data.split(":")
    et = event_types.get(task_type)
    reset = getattr(et, "reset", None) if et is not None else None
    if reset is None or not raw.isdigit():
        await callback.answer()
        return
    res = await reset(db_session, int(raw))
    if res is None:  # type isn't re-runnable
        await callback.answer()
        return
    if res.ok:
        await db_session.commit()
    await callback.answer(res.message, show_alert=res.alert)
    # Still exists (now `ready` again) → refresh its detail if the type has one.
    render_detail = getattr(et, "render_detail", None)
    if render_detail is not None:
        await render_detail(callback.message, db_session, int(raw))
    else:
        await et.render_list(callback.message, db_session)


# ---------------------------------------------------------------------------
# Schedule (hands off to handlers.schedule)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("ev:sched:"), IsAdminCallbackFilter())
async def cb_schedule(callback: CallbackQuery, state: FSMContext) -> None:
    # Optional 5th segment pins what to schedule ("close"), used by the buttons on
    # an item that is already running — there, «avvio» is not one of the answers.
    parts = callback.data.split(":")
    task_type, raw = parts[2], parts[3]
    action = parts[4] if len(parts) > 4 else None
    et = event_types.get(task_type)
    if et is None or not raw.isdigit():
        await callback.answer()
        return
    from handlers.schedule import start_schedule_for
    await start_schedule_for(
        callback.message, state, task_type, int(raw), f"{et.hub_label} #{raw}", action
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Create — generic dispatch; each type enters its own creation FSM
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("ev:new:"), IsAdminCallbackFilter())
async def cb_new(callback: CallbackQuery, state: FSMContext) -> None:
    et = event_types.get(callback.data.split(":")[2])
    if et is None:
        await callback.answer()
        return
    await et.start_creation(callback.message, state, creator_id=callback.from_user.id)
    await callback.answer()


def _pt_cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Annulla", callback_data="ev:pt:cancel")
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


@router.callback_query(F.data == "ev:pt:cancel", IsAdminCallbackFilter())
async def cb_pt_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() is None:
        await callback.answer()
        return
    await callback.message.answer(
        "⚠️ Sicuro di voler annullare il sondaggio? I dati inseriti andranno persi.",
        reply_markup=confirm_cancel_kb("ev:pt:cancel_yes", "ev:pt:cancel_no"),
    )
    await callback.answer()


@router.callback_query(F.data == "ev:pt:cancel_yes", IsAdminCallbackFilter())
async def cb_pt_cancel_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Creazione sondaggio annullata.")
    await callback.answer()


@router.callback_query(F.data == "ev:pt:cancel_no", IsAdminCallbackFilter())
async def cb_pt_cancel_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("▶️ Ok, continua pure da dove eri rimasto.")
    await callback.answer()
