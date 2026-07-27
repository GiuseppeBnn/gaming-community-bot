"""Tests for GroupMemberMiddleware utilities."""

from __future__ import annotations

from unittest.mock import MagicMock



class TestInvalidateCache:
    def test_removes_user_from_cache(self):
        import middlewares.group_guard as gg

        # Manually populate the module-level cache
        gg._cache[42] = (True, 9999.0)
        assert 42 in gg._cache

        gg.invalidate_cache(42)
        assert 42 not in gg._cache

    def test_safe_when_user_not_in_cache(self):
        import middlewares.group_guard as gg

        # Should not raise even if user was never cached
        gg.invalidate_cache(99999)

    def test_only_removes_target_user(self):
        import middlewares.group_guard as gg

        gg._cache[1] = (True, 0.0)
        gg._cache[2] = (False, 0.0)
        gg.invalidate_cache(1)

        assert 1 not in gg._cache
        assert 2 in gg._cache
        # Cleanup
        gg._cache.pop(2, None)


class TestChatType:
    # _chat_type dispatches on isinstance(event, Message) and isinstance(event, CallbackQuery).
    # Aiogram v3 Pydantic models can't be spec'd with MagicMock (fields aren't regular attrs),
    # so we test the fall-through (None) path with plain objects, and the isinstance paths
    # via monkeypatching the module-level Message/CallbackQuery references.

    def test_unknown_event_returns_none(self):
        from middlewares.group_guard import _chat_type

        assert _chat_type(object()) is None

    def test_plain_mock_returns_none(self):
        from middlewares.group_guard import _chat_type

        assert _chat_type(MagicMock()) is None

    def test_message_branch_via_patch(self, monkeypatch):
        from middlewares import group_guard

        # Build a plain fake object that has the .chat.type attribute
        fake_chat = MagicMock()
        fake_chat.type = "private"
        fake_msg = MagicMock()
        fake_msg.chat = fake_chat

        # Temporarily make Message point at a class that fake_msg IS an instance of
        FakeMsgClass = type(fake_msg)
        monkeypatch.setattr(group_guard, "Message", FakeMsgClass)

        result = group_guard._chat_type(fake_msg)
        assert result == "private"

    def test_callback_branch_via_patch(self, monkeypatch):
        from middlewares import group_guard

        fake_inner_chat = MagicMock()
        fake_inner_chat.type = "supergroup"
        fake_inner_msg = MagicMock()
        fake_inner_msg.chat = fake_inner_chat
        fake_cb = MagicMock()
        fake_cb.message = fake_inner_msg

        FakeCbClass = type(fake_cb)
        monkeypatch.setattr(group_guard, "Message", type(None))      # not a Message
        monkeypatch.setattr(group_guard, "CallbackQuery", FakeCbClass)

        result = group_guard._chat_type(fake_cb)
        assert result == "supergroup"

    def test_callback_without_message_via_patch(self, monkeypatch):
        from middlewares import group_guard

        fake_cb = MagicMock()
        fake_cb.message = None

        FakeCbClass = type(fake_cb)
        monkeypatch.setattr(group_guard, "Message", type(None))
        monkeypatch.setattr(group_guard, "CallbackQuery", FakeCbClass)

        assert group_guard._chat_type(fake_cb) is None


class TestNonMemberStatuses:
    def test_non_member_statuses_set(self):
        from middlewares.group_guard import _NON_MEMBER_STATUSES

        assert "left" in _NON_MEMBER_STATUSES
        assert "kicked" in _NON_MEMBER_STATUSES
        assert "banned" in _NON_MEMBER_STATUSES

    def test_active_statuses_not_in_set(self):
        from middlewares.group_guard import _NON_MEMBER_STATUSES

        assert "member" not in _NON_MEMBER_STATUSES
        assert "administrator" not in _NON_MEMBER_STATUSES
        assert "creator" not in _NON_MEMBER_STATUSES
