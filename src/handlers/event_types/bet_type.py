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
from services import bet_service, group_registry, schedule_service
from utils.text import esc

from .base import StartResult, edit_or_send

log = logging.getLogger(__name__)


class BetType:
    key = "bet"
    hub_label = "🎲 Scommessa"
    create_label = "➕ Crea scommessa"

    async def render_list(self, message: Message, db_session: AsyncSession) -> None:
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
        await self._announce_open(bot, db_session, event)
        return StartResult(True, "🎲 Scommessa avviata nel gruppo!")

    async def execute_scheduled(
        self, bot, session: AsyncSession, task: ScheduledTask, group_id: int
    ) -> None:
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
        await self._announce_open(bot, session, event, description)

    async def close_now(
        self, bot, db_session: AsyncSession, item_id: int
    ) -> StartResult | None:
        return None

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
                f"<b>{esc(event.title)}</b>\n{esc(description)}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🎯 Scommetti",
                        url=f"https://t.me/{bot_info.username}?start=bet_{event.id}",
                    )
                ]]),
            )
        except Exception:  # noqa: BLE001
            log.warning("Annuncio scommessa #%s fallito", event.id)
