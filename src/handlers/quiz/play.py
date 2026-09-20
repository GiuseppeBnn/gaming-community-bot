"""Playing a quiz in private: one question at a time, in order.

This is the only place in the bot where order is enforced. A player may answer only
their current question, in the order shuffled for them, and only once — the podium
ranks by correct answers and then by how long each player took, so an answer accepted
out of order or twice corrupts a ranking that pays out coins.

Each question with a time limit arms a countdown task, kept in the process-global
`_PLAY` map along with the moment the question was shown. Answering cancels the
countdown before recording; a timer that survived would fire later, find the question
already answered, and mark the *next* one wrong."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from aiogram import Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import async_session_maker
from handlers.callbacks import QuizAnswerCb
from services import quiz_service
from utils.text import esc, format_seconds_short

from handlers.quiz._shared import (
    _MAX_OPTION,
    log,
    router,
)


def _question_kb(
    quiz_id: int, question_id: int, ordered_options: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    """``ordered_options`` are ``(real_index, text)`` pairs, already in display order —
    the callback always carries ``real_index`` so answer recording is unaffected by
    display-order randomization (§19)."""
    b = InlineKeyboardBuilder()
    for real_idx, opt in ordered_options:
        # Slice to the single validated cap (`_MAX_OPTION`), not a separate hard
        # 40: a divergent display cap is what cut answers the creation flow had
        # accepted. Defensive for legacy rows stored before the cap dropped.
        b.button(
            text=opt[:_MAX_OPTION],
            callback_data=QuizAnswerCb(
                action="answer", quiz_id=quiz_id, question_id=question_id, option_id=real_idx
            ).pack(),
        )
    b.adjust(1)
    return b.as_markup()


# ---------------------------------------------------------------------------
# Private play (one question at a time, with an optional per-question timer)
# ---------------------------------------------------------------------------


@dataclass
class _PlayCtx:
    """In-memory state of the question a user is currently looking at — used to
    measure response time and to run the countdown. Keyed by (quiz_id, user_tg_id)."""

    question_id: int
    shown_at: float  # time.monotonic() when the question was sent
    message_id: int
    chat_id: int
    timer: "asyncio.Task | None" = None


_PLAY: dict[tuple[int, int], _PlayCtx] = {}


def _play_key(quiz_id: int, user_tg_id: int) -> tuple[int, int]:
    return (quiz_id, user_tg_id)


def _cancel_task(task: "asyncio.Task | None") -> None:
    """Cancel a question's countdown task — but NEVER the one currently running.
    A timer that cancels itself would raise CancelledError (a BaseException, so it
    slips past ``except Exception``) at the next await and abort the very coroutine
    that is finishing the quiz — the user would never see "Quiz completato!"."""
    if task is not None and task is not asyncio.current_task():
        task.cancel()


def _cancel_timer(quiz_id: int, user_tg_id: int) -> None:
    ctx = _PLAY.get(_play_key(quiz_id, user_tg_id))
    if ctx is not None:
        _cancel_task(ctx.timer)


def _forget_play(quiz_id: int, user_tg_id: int) -> None:
    ctx = _PLAY.pop(_play_key(quiz_id, user_tg_id), None)
    if ctx is not None:
        _cancel_task(ctx.timer)


def _response_ms(quiz_id: int, user_tg_id: int, question_id: int, limit: int) -> int:
    """Milliseconds the user took on this question (from the in-memory shown_at).
    Falls back to 0 if the context is missing (e.g. after a restart)."""
    ctx = _PLAY.get(_play_key(quiz_id, user_tg_id))
    if ctx is None or ctx.question_id != question_id:
        return 0
    ms = round((time.monotonic() - ctx.shown_at) * 1000)
    if limit > 0:
        ms = min(ms, limit * 1000)
    return max(0, ms)


async def start_quiz_session(message: Message, db_session: AsyncSession, quiz_id: int) -> None:
    """Deep-link quiz_<id>: start (or resume) playing the quiz in private."""
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or not quiz.questions:
        await message.answer("⚠️ Quiz non trovato.")
        return
    if quiz.status == "finished":
        await message.answer("🏁 Questo quiz è già terminato.")
        return
    if quiz.status != "running":
        await message.answer("⏳ Questo quiz non è ancora iniziato. Aspetta che un admin lo avvii.")
        return

    total = len(quiz.questions)
    done = await quiz_service.answered_count(db_session, quiz_id, message.from_user.id)
    if done >= total:
        correct = await quiz_service.correct_count(db_session, quiz_id, message.from_user.id)
        await message.answer(
            f"✅ Hai già completato <b>{esc(quiz.title)}</b>: <b>{correct}/{total}</b> corrette.\n"
            "Aspetta la chiusura per vedere il podio! 🏁"
        )
        return

    limit = quiz_service.time_limit_seconds(quiz)
    rules = (
        f"Hai <b>{limit} secondi</b> per ogni domanda: allo scadere è data come sbagliata. "
        "A parità di risposte, chi finisce prima sale sul podio!"
        if limit > 0
        else "Nessun limite di tempo, ma chi finisce prima sale sul podio a parità di risposte!"
    )
    # The admin's own description, under the title (skipped in creation ⇒ empty).
    desc_txt = f"📝 <i>{esc(quiz.description)}</i>\n" if quiz.description else ""
    await message.answer(f"🧠 <b>{esc(quiz.title)}</b>\n{desc_txt}<i>{rules}</i>")
    await _present_question(message.bot, message.chat.id, message.from_user.id, quiz, done)


async def _present_question(bot: Bot, chat_id: int, user_tg_id: int, quiz, index: int) -> None:
    """Send question #index (in this user's order) and arm its countdown (if a limit is set)."""
    question = quiz_service.user_question_order(quiz, user_tg_id)[index]
    ordered_options = quiz_service.user_option_order(quiz, question, user_tg_id)
    limit = question.open_period
    hint = f"\n\n⏱️ <i>Hai {limit} secondi</i>" if limit > 0 else ""
    sent = await bot.send_message(
        chat_id,
        f"❓ <b>Domanda {index + 1}/{len(quiz.questions)}</b>\n\n{esc(question.text)}{hint}",
        reply_markup=_question_kb(quiz.id, question.id, ordered_options),
    )

    key = _play_key(quiz.id, user_tg_id)
    old = _PLAY.pop(key, None)
    if old is not None:
        _cancel_task(old.timer)
    ctx = _PlayCtx(
        question_id=question.id,
        shown_at=time.monotonic(),
        message_id=sent.message_id,
        chat_id=chat_id,
    )
    _PLAY[key] = ctx
    if limit > 0:
        ctx.timer = asyncio.create_task(
            _expire_question(bot, chat_id, quiz.id, user_tg_id, question.id, sent.message_id, limit)
        )


