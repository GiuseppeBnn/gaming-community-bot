"""Event registry adapter for Alduino's collaborative twenty questions."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask
from handlers.callbacks import EventCb
from services import ai_game_service, group_registry, schedule_service
from services.public_event import PublicEvent
from utils.text import esc

from .base import StartResult, edit_or_send

_STATUS = {"running": ("🟢", "in corso"), "ready": ("🟡", "pronta"), "finished": ("🏁", "conclusa")}


class TwentyQuestionsType:
    key = ai_game_service.GAME_TYPE
    hub_label = "🐲 20 Domande ad Alduino"
    create_label = "➕ Crea 20 Domande"
    closable = True

    async def discover_open(self, db_session: AsyncSession) -> list[PublicEvent]:
        return [PublicEvent(
            key=self.key, item_id=row.id, title=row.title,
            summary="20 domande · 3 tentativi · gioca nel gruppo", emoji="🐲",
        ) for row in await ai_game_service.list_manageable(db_session) if row.status == "running"]

    async def describe_scheduled(
        self, db_session: AsyncSession, item_id: int,
    ) -> PublicEvent | None:
        snapshot = await ai_game_service.get_snapshot(db_session, item_id)
        if snapshot is None or snapshot.session.status != "ready":
            return None
        return PublicEvent(
            key=self.key, item_id=item_id, title=snapshot.session.title,
            summary="20 domande · 3 tentativi", emoji="🐲",
        )

    async def render_list(self, message: Message, db_session: AsyncSession) -> None:
        rows = await ai_game_service.list_manageable(db_session)
        b = InlineKeyboardBuilder()
        lines = ["🐲 <b>20 Domande ad Alduino</b>\n"]
        for row in rows:
            dot, label = _STATUS.get(row.status, ("•", row.status))
            lines.append(f"{dot} #{row.id} {esc(row.title)} — <i>{label}</i>")
            b.button(
                text=f"{dot} #{row.id} {row.title[:22]}",
                callback_data=EventCb(action="item", task_type=self.key, item_id=row.id).pack(),
            )
        if not rows:
            lines.append("<i>Nessuna partita. Creane una.</i>")
        b.button(text=self.create_label, callback_data=EventCb(action="new", task_type=self.key).pack())
        b.button(text="⬅️ Eventi", callback_data=EventCb(action="home").pack())
        b.adjust(1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def render_detail(self, message: Message, db_session: AsyncSession, item_id: int) -> None:
        snapshot = await ai_game_service.get_snapshot(db_session, item_id)
        if snapshot is None:
            await edit_or_send(message, "⚠️ Partita non trovata.")
            return
        root, game = snapshot.session, snapshot.game
        dot, label = _STATUS.get(root.status, ("•", root.status))
        lines = [
            f"{dot} <b>{esc(root.title)}</b> — <i>{label}</i>",
            f"\n🔐 Segreto admin: <b>{esc(game.answer)}</b>",
            f"❓ {game.questions_used}/{game.question_limit} · 🎯 {game.guesses_used}/{game.guess_limit}",
        ]
        b = InlineKeyboardBuilder()
        if root.status == "ready":
            b.button(text="▶️ Avvia ora", callback_data=EventCb(action="askstart", task_type=self.key, item_id=item_id).pack())
            b.button(text="🗓️ Programma", callback_data=EventCb(action="sched", task_type=self.key, item_id=item_id).pack())
            b.button(text="🗑️ Elimina", callback_data=EventCb(action="askdel", task_type=self.key, item_id=item_id).pack())
        elif root.status == "running":
            b.button(text="🏁 Chiudi", callback_data=EventCb(action="askclose", task_type=self.key, item_id=item_id).pack())
        else:
            b.button(text="🗑️ Elimina", callback_data=EventCb(action="askdel", task_type=self.key, item_id=item_id).pack())
        b.button(text="⬅️ Indietro", callback_data=EventCb(action="list", task_type=self.key).pack())
        b.adjust(2, 1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def delete(self, db_session: AsyncSession, item_id: int) -> StartResult:
        ok = await ai_game_service.delete_game(db_session, item_id)
        return StartResult(ok, "🗑️ Partita eliminata." if ok else "Partita non eliminabile.", alert=not ok)

    async def schedulable_items(self, db_session: AsyncSession) -> list[tuple[int, str]]:
        return [(row.id, row.title) for row in await ai_game_service.list_ready(db_session)]

    async def start_creation(self, message: Message, state: FSMContext, creator_id: int) -> None:
        from handlers.twenty_questions import start_creation
        await start_creation(message, state, creator_id)

    async def _open(self, bot, session: AsyncSession, item_id: int) -> StartResult:
        group_id = group_registry.get_group_id()
        snapshot = await ai_game_service.get_snapshot(session, item_id)
        if not group_id:
            return StartResult(False, "GROUP_ID non configurato.", alert=True)
        if snapshot is None or snapshot.session.status != "ready":
            return StartResult(False, "Partita non disponibile.", alert=True)
        from handlers.twenty_questions import render_card
        try:
            sent = await group_registry.send_group_message(
                bot, session, render_card(snapshot, open_preview=True),
            )
        except Exception:  # noqa: BLE001
            return StartResult(False, "Impossibile annunciare la partita nel gruppo.", alert=True)
        if not await ai_game_service.start(
            session, item_id, group_id=group_id, anchor_message_id=sent.message_id,
        ):
            try:
                await sent.delete()
            except Exception:  # noqa: BLE001
                pass
            return StartResult(False, "La partita è già stata avviata.", alert=True)
        return StartResult(True, "🐲 Partita avviata nel gruppo!")

    async def start_now(self, bot, db_session: AsyncSession, item_id: int) -> StartResult:
        return await self._open(bot, db_session, item_id)

    async def execute_scheduled(
        self, bot, session: AsyncSession, task: ScheduledTask, group_id: int,
    ) -> None:
        if task.ref_id is None:
            raise RuntimeError("Partita programmata senza ref_id.")
        if schedule_service.task_payload(task).get("action") == "close":
            result = await self.close_now(bot, session, task.ref_id)
        else:
            result = await self._open(bot, session, task.ref_id)
        if result is None:
            raise RuntimeError("Operazione non supportata.")
        if not result.ok:
            raise RuntimeError(result.message)

    async def close_now(
        self, bot, db_session: AsyncSession, item_id: int,
    ) -> StartResult | None:
        if not await ai_game_service.finish(db_session, item_id):
            return StartResult(False, "La partita non è in corso.", alert=True)
        snapshot = await ai_game_service.get_snapshot(db_session, item_id)
        if snapshot is not None:
            from handlers.twenty_questions import refresh_group_card
            await refresh_group_card(bot, db_session, snapshot)
        return StartResult(True, "🏁 Partita chiusa e risposta rivelata.")
