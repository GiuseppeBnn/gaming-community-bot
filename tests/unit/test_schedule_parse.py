"""Unit tests for schedule_service.parse_run_at (pure time parsing)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.schedule_service import parse_run_at, to_local


class TestParseRunAt:
    def test_relative_minutes(self):
        result = parse_run_at("30m")
        delta = result - datetime.utcnow()
        assert timedelta(minutes=29) < delta < timedelta(minutes=31)

    def test_relative_hours_and_days(self):
        assert parse_run_at("2h") > datetime.utcnow() + timedelta(minutes=110)
        assert parse_run_at("1d") > datetime.utcnow() + timedelta(hours=23)

    def test_absolute_future(self):
        future = datetime.now() + timedelta(days=2)
        text = future.strftime("%Y-%m-%d %H:%M")
        assert parse_run_at(text) > datetime.utcnow()

    def test_absolute_past_raises(self):
        with pytest.raises(ValueError):
            parse_run_at("2020-01-01 00:00")

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            parse_run_at("domani forse")

    def test_zero_relative_raises(self):
        with pytest.raises(ValueError):
            parse_run_at("0m")


class TestToLocal:
    def test_round_trips_with_parse_run_at(self):
        # An absolute time entered in local tz, stored as UTC, must display back
        # as the exact same wall-clock the admin typed (the schedule list/confirm).
        future = datetime.now() + timedelta(days=2)
        text = future.strftime("%Y-%m-%d %H:%M")
        stored_utc = parse_run_at(text)  # naive UTC, as persisted
        shown = to_local(stored_utc).strftime("%Y-%m-%d %H:%M")
        assert shown == text

    def test_default_tz_is_ahead_of_utc_in_summer(self):
        # Europe/Rome is UTC+2 in summer (DST) — display should not equal raw UTC.
        utc = datetime(2026, 7, 1, 12, 0)
        assert to_local(utc) == datetime(2026, 7, 1, 14, 0)
