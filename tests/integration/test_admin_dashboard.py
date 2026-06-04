"""Integration tests for the admin dashboard's shared logic.

The callback handlers themselves are thin wrappers around the service layer; here
we test the pieces with real behaviour: the shared warn helper (parity with the
/warn command, including audit + escalation), the user-detail renderer, and the
paginated user picker query.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import config_data.config as cfg
from database.models import AdminAction
from handlers.admin import apply_warning
from handlers.admin_dashboard import render_user_detail
from services import admin_service


class _StubBot:
    """Records moderation calls so we can assert escalation happened."""

    def __init__(self):
        self.banned = []
        self.restricted = []

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.banned.append((chat_id, user_id))

    async def restrict_chat_member(self, chat_id, user_id, permissions, **kw):
        self.restricted.append((chat_id, user_id))


class TestApplyWarning:
    async def test_single_warn_logs_no_escalation(self, session, user_factory):
        await user_factory(tg_id=10, coins=0)
        count, escalation = await apply_warning(_StubBot(), session, admin_id=1, target_id=10,
                                                chat_id=-100123, reason="spam")
        await session.commit()
        assert count == 1
        assert escalation == ""
        actions = (await session.execute(
            select(AdminAction).where(AdminAction.action_type == "warn")
        )).scalars().all()
        assert len(actions) == 1 and actions[0].target_tg_id == 10

    async def test_mute_escalation_at_threshold(self, session, user_factory, monkeypatch):
        monkeypatch.setattr(cfg.settings, "warn_mute_threshold", 3)
        monkeypatch.setattr(cfg.settings, "warn_ban_threshold", 5)
        await user_factory(tg_id=11, coins=0)
        bot = _StubBot()
        for _ in range(2):
            await apply_warning(bot, session, 1, 11, -100123, None)
        count, escalation = await apply_warning(bot, session, 1, 11, -100123, None)
        await session.commit()
        assert count == 3
        assert "mute automatico" in escalation
        assert bot.restricted  # the mute was actually applied via the bot

    async def test_ban_escalation_at_threshold(self, session, user_factory, monkeypatch):
        monkeypatch.setattr(cfg.settings, "warn_mute_threshold", 3)
        monkeypatch.setattr(cfg.settings, "warn_ban_threshold", 5)
        await user_factory(tg_id=12, coins=0)
        bot = _StubBot()
        for _ in range(4):
            await apply_warning(bot, session, 1, 12, -100123, None)
        count, escalation = await apply_warning(bot, session, 1, 12, -100123, None)
        await session.commit()
        assert count == 5
        assert "BAN automatico" in escalation
        assert bot.banned


class TestRenderUserDetail:
    async def test_returns_text_and_kb(self, session, user_factory, monkeypatch):
        monkeypatch.setattr(cfg.settings, "group_id", 0)  # skip live group lookup
        await user_factory(tg_id=20, username="alice", coins=1234)
        rendered = await render_user_detail(bot=None, db_session=session, tg_id=20)
        assert rendered is not None
        text, kb = rendered
        assert "1,234" in text
        assert "alice" in text
        assert kb is not None

    async def test_unknown_user_returns_none(self, session, monkeypatch):
        monkeypatch.setattr(cfg.settings, "group_id", 0)
        assert await render_user_detail(bot=None, db_session=session, tg_id=999) is None


class TestUserPicker:
    async def test_pagination_and_count(self, session, user_factory):
        for i in range(1, 6):
            await user_factory(tg_id=i, username=f"u{i}")
        assert await admin_service.count_users(session) == 5
        page0 = await admin_service.list_users(session, offset=0, limit=2)
        page1 = await admin_service.list_users(session, offset=2, limit=2)
        assert len(page0) == 2 and len(page1) == 2
        ids0 = {u.tg_id for u, _ in page0}
        ids1 = {u.tg_id for u, _ in page1}
        assert ids0.isdisjoint(ids1)  # no overlap between pages
