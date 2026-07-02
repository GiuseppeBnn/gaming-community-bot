"""Integration tests for BetType.execute_scheduled — the timed auto-close.

A scheduled ``bet`` task with payload ``{"action": "lock"}`` closes the betting
window (→ ``locked``). If the bet is no longer ``open`` (an admin locked/resolved/
cancelled it first) the task is a *skip*, not a failure: it raises
``schedule_service.TaskSkip`` so the loop marks it done and DMs the creator.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import services.bet_service as bet_svc
from database.models import EventStatus
from handlers.event_types.bet_type import BetType
from services import group_registry
from services.schedule_service import TaskSkip


@pytest.fixture(autouse=True)
def _no_group(monkeypatch):
    # No group configured → the best-effort group announcement is a no-op, so the
    # tests don't need a live bot.
    monkeypatch.setattr(group_registry, "get_group_id", lambda: 0)


def _lock_task(ref_id: int):
    return SimpleNamespace(
        ref_id=ref_id,
        task_type="bet",
        group_id=123,
        id=7,
        created_by_tg_id=1,
        payload_json=json.dumps({"action": "lock"}),
    )


async def _open_event(session):
    event = await bet_svc.create_event(
        session,
        creator_tg_id=1,
        title="Timed",
        description="d",
        options=[{"label": "A"}, {"label": "B"}],
        status=EventStatus.open.value,
        window_seconds=900,
    )
    await session.commit()
    return event


async def test_scheduled_lock_closes_open_bet(session, user_factory):
    await user_factory(tg_id=1)
    event = await _open_event(session)
    event_id = event.id

    await BetType().execute_scheduled(None, session, _lock_task(event_id), 123)
    await session.commit()

    session.expire_all()
    reloaded = await bet_svc.get_event_detail(session, event_id)
    assert reloaded.status == EventStatus.locked.value
    assert reloaded.locked_at is not None


async def test_scheduled_lock_skips_when_already_settled(session, user_factory):
    await user_factory(tg_id=1)
    event = await _open_event(session)
    await bet_svc.lock_event(session, event.id)  # admin already locked it
    await session.commit()

    with pytest.raises(TaskSkip):
        await BetType().execute_scheduled(None, session, _lock_task(event.id), 123)
