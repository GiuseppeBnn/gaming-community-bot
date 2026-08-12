"""Poll event type — pre-created native Telegram polls.

``start_now`` / ``execute_scheduled`` consolidate the poll-publishing logic that
was previously duplicated between the events hub and the scheduler executor. The
creation FSM stays in ``handlers.events`` (it keeps the proven router-level admin
gate, STEERING §8); this spec delegates to it via a lazy import.
"""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask
from handlers.callbacks import EventCb
from services import group_registry, poll_service, schedule_service
from services.public_event import PublicEvent
from utils.text import esc

from .base import StartResult, edit_or_send


class PollType:
    key = "poll"
    hub_label = "📊 Sondaggio"
    create_label = "➕ Crea sondaggio"

    async def describe_scheduled(
        self, db_session: AsyncSession, item_id: int
    ) -> PublicEvent | None:
        poll = await poll_service.get(db_session, item_id)
        if poll is None or poll.status != "ready":
            return None
        return PublicEvent(
            key=self.key, item_id=poll.id, title=poll.question,
            summary=f"{len(poll_service.options_of(poll))} opzioni", emoji="📊",
        )

    async def render_list(self, message: Message, db_session: AsyncSession) -> None:
        polls = await poll_service.list_ready(db_session)
        b = InlineKeyboardBuilder()
        lines = ["📊 <b>Sondaggi pronti</b>\n"]
        for p in polls:
            lines.append(f"#{p.id} {esc(p.question)}")
            b.button(text=f"⚙️ #{p.id} {p.question[:22]}",
                     callback_data=EventCb(action="item", task_type="poll", item_id=p.id).pack())
        if not polls:
            lines.append("<i>Nessun sondaggio pronto. Creane uno.</i>")
        b.button(text="➕ Crea sondaggio", callback_data=EventCb(action="new", task_type="poll").pack())
        b.button(text="⬅️ Eventi", callback_data=EventCb(action="home").pack())
        b.adjust(1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def schedulable_items(self, db_session: AsyncSession) -> list[tuple[int, str]]:
        return [(p.id, p.question) for p in await poll_service.list_ready(db_session)]

    async def start_creation(
        self, message: Message, state: FSMContext, creator_id: int
    ) -> None:
        from handlers.events import start_poll_creation

        await start_poll_creation(message, state)

    async def start_now(self, bot, db_session: AsyncSession, item_id: int) -> StartResult:
        poll = await poll_service.get(db_session, item_id)
        if poll is None or poll.status != "ready":
            return StartResult(False, "Sondaggio non disponibile.", alert=True)
        group_id = group_registry.get_group_id()
        if not group_id:
            return StartResult(False, "GROUP_ID non configurato.", alert=True)
        await bot.send_poll(
            chat_id=group_id,
            question=poll.question,
            options=poll_service.options_of(poll),
            is_anonymous=False,
        )
        await poll_service.mark_used(db_session, item_id)
        return StartResult(True, "📊 Sondaggio pubblicato nel gruppo!")

    async def execute_scheduled(
        self, bot, session: AsyncSession, task: ScheduledTask, group_id: int
    ) -> None:
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
            chat_id=group_id, question=question, options=options, is_anonymous=False
        )

    async def close_now(
        self, bot, db_session: AsyncSession, item_id: int
    ) -> StartResult | None:
        return None
