"""Unit tests for the scheduler daemon — ``_run_due_task``, ``_notify_creator`` and
``scheduler_loop`` in ``handlers/schedule.py``.

``execute_task`` (the generic registry dispatch) is covered in
``test_schedule_dispatch.py``. What was missing is everything *around* it: the parts
whose whole job is to keep the loop alive and the task table honest. Their docstrings
make strong claims — a poisoned transaction must never strand a task as ``pending``,
a failing task must never bleed into the next one, the loop must never die — and none
of them had ever been executed by a test.

Same shape as ``test_backup_loop.py``: the endless loop is driven for a fixed number
of ticks by making the patched ``asyncio.sleep`` raise, which is the only way out.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from config_data.config import settings
from handlers import schedule
from services import schedule_service


class _Stop(BaseException):
    """Breaks out of a `while True` under test.

    **Must** derive from `BaseException`, not `Exception`: the loop's whole contract
    is a total `except Exception`, so an `Exception` raised from inside the loop body
    is caught and the test hangs forever. (Learned the hard way — the first version
    of this file did exactly that.) `BaseException` is deliberately not caught there,
    which is also what lets a real `CancelledError` shut the daemon down on exit.
    """


class _FakeSession:
    """Records the order of transaction calls — the ordering *is* the guarantee."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


def _task(task_id: int = 7, created_by: int | None = 999):
    return SimpleNamespace(
        id=task_id, task_type="demo", ref_id=1, group_id=123,
        payload_json=None, created_by_tg_id=created_by,
    )


@pytest.fixture
def marks(monkeypatch):
    """Capture mark_done / mark_failed instead of touching a database."""
    recorded: list[tuple[str, str]] = []

    async def mark_done(session, task):
        recorded.append(("done", ""))

    async def mark_failed(session, task, reason):
        recorded.append(("failed", reason))

    monkeypatch.setattr(schedule.schedule_service, "mark_done", mark_done)
    monkeypatch.setattr(schedule.schedule_service, "mark_failed", mark_failed)
    return recorded


@pytest.fixture
def silent_notify(monkeypatch):
    """Swallow the creator DM by default; tests that care patch it themselves."""
    sent: list[str] = []

    async def notify(bot, task, text):
        sent.append(text)

    monkeypatch.setattr(schedule, "_notify_creator", notify)
    return sent


class TestRunDueTask:
    async def test_a_successful_task_is_marked_done_and_committed(
        self, monkeypatch, marks, silent_notify
    ):
        async def ok(bot, session, task):
            return None

        monkeypatch.setattr(schedule, "execute_task", ok)
        session = _FakeSession()

        await schedule._run_due_task(object(), session, _task())

        assert marks == [("done", "")]
        assert session.events == ["commit"]
        assert silent_notify == [], "a clean run must not DM the creator"

    async def test_a_skip_is_marked_done_not_failed(
        self, monkeypatch, marks, silent_notify
    ):
        """`TaskSkip` is an intentional no-op (e.g. the quiz is already running), not
        an error. Marking it failed would show the admin a scary notice and leave a
        failed row behind for something that was deliberate.
        """
        async def skip(bot, session, task):
            raise schedule_service.TaskSkip("quiz già in corso")

        monkeypatch.setattr(schedule, "execute_task", skip)
        session = _FakeSession()

        await schedule._run_due_task(object(), session, _task())

        assert [kind for kind, _ in marks] == ["done"]
        assert session.events == ["rollback", "commit"]
        assert silent_notify and "saltato" in silent_notify[0]

    async def test_a_failure_rolls_back_before_marking_failed(
        self, monkeypatch, silent_notify
    ):
        """The ordering is the point, and it is why the task table can be trusted.

        A task that raised may have left the session poisoned by a partial flush. If
        `mark_failed` ran on that session it would raise too, the task would stay
        `pending`, and the loop would retry it **forever** — every tick, indefinitely.
        Rolling back first is what makes the failure recordable.
        """
        order: list[str] = []
        session = _FakeSession()
        session.events = order  # share one list, so ordering across both is visible

        async def boom(bot, session_, task):
            raise RuntimeError("spec exploded")

        async def mark_failed(session_, task, reason):
            order.append("mark_failed")

        async def mark_done(session_, task):
            order.append("mark_done")

        monkeypatch.setattr(schedule, "execute_task", boom)
        monkeypatch.setattr(schedule.schedule_service, "mark_failed", mark_failed)
        monkeypatch.setattr(schedule.schedule_service, "mark_done", mark_done)

        await schedule._run_due_task(object(), session, _task())

        assert order.index("rollback") < order.index("mark_failed")
        assert "mark_done" not in order
        assert silent_notify and "fallito" in silent_notify[0]

    async def test_a_failure_to_persist_the_outcome_does_not_propagate(
        self, monkeypatch, silent_notify
    ):
        """If even recording the outcome fails (DB gone), the task is left alone for
        the next tick rather than taking the loop down. The creator is *not* notified:
        nothing is known to have happened yet.
        """
        async def boom(bot, session, task):
            raise RuntimeError("spec exploded")

        async def mark_failed(session, task, reason):
            raise ConnectionError("database unreachable")

        monkeypatch.setattr(schedule, "execute_task", boom)
        monkeypatch.setattr(schedule.schedule_service, "mark_failed", mark_failed)
        session = _FakeSession()

        await schedule._run_due_task(object(), session, _task())  # must simply return

        assert session.events[-1] == "rollback"
        assert silent_notify == []


