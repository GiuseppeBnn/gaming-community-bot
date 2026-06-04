"""Unit tests for schedule_service.parse_run_at (pure time parsing)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.schedule_service import parse_run_at


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
