"""Access-control regression tests for poll creation (STEERING §8/§16).

Two defects this guards against:

1. The interactive creation flow must run **in private chat**, never in the group
   where anyone can read the prompts or interfere with the FSM. ``/sondaggio`` in a
   group must hand back a deep-link button and start *nothing* there.
2. The deep-link landing (``?start=create_poll``) lives on the **public** common
   router, so it must **re-check admin** itself — a non-admin could craft the link
   directly and otherwise bypass the originating command's admin filter.
"""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from services import poll_service


def _fresh_state(user_id: int = 1) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


class _BotWithMe:
    async def get_me(self):
        return SimpleNamespace(username="testbot")


class _GroupMessage:
    """A /sondaggio sent in a group chat."""

    def __init__(self, user_id: int = 1):
        self.chat = SimpleNamespace(type="group", id=-1001)
        self.from_user = SimpleNamespace(id=user_id)
        self.bot = _BotWithMe()
        self.replies: list[tuple[str, object]] = []

    async def reply(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))

    async def answer(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class _PrivateMessage:
    """A deep-link landing in the bot's private chat."""

    def __init__(self, user_id: int):
        self.from_user = SimpleNamespace(id=user_id)
        self.bot = object()
        self.replies: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


def _button_urls(kb) -> list[str]:
    return [b.url for row in kb.inline_keyboard for b in row if getattr(b, "url", None)]


class TestSondaggioStaysPrivate:
    """`/sondaggio` in a group must NOT run the FSM there — it redirects to private."""

    async def test_group_sondaggio_redirects_and_starts_nothing(self):
        from handlers.schedule import cmd_sondaggio

        state = _fresh_state()
        msg = _GroupMessage()

        await cmd_sondaggio(msg, state)

        # No FSM started in the group chat...
        assert await state.get_state() is None
        # ...and the only reply is a deep-link button into the private chat.
        assert msg.replies
        _text, kb = msg.replies[-1]
        assert any("start=create_poll" in u for u in _button_urls(kb))


class TestCreatePollDeepLinkGate:
    """The public ?start=create_poll landing must re-check admin itself."""

    async def test_non_admin_is_denied(self, session, user_factory, monkeypatch):
        import handlers.common as common

        # An onboarded NON-admin (so we reach the payload branch, not the rules gate).
        await user_factory(2, "member", onboarding_completed=True)

        async def not_admin(bot, user_id):
            return False

        monkeypatch.setattr(common, "is_bot_admin", not_admin)

        state = _fresh_state(2)
        msg = _PrivateMessage(2)
        await common.cmd_start(msg, CommandObject(command="start", args="create_poll"), state, session)

        # Denied: no FSM started, no poll stored, explicit refusal shown.
        assert await state.get_state() is None
        assert await poll_service.list_ready(session) == []
        assert any("non autorizzato" in text.lower() for text, _ in msg.replies)

    async def test_admin_starts_creation_in_private(self, session, user_factory, monkeypatch):
        import handlers.common as common
        from handlers.events import PollTemplateStates

        await user_factory(1, "boss", onboarding_completed=True)

        async def yes_admin(bot, user_id):
            return True

        monkeypatch.setattr(common, "is_bot_admin", yes_admin)

        state = _fresh_state(1)
        msg = _PrivateMessage(1)
        await common.cmd_start(msg, CommandObject(command="start", args="create_poll"), state, session)

        # Admin: the canonical creation FSM is entered (question step), nothing posted.
        assert await state.get_state() == PollTemplateStates.question.state
        assert msg.replies  # prompted for the question
