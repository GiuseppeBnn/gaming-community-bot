"""Guess event types — one spec class, instantiated once per game.

`QuizType` is a class per game because there is one quiz. Here the two games are
the same engine with different labels, so the spec takes a ``kind`` and the
registry holds two instances. Everything that differs is read from
``handlers.guess._shared.KINDS``.

``kind`` does triple duty on purpose — event-type key, ``ScheduledTask.task_type``
and the trophy ``game_key`` — because three strings that happen to match today
would eventually not.

Handler functions are imported lazily inside methods to avoid an import cycle
(``handlers.guess`` → routers → … → this module), the same as ``quiz_type``.

Commit convention (STEERING §5): these methods mutate but never commit.
"""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask
from handlers.callbacks import EventCb, GuessAliasCb
from handlers.guess._shared import kind_of
from services import guess_service, schedule_service
from services.public_event import PublicEvent
from utils.text import esc, format_seconds_short

from .base import StartResult, edit_or_send

# Status → (dot, human label) for the list and detail header.
_STATUS = {
    "running": ("🟢", "in corso"),
    "ready": ("🟡", "pronto"),
    "finished": ("🏁", "concluso"),
}

#: How many rejected answers the detail screen shows. The admin needs enough to
#: spot a bad AI verdict, not a transcript.
_AUDIT_LIMIT = 5


def _fmt_dt(dt) -> str:
    """Both call sites already guard on the timestamp being set, so there is no
    "missing" branch here to carry — the line simply isn't rendered."""
    return schedule_service.to_local(dt).strftime("%d/%m %H:%M")


