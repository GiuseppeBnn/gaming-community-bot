"""Real ORM batches must survive another task's rollback or read transaction."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from database.models import ScheduledTask
from handlers import schedule
from services import schedule_service as service


class _Stop(BaseException):
    pass


async def _seed(session, payload=None):
    now = datetime(2026, 11, 16, 10, 0)
    first = await service.schedule_task(session, "quiz", now, 0, -1001, payload=payload)
    second = await service.schedule_task(session, "quiz", now + timedelta(seconds=1), 0, -1001)
    await session.commit()
    return first.id, second.id


async def _check_batch_rollback(session, engine, monkeypatch, outcome):
    first, second = await _seed(session, {"internal": True, "action": "expire"} if outcome == "retry" else None)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    seen = []
    sessions = []
    async def execute(bot, db_session, task):
        seen.append(task.id)
        sessions.append(db_session)
        if task.id == first:
            if outcome == "post_commit_rollback":
                async def hook():
                    # Even a successful task's publisher may end a later read
                    # transaction by rollback, expiring every retained entity.
                    await db_session.execute(select(ScheduledTask.id))
                    await db_session.rollback()
                return hook
            task.status = "running"
            await db_session.flush()
            if outcome == "skipped":
                raise service.TaskSkip("already closed")
            raise RuntimeError("synthetic first-task failure")
        return None
    async def stop(_seconds):
        raise _Stop
    monkeypatch.setattr(schedule, "async_session_maker", factory)
    monkeypatch.setattr(schedule, "execute_task", execute)
    monkeypatch.setattr(service, "utcnow", lambda: datetime(2026, 11, 16, 11, 0))
    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(_Stop):
        await schedule.scheduler_loop(object())
    assert seen == [first, second]
    async with factory() as check:
        rows = dict((await check.execute(select(ScheduledTask.id, ScheduledTask))).all())
        assert rows[second].status == "done"
        expected = {"failed": "failed", "skipped": "done", "retry": "pending", "post_commit_rollback": "done"}
        assert rows[first].status == expected[outcome]
        if outcome == "retry":
            assert rows[first].retry_count == 1
    assert sessions[0] is not sessions[1]


@pytest.mark.parametrize("outcome", ["failed", "skipped", "retry", "post_commit_rollback"])
async def test_one_tick_continues_after_first_task_rolls_back(session, engine, monkeypatch, outcome):
    await _check_batch_rollback(session, engine, monkeypatch, outcome)


@pytest.mark.pg
@pytest.mark.parametrize("outcome", ["failed", "skipped", "retry", "post_commit_rollback"])
async def test_postgres_batch_rollback_is_isolated(pg_session, pg_engine, monkeypatch, outcome):
    await _check_batch_rollback(pg_session, pg_engine, monkeypatch, outcome)


@pytest.mark.parametrize("change", ["cancelled", "rescheduled", "deleted"])
async def test_later_task_is_rechecked_after_first_changes_it(session, engine, monkeypatch, change):
    from sqlalchemy import delete, update

    first, second = await _seed(session)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    seen = []
    async def execute(bot, db_session, task):
        seen.append(task.id)
        if task.id == first:
            if change == "deleted":
                await db_session.execute(delete(ScheduledTask).where(ScheduledTask.id == second))
            else:
                values = {"status": "cancelled"} if change == "cancelled" else {"run_at": datetime(2026, 11, 17)}
                await db_session.execute(update(ScheduledTask).where(ScheduledTask.id == second).values(**values).execution_options(synchronize_session=False))
        return None
    async def stop(_seconds):
        raise _Stop
    monkeypatch.setattr(schedule, "async_session_maker", factory)
    monkeypatch.setattr(schedule, "execute_task", execute)
    monkeypatch.setattr(service, "utcnow", lambda: datetime(2026, 11, 16, 11, 0))
    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(_Stop):
        await schedule.scheduler_loop(object())
    assert seen == [first]


async def test_task_load_failure_does_not_abort_other_due_ids(session, engine, monkeypatch):
    first, second = await _seed(session)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    original = service.get_due_task
    seen = []
    async def get_due(db_session, task_id, now):
        if task_id == first:
            raise RuntimeError("synthetic read failure")
        return await original(db_session, task_id, now)
    async def execute(bot, db_session, task):
        seen.append(task.id)
        return None
    async def stop(_seconds):
        raise _Stop
    monkeypatch.setattr(service, "get_due_task", get_due)
    monkeypatch.setattr(schedule, "async_session_maker", factory)
    monkeypatch.setattr(schedule, "execute_task", execute)
    monkeypatch.setattr(service, "utcnow", lambda: datetime(2026, 11, 16, 11, 0))
    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(_Stop):
        await schedule.scheduler_loop(object())
    assert seen == [second]
