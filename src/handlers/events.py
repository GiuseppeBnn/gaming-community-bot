"""
Events hub — one place to manage the three event types (quiz · sondaggi ·
scommesse) under a shared model: each is **pre-created**, then either **started
now** in the group or **scheduled**, exactly like quizzes already worked.

Callback grammar (namespace ``ev:*``, well within Telegram's 64-byte limit):
  ev:home                      → the events hub (three type buttons)
  ev:list:<type>               → list pre-created items of a type
  ev:item:<type>:<id>          → manage one item (avvia ora / programma)
  ev:start:<type>:<id>         → start it now in the group
  ev:sched:<type>:<id>         → schedule it (hands off to handlers.schedule)
  ev:close:quiz:<id>           → close a running quiz (publish the podium)
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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers._privacy import redirect_to_private
from keyboards.common_kb import confirm_cancel_kb
from services import bet_service, group_registry, poll_service, quiz_service
from utils.text import esc

log = logging.getLogger(__name__)
router = Router()
# Admin-only router: gate every message/callback handler so no FSM-state-only
# handler can be driven by a user who has lost admin (STEERING §8).
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminCallbackFilter())

_MIN_OPTIONS, _MAX_OPTIONS = 2, 10
_TYPE_LABEL = {"quiz": "🧠 Quiz", "poll": "📊 Sondaggio", "bet": "🎲 Scommessa"}


class PollTemplateStates(StatesGroup):
    question = State()
    options = State()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

async def _edit(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit in place when possible, else send a fresh message (callbacks vs DMs)."""
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001 — message may be too old / identical
        await message.answer(text, reply_markup=kb)


def _hub_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🧠 Quiz", callback_data="ev:list:quiz")
    b.button(text="📊 Sondaggi", callback_data="ev:list:poll")
    b.button(text="🎲 Scommesse", callback_data="ev:list:bet")
    b.button(text="⬅️ Dashboard", callback_data="adm:home")
    b.adjust(3, 1)
    return b.as_markup()


def _item_kb(task_type: str, item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Avvia ora", callback_data=f"ev:start:{task_type}:{item_id}")
    b.button(text="🗓️ Programma", callback_data=f"ev:sched:{task_type}:{item_id}")
    b.button(text="⬅️ Indietro", callback_data=f"ev:list:{task_type}")
    b.adjust(2, 1)
    return b.as_markup()


async def show_hub(message: Message) -> None:
    await _edit(
        message,
        "🎬 <b>Eventi</b>\n\n"
        "Crea quiz, sondaggi e scommesse, poi <b>avviali subito</b> nel gruppo "
        "oppure <b>programmali</b>.\n\nScegli un tipo:",
        _hub_kb(),
    )


async def _render_quiz_list(message: Message, db_session: AsyncSession) -> None:
    quizzes = await quiz_service.list_ready(db_session)  # ready + running
    b = InlineKeyboardBuilder()
    lines = ["🧠 <b>Quiz</b>\n"]
    for q in quizzes:
        if q.status == "running":
            lines.append(f"🟢 #{q.id} {esc(q.title)} — <i>in corso</i>")
            b.button(text=f"🏁 Chiudi #{q.id}", callback_data=f"ev:close:quiz:{q.id}")
        else:
            lines.append(f"🟡 #{q.id} {esc(q.title)} — <i>pronto</i>")
            b.button(text=f"⚙️ #{q.id} {q.title[:22]}", callback_data=f"ev:item:quiz:{q.id}")
    if not quizzes:
        lines.append("<i>Nessun quiz. Creane uno.</i>")
    b.button(text="➕ Crea quiz", callback_data="ev:new:quiz")
    b.button(text="⬅️ Eventi", callback_data="ev:home")
    b.adjust(1)
    await _edit(message, "\n".join(lines), b.as_markup())


async def _render_poll_list(message: Message, db_session: AsyncSession) -> None:
    polls = await poll_service.list_ready(db_session)
    b = InlineKeyboardBuilder()
    lines = ["📊 <b>Sondaggi pronti</b>\n"]
    for p in polls:
        lines.append(f"#{p.id} {esc(p.question)}")
        b.button(text=f"⚙️ #{p.id} {p.question[:22]}", callback_data=f"ev:item:poll:{p.id}")
    if not polls:
        lines.append("<i>Nessun sondaggio pronto. Creane uno.</i>")
    b.button(text="➕ Crea sondaggio", callback_data="ev:new:poll")
    b.button(text="⬅️ Eventi", callback_data="ev:home")
    b.adjust(1)
    await _edit(message, "\n".join(lines), b.as_markup())


async def _render_bet_list(message: Message, db_session: AsyncSession) -> None:
    drafts = await bet_service.list_drafts(db_session)
    b = InlineKeyboardBuilder()
    lines = ["🎲 <b>Scommesse in bozza</b>\n"]
    for e in drafts:
        lines.append(f"#{e.id} {esc(e.title)}")
        b.button(text=f"⚙️ #{e.id} {e.title[:22]}", callback_data=f"ev:item:bet:{e.id}")
    if not drafts:
        lines.append("<i>Nessuna bozza. Creane una.</i>")
    b.button(text="➕ Crea scommessa", callback_data="ev:new:bet")
    b.button(text="🛠️ Scommesse attive", callback_data="adm:bets")
    b.button(text="⬅️ Eventi", callback_data="ev:home")
    b.adjust(1)
    await _edit(message, "\n".join(lines), b.as_markup())


_RENDER = {"quiz": _render_quiz_list, "poll": _render_poll_list, "bet": _render_bet_list}


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
    task_type = callback.data.split(":")[2]
    render = _RENDER.get(task_type)
    if render is None:
        await callback.answer()
        return
    await render(callback.message, db_session)
    await callback.answer()


@router.callback_query(F.data.startswith("ev:item:"), IsAdminCallbackFilter())
async def cb_item(callback: CallbackQuery) -> None:
    _, _, task_type, raw = callback.data.split(":")
    if task_type not in _TYPE_LABEL or not raw.isdigit():
        await callback.answer()
        return
    await _edit(
        callback.message,
        f"{_TYPE_LABEL[task_type]} #{raw}\n\nVuoi avviarlo subito nel gruppo o programmarlo?",
        _item_kb(task_type, int(raw)),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Start now
# ---------------------------------------------------------------------------

async def _announce_bet_open(bot, db_session: AsyncSession, event) -> None:
    if group_registry.get_group_id() == 0:
        return
    bot_info = await bot.get_me()
    try:
        await group_registry.send_group_message(
            bot, db_session,
            f"🎲 <b>Nuova scommessa aperta!</b>\n\n<b>{esc(event.title)}</b>\n{esc(event.description)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🎯 Scommetti", url=f"https://t.me/{bot_info.username}?start=bet_{event.id}"
                )
            ]]),
        )
    except Exception:  # noqa: BLE001
        log.warning("Annuncio scommessa #%s fallito", event.id)


