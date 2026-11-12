"""Private native quizzes: SQL owns delivery, timing and answer deduplication.

Telegram closes polls and emits poll updates. Existing durable ScheduledTasks
recover missed expiries after downtime; there are no process-local quiz timers.
"""

from __future__ import annotations

from datetime import timedelta, timezone

from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, Poll, PollAnswer, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from handlers.callbacks import QuizAnswerCb
from handlers.quiz import _native
from handlers.quiz._shared import router
from services import quiz_poll_service as polls, quiz_service, schedule_service
from utils.text import esc, format_seconds_short


async def start_quiz_session(message: Message, db_session: AsyncSession, quiz_id: int) -> None:
    """Deep-link quiz_<id>: start or resume without re-sending an open question."""
    if message.chat.type != "private":
        return
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    await db_session.commit()
    if quiz is None or not quiz.questions:
        await message.answer("⚠️ Quiz non trovato.")
        return
    if quiz.status == "finished":
        await message.answer("🏁 Questo quiz è già terminato.")
        return
    if quiz.status != "running":
        await message.answer("⏳ Questo quiz non è ancora iniziato. Aspetta che un admin lo avvii.")
        return
    limit = quiz_service.time_limit_seconds(quiz)
    rules = (
        f"Hai <b>{limit} secondi</b> per domanda; allo scadere conta come sbagliata."
        if limit
        else "Nessun limite di tempo."
    )
    desc = f"\n<i>{esc(quiz.description)}</i>" if quiz.description else ""
    await message.answer(
        f"🧠 <b>{esc(quiz.title)}</b>{desc}\n{rules}\n"
        "A parità di risposte corrette vince chi impiega meno tempo."
    )
    del quiz  # do not pin a stale run in the identity map across the network
    await advance(message.bot, db_session, quiz_id, message.from_user.id)


async def advance(bot: Bot, db_session: AsyncSession, quiz_id: int, user_id: int) -> None:
    """Claim before the network; attach the poll in another short transaction."""
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or quiz.status != "running":
        return
    delivery, owned = await polls.claim(db_session, quiz, user_id, user_id)
    if delivery is None:
        done = await quiz_service.answered_count(db_session, quiz.id, user_id)
        if done < len(quiz.questions):
            return
        correct = await quiz_service.correct_count(db_session, quiz.id, user_id)
        secs = await quiz_service.user_completion_seconds(db_session, quiz.id, user_id)
        timing = (
            f"\n⏱️ Tempo impiegato: <b>{format_seconds_short(secs)}</b>" if secs is not None else ""
        )
        await db_session.commit()
        await bot.send_message(
            user_id,
            f"🏁 <b>Quiz completato!</b>\n\n<b>{correct}/{len(quiz.questions)}</b> corrette."
            f"{timing}\nAspetta la chiusura per scoprire il podio! 🏆",
        )
        return
    if not owned:
        await db_session.commit()
        if delivery.state == "open":
            if delivery.expires_at is not None and delivery.expires_at <= schedule_service.utcnow():
                await expire(bot, db_session, delivery.token)
            else:
                await bot.send_message(
                    user_id,
                    "👆 Rispondi alla domanda già aperta: il timer non riparte.",
                    reply_parameters=ReplyParameters(
                        message_id=delivery.message_id,
                        allow_sending_without_reply=True,
                    ),
                )
        else:
            await bot.send_message(
                user_id, "⏳ Sto preparando la domanda. Riprova tra un minuto se non arriva."
            )
        return
    question = next(q for q in quiz.questions if q.id == delivery.question_id)
    options = quiz_service.question_options(question)
    ordered = [(real_id, options[real_id]) for real_id in delivery.option_order]
    index = quiz_service.user_question_order(quiz, user_id).index(question)
    await db_session.commit()
    try:
        sent = await _native.send_question(
            bot,
            user_id,
            question,
            ordered,
            index=index,
            total=len(quiz.questions),
            time_limit=question.open_period,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        await polls.release_rejected(db_session, delivery.token)
        await db_session.commit()
        raise
    # The server date anchors the deadline; our high-resolution delivery ack
    # measures answers across restarts without charging the send's SQL/API time.
    stamp = sent.date.astimezone(timezone.utc).replace(tzinfo=None)
    deadline = stamp + timedelta(seconds=question.open_period) if question.open_period else None
    published = await polls.publish(
        db_session,
        delivery,
        poll_id=sent.poll.id,
        message_id=sent.message_id,
        shown_at=schedule_service.utcnow(),
        expires_at=deadline,
    )
    await db_session.commit()
    if not published:
        await _native.stop_question(bot, user_id, sent.message_id)


async def on_answer(answer: PollAnswer, bot: Bot, db_session: AsyncSession) -> bool:
    """True means a real-quiz poll owns this id, even for a stale/foreign vote."""
    received_at = schedule_service.utcnow()
    delivery = await polls.get(db_session, tg_poll_id=answer.poll_id)
    if delivery is None:
        return False
    user = answer.user
    if user is None or user.is_bot or user.id != delivery.user_tg_id or len(answer.option_ids) != 1:
        return True
    if not await polls.answer(db_session, delivery, answer.option_ids[0], now=received_at):
        return True
    await db_session.commit()
    await _native.stop_question(bot, delivery.chat_id, delivery.message_id)
    await advance(bot, db_session, delivery.quiz_id, delivery.user_tg_id)
    return True


async def prepare_expiry(db_session: AsyncSession, token: str):
    """DB-only half, reusable by a native update and a scheduler post-commit hook."""
    delivery = await polls.get(db_session, token=token)
    if delivery is None or not await polls.answer(db_session, delivery, None):
        return None
    return delivery


async def finish_expiry(bot: Bot, db_session: AsyncSession, delivery: polls.Delivery) -> None:
    # Native timed polls are already closed server-side: no redundant stopPoll
    # request (or normal "already closed" error) for every missed question.
    await bot.send_message(delivery.chat_id, "⏱️ Tempo scaduto! Nessuna risposta.")
    await advance(bot, db_session, delivery.quiz_id, delivery.user_tg_id)


async def expire(bot: Bot, db_session: AsyncSession, token: str) -> None:
    delivery = await prepare_expiry(db_session, token)
    await db_session.commit()
    if delivery is not None:
        await finish_expiry(bot, db_session, delivery)


@router.poll(F.is_closed)
async def on_poll_closed(poll: Poll, bot: Bot, db_session: AsyncSession) -> None:
    # A close with a vote can race its PollAnswer: only that authenticated update
    # is allowed to award correctness. The fallback task handles a missing vote.
    if poll.total_voter_count:
        return
    delivery = await polls.get(db_session, tg_poll_id=poll.id)
    if delivery is not None:
        await expire(bot, db_session, delivery.token)


@router.callback_query(QuizAnswerCb.filter(F.action == "answer"))
async def cb_quiz_answer(callback: CallbackQuery, callback_data: QuizAnswerCb) -> None:
    # Retire old keyboards safely: a pre-deploy button must not bypass the new
    # poll owner/deadline checks, even for a quiz that is still running.
    await callback.answer(
        "Questi pulsanti sono superati. Riapri il quiz da /quiz.", show_alert=True
    )
