"""
Schedule service — persistence + time parsing for the in-process scheduler.

Telegram's Bot API cannot schedule polls, so the bot schedules tasks itself:
each ScheduledTask is executed by `scheduler_loop` (main.py) when due. Stored in
the DB, so schedules survive a restart. Follows STEERING §5 (no commit here).
"""

from __future__ import annotations

from collections.abc import Collection
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import ScheduledTask


class TaskSkip(Exception):
    """Raised by ``execute_scheduled`` when a task should NOT run but it is *not*
    an error (e.g. the quiz is already in progress). The scheduler loop marks the
    task ``done`` and notifies the creator, instead of marking it ``failed``."""


def utcnow() -> datetime:
    """Naive UTC, matching the DB's naive timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def retry_delay_minutes(retry_count: int) -> int:
    """Bounded exponential delay for a failed internal lifecycle expiry."""
    return min(60, 2 ** max(0, retry_count))

_ABS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$")
_REL_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)
_REL_UNIT = {"m": 60, "h": 3600, "d": 86400}


# Upper bound on any relative duration accepted from chat input. Both parsers
# below feed it into arithmetic that overflows on absurd values: `parse_run_at`
# into `datetime + timedelta` (raises OverflowError past year 9999) and
# `parse_duration` into `BettingEvent.betting_window_seconds`, an int32 column
# (Postgres rejects anything past 2^31 with "integer out of range"). A year is
# far beyond any real scheduled event or betting window.
_MAX_REL_SECONDS = 365 * 86400


def _rel_seconds(text: str) -> int | None:
    """Seconds for a relative token ('30m'/'2h'/'1d'), or None if it isn't one.

    Single place where the bound is enforced, so neither caller can forget it.
    """
    rel = _REL_RE.match(text)
    if not rel:
        return None
    seconds = int(rel.group(1)) * _REL_UNIT[rel.group(2).lower()]
    if seconds <= 0:
        raise ValueError("La durata deve essere positiva.")
    if seconds > _MAX_REL_SECONDS:
        raise ValueError("Durata troppo lunga: il massimo è 365 giorni.")
    return seconds


def parse_run_at(text: str, tz_name: str | None = None) -> datetime:
    """Parse an absolute 'YYYY-MM-DD HH:MM' or relative '30m'/'2h'/'1d' time.

    Returns a naive UTC datetime (to match the DB's naive timestamps). Raises
    ValueError on unparseable input or a time in the past.
    """
    text = (text or "").strip()
    tz = ZoneInfo(tz_name or settings.scheduler_timezone)
    now_local = datetime.now(tz)

    seconds = _rel_seconds(text)
    if seconds is not None:
        target_local = now_local + timedelta(seconds=seconds)
    else:
        abs_m = _ABS_RE.match(text)
        if not abs_m:
            raise ValueError(
                "Formato non valido. Usa <code>AAAA-MM-GG HH:MM</code> oppure <code>30m</code>/<code>2h</code>/<code>1d</code>."
            )
        y, mo, d, h, mi = (int(g) for g in abs_m.groups())
        target_local = datetime(y, mo, d, h, mi, tzinfo=tz)
        if target_local <= now_local:
            raise ValueError("L'orario indicato è già passato.")

    # Convert to naive UTC (DB stores naive UTC timestamps).
    return target_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def parse_duration(text: str) -> int:
    """Parse a relative duration ('30m'/'2h'/'1d') into seconds.

    Unlike ``parse_run_at`` (which yields an absolute instant), this returns a plain
    duration — used by the betting-window creation step. Raises ``ValueError`` on
    unparseable input or a non-positive duration.
    """
    seconds = _rel_seconds((text or "").strip())
    if seconds is None:
        raise ValueError(
            "Formato non valido. Usa <code>30m</code>, <code>2h</code> oppure <code>1d</code>."
        )
    return seconds


def to_local(dt: datetime) -> datetime:
    """Convert a naive UTC datetime (as stored in the DB) to the configured local
    timezone, for display. Inverse of ``parse_run_at``; returns a naive datetime so
    callers can ``strftime`` it directly. Timezone is driven by ``SCHEDULER_TIMEZONE``
    (default ``Europe/Rome``), so the displayed time matches the admin's input."""
    return (
        dt.replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo(settings.scheduler_timezone))
        .replace(tzinfo=None)
    )


