"""Native quiz UI and durable delivery: no real Telegram calls."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPoll
from sqlalchemy import select, update

from database.models import QuizAnswer, QuizPoll, ScheduledTask
from handlers.callbacks import QuizAnswerCb
from handlers.poll_vote import on_poll_answer
from handlers.quiz import play
from services import quiz_poll_service as polls, quiz_service, schedule_service
from tests.quiz_support import Bot, Callback, Message, answer, make_quiz


async def current(session, quiz_id, user=42):
    return (
        (
            await session.execute(
                select(QuizPoll)
                .where(QuizPoll.quiz_id == quiz_id, QuizPoll.user_tg_id == user)
                .order_by(QuizPoll.id.desc())
            )
        )
        .scalars()
        .first()
    )


async def started(session, **kw):
    quiz = await make_quiz(session, **kw)
    bot = Bot()
    await play.start_quiz_session(Message(bot), session, quiz.id)
    return quiz, bot, await current(session, quiz.id)


async def test_native_request_has_complete_answers_and_telegram_feedback(session):
    quiz, bot, row = await started(session, limit=30)
    sent = bot.polls[0]
    assert sent["question"] == quiz.questions[0].text
    assert [o.text for o in sent["options"]] == ["A" * 100, "B" * 65, "C"]
    assert sent["description"] == "Domanda 1/2"
    assert sent["type"] == "quiz" and sent["is_anonymous"] is False
    assert sent["correct_option_ids"] == [0]
    assert not sent["allows_revoting"] and not sent["allows_multiple_answers"]
    assert sent["protect_content"] is True and sent["open_period"] == 30
    assert sent["explanation"] == "Perché è così."
    assert sent["explanation_parse_mode"] is None
    assert row.state == "open" and row.expires_at is not None
    task = (await session.execute(select(ScheduledTask))).scalar_one()
    assert schedule_service.task_payload(task) == {
        "action": "expire",
        "internal": True,
        "poll_token": row.token,
    }
    assert await schedule_service.list_pending(session) == []


async def test_resume_keeps_original_poll_and_deadline(session):
    quiz, bot, row = await started(session, limit=30)
    original = (row.tg_poll_id, row.shown_at, row.expires_at)
    await play.start_quiz_session(Message(bot), session, quiz.id)
    assert len(bot.polls) == 1
    assert "timer non riparte" in bot.said
    refreshed = await polls.get(session, token=row.token)
    assert (refreshed.tg_poll_id, refreshed.shown_at, refreshed.expires_at) == original
    assert bot.messages[-1][2]["reply_parameters"].message_id == row.message_id


@pytest.mark.parametrize("choice,correct", [(0, 1), (1, 0)])
async def test_answer_records_once_advances_and_ignores_duplicates(session, choice, correct):
    quiz, bot, row = await started(session)
    vote = answer(row.tg_poll_id, [choice])
    await on_poll_answer(vote, session, bot)
    assert await quiz_service.answered_count(session, quiz.id, 42) == 1
    assert await quiz_service.correct_count(session, quiz.id, 42) == correct
    assert len(bot.polls) == 2
    await on_poll_answer(vote, session, bot)
    assert len(bot.polls) == 2
    assert await quiz_service.answered_count(session, quiz.id, 42) == 1


@pytest.mark.parametrize("options,user", [([], 42), ([0, 1], 42), ([9], 42), ([0], 999)])
async def test_invalid_or_foreign_vote_does_not_consume(session, options, user):
    quiz, bot, row = await started(session)
    await on_poll_answer(answer(row.tg_poll_id, options, user), session, bot)
    assert await quiz_service.answered_count(session, quiz.id, 42) == 0
    assert len(bot.polls) == 1


async def test_shuffled_options_and_questions_keep_real_answer_mapping(session):
    quiz, bot, row = await started(session, shuffle=True, count=4)
    order = quiz_service.user_question_order(quiz, 42)
    for index, question in enumerate(order):
        row = await current(session, quiz.id)
        delivery = await polls.get(session, token=row.token)
        displayed = delivery.option_order.index(question.correct_option_id)
        assert bot.polls[index]["correct_option_ids"] == [displayed]
        await on_poll_answer(answer(row.tg_poll_id, [displayed]), session, bot)
    assert await quiz_service.correct_count(session, quiz.id, 42) == 4
    assert "Quiz completato" in bot.said and "4/4" in bot.said
    assert len(bot.polls) == 4
    await play.start_quiz_session(Message(bot), session, quiz.id)
    assert len(bot.polls) == 4


async def test_different_players_never_share_a_poll(session):
    quiz, bot, first = await started(session)
    await play.start_quiz_session(Message(bot, user_id=77), session, quiz.id)
    second = await current(session, quiz.id, 77)
    assert first.tg_poll_id != second.tg_poll_id
    await on_poll_answer(answer(first.tg_poll_id, user_id=77), session, bot)
    assert await quiz_service.answered_count(session, quiz.id, 77) == 0


@pytest.mark.parametrize(
    "status,fragment", [("ready", "non è ancora iniziato"), ("finished", "già terminato")]
)
async def test_unavailable_quiz_explains_without_sending_poll(session, status, fragment):
    quiz = await make_quiz(session, status=status)
    bot = Bot()
    await play.start_quiz_session(Message(bot), session, quiz.id)
    assert not bot.polls and fragment in bot.said


async def test_missing_empty_or_group_quiz_never_sends_poll(session):
    bot = Bot()
    await play.start_quiz_session(Message(bot), session, 999)
    assert "non trovato" in bot.said
    quiz = await make_quiz(session, count=0)
    await play.start_quiz_session(Message(bot), session, quiz.id)
    await play.start_quiz_session(Message(bot, chat_type="supergroup"), session, quiz.id)
    await play.advance(bot, session, 999, 42)
    assert not bot.polls


async def test_closed_quiz_and_old_inline_keyboards_cannot_change_podium(session):
    quiz, bot, row = await started(session)
    await quiz_service.claim_close(session, quiz.id)
    await session.commit()
    await on_poll_answer(answer(row.tg_poll_id), session, bot)
    assert await quiz_service.answered_count(session, quiz.id, 42) == 0
    callback = Callback(Message(bot))
    await play.cb_quiz_answer(
        callback,
        QuizAnswerCb(
            action="answer",
            quiz_id=quiz.id,
            question_id=row.question_id,
            option_id=0,
        ),
    )
    assert "superati" in callback.answers[-1]


async def test_timing_persists_without_in_memory_context_and_is_capped(session):
    quiz, bot, row = await started(session, limit=30)
    await session.execute(
        update(QuizPoll)
        .where(QuizPoll.token == row.token)
        .values(
            shown_at=schedule_service.utcnow() - timedelta(hours=1),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await on_poll_answer(answer(row.tg_poll_id), session, bot)
    stored = (await session.execute(select(QuizAnswer))).scalar_one()
    assert stored.response_ms == 30000


async def test_poll_closure_expires_once_and_advances(session):
    quiz, bot, row = await started(session, limit=5)
    await session.execute(
        update(QuizPoll)
        .where(QuizPoll.token == row.token)
        .values(
            expires_at=schedule_service.utcnow() - timedelta(seconds=1),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    closed = SimpleNamespace(id=row.tg_poll_id, is_closed=True, total_voter_count=0)
    await play.on_poll_closed(closed, bot, session)
    await play.on_poll_closed(closed, bot, session)
    assert await quiz_service.answered_count(session, quiz.id, 42) == 1
    stored = (await session.execute(select(QuizAnswer))).scalar_one()
    assert stored.selected_option_id == -1 and stored.response_ms == 5000
    assert "Tempo scaduto" in bot.said and len(bot.polls) == 2
    await on_poll_answer(answer(row.tg_poll_id), session, bot)
    assert await quiz_service.correct_count(session, quiz.id, 42) == 0


async def test_close_update_with_vote_waits_for_authenticated_answer(session):
    quiz, bot, row = await started(session, limit=30)
    await play.on_poll_closed(SimpleNamespace(id=row.tg_poll_id, total_voter_count=1), bot, session)
    await play.on_poll_closed(SimpleNamespace(id="unknown", total_voter_count=0), bot, session)
    assert await quiz_service.answered_count(session, quiz.id, 42) == 0
    await on_poll_answer(answer(row.tg_poll_id), session, bot)
    assert await quiz_service.correct_count(session, quiz.id, 42) == 1


async def test_resume_after_downtime_expires_original_question_not_a_new_timer(session):
    quiz, bot, row = await started(session, limit=5, count=1)
    await session.execute(
        update(QuizPoll)
        .where(QuizPoll.token == row.token)
        .values(
            expires_at=schedule_service.utcnow() - timedelta(minutes=10),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await play.start_quiz_session(Message(bot), session, quiz.id)
    assert len(bot.polls) == 1 and "Quiz completato" in bot.said
    assert await quiz_service.correct_count(session, quiz.id, 42) == 0


async def test_stop_failure_does_not_block_next_question(session):
    quiz, bot, row = await started(session)
    bot.stop_error = TimeoutError("network")
    await on_poll_answer(answer(row.tg_poll_id), session, bot)
    assert len(bot.polls) == 2
    assert await quiz_service.answered_count(session, quiz.id, 42) == 1


async def test_no_database_transaction_is_held_over_telegram(session):
    quiz = await make_quiz(session, count=1)
    bot = Bot(session)
    await play.start_quiz_session(Message(bot), session, quiz.id)
    row = await current(session, quiz.id)
    await on_poll_answer(answer(row.tg_poll_id), session, bot)
    assert "Quiz completato" in bot.said


async def test_rejected_send_releases_claim_but_uncertain_send_keeps_lease(session):
    quiz = await make_quiz(session)
    bot = Bot()
    bot.send_error = TelegramBadRequest(
        method=SendPoll(chat_id=42, question="Q", options=[]), message="rejected"
    )
    with pytest.raises(TelegramBadRequest):
        await play.start_quiz_session(Message(bot), session, quiz.id)
    assert await current(session, quiz.id) is None
    bot.send_error = TimeoutError("unknown outcome")
    with pytest.raises(TimeoutError):
        await play.start_quiz_session(Message(bot), session, quiz.id)
    row = await current(session, quiz.id)
    assert row.state == "sending"
    bot.send_error = None
    await play.start_quiz_session(Message(bot), session, quiz.id)
    assert not bot.polls and "preparando" in bot.said


async def test_delayed_publish_of_revoked_claim_closes_orphan_poll(session, monkeypatch):
    quiz = await make_quiz(session)
    bot = Bot()

    async def lost(*args, **kwargs):
        return False

    monkeypatch.setattr(polls, "publish", lost)
    await play.start_quiz_session(Message(bot), session, quiz.id)
    assert bot.stopped == [(42, 1)]


async def test_claim_fences_double_start_and_late_publish(session):
    quiz = await make_quiz(session)
    first, owned = await polls.claim(session, quiz, 42, 42)
    assert owned
    same, owned = await polls.claim(session, quiz, 42, 42)
    assert not owned and same.token == first.token
    await session.execute(
        update(QuizPoll)
        .where(QuizPoll.token == first.token)
        .values(
            lease_until=schedule_service.utcnow() - timedelta(seconds=1),
        )
        .execution_options(synchronize_session=False)
    )
    second, owned = await polls.claim(session, quiz, 42, 42)
    assert owned and second.token != first.token
    assert not await polls.publish(
        session, first, poll_id="old", message_id=1, shown_at=first.shown_at, expires_at=None
    )
    assert await polls.publish(
        session, second, poll_id="new", message_id=2, shown_at=second.shown_at, expires_at=None
    )
    assert not await polls.publish(
        session, second, poll_id="repeat", message_id=3, shown_at=second.shown_at, expires_at=None
    )
    assert not (await polls.claim(session, quiz, 42, -100))[1]


async def test_service_rejects_invalid_early_missing_and_non_current_answers(session):
    quiz, bot, row = await started(session, limit=30)
    delivery = await polls.get(session, token=row.token)
    assert not await polls.answer(session, delivery, None)
    assert not await polls.answer(session, replace(delivery, token="unknown"), 0)
    assert not await polls.answer(session, delivery, 99)
    # An out-of-order poll cannot become a correct answer.
    await session.execute(
        update(QuizPoll)
        .where(QuizPoll.token == row.token)
        .values(
            question_id=quiz.questions[1].id,
        )
        .execution_options(synchronize_session=False)
    )
    assert not await polls.answer(session, delivery, 0)
    await session.commit()


async def test_reset_and_delete_remove_poll_handles_and_reject_old_votes(session):
    quiz, bot, row = await started(session)
    token, poll_id = row.token, row.tg_poll_id
    await quiz_service.claim_close(session, quiz.id)
    await quiz_service.reset_quiz(session, quiz.id)
    await session.commit()
    assert await polls.get(session, token=token) is None
    assert not await play.on_answer(answer(poll_id), bot, session)
    await quiz_service.set_status(session, quiz.id, "running")
    await session.commit()
    await play.start_quiz_session(Message(bot), session, quiz.id)
    assert (await current(session, quiz.id)).token != token
    await quiz_service.delete_quiz(session, quiz.id)
    await session.commit()
    assert await current(session, quiz.id) is None
    assert await polls.claim(session, quiz, 42, 42) == (None, False)


async def test_missing_and_closed_advance_do_nothing(session):
    quiz = await make_quiz(session, status="finished")
    bot = Bot()
    await play.advance(bot, session, quiz.id, 42)
    assert not bot.polls
    assert not await polls.publish(
        session,
        SimpleNamespace(quiz_id=999),
        poll_id="x",
        message_id=1,
        shown_at=schedule_service.utcnow(),
        expires_at=None,
    )


async def test_failed_delivery_claim_does_not_send_question(session, monkeypatch):
    quiz = await make_quiz(session)
    bot = Bot()

    async def no_claim(*args):
        return None, False

    monkeypatch.setattr(polls, "claim", no_claim)
    await play.advance(bot, session, quiz.id, 42)
    assert not bot.messages and not bot.polls


async def test_lost_conditional_consume_never_records_answer(session, monkeypatch):
    from sqlalchemy.sql.dml import Update

    quiz, bot, row = await started(session)
    delivery = await polls.get(session, token=row.token)
    original = session.execute

    async def lost(stmt, *args, **kwargs):
        if isinstance(stmt, Update) and stmt.table.name == "quiz_polls":
            return SimpleNamespace(rowcount=0)
        return await original(stmt, *args, **kwargs)

    monkeypatch.setattr(session, "execute", lost)
    assert not await polls.answer(session, delivery, 0)
    assert await quiz_service.answered_count(session, quiz.id, 42) == 0


async def test_real_aiogram_dispatcher_routes_poll_answer_and_injects_bot(session):
    from aiogram import Dispatcher, Router
    from aiogram.types import Update

    quiz, bot, row = await started(session, count=1)
    dispatcher = Dispatcher()
    router = Router()  # never attach the application's singleton routers in tests
    router.poll_answer.register(on_poll_answer)
    dispatcher.include_router(router)
    try:
        await dispatcher.feed_update(
            bot, Update(update_id=999, poll_answer=answer(row.tg_poll_id)),
            db_session=session,
        )
    finally:
        await dispatcher.storage.close()
    assert await quiz_service.correct_count(session, quiz.id, 42) == 1
    assert "Quiz completato" in bot.said
