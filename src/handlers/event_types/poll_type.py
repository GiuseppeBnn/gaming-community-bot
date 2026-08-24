"""Poll event type — pre-created native Telegram polls with an optional
participation prize, description and scheduled auto-close.

A poll now has a real lifecycle (``ready`` → ``running`` → ``finished``) instead of
being sent fire-and-forget: starting it stores the live-poll handles so it can be
stopped and its voters paid, and closing it stops the poll, announces the winning
*option* (a poll has no "right" answer, so the prize is for participation) and
mints the CoInn/XP prize to everyone who voted.

``open_poll`` / ``close_poll`` are the shared engine used by both the hub buttons
(``start_now`` / ``close_now``) and the scheduler (``execute_scheduled``), the same
split ``handlers.guess.lifecycle`` uses. ``open_poll`` never commits (the caller
does); ``close_poll`` commits the money before announcing, so a failed announcement
never turns a paid-out poll into an error.

The creation FSM stays in ``handlers.events`` (it keeps the proven router-level
admin gate, STEERING §8); this spec delegates to it via a lazy import.
"""

from __future__ import annotations

import logging

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask
from handlers.callbacks import EventCb
from services import group_registry, poll_service, schedule_service
from services.public_event import PublicEvent
from utils.text import esc

from .base import StartResult, edit_or_send

log = logging.getLogger(__name__)

# Status → (dot, human label) for the list and detail header. The legacy `used`
# status (fire-and-forget sends from before this feature) reads as finished.
_STATUS = {
    "running": ("🟢", "in corso"),
    "ready": ("🟡", "pronto"),
    "finished": ("🏁", "concluso"),
    "used": ("🏁", "concluso"),
}


# ---------------------------------------------------------------------------
# Shared engine: open / close a poll (used by the hub AND the scheduler)
# ---------------------------------------------------------------------------

async def open_poll(bot, db_session: AsyncSession, poll_id: int) -> tuple[bool, str]:
    """Publish the poll in the group. Caller commits.

    Two shapes, decided by whether the poll has a scheduled close (``closes_at``):

    * **plain** (no ``closes_at``, hence no prize): a normal fire-and-forget
      Telegram poll — publish and mark it ``used``. No vote tracking, no auto-close,
      no results announcement. This is the default a bare poll falls back to.
    * **managed** (``closes_at`` set): the poll goes ``running``; its live handles
      are stored (so it can be stopped and its votes paid), an auto-close is armed,
      and the prize + close "info block" is folded **into the poll question** (below
      the title/description) when it still fits in 300 chars, otherwise posted as a
      separate message. It is sent **non-anonymous** on purpose — the only way to
      receive ``poll_answer`` updates and know whom to pay.
    """
    poll = await poll_service.get(db_session, poll_id)
    if poll is None:
        return False, "Sondaggio non trovato."
    if poll.status == "running":
        return False, "Questo sondaggio è già in corso."
    if poll.status not in ("ready",):
        return False, "Questo sondaggio è già stato usato."
    group_id = group_registry.get_group_id()
    if not group_id:
        return False, "GROUP_ID non configurato."

    # --- Plain poll: publish and forget --------------------------------------
    # The description (when any) is folded into the poll question itself, under the
    # title — no separate intro message (STEERING §18.2).
    if poll.closes_at is None:
        try:
            await bot.send_poll(
                chat_id=group_id, question=poll_service.render_question(poll),
                options=poll_service.options_of(poll), is_anonymous=False,
            )
        except Exception as e:  # noqa: BLE001 — bot not in group / not admin / API down
            log.warning("Invio sondaggio %s nel gruppo fallito: %s", poll_id, e)
            return False, "Impossibile pubblicare il sondaggio (il bot è nel gruppo?)."
        await poll_service.mark_used(db_session, poll_id)
        return True, "📊 Sondaggio pubblicato nel gruppo!"

    # --- Managed poll: tracked, with an armed auto-close ----------------------
    # An absolute close fixed at creation cannot be armed in the past.
    if poll.closes_at <= poll_service._now():
        return False, ("La data di chiusura automatica è già passata. "
                       "Aggiornala prima di avviare.")

    # The description is already folded into the poll question (below the title).
    # The prize + close "info block" is folded in **too** — under the title, in the
    # same poll message — as long as title + description + info still fit in the
    # 300-char question. If it doesn't, the info goes as a separate message (its old
    # shape). Plain text inside the question (polls aren't HTML-parsed), HTML in the
    # separate message where bold renders (STEERING §18.2).
    close_str = f"{schedule_service.to_local(poll.closes_at):%d/%m %H:%M}"
    info_plain: list[str] = []
    info_html: list[str] = ["📊 <b>Sondaggio</b>"]
    if poll_service.has_prize(poll):
        summary = poll_service.format_prize_summary(poll)
        info_plain.append(f"🏆 Premio: {summary} — vota per riceverlo!")
        info_html.append(f"🏆 Premio: <b>{summary}</b> — vota per riceverlo!")
    info_plain.append(f"🏁 Si chiude il {close_str}.")
    info_html.append(f"🏁 Si chiude il <b>{close_str}</b>.")

    core = poll_service.render_question(poll)  # title + description
    merged = f"{core}\n\n" + "\n".join(info_plain)
    if poll_service.question_length(merged) <= poll_service.POLL_QUESTION_MAX:
        question = merged  # everything in the poll message — no separate intro
    else:
        question = core
        try:
            await group_registry.send_group_message(bot, db_session, "\n\n".join(info_html))
        except Exception as e:  # noqa: BLE001 — the intro is a bonus, not the poll
            log.warning("Intro sondaggio %s non inviata: %s", poll_id, e)

    try:
        sent = await bot.send_poll(
            chat_id=group_id,
            question=question,
            options=poll_service.options_of(poll),
            is_anonymous=False,
        )
    except Exception as e:  # noqa: BLE001 — bot not in group / not admin / API down
        log.warning("Invio sondaggio %s nel gruppo fallito: %s", poll_id, e)
        return False, "Impossibile pubblicare il sondaggio (il bot è nel gruppo?)."

    await poll_service.mark_running(
        db_session, poll_id,
        message_id=sent.message_id, chat_id=sent.chat.id, tg_poll_id=sent.poll.id,
    )
    await _arm_auto_close(db_session, poll)
    return True, "📊 Sondaggio pubblicato nel gruppo!"