class GuessType:
    """One instance per game; ``key`` is the ``kind``."""

    #: Its close pays the podium and reveals the medium, so it is worth scheduling
    #: on its own clock — `handlers.schedule` offers «avvio o chiusura?» only for
    #: types that say this. (A round with `round_duration_seconds` already arms its
    #: own close at start; this is the admin picking a wall-clock instant instead.)
    closable = True

    def __init__(self, kind: str) -> None:
        spec = kind_of(kind)
        self.kind = kind
        self.key = spec.key
        self.hub_label = f"{spec.emoji} {spec.label}"
        self.create_label = spec.create_label

    async def discover_open(self, db_session: AsyncSession) -> list[PublicEvent]:
        rounds = await guess_service.list_manageable(db_session, self.kind)
        return [
            PublicEvent(
                key=self.key, item_id=round_.id, title=round_.title,
                summary=f"{round_.max_attempts} tentativi · gioca in privato",
                emoji=kind_of(self.kind).emoji,
                deep_link_payload=f"{self.kind}_{round_.id}",
            )
            for round_ in rounds if round_.status == "running"
        ]

    async def describe_scheduled(
        self, db_session: AsyncSession, item_id: int
    ) -> PublicEvent | None:
        round_ = await guess_service.get_round(db_session, item_id)
        if round_ is None or round_.kind != self.kind or round_.status != "ready":
            return None
        return PublicEvent(
            key=self.key, item_id=round_.id, title=round_.title,
            summary=f"{round_.max_attempts} tentativi", emoji=kind_of(self.kind).emoji,
            deep_link_payload=f"{self.kind}_{round_.id}",
        )

    # ------------------------------------------------------------------
    # Hub screens
    # ------------------------------------------------------------------

    async def render_list(self, message: Message, db_session: AsyncSession) -> None:
        # Rounds are persistent objects: show ready/running AND the recent finished
        # ones (archive). Tapping any of them opens its detail screen, never starts
        # it (STEERING §18.2, no accidental launch).
        rounds = await guess_service.list_manageable(db_session, self.kind)
        b = InlineKeyboardBuilder()
        lines = [f"{self.hub_label}\n"]
        for r in rounds:
            dot, label = _STATUS.get(r.status, ("•", r.status))
            # Title only — no `#id` in the listing (the id still travels in the
            # callback payload, so tapping still resolves the right round).
            lines.append(f"{dot} {esc(r.title)} — <i>{label}</i>")
            b.button(
                text=f"{dot} {r.title[:25]}",
                callback_data=EventCb(action="item", task_type=self.key, item_id=r.id).pack(),
            )
        if not rounds:
            lines.append("<i>Nessun round. Creane uno.</i>")
        b.button(
            text=self.create_label, callback_data=EventCb(action="new", task_type=self.key).pack()
        )
        b.button(text="⬅️ Eventi", callback_data=EventCb(action="home").pack())
        b.adjust(1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def render_detail(self, message: Message, db_session: AsyncSession, item_id: int) -> None:
        """Info screen for a single round with status-aware actions.

        The correct answer is shown here. This screen is admin-only — the hub
        router is gated at the router level (STEERING §8) — and an admin who
        cannot see the answer cannot tell a bad AI verdict from a good one. For
        the same reason the last few rejected answers are listed: it is the only
        way to notice the judge turning down something it should have accepted.
        """
        round_ = await guess_service.get_round(db_session, item_id)
        if round_ is None or round_.kind != self.kind:
            b = InlineKeyboardBuilder()
            b.button(
                text="⬅️ Indietro", callback_data=EventCb(action="list", task_type=self.key).pack()
            )
            await edit_or_send(message, "⚠️ Round non trovato (eliminato?).", b.as_markup())
            return

        dot, label = _STATUS.get(round_.status, ("•", round_.status))
        limit = round_.time_limit_seconds
        lines = [
            f"{dot} <b>{esc(round_.title)}</b> — <i>{label}</i>",
            f"\n✅ Risposta: <b>{esc(round_.answer)}</b>",
            f"🎯 {round_.max_attempts} tentativi · "
            + (f"⏱️ {format_seconds_short(limit)}" if limit else "⏱️ senza limite"),
            "⏳ Chiusura: " + (
                f"automatica il {schedule_service.to_local(round_.closes_at):%d/%m %H:%M}"
                if round_.closes_at is not None
                else f"automatica dopo {format_seconds_short(round_.round_duration_seconds)}"
                if round_.round_duration_seconds
                else "manuale"
            ),
            f"🏆 {guess_service.format_prize_summary(round_)}",
        ]
        hints = guess_service.hints_of(round_)
        if hints:
            lines.append("💡 " + " · ".join(f"dopo {a}" for a, _ in hints))

        if round_.status in ("running", "finished"):
            players, attempts = await guess_service.play_stats(db_session, item_id)
            solvers = len(await guess_service.standings(db_session, item_id))
            lines.append(
                f"👥 {players} giocatori · ✍️ {attempts} tentativi · 🎉 {solvers} indovinati"
            )
            rejected = await guess_service.recent_rejected(db_session, item_id, limit=_AUDIT_LIMIT)
            if rejected:
                lines.append(
                    "\n<i>Ultime risposte scartate (controlla che il giudice non "
                    "abbia sbagliato):</i>\n" + "\n".join(f"• {esc(r)}" for r in rejected)
                )
        if round_.started_at:
            lines.append(f"▶️ Avviato: {_fmt_dt(round_.started_at)}")
        if round_.finished_at:
            lines.append(f"🏁 Concluso: {_fmt_dt(round_.finished_at)}")

        b = InlineKeyboardBuilder()
        if round_.status == "ready":
            b.button(
                text="▶️ Avvia ora",
                callback_data=EventCb(
                    action="askstart", task_type=self.key, item_id=item_id
                ).pack(),
            )
            b.button(
                text="🗓️ Programma",
                callback_data=EventCb(action="sched", task_type=self.key, item_id=item_id).pack(),
            )
            b.button(
                text="🔤 Aggiungi grafie",
                callback_data=GuessAliasCb(action="add", round_id=item_id).pack(),
            )
            b.button(
                text="🗑️ Elimina",
                callback_data=EventCb(action="askdel", task_type=self.key, item_id=item_id).pack(),
            )
        elif round_.status == "running":
            b.button(
                text="🏁 Chiudi",
                callback_data=EventCb(
                    action="askclose", task_type=self.key, item_id=item_id
                ).pack(),
            )
            b.button(
                text="🗓️ Programma chiusura",
                # was the optional 5th segment ("...:close"), now its own action
                callback_data=EventCb(
                    action="sched_close", task_type=self.key, item_id=item_id
                ).pack(),
            )
            # The judge is an LLM: it will occasionally turn down a spelling that
            # was right. Accepting it here fixes the round for everyone who guesses
            # from now on, without rebuilding it (see `handlers.guess.editing`).
            b.button(
                text="🔤 Aggiungi grafie",
                callback_data=GuessAliasCb(action="add", round_id=item_id).pack(),
            )
            b.button(
                text="🗑️ Elimina",
                callback_data=EventCb(action="askdel", task_type=self.key, item_id=item_id).pack(),
            )
        else:  # finished
            b.button(
                text="🔁 Riproponi",
                callback_data=EventCb(
                    action="askreset", task_type=self.key, item_id=item_id
                ).pack(),
            )
            b.button(
                text="🗑️ Elimina",
                callback_data=EventCb(action="askdel", task_type=self.key, item_id=item_id).pack(),
            )
        b.button(text="⬅️ Indietro", callback_data=EventCb(action="list", task_type=self.key).pack())
        b.adjust(2, 1, 1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    # ------------------------------------------------------------------
    # Optional capabilities (probed with getattr by the hub)
    # ------------------------------------------------------------------

    async def delete(self, db_session: AsyncSession, item_id: int) -> StartResult:
        ok = await guess_service.delete_round(db_session, item_id)
        return StartResult(ok, "🗑️ Round eliminato." if ok else "Round non trovato.", alert=not ok)

    async def reset(self, db_session: AsyncSession, item_id: int) -> StartResult | None:
        ok = await guess_service.reset_round(db_session, item_id)
        return StartResult(
            ok,
            "🔁 Round riproposto: tentativi azzerati, di nuovo pronto."
            if ok
            else "Impossibile riproporre (solo i round conclusi).",
            alert=not ok,
        )

    # ------------------------------------------------------------------
    # EventType contract
    # ------------------------------------------------------------------

    async def schedulable_items(self, db_session: AsyncSession) -> list[tuple[int, str]]:
        return [(r.id, r.title) for r in await guess_service.list_ready(db_session, self.kind)]

    async def start_creation(self, message: Message, state: FSMContext, creator_id: int) -> None:
        from handlers.guess import start_guess_creation

        await start_guess_creation(message, state, kind=self.kind, creator_id=creator_id)

    async def start_now(self, bot, db_session: AsyncSession, item_id: int) -> StartResult:
        from handlers.guess import open_round

        ok, msg = await open_round(bot, db_session, item_id)
        return StartResult(ok, msg, alert=not ok)

    async def close_now(self, bot, db_session: AsyncSession, item_id: int) -> StartResult | None:
        from handlers.guess import close_round

        ok, msg = await close_round(bot, db_session, item_id)
        return StartResult(ok, "🏁 Round chiuso. Podio pubblicato." if ok else msg, alert=not ok)

    async def execute_scheduled(
        self, bot, session: AsyncSession, task: ScheduledTask, group_id: int
    ) -> None:
        from handlers.guess import close_round, open_round
        from services.schedule_service import TaskSkip

        # Auto-close reuses this same task_type with an action payload — the very
        # pattern the betting window already uses (STEERING §20). No new task type.
        if schedule_service.task_payload(task).get("action") == "close":
            ok, msg = await close_round(bot, session, task.ref_id)
            if not ok:
                raise TaskSkip(msg)
            return

        # Already running (an admin started it by hand before the scheduled time)
        # → skip, don't fail: it is the intended end state anyway.
        round_ = await guess_service.get_round(session, task.ref_id)
        if round_ is not None and round_.status == "running":
            raise TaskSkip("il round era già in corso, avvio programmato saltato.")

        ok, msg = await open_round(bot, session, task.ref_id)
        if not ok:
            raise RuntimeError(msg)
