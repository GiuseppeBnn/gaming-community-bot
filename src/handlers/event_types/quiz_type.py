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
from services import quiz_service, schedule_service
from utils.text import esc

from .base import StartResult, edit_or_send

# Status → (dot, human label) for the list and detail header.
_STATUS = {
    "running": ("🟢", "in corso"),
    "ready": ("🟡", "pronto"),
    "finished": ("🏁", "concluso"),
}


def _fmt_dt(dt) -> str:
    return schedule_service.to_local(dt).strftime("%d/%m %H:%M") if dt else "—"


class QuizType:
    key = "quiz"
    hub_label = "🧠 Quiz"
    create_label = "➕ Crea quiz"
    #: Its close publishes the podium, so it is worth scheduling on its own clock —
    #: `handlers.schedule` offers «avvio o chiusura?» only for types that say this.
    closable = True

    async def render_list(self, message: Message, db_session: AsyncSession) -> None:
        # Quizzes are persistent objects: show ready/running AND the recent
        # finished ones (archive) — tapping any of them opens its detail screen,
        # never starts it (STEERING §18.2, no accidental launch).
        quizzes = await quiz_service.list_manageable(db_session)
        b = InlineKeyboardBuilder()
        lines = ["🧠 <b>Quiz</b>\n"]
        for q in quizzes:
            dot, label = _STATUS.get(q.status, ("•", q.status))
            lines.append(f"{dot} #{q.id} {esc(q.title)} — <i>{label}</i>")
            b.button(text=f"{dot} #{q.id} {q.title[:22]}", callback_data=f"ev:item:quiz:{q.id}")
        if not quizzes:
            lines.append("<i>Nessun quiz. Creane uno.</i>")
        b.button(text="➕ Crea quiz", callback_data="ev:new:quiz")
        b.button(text="⬅️ Eventi", callback_data="ev:home")
        b.adjust(1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def render_detail(
        self, message: Message, db_session: AsyncSession, item_id: int
    ) -> None:
        """Info screen for a single quiz with status-aware actions. Every impactful
        action (avvia / chiudi / elimina / riproponi) routes through an ``ev:ask*``
        confirmation — no one-tap launch."""
        quiz = await quiz_service.get_quiz(db_session, item_id)
        if quiz is None:
            b = InlineKeyboardBuilder()
            b.button(text="⬅️ Indietro", callback_data="ev:list:quiz")
            await edit_or_send(message, "⚠️ Quiz non trovato (eliminato?).", b.as_markup())
            return

        dot, label = _STATUS.get(quiz.status, ("•", quiz.status))
        lines = [
            f"{dot} <b>{esc(quiz.title)}</b> — <i>{label}</i>",
        ]
        if quiz.description:
            lines.append(f"\n{esc(quiz.description)}")
        lines.append(
            f"\n📋 {len(quiz.questions)} domande · 🏆 {quiz_service.format_prize_summary(quiz)}"
        )
        if quiz.status in ("running", "finished"):
            participants, answers = await quiz_service.answer_stats(db_session, item_id)
            finishers = len(await quiz_service.podium(db_session, item_id))
            lines.append(f"👥 {participants} partecipanti · ✍️ {answers} risposte · 🏁 {finishers} finisher")
        if quiz.started_at:
            lines.append(f"▶️ Avviato: {_fmt_dt(quiz.started_at)}")
        if quiz.finished_at:
            lines.append(f"🏁 Concluso: {_fmt_dt(quiz.finished_at)}")

        b = InlineKeyboardBuilder()
        if quiz.status == "ready":
            b.button(text="▶️ Avvia ora", callback_data=f"ev:askstart:quiz:{item_id}")
            b.button(text="🗓️ Programma", callback_data=f"ev:sched:quiz:{item_id}")
            b.button(text="✏️ Modifica domande", callback_data=f"quiz_edit:nav:{item_id}:0")
            b.button(text="🧪 Prova", callback_data=f"quiz_try:start:{item_id}")
            b.button(text="🗑️ Elimina", callback_data=f"ev:askdel:quiz:{item_id}")
        elif quiz.status == "running":
            b.button(text="🏁 Chiudi", callback_data=f"ev:askclose:quiz:{item_id}")
            b.button(text="🗓️ Programma chiusura",
                     callback_data=f"ev:sched:quiz:{item_id}:close")
            b.button(text="🗑️ Elimina", callback_data=f"ev:askdel:quiz:{item_id}")
        else:  # finished
            b.button(text="🔁 Riproponi", callback_data=f"ev:askreset:quiz:{item_id}")
            b.button(text="🗑️ Elimina", callback_data=f"ev:askdel:quiz:{item_id}")
        b.button(text="⬅️ Indietro", callback_data="ev:list:quiz")
        b.adjust(2, 2, 1)  # action pairs per row, then Elimina/Indietro on their own rows
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def delete(self, db_session: AsyncSession, item_id: int) -> StartResult:
        ok = await quiz_service.delete_quiz(db_session, item_id)
        return StartResult(ok, "🗑️ Quiz eliminato." if ok else "Quiz non trovato.", alert=not ok)

    async def reset(self, db_session: AsyncSession, item_id: int) -> StartResult | None:
        ok = await quiz_service.reset_quiz(db_session, item_id)
        return StartResult(
            ok,
            "🔁 Quiz riproposto: risposte azzerate, di nuovo pronto." if ok
            else "Impossibile riproporre (solo i quiz conclusi).",
            alert=not ok,
        )

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
        from handlers.quiz import close_quiz, open_quiz
        from services.schedule_service import TaskSkip

        # A scheduled close reuses this same task_type with an action payload — the
        # pattern the guess auto-close and the betting auto-lock already use. No new
        # task type. Not closable yet (never started, closed by hand) is a skip, not
        # a failure: the end state is the one asked for either way.
        if schedule_service.task_payload(task).get("action") == "close":
            ok, msg = await close_quiz(bot, session, task.ref_id)
            if not ok:
                raise TaskSkip(msg)
            return

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
