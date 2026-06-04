"""Unit tests for the AI command cooldown (handlers/fun_ai._check_cooldown)."""

from __future__ import annotations

import pytest

import handlers.fun_ai as fun_ai


class _FakeMessage:
    def __init__(self, user_id: int):
        self.from_user = type("U", (), {"id": user_id})()
        self.bot = object()
        self.replies: list[str] = []

    async def reply(self, text: str):
        self.replies.append(text)


@pytest.fixture(autouse=True)
def _reset():
    fun_ai._last_used.clear()
    yield
    fun_ai._last_used.clear()


async def test_admin_is_exempt(monkeypatch):
    async def fake_is_admin(bot, uid):
        return True
    monkeypatch.setattr(fun_ai, "is_admin", fake_is_admin)
    msg = _FakeMessage(1)
    # Even with a recent timestamp, an admin is never blocked.
    fun_ai._mark_used(msg)
    assert await fun_ai._check_cooldown(msg) is True


async def test_first_use_allowed_then_blocked(monkeypatch):
    async def fake_is_admin(bot, uid):
        return False
    monkeypatch.setattr(fun_ai, "is_admin", fake_is_admin)
    monkeypatch.setattr(fun_ai.settings, "ai_cooldown_seconds", 60)

    msg = _FakeMessage(2)
    assert await fun_ai._check_cooldown(msg) is True
    fun_ai._mark_used(msg)
    # Immediate second attempt is blocked with a reply.
    assert await fun_ai._check_cooldown(msg) is False
    assert msg.replies and "Aspetta" in msg.replies[-1]


async def test_window_expired_allows_again(monkeypatch):
    async def fake_is_admin(bot, uid):
        return False
    monkeypatch.setattr(fun_ai, "is_admin", fake_is_admin)
    monkeypatch.setattr(fun_ai.settings, "ai_cooldown_seconds", 0)

    msg = _FakeMessage(3)
    fun_ai._mark_used(msg)
    # With a 0s window the cooldown never blocks.
    assert await fun_ai._check_cooldown(msg) is True
