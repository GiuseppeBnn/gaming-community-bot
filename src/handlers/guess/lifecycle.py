"""Launching and closing a guess round.

Two orderings that are not interchangeable, and both for the same kind of reason.

`open_round` announces in the group **before** setting the status to `running`: a
send that fails leaves a `ready` round rather than a running one nobody was told
about.

`close_round` does the opposite — it claims the close as a conditional UPDATE
**before** paying — so two admins closing at once cannot pay the pool twice.
Prizes are committed before the podium is announced, so a failed announcement
never turns a paid-out round into an error.

The announcement deliberately carries no medium. Posting the image or the clip in
the group would move the game into the group, where the answer gets discussed and
playing in private stops meaning anything. The reveal happens at the close, under
the podium, when it can no longer spoil anything.
"""

from __future__ import annotations

from datetime import timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask
from handlers._mentions import mention
from handlers._trophy_announce import announce_trophies
from services import (
    badge_service,
    group_registry,
    guess_service,
    progress_service,
    schedule_service,
)
from utils.text import esc, format_seconds_short

from handlers.guess._shared import kind_of, log, send_media


async def open_round(bot, db_session: AsyncSession, round_id: int) -> tuple[bool, str]:
    """Set a round running and announce it in the group. Caller commits."""
    group_id = group_registry.get_group_id()
    if group_id == 0:
        return False, "GROUP_ID non configurato."
    round_ = await guess_service.get_round(db_session, round_id)
    if round_ is None:
        return False, "Round non trovato."
    if round_.status == "running":
        return False, "Questo round è già in corso."
    if round_.status == "finished":
        return False, "Questo round è già stato giocato."

    # An absolute close is fixed at creation, so a round started after that instant
    # would arm its auto-close in the past. Refuse here, before the announcement is
    # sent, rather than schedule a task that fires immediately.
    if round_.closes_at is not None and round_.closes_at <= guess_service.now():
        return False, ("La data di chiusura automatica è già passata. "
                       "Aggiornala prima di avviare.")

    spec = kind_of(round_.kind)
    limit = round_.time_limit_seconds
    time_txt = (
        f"⏱️ {format_seconds_short(limit)} a testa" if limit else "⏱️ senza limite"
    )
    # Say when it ends. A round that closes itself at a time nobody was told is a
    # deadline that ambushes people. An absolute date is stated as such; a relative
    # duration as "fra …".
    if round_.closes_at is not None:
        closes_txt = (
            f"\n🏁 Si chiude il "
            f"<b>{schedule_service.to_local(round_.closes_at):%d/%m %H:%M}</b>."
        )
    elif round_.round_duration_seconds > 0:
        closes_txt = (
            f"\n🏁 Si chiude fra "
            f"<b>{format_seconds_short(round_.round_duration_seconds)}</b>."
        )
    else:
        closes_txt = ""
    # Announce FIRST: a failed send leaves a `ready` round instead of a running
    # one nobody knows about. The medium is NOT posted — see the module docstring.
    try:
        bot_info = await bot.get_me()
        await group_registry.send_group_message(
            bot, db_session,
            f"{spec.emoji} <b>{esc(spec.label.upper())}: {esc(round_.title)}</b>\n"
            f"🎯 {round_.max_attempts} tentativi · {time_txt} · "
            f"🏆 {guess_service.format_prize_summary(round_)}{closes_txt}\n\n"
            "Gioca in <b>chat privata</b> col bot! Vince chi ci arriva in <b>meno "
            "tentativi</b> — a parità conta il tempo. 🏁",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="▶️ Gioca",
                    url=f"https://t.me/{bot_info.username}?start={round_.kind}_{round_.id}",
                )
            ]]),
        )
    except Exception as e:  # noqa: BLE001 — bot not in group / not admin / API down
        log.warning("Annuncio round %s nel gruppo fallito: %s", round_id, e)
        return False, "Impossibile annunciare nel gruppo (il bot è nel gruppo?)."

    await guess_service.set_status(db_session, round_id, "running")
    await _schedule_auto_close(db_session, round_)
    return True, f"{spec.label} avviato nel gruppo!"


async def _schedule_auto_close(db_session: AsyncSession, round_) -> None:
    """Arm the round's own closing time, if it has one.

    Two ways to have one: an admin-picked absolute ``closes_at`` (a fixed instant,
    used as-is) or a relative ``round_duration_seconds`` (armed now, at open-time).
    ``closes_at`` wins; neither ⇒ the round is closed by hand.

    Reuses ``task_type = kind`` with ``payload.action = "close"`` — the same
    pattern as the betting window's auto-lock, and the branch
    ``guess_type.execute_scheduled`` already carries. **No new task type.**

    Without this the branch was unreachable and every round ran until an admin
    remembered it: players sat in rounds that were over for them and never closed
    for anyone.
    """
    if round_.closes_at is not None:
        run_at = round_.closes_at
    elif round_.round_duration_seconds > 0:
        run_at = guess_service.now() + timedelta(seconds=round_.round_duration_seconds)
    else:
        return  # closed by hand, on purpose
    await schedule_service.schedule_task(
        db_session,
        task_type=round_.kind,
        run_at=run_at,
        created_by_tg_id=round_.creator_tg_id,
        group_id=round_.group_id,
        ref_id=round_.id,
        payload={"action": "close"},
    )


