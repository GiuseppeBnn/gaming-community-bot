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
import json
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


def _task(
    task_id: int = 7,
    created_by: int | None = 999,
    *,
    payload: dict | None = None,
    retry_count: int = 0,
):
    return SimpleNamespace(
        id=task_id, task_type="demo", ref_id=1, group_id=123,
        payload_json=json.dumps(payload) if payload is not None else None,
        created_by_tg_id=created_by, retry_count=retry_count,
    )


@pytest.fixture
def marks(monkeypatch):
    """Capture mark_done / mark_failed instead of touching a database."""
    recorded: list[tuple[str, str]] = []

    async def mark_done(session, task):
        recorded.append(("done", ""))

    async def mark_failed(session, task, reason):
        recorded.append(("failed", reason))

    async def mark_done_by_id(session, task_id):
        recorded.append(("done", str(task_id)))

    async def mark_failed_by_id(session, task_id, reason):
        recorded.append(("failed", reason))

    monkeypatch.setattr(schedule.schedule_service, "mark_done", mark_done)
    monkeypatch.setattr(schedule.schedule_service, "mark_failed", mark_failed)
    monkeypatch.setattr(schedule.schedule_service, "mark_done_by_id", mark_done_by_id)
    monkeypatch.setattr(schedule.schedule_service, "mark_failed_by_id", mark_failed_by_id)
    return recorded


