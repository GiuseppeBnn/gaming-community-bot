"""Integration tests for QuizType.execute_scheduled (scheduler adapter).

A scheduled quiz-open that finds the quiz already ``running`` (e.g. an admin
started it by hand before the scheduled time) must be a *skip*, not a failure:
it raises ``schedule_service.TaskSkip`` so the loop marks the task done and DMs
the creator, instead of marking it failed. Genuine failures still raise.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import services.quiz_service as qz
from database.models import Quiz
from handlers.event_types.quiz_type import QuizType
from services import group_registry
from services.schedule_service import TaskSkip


def _task(ref_id: int, *, action: str | None = None):
    """A stand-in for the ScheduledTask row. ``payload_json`` is spelled out even
    when empty: it is what tells an open apart from a scheduled close."""
    return SimpleNamespace(
        ref_id=ref_id, task_type="quiz", group_id=123, id=7,
        payload_json=None if action is None else json.dumps({"action": action}),
    )


async def _ready_quiz(session):
    quiz = await qz.create_quiz(session, 9, "Capitali", "Geo")
    await qz.add_question(
        session, quiz.id, "Q", ["Roma", "Milano"], correct_option_id=0, explanation=None
    )
    await qz.set_status(session, quiz.id, "ready")
    await session.commit()
    return quiz


async def test_scheduled_open_skips_when_already_running(session):
    quiz = await _ready_quiz(session)
    await qz.set_status(session, quiz.id, "running")
    await session.commit()

    with pytest.raises(TaskSkip):
        await QuizType().execute_scheduled(None, session, _task(quiz.id), 123)


async def test_a_scheduled_close_closes_the_running_quiz(session, monkeypatch):
    """Same task_type, `action=close` in the payload: the podium is published on the
    clock the admin set, without anyone having to be awake for it."""
    quiz = await _ready_quiz(session)
    await qz.set_status(session, quiz.id, "running")
    await session.commit()
    monkeypatch.setattr(group_registry, "get_group_id", lambda: 0)  # no group to post to

    await QuizType().execute_scheduled(None, session, _task(quiz.id, action="close"), 123)

    # Read the column, not the entity: `claim_close` writes with
    # synchronize_session=False, so the instance in the identity map still says
    # «running» (STEERING §22).
    status = (
        await session.execute(select(Quiz.status).where(Quiz.id == quiz.id))
    ).scalar_one()
    assert status == "finished"


async def test_a_scheduled_close_on_a_quiz_that_is_not_running_is_a_skip(session):
    """It was closed by hand, or never started: the end state is the one asked for
    either way, so it must not be logged as a failure."""
    quiz = await _ready_quiz(session)

    with pytest.raises(TaskSkip):
        await QuizType().execute_scheduled(None, session, _task(quiz.id, action="close"), 123)


async def test_scheduled_open_failure_raises_runtimeerror(session, monkeypatch):
    # Not running → not a skip. Force a genuine open_quiz failure (no group) and
    # check it surfaces as RuntimeError (the loop will then DM the admin).
    quiz = await _ready_quiz(session)
    monkeypatch.setattr(group_registry, "get_group_id", lambda: 0)

    with pytest.raises(RuntimeError):
        await QuizType().execute_scheduled(None, session, _task(quiz.id), 123)
