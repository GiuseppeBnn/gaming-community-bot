"""Admin native poll dry-run stays structurally outside scores and prizes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from database.models import QuizAnswer, QuizPoll, ScheduledTask
from handlers.callbacks import QuizTryCb
from handlers.poll_vote import on_poll_answer
from handlers.quiz import trying as trial
from services import quiz_service
from tests.quiz_support import Bot, Callback, Message, answer, make_quiz


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    from config_data.config import settings

    monkeypatch.setattr(settings, "admin_ids", [42])
    trial._TRY.clear()
    yield
    trial._TRY.clear()


async def start(session, **kw):
    quiz = await make_quiz(session, status="ready", **kw)
    bot = Bot()
    # This is a BOT-authored message, as in production callback entry points.
    msg = Message(bot, user_id=42)
    msg.from_user = SimpleNamespace(id=777, is_bot=True)
    cb = Callback(msg, user_id=42)
    await trial.cb_try_start(cb, QuizTryCb(action="start", quiz_id=quiz.id), session)
    return quiz, bot, msg


async def test_native_dry_run_never_writes_scores_deliveries_or_tasks(session):
    quiz, bot, msg = await start(session, shuffle=True, limit=30)
    assert (quiz.id, 42) in trial._TRY and (quiz.id, 777) not in trial._TRY
    assert "MODALITÀ PROVA" in bot.said
    for _ in range(2):
        ctx = trial._TRY[(quiz.id, 42)]
        assert bot.polls[-1]["description"].startswith("🧪 PROVA")
        assert bot.polls[-1]["open_period"] is None
        correct = bot.polls[-1]["correct_option_ids"][0]
        await on_poll_answer(answer(ctx.poll_id, [correct]), session, bot)
    assert "Prova completata" in bot.said and "2/2" in bot.said
    assert not trial._TRY
    for model in (QuizAnswer, QuizPoll, ScheduledTask):
        assert (await session.execute(select(func.count()).select_from(model))).scalar_one() == 0
    assert await quiz_service.podium(session, quiz.id) == []
    assert await quiz_service.award_prizes(session, quiz.id) == []


async def test_wrong_answer_and_duplicate_are_not_recounted(session):
    quiz, bot, msg = await start(session)
    ctx = trial._TRY[(quiz.id, 42)]
    vote = answer(ctx.poll_id, [1])
    await on_poll_answer(vote, session, bot)
    await on_poll_answer(vote, session, bot)
    assert ctx.index == 1 and ctx.correct == 0 and len(bot.polls) == 2
    await on_poll_answer(answer(ctx.poll_id, [1]), session, bot)
    assert "1/2" in bot.said


@pytest.mark.parametrize("options,user", [([], 42), ([0, 1], 42), ([9], 42), ([0], 99)])
async def test_invalid_and_foreign_answers_cannot_advance(session, options, user):
    quiz, bot, msg = await start(session)
    ctx = trial._TRY[(quiz.id, 42)]
    await on_poll_answer(answer(ctx.poll_id, options, user), session, bot)
    assert ctx.index == 0 and len(bot.polls) == 1


async def test_admin_revocation_stops_trial(session, monkeypatch):
    quiz, bot, msg = await start(session)
    ctx = trial._TRY[(quiz.id, 42)]

    async def denied(*args):
        return False

    monkeypatch.setattr(trial, "is_admin", denied)
    await on_poll_answer(answer(ctx.poll_id), session, bot)
    assert not trial._TRY and len(bot.polls) == 1


async def test_stop_and_restart_retire_old_poll(session):
    quiz, bot, msg = await start(session)
    old = trial._TRY[(quiz.id, 42)].poll_id
    await trial.cb_try_start(Callback(msg), QuizTryCb(action="start", quiz_id=quiz.id), session)
    assert bot.stopped == [(42, 1)]
    assert trial._TRY[(quiz.id, 42)].poll_id != old
    await on_poll_answer(answer(old), session, bot)
    assert trial._TRY[(quiz.id, 42)].index == 0
    await trial.cb_try_stop(Callback(msg), QuizTryCb(action="stop", quiz_id=quiz.id))
    assert not trial._TRY and "interrotta" in bot.said
    await trial.cb_try_stop(Callback(msg), QuizTryCb(action="stop", quiz_id=quiz.id))


@pytest.mark.parametrize("status", ["running", "finished"])
async def test_non_ready_quiz_is_refused(session, status):
    quiz = await make_quiz(session, status=status)
    msg = Message()
    await trial.start_quiz_try(msg, session, quiz.id, 42)
    assert "solo per un quiz" in msg.bot.said and not trial._TRY


async def test_missing_empty_and_group_quizzes_do_not_start(session):
    msg = Message()
    await trial.start_quiz_try(msg, session, 999, 42)
    assert "non trovato" in msg.bot.said
    quiz = await make_quiz(session, status="ready", count=0)
    await trial.start_quiz_try(msg, session, quiz.id, 42)
    await trial.start_quiz_try(Message(chat_type="group"), session, quiz.id, 42)
    assert not trial._TRY and not msg.bot.polls


@pytest.mark.parametrize("delete_quiz", [False, True])
async def test_quiz_started_or_deleted_during_trial_stops_safely(session, delete_quiz):
    quiz, bot, msg = await start(session)
    ctx = trial._TRY[(quiz.id, 42)]
    if delete_quiz:
        await quiz_service.delete_quiz(session, quiz.id)
    else:
        await quiz_service.set_status(session, quiz.id, "running")
    await session.commit()
    await on_poll_answer(answer(ctx.poll_id), session, bot)
    assert not trial._TRY and "non è più disponibile" in bot.said


async def test_deleted_current_question_ends_trial_without_score(session):
    quiz, bot, msg = await start(session)
    ctx = trial._TRY[(quiz.id, 42)]
    ctx.order[0] = 999
    await on_poll_answer(answer(ctx.poll_id), session, bot)
    assert not trial._TRY and "Prova completata" in bot.said


async def test_missing_next_question_finishes_and_legacy_callbacks_are_retired(session):
    quiz, bot, msg = await start(session)
    ctx = trial._TRY[(quiz.id, 42)]
    ctx.order[1] = 999
    await on_poll_answer(answer(ctx.poll_id), session, bot)
    assert not trial._TRY
    cb = Callback(msg)
    await trial.cb_try_answer(cb, QuizTryCb(action="answer", quiz_id=quiz.id))
    assert "quiz nativi" in cb.answers[-1]


async def test_native_trial_reuses_presenter_without_holding_transaction(session):
    quiz = await make_quiz(session, status="ready", count=1)
    bot = Bot(session)
    msg = Message(bot)
    await trial.start_quiz_try(msg, session, quiz.id, 42)
    ctx = trial._TRY[(quiz.id, 42)]
    await on_poll_answer(answer(ctx.poll_id), session, bot)
    assert "Prova completata" in bot.said


async def test_trial_stopped_while_poll_is_sending_closes_orphan(session):
    quiz = await make_quiz(session, status="ready")
    bot = Bot()
    original = bot.send_poll

    async def interrupted(**kwargs):
        sent = await original(**kwargs)
        trial._TRY.clear()
        return sent

    bot.send_poll = interrupted
    await trial.start_quiz_try(Message(bot), session, quiz.id, 42)
    assert not trial._TRY and bot.stopped == [(42, 1)]


async def test_stale_trial_cannot_pop_replacement_or_process_old_vote(session, monkeypatch):
    quiz, bot, msg = await start(session)
    old = trial._TRY[(quiz.id, 42)]
    replacement = trial._TryCtx(quiz.id, [999])

    async def restarted(*args):
        trial._TRY[(quiz.id, 42)] = replacement
        return True

    monkeypatch.setattr(trial, "is_admin", restarted)
    await on_poll_answer(answer(old.poll_id), session, bot)
    await trial._finish(bot, 42, old)
    assert trial._TRY[(quiz.id, 42)] is replacement
    assert old.index == 0 and len(bot.polls) == 1


async def test_trial_stopped_during_commit_does_not_send_next_poll(session, monkeypatch):
    quiz, bot, msg = await start(session)
    old = trial._TRY[(quiz.id, 42)]
    vote = answer(old.poll_id)
    original = session.commit

    async def stopped():
        await original()
        trial._TRY.clear()

    monkeypatch.setattr(session, "commit", stopped)
    assert await trial.on_answer(vote, bot, session)
    assert not trial._TRY and len(bot.polls) == 1
