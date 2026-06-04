"""Integration tests for services/schedule_service.py (in-memory SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta

import services.schedule_service as sch


async def test_schedule_and_due(session):
    past = datetime.utcnow() - timedelta(minutes=1)
    future = datetime.utcnow() + timedelta(hours=1)
    await sch.schedule_task(session, "poll", past, 9, -100, payload={"q": "x"})
    await sch.schedule_task(session, "bet", future, 9, -100, payload={"title": "y"})
    await session.commit()

    due = await sch.due_tasks(session, datetime.utcnow())
    assert len(due) == 1
    assert due[0].task_type == "poll"
    assert sch.task_payload(due[0]) == {"q": "x"}


async def test_mark_done_excludes_from_due(session):
    past = datetime.utcnow() - timedelta(minutes=1)
    t = await sch.schedule_task(session, "quiz", past, 9, -100, ref_id=5)
    await session.commit()
    await sch.mark_done(session, t)
    await session.commit()
    assert await sch.due_tasks(session, datetime.utcnow()) == []
    assert t.status == "done"


async def test_mark_failed_records_error(session):
    past = datetime.utcnow() - timedelta(minutes=1)
    t = await sch.schedule_task(session, "poll", past, 9, -100, payload={})
    await session.commit()
    await sch.mark_failed(session, t, "boom")
    await session.commit()
    assert t.status == "failed"
    assert t.error == "boom"


async def test_cancel(session):
    future = datetime.utcnow() + timedelta(hours=2)
    t = await sch.schedule_task(session, "bet", future, 9, -100, payload={})
    await session.commit()
    assert await sch.cancel(session, t.id) is True
    await session.commit()
    assert t.status == "cancelled"
    # cannot cancel twice
    assert await sch.cancel(session, t.id) is False

    pending = await sch.list_pending(session)
    assert pending == []
