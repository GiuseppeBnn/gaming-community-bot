"""Unit tests for schedule_service.parse_run_at (pure time parsing)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.schedule_service import parse_duration, parse_run_at, to_local


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

    def test_absurd_relative_raises_instead_of_overflowing(self):
        # Public/admin chat input: without the cap this reached
        # `datetime + timedelta(seconds=8.6e13)` and died with OverflowError,
        # which no handler catches (ValueError is the one that's handled).
        with pytest.raises(ValueError):
            parse_run_at("999999999d")


class TestParseDuration:
    def test_minutes_hours_days_to_seconds(self):
        assert parse_duration("30m") == 30 * 60
        assert parse_duration("2h") == 2 * 3600
        assert parse_duration("1d") == 86400

    def test_case_insensitive_and_trimmed(self):
        assert parse_duration("  45M ") == 45 * 60

    def test_absolute_format_rejected(self):
        # parse_duration only accepts a relative span, not an absolute instant.
        with pytest.raises(ValueError):
            parse_duration("2026-01-01 10:00")

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            parse_duration("un po'")

    def test_zero_raises(self):
        with pytest.raises(ValueError):
            parse_duration("0m")

    def test_absurd_duration_raises_instead_of_overflowing_int32(self):
        # /crea_scommessa feeds this into betting_window_seconds (int32): on
        # Postgres an uncapped value is "integer out of range" at commit time,
        # i.e. after the user was already told the bet was created.
        with pytest.raises(ValueError):
            parse_duration("999999999d")

    def test_cap_boundary_is_accepted(self):
        assert parse_duration("365d") == 365 * 86400


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