async def schedule_task(
    session: AsyncSession,
    task_type: str,
    run_at: datetime,
    created_by_tg_id: int,
    group_id: int | None,
    ref_id: int | None = None,
    payload: dict | None = None,
) -> ScheduledTask:
    task = ScheduledTask(
        task_type=task_type,
        ref_id=ref_id,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        run_at=run_at,
        status="pending",
        created_by_tg_id=created_by_tg_id,
        group_id=group_id,
    )
    session.add(task)
    await session.flush()
    return task


def task_payload(task: ScheduledTask) -> dict:
    return json.loads(task.payload_json) if task.payload_json else {}


async def due_tasks(session: AsyncSession, now: datetime) -> list[ScheduledTask]:
    result = await session.execute(
        select(ScheduledTask)
        .where(ScheduledTask.status == "pending", ScheduledTask.run_at <= now)
        .order_by(ScheduledTask.run_at.asc())
    )
    return list(result.scalars().all())


async def list_pending(session: AsyncSession) -> list[ScheduledTask]:
    result = await session.execute(
        select(ScheduledTask)
        .where(ScheduledTask.status == "pending")
        .order_by(ScheduledTask.run_at.asc())
    )
    # Internal lifecycle timers must remain durable but must not appear in
    # /programmati: cancelling one there could strand the owning feature with no
    # future transition.
    return [
        task for task in result.scalars().all()
        if not task_payload(task).get("internal")
    ]


async def mark_done(session: AsyncSession, task: ScheduledTask) -> None:
    task.status = "done"
    task.executed_at = utcnow()


async def mark_failed(session: AsyncSession, task: ScheduledTask, error: str) -> None:
    task.status = "failed"
    task.executed_at = utcnow()
    task.error = error[:512]


async def mark_done_by_id(session: AsyncSession, task_id: int) -> None:
    """Durably complete a task without dereferencing an ORM row after rollback."""
    await session.execute(
        update(ScheduledTask)
        .where(ScheduledTask.id == task_id)
        .values(status="done", executed_at=utcnow())
        .execution_options(synchronize_session=False)
    )


async def mark_failed_by_id(session: AsyncSession, task_id: int, error: str) -> None:
    """Durably fail a task without dereferencing an ORM row after rollback."""
    await session.execute(
        update(ScheduledTask)
        .where(ScheduledTask.id == task_id)
        .values(status="failed", executed_at=utcnow(), error=error[:512])
        .execution_options(synchronize_session=False)
    )


async def mark_retry(
    session: AsyncSession,
    task_id: int,
    *,
    retry_count: int,
    error: str,
    now: datetime,
) -> None:
    """Persist the next bounded retry for one still-pending internal expiry.

    The caller supplies only primitives captured before a rollback.  A lost row or
    a concurrent cancellation is not silently resurrected as a retry.
    """
    result = await session.execute(
        update(ScheduledTask)
        .where(ScheduledTask.id == task_id, ScheduledTask.status == "pending")
        .values(
            status="pending",
            retry_count=retry_count + 1,
            run_at=now + timedelta(minutes=retry_delay_minutes(retry_count)),
            error=error[:512],
            executed_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise RuntimeError(f"scheduled task {task_id} is no longer pending")


async def cancel_pending_for_ref(
    session: AsyncSession,
    *,
    task_type: str,
    ref_id: int,
    actions: Collection[str] | None = None,
) -> int:
    """Cancel pending tasks belonging to exactly one aggregate, without committing.

    Payload JSON is intentionally filtered in Python: this keeps the service
    portable between SQLite tests and PostgreSQL production while leaving the
    persisted task schema unchanged.  An absent action means the historic start
    action, matching the scheduler's payload convention.
    """
    scope = (
        ScheduledTask.task_type == task_type,
        ScheduledTask.ref_id == ref_id,
        ScheduledTask.status == "pending",
    )
    if actions is None:
        result = await session.execute(
            update(ScheduledTask)
            .where(*scope)
            .values(status="cancelled")
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    wanted = set(actions)
    if not wanted:
        return 0
    rows = (await session.execute(
        select(ScheduledTask.id, ScheduledTask.payload_json).where(*scope)
    )).all()
    ids: list[int] = []
    for task_id, payload_json in rows:
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("action", "start") in wanted:
            ids.append(task_id)
    if not ids:
        return 0
    result = await session.execute(
        update(ScheduledTask)
        .where(ScheduledTask.id.in_(ids), ScheduledTask.status == "pending")
        .values(status="cancelled")
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def cancel(session: AsyncSession, task_id: int, by_tg_id: int | None = None) -> bool:
    task = (
        await session.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
    ).scalar_one_or_none()
    if task is None or task.status != "pending":
        return False
    task.status = "cancelled"
    return True
