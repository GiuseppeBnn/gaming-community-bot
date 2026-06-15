"""BannedUserMiddleware: a bot-banned user's updates are dropped silently
(handler never runs, nothing is sent); the cache honors explicit invalidation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import middlewares.ban_guard as bg
from middlewares.ban_guard import BannedUserMiddleware


@pytest.fixture(autouse=True)
def _clean_cache():
    bg.invalidate_all()
    yield
    bg.invalidate_all()


def _data(user_id, session):
    return {"event_from_user": SimpleNamespace(id=user_id), "db_session": session}


async def _run(mw, data):
    """Invoke the middleware with a handler that records whether it ran."""
    calls = []

    async def handler(event, d):
        calls.append(True)
        return "ran"

    result = await mw(handler, object(), data)
    return calls, result


async def test_banned_user_is_dropped_silently(session, user_factory):
    await user_factory(1, is_banned=True)
    calls, result = await _run(BannedUserMiddleware(), _data(1, session))
    assert calls == []          # handler never invoked
    assert result is None        # silent: nothing returned/sent


async def test_non_banned_user_passes_through(session, user_factory):
    await user_factory(2, is_banned=False)
    calls, result = await _run(BannedUserMiddleware(), _data(2, session))
    assert calls == [True]
    assert result == "ran"


async def test_unknown_user_passes_through(session):
    # No row at all → not banned → allowed.
    calls, result = await _run(BannedUserMiddleware(), _data(999, session))
    assert calls == [True]


async def test_missing_user_or_session_passes_through(session):
    mw = BannedUserMiddleware()
    calls, _ = await _run(mw, {"event_from_user": None, "db_session": session})
    assert calls == [True]
    calls, _ = await _run(mw, {"event_from_user": SimpleNamespace(id=1), "db_session": None})
    assert calls == [True]


async def test_invalidation_applies_unban_on_next_update(session, user_factory):
    user, _ = await user_factory(3, is_banned=True)
    mw = BannedUserMiddleware()

    calls, _ = await _run(mw, _data(3, session))
    assert calls == []           # banned → dropped (and cached as banned)

    # Unban in the DB and invalidate the cache (what /sban does after commit).
    user.is_banned = False
    await session.commit()
    bg.invalidate(3)

    calls, result = await _run(mw, _data(3, session))
    assert calls == [True]       # cache re-read → now allowed
    assert result == "ran"
