"""
Scheduling — program the future opening of a quiz, a poll or a bet (admin).

Telegram cannot schedule polls server-side, so we persist ScheduledTask rows and
run them from an in-process loop (`scheduler_loop`, started in main.py). The loop
survives restarts because tasks are in the DB.

Also provides /sondaggio to post a poll to the group immediately.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters.command import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config_data.config import settings
from database.connection import async_session_maker
from filters.admin_filter import IsAdminFilter
from services import bet_service, quiz_service, schedule_service

log = logging.getLogger(__name__)
router = Router()

_MIN_OPTIONS, _MAX_OPTIONS = 2, 10


class ScheduleStates(StatesGroup):
    quiz_runat = State()
    poll_question = State()
    poll_options = State()
    poll_runat = State()
    bet_title = State()
    bet_description = State()
    bet_options = State()
    bet_runat = State()


class SondaggioStates(StatesGroup):
    question = State()
    options = State()


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Annulla", callback_data="sched:cancel")
    ]])


_RUNAT_HINT = (
    "🕒 Quando? Invia un orario:\n"
    "• assoluto: <code>2026-05-30 18:00</code>\n"
    "• relativo: <code>30m</code> · <code>2h</code> · <code>1d</code>"
)


# ---------------------------------------------------------------------------
# /programma — choose what to schedule
# ---------------------------------------------------------------------------

async def start_schedule_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="🧠 Quiz", callback_data="sched:type:quiz")
    b.button(text="📊 Sondaggio", callback_data="sched:type:poll")
    b.button(text="🎲 Scommessa", callback_data="sched:type:bet")
    b.button(text="❌ Annulla", callback_data="sched:cancel")
    b.adjust(3, 1)
    await message.answer("🗓️ <b>Programma un evento</b>\n\nCosa vuoi programmare?", reply_markup=b.as_markup())


@router.message(Command("programma"), IsAdminFilter())
async def cmd_programma(message: Message, state: FSMContext) -> None:
    if message.chat.type != ChatType.PRIVATE:
        bot_info = await message.bot.get_me()
        await message.reply(
            "🗓️ Programma in chat privata:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="➡️ Programma", url=f"https://t.me/{bot_info.username}?start=programma"
                )
            ]]),
        )
        return
    await start_schedule_flow(message, state)


@router.callback_query(F.data == "sched:cancel")
async def cb_sched_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Operazione annullata.")
    await callback.answer()


# --- Quiz scheduling ---

@router.callback_query(F.data == "sched:type:quiz")
async def cb_type_quiz(callback: CallbackQuery, state: FSMContext, db_session) -> None:
    quizzes = await quiz_service.list_ready(db_session)
    if not quizzes:
        await callback.answer("Nessun quiz pronto. Creane uno con /crea_quiz.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for q in quizzes:
        b.button(text=f"#{q.id} {q.title[:30]}", callback_data=f"sched:quiz:{q.id}")
    b.button(text="❌ Annulla", callback_data="sched:cancel")
    b.adjust(1)
    await callback.message.edit_text("🧠 Quale quiz vuoi programmare?", reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("sched:quiz:"))
async def cb_pick_quiz(callback: CallbackQuery, state: FSMContext) -> None:
    quiz_id = int(callback.data.split(":")[2])
    await state.update_data(quiz_id=quiz_id)
    await state.set_state(ScheduleStates.quiz_runat)
    await callback.message.edit_text(_RUNAT_HINT, reply_markup=_cancel_kb())
    await callback.answer()


@router.message(ScheduleStates.quiz_runat, ~F.text.startswith("/"))
async def fsm_quiz_runat(message: Message, state: FSMContext, db_session) -> None:
    run_at = await _parse_or_reprompt(message)
    if run_at is None:
        return
    data = await state.get_data()
    task = await schedule_service.schedule_task(
        db_session, "quiz", run_at, message.from_user.id, settings.group_id or None,
        ref_id=data["quiz_id"],
    )
    await db_session.commit()
    await state.clear()
    await _confirm(message, task)


# --- Poll scheduling ---

@router.callback_query(F.data == "sched:type:poll")
async def cb_type_poll(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ScheduleStates.poll_question)
    await callback.message.edit_text("📊 Invia la <b>domanda</b> del sondaggio:", reply_markup=_cancel_kb())
    await callback.answer()


@router.message(ScheduleStates.poll_question, ~F.text.startswith("/"))
async def fsm_poll_question(message: Message, state: FSMContext) -> None:
    q = (message.text or "").strip()[:300]
    if len(q) < 3:
        await message.answer("⚠️ Domanda troppo corta.", reply_markup=_cancel_kb())
        return
    await state.update_data(poll_question=q)
    await state.set_state(ScheduleStates.poll_options)
    await message.answer(
        f"Invia le <b>opzioni</b>, una per riga (da {_MIN_OPTIONS} a {_MAX_OPTIONS}):",
        reply_markup=_cancel_kb(),
    )


@router.message(ScheduleStates.poll_options, ~F.text.startswith("/"))
async def fsm_poll_options(message: Message, state: FSMContext) -> None:
    options = _parse_options(message.text)
    if options is None:
        await message.answer(
            f"⚠️ Servono da {_MIN_OPTIONS} a {_MAX_OPTIONS} opzioni (≤100 caratteri, una per riga).",
            reply_markup=_cancel_kb(),
        )
        return
    await state.update_data(poll_options=options)
    await state.set_state(ScheduleStates.poll_runat)
    await message.answer(_RUNAT_HINT, reply_markup=_cancel_kb())


@router.message(ScheduleStates.poll_runat, ~F.text.startswith("/"))
async def fsm_poll_runat(message: Message, state: FSMContext, db_session) -> None:
    run_at = await _parse_or_reprompt(message)
    if run_at is None:
        return
    data = await state.get_data()
    task = await schedule_service.schedule_task(
        db_session, "poll", run_at, message.from_user.id, settings.group_id or None,
        payload={"question": data["poll_question"], "options": data["poll_options"]},
    )
    await db_session.commit()
    await state.clear()
    await _confirm(message, task)


# --- Bet scheduling ---

@router.callback_query(F.data == "sched:type:bet")
async def cb_type_bet(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ScheduleStates.bet_title)
    await callback.message.edit_text("🎲 Invia il <b>titolo</b> della scommessa:", reply_markup=_cancel_kb())
    await callback.answer()


@router.message(ScheduleStates.bet_title, ~F.text.startswith("/"))
async def fsm_bet_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:200]
    if len(title) < 4:
        await message.answer("⚠️ Titolo troppo corto (min 4).", reply_markup=_cancel_kb())
        return
    await state.update_data(bet_title=title)
    await state.set_state(ScheduleStates.bet_description)
    await message.answer("Invia una breve <b>descrizione</b> (o «-»):", reply_markup=_cancel_kb())


@router.message(ScheduleStates.bet_description, ~F.text.startswith("/"))
async def fsm_bet_desc(message: Message, state: FSMContext) -> None:
    desc = (message.text or "").strip()[:500]
    if desc == "-":
        desc = ""
    await state.update_data(bet_description=desc)
    await state.set_state(ScheduleStates.bet_options)
    await message.answer(
        f"Invia le <b>opzioni</b>, una per riga (da {_MIN_OPTIONS} a {_MAX_OPTIONS}):",
        reply_markup=_cancel_kb(),
    )


@router.message(ScheduleStates.bet_options, ~F.text.startswith("/"))
async def fsm_bet_options(message: Message, state: FSMContext) -> None:
    options = _parse_options(message.text)
    if options is None:
        await message.answer(
            f"⚠️ Servono da {_MIN_OPTIONS} a {_MAX_OPTIONS} opzioni (una per riga).",
            reply_markup=_cancel_kb(),
        )
        return
    await state.update_data(bet_options=options)
    await state.set_state(ScheduleStates.bet_runat)
    await message.answer(_RUNAT_HINT, reply_markup=_cancel_kb())


@router.message(ScheduleStates.bet_runat, ~F.text.startswith("/"))
async def fsm_bet_runat(message: Message, state: FSMContext, db_session) -> None:
    run_at = await _parse_or_reprompt(message)
    if run_at is None:
        return
    data = await state.get_data()
    task = await schedule_service.schedule_task(
        db_session, "bet", run_at, message.from_user.id, settings.group_id or None,
        payload={
            "title": data["bet_title"],
            "description": data.get("bet_description", ""),
            "options": data["bet_options"],
        },
    )
    await db_session.commit()
    await state.clear()
    await _confirm(message, task)


# ---------------------------------------------------------------------------
# /programmati — list & cancel
# ---------------------------------------------------------------------------

@router.message(Command("programmati"), IsAdminFilter())
async def cmd_programmati(message: Message, db_session) -> None:
    tasks = await schedule_service.list_pending(db_session)
    if not tasks:
        await message.reply("🗓️ Nessun evento programmato.")
        return
    labels = {"quiz": "🧠 Quiz", "poll": "📊 Sondaggio", "bet": "🎲 Scommessa"}
    b = InlineKeyboardBuilder()
    lines = ["🗓️ <b>Eventi programmati</b>\n"]
    for t in tasks:
        when = t.run_at.strftime("%d/%m %H:%M")
        lines.append(f"• #{t.id} {labels.get(t.task_type, t.task_type)} — {when} UTC")
        b.button(text=f"❌ Annulla #{t.id}", callback_data=f"sched:del:{t.id}")
    b.adjust(1)
    await message.reply("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("sched:del:"), IsAdminFilter())
async def cb_sched_del(callback: CallbackQuery, db_session) -> None:
    task_id = int(callback.data.split(":")[2])
    ok = await schedule_service.cancel(db_session, task_id)
    await db_session.commit()
    await callback.answer("Annullato." if ok else "Non annullabile.", show_alert=not ok)
    if ok:
        await callback.message.edit_text(f"❌ Evento #{task_id} annullato.")


# ---------------------------------------------------------------------------
# /sondaggio — post a poll to the group right now
# ---------------------------------------------------------------------------

@router.message(Command("sondaggio"), IsAdminFilter())
async def cmd_sondaggio(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SondaggioStates.question)
    await message.reply("📊 Invia la <b>domanda</b> del sondaggio:", reply_markup=_cancel_kb())


@router.message(SondaggioStates.question, ~F.text.startswith("/"))
async def fsm_sondaggio_q(message: Message, state: FSMContext) -> None:
    q = (message.text or "").strip()[:300]
    if len(q) < 3:
        await message.answer("⚠️ Domanda troppo corta.", reply_markup=_cancel_kb())
        return
    await state.update_data(question=q)
    await state.set_state(SondaggioStates.options)
    await message.answer(
        f"Invia le <b>opzioni</b>, una per riga (da {_MIN_OPTIONS} a {_MAX_OPTIONS}):",
        reply_markup=_cancel_kb(),
    )


@router.message(SondaggioStates.options, ~F.text.startswith("/"))
async def fsm_sondaggio_opts(message: Message, state: FSMContext) -> None:
    options = _parse_options(message.text)
    if options is None:
        await message.answer(
            f"⚠️ Servono da {_MIN_OPTIONS} a {_MAX_OPTIONS} opzioni (una per riga).",
            reply_markup=_cancel_kb(),
        )
        return
    data = await state.get_data()
    await state.clear()
    target = settings.group_id or message.chat.id
    await message.bot.send_poll(
        chat_id=target, question=data["question"], options=options, is_anonymous=False
    )
    await message.answer("✅ Sondaggio pubblicato nel gruppo!")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_options(text: str | None) -> list[str] | None:
    options = [o.strip() for o in (text or "").splitlines() if o.strip()]
    if not (_MIN_OPTIONS <= len(options) <= _MAX_OPTIONS):
        return None
    if any(len(o) > 100 for o in options):
        return None
    return options


async def _parse_or_reprompt(message: Message) -> datetime | None:
    try:
        return schedule_service.parse_run_at(message.text or "")
    except ValueError as e:
        await message.answer(f"⚠️ {e}\n\n{_RUNAT_HINT}", reply_markup=_cancel_kb())
        return None


async def _confirm(message: Message, task) -> None:
    labels = {"quiz": "🧠 Quiz", "poll": "📊 Sondaggio", "bet": "🎲 Scommessa"}
    when = task.run_at.strftime("%d/%m/%Y %H:%M")
    await message.answer(
        f"✅ <b>Programmato!</b>\n\n"
        f"{labels.get(task.task_type, task.task_type)} · #{task.id}\n"
        f"🕒 Esecuzione: <b>{when} UTC</b>\n\n"
        f"Vedi/annulla con /programmati."
    )


# ---------------------------------------------------------------------------
# Scheduler loop + executor (started from main.py)
# ---------------------------------------------------------------------------

async def execute_task(bot, session, task) -> None:
    """Execute a single due task. Raises on failure (caller marks failed)."""
    group_id = task.group_id or settings.group_id
    if not group_id:
        raise RuntimeError("GROUP_ID non configurato")

    if task.task_type == "quiz":
        from handlers.quiz import open_quiz
        ok, msg = await open_quiz(bot, session, task.ref_id)
        if not ok:
            raise RuntimeError(msg)

    elif task.task_type == "poll":
        payload = schedule_service.task_payload(task)
        await bot.send_poll(
            chat_id=group_id, question=payload["question"],
            options=payload["options"], is_anonymous=False,
        )

    elif task.task_type == "bet":
        payload = schedule_service.task_payload(task)
        event = await bet_service.create_event(
            session,
            creator_tg_id=task.created_by_tg_id,
            title=payload["title"],
            description=payload.get("description", ""),
            options=[{"label": o} for o in payload["options"]],
        )
        await session.flush()
        bot_info = await bot.get_me()
        await bot.send_message(
            group_id,
            f"🎲 <b>Nuova scommessa aperta!</b>\n\n"
            f"<b>{event.title}</b>\n{payload.get('description', '')}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🎯 Scommetti", url=f"https://t.me/{bot_info.username}?start=bet_{event.id}"
                )
            ]]),
        )
    else:
        raise RuntimeError(f"Tipo task sconosciuto: {task.task_type}")


async def scheduler_loop(bot) -> None:
    """Background loop: execute due ScheduledTasks. Started in main()."""
    log.info("Scheduler avviato (intervallo %ss).", settings.scheduler_poll_interval)
    while True:
        try:
            async with async_session_maker() as session:
                tasks = await schedule_service.due_tasks(session, schedule_service.utcnow())
                for task in tasks:
                    try:
                        await execute_task(bot, session, task)
                        await schedule_service.mark_done(session, task)
                    except Exception as e:  # noqa: BLE001
                        log.exception("Task #%s fallito: %s", task.id, e)
                        await schedule_service.mark_failed(session, task, str(e))
                    await session.commit()
        except Exception:  # noqa: BLE001 — loop must never die
            log.exception("Scheduler loop error")
        await asyncio.sleep(settings.scheduler_poll_interval)