async def _advance_or_finish(
    bot: Bot, chat_id: int, user_tg_id: int, quiz, db_session: AsyncSession
) -> None:
    """Send the next question or, once all are answered, the final wrap-up."""
    total = len(quiz.questions)
    done = await quiz_service.answered_count(db_session, quiz.id, user_tg_id)
    if done < total:
        await _present_question(bot, chat_id, user_tg_id, quiz, done)
        return
    _forget_play(quiz.id, user_tg_id)
    correct = await quiz_service.correct_count(db_session, quiz.id, user_tg_id)
    secs = await quiz_service.user_completion_seconds(db_session, quiz.id, user_tg_id)
    time_line = (
        f"⏱️ Tempo impiegato: <b>{format_seconds_short(secs)}</b>\n" if secs is not None else ""
    )
    await bot.send_message(
        chat_id,
        f"🏁 <b>Quiz completato!</b>\n\n"
        f"Hai totalizzato <b>{correct}/{total}</b> risposte corrette.\n"
        f"{time_line}"
        "Aspetta la chiusura per scoprire il podio! 🏆",
    )


async def _expire_question(
    bot: Bot,
    chat_id: int,
    quiz_id: int,
    user_tg_id: int,
    question_id: int,
    message_id: int,
    limit: int,
) -> None:
    """Countdown for one question: after `limit` seconds, if still unanswered, mark
    it wrong and advance. Uses its own DB session (the request's is long gone)."""
    try:
        await asyncio.sleep(limit)
    except asyncio.CancelledError:
        return
    try:
        async with async_session_maker() as session:
            quiz = await quiz_service.get_quiz(session, quiz_id)
            if quiz is None or quiz.status != "running":
                return
            question = next((q for q in quiz.questions if q.id == question_id), None)
            if question is None:
                return
            # -1 = no option chosen → never matches the correct id → wrong.
            outcome = await quiz_service.record_answer(
                session, quiz_id, question_id, user_tg_id, -1, response_ms=limit * 1000
            )
            if outcome is None or not outcome.recorded:
                return  # the user answered just in time — nothing to do
            await session.commit()

            options = quiz_service.question_options(question)
            correct_label = esc(options[outcome.correct_option_id])
            feedback = (
                f"⏱️ <b>Tempo scaduto!</b> Nessuna risposta.\n✅ Giusta: <b>{correct_label}</b>"
            )
            if question.explanation:
                feedback += f"\n\n💡 <i>{esc(question.explanation)}</i>"
            try:
                await bot.edit_message_text(
                    f"❓ {esc(question.text)}\n\n{feedback}",
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except Exception:  # noqa: BLE001 — message may be too old to edit
                pass
            await _advance_or_finish(bot, chat_id, user_tg_id, quiz, session)
    except Exception:  # noqa: BLE001 — a timer must never crash the event loop
        log.exception("Timer domanda quiz %s/%s fallito", quiz_id, question_id)


@router.callback_query(QuizAnswerCb.filter(F.action == "answer"))
async def cb_quiz_answer(
    callback: CallbackQuery, callback_data: QuizAnswerCb, db_session: AsyncSession
) -> None:
    quiz_id = callback_data.quiz_id
    question_id = callback_data.question_id
    opt_idx = callback_data.option_id

    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or quiz.status != "running":
        await callback.answer("⚠️ Questo quiz non è più disponibile.", show_alert=True)
        return

    question = next((q for q in quiz.questions if q.id == question_id), None)
    if question is None:
        await callback.answer("Domanda non valida.", show_alert=True)
        return
    options = quiz_service.question_options(question)
    if not (0 <= opt_idx < len(options)):
        await callback.answer("Opzione non valida.", show_alert=True)
        return

    user_tg_id = callback.from_user.id
    # Enforce sequential play: only the user's current question (in THEIR order,
    # which may be randomized — §19) is answerable.
    order = quiz_service.user_question_order(quiz, user_tg_id)
    idx_in_order = next((i for i, q in enumerate(order) if q.id == question.id), -1)
    done_before = await quiz_service.answered_count(db_session, quiz_id, user_tg_id)
    if idx_in_order != done_before:
        await callback.answer(
            "Hai già risposto a questa domanda."
            if idx_in_order < done_before
            else "Rispondi prima alla domanda corrente."
        )
        return

    # Measure the response time, then stop this question's countdown before recording.
    resp_ms = _response_ms(quiz_id, user_tg_id, question_id, question.open_period)
    _cancel_timer(quiz_id, user_tg_id)
    outcome = await quiz_service.record_answer(
        db_session, quiz_id, question_id, user_tg_id, opt_idx, response_ms=resp_ms
    )
    if outcome is None:
        await callback.answer("Domanda non valida.", show_alert=True)
        return
    if not outcome.recorded:
        await callback.answer("Hai già risposto a questa domanda.")
        return
    await db_session.commit()

    # Feedback on the answered message (buttons removed).
    chosen = esc(options[opt_idx])
    if outcome.is_correct:
        feedback = f"✅ <b>Esatto!</b> — {chosen}"
    else:
        correct_label = esc(options[outcome.correct_option_id])
        feedback = f"❌ <b>Sbagliato.</b> Hai scelto: {chosen}\n✅ Giusta: <b>{correct_label}</b>"
    if question.explanation:
        feedback += f"\n\n💡 <i>{esc(question.explanation)}</i>"
    try:
        await callback.message.edit_text(f"❓ {esc(question.text)}\n\n{feedback}")
    except Exception:  # noqa: BLE001 — editing may fail if message is too old
        await callback.message.answer(feedback)

    await _advance_or_finish(callback.bot, callback.message.chat.id, user_tg_id, quiz, db_session)
    await callback.answer()
