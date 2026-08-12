"""
Scheduling — program the future opening (and, where it means something, the
closing) of a quiz, a guess round, a poll or a bet (admin).

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
from handlers.callbacks import SchedCb
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
        InlineKeyboardButton(text="❌ Annulla", callback_data=SchedCb(action="cancel").pack())
    ]])


_RUNAT_HINT = (
    "🕒 Quando? Invia un orario:\n"
    "• assoluto: <code>2026-05-30 18:00</code>\n"
    "• relativo: <code>30m</code> · <code>2h</code> · <code>1d</code>"
)

# What a scheduled task does to its item. `start` is the historical behaviour and
# stays payload-free; `close` rides the SAME task_type with an action payload — the
# pattern the betting auto-lock and the guess auto-close already use, so no new
# task type and no new column (STEERING §18.2).
_ACTION_START = "start"
_ACTION_CLOSE = "close"
#: action → (button label, the word used in the prompts and the confirmation)
_ACTIONS: dict[str, tuple[str, str]] = {
    _ACTION_START: ("▶️ Avvio", "l'avvio"),
    _ACTION_CLOSE: ("🏁 Chiusura e risultati", "la chiusura"),
}


# ---------------------------------------------------------------------------
# /programma — choose what to schedule
# ---------------------------------------------------------------------------

async def start_schedule_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    b = InlineKeyboardBuilder()
    types = event_types.all_types()
    for et in types:
        b.button(text=et.hub_label, callback_data=SchedCb(action="type", key=et.key).pack())
    b.button(text="❌ Annulla", callback_data=SchedCb(action="cancel").pack())
    b.adjust(1)
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


@router.callback_query(SchedCb.filter(F.action == "cancel"))
async def cb_sched_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    # Nothing entered yet (e.g. the type-choice menu / quiz picker) → cancel
    # directly; otherwise confirm before discarding the entered data.
    if await state.get_state() is None:
        await callback.message.edit_text("❌ Operazione annullata.")
        await callback.answer()
        return
    await callback.message.answer(
        "⚠️ Sicuro di voler annullare? I dati inseriti andranno persi.",
        reply_markup=confirm_cancel_kb(
            SchedCb(action="cancel_yes").pack(), SchedCb(action="cancel_no").pack()
        ),
    )
    await callback.answer()


@router.callback_query(SchedCb.filter(F.action == "cancel_yes"))
async def cb_sched_cancel_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Operazione annullata.")
    await callback.answer()


@router.callback_query(SchedCb.filter(F.action == "cancel_no"))
async def cb_sched_cancel_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("▶️ Ok, continua pure da dove eri rimasto.")
    await callback.answer()


# --- Unified scheduling: pick a PRE-CREATED item, then a run-at time ---

def _pick_kb(task_type: str, items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for iid, label in items:
        b.button(
            text=f"#{iid} {label[:30]}",
            callback_data=SchedCb(action="pick", key=task_type, item_id=iid).pack(),
        )
    b.button(text="❌ Annulla", callback_data=SchedCb(action="cancel").pack())
    b.adjust(1)
    return b.as_markup()


async def start_schedule_for(
    message: Message, state: FSMContext, task_type: str, ref_id: int, label: str,
    action: str | None = None,
) -> None:
    """Enter the scheduling flow for an already-created event (used by /programma
    and by the Events hub «🗓️ Programma» buttons).

    With `action` given the run-at step comes straight away. Without it, a type that
    can also be closed on a timer (`closable`) is asked *what* to schedule first —
    the other types keep going directly to the time, because a question with one
    possible answer is not a question.
    """
    await state.clear()
    await state.update_data(sched_type=task_type, sched_ref=ref_id, sched_label=label)
    et = event_types.get(task_type)
    if action is None and getattr(et, "closable", False):
        b = InlineKeyboardBuilder()
        for key, (button, _word) in _ACTIONS.items():
            b.button(text=button, callback_data=SchedCb(action="act", key=key).pack())
        b.button(text="❌ Annulla", callback_data=SchedCb(action="cancel").pack())
        b.adjust(2, 1)
        await message.answer(
            f"🗓️ <b>Programma:</b> {esc(label)}\n\nCosa vuoi programmare?",
            reply_markup=b.as_markup(),
        )
        return
    await _ask_run_at(message, state, label, action or _ACTION_START)


async def _ask_run_at(
    message: Message, state: FSMContext, label: str, action: str
) -> None:
    await state.update_data(sched_action=action)
    await state.set_state(ScheduleStates.event_runat)
    word = _ACTIONS[action][1]
    await message.answer(
        f"🗓️ <b>Programma {word}:</b> {esc(label)}\n\n{_RUNAT_HINT}",
        reply_markup=_cancel_kb(),
    )


@router.callback_query(SchedCb.filter(F.action == "act"))
async def cb_action(
    callback: CallbackQuery, callback_data: SchedCb, state: FSMContext
) -> None:
    action = callback_data.key
    data = await state.get_data()
    if action is None or action not in _ACTIONS or "sched_ref" not in data:
        # Unknown action, or a stale button from a flow that has since been
        # cleared: there is nothing left to schedule, so say so instead of
        # arming a run-at step with no target.
        await callback.answer("Ricomincia da /programma.", show_alert=True)
        return
    await _ask_run_at(callback.message, state, data["sched_label"], action)
    await callback.answer()


@router.callback_query(SchedCb.filter(F.action == "type"))
async def cb_type(
    callback: CallbackQuery, callback_data: SchedCb, state: FSMContext, db_session
) -> None:
    task_type = callback_data.key
    if task_type is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
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


@router.callback_query(SchedCb.filter(F.action == "pick"))
async def cb_pick_event(
    callback: CallbackQuery, callback_data: SchedCb, state: FSMContext
) -> None:
    task_type = callback_data.key
    # No isdigit() guard: a non-numeric id no longer reaches this handler, the
    # filter drops it (tests/unit/test_callbacks.py).
    item_id = callback_data.item_id
    if task_type is None or item_id is None:
        await callback.answer()
        return
    et = event_types.get(task_type)
    if et is None:
        await callback.answer()
        return
    ref_id = item_id
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
    action = data.get("sched_action", _ACTION_START)
    task = await schedule_service.schedule_task(
        db_session, data["sched_type"], run_at, message.from_user.id,
        group_registry.get_group_id() or None, ref_id=data["sched_ref"],
        # A start carries no payload, exactly as before: the close is the addition.
        payload={"action": _ACTION_CLOSE} if action == _ACTION_CLOSE else None,
    )
    await db_session.commit()
    await state.clear()
    await _confirm(message, task, action)


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
        # An item can have both a start and a close pending: saying which is which
        # is the difference between cancelling the right one and the wrong one.
        action = schedule_service.task_payload(t).get("action", _ACTION_START)
        what = _ACTIONS[action][0] if action in _ACTIONS else ""
        lines.append(f"• #{t.id} {label} {what} — {when}")
        b.button(
            text=f"❌ Annulla #{t.id}",
            callback_data=SchedCb(action="del", item_id=t.id).pack(),
        )
    b.adjust(1)
    await message.reply("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(SchedCb.filter(F.action == "del"))
async def cb_sched_del(callback: CallbackQuery, callback_data: SchedCb, db_session) -> None:
    task_id = callback_data.item_id
    if task_id is None:
        await callback.answer()
        return
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


async def _confirm(message: Message, task, action: str = _ACTION_START) -> None:
    et = event_types.get(task.task_type)
    label = et.hub_label if et else task.task_type
    when = schedule_service.to_local(task.run_at).strftime("%d/%m/%Y %H:%M")
    what = _ACTIONS.get(action, _ACTIONS[_ACTION_START])[0]
    await message.answer(
        f"✅ <b>Programmato!</b>\n\n"
        f"{label} · #{task.id} · {what}\n"
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


async def _run_due_task(bot, session, task) -> None:
    """Execute one due task and persist its outcome, isolating failures.

    On any error the session is **rolled back before** the task is marked failed,
    so a poisoned transaction (e.g. a partial flush that raised) can neither
    strand the task as ``pending`` — which would make the loop retry it forever —
    nor commit half-done work. The id/type are captured up front because reading
    them off the ORM object after a rollback would trigger an implicit reload
    (illegal in async). Each task is its own unit: a failure never bleeds into the
    next one in the same tick.
    """
    task_id = getattr(task, "id", "?")
    task_type = getattr(task, "task_type", "?")
    notice: str | None = None
    try:
        try:
            await execute_task(bot, session, task)
            await schedule_service.mark_done(session, task)
        except schedule_service.TaskSkip as e:
            # Not an error: the task was an intentional no-op (e.g. quiz already running).
            await session.rollback()
            log.info("Task #%s saltato: %s", task_id, e)
            await schedule_service.mark_done(session, task)
            notice = f"ℹ️ Task #{task_id} ({task_type}) saltato: {e}"
        except Exception as e:  # noqa: BLE001
            await session.rollback()
            log.exception("Task #%s fallito: %s", task_id, e)
            await schedule_service.mark_failed(session, task, str(e))
            notice = f"⚠️ Task #{task_id} ({task_type}) fallito: {e}"
        await session.commit()
    except Exception:  # noqa: BLE001 — persisting the outcome failed; retry next tick
        log.exception("Task #%s: persistenza esito fallita", task_id)
        await session.rollback()
        return
    if notice:
        await _notify_creator(bot, task, notice)


async def scheduler_loop(bot) -> None:
    """Background loop: execute due ScheduledTasks. Started in main()."""
    log.info("Scheduler avviato (intervallo %ss).", settings.scheduler_poll_interval)
    while True:
        try:
            async with async_session_maker() as session:
                tasks = await schedule_service.due_tasks(session, schedule_service.utcnow())
                for task in tasks:
                    await _run_due_task(bot, session, task)
        except Exception:  # noqa: BLE001 — loop must never die
            log.exception("Scheduler loop error")
        await asyncio.sleep(settings.scheduler_poll_interval)
