"""Unit tests for utils/daytime — the shared "what is a day" helper.

Timestamps are stored as naive UTC but every daily reset happens at *local*
midnight, so the interesting cases are the ones where the local day and the UTC
day disagree, plus the two DST offsets.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from config_data.config import settings
from utils import daytime

_ROME = ZoneInfo("Europe/Rome")


@pytest.fixture(autouse=True)
def _pin_tz(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_timezone", "Europe/Rome")


def _utc(local: str) -> datetime:
    return (
        datetime.strptime(local, "%Y-%m-%d %H:%M")
        .replace(tzinfo=_ROME)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def test_local_day_differs_from_utc_day_late_at_night():
    """23:30 local on 15 June is already the 15th locally but 21:30 UTC."""
    stamp = _utc("2026-06-15 23:30")
    assert stamp.date().isoformat() == "2026-06-15"      # UTC date
    assert daytime.local_day(stamp).isoformat() == "2026-06-15"


def test_local_day_after_midnight_is_the_new_day_while_utc_lags():
    """00:30 local on 16 June is still 22:30 UTC on the 15th (CEST, +2)."""
    stamp = _utc("2026-06-16 00:30")
    assert stamp.date().isoformat() == "2026-06-15"      # UTC still on the 15th
    assert daytime.local_day(stamp).isoformat() == "2026-06-16"


def test_next_local_midnight_summer_offset():
    """CEST (+2): local midnight of the 16th is 22:00 UTC on the 15th."""
    assert daytime.next_local_midnight(_utc("2026-06-15 10:00")) == datetime(2026, 6, 15, 22, 0)


def test_next_local_midnight_winter_offset():
    """CET (+1): local midnight of the 16th is 23:00 UTC on the 15th."""
    assert daytime.next_local_midnight(_utc("2026-01-15 10:00")) == datetime(2026, 1, 15, 23, 0)


@pytest.mark.parametrize("local", ["2026-03-29 10:00", "2026-10-25 10:00"])
def test_next_local_midnight_lands_on_the_next_local_day_across_dst(local):
    """The DST switch days themselves must still yield the next local midnight."""
    stamp = _utc(local)
    nxt = daytime.next_local_midnight(stamp)
    assert daytime.local_day(nxt) == daytime.local_day(stamp) + timedelta(days=1)
    assert daytime.to_local(nxt).hour == 0


def test_next_local_midnight_is_strictly_in_the_future():
    stamp = _utc("2026-06-15 00:00")  # exactly at local midnight
    assert daytime.next_local_midnight(stamp) > stamp


# ---------------------------------------------------------------------------
# local_midnight — the *opening* instant of a local day
#
# `next_local_midnight` answers "when does the current day end". SQL needs the
# other half: a fixed lower bound it can compare a stored column against, so a
# daily reset can be expressed as `WHERE last_claim < :today_started` instead of
# recomputing a per-row threshold in Python (see economy_service.claim_daily).
# ---------------------------------------------------------------------------

def test_local_midnight_summer_offset():
    """CEST (+2): the 16th of June opens at 22:00 UTC on the 15th."""
    assert daytime.local_midnight(date(2026, 6, 16)) == datetime(2026, 6, 15, 22, 0)


def test_local_midnight_winter_offset():
    """CET (+1): the 16th of January opens at 23:00 UTC on the 15th."""
    assert daytime.local_midnight(date(2026, 1, 16)) == datetime(2026, 1, 15, 23, 0)


@pytest.mark.parametrize("local", ["2026-03-29 10:00", "2026-10-25 10:00", "2026-06-15 10:00"])
def test_local_midnight_opens_the_day_that_contains_the_stamp(local):
    """A timestamp always falls on or after the opening of its own local day, and
    before the next one. Checked on both DST switch days."""
    stamp = _utc(local)
    opens = daytime.local_midnight(daytime.local_day(stamp))
    assert opens <= stamp < daytime.next_local_midnight(stamp)


@pytest.mark.parametrize("local", ["2026-03-29 10:00", "2026-10-25 10:00", "2026-06-15 10:00"])
def test_next_local_midnight_is_the_opening_of_the_following_day(local):
    """The two helpers must agree, including on the 23h and 25h DST days — where
    naively adding timedelta(days=1) to a midnight would be off by an hour."""
    stamp = _utc(local)
    assert daytime.next_local_midnight(stamp) == daytime.local_midnight(
        daytime.local_day(stamp) + timedelta(days=1)
    )
