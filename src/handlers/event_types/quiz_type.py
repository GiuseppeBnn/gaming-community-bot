"""Quiz event type — delegates to the existing quiz handler/service.

The quiz creation FSM and the open/close flows already live in ``handlers.quiz``;
this spec is a thin adapter that plugs them into the event-type registry. Handler
functions are imported lazily inside methods to avoid an import cycle
(``handlers.quiz`` → routers → … → this module).
"""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask
from services import quiz_service
from utils.text import esc

from .base import StartResult, edit_or_send


class QuizType:
    key = "quiz"
    hub_label = "🧠 Quiz"
    create_label = "➕ Crea quiz"

    async def render_list(self, message: Message, db_session: AsyncSession) -> None:
        quizzes = await quiz_service.list_ready(db_session)  # ready + running
        b = InlineKeyboardBuilder()
        lines = ["🧠 <b>Quiz</b>\n"]
        for q in quizzes:
            if q.status == "running":
                lines.append(f"🟢 #{q.id} {esc(q.title)} — <i>in corso</i>")
                b.button(text=f"🏁 Chiudi #{q.id}", callback_data=f"ev:close:quiz:{q.id}")
            else:
                lines.append(f"🟡 #{q.id} {esc(q.title)} — <i>pronto</i>")
                b.button(text=f"⚙️ #{q.id} {q.title[:22]}", callback_data=f"ev:item:quiz:{q.id}")
        if not quizzes:
            lines.append("<i>Nessun quiz. Creane uno.</i>")
        b.button(text="➕ Crea quiz", callback_data="ev:new:quiz")
        b.button(text="⬅️ Eventi", callback_data="ev:home")
        b.adjust(1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def schedulable_items(self, db_session: AsyncSession) -> list[tuple[int, str]]:
        return [
            (q.id, q.title)
            for q in await quiz_service.list_ready(db_session)
            if q.status == "ready"
        ]

    async def start_creation(
        self, message: Message, state: FSMContext, creator_id: int
    ) -> None:
        from handlers.quiz import start_quiz_creation

        await start_quiz_creation(message, state, creator_id=creator_id)

    async def start_now(self, bot, db_session: AsyncSession, item_id: int) -> StartResult:
        from handlers.quiz import open_quiz

        ok, msg = await open_quiz(bot, db_session, item_id)
        return StartResult(ok, msg, alert=not ok)

    async def execute_scheduled(
        self, bot, session: AsyncSession, task: ScheduledTask, group_id: int
    ) -> None:
        from handlers.quiz import open_quiz
        from services.schedule_service import TaskSkip

        # Already running (e.g. an admin started it by hand before the scheduled
        # time) → skip, don't fail: it's the intended end state anyway.
        quiz = await quiz_service.get_quiz(session, task.ref_id)
        if quiz is not None and quiz.status == "running":
            raise TaskSkip("il quiz era già in corso, avvio programmato saltato.")

        ok, msg = await open_quiz(bot, session, task.ref_id)
        if not ok:
            raise RuntimeError(msg)

    async def close_now(
        self, bot, db_session: AsyncSession, item_id: int
    ) -> StartResult | None:
        from handlers.quiz import close_quiz

        ok, msg = await close_quiz(bot, db_session, item_id)
        return StartResult(ok, "🏁 Quiz chiuso. Podio pubblicato." if ok else msg, alert=not ok)