async def _arm_auto_close(db_session: AsyncSession, poll) -> None:
    """Schedule the auto-close on ``closes_at``, reusing ``task_type="poll"`` with an
    ``action=close`` payload — the same pattern as the betting auto-lock and the
    guess auto-close (STEERING §18.2/§20). No new task type."""
    await schedule_service.schedule_task(
        db_session,
        task_type="poll",
        run_at=poll.closes_at,
        created_by_tg_id=poll.creator_tg_id,
        group_id=poll.group_id,
        ref_id=poll.id,
        payload={"action": "close"},
    )


async def close_poll(bot, db_session: AsyncSession, poll_id: int) -> tuple[bool, str]:
    """Stop the poll, announce the winning option and pay every voter. Commits.

    Order: stop the Telegram poll first (freezes the tallies and yields the final
    counts), then claim the ``running`` → ``finished`` transition as the payment
    guard — two admins closing at once cannot pay twice — then pay and, only after
    the money is committed, announce.
    """
    poll = await poll_service.get(db_session, poll_id)
    if poll is None:
        return False, "Sondaggio non trovato."

    # Claim the close FIRST: the conditional running→finished UPDATE is the payment
    # guard (only one caller wins it), and it also freezes vote recording — the
    # poll_answer handler ignores a non-running poll — so the tallies read below are
    # final. The `poll` fetched above stays valid (the claim doesn't delete it).
    blocked = await poll_service.claim_close(db_session, poll_id)
    if blocked in ("finished", "used"):
        return False, "Questo sondaggio è già stato chiuso."
    if blocked is not None:  # ready (never started), or a concurrent delete
        return False, "Questo sondaggio non è in corso (avvialo prima)."

    # Best-effort stop to read the final tallies. A failure here (already closed,
    # message deleted) must not block the payout — we just announce without the
    # winning-option line.
    final = None
    if poll.chat_id is not None and poll.message_id is not None:
        try:
            final = await bot.stop_poll(poll.chat_id, poll.message_id)
        except Exception as e:  # noqa: BLE001
            log.warning("stop_poll del sondaggio %s fallito: %s", poll_id, e)

    # This close cancels any armed auto-close still pending for the poll, so it
    # cannot fire later against a finished poll and log a spurious failure.
    await db_session.execute(
        update(ScheduledTask)
        .where(ScheduledTask.task_type == "poll",
               ScheduledTask.ref_id == poll_id,
               ScheduledTask.status == "pending")
        .values(status="cancelled")
    )

    paid = await poll_service.pay_voters(db_session, poll)
    # Money BEFORE announcing: a failed send must never undo a paid-out poll.
    await db_session.commit()

    # Notify each paid voter privately, like an admin manual grant. Best-effort and
    # post-commit: a DM failure (user never started the bot) must never undo a
    # paid-out poll, exactly like the announcement below.
    if paid and poll_service.has_prize(poll):
        dm = poll_service.format_reward_dm(poll)
        for uid in paid:
            try:
                await bot.send_message(uid, dm)
            except Exception:  # noqa: BLE001 — user may not have started the bot
                log.debug("DM premio sondaggio %s a %s saltato.", poll_id, uid)

    text = _close_text(poll, final, len(paid))
    if group_registry.get_group_id() != 0:
        try:
            await group_registry.send_group_message(bot, db_session, text)
        except Exception:  # noqa: BLE001
            log.warning("Annuncio chiusura sondaggio %s fallito.", poll_id)
        return True, "🏁 Sondaggio chiuso. Risultati pubblicati nel gruppo."
    return True, text