@pytest.fixture
def silent_notify(monkeypatch):
    """Swallow the creator DM by default; tests that care patch it themselves."""
    sent: list[str] = []

    async def notify(bot, creator_tg_id, task_id, text):
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

        async def mark_failed_by_id(session_, task_id, reason):
            order.append("mark_failed")

        async def mark_done(session_, task):
            order.append("mark_done")

        monkeypatch.setattr(schedule, "execute_task", boom)
        monkeypatch.setattr(schedule.schedule_service, "mark_failed_by_id", mark_failed_by_id)
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

        async def mark_failed_by_id(session, task_id, reason):
            raise ConnectionError("database unreachable")

        monkeypatch.setattr(schedule, "execute_task", boom)
        monkeypatch.setattr(schedule.schedule_service, "mark_failed_by_id", mark_failed_by_id)
        session = _FakeSession()

        await schedule._run_due_task(object(), session, _task())  # must simply return

        assert session.events[-1] == "rollback"
        assert silent_notify == []

    async def test_success_commits_before_running_a_generic_post_commit_hook(
        self, monkeypatch, silent_notify,
    ):
        """Moving the hook before commit would publish a card for rolled-back state."""
        events: list[str] = []
        session = _FakeSession()
        session.events = events

        async def hook():
            events.append("hook")

        async def execute(bot, session_, task):
            events.append("spec")
            return hook

        async def mark_done(session_, task):
            events.append("done")

        monkeypatch.setattr(schedule, "execute_task", execute)
        monkeypatch.setattr(schedule.schedule_service, "mark_done", mark_done)

        await schedule._run_due_task(object(), session, _task())

        assert events == ["spec", "done", "commit", "hook"]
        assert silent_notify == []

    async def test_commit_failure_never_runs_the_post_commit_hook(self, monkeypatch, silent_notify):
        """A failed commit has no state the hook is allowed to present as saved."""
        events: list[str] = []

        class _CommitFailsOnce(_FakeSession):
            def __init__(self):
                super().__init__()
                self.events = events
                self.commits = 0

            async def commit(self):
                self.events.append("commit")
                self.commits += 1
                if self.commits == 1:
                    raise RuntimeError("commit unavailable")

        async def hook():
            events.append("hook")

        async def execute(bot, session_, task):
            return hook

        async def mark_done(session_, task):
            events.append("done")

        async def mark_failed_by_id(session_, task_id, error):
            events.append("failed")

        monkeypatch.setattr(schedule, "execute_task", execute)
        monkeypatch.setattr(schedule.schedule_service, "mark_done", mark_done)
        monkeypatch.setattr(schedule.schedule_service, "mark_failed_by_id", mark_failed_by_id)

        await schedule._run_due_task(object(), _CommitFailsOnce(), _task())

        assert "hook" not in events
        assert events == ["done", "commit", "rollback", "failed", "commit"]
        assert silent_notify and "fallito" in silent_notify[0]

    async def test_post_commit_hook_failure_keeps_the_task_done(self, monkeypatch, silent_notify):
        """A Telegram failure after commit cannot reopen a durable scheduled task."""
        events: list[str] = []
        session = _FakeSession()
        session.events = events

        async def hook():
            events.append("hook")
            raise RuntimeError("telegram unavailable")

        async def execute(bot, session_, task):
            return hook

        async def mark_done(session_, task):
            events.append("done")

        async def retry(*args, **kwargs):
            raise AssertionError("hook errors must not retry a completed task")

        monkeypatch.setattr(schedule, "execute_task", execute)
        monkeypatch.setattr(schedule.schedule_service, "mark_done", mark_done)
        monkeypatch.setattr(schedule.schedule_service, "mark_retry", retry)

        await schedule._run_due_task(object(), session, _task(payload={"internal": True, "action": "expire"}))

        assert events == ["done", "commit", "hook"]
        assert silent_notify == []

    @pytest.mark.parametrize(
        ("prior_retry_count", "should_notify"),
        [(0, True), (1, False), (5, True), (6, False), (11, True)],
    )
    async def test_only_internal_expiry_retries_and_notifies_on_first_then_each_sixth(
        self, monkeypatch, silent_notify, prior_retry_count, should_notify,
    ):
        """Changing payload predicate would retry ordinary tasks forever or hide expiry trouble."""
        retried: list[tuple[int, int, str]] = []

        async def boom(bot, session, task):
            raise RuntimeError("provider exploded")

        async def mark_retry(session, task_id, *, retry_count, error, now):
            retried.append((task_id, retry_count, error))

        async def mark_failed_by_id(*args, **kwargs):
            raise AssertionError("internal expiry must not become failed")

        monkeypatch.setattr(schedule, "execute_task", boom)
        monkeypatch.setattr(schedule.schedule_service, "mark_retry", mark_retry)
        monkeypatch.setattr(schedule.schedule_service, "mark_failed_by_id", mark_failed_by_id)

        await schedule._run_due_task(
            object(), _FakeSession(),
            _task(payload={"internal": True, "action": "expire"}, retry_count=prior_retry_count),
        )

        assert retried == [(7, prior_retry_count, "provider exploded")]
        assert bool(silent_notify) is should_notify

    @pytest.mark.parametrize(
        "payload",
        (
            {"internal": True, "action": "start"},
            {"action": "expire"},
            {"internal": False, "action": "expire"},
        ),
    )
    async def test_non_internal_failure_is_marked_failed_not_retried(
        self, monkeypatch, silent_notify, payload,
    ):
        """The retry exception is payload-based, never a task-type special case."""
        failed: list[tuple[int, str]] = []

        async def boom(bot, session, task):
            raise RuntimeError("ordinary failure")

        async def mark_failed_by_id(session, task_id, error):
            failed.append((task_id, error))

        async def mark_retry(*args, **kwargs):
            raise AssertionError("non-expiry task retried")

        monkeypatch.setattr(schedule, "execute_task", boom)
        monkeypatch.setattr(schedule.schedule_service, "mark_failed_by_id", mark_failed_by_id)
        monkeypatch.setattr(schedule.schedule_service, "mark_retry", mark_retry)

        await schedule._run_due_task(
            object(), _FakeSession(), _task(payload=payload),
        )

        assert failed == [(7, "ordinary failure")]
        assert silent_notify and "fallito" in silent_notify[0]

    async def test_retry_persistence_failure_rolls_back_again_without_notification(
        self, monkeypatch, silent_notify,
    ):
        """A failed retry write must leave its original pending row for a later tick."""
        session = _FakeSession()

        async def boom(bot, session_, task):
            raise RuntimeError("expiry failed")

        async def mark_retry(*args, **kwargs):
            raise ConnectionError("database unavailable")

        monkeypatch.setattr(schedule, "execute_task", boom)
        monkeypatch.setattr(schedule.schedule_service, "mark_retry", mark_retry)

        await schedule._run_due_task(
            object(), session, _task(payload={"internal": True, "action": "expire"}),
        )

        assert session.events == ["rollback", "rollback"]
        assert silent_notify == []

    @pytest.mark.parametrize(
        "payload_json",
        (
            "{not-json",
            "null",
            "42",
            '"expire"',
            "[]",
            '[["internal", true], ["action", "expire"]]',
        ),
    )
    async def test_malformed_or_non_object_payload_is_failed_never_retried(
        self, monkeypatch, marks, silent_notify, payload_json,
    ):
        """Only a JSON object is schedulable; list-pairs must not forge retry markers.
        """
        task = _task()
        task.payload_json = payload_json

        async def execute(*args, **kwargs):
            raise AssertionError("bad payload must fail before event dispatch")

        monkeypatch.setattr(schedule, "execute_task", execute)
        session = _FakeSession()

        await schedule._run_due_task(object(), session, task)

        assert len(marks) == 1 and marks[0][0] == "failed"
        assert session.events == ["rollback", "commit"]
        assert silent_notify and "fallito" in silent_notify[0]


class TestNotifyCreator:
    async def test_a_failed_dm_does_not_propagate(self):
        """Best-effort by design: the admin may never have opened the bot in private,
        and a scheduling notice must not turn into a task failure."""
        class _Bot:
            async def send_message(self, chat_id, text):
                raise RuntimeError("bot was blocked by the user")

        await schedule._notify_creator(_Bot(), 999, 7, "ciao")  # must not raise

    async def test_nothing_is_sent_without_a_creator(self):
        class _Bot:
            async def send_message(self, chat_id, text):
                raise AssertionError("messaged nobody's chat")

        await schedule._notify_creator(_Bot(), None, 7, "ciao")


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
