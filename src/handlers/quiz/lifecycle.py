"""Launching, listing and closing a quiz.

`open_quiz` announces in the group **before** setting the status to `running`: a send
that fails leaves a `ready` quiz rather than a running one nobody was told about.

`close_quiz` does the opposite ordering and for the same kind of reason — it claims
the close as a conditional UPDATE **before** paying, so two admins closing at once
cannot pay the pool twice. Prizes are committed before the podium is announced, so a
failed announcement never turns a paid-out quiz into an error."""

from __future__ import annotations


from aiogram.filters.command import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from filters.admin_filter import IsAdminFilter, is_admin
from handlers._mentions import mention
from handlers._privacy import redirect_to_private
from handlers._trophy_announce import announce_trophies
from services import badge_service, group_registry, progress_service, quiz_service
from utils import cooldown
from utils.static_reply import reply_static
from utils.text import esc, format_seconds_short

from handlers.quiz._shared import (
    _QUIZ_PRIVATE_NOTICE,
    log,
    router,
)


# ---------------------------------------------------------------------------
# Launch (announce in group) + listing + close
# ---------------------------------------------------------------------------

async def open_quiz(bot, db_session: AsyncSession, quiz_id: int) -> tuple[bool, str]:
    """Set a quiz running and announce it in the group. Caller commits."""
    group_id = group_registry.get_group_id()
    if group_id == 0:
        return False, "GROUP_ID non configurato."
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or not quiz.questions:
        return False, "Quiz non trovato o senza domande."
    if quiz.status == "running":
        return False, "Questo quiz è già in corso."
    if quiz.status == "finished":
        return False, "Questo quiz è già stato giocato."

    # Announce FIRST: if the group send fails (bot not admin / not in group) we
    # leave the quiz as `ready` rather than marking it running with no announcement.
    limit = quiz_service.time_limit_seconds(quiz)
    tl_text = f"⏱️ {limit}s/domanda" if limit else "⏱️ senza limite"
    try:
        bot_info = await bot.get_me()
        await group_registry.send_group_message(
            bot, db_session,
            f"🧠 <b>QUIZ: {esc(quiz.title)}</b>\n"
            f"❓ {len(quiz.questions)} domande · {tl_text} · 🏆 {quiz_service.format_prize_summary(quiz)}\n\n"
            "Gioca in <b>chat privata</b> col bot! Vince chi ne azzecca di più — "
            "a parità conta l'ordine di arrivo. Premio garantito a tutti i finisher! 🏁",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="▶️ Gioca", url=f"https://t.me/{bot_info.username}?start=quiz_{quiz.id}"
                )
            ]]),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Annuncio quiz %s nel gruppo fallito: %s", quiz_id, e)
        return False, "Impossibile annunciare nel gruppo (il bot è nel gruppo?)."

    await quiz_service.set_status(db_session, quiz_id, "running")
    return True, "Quiz avviato nel gruppo!"


async def _show_play_view(message: Message, db_session: AsyncSession) -> None:
    """Non-admin view of /quiz: a button to play the running quiz, or a clear
    'no quiz active' message — never silence (the old admin filter just dropped
    a non-admin's /quiz). One live reply per user in groups (anti-flood)."""
    if not await cooldown.ready(message, "quiz", settings.command_cooldown_seconds):
        return
    running = [q for q in await quiz_service.list_ready(db_session) if q.status == "running"]
    if not running:
        await reply_static(
            message,
            "🧠 <b>Nessun quiz attivo al momento.</b>\n"
            "Quando un admin ne avvia uno te lo segnaliamo qui nel gruppo! 🏁",
            "quiz",
        )
        return
    bot_info = await message.bot.get_me()
    b = InlineKeyboardBuilder()
    for q in running:
        # Button text is not HTML-parsed → raw title is fine here.
        b.button(
            text=f"▶️ Gioca: {q.title[:30]}",
            url=f"https://t.me/{bot_info.username}?start=quiz_{q.id}",
        )
    b.adjust(1)
    await reply_static(
        message,
        "🧠 <b>Quiz in corso!</b> Tocca per giocare in chat privata:",
        "quiz",
        reply_markup=b.as_markup(),
    )


@router.message(Command("quiz"))
async def cmd_quiz_list(message: Message, db_session: AsyncSession) -> None:
    # Public entry point. Non-admins get the "play" view (with a clear message
    # when no quiz is active); admins get the events-hub management list. The admin
    # branch is still gated by this in-handler is_admin check.
    if not await is_admin(message.bot, message.from_user.id):
        await _show_play_view(message, db_session)
        return
    if await redirect_to_private(message, "admin", "🛠️ Apri il pannello", notice=_QUIZ_PRIVATE_NOTICE):
        return
    # Management goes through the events hub: tapping a quiz opens its detail
    # screen (info + avvia/programma/chiudi/riproponi/elimina, each with a
    # confirmation) — never a one-tap launch (STEERING §18.2).
    from handlers.event_types.quiz_type import QuizType

    await QuizType().render_list(message, db_session)


@router.message(Command("avvia_quiz"), IsAdminFilter())
async def cmd_avvia_quiz(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "admin", "🛠️ Apri il pannello", notice=_QUIZ_PRIVATE_NOTICE):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.reply("ℹ️ Uso: <code>/avvia_quiz &lt;id&gt;</code>")
        return
    ok, msg = await open_quiz(message.bot, db_session, int(raw))
    if ok:
        await db_session.commit()
    await message.reply(("🎬 " if ok else "⚠️ ") + msg)