def _close_text(poll, final, paid: int) -> str:
    lines = [f"🏁 <b>Sondaggio chiuso!</b>\n❓ {esc(poll.question)}"]
    if final is not None and final.options:
        top = max(o.voter_count for o in final.options)
        if top <= 0:
            lines.append("\n<i>Nessun voto.</i>")
        else:
            winners = [o for o in final.options if o.voter_count == top]
            plural = "voti" if top != 1 else "voto"
            if len(winners) == 1:
                lines.append(f"\n🥇 Opzione vincente: <b>{esc(winners[0].text)}</b> ({top} {plural})")
            else:
                names = ", ".join(f"<b>{esc(o.text)}</b>" for o in winners)
                lines.append(f"\n🤝 Pareggio ({top} {plural}): {names}")
    if poll_service.has_prize(poll):
        if paid > 0:
            who = "votante premiato" if paid == 1 else "votanti premiati"
            lines.append(
                f"\n🏆 {paid} {who} con <b>{poll_service.format_prize_summary(poll)}</b>."
            )
        else:
            lines.append("\n🏆 Nessun votante da premiare.")
    return "\n".join(lines)


class PollType:
    key = "poll"
    hub_label = "📊 Sondaggio"
    create_label = "➕ Crea sondaggio"
    #: Its close stops the poll, announces the winning option and pays the voters,
    #: so it is worth scheduling on its own clock (STEERING §20).
    closable = True

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
        polls = await poll_service.list_manageable(db_session)
        b = InlineKeyboardBuilder()
        lines = ["📊 <b>Sondaggi</b>\n"]
        for p in polls:
            dot, label = _STATUS.get(p.status, ("•", p.status))
            # Title only — no `#id` in the listing (the id travels in the payload).
            lines.append(f"{dot} {esc(p.question)} — <i>{label}</i>")
            b.button(
                text=f"{dot} {p.question[:25]}",
                callback_data=EventCb(action="item", task_type="poll", item_id=p.id).pack(),
            )
        if not polls:
            lines.append("<i>Nessun sondaggio. Creane uno.</i>")
        b.button(text="➕ Crea sondaggio", callback_data=EventCb(action="new", task_type="poll").pack())
        b.button(text="⬅️ Eventi", callback_data=EventCb(action="home").pack())
        b.adjust(1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def render_detail(self, message: Message, db_session: AsyncSession, item_id: int) -> None:
        """Info screen for a single poll with status-aware actions (avvia / chiudi /
        programma chiusura / elimina), each routed through an ``ev:ask*`` confirm."""
        poll = await poll_service.get(db_session, item_id)
        if poll is None:
            b = InlineKeyboardBuilder()
            b.button(text="⬅️ Indietro", callback_data=EventCb(action="list", task_type="poll").pack())
            await edit_or_send(message, "⚠️ Sondaggio non trovato (eliminato?).", b.as_markup())
            return

        dot, label = _STATUS.get(poll.status, ("•", poll.status))
        options = poll_service.options_of(poll)
        lines = [f"{dot} <b>{esc(poll.question)}</b> — <i>{label}</i>"]
        if poll.description:
            lines.append(f"\n📝 {esc(poll.description)}")
        lines.append(f"\n🗳️ {len(options)} opzioni · 🏆 {poll_service.format_prize_summary(poll)}")
        if poll.closes_at is not None:
            lines.append(
                f"⏳ Chiusura automatica: {schedule_service.to_local(poll.closes_at):%d/%m %H:%M}"
            )
        if poll.status in ("running", "finished", "used"):
            votes = await poll_service.voter_count(db_session, item_id)
            lines.append(f"👥 {votes} votanti tracciati")

        b = InlineKeyboardBuilder()
        if poll.status == "ready":
            b.button(
                text="▶️ Avvia ora",
                callback_data=EventCb(action="askstart", task_type="poll", item_id=item_id).pack(),
            )
            b.button(
                text="🗓️ Programma",
                callback_data=EventCb(action="sched", task_type="poll", item_id=item_id).pack(),
            )
            b.button(
                text="🗑️ Elimina",
                callback_data=EventCb(action="askdel", task_type="poll", item_id=item_id).pack(),
            )
        elif poll.status == "running":
            b.button(
                text="🏁 Chiudi",
                callback_data=EventCb(action="askclose", task_type="poll", item_id=item_id).pack(),
            )
            b.button(
                text="🗓️ Programma chiusura",
                callback_data=EventCb(action="sched_close", task_type="poll", item_id=item_id).pack(),
            )
            b.button(
                text="🗑️ Elimina",
                callback_data=EventCb(action="askdel", task_type="poll", item_id=item_id).pack(),
            )
        else:  # finished / used
            b.button(
                text="🗑️ Elimina",
                callback_data=EventCb(action="askdel", task_type="poll", item_id=item_id).pack(),
            )
        b.button(text="⬅️ Indietro", callback_data=EventCb(action="list", task_type="poll").pack())
        b.adjust(2, 1)
        await edit_or_send(message, "\n".join(lines), b.as_markup())

    async def delete(self, db_session: AsyncSession, item_id: int) -> StartResult:
        ok = await poll_service.delete_poll(db_session, item_id)
        return StartResult(ok, "🗑️ Sondaggio eliminato." if ok else "Sondaggio non trovato.", alert=not ok)

    async def schedulable_items(self, db_session: AsyncSession) -> list[tuple[int, str]]:
        return [(p.id, p.question) for p in await poll_service.list_ready(db_session)]

    async def start_creation(
        self, message: Message, state: FSMContext, creator_id: int
    ) -> None:
        from handlers.events import start_poll_creation

        await start_poll_creation(message, state)

    async def start_now(self, bot, db_session: AsyncSession, item_id: int) -> StartResult:
        ok, msg = await open_poll(bot, db_session, item_id)
        return StartResult(ok, msg, alert=not ok)

    async def close_now(
        self, bot, db_session: AsyncSession, item_id: int
    ) -> StartResult | None:
        ok, msg = await close_poll(bot, db_session, item_id)
        return StartResult(ok, msg, alert=not ok)

    async def execute_scheduled(
        self, bot, session: AsyncSession, task: ScheduledTask, group_id: int
    ) -> None:
        from services.schedule_service import TaskSkip

        # Auto-close reuses this same task_type with an action payload — same pattern
        # as the guess auto-close and the betting auto-lock. No new task type.
        if schedule_service.task_payload(task).get("action") == "close":
            ok, msg = await close_poll(bot, session, task.ref_id)
            if not ok:
                raise TaskSkip(msg)
            return

        # New model: ref_id → a pre-created PollTemplate (with prize/close support).
        if task.ref_id:
            poll = await poll_service.get(session, task.ref_id)
            if poll is None:
                raise RuntimeError(f"Sondaggio #{task.ref_id} non trovato")
            if poll.status == "running":
                raise TaskSkip("il sondaggio era già in corso, avvio programmato saltato.")
            ok, msg = await open_poll(bot, session, task.ref_id)
            if not ok:
                raise RuntimeError(msg)
            return

        # Legacy: inline payload (no template) — fire-and-forget, no prize/close.
        payload = schedule_service.task_payload(task)
        await bot.send_poll(
            chat_id=group_id, question=payload["question"], options=payload["options"],
            is_anonymous=False,
        )