class TestNotifyCreator:
    async def test_a_failed_dm_does_not_propagate(self):
        """Best-effort by design: the admin may never have opened the bot in private,
        and a scheduling notice must not turn into a task failure."""
        class _Bot:
            async def send_message(self, chat_id, text):
                raise RuntimeError("bot was blocked by the user")

        await schedule._notify_creator(_Bot(), _task(), "ciao")  # must not raise

    async def test_nothing_is_sent_without_a_creator(self):
        class _Bot:
            async def send_message(self, chat_id, text):
                raise AssertionError("messaged nobody's chat")

        await schedule._notify_creator(_Bot(), _task(created_by=None), "ciao")


class TestSchedulerLoop:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr(settings, "scheduler_poll_interval", 20)

    async def test_the_loop_survives_a_failing_tick(self, monkeypatch):
        """A tick can fail for reasons outside any single task — the session itself
        failing to open, for one. The loop logs and comes back next interval.

        This is the guarantee that keeps the scheduler alive across a database blip:
        without it, one `ConnectionError` at 3am kills every scheduled quiz, poll and
        bet-close until someone restarts the bot.
        """
        ticks = 0

        def session_maker():
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                raise ConnectionError("database unreachable")
            raise _Stop  # second tick: end the test from inside the loop body

        monkeypatch.setattr(schedule, "async_session_maker", session_maker)

        async def fake_sleep(_s):
            return None

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        # The second tick's _Stop escapes the loop's `except Exception` — it must,
        # or the test could never end; BaseException is deliberately not caught there.
        with pytest.raises(_Stop):
            await schedule.scheduler_loop(object())

        assert ticks == 2, "the loop died on the failing tick instead of retrying"

    async def test_every_due_task_in_the_tick_is_processed(self, monkeypatch):
        """All due tasks, in order, in one tick.

        Note what is deliberately **not** asserted: that the loop keeps going if
        `_run_due_task` *raises*. It would not — the `for` is inside the tick's `try`,
        so a raise there aborts the rest of the tick. That guarantee lives entirely in
        `_run_due_task` never raising, which `TestRunDueTask` above pins directly and
        `tests/integration/test_scheduler_failure_path.py` pins against a real DB.
        Asserting it here with a raising stub would only test the stub.
        """
        handled: list[int] = []

        async def due_tasks(session, now):
            return [_task(1), _task(2), _task(3)]

        async def run_one(bot, session, task):
            handled.append(task.id)

        class _Ctx:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(schedule, "async_session_maker", _Ctx)
        monkeypatch.setattr(schedule.schedule_service, "due_tasks", due_tasks)
        monkeypatch.setattr(schedule, "_run_due_task", run_one)

        async def fake_sleep(_s):
            raise _Stop

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        with pytest.raises(_Stop):
            await schedule.scheduler_loop(object())

        assert handled == [1, 2, 3], f"the tick stopped early: {handled}"

    async def test_the_loop_waits_the_configured_interval(self, monkeypatch):
        slept: list[float] = []

        async def due_tasks(session, now):
            return []

        class _Ctx:
            async def __aenter__(self):
                return _FakeSession()

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(schedule, "async_session_maker", _Ctx)
        monkeypatch.setattr(schedule.schedule_service, "due_tasks", due_tasks)

        async def fake_sleep(seconds):
            slept.append(seconds)
            raise _Stop

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        with pytest.raises(_Stop):
            await schedule.scheduler_loop(object())

        assert slept == [settings.scheduler_poll_interval]