@router.callback_query(F.data.startswith("ev:start:"), IsAdminCallbackFilter())
async def cb_start_now(callback: CallbackQuery, db_session: AsyncSession) -> None:
    _, _, task_type, raw = callback.data.split(":")
    if not raw.isdigit():
        await callback.answer()
        return
    item_id = int(raw)

    if task_type == "quiz":
        from handlers.quiz import open_quiz
        ok, msg = await open_quiz(callback.bot, db_session, item_id)
        if ok:
            await db_session.commit()
        await callback.answer(msg, show_alert=not ok)

    elif task_type == "poll":
        poll = await poll_service.get(db_session, item_id)
        if poll is None or poll.status != "ready":
            await callback.answer("Sondaggio non disponibile.", show_alert=True)
            return
        group_id = group_registry.get_group_id()
        if not group_id:
            await callback.answer("GROUP_ID non configurato.", show_alert=True)
            return
        await callback.bot.send_poll(
            chat_id=group_id, question=poll.question,
            options=poll_service.options_of(poll), is_anonymous=False,
        )
        await poll_service.mark_used(db_session, item_id)
        await db_session.commit()
        await callback.answer("📊 Sondaggio pubblicato nel gruppo!")

    elif task_type == "bet":
        try:
            event = await bet_service.activate_event(db_session, item_id)
        except Exception as e:  # noqa: BLE001
            await callback.answer(f"⚠️ {e}", show_alert=True)
            return
        await db_session.commit()
        await _announce_bet_open(callback.bot, db_session, event)
        await callback.answer("🎲 Scommessa avviata nel gruppo!")
    else:
        await callback.answer()
        return

    await _RENDER[task_type](callback.message, db_session)


@router.callback_query(F.data.startswith("ev:close:quiz:"), IsAdminCallbackFilter())
async def cb_close_quiz(callback: CallbackQuery, db_session: AsyncSession) -> None:
    quiz_id = int(callback.data.split(":")[3])
    from handlers.quiz import close_quiz
    ok, msg = await close_quiz(callback.bot, db_session, quiz_id)
    await callback.answer("🏁 Quiz chiuso. Podio pubblicato." if ok else msg, show_alert=not ok)
    await _render_quiz_list(callback.message, db_session)


# ---------------------------------------------------------------------------
# Schedule (hands off to handlers.schedule)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("ev:sched:"), IsAdminCallbackFilter())
async def cb_schedule(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, task_type, raw = callback.data.split(":")
    if task_type not in _TYPE_LABEL or not raw.isdigit():
        await callback.answer()
        return
    from handlers.schedule import start_schedule_for
    await start_schedule_for(
        callback.message, state, task_type, int(raw), f"{_TYPE_LABEL[task_type]} #{raw}"
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "ev:new:quiz", IsAdminCallbackFilter())
async def cb_new_quiz(callback: CallbackQuery, state: FSMContext) -> None:
    from handlers.quiz import start_quiz_creation
    await start_quiz_creation(callback.message, state, creator_id=callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "ev:new:bet", IsAdminCallbackFilter())
async def cb_new_bet(callback: CallbackQuery, state: FSMContext) -> None:
    from handlers.betting import start_bet_creation
    await start_bet_creation(callback.message, state, as_draft=True)
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


@router.callback_query(F.data == "ev:new:poll", IsAdminCallbackFilter())
async def cb_new_poll(callback: CallbackQuery, state: FSMContext) -> None:
    await start_poll_creation(callback.message, state)
    await callback.answer()


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