async def close_round(bot, db_session: AsyncSession, round_id: int) -> tuple[bool, str]:
    """Close a running round: rank solvers, pay, record trophies, reveal.

    Returns (ok, message). Caller need not commit.

    Claiming the close *is* the status transition, in one conditional UPDATE: only
    one caller can win it, so the prizes below are paid exactly once. Checking the
    status here and flipping it afterwards would be a read-then-write, and the
    round row is often already in this session's cache — the check would pass
    twice (STEERING §22).
    """
    blocked = await guess_service.claim_close(db_session, round_id)
    if blocked == guess_service.ROUND_MISSING:
        return False, "Round non trovato."
    if blocked == "finished":
        return False, "Questo round è già stato chiuso."
    if blocked is not None:
        return False, "Questo round non è in corso (avvialo prima)."

    round_ = await guess_service.get_round(db_session, round_id)
    if round_ is None:  # deleted between the claim and here
        return False, "Round non trovato."

    # This close won the claim, so any armed auto-close is now moot. Left pending
    # it would fire later, find a `finished` round and log a failure for something
    # that went right.
    await db_session.execute(
        update(ScheduledTask)
        .where(ScheduledTask.task_type == round_.kind,
               ScheduledTask.ref_id == round_id,
               ScheduledTask.status == "pending")
        .values(status="cancelled")
    )

    ranked = await guess_service.standings(db_session, round_id)
    awards = await guess_service.award_prizes(db_session, round_id)

    # `kind` is the trophy game_key, so `podium_count` / `first_place_count`
    # trophies for guess/sound light up with no extra wiring.
    affected: set[int] = set()
    for rank, row in enumerate(ranked[:3], start=1):
        await progress_service.record_podium(
            db_session, row.user_tg_id, round_.kind, rank, round_id
        )
        affected.add(row.user_tg_id)
    await db_session.flush()  # make the new rows visible to the count queries
    trophy_notes: dict[int, list] = {}
    for uid in affected:
        earned = await badge_service.check_and_award_milestones(db_session, uid)
        if earned:
            trophy_notes[uid] = earned
    # Commit the money BEFORE announcing: a failed send must never turn a
    # paid-out round into an error.
    await db_session.commit()

    for uid, earned in trophy_notes.items():
        await announce_trophies(bot, db_session, uid, earned)

    text = await _podium_text(db_session, round_, ranked, awards)
    group_id = group_registry.get_group_id()
    if group_id != 0:
        # Two separate `try`s, and the split is the point. The medium is a bonus;
        # the podium is the announcement people are waiting for. Sharing one
        # block, a dead `file_id` swallowed the podium with it — prizes paid, and
        # the group never told who won.
        try:
            await send_media(bot, group_id, round_.media_file_id, round_.media_kind)
        except Exception as exc:  # noqa: BLE001 — a dead file_id is not a bug here
            log.warning("Reveal del media del round %s fallito: %s", round_id, exc)
        try:
            await group_registry.send_group_message(bot, db_session, text)
        except Exception:  # noqa: BLE001
            log.warning("Impossibile annunciare il podio del round %s.", round_id)
        return True, "🏁 Round chiuso. Podio pubblicato nel gruppo."
    return True, text


async def _podium_text(db_session: AsyncSession, round_, ranked, awards) -> str:
    spec = kind_of(round_.kind)
    header = (
        f"🏁 <b>{esc(round_.title)}</b> — chiuso!\n"
        f"✅ Era: <b>{esc(round_.answer)}</b>\n"
    )
    if not ranked:
        return header + "\n<i>Nessuno ci è arrivato. Alla prossima!</i>"

    award_by_user = {a.user_tg_id: a for a in awards}
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{spec.emoji} {header}", f"<b>PODIO {esc(spec.label.upper())}</b>\n"]
    for i, row in enumerate(ranked[:10]):
        rank = medals[i] if i < 3 else f"{i + 1}."
        who = await mention(db_session, row.user_tg_id)
        tries = "1 tentativo" if row.attempts == 1 else f"{row.attempts} tentativi"
        time_txt = f" · ⏱️ {format_seconds_short(round(row.solve_ms / 1000))}"
        award = award_by_user.get(row.user_tg_id)
        prize_txt = ""
        if award and award.coins:
            icon = "🎖️" if award.kind == "consolation" else "🏆"
            prize_txt = f" — {icon} <b>+{award.coins} 🪙 CoInn</b>"
        lines.append(f"{rank} {who} — {tries}{time_txt}{prize_txt}")
    return "\n".join(lines)
