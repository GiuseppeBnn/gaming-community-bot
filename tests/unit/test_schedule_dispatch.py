"""Unit tests for ``schedule.execute_task`` — generic dispatch via the registry.

``execute_task`` must not branch per type: it resolves the group id, looks the
spec up in the event-type registry, and delegates to ``execute_scheduled`` (or
raises for an unknown type / missing group).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from handlers import event_types, schedule
from handlers.event_types.base import _REGISTRY


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    snapshot = dict(_REGISTRY)
    event_types.clear()
    # Deterministic: no runtime group configured → execute_task falls back to
    # the task's own group_id.
    monkeypatch.setattr(schedule.group_registry, "get_group_id", lambda: 0)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


def _task(task_type: str = "demo", group_id: int | None = 123):
    return SimpleNamespace(
        task_type=task_type, ref_id=1, group_id=group_id, payload_json=None, id=7
    )


async def test_execute_task_delegates_to_spec():
    calls = []

    class _Demo:
        key = "demo"
        hub_label = "🎮 Demo"
        create_label = "➕ Demo"

        async def execute_scheduled(self, bot, session, task, group_id):
            calls.append((bot, session, task, group_id))

    event_types.register(_Demo())
    bot, session, task = object(), object(), _task()
    await schedule.execute_task(bot, session, task)

    assert len(calls) == 1
    got_bot, got_session, got_task, got_group = calls[0]
    assert got_bot is bot and got_session is session and got_task is task
    assert got_group == 123  # resolved from task.group_id


async def test_execute_task_unknown_type_raises():
    task = _task(task_type="ghost")  # not registered
    with pytest.raises(RuntimeError, match="Tipo task sconosciuto"):
        await schedule.execute_task(object(), object(), task)


async def test_execute_task_missing_group_raises():
    task = _task(group_id=None)
    with pytest.raises(RuntimeError, match="GROUP_ID"):
        await schedule.execute_task(object(), object(), task)
