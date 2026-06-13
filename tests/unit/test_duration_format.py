"""Unit tests for the duration formatters in utils.text."""

from __future__ import annotations

from utils.text import format_duration, format_seconds_short


class TestFormatDuration:
    def test_sub_minute(self):
        assert format_duration(0) == "meno di 1 minuto"
        assert format_duration(59) == "meno di 1 minuto"

    def test_minutes_only(self):
        assert format_duration(60) == "1min"
        assert format_duration(45 * 60) == "45min"

    def test_hours_only(self):
        assert format_duration(2 * 3600) == "2h"

    def test_hours_and_minutes(self):
        assert format_duration(3 * 3600 + 15 * 60) == "3h 15min"
        # The /daily 20h cooldown should read cleanly, never "19.8 ore".
        assert "." not in format_duration(19 * 3600 + 48 * 60)

    def test_negative_clamped(self):
        assert format_duration(-100) == "meno di 1 minuto"


class TestFormatSecondsShort:
    def test_seconds(self):
        assert format_seconds_short(45) == "45s"
        assert format_seconds_short(0) == "0s"

    def test_minutes_and_seconds(self):
        assert format_seconds_short(90) == "1m 30s"

    def test_hours_and_minutes(self):
        assert format_seconds_short(3600 + 2 * 60) == "1h 2m"
