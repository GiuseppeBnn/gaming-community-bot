"""Event-registry adapter for the asynchronous narrative raid."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask
from handlers.callbacks import EventCb
from services import group_registry, raid_service, schedule_service
from services.public_event import PublicEvent
from utils.text import esc

from .base import StartResult, edit_or_send

_STATUS = {
    "running": ("🟢", "in corso"),
    "ready": ("🟡", "pronto"),
    "finished": ("🏁", "concluso"),
}


class RaidType:
    key = raid_service.GAME_TYPE
    hub_label = "🐉 Raid narrativo"
    create_label = "➕ Crea raid"
    # Only opening is user-schedulable. Phase deadlines are internal durable
    # tasks, while manual termination remains available on the detail screen.
    closable = False

    async def discover_open(self, db_session: AsyncSession) -> list[PublicEvent]:
        return [PublicEvent(
            key=self.key, item_id=row.id, title=row.title,
            summary="raid asincrono · entra anche a partita iniziata", emoji="🐉",
        ) for row in await raid_service.list_manageable(db_session) if row.status == "running"]

    async def describe_scheduled(
        self, db_session: AsyncSession, item_id: int,
    ) -> PublicEvent | None:
        snapshot = await raid_service.get_snapshot(db_session, item_id)
        if snapshot is None or snapshot.session.status != "ready":
            return None
        return PublicEvent(
            key=self.key, item_id=item_id, title=snapshot.session.title,
            summary="raid narrativo · 3 fasi asincrone", emoji="🐉",
        )

    async def render_list(self, message: Message, db_session: AsyncSession) -> None:
        rows = await raid_service.list_manageable(db_session)
        b = InlineKeyboardBuilder()
        lines = ["🐉 <b>Raid narrativi</b>\n"]
        for row in rows:
            dot, label = _STATUS.get(row.status, ("•", row.status))
            lines.append(f"{dot} #{row.id} {esc(row.title)} — <i>{label}</i>")
            b.button(
                text=f"{dot} #{row.id} {row.title[:22]}",
                callback_data=EventCb(
                    action="item", task_type=self.key, item_id=row.id,
                ).pack(),
            )
        if not rows:
            lines.append("<i>Nessun raid. Creane uno.</i>")
        b.button(
            text=self.create_label,
            callback_data=EventCb(action="new", task_type=self.key).pack(),
        )
        b.button(text="⬅️ Eventi", callback_data=EventCb(action="home").pack())
        b.adjust(1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def render_detail(
        self, message: Message, db_session: AsyncSession, item_id: int,
    ) -> None:
        snapshot = await raid_service.get_snapshot(db_session, item_id)
        if snapshot is None:
            await edit_or_send(message, "⚠️ Raid non trovato.")
            return
        root, game = snapshot.session, snapshot.game
        dot, label = _STATUS.get(root.status, ("•", root.status))
        lines = [
            f"{dot} <b>{esc(root.title)}</b> — <i>{label}</i>",
            f"\n👹 Boss: <b>{esc(snapshot.blueprint.boss_name)}</b>",
            f"❤️ {game.boss_hp}/{raid_service.MAX_HP} HP",
        ]
        if root.status == "running":
            lines.extend([
                f"🎬 Fase {game.current_phase}/{raid_service.MAX_PHASES}",
                f"👥 Scelte ricevute in questa fase: {snapshot.current_participants}",
            ])
        b = InlineKeyboardBuilder()
        if root.status == "ready":
            b.button(
                text="▶️ Avvia ora",
                callback_data=EventCb(
                    action="askstart", task_type=self.key, item_id=item_id,
                ).pack(),
            )
            b.button(
                text="🗓️ Programma",
                callback_data=EventCb(
                    action="sched", task_type=self.key, item_id=item_id,
                ).pack(),
            )
            b.button(
                text="🗑️ Elimina",
                callback_data=EventCb(
                    action="askdel", task_type=self.key, item_id=item_id,
                ).pack(),
            )
        elif root.status == "running":
            b.button(
                text="⚡ Risolvi fase ora",
                callback_data=EventCb(
                    action="askadvance", task_type=self.key, item_id=item_id,
                ).pack(),
            )
            b.button(
                text="🏳️ Termina raid",
                callback_data=EventCb(
                    action="askclose", task_type=self.key, item_id=item_id,
                ).pack(),
            )
        else:
            b.button(
                text="🗑️ Elimina",
                callback_data=EventCb(
                    action="askdel", task_type=self.key, item_id=item_id,
                ).pack(),
            )
        b.button(
            text="⬅️ Indietro",
            callback_data=EventCb(action="list", task_type=self.key).pack(),
        )
        b.adjust(1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def delete(self, db_session: AsyncSession, item_id: int) -> StartResult:
        ok = await raid_service.delete_raid(db_session, item_id)
        return StartResult(
            ok, "🗑️ Raid eliminato." if ok else "Raid non eliminabile.", alert=not ok,
        )

    async def schedulable_items(self, db_session: AsyncSession) -> list[tuple[int, str]]:
        return [(row.id, row.title) for row in await raid_service.list_ready(db_session)]

    async def start_creation(
        self, message: Message, state: FSMContext, creator_id: int,
    ) -> None:
        from handlers.raid import start_creation
        await start_creation(message, state, creator_id)

    async def _open(self, bot, session: AsyncSession, item_id: int) -> StartResult:
        group_id = group_registry.get_group_id()
        snapshot = await raid_service.get_snapshot(session, item_id)
        if not group_id:
            return StartResult(False, "GROUP_ID non configurato.", alert=True)
        if snapshot is None or snapshot.session.status != "ready":
            return StartResult(False, "Raid non disponibile.", alert=True)
        from handlers.raid import refresh_group_card, render_card
        text, keyboard = render_card(snapshot, open_preview=True)
        try:
            sent = await group_registry.send_group_message(
                bot, session, text, reply_markup=keyboard,
            )
        except Exception:  # noqa: BLE001
            return StartResult(False, "Impossibile annunciare il raid nel gruppo.", alert=True)
        if not await raid_service.start(
            session, item_id, group_id=group_id, anchor_message_id=sent.message_id,
        ):
            try:
                await sent.delete()
            except Exception:  # noqa: BLE001
                pass
            return StartResult(
                False,
                "Un raid è già in corso o questo non è più disponibile.",
                alert=True,
            )
        running = await raid_service.get_snapshot(session, item_id)
        if running is not None:
            if not await refresh_group_card(bot, session, running):
                await raid_service.schedule_card_refresh(session, running.session)
        return StartResult(True, "🐉 Raid avviato nel gruppo!")

    async def start_now(self, bot, db_session: AsyncSession, item_id: int) -> StartResult:
        return await self._open(bot, db_session, item_id)

    async def execute_scheduled(
        self, bot, session: AsyncSession, task: ScheduledTask, group_id: int,
    ) -> None:
        if task.ref_id is None:
            raise RuntimeError("Raid programmato senza ref_id.")
        payload = schedule_service.task_payload(task)
        if payload.get("action") == "refresh":
            snapshot = await raid_service.get_snapshot(session, task.ref_id)
            if snapshot is None:
                raise schedule_service.TaskSkip("raid non più disponibile")
            from handlers.raid import refresh_group_card
            if await refresh_group_card(bot, session, snapshot):
                return
            attempt = payload.get("attempt", 1)
            attempt = attempt if isinstance(attempt, int) and attempt >= 1 else 1
            if attempt >= 2:
                raise RuntimeError("Card raid non aggiornabile dopo 3 tentativi.")
            await raid_service.schedule_card_refresh(
                session, snapshot.session, attempt=attempt + 1,
            )
            return
        if payload.get("action") == "phase":
            phase = payload.get("phase")
            if not isinstance(phase, int):
                raise RuntimeError("Task fase raid non valido.")
            advance_result = await raid_service.advance_phase(
                session, task.ref_id, expected_phase=phase,
            )
            if advance_result.snapshot is not None:
                from handlers.raid import refresh_group_card
                if not await refresh_group_card(bot, session, advance_result.snapshot):
                    await raid_service.schedule_card_refresh(
                        session, advance_result.snapshot.session,
                    )
            return
        open_result = await self._open(bot, session, task.ref_id)
        if not open_result.ok:
            raise RuntimeError(open_result.message)

    async def advance_now(
        self, bot, db_session: AsyncSession, item_id: int,
    ) -> StartResult:
        result = await raid_service.advance_phase(db_session, item_id, manual=True)
        if result.ok and result.snapshot is not None:
            from handlers.raid import refresh_group_card
            if not await refresh_group_card(bot, db_session, result.snapshot):
                await raid_service.schedule_card_refresh(
                    db_session, result.snapshot.session,
                )
        return StartResult(result.ok, result.message, alert=not result.ok)

    async def close_now(
        self, bot, db_session: AsyncSession, item_id: int,
    ) -> StartResult | None:
        if not await raid_service.close(db_session, item_id):
            return StartResult(False, "Il raid non è in corso.", alert=True)
        snapshot = await raid_service.get_snapshot(db_session, item_id)
        if snapshot is not None:
            from handlers.raid import refresh_group_card
            if not await refresh_group_card(bot, db_session, snapshot):
                await raid_service.schedule_card_refresh(db_session, snapshot.session)
        return StartResult(True, "🏳️ Raid terminato senza penalità.")
