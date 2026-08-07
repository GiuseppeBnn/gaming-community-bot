"""Integration tests for services/schedule_service.py (in-memory SQLite)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

import services.schedule_service as sch
from database.models import ScheduledTask
from handlers import event_types, schedule
from handlers.event_types.base import _REGISTRY


async def test_schedule_and_due(session):
    past = sch.utcnow() - timedelta(minutes=1)
    future = sch.utcnow() + timedelta(hours=1)
    await sch.schedule_task(session, "poll", past, 9, -100, payload={"q": "x"})
    await sch.schedule_task(session, "bet", future, 9, -100, payload={"title": "y"})
    await session.commit()

    due = await sch.due_tasks(session, sch.utcnow())
    assert len(due) == 1
    assert due[0].task_type == "poll"
    assert sch.task_payload(due[0]) == {"q": "x"}


async def test_mark_done_excludes_from_due(session):
    past = sch.utcnow() - timedelta(minutes=1)
    t = await sch.schedule_task(session, "quiz", past, 9, -100, ref_id=5)
    await session.commit()
    await sch.mark_done(session, t)
    await session.commit()
    assert await sch.due_tasks(session, sch.utcnow()) == []
    assert t.status == "done"


async def test_mark_failed_records_error(session):
    past = sch.utcnow() - timedelta(minutes=1)
    t = await sch.schedule_task(session, "poll", past, 9, -100, payload={})
    await session.commit()
    await sch.mark_failed(session, t, "boom")
    await session.commit()
    assert t.status == "failed"
    assert t.error == "boom"


async def test_cancel(session):
    future = sch.utcnow() + timedelta(hours=2)
    t = await sch.schedule_task(session, "bet", future, 9, -100, payload={})
    await session.commit()
    assert await sch.cancel(session, t.id) is True
    await session.commit()
    assert t.status == "cancelled"
    # cannot cancel twice
    assert await sch.cancel(session, t.id) is False

    pending = await sch.list_pending(session)
    assert pending == []


async def test_run_due_task_rolls_back_partial_writes_on_failure(session, monkeypatch):
    """A task that flushes a partial write and then raises must end up ``failed``
    with that partial write rolled back — never stranded ``pending`` (which the
    loop would retry forever) and never half-committed.
    """
    snapshot = dict(_REGISTRY)
    event_types.clear()
    monkeypatch.setattr(schedule.group_registry, "get_group_id", lambda: 0)
    try:
        class _Boom:
            key = "boom"
            hub_label = "💥 Boom"
            create_label = "x"

            async def execute_scheduled(self, bot, sess, task, group_id):
                # Partial write + flush, then fail: leaves a live, dirty transaction.
                sess.add(ScheduledTask(
                    task_type="orphan", run_at=task.run_at, status="pending",
                    created_by_tg_id=1, group_id=-100,
                ))
                await sess.flush()
                raise RuntimeError("kaboom")

        event_types.register(_Boom())

        past = sch.utcnow() - timedelta(minutes=1)
        task = await sch.schedule_task(session, "boom", past, 9, -100)
        await session.commit()

        # bot is a bare object(): _notify_creator's send_message attempt fails but
        # is swallowed (best-effort), so the outcome persistence is what we assert.
        await schedule._run_due_task(object(), session, task)

        assert task.status == "failed"
        assert "kaboom" in (task.error or "")
        # The orphan partial write was rolled back; only the real task row remains.
        total = (
            await session.execute(select(func.count()).select_from(ScheduledTask))
        ).scalar_one()
        assert total == 1
        # …and it is not stuck pending (it's the failed one).
        assert await sch.due_tasks(session, sch.utcnow()) == []
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(snapshot)


async def test_run_due_task_marks_done_on_success(session, monkeypatch):
    """The happy path still commits the task as ``done``."""
    snapshot = dict(_REGISTRY)
    event_types.clear()
    monkeypatch.setattr(schedule.group_registry, "get_group_id", lambda: 0)
    try:
        class _Ok:
            key = "ok"
            hub_label = "✅ Ok"
            create_label = "x"

            async def execute_scheduled(self, bot, sess, task, group_id):
                return None

        event_types.register(_Ok())
        past = sch.utcnow() - timedelta(minutes=1)
        task = await sch.schedule_task(session, "ok", past, 9, -100)
        await session.commit()

        await schedule._run_due_task(object(), session, task)
        assert task.status == "done"
        assert task.executed_at is not None
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(snapshot)
