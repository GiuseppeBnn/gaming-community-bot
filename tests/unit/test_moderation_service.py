"""Unit tests for services/moderation_service.py (duration parsing + error mapping)."""

from __future__ import annotations

import pytest

import services.moderation_service as mod


# ---------------------------------------------------------------------------
# Duration parsing (pure)
# ---------------------------------------------------------------------------

class TestParseDuration:
    @pytest.mark.parametrize(
        "text,expected",
        [("45s", 45), ("10m", 600), ("1h", 3600), ("2d", 172800), ("3 h", 10800)],
    )
    def test_valid(self, text, expected):
        assert mod.parse_duration(text) == expected

    def test_default_on_empty_or_garbage(self):
        assert mod.parse_duration(None) == mod._DEFAULT_MUTE_SECONDS
        assert mod.parse_duration("abc") == mod._DEFAULT_MUTE_SECONDS

    def test_caps_at_max(self):
        assert mod.parse_duration("9999d") == mod._MAX_DURATION_SECONDS

    def test_looks_like_duration(self):
        assert mod.looks_like_duration("10m") is True
        assert mod.looks_like_duration("spam") is False
        assert mod.looks_like_duration(None) is False


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

class _FakeBot:
    """Bot stub whose API methods raise a configured exception (or succeed)."""

    def __init__(self, error: str | None = None):
        self._error = error

    async def _maybe_raise(self, *_, **__):
        if self._error is not None:
            raise Exception(self._error)

    ban_chat_member = _maybe_raise
    unban_chat_member = _maybe_raise
    restrict_chat_member = _maybe_raise


class TestErrorMapping:
    async def test_ban_success(self):
        ok, reason = await mod.ban(_FakeBot(), -100, 1)
        assert ok is True and reason == ""

    async def test_ban_not_enough_rights(self):
        ok, reason = await mod.ban(_FakeBot("Bad Request: not enough rights"), -100, 1)
        assert ok is False
        assert "permessi" in reason

    async def test_mute_admin_target(self):
        ok, reason = await mod.mute(_FakeBot("can't restrict self"), -100, 1, 600)
        assert ok is False
        assert "admin" in reason

    async def test_unmute_user_not_participant(self):
        ok, reason = await mod.unmute(_FakeBot("USER_NOT_PARTICIPANT"), -100, 1)
        assert ok is False
        assert "gruppo" in reason

    async def test_unknown_error_passthrough(self):
        ok, reason = await mod.ban(_FakeBot("Some weird error"), -100, 1)
        assert ok is False
        assert "Errore Telegram" in reason
