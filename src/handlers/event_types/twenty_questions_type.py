"""Event registry adapter for Alduino's collaborative twenty questions."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask
from handlers.callbacks import EventCb
from services import ai_game_service, group_registry, schedule_service
from services.ai_game_types import FinishReason
from services.public_event import PublicEvent
from utils.text import esc

from .base import PostCommitHook, StartResult, edit_or_send

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
        usage = (
            f"❓ {game.questions_used} domande · 🎯 {game.guesses_used} tentativi"
            if game.rules_version == 2 else
            f"❓ {game.questions_used}/{game.question_limit} · "
            f"🎯 {game.guesses_used}/{game.guess_limit}"
        )
        lines = [
            f"{dot} <b>{esc(root.title)}</b> — <i>{label}</i>",
            f"\n🔐 Segreto admin: <b>{esc(game.answer)}</b>",
            usage,
        ]
        b = InlineKeyboardBuilder()
        if root.status == "ready":
            b.button(text="▶️ Avvia ora", callback_data=EventCb(action="askstart", task_type=self.key, item_id=item_id).pack())
            b.button(text="🗓️ Programma", callback_data=EventCb(action="sched", task_type=self.key, item_id=item_id).pack())
            b.button(text="🗑️ Elimina definitivamente", callback_data=EventCb(action="askdel", task_type=self.key, item_id=item_id).pack())
        elif root.status == "running":
            b.button(text="🏁 Chiudi", callback_data=EventCb(action="askclose", task_type=self.key, item_id=item_id).pack())
        elif game.rules_version == 2:
            lines.append(
                "\n<i>Archiviare la partita la nasconde: non la elimina definitivamente.</i>"
            )
            b.button(text="🗃️ Archivia / nascondi", callback_data=EventCb(action="askarchive", task_type=self.key, item_id=item_id).pack())
        else:
            b.button(text="🗑️ Elimina definitivamente", callback_data=EventCb(action="askdel", task_type=self.key, item_id=item_id).pack())
        b.button(text="⬅️ Indietro", callback_data=EventCb(action="list", task_type=self.key).pack())
        b.adjust(2, 1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def delete(self, db_session: AsyncSession, item_id: int) -> StartResult:
        ok = await ai_game_service.delete_game(db_session, item_id)
        return StartResult(ok, "🗑️ Partita eliminata." if ok else "Partita non eliminabile.", alert=not ok)

    async def archive(self, db_session: AsyncSession, item_id: int) -> StartResult:
        ok = await ai_game_service.archive_game(db_session, item_id)
        return StartResult(
            ok,
            "🗃️ Partita archiviata." if ok else "Partita non archiviabile.",
            alert=not ok,
        )

    async def schedulable_items(self, db_session: AsyncSession) -> list[tuple[int, str]]:
        return [(row.id, row.title) for row in await ai_game_service.list_ready(db_session)]

    async def start_creation(self, message: Message, state: FSMContext, creator_id: int) -> None:
        from handlers.twenty_questions import start_creation
        await start_creation(message, state, creator_id)

    def _start_hook(self, bot, session: AsyncSession, item_id: int) -> PostCommitHook:
        """Re-read the durable v2 view before publishing its initial/recovery card."""
        async def publish() -> None:
            try:
                view = await ai_game_service.get_game_view(session, item_id)
                # The read itself opens a transaction. Close it before Telegram.
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            if view is None or view.status != "running":
                raise RuntimeError(f"20 domande #{item_id} non piu' pubblicabile")
            from handlers.twenty_questions import refresh_group_card
            await refresh_group_card(bot, session, view)
        return publish

    def _terminal_hook(self, bot, session: AsyncSession, result) -> PostCommitHook:
        async def publish() -> None:
            from handlers.twenty_questions import publish_terminal
            await publish_terminal(bot, session, result)
        return publish

    def _legacy_refresh_hook(self, bot, session: AsyncSession, snapshot) -> PostCommitHook:
        """Keep the v1 publisher's anchor fallback in a second, committed transaction."""
        async def publish() -> None:
            try:
                from handlers.twenty_questions import refresh_group_card
                await refresh_group_card(bot, session, snapshot)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return publish

    async def _open(
        self,
        bot,
        session: AsyncSession,
        item_id: int,
        *,
        group_id: int | None = None,
    ) -> StartResult:
        configured_group_id = group_id if group_id is not None else group_registry.get_group_id()
        snapshot = await ai_game_service.get_snapshot(session, item_id)
        if snapshot is None:
            return StartResult(False, "Partita non disponibile.", alert=True)
        if snapshot.game.rules_version == 2:
            if snapshot.session.status == "running":
                if snapshot.session.anchor_message_id is None:
                    return StartResult(
                        True,
                        "🐲 Partita gia' avviata: ripubblico la card.",
                        post_commit=self._start_hook(bot, session, item_id),
                    )
                return StartResult(False, "La partita e' gia' stata avviata.", alert=True)
            if snapshot.session.status != "ready":
                return StartResult(False, "Partita non disponibile.", alert=True)
            if not configured_group_id:
                return StartResult(False, "GROUP_ID non configurato.", alert=True)
            started = await ai_game_service.start(
                session,
                item_id,
                group_id=configured_group_id,
            )
            if not started.started:
                if started.reason is not None and started.reason.value == "providers_unavailable":
                    message = "Provider IA non disponibili: riprova piu' tardi."
                elif started.reason is not None and started.reason.value == "absolute_expiry_elapsed":
                    message = "La scadenza della partita e' gia' trascorsa."
                else:
                    message = "Partita non disponibile."
                return StartResult(False, message, alert=True)
            return StartResult(
                True,
                "🐲 Partita avviata nel gruppo!",
                post_commit=self._start_hook(bot, session, item_id),
            )

        if not configured_group_id:
            return StartResult(False, "GROUP_ID non configurato.", alert=True)
        if snapshot.session.status != "ready":
            return StartResult(False, "Partita non disponibile.", alert=True)
        # Legacy v1 keeps its announce-first protocol, but no read transaction may
        # remain open while it reaches Telegram.
        from handlers.twenty_questions import render_card
        preview = render_card(snapshot, open_preview=True)
        await session.rollback()
        try:
            sent = await group_registry.send_group_message(
                bot, session, preview,
            )
        except Exception:  # noqa: BLE001
            return StartResult(False, "Impossibile annunciare la partita nel gruppo.", alert=True)
        if not await ai_game_service.start(
            session, item_id, group_id=configured_group_id, anchor_message_id=sent.message_id,
        ):
            await session.rollback()
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
    ) -> PostCommitHook | None:
        if task.ref_id is None:
            raise RuntimeError("Partita programmata senza ref_id.")
        action = schedule_service.task_payload(task).get("action", "start")
        if action == "start":
            result = await self._open(bot, session, task.ref_id, group_id=group_id)
            if not result.ok:
                snapshot = await ai_game_service.get_snapshot(session, task.ref_id)
                if snapshot is not None and snapshot.session.status != "ready":
                    raise schedule_service.TaskSkip(result.message)
                raise RuntimeError(result.message)
            return result.post_commit
        if action == "close":
            close_result = await self.close_now(bot, session, task.ref_id)
            if close_result is None or not close_result.ok:
                raise schedule_service.TaskSkip(
                    close_result.message if close_result is not None else "Operazione non supportata.",
                )
            return close_result.post_commit
        if action == "expire":
            snapshot = await ai_game_service.get_snapshot(session, task.ref_id)
            if (
                snapshot is None
                or snapshot.game.rules_version != 2
                or snapshot.session.status != "running"
            ):
                raise schedule_service.TaskSkip("Partita non piu' in corso.")
            terminal = await ai_game_service.terminalize(
                session,
                session_id=task.ref_id,
                reason=FinishReason.expired,
            )
            if not terminal.transitioned:
                raise schedule_service.TaskSkip("Partita non piu' in corso.")
            return self._terminal_hook(bot, session, terminal)
        raise RuntimeError(f"Azione programmata non supportata: {action}")

    async def close_now(
        self, bot, db_session: AsyncSession, item_id: int,
    ) -> StartResult | None:
        snapshot = await ai_game_service.get_snapshot(db_session, item_id)
        if snapshot is None or snapshot.session.status != "running":
            return StartResult(False, "La partita non è in corso.", alert=True)
        if snapshot.game.rules_version == 2:
            terminal = await ai_game_service.terminalize(
                db_session,
                session_id=item_id,
                reason=FinishReason.admin_closed,
            )
            if not terminal.transitioned:
                return StartResult(False, "La partita non è in corso.", alert=True)
            return StartResult(
                True,
                "🏁 Partita chiusa e risposta rivelata.",
                post_commit=self._terminal_hook(bot, db_session, terminal),
            )
        if not await ai_game_service.finish(db_session, item_id):
            return StartResult(False, "La partita non è in corso.", alert=True)
        snapshot = await ai_game_service.get_snapshot(db_session, item_id)
        if snapshot is not None:
            return StartResult(
                True,
                "🏁 Partita chiusa e risposta rivelata.",
                post_commit=self._legacy_refresh_hook(bot, db_session, snapshot),
            )
        return StartResult(True, "🏁 Partita chiusa e risposta rivelata.")
