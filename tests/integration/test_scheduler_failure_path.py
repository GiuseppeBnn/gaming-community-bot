"""The scheduler's failure path, against a real database and a real ORM object.

`tests/unit/test_scheduler_loop.py` pins the control flow with fakes. This file pins
the part fakes cannot see: what happens to a **real** `ScheduledTask` instance after
`session.rollback()`.

## Why this file exists

`_run_due_task`'s docstring says the id and type are captured up front «because
reading them off the ORM object after a rollback would trigger an implicit reload
(illegal in async)». That is true and measurable — after a rollback that had work to
undo, touching any attribute of the instance raises `MissingGreenlet`:

    task.status = "running"; await session.flush(); await session.rollback()
    task.created_by_tg_id   → MissingGreenlet

Task 13 closes that hazard instead of relying on an incidental ORM refresh:
`_run_due_task` copies id, creator and payload before risk/rollback, persists its
outcome with a scalar-ID update, then notifies with primitives. The live instance may
remain expired forever; its readability is neither assumed nor needed.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from database.models import ScheduledTask
from handlers import schedule
from services import schedule_service


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


async def _pending_task(session, *, created_by: int = 999) -> ScheduledTask:
    session.add(
        ScheduledTask(
            task_type="quiz", ref_id=1, run_at=datetime(2026, 1, 1),
            group_id=123, created_by_tg_id=created_by, status="pending",
        )
    )
    await session.commit()
    return (await session.execute(select(ScheduledTask))).scalar_one()


def _raising_execute(exc: BaseException):
    """An `execute_task` that leaves pending work behind before failing — which is
    what makes the following rollback a real one with something to undo."""
    async def _execute(bot, session, task):
        task.status = "running"
        await session.flush()
        raise exc
    return _execute


class TestFailurePathOnARealSession:
    async def test_a_failed_task_is_recorded_and_the_creator_is_told(
        self, session, monkeypatch
    ):
        task = await _pending_task(session)
        monkeypatch.setattr(
            schedule, "execute_task", _raising_execute(RuntimeError("spec exploded"))
        )
        bot = _FakeBot()

        await schedule._run_due_task(bot, session, task)  # must not raise

        stored = (await session.execute(select(ScheduledTask))).scalar_one()
        assert stored.status == "failed", (
            "left pending ⇒ the loop would retry this task forever, every tick"
        )
        assert stored.error and "spec exploded" in stored.error
        assert bot.sent and "fallito" in bot.sent[0][1]
        assert bot.sent[0][0] == 999

    async def test_a_skipped_task_is_recorded_as_done(self, session, monkeypatch):
        """`TaskSkip` is an intentional no-op, so it must settle as `done`: marking it
        failed would leave a failed row and a scary notice for something deliberate,
        and leaving it pending would retry it forever."""
        task = await _pending_task(session)
        monkeypatch.setattr(
            schedule,
            "execute_task",
            _raising_execute(schedule_service.TaskSkip("quiz già in corso")),
        )
        bot = _FakeBot()

        await schedule._run_due_task(bot, session, task)

        stored = (await session.execute(select(ScheduledTask))).scalar_one()
        assert stored.status == "done"
        assert bot.sent and "saltato" in bot.sent[0][1]

    async def test_a_task_with_no_reachable_creator_is_still_settled(
        self, session, monkeypatch
    ):
        """No creator to DM must not stop the outcome from being persisted — the
        notification is best-effort, the bookkeeping is not.

        Uses `0` rather than `None`: `created_by_tg_id` is NOT NULL, so `None` is not
        a state the database can hold, and `_notify_creator`'s falsy guard is only
        reachable with a zero. (Found by writing this test against `None` first and
        getting an IntegrityError.)
        """
        task = await _pending_task(session, created_by=0)
        monkeypatch.setattr(
            schedule, "execute_task", _raising_execute(RuntimeError("boom"))
        )
        bot = _FakeBot()

        await schedule._run_due_task(bot, session, task)

        stored = (await session.execute(select(ScheduledTask))).scalar_one()
        assert stored.status == "failed"
        assert bot.sent == []

    async def test_the_failure_path_uses_captured_primitives_not_the_expired_instance(
        self, session, monkeypatch
    ):
        """The post-rollback path stays safe even when SQLAlchemy expires the row."""
        task = await _pending_task(session)
        task_id = task.id
        notified: list[tuple[int | None, int, str]] = []

        async def notify(bot, creator_tg_id, notified_task_id, text):
            notified.append((creator_tg_id, notified_task_id, text))

        monkeypatch.setattr(
            schedule, "execute_task", _raising_execute(RuntimeError("boom"))
        )
        monkeypatch.setattr(schedule, "_notify_creator", notify)

        await schedule._run_due_task(_FakeBot(), session, task)

        stored = (await session.execute(
            select(ScheduledTask).where(ScheduledTask.id == task_id)
        )).scalar_one()
        assert stored.status == "failed"
        assert notified == [(999, task_id, f"⚠️ Task #{task_id} fallito.")]


@pytest.mark.pg
class TestFailurePathOnPostgres:
    async def test_the_rollback_really_expires_the_instance(self, pg_session):
        """Proof that the hazard above is not hypothetical — and that SQLite is not
        where you would find out.

        Under a real connection, an attribute read straight after a rollback that had
        work to undo raises. The production code is safe only because `mark_failed`
        plus `commit` sit in between.
        """
        task = await _pending_task(pg_session)
        task.status = "running"
        await pg_session.flush()
        await pg_session.rollback()

        with pytest.raises(Exception) as exc:  # MissingGreenlet (sqlalchemy.exc)
            _ = task.created_by_tg_id

        assert "greenlet_spawn" in str(exc.value)
