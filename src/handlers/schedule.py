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
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers import event_types
from keyboards.common_kb import confirm_cancel_kb
from services import group_registry, schedule_service
from utils import cooldown
from utils.text import esc

log = logging.getLogger(__name__)
router = Router()
# Admin-only router: gate every message/callback handler. Several flows here are
# FSM-state driven (run-at input, pickers); without a router-level admin gate a
# user who started a flow while admin and then lost admin could still drive it to
# completion — FSM state has no TTL (STEERING §8).
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminCallbackFilter())


class ScheduleStates(StatesGroup):
    # Unified flow: pick a pre-created quiz/poll/bet, then send the run-at time.
    event_runat = State()


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
    types = event_types.all_types()
    for et in types:
        b.button(text=et.hub_label, callback_data=f"sched:type:{et.key}")
    b.button(text="❌ Annulla", callback_data="sched:cancel")
    b.adjust(len(types) or 1, 1)
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


# --- Unified scheduling: pick a PRE-CREATED item, then a run-at time ---

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


@router.callback_query(F.data.startswith("sched:type:"))
async def cb_type(callback: CallbackQuery, state: FSMContext, db_session) -> None:
    et = event_types.get(callback.data.split(":")[2])
    if et is None:
        await callback.answer()
        return
    items = await et.schedulable_items(db_session)
    if not items:
        await callback.answer(
            f"Nessun elemento pronto per «{et.hub_label}». Crealo dagli Eventi.",
            show_alert=True,
        )
        return
    await callback.message.edit_text(
        f"{et.hub_label}: quale vuoi programmare?", reply_markup=_pick_kb(et.key, items)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sched:pick:"))
async def cb_pick_event(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, task_type, raw_id = callback.data.split(":")
    et = event_types.get(task_type)
    if et is None or not raw_id.isdigit():
        await callback.answer()
        return
    ref_id = int(raw_id)
    await start_schedule_for(
        callback.message, state, task_type, ref_id, f"{et.hub_label} #{ref_id}"
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
    b = InlineKeyboardBuilder()
    lines = ["🗓️ <b>Eventi programmati</b>\n"]
    for t in tasks:
        when = schedule_service.to_local(t.run_at).strftime("%d/%m %H:%M")
        et = event_types.get(t.task_type)
        label = et.hub_label if et else t.task_type
        lines.append(f"• #{t.id} {label} — {when}")
        b.button(text=f"❌ Annulla #{t.id}", callback_data=f"sched:del:{t.id}")
    b.adjust(1)
    await message.reply("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("sched:del:"))
async def cb_sched_del(callback: CallbackQuery, db_session) -> None:
    task_id = int(callback.data.split(":")[2])
    ok = await schedule_service.cancel(db_session, task_id)
    await db_session.commit()
    await callback.answer("Annullato." if ok else "Non annullabile.", show_alert=not ok)
    if ok:
        await callback.message.edit_text(f"❌ Evento #{task_id} annullato.")


# ---------------------------------------------------------------------------
# /sondaggio — create a poll (stored), then choose: start now or schedule
# ---------------------------------------------------------------------------

@router.message(Command("sondaggio"), IsAdminFilter())
async def cmd_sondaggio(message: Message, state: FSMContext) -> None:
    # The whole creation flow must happen in private chat — never in the group,
    # where anyone could read the prompts or interfere with the FSM. Same pattern
    # as /crea_quiz and /crea_scommessa: in a group, hand back a deep-link button
    # that re-opens (and re-checks admin) in private (STEERING §16).
    if message.chat.type != ChatType.PRIVATE:
        bot_info = await message.bot.get_me()
        await message.reply(
            "📊 Crea il sondaggio in chat privata:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="➡️ Crea sondaggio",
                    url=f"https://t.me/{bot_info.username}?start=create_poll",
                )
            ]]),
        )
        return
    if not await cooldown.guard(
        message, "event_create", settings.event_create_cooldown_seconds, exempt_admin=False
    ):
        return
    # Like quiz/scommesse: a poll is created and stored, then the admin chooses to
    # start it now or schedule it. Reuse the canonical poll-creation flow (no
    # immediate publish, no duplicated FSM). Lazy import avoids a circular import.
    from handlers.events import start_poll_creation

    await start_poll_creation(message, state)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _parse_or_reprompt(message: Message) -> datetime | None:
    try:
        return schedule_service.parse_run_at(message.text or "")
    except ValueError as e:
        await message.answer(f"⚠️ {e}\n\n{_RUNAT_HINT}", reply_markup=_cancel_kb())
        return None


async def _confirm(message: Message, task) -> None:
    et = event_types.get(task.task_type)
    label = et.hub_label if et else task.task_type
    when = schedule_service.to_local(task.run_at).strftime("%d/%m/%Y %H:%M")
    await message.answer(
        f"✅ <b>Programmato!</b>\n\n"
        f"{label} · #{task.id}\n"
        f"🕒 Esecuzione: <b>{when}</b>\n\n"
        f"Vedi/annulla con /programmati."
    )


# ---------------------------------------------------------------------------
# Scheduler loop + executor (started from main.py)
# ---------------------------------------------------------------------------

async def execute_task(bot, session, task) -> None:
    """Execute a single due task by delegating to its event-type spec.

    Raises on failure (the caller marks the task failed). Dispatch goes through
    the event-type registry — no per-type branching here.
    """
    # Prefer the live effective group id (the task's stored id may be stale after
    # a migration); fall back to the task's own group_id if none is configured.
    group_id = group_registry.get_group_id() or task.group_id
    if not group_id:
        raise RuntimeError("GROUP_ID non configurato")

    et = event_types.get(task.task_type)
    if et is None:
        raise RuntimeError(f"Tipo task sconosciuto: {task.task_type}")
    await et.execute_scheduled(bot, session, task, group_id)


async def _notify_creator(bot, task, text: str) -> None:
    """Best-effort DM to the admin who scheduled the task, so failures/skips are
    visible on Telegram and not only in the logs."""
    if not getattr(task, "created_by_tg_id", None):
        return
    try:
        await bot.send_message(task.created_by_tg_id, text)
    except Exception:  # noqa: BLE001 — the admin may have never opened the bot in private
        log.warning("Avviso task #%s all'admin %s fallito", task.id, task.created_by_tg_id)


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
                    except schedule_service.TaskSkip as e:
                        # Not an error: the task was a no-op (e.g. quiz already running).
                        log.info("Task #%s saltato: %s", task.id, e)
                        await schedule_service.mark_done(session, task)
                        await _notify_creator(
                            bot, task, f"ℹ️ Task #{task.id} ({task.task_type}) saltato: {e}"
                        )
                    except Exception as e:  # noqa: BLE001
                        log.exception("Task #%s fallito: %s", task.id, e)
                        await schedule_service.mark_failed(session, task, str(e))
                        await _notify_creator(
                            bot, task, f"⚠️ Task #{task.id} ({task.task_type}) fallito: {e}"
                        )
                    await session.commit()
        except Exception:  # noqa: BLE001 — loop must never die
            log.exception("Scheduler loop error")
        await asyncio.sleep(settings.scheduler_poll_interval)
