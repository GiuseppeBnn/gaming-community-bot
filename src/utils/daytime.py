"""Single source of truth for what "a day" means to the bot.

Timestamps are stored as **naive UTC** everywhere (§3). But anything that
resets "at midnight" — the `/daily` claim, the daily XP cap — must reset at
midnight in the **community's** timezone (`settings.scheduler_timezone`), not
in UTC: computing the day in UTC would move the reset to 01:00 (winter) or
02:00 (summer) Italian time, which reads as broken to members.

Same timezone and same naive-UTC storage convention already used by
`services/schedule_service.py`, so there is one notion of local time in the
codebase and DST is handled by `zoneinfo` rather than by hand.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from config_data.config import settings


def _tz() -> ZoneInfo:
    # ZoneInfo caches instances internally, so this is cheap to call per use
    # and still picks up a settings change (tests override the timezone).
    return ZoneInfo(settings.scheduler_timezone)


def utc_now() -> datetime:
    """"Now" in the storage convention used across the codebase: naive UTC."""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def to_local(naive_utc: datetime) -> datetime:
    """Naive-UTC timestamp → aware datetime in the community timezone."""
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(_tz())


def local_day(naive_utc: datetime) -> date:
    """The local calendar day a naive-UTC timestamp falls on."""
    return to_local(naive_utc).date()


def local_today() -> date:
    """The local calendar day it is right now."""
    return local_day(utc_now())


def local_midnight(day: date) -> datetime:
    """First instant of the local calendar day `day`, as naive UTC.

    The lower-bound counterpart of `next_local_midnight`: a *fixed* instant a
    stored column can be compared against, which is what lets a daily reset be
    written as one SQL predicate (`WHERE last_claim < :day_opened`) instead of a
    per-row threshold recomputed in Python.

    Midnight always exists in Europe/Rome (DST shifts happen at 02:00/03:00), so
    there is no ambiguous/skipped-hour case to disambiguate here.
    """
    midnight_local = datetime.combine(day, time.min, tzinfo=_tz())
    return midnight_local.astimezone(timezone.utc).replace(tzinfo=None)


def next_local_midnight(naive_utc: datetime) -> datetime:
    """First instant of the local day *after* `naive_utc`, as naive UTC."""
    return local_midnight(local_day(naive_utc) + timedelta(days=1))
