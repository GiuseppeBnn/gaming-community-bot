"""on_chat_member keeps the bot-level ban in sync with NATIVE (Telegram-UI)
moderation, so a user banned/unbanned outside the bot's own /ban is also cut off
from / restored to the bot in private. Bot-initiated changes are left alone (the
command handlers own ``is_banned``; a /kick transits through "kicked" transiently).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

import middlewares.ban_guard as bg
from database.models import User
from handlers.group_events import on_chat_member
from services import group_registry

GROUP_ID = -100999
BOT_ID = 42


@pytest.fixture(autouse=True)
def _env():
    bg.invalidate_all()
    group_registry.set_runtime_group_id(GROUP_ID)
    yield
    group_registry.set_runtime_group_id(None)
    bg.invalidate_all()


def _event(*, target_id, old, new, actor_id, chat_id=GROUP_ID):
    user = SimpleNamespace(id=target_id)
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=actor_id),
        bot=SimpleNamespace(id=BOT_ID),
        old_chat_member=SimpleNamespace(status=old, user=user),
        new_chat_member=SimpleNamespace(status=new, user=user),
    )


async def _banned(session, tg_id):
    return await session.scalar(select(User.is_banned).where(User.tg_id == tg_id))


async def test_native_ban_sets_bot_ban(session, user_factory):
    await user_factory(tg_id=50, coins=0)
    await on_chat_member(_event(target_id=50, old="member", new="kicked", actor_id=7), session)
    assert await _banned(session, 50) is True


async def test_native_ban_without_row_creates_banned_stub(session):
    await on_chat_member(_event(target_id=51, old="member", new="kicked", actor_id=7), session)
    user = await session.get(User, 51)
    assert user is not None and user.is_banned is True


async def test_native_unban_clears_bot_ban(session, user_factory):
    await user_factory(tg_id=52, coins=0, is_banned=True)
    await on_chat_member(_event(target_id=52, old="kicked", new="left", actor_id=7), session)
    assert await _banned(session, 52) is False


async def test_bot_initiated_change_is_skipped(session, user_factory):
    # The bot performed the change (actor == bot): a /kick transits through "kicked",
    # so re-deriving is_banned here would wrongly ban a kicked (rejoinable) user.
    await user_factory(tg_id=53, coins=0, is_banned=False)
    await on_chat_member(_event(target_id=53, old="member", new="kicked", actor_id=BOT_ID), session)
    assert await _banned(session, 53) is False


async def test_change_in_other_group_ignored(session, user_factory):
    await user_factory(tg_id=54, coins=0)
    await on_chat_member(
        _event(target_id=54, old="member", new="kicked", actor_id=7, chat_id=-100111), session
    )
    assert await _banned(session, 54) is False