async def close_quiz(bot, db_session: AsyncSession, quiz_id: int) -> tuple[bool, str]:
    """Close a running quiz: rank finishers, pay prizes, announce the podium.

    Returns (ok, message). The message is the podium text when there is no group
    to announce to, otherwise a short confirmation. Caller need not commit.
    """
    # Claiming the close *is* the status transition, in one conditional UPDATE: only
    # one caller can win it, so the prizes below are paid exactly once. Checking the
    # status here and flipping it afterwards would be a read-then-write, and the
    # quiz row is often already in this session's cache — the check would pass twice.
    blocked = await quiz_service.claim_close(db_session, quiz_id)
    if blocked == quiz_service.QUIZ_MISSING:
        return False, "Quiz non trovato."
    if blocked == "finished":
        return False, "Questo quiz è già stato chiuso."
    if blocked is not None:
        return False, "Questo quiz non è in corso (avvialo prima)."

    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None:  # deleted between the claim and here
        return False, "Quiz non trovato."

    ranked = await quiz_service.podium(db_session, quiz_id)
    awards = await quiz_service.award_prizes(db_session, quiz_id)

    # Record Trivia progress for the trophy engine, then re-check trophies for
    # everyone it could affect (not just the podium):
    #   • podium, top 3          → game_podiums (podium_count / first_place_count)
    #   • last place, ≥2 players → "trivia_last_place" event (hidden trophies)
    #   • completed under 30 s   → "trivia_sub30" event (Velocista / Need For Speed …)
    # plus any XP/coin milestone hit by the quiz rewards. record_event is idempotent
    # per (user, metric, quiz) so a re-close cannot double-count.
    affected: set[int] = set()
    for rank, row in enumerate(ranked[:3], start=1):
        await progress_service.record_podium(db_session, row.user_tg_id, "trivia", rank, quiz_id)
        affected.add(row.user_tg_id)
    if len(ranked) >= 2:
        last = ranked[-1]
        await progress_service.record_event(
            db_session, last.user_tg_id, progress_service.TRIVIA_LAST_PLACE, quiz_id
        )
        affected.add(last.user_tg_id)
    for row in ranked:
        if row.completion_ms < 30_000:  # total play time under 30 seconds
            await progress_service.record_event(
                db_session, row.user_tg_id, progress_service.TRIVIA_SUB30, quiz_id
            )
            affected.add(row.user_tg_id)
    await db_session.flush()  # make the new rows visible to the count queries
    trophy_notes: dict[int, list] = {}
    for uid in affected:
        earned = await badge_service.check_and_award_milestones(db_session, uid)
        if earned:
            trophy_notes[uid] = earned
    await db_session.commit()

    # Announce newly unlocked trophies in the group, tagging each finisher.
    for uid, earned in trophy_notes.items():
        await announce_trophies(bot, db_session, uid, earned)

    text = await _podium_text(db_session, quiz.title, ranked, awards)
    if group_registry.get_group_id() != 0:
        try:
            await group_registry.send_group_message(bot, db_session, text)
        except Exception:  # noqa: BLE001
            log.warning("Impossibile annunciare il podio nel gruppo.")
        return True, "🏁 Quiz chiuso. Podio pubblicato nel gruppo."
    return True, text


@router.message(Command("chiudi_quiz"), IsAdminFilter())
async def cmd_chiudi_quiz(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "admin", "🛠️ Apri il pannello", notice=_QUIZ_PRIVATE_NOTICE):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.reply("ℹ️ Uso: <code>/chiudi_quiz &lt;id&gt;</code>")
        return
    ok, msg = await close_quiz(message.bot, db_session, int(raw))
    await message.reply(msg if ok else f"⚠️ {msg}")


async def _podium_text(db_session: AsyncSession, title: str, ranked, awards) -> str:
    if not ranked:
        return f"🏁 <b>{esc(title)}</b> — chiuso!\n\n<i>Nessun partecipante ha completato il quiz.</i>"
    award_by_user = {a.user_tg_id: a for a in awards}
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏁 <b>{esc(title)} — PODIO</b>\n"]
    for i, row in enumerate(ranked[:10]):
        rank = medals[i] if i < 3 else f"{i + 1}."
        name = await _display_name(db_session, row.user_tg_id)
        award = award_by_user.get(row.user_tg_id)
        prize_txt = ""
        if award and award.coins:
            icon = "🎖️" if award.kind == "consolation" else "🏆"
            prize_txt = f" — {icon} <b>+{award.coins} 🪙 CoInn</b>"
        time_txt = ""
        if row.completion_seconds is not None:
            time_txt = f" · ⏱️ {format_seconds_short(row.completion_seconds)}"
        lines.append(f"{rank} {name} — {row.correct} ✅{time_txt}{prize_txt}")
    if settings.quiz_xp_participation > 0 or settings.quiz_xp_per_correct > 0:
        lines.append(
            f"\n⚡ <i>XP a tutti i partecipanti: {settings.quiz_xp_participation} di base"
            f" + {settings.quiz_xp_per_correct}/risposta giusta + bonus podio.</i>"
        )
    return "\n".join(lines)


async def _display_name(db_session: AsyncSession, tg_id: int) -> str:
    return await mention(db_session, tg_id)

