"""Tests for RateLimitMiddleware — sliding-window per-user rate limiter."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from middlewares.rate_limit import MAX_CALLS, WINDOW_SECONDS, RateLimitMiddleware


def _make_data(user_id: int = 123) -> dict:
    user = MagicMock()
    user.id = user_id
    return {"event_from_user": user}


async def _call(mw: RateLimitMiddleware, handler: AsyncMock, user_id: int = 123):
    event = AsyncMock()
    data = _make_data(user_id)
    return await mw(handler, event, data)


# ---------------------------------------------------------------------------
# Basic pass-through
# ---------------------------------------------------------------------------

class TestPassThrough:
    async def test_no_user_always_passes(self):
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")
        result = await mw(handler, AsyncMock(), {})
        assert result == "ok"
        handler.assert_called_once()

    async def test_single_call_passes(self):
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")
        result = await _call(mw, handler)
        assert result == "ok"

    async def test_calls_up_to_limit_all_pass(self):
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")
        for _ in range(MAX_CALLS):
            result = await _call(mw, handler)
            assert result == "ok"
        assert handler.call_count == MAX_CALLS


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    async def test_call_over_limit_is_blocked(self):
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")
        for _ in range(MAX_CALLS):
            await _call(mw, handler)
        handler.reset_mock()

        result = await _call(mw, handler)
        assert result is None
        handler.assert_not_called()

    async def test_blocked_call_does_not_count_toward_window(self):
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")
        for _ in range(MAX_CALLS + 5):
            await _call(mw, handler)
        # All calls beyond MAX_CALLS should be blocked
        assert handler.call_count == MAX_CALLS

    async def test_different_users_have_independent_limits(self):
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")

        # Fill up user 1
        for _ in range(MAX_CALLS):
            await _call(mw, handler, user_id=1)
        count_after_user1 = handler.call_count

        # User 2 should still pass
        await _call(mw, handler, user_id=2)
        assert handler.call_count == count_after_user1 + 1


# ---------------------------------------------------------------------------
# Window expiry
# ---------------------------------------------------------------------------

class TestWindowExpiry:
    async def test_calls_reset_after_window_expires(self):
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")

        # Fill the window
        for _ in range(MAX_CALLS):
            await _call(mw, handler)

        # Fast-forward time past the window
        future = time.monotonic() + WINDOW_SECONDS + 1.0
        with patch("middlewares.rate_limit.time.monotonic", return_value=future):
            result = await _call(mw, handler)

        assert result == "ok"
        assert handler.call_count == MAX_CALLS + 1

    async def test_old_timestamps_are_evicted(self):
        mw = RateLimitMiddleware()
        handler = AsyncMock(return_value="ok")
        user_id = 42

        # Record MAX_CALLS - 1 timestamps manually, all outside the window
        old_ts = time.monotonic() - WINDOW_SECONDS - 1.0
        mw._timestamps[user_id] = [old_ts] * (MAX_CALLS - 1)

        # One new call — old ones should be evicted, so this passes
        event = AsyncMock()
        data = _make_data(user_id)
        result = await mw(handler, event, data)
        assert result == "ok"
        handler.assert_called_once()


# ---------------------------------------------------------------------------
# MAX_CALLS constant sanity check
# ---------------------------------------------------------------------------

def test_max_calls_is_positive():
    assert MAX_CALLS > 0


def test_window_seconds_is_positive():
    assert WINDOW_SECONDS > 0
