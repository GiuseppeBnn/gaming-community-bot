"""Registry-only integration: the hub/scheduler need no telephone branches."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import ScheduledTask
from handlers.callbacks import EventCb, TelephoneCb
from services import admin_service, group_registry, schedule_service, telephone_service as game
from services.public_event import PublicEvent

from .base import StartResult, edit_or_send


class TelephoneType:
    key = game.GAME_TYPE
    hub_label = "📞 Telefono senza fili"
    create_label = "➕ Crea staffetta"
    closable = True

    def _hook(self, bot, session, game_id):
        async def publish():
            from handlers.telephone import publish_card

            await publish_card(bot, session, game_id)

        return publish

    async def render_list(self, message: Message, db_session: AsyncSession) -> None:
        rows = await game.list_games(db_session)
        await db_session.rollback()
        b = InlineKeyboardBuilder()
        labels = {"ready": "pronta", "running": "in corso", "finished": "conclusa"}
        for game_id, status in rows:
            b.button(
                text=f"📞 #{game_id} · {labels.get(status, status)}",
                callback_data=EventCb(action="item", task_type=self.key, item_id=game_id).pack(),
            )
        if settings.telephone_enabled:
            b.button(
                text=self.create_label,
                callback_data=EventCb(action="new", task_type=self.key).pack(),
            )
        b.button(text="⬅️ Eventi", callback_data=EventCb(action="home").pack())
        b.adjust(1)
        await edit_or_send(
            message,
            "📞 <b>Telefono senza fili</b>\n4 immagini · 4 persone · 30 minuti.\n"
            + (
                "Scegli o crea una staffetta."
                if settings.telephone_enabled
                else "Attività non abilitata in questa istanza."
            ),
            b.as_markup(),
        )

    async def render_detail(self, message: Message, db_session: AsyncSession, item_id: int) -> None:
        view = await game.get_view(db_session, item_id)
        await db_session.rollback()
        if view is None:
            await edit_or_send(message, "Staffetta non trovata.")
            return
        b = InlineKeyboardBuilder()
        if view.status == "ready":
            for action, label in (("askstart", "▶️ Avvia ora"), ("sched", "🗓️ Programma")):
                b.button(
                    text=label,
                    callback_data=EventCb(
                        action=action, task_type=self.key, item_id=item_id
                    ).pack(),
                )
        else:
            # Idempotent refresh: also recovers a failed post-commit reveal.
            b.button(
                text="🔄 Aggiorna card / risultato",
                callback_data=EventCb(action="start", task_type=self.key, item_id=item_id).pack(),
            )
            if view.status == "running":
                b.button(
                    text="🏁 Chiudi e svela",
                    callback_data=EventCb(
                        action="askclose", task_type=self.key, item_id=item_id
                    ).pack(),
                )
        b.button(
            text="⬅️ Staffette", callback_data=EventCb(action="list", task_type=self.key).pack()
        )
        b.adjust(1)
        warning = (
            "\n🎨 Generazione in corso: chiudere adesso può scartare un'immagine già pagata."
            if view.kind == "generating"
            else ""
        )
        await edit_or_send(
            message,
            f"📞 <b>Telefono senza fili #{item_id}</b>\nImmagini raccolte: {len(view.stages)}/4.\nUn partecipante per passaggio; massimo 150 caratteri, 2 minuti per scrivere.\nNessun premio in CoInn/XP.{warning}",
            b.as_markup(),
        )

    async def schedulable_items(self, db_session: AsyncSession) -> list[tuple[int, str]]:
        return [
            (i, f"Telefono senza fili #{i}")
            for i, status in await game.list_games(db_session)
            if status == "ready"
        ]

    async def start_creation(self, message: Message, state: FSMContext, creator_id: int) -> None:
        await state.clear()
        b = InlineKeyboardBuilder()
        b.button(
            text="✅ Prepara staffetta",
            callback_data=TelephoneCb(action="create", game_id=0).pack(),
        )
        await edit_or_send(
            message,
            "📞 Una catena di 4 immagini, senza audio. I passaggi sono privati, il risultato pubblico.\nLa creazione non genera immagini né spende crediti; ogni immagine userà il budget AI esistente.",
            b.as_markup(),
        )

    async def start_now(self, bot, db_session: AsyncSession, item_id: int) -> StartResult:
        view = await game.get_view(db_session, item_id)
        if view is not None and view.status == "finished":
            return StartResult(
                True, "Risultato aggiornato.", post_commit=self._hook(bot, db_session, item_id)
            )
        ok = await game.start(db_session, item_id, group_registry.get_group_id())
        return StartResult(
            ok,
            "Staffetta avviata."
            if ok
            else "Staffetta non avviabile: controlla abilitazione, gruppo e altre staffette attive.",
            alert=not ok,
            post_commit=self._hook(bot, db_session, item_id) if ok else None,
            audit_action="telephone_start",
            audit_detail=f"game={item_id}",
        )

    async def close_now(self, bot, db_session: AsyncSession, item_id: int) -> StartResult:
        ok = await game.finish(db_session, item_id, "admin_closed")
        return StartResult(
            ok,
            "Staffetta chiusa." if ok else "Staffetta non in corso.",
            alert=not ok,
            post_commit=self._hook(bot, db_session, item_id) if ok else None,
            audit_action="telephone_close",
            audit_detail=f"game={item_id}",
        )

    async def execute_scheduled(
        self, bot, session: AsyncSession, task: ScheduledTask, group_id: int
    ):
        if task.ref_id is None:
            raise ValueError("Missing telephone reference")
        action = schedule_service.task_payload(task).get("action", "start")
        if action == "expire":
            await game.tick(session, task.ref_id)
            # Always refresh: a previous publisher may have failed after commit.
            return self._hook(bot, session, task.ref_id)
        if action not in ("start", "close"):
            raise ValueError("Unknown telephone action")
        ok = (
            await game.start(session, task.ref_id, group_id)
            if action == "start"
            else await game.finish(session, task.ref_id, "admin_closed")
        )
        if not ok:
            raise schedule_service.TaskSkip
        await admin_service.log_action(
            session,
            task.created_by_tg_id,
            f"telephone_{action}",
            detail=f"game={task.ref_id}; scheduled={task.id}",
        )
        return self._hook(bot, session, task.ref_id)

    async def discover_open(self, db_session: AsyncSession) -> list[PublicEvent]:
        result = []
        for game_id, status in await game.list_games(db_session):
            if status == "running":
                view = await game.get_view(db_session, game_id)
                if view is not None and view.expires_at > schedule_service.utcnow():
                    result.append(
                        PublicEvent(
                            self.key,
                            game_id,
                            "Telefono senza fili",
                            "Staffetta privata, finale pubblico",
                            "📞",
                            deep_link_payload=f"telefono_{game_id}",
                            ends_at=view.expires_at,
                        )
                    )
        return result

    async def describe_scheduled(
        self, db_session: AsyncSession, item_id: int
    ) -> PublicEvent | None:
        view = await game.get_view(db_session, item_id)
        if view is None or view.status != "ready":
            return None
        return PublicEvent(
            self.key,
            item_id,
            "Telefono senza fili",
            "Staffetta di immagini",
            "📞",
            deep_link_payload=f"telefono_{item_id}",
        )
