"""/profilo and /saldo are public in the group (Punti 2-3): they answer in chat
(no privacy redirect) and /profilo never exposes the Telegram ID."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType

import filters.admin_filter as af
import handlers.common as common
import handlers.economy as economy
from services import group_registry
from utils import cooldown, static_reply


class _Sent:
    message_id = 1


class _FakeBot:
    async def delete_message(self, chat_id, message_id):
        return True


class _FakeMsg:
    def __init__(self, uid: int, chat_type: ChatType):
        self.from_user = SimpleNamespace(id=uid, username="mario", full_name="Mario Rossi")
        self.bot = _FakeBot()
        self.chat = SimpleNamespace(id=-100 if chat_type != ChatType.PRIVATE else uid, type=chat_type)
        self.answers: list[tuple[str, dict]] = []
        self.replies: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return _Sent()

    async def reply(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return _Sent()


@pytest.fixture(autouse=True)
def _isolate():
    group_registry.set_runtime_group_id(None)
    cooldown.reset()
    static_reply.reset()
    af._cache.clear()
    yield
    group_registry.set_runtime_group_id(None)
    cooldown.reset()
    static_reply.reset()


async def test_show_profilo_hides_telegram_id(session, user_factory):
    await user_factory(tg_id=321, username="mario", coins=777)
    msg = _FakeMsg(uid=321, chat_type=ChatType.PRIVATE)

    await common.show_profilo(msg, session)

    text = msg.answers[0][0]
    assert "Telegram ID" not in text
    assert "<code>321</code>" not in text  # the id itself is gone
    assert "777" in text                    # balance still shown


async def test_cmd_saldo_answers_in_group_without_redirect(session, user_factory):
    await user_factory(tg_id=321, username="mario", coins=500)
    msg = _FakeMsg(uid=321, chat_type=ChatType.GROUP)

    await economy.cmd_saldo(msg, session)

    # Answered in the group with the balance — not a "open in private" redirect.
    assert msg.answers, "expected an in-group answer"
    text = msg.answers[0][0]
    assert "500" in text
    assert msg.replies == []  # redirect_to_private would have used .reply with a button


async def test_cmd_profilo_answers_in_group_without_redirect(session, user_factory):
    await user_factory(tg_id=321, username="mario", coins=500)
    msg = _FakeMsg(uid=321, chat_type=ChatType.GROUP)

    await common.cmd_profilo(msg, session)

    assert msg.answers, "expected an in-group answer"
    assert msg.replies == []
    assert "Telegram ID" not in msg.answers[0][0]
