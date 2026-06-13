"""Unit tests for the reusable per-(bucket, user) cooldown util."""

from __future__ import annotations

import pytest

from utils import cooldown


class _FakeUser:
    def __init__(self, uid: int) -> None:
        self.id = uid


class _FakeMessage:
    def __init__(self, uid: int) -> None:
        self.from_user = _FakeUser(uid)
        self.bot = object()
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw) -> None:
        self.replies.append(text)


@pytest.fixture(autouse=True)
def _clean():
    cooldown.reset()
    yield
    cooldown.reset()


def test_remaining_zero_when_unmarked():
    assert cooldown.remaining("b", 1, 10) == 0.0


def test_mark_then_remaining_positive():
    cooldown.mark("b", 1)
    assert cooldown.remaining("b", 1, 10) > 0


def test_buckets_are_independent():
    cooldown.mark("a", 1)
    assert cooldown.remaining("a", 1, 10) > 0
    assert cooldown.remaining("b", 1, 10) == 0.0


async def test_guard_allows_then_blocks_non_admin():
    msg = _FakeMessage(42)
    assert await cooldown.guard(msg, "x", 60, exempt_admin=False) is True
    # Second immediate call is blocked and replies with a wait notice.
    assert await cooldown.guard(msg, "x", 60, exempt_admin=False) is False
    assert msg.replies and "{s}" not in msg.replies[-1]


async def test_guard_separate_users_not_blocked():
    a, b = _FakeMessage(1), _FakeMessage(2)
    assert await cooldown.guard(a, "x", 60, exempt_admin=False) is True
    assert await cooldown.guard(b, "x", 60, exempt_admin=False) is True
