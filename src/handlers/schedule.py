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
from datetime import datetime

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
from keyboards.common_kb import confirm_cancel_kb
from services import bet_service, group_registry, poll_service, quiz_service, schedule_service
from utils import cooldown
from utils.text import esc

log = logging.getLogger(__name__)
router = Router()

_MIN_OPTIONS, _MAX_OPTIONS = 2, 10


class ScheduleStates(StatesGroup):
    # Unified flow: pick a pre-created quiz/poll/bet, then send the run-at time.
    event_runat = State()


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
    # Nothing entered yet (e.g. the type-choice menu / quiz picker) → cancel
    # directly; otherwise confirm before discarding the entered data.
    if await state.get_state() is None:
        await callback.message.edit_text("❌ Operazione annullata.")
        await callback.answer()
        return
    await callback.message.answer(
        "⚠️ Sicuro di voler annullare? I dati inseriti andranno persi.",
        reply_markup=confirm_cancel_kb("sched:cancel_yes", "sched:cancel_no"),
    )
    await callback.answer()


@router.callback_query(F.data == "sched:cancel_yes")
async def cb_sched_cancel_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Operazione annullata.")
    await callback.answer()


@router.callback_query(F.data == "sched:cancel_no")
async def cb_sched_cancel_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("▶️ Ok, continua pure da dove eri rimasto.")
    await callback.answer()


# --- Unified scheduling: pick a PRE-CREATED quiz/poll/bet, then a run-at time ---

_TYPE_LABEL = {"quiz": "🧠 Quiz", "poll": "📊 Sondaggio", "bet": "🎲 Scommessa"}


def _pick_kb(task_type: str, items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for iid, label in items:
        b.button(text=f"#{iid} {label[:30]}", callback_data=f"sched:pick:{task_type}:{iid}")
    b.button(text="❌ Annulla", callback_data="sched:cancel")
    b.adjust(1)
    return b.as_markup()


async def start_schedule_for(
    message: Message, state: FSMContext, task_type: str, ref_id: int, label: str
) -> None:
    """Enter the run-at step for an already-created event (used by /programma and
    by the Events hub «🗓️ Programma» buttons)."""
    await state.clear()
    await state.update_data(sched_type=task_type, sched_ref=ref_id, sched_label=label)
    await state.set_state(ScheduleStates.event_runat)
    await message.answer(
        f"🗓️ <b>Programma:</b> {esc(label)}\n\n{_RUNAT_HINT}", reply_markup=_cancel_kb()
    )


@router.callback_query(F.data == "sched:type:quiz")
async def cb_type_quiz(callback: CallbackQuery, state: FSMContext, db_session) -> None:
    items = [(q.id, q.title) for q in await quiz_service.list_ready(db_session) if q.status == "ready"]
    if not items:
        await callback.answer("Nessun quiz pronto. Crealo dagli Eventi o con /crea_quiz.", show_alert=True)
        return
    await callback.message.edit_text("🧠 Quale quiz vuoi programmare?", reply_markup=_pick_kb("quiz", items))
    await callback.answer()


@router.callback_query(F.data == "sched:type:poll")
async def cb_type_poll(callback: CallbackQuery, state: FSMContext, db_session) -> None:
    items = [(p.id, p.question) for p in await poll_service.list_ready(db_session)]
    if not items:
        await callback.answer("Nessun sondaggio pronto. Crealo dagli Eventi.", show_alert=True)
        return
    await callback.message.edit_text("📊 Quale sondaggio vuoi programmare?", reply_markup=_pick_kb("poll", items))
    await callback.answer()


@router.callback_query(F.data == "sched:type:bet")
async def cb_type_bet(callback: CallbackQuery, state: FSMContext, db_session) -> None:
    items = [(e.id, e.title) for e in await bet_service.list_drafts(db_session)]
    if not items:
        await callback.answer("Nessuna scommessa in bozza. Creala dagli Eventi.", show_alert=True)
        return
    await callback.message.edit_text("🎲 Quale scommessa vuoi programmare?", reply_markup=_pick_kb("bet", items))
    await callback.answer()


@router.callback_query(F.data.startswith("sched:pick:"))
async def cb_pick_event(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, task_type, raw_id = callback.data.split(":")
    if task_type not in _TYPE_LABEL or not raw_id.isdigit():
        await callback.answer()
        return
    ref_id = int(raw_id)
    await start_schedule_for(
        callback.message, state, task_type, ref_id, f"{_TYPE_LABEL[task_type]} #{ref_id}"
    )
    await callback.answer()


@router.message(ScheduleStates.event_runat, ~F.text.startswith("/"))
async def fsm_event_runat(message: Message, state: FSMContext, db_session) -> None:
    run_at = await _parse_or_reprompt(message)
    if run_at is None:
        return
    data = await state.get_data()
    task = await schedule_service.schedule_task(
        db_session, data["sched_type"], run_at, message.from_user.id,
        group_registry.get_group_id() or None, ref_id=data["sched_ref"],
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
    if not await cooldown.guard(
        message, "event_create", settings.event_create_cooldown_seconds, exempt_admin=False
    ):
        return
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
    target = group_registry.get_group_id() or message.chat.id
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
    # Prefer the live effective group id (the task's stored id may be stale after
    # a migration); fall back to the task's own group_id if none is configured.
    group_id = group_registry.get_group_id() or task.group_id
    if not group_id:
        raise RuntimeError("GROUP_ID non configurato")

    if task.task_type == "quiz":
        from handlers.quiz import open_quiz
        ok, msg = await open_quiz(bot, session, task.ref_id)
        if not ok:
            raise RuntimeError(msg)

    elif task.task_type == "poll":
        # New model: ref_id → a pre-created PollTemplate. Legacy: inline payload.
        if task.ref_id:
            poll = await poll_service.get(session, task.ref_id)
            if poll is None:
                raise RuntimeError(f"Sondaggio #{task.ref_id} non trovato")
            question, options = poll.question, poll_service.options_of(poll)
            await poll_service.mark_used(session, poll.id)
        else:
            payload = schedule_service.task_payload(task)
            question, options = payload["question"], payload["options"]
        await bot.send_poll(
            chat_id=group_id, question=question, options=options, is_anonymous=False,
        )

    elif task.task_type == "bet":
        # New model: ref_id → activate a pre-created draft event. Legacy: payload.
        if task.ref_id:
            event = await bet_service.activate_event(session, task.ref_id)
            description = event.description
        else:
            payload = schedule_service.task_payload(task)
            event = await bet_service.create_event(
                session,
                creator_tg_id=task.created_by_tg_id,
                title=payload["title"],
                description=payload.get("description", ""),
                options=[{"label": o} for o in payload["options"]],
            )
            description = payload.get("description", "")
        await session.flush()
        bot_info = await bot.get_me()
        await group_registry.send_group_message(
            bot, session,
            f"🎲 <b>Nuova scommessa aperta!</b>\n\n"
            f"<b>{esc(event.title)}</b>\n{esc(description)}",
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
