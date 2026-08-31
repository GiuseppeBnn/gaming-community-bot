"""Integration tests for services/schedule_service.py (in-memory SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


@pytest.mark.parametrize(
    ("retry_count", "minutes"),
    [(0, 1), (1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (6, 60), (99, 60)],
)
def test_internal_expiry_backoff_is_bounded(retry_count, minutes):
    """A wrong exponent would either hammer Telegram or strand expiry for hours."""
    assert sch.retry_delay_minutes(retry_count) == minutes


async def test_mark_retry_persists_across_a_new_session_and_truncates_error(session, engine):
    """A restart must observe one durable pending retry, not an in-memory retry plan."""
    now = datetime(2026, 8, 23, 10, 0)
    task = await sch.schedule_task(
        session, "twentyq", now, 9, -100, ref_id=7,
        payload={"internal": True, "action": "expire"},
    )
    await session.commit()

    await sch.mark_retry(
        session, task.id, retry_count=5, error="x" * 700, now=now,
    )
    await session.commit()

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as observer:
        stored = await observer.get(ScheduledTask, task.id)
        assert stored is not None
        assert (stored.status, stored.retry_count, stored.run_at, stored.executed_at) == (
            "pending", 6, now + timedelta(minutes=32), None,
        )
        assert stored.error == "x" * 512


async def test_retry_is_not_due_again_until_its_persisted_backoff_elapsed(session):
    """A failed expiry must leave this tick before a later scheduler tick can claim it."""
    now = datetime(2026, 8, 23, 10, 0)
    task = await sch.schedule_task(
        session,
        "twentyq",
        now,
        9,
        -100,
        ref_id=7,
        payload={"internal": True, "action": "expire"},
    )
    await session.commit()

    await sch.mark_retry(session, task.id, retry_count=0, error="transient", now=now)
    await session.commit()

    assert await sch.due_tasks(session, now) == []
    assert [due.id for due in await sch.due_tasks(session, now + timedelta(minutes=1))] == [task.id]


async def test_cancel_pending_for_ref_scopes_type_reference_status_and_requested_actions(session):
    """Terminalizing one game must not cancel another game's timer or a finished task."""
    run_at = sch.utcnow() + timedelta(hours=1)
    expire = await sch.schedule_task(
        session, "twentyq", run_at, 9, -100, ref_id=7,
        payload={"internal": True, "action": "expire"},
    )
    close = await sch.schedule_task(
        session, "twentyq", run_at, 9, -100, ref_id=7, payload={"action": "close"},
    )
    other_ref = await sch.schedule_task(
        session, "twentyq", run_at, 9, -100, ref_id=8, payload={"action": "expire"},
    )
    other_type = await sch.schedule_task(
        session, "quiz", run_at, 9, -100, ref_id=7, payload={"action": "close"},
    )
    done = await sch.schedule_task(
        session, "twentyq", run_at, 9, -100, ref_id=7, payload={"action": "expire"},
    )
    await sch.mark_done(session, done)
    await session.commit()

    cancelled = await sch.cancel_pending_for_ref(
        session, task_type="twentyq", ref_id=7, actions={"expire"},
    )
    await session.commit()

    statuses = dict((await session.execute(select(ScheduledTask.id, ScheduledTask.status))).all())
    assert cancelled == 1
    assert statuses == {
        expire.id: "cancelled",
        close.id: "pending",
        other_ref.id: "pending",
        other_type.id: "pending",
        done.id: "done",
    }


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
        task_id = task.id

        # bot is a bare object(): _notify_creator's send_message attempt fails but
        # is swallowed (best-effort), so the outcome persistence is what we assert.
        await schedule._run_due_task(object(), session, task)

        stored = (await session.execute(
            select(ScheduledTask).where(ScheduledTask.id == task_id)
        )).scalar_one()
        assert stored.status == "failed"
        assert "kaboom" in (stored.error or "")
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
