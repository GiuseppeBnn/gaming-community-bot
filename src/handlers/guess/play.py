"""Playing a round in private: one medium, N attempts, a free-text answer.

There is no timer task and no in-memory state. The deadline is
``session.started_at + round.time_limit_seconds``, derived and checked at every
submission, so it survives a restart and re-entering cannot reset it. The quiz
needs asyncio timers because its clock is per question; here it is per session,
and the simpler shape is the safer one. The player is told the deadline as a wall
clock time up front, so nobody waits for a "time's up!" that no timer will send.

**The guard order below is load-bearing**: cooldown → already solved → deadline →
attempts → judge. A throttled, late or budget-exhausted message must never cost
an attempt and must never reach the model.

A wrong answer never echoes the correct one, and the model's own words never
reach the player — only the boolean it produced.
"""

from __future__ import annotations

from aiogram import F
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from services import guess_judge, guess_service, schedule_service
from utils import cooldown
from utils.text import esc

from handlers.guess._shared import kind_of, log, router, send_media

_ANSWER_BUCKET = "guess_answer"


class GuessPlayStates(StatesGroup):
    answering = State()


def _quit_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚪 Esci dal gioco", callback_data="guess_play:quit")
    return b.as_markup()


async def start_guess_session(
    message: Message, db_session: AsyncSession, state: FSMContext, round_id: int
) -> None:
    """Deep-link ``<kind>_<id>``: start (or resume) playing this round in private."""
    round_ = await guess_service.get_round(db_session, round_id)
    if round_ is None:
        await message.answer("⚠️ Round non trovato.")
        return
    if round_.status == "finished":
        await message.answer("🏁 Questo round è già terminato.")
        return
    if round_.status != "running":
        await message.answer(
            "⏳ Questo round non è ancora iniziato. Aspetta che un admin lo avvii."
        )
        return

    user_id = message.from_user.id
    sess = await guess_service.start_or_resume(db_session, round_id, user_id)
    await db_session.commit()

    if sess.solved_at is not None:
        await message.answer(
            f"✅ Hai <b>già indovinato</b> «{esc(round_.title)}» in "
            f"<b>{sess.solved_attempts}</b> tentativi.\n"
            "Aspetta la chiusura per vedere il podio! 🏁"
        )
        return

    left = await guess_service.attempts_left(db_session, round_, user_id)
    if left <= 0:
        await message.answer("❌ Hai esaurito i tentativi per questo round.")
        return

    spec = kind_of(round_.kind)
    try:
        await send_media(message.bot, message.chat.id,
                         round_.media_file_id, round_.media_kind)
    except Exception as exc:  # noqa: BLE001 — a dead file_id must not read as a bug
        log.warning("Media del round %s non inviabile: %s", round_id, exc)
        await message.answer(
            "⚠️ Non riesco a caricare il contenuto di questo round. "
            "Segnalalo a un admin."
        )
        return

    deadline = guess_service.deadline(round_, sess)
    when = (
        f"\n⏱️ Hai tempo fino alle <b>"
        f"{schedule_service.to_local(deadline):%H:%M}</b>."
        if deadline is not None else ""
    )
    await state.set_state(GuessPlayStates.answering)
    await state.update_data(round_id=round_id)
    await message.answer(
        f"{spec.emoji} <b>{esc(round_.title)}</b>\n\n"
        f"Scrivimi il <b>titolo del gioco</b>. Hai <b>{left}</b> tentativi.{when}\n"
        "<i>Meno tentativi usi, più in alto finisci nel podio!</i>",
        reply_markup=_quit_kb(),
    )


@router.message(GuessPlayStates.answering, F.chat.type == ChatType.PRIVATE)
async def fsm_answer(
    message: Message, db_session: AsyncSession, state: FSMContext
) -> None:
    raw = (message.text or "").strip()
    if not raw:
        return  # a sticker or a photo is not an attempt

    round_id = (await state.get_data()).get("round_id")
    round_ = (
        await guess_service.get_round(db_session, round_id) if round_id else None
    )
    if round_ is None or round_.status != "running":
        await state.clear()
        await message.answer("🏁 Questo round è chiuso.")
        return

    # Cooldown FIRST: a throttled message must not cost an attempt.
    # exempt_admin=False on purpose — a game is a game, and an admin who can
    # hammer the judge is an admin with a better shot at the podium.
    if not await cooldown.guard(
        message, _ANSWER_BUCKET, settings.guess_answer_cooldown_seconds,
        exempt_admin=False,
        notice="⏳ Vai più piano! Riprova tra {s}s.",
    ):
        return

    user_id = message.from_user.id
    sess = await guess_service.start_or_resume(db_session, round_.id, user_id)
    if sess.solved_at is not None:
        await state.clear()
        await message.answer("✅ Hai già indovinato questo round.")
        return

    deadline = guess_service.deadline(round_, sess)
    if deadline is not None and guess_service.now() > deadline:
        await state.clear()
        await message.answer("⏱️ <b>Tempo scaduto</b> per questo round.")
        return

    if await guess_service.attempts_left(db_session, round_, user_id) <= 0:
        await state.clear()
        await message.answer("❌ Tentativi <b>esauriti</b> per questo round.")
        return

    verdict = await guess_judge.judge(db_session, round_, raw)
    outcome = await guess_service.record_attempt(
        db_session, round_, user_id, raw, verdict
    )
    await db_session.commit()

    if not outcome.recorded:
        await message.answer("⏳ Sto ancora valutando il tentativo precedente.")
        return

    if not verdict.verified:
        await message.answer(
            "⚠️ Non sono riuscito a <b>verificare</b> la tua risposta. "
            "Riprova: questo tentativo non conta.",
            reply_markup=_quit_kb(),
        )
        return

    if outcome.solved:
        await state.clear()
        tries = "1 tentativo" if outcome.attempt_no == 1 else f"{outcome.attempt_no} tentativi"
        await message.answer(
            f"🎉 <b>Indovinato!</b> In <b>{tries}</b>.\n"
            "Aspetta la chiusura per scoprire il podio! 🏆"
        )
        return

    if outcome.attempts_left <= 0:
        await state.clear()
        await message.answer(
            "❌ Tentativi <b>esauriti</b>. Ci vediamo al prossimo round!"
        )
        return

    # A wrong answer never echoes the correct one.
    lines = [f"❌ <b>Non ci siamo.</b> Ti restano <b>{outcome.attempts_left}</b> tentativi."]
    if outcome.hint:
        lines.append(f"\n💡 <i>{esc(outcome.hint)}</i>")
    await message.answer("\n".join(lines), reply_markup=_quit_kb())


@router.callback_query(GuessPlayStates.answering, F.data == "guess_play:quit")
async def cb_quit(callback, state: FSMContext) -> None:
    """Leave the answering mode. The session (and its clock) stays: quitting is
    not a way to buy more time."""
    await state.clear()
    await callback.message.answer(
        "🚪 Uscito dal gioco. Riapri il link del gruppo per riprendere — "
        "il tempo però continua a scorrere."
    )
    await callback.answer()
