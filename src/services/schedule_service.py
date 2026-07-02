"""
Schedule service — persistence + time parsing for the in-process scheduler.

Telegram's Bot API cannot schedule polls, so the bot schedules tasks itself:
each ScheduledTask is executed by `scheduler_loop` (main.py) when due. Stored in
the DB, so schedules survive a restart. Follows STEERING §5 (no commit here).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
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

_ABS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$")
_REL_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)
_REL_UNIT = {"m": 60, "h": 3600, "d": 86400}


def parse_run_at(text: str, tz_name: str | None = None) -> datetime:
    """Parse an absolute 'YYYY-MM-DD HH:MM' or relative '30m'/'2h'/'1d' time.

    Returns a naive UTC datetime (to match the DB's naive timestamps). Raises
    ValueError on unparseable input or a time in the past.
    """
    text = (text or "").strip()
    tz = ZoneInfo(tz_name or settings.scheduler_timezone)
    now_local = datetime.now(tz)

    rel = _REL_RE.match(text)
    if rel:
        seconds = int(rel.group(1)) * _REL_UNIT[rel.group(2).lower()]
        if seconds <= 0:
            raise ValueError("La durata deve essere positiva.")
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
    rel = _REL_RE.match((text or "").strip())
    if not rel:
        raise ValueError(
            "Formato non valido. Usa <code>30m</code>, <code>2h</code> oppure <code>1d</code>."
        )
    seconds = int(rel.group(1)) * _REL_UNIT[rel.group(2).lower()]
    if seconds <= 0:
        raise ValueError("La durata deve essere positiva.")
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
    return list(result.scalars().all())


async def mark_done(session: AsyncSession, task: ScheduledTask) -> None:
    task.status = "done"
    task.executed_at = utcnow()


async def mark_failed(session: AsyncSession, task: ScheduledTask, error: str) -> None:
    task.status = "failed"
    task.executed_at = utcnow()
    task.error = error[:512]


async def cancel(session: AsyncSession, task_id: int, by_tg_id: int | None = None) -> bool:
    task = (
        await session.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
    ).scalar_one_or_none()
    if task is None or task.status != "pending":
        return False
    task.status = "cancelled"
    return True
