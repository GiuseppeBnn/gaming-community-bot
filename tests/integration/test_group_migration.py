"""Chat migration and cache invalidation — `handlers/group_events.py`.

Making a basic group public turns it into a supergroup with a **brand-new chat
id**. The id in the .env goes stale at that instant and every admin check, every
membership check and every group announcement silently starts pointing at a chat
that no longer exists. These handlers are the recovery: they catch the migration
service messages and move the bot's runtime group id.

Which makes the guard on them the interesting part, and the reason for this file:
the message says «this chat migrated to X», and it can arrive from **any** chat the
bot is in. Accepting one from a chat that is not the configured group would let an
unrelated group take over the bot — announcements, admin recognition and the
membership gate would all follow it. So both handlers check the chat id first, and
that check is what the tests below hold in place.

The rest is cache hygiene: a promotion, a demotion or a ban must take effect now,
not in up to 300 seconds when the TTL happens to expire.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

import filters.admin_filter as admin_filter
import middlewares.ban_guard as ban_guard
import middlewares.group_guard as group_guard
from database.models import BotState
from handlers import group_events
from services import group_registry

GROUP_ID = -100_999
NEW_GROUP_ID = -1_001_234_567
OTHER_CHAT = -100_555
BOT_ID = 42
USER_ID = 50


@pytest.fixture(autouse=True)
def _env():
    """Group id and both caches are module-level; restore all three."""
    group_registry.set_runtime_group_id(GROUP_ID)
    admin_filter._cache.clear()
    group_guard.invalidate_all()
    ban_guard.invalidate_all()
    yield
    group_registry.set_runtime_group_id(None)
    admin_filter._cache.clear()
    group_guard.invalidate_all()
    ban_guard.invalidate_all()


def _migrate_to(chat_id: int, new_id: int):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="group"),
        migrate_to_chat_id=new_id,
        migrate_from_chat_id=None,
    )


def _migrate_from(chat_id: int, old_id: int):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        migrate_to_chat_id=None,
        migrate_from_chat_id=old_id,
    )


def _member_event(*, target_id=USER_ID, old="member", new="administrator",
                  actor_id=7, chat_id=GROUP_ID):
    user = SimpleNamespace(id=target_id)
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=actor_id),
        bot=SimpleNamespace(id=BOT_ID),
        old_chat_member=SimpleNamespace(status=old, user=user),
        new_chat_member=SimpleNamespace(status=new, user=user),
    )


async def _stored(session, key: str) -> str | None:
    return await session.scalar(select(BotState.value).where(BotState.key == key))


class TestMigration:
    async def test_the_old_chats_farewell_moves_the_bot_to_the_new_one(self, session):
        await group_events.on_migrate_to(_migrate_to(GROUP_ID, NEW_GROUP_ID), session)

        assert group_registry.get_group_id() == NEW_GROUP_ID

    async def test_the_new_chats_greeting_does_the_same(self, session):
        """Only one half of the migration pair may reach the bot (it can be offline
        for the other), so each must work on its own."""
        await group_events.on_migrate_from(_migrate_from(NEW_GROUP_ID, GROUP_ID), session)

        assert group_registry.get_group_id() == NEW_GROUP_ID

    async def test_the_move_survives_a_restart(self, session):
        """It is written to bot_state precisely because the .env is now wrong and
        will stay wrong until someone edits it."""
        await group_events.on_migrate_to(_migrate_to(GROUP_ID, NEW_GROUP_ID), session)
        await session.rollback()  # only a committed migration survives this

        assert await _stored(session, "effective_group_id") == str(NEW_GROUP_ID)

    async def test_a_migration_from_another_chat_is_ignored(self, session):
        """The decisive one: this service message can arrive from any chat the bot
        is in. Following it would hand the bot — announcements, admin recognition,
        the membership gate — to a group that simply asked for it."""
        await group_events.on_migrate_to(_migrate_to(OTHER_CHAT, NEW_GROUP_ID), session)

        assert group_registry.get_group_id() == GROUP_ID
        assert await _stored(session, "effective_group_id") is None

    async def test_a_greeting_naming_another_chat_is_ignored_too(self, session):
        await group_events.on_migrate_from(_migrate_from(NEW_GROUP_ID, OTHER_CHAT), session)

        assert group_registry.get_group_id() == GROUP_ID

    async def test_both_caches_are_dropped_on_the_move(self, session):
        """They are keyed on the *old* group: an admin set and a membership answer
        computed against a chat that no longer exists are both wrong."""
        admin_filter._cache[GROUP_ID] = ({1, 2}, 9_999_999.0)
        group_guard._cache[USER_ID] = (True, 9_999_999.0)

        await group_events.on_migrate_to(_migrate_to(GROUP_ID, NEW_GROUP_ID), session)

        assert admin_filter._cache == {}
        assert group_guard._cache == {}


class TestCacheInvalidation:
    async def test_a_promotion_drops_the_admin_cache(self, session):
        """Otherwise a freshly promoted admin is refused for up to the 300s TTL."""
        admin_filter._cache[GROUP_ID] = ({1}, 9_999_999.0)

        await group_events.on_chat_member(
            _member_event(old="member", new="administrator"), session
        )

        assert admin_filter._cache == {}

    async def test_a_demotion_drops_it_too(self, session):
        """This direction matters more: a demoted admin keeping their powers is the
        one that can do damage."""
        admin_filter._cache[GROUP_ID] = ({1}, 9_999_999.0)

        await group_events.on_chat_member(
            _member_event(old="creator", new="member"), session
        )

        assert admin_filter._cache == {}

    async def test_an_ordinary_join_leaves_the_admin_cache_alone(self, session):
        """Nothing about the admin set changed; dropping it would mean re-querying
        Telegram on every join in a busy group."""
        admin_filter._cache[GROUP_ID] = ({1}, 9_999_999.0)

        await group_events.on_chat_member(
            _member_event(old="left", new="member"), session
        )

        assert GROUP_ID in admin_filter._cache

    async def test_any_membership_change_drops_that_users_membership_entry(self, session):
        group_guard._cache[USER_ID] = (True, 9_999_999.0)

        await group_events.on_chat_member(
            _member_event(old="member", new="left"), session
        )

        assert USER_ID not in group_guard._cache

    async def test_a_change_in_another_chat_touches_nothing(self, session):
        admin_filter._cache[GROUP_ID] = ({1}, 9_999_999.0)
        group_guard._cache[USER_ID] = (True, 9_999_999.0)

        await group_events.on_chat_member(
            _member_event(chat_id=OTHER_CHAT, old="member", new="administrator"), session
        )

        assert GROUP_ID in admin_filter._cache
        assert USER_ID in group_guard._cache

    async def test_the_bots_own_status_change_drops_the_admin_cache(self, session):
        """Being added, removed or promoted changes what the bot can see of the
        admin list, so the cached set is no longer trustworthy."""
        admin_filter._cache[GROUP_ID] = ({1}, 9_999_999.0)

        await group_events.on_my_chat_member(_member_event(target_id=BOT_ID))

        assert admin_filter._cache == {}


class TestBotInitiatedChanges:
    async def test_a_change_made_by_the_bot_itself_is_recognised(self):
        """`/kick` transits through "kicked": re-deriving the ban from that update
        would turn every kick into a ban."""
        assert group_events._initiated_by_bot(_member_event(actor_id=BOT_ID)) is True
        assert group_events._initiated_by_bot(_member_event(actor_id=7)) is False

    async def test_an_event_without_an_actor_is_not_attributed_to_the_bot(self):
        event = _member_event()
        event.from_user = None

        assert group_events._initiated_by_bot(event) is False
