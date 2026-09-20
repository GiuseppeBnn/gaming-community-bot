"""Bet event type — pre-created betting events activated in the group.

``_announce_open`` consolidates the "nuova scommessa aperta" announcement that was
previously duplicated in ``handlers.events`` (start-now) and
``handlers.schedule`` (scheduled). It is best-effort: a failed announcement never
fails the activation (the bet is open regardless).
"""

from __future__ import annotations

import logging

from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BettingEvent, ScheduledTask
from handlers.callbacks import AdminCb, EventCb
from services import bet_service, group_registry, schedule_service
from services.public_event import PublicEvent
from utils.text import esc

from .base import StartResult, edit_or_send

log = logging.getLogger(__name__)


class BetType:
    key = "bet"
    hub_label = "🎲 Scommessa"
    create_label = "➕ Crea scommessa"

    async def discover_open(self, db_session: AsyncSession) -> list[PublicEvent]:
        return [
            PublicEvent(
                key=self.key, item_id=event.id, title=event.title,
                summary=f"{len(event.options)} opzioni · puntate aperte",
                emoji="🎲", deep_link_payload=f"bet_{event.id}",
            )
            for event in await bet_service.get_open_events(db_session)
        ]

    async def describe_scheduled(
        self, db_session: AsyncSession, item_id: int
    ) -> PublicEvent | None:
        event = await bet_service.get_event_detail(db_session, item_id)
        if event is None or event.status != "draft":
            return None
        return PublicEvent(
            key=self.key, item_id=event.id, title=event.title,
            summary=f"{len(event.options)} opzioni", emoji="🎲",
            deep_link_payload=f"bet_{event.id}",
        )

    async def render_list(self, message: Message, db_session: AsyncSession) -> None:
        drafts = await bet_service.list_drafts(db_session)
        b = InlineKeyboardBuilder()
        lines = ["🎲 <b>Scommesse in bozza</b>\n"]
        for e in drafts:
            lines.append(f"#{e.id} {esc(e.title)}")
            b.button(text=f"⚙️ #{e.id} {e.title[:22]}",
                     callback_data=EventCb(action="item", task_type="bet", item_id=e.id).pack())
        if not drafts:
            lines.append("<i>Nessuna bozza. Creane una.</i>")
        b.button(text="➕ Crea scommessa", callback_data=EventCb(action="new", task_type="bet").pack())
        b.button(text="🛠️ Scommesse attive", callback_data=AdminCb(action="bets").pack())
        b.button(text="⬅️ Eventi", callback_data=EventCb(action="home").pack())
        b.adjust(1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def schedulable_items(self, db_session: AsyncSession) -> list[tuple[int, str]]:
        return [(e.id, e.title) for e in await bet_service.list_drafts(db_session)]

    async def start_creation(
        self, message: Message, state: FSMContext, creator_id: int
    ) -> None:
        from handlers.betting import start_bet_creation

        await start_bet_creation(message, state, as_draft=True)

    async def start_now(self, bot, db_session: AsyncSession, item_id: int) -> StartResult:
        try:
            event = await bet_service.activate_event(db_session, item_id)
        except Exception as e:  # noqa: BLE001 — surface the reason to the admin
            return StartResult(False, f"⚠️ {e}", alert=True)
        # activate_event armed event.closes_at from the window; schedule its auto-lock.
        await bet_service.schedule_close(
            db_session, event, event.creator_tg_id, group_registry.get_group_id() or None
        )
        await self._announce_open(bot, db_session, event)
        return StartResult(True, "🎲 Scommessa avviata nel gruppo!")

    async def execute_scheduled(
        self, bot, session: AsyncSession, task: ScheduledTask, group_id: int
    ) -> None:
        payload = schedule_service.task_payload(task)
        # Auto-lock task (payload action=lock): close the betting window, don't open.
        if payload.get("action") == "lock":
            await self._auto_lock(bot, session, task)
            return

        # Open path — New model: ref_id → activate a pre-created draft event. Legacy: payload.
        if task.ref_id:
            event = await bet_service.activate_event(session, task.ref_id)
            description = event.description
        else:
            event = await bet_service.create_event(
                session,
                creator_tg_id=task.created_by_tg_id,
                title=payload["title"],
                description=payload.get("description", ""),
                options=[{"label": o} for o in payload["options"]],
            )
            description = payload.get("description", "")
        await session.flush()
        # activate_event/create_event armed event.closes_at from the window; schedule
        # the follow-up auto-lock (picked up on a later scheduler tick).
        await bet_service.schedule_close(session, event, task.created_by_tg_id, group_id)
        await self._announce_open(bot, session, event, description)

    async def _auto_lock(self, bot, session: AsyncSession, task: ScheduledTask) -> None:
        """Execute a due auto-lock: close the betting window (→ ``locked``). If the
        bet is no longer open (admin locked/resolved/cancelled it first) it's a no-op
        skip, not a failure."""
        event = await bet_service.get_event_detail(session, task.ref_id)
        if event is None:
            raise RuntimeError(f"Scommessa #{task.ref_id} non trovata.")
        if event.status != "open":
            raise schedule_service.TaskSkip(
                "la scommessa non era più aperta, chiusura automatica saltata."
            )
        await bet_service.lock_event(session, event.id)
        await self._announce_closed(bot, session, event)

    async def close_now(
        self, bot, db_session: AsyncSession, item_id: int
    ) -> StartResult | None:
        return None

    def _deadline_line(self, event: BettingEvent) -> str:
        if event.closes_at is None:
            return "♾️ Puntate aperte senza scadenza."
        when = schedule_service.to_local(event.closes_at).strftime("%d/%m %H:%M")
        return f"⏳ <b>Chiude alle {when}</b> — poi le puntate si bloccano."

    async def _announce_open(
        self, bot, session: AsyncSession, event: BettingEvent, description: str | None = None
    ) -> None:
        """Best-effort group announcement of a freshly opened bet (single source
        for both the start-now and scheduled paths)."""
        if description is None:
            description = event.description
        if group_registry.get_group_id() == 0:
            return
        bot_info = await bot.get_me()
        try:
            await group_registry.send_group_message(
                bot,
                session,
                f"🎲 <b>Nuova scommessa aperta!</b>\n\n"
                f"<b>{esc(event.title)}</b>\n{esc(description)}\n\n"
                f"{self._deadline_line(event)}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🎯 Scommetti",
                        url=f"https://t.me/{bot_info.username}?start=bet_{event.id}",
                    )
                ]]),
            )
        except Exception:  # noqa: BLE001
            log.warning("Annuncio scommessa #%s fallito", event.id)

    async def _announce_closed(
        self, bot, session: AsyncSession, event: BettingEvent
    ) -> None:
        """Best-effort group notice that the betting window has closed."""
        if group_registry.get_group_id() == 0:
            return
        try:
            await group_registry.send_group_message(
                bot,
                session,
                f"⏰ <b>Scommesse chiuse!</b>\n\n"
                f"<b>{esc(event.title)}</b>\n"
                f"Le puntate sono terminate. In attesa del risultato…",
            )
        except Exception:  # noqa: BLE001
            log.warning("Annuncio chiusura scommessa #%s fallito", event.id)
