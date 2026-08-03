"""End-to-end admin gating across EVERY admin router (STEERING §8).

Unlike test_schedule_admin_gate (which stubs ``is_admin`` to check the wiring),
this exercises the **real** ``is_admin`` so the policy itself is pinned down:

    admin  ==  env owner (settings.admin_ids)  OR  current Telegram group
               administrator/creator  —  and NOBODY else.

Every 100%-admin router now carries a root-level admin filter, so a genuine
non-admin is rejected before any handler runs, even if a per-handler filter were
ever forgotten. We assert that across all of them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import filters.admin_filter as af
import handlers.admin as admin
import handlers.admin_betting as admin_betting
import handlers.admin_dashboard as admin_dashboard
import handlers.backup as backup
import handlers.dialog_spike as dialog_spike
import handlers.events as events
import handlers.schedule as schedule
from services import group_registry

_TEST_GROUP = -1009999999999
_ENV_OWNER = 999          # listed in settings.admin_ids
_TG_GROUP_ADMIN = 100     # returned by get_chat_administrators
_RANDOM_USER = 1          # neither → must get nothing

ADMIN_ROUTERS = [
    admin.router,
    admin_dashboard.router,
    admin_betting.router,
    backup.router,
    events.router,
    schedule.router,
    dialog_spike.router,
]


class _BotWithAdmins:
    """Minimal Bot stand-in: get_chat_administrators returns a fixed admin set,
    and get_chat_member_count a total (defaults to a large group so the
    all-members-are-admins guard never trips unless a test asks for it)."""

    def __init__(self, admin_ids, member_count: int | None = None):
        self._ids = list(admin_ids)
        self._member_count = member_count if member_count is not None else len(admin_ids) + 50

    async def get_chat_administrators(self, chat_id):
        return [SimpleNamespace(user=SimpleNamespace(id=i)) for i in self._ids]

    async def get_chat_member_count(self, chat_id):
        return self._member_count


def _event(user_id: int, bot) -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), bot=bot)


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    """Env owner = 999; the group's only Telegram admin = 100; fresh cache."""
    monkeypatch.setattr(af.settings, "admin_ids", [_ENV_OWNER])
    af._cache.clear()
    group_registry.set_runtime_group_id(_TEST_GROUP)
    yield
    af._cache.clear()
    group_registry.set_runtime_group_id(None)


@pytest.mark.parametrize("router", ADMIN_ROUTERS)
async def test_random_non_admin_is_denied_everywhere(router):
    bot = _BotWithAdmins([_TG_GROUP_ADMIN])
    msg_ok, _ = await router.message.check_root_filters(_event(_RANDOM_USER, bot))
    cb_ok, _ = await router.callback_query.check_root_filters(_event(_RANDOM_USER, bot))
    assert msg_ok is False, f"{router!r} let a non-admin through (message)"
    assert cb_ok is False, f"{router!r} let a non-admin through (callback)"


@pytest.mark.parametrize("router", ADMIN_ROUTERS)
async def test_telegram_group_admin_is_allowed(router):
    bot = _BotWithAdmins([_TG_GROUP_ADMIN])
    msg_ok, _ = await router.message.check_root_filters(_event(_TG_GROUP_ADMIN, bot))
    cb_ok, _ = await router.callback_query.check_root_filters(_event(_TG_GROUP_ADMIN, bot))
    assert msg_ok is True and cb_ok is True


@pytest.mark.parametrize("router", ADMIN_ROUTERS)
async def test_env_owner_is_allowed_even_if_not_a_telegram_admin(router):
    # The owner from .env is admin even when the group lists *no* admins / the
    # API call fails (fail-closed only affects the Telegram half).
    bot = _BotWithAdmins([])  # empty group admin set
    msg_ok, _ = await router.message.check_root_filters(_event(_ENV_OWNER, bot))
    assert msg_ok is True


class TestAllMembersAreAdminsGuard:
    """The legacy basic-group 'all members are admins' mode must NOT elevate
    everyone — only the .env owner stays admin there."""

    async def test_whole_group_listed_as_admins_grants_nobody(self):
        # 5-member group where Telegram reports all 5 as administrators.
        members = [10, 11, 12, 13, 14]
        bot = _BotWithAdmins(members, member_count=5)
        af._cache.clear()
        for uid in members:
            assert await af.is_admin(bot, uid) is False
        # ...but the env owner is unaffected.
        assert await af.is_admin(bot, _ENV_OWNER) is True

    async def test_real_admin_subset_is_still_trusted(self):
        # 2 admins out of 100 members → a genuine subset → trusted as usual.
        bot = _BotWithAdmins([10, 11], member_count=100)
        af._cache.clear()
        assert await af.is_admin(bot, 10) is True
        assert await af.is_admin(bot, 11) is True
        assert await af.is_admin(bot, 12) is False


async def test_demoted_admin_loses_access_after_cache_invalidation():
    """An ex-admin (was in the cached set) is denied once the cache is dropped
    and Telegram no longer lists them — the demotion path calls
    invalidate_admin_cache(), so there is no lingering access beyond that."""
    # User 100 starts as a group admin → allowed, and the set gets cached.
    bot_before = _BotWithAdmins([_TG_GROUP_ADMIN])
    assert await af.is_admin(bot_before, _TG_GROUP_ADMIN) is True

    # Demotion: Telegram no longer lists them; the handler invalidates the cache.
    af.invalidate_admin_cache()
    bot_after = _BotWithAdmins([])  # 100 is gone from the admin set
    assert await af.is_admin(bot_after, _TG_GROUP_ADMIN) is False
