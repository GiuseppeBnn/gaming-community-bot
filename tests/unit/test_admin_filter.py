"""Unit tests for filters.admin_filter.is_admin (dynamic Telegram admin check)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import filters.admin_filter as af


@pytest.fixture(autouse=True)
def _clear_cache():
    af.invalidate_admin_cache()
    yield
    af.invalidate_admin_cache()


class _FakeBot:
    """Bot stub for get_chat_administrators."""

    def __init__(self, admin_ids=None, raise_error=False):
        self._admin_ids = admin_ids or []
        self._raise = raise_error
        self.calls = 0

    async def get_chat_administrators(self, group_id):
        self.calls += 1
        if self._raise:
            raise RuntimeError("API down")
        return [SimpleNamespace(user=SimpleNamespace(id=i)) for i in self._admin_ids]


async def test_configured_admin(monkeypatch):
    monkeypatch.setattr(af.settings, "admin_ids", [42])
    monkeypatch.setattr(af.settings, "group_id", 0)
    assert await af.is_admin(_FakeBot(), 42) is True


async def test_not_admin_no_group(monkeypatch):
    monkeypatch.setattr(af.settings, "admin_ids", [42])
    monkeypatch.setattr(af.settings, "group_id", 0)
    assert await af.is_admin(_FakeBot(), 99) is False


async def test_telegram_group_admin(monkeypatch):
    monkeypatch.setattr(af.settings, "admin_ids", [])
    monkeypatch.setattr(af.settings, "group_id", -100123)
    bot = _FakeBot(admin_ids=[7, 8])
    assert await af.is_admin(bot, 7) is True
    assert await af.is_admin(bot, 999) is False


async def test_cache_avoids_repeated_api_calls(monkeypatch):
    monkeypatch.setattr(af.settings, "admin_ids", [])
    monkeypatch.setattr(af.settings, "group_id", -100123)
    bot = _FakeBot(admin_ids=[7])
    await af.is_admin(bot, 7)
    await af.is_admin(bot, 7)
    assert bot.calls == 1  # second lookup served from cache


async def test_fail_closed_on_api_error(monkeypatch):
    monkeypatch.setattr(af.settings, "admin_ids", [42])
    monkeypatch.setattr(af.settings, "group_id", -100123)
    bot = _FakeBot(raise_error=True)
    assert await af.is_admin(bot, 42) is True   # configured admin still works
    assert await af.is_admin(bot, 7) is False   # no powers granted on error


async def test_api_error_is_not_cached(monkeypatch):
    """A transient API error must not poison the cache: once the API recovers,
    the group owner/admins are recognized on the very next check."""
    monkeypatch.setattr(af.settings, "admin_ids", [])
    monkeypatch.setattr(af.settings, "group_id", -100123)

    class _FlakyBot:
        def __init__(self):
            self.calls = 0

        async def get_chat_administrators(self, group_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("API down")
            return [SimpleNamespace(user=SimpleNamespace(id=7))]

    bot = _FlakyBot()
    assert await af.is_admin(bot, 7) is False  # first call: API down → fail closed
    assert await af.is_admin(bot, 7) is True   # API recovered → owner now recognized
    assert bot.calls == 2                       # failure was retried, not served stale
