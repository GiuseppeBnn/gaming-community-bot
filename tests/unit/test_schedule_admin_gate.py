"""Regression test for the privilege-escalation fix (STEERING §8).

`schedule.router` (and, defensively, `events.router`) are admin-only. Several of
their handlers are driven purely by FSM state (run-at input, the /sondaggio poll
flow, the pickers). FSM state has NO TTL, so before the fix an admin who started a
flow and then *lost* admin could still drive it to completion. The fix gates the
whole router via a root-level admin filter — checked here behaviourally through
aiogram's ``check_root_filters`` with a stubbed ``is_admin``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import filters.admin_filter as af
import handlers.events as events
import handlers.schedule as schedule


def _event(user_id: int):
    """A minimal stand-in: the admin filters only read ``from_user`` and ``bot``."""
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), bot=object())


@pytest.fixture
def stub_is_admin(monkeypatch):
    """Only user 1 is admin; user 2 was demoted (never/no-longer admin)."""
    async def fake_is_admin(bot, user_id):
        return user_id == 1
    monkeypatch.setattr(af, "is_admin", fake_is_admin)


@pytest.mark.parametrize("router", [schedule.router, events.router])
async def test_non_admin_denied_on_messages(router, stub_is_admin):
    passed, _ = await router.message.check_root_filters(_event(user_id=2))
    assert passed is False  # demoted user cannot drive the FSM message handlers


@pytest.mark.parametrize("router", [schedule.router, events.router])
async def test_non_admin_denied_on_callbacks(router, stub_is_admin):
    passed, _ = await router.callback_query.check_root_filters(_event(user_id=2))
    assert passed is False  # demoted user cannot drive the inline callbacks


@pytest.mark.parametrize("router", [schedule.router, events.router])
async def test_admin_allowed(router, stub_is_admin):
    msg_passed, _ = await router.message.check_root_filters(_event(user_id=1))
    cb_passed, _ = await router.callback_query.check_root_filters(_event(user_id=1))
    assert msg_passed is True and cb_passed is True  # genuine admin is unaffected
