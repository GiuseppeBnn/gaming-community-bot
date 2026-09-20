"""Read-only discovery of public events for Telegram inline mode.

The service knows the scheduling table and the event registry, but it does not
know any concrete event model. Event types opt in through two optional
capabilities (``discover_open`` and ``describe_scheduled``), keeping the same
plugin boundary used by the admin hub and scheduler.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ScheduledTask
from services import schedule_service
from services.public_event import PublicEvent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def list_public_events(
    session: AsyncSession,
    *,
    event_types: Iterable[object],
    mode: str = "all",
    now: datetime | None = None,
    limit: int = 30,
) -> list[PublicEvent]:
    """Return open events first, then future starts in chronological order.

    ``mode`` is ``all``, ``open`` or ``soon``. Unknown values deliberately act
    like ``all`` so arbitrary inline text never turns into a hidden query API.
    """
    now = now or _utcnow()
    include_open = mode != "soon"
    include_soon = mode != "open"
    found: list[PublicEvent] = []
    registered = tuple(event_types)
    by_key = {getattr(event_type, "key", ""): event_type for event_type in registered}

    if include_open:
        for event_type in registered:
            discover = getattr(event_type, "discover_open", None)
            if discover is not None:
                found.extend(await discover(session))

    if include_soon:
        tasks = list((await session.execute(
            select(ScheduledTask)
            .where(
                ScheduledTask.status == "pending",
                ScheduledTask.run_at > now,
                ScheduledTask.ref_id.is_not(None),
            )
            .order_by(ScheduledTask.run_at.asc(), ScheduledTask.id.asc())
        )).scalars().all())
        for task in tasks:
            # Follow-up closures/locks are operational details, not a new public
            # event about to start.
            if schedule_service.task_payload(task).get("action") not in (None, "start"):
                continue
            scheduled_type = by_key.get(task.task_type)
            describe = getattr(scheduled_type, "describe_scheduled", None)
            if describe is None:
                continue
            event = await describe(session, task.ref_id)
            if event is not None:
                found.append(replace(event, starts_at=task.run_at, schedule_id=task.id))

    open_events = sorted(
        (event for event in found if event.is_open),
        key=lambda event: (event.key, event.item_id),
    )
    scheduled = sorted(
        (event for event in found if not event.is_open),
        key=lambda event: (event.starts_at, event.schedule_id),
    )
    return (open_events + scheduled)[:limit]
