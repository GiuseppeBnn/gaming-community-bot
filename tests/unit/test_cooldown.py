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


# ---------------------------------------------------------------------------
# _prune — the store must stay bounded in a busy group
#
# Moved here from tests/unit/test_fun_ai_hardening.py when fun_ai stopped keeping
# its own `_last_used` dict: the behaviour did not disappear, it moved, so the only
# coverage the repo had for it moved too.
# ---------------------------------------------------------------------------

def test_prune_is_a_noop_below_the_threshold():
    """Pruning is a **memory** guard, not part of the cooldown decision: below the
    threshold even a long-expired entry is left alone. `remaining()` already treats
    it as ready, so dropping it would be work for nothing."""
    import time

    cooldown._store[("ai", 1)] = time.monotonic() - 10_000

    cooldown._prune(time.monotonic())

    assert ("ai", 1) in cooldown._store


def test_prune_drops_expired_entries_and_keeps_recent_ones():
    """Above the threshold, entries older than `_PRUNE_MAX_AGE` go and live ones
    stay — a group that runs a lot of throttled commands must not grow the store
    without bound, and must not lose an active cooldown to make room."""
    import time

    now = time.monotonic()
    expired = now - cooldown._PRUNE_MAX_AGE - 1
    for i in range(cooldown._PRUNE_THRESHOLD + 50):
        cooldown._store[("ai", 1000 + i)] = expired
    cooldown._store[("ai", 42)] = now  # active

    cooldown._prune(now)

    assert ("ai", 42) in cooldown._store, "pruned a cooldown that was still running"
    assert ("ai", 1000) not in cooldown._store
    assert len(cooldown._store) == 1
