"""static_reply.reply_static: 'sostituisci il precedente' anti-flood.

One live bot reply per (chat, user, command) in groups (delete the previous one
before sending a fresh copy); in private just send, no dedup, no delete.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType

import utils.static_reply as sr
from utils.static_reply import reply_static


class _Sent:
    def __init__(self, message_id: int):
        self.message_id = message_id


class _FakeBot:
    def __init__(self):
        self.deleted: list[tuple[int, int]] = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _FakeMessage:
    """Minimal Message stand-in: answer() returns a Sent with an incrementing id."""

    def __init__(self, chat_id: int, user_id: int, chat_type: ChatType, bot: _FakeBot):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.from_user = SimpleNamespace(id=user_id)
        self.bot = bot
        self.sent: list[tuple[str, dict]] = []
        self._mid = 1000

    async def answer(self, text: str, **kwargs):
        self._mid += 1
        self.sent.append((text, kwargs))
        return _Sent(self._mid)


@pytest.fixture(autouse=True)
def _clean_state():
    sr.reset()
    yield
    sr.reset()


async def test_group_replace_deletes_previous_reply():
    bot = _FakeBot()
    msg = _FakeMessage(chat_id=-100, user_id=7, chat_type=ChatType.GROUP, bot=bot)

    first = await reply_static(msg, "saldo: 100", "saldo")
    assert bot.deleted == []                       # nothing to delete yet
    assert len(msg.sent) == 1

    second = await reply_static(msg, "saldo: 120", "saldo")
    assert bot.deleted == [(-100, first.message_id)]  # previous reply removed
    assert second.message_id != first.message_id
    assert len(msg.sent) == 2


async def test_group_buckets_are_independent():
    bot = _FakeBot()
    msg = _FakeMessage(chat_id=-100, user_id=7, chat_type=ChatType.GROUP, bot=bot)
    await reply_static(msg, "saldo", "saldo")
    await reply_static(msg, "profilo", "profilo")
    # Different commands → no cross-deletion.
    assert bot.deleted == []


async def test_supergroup_also_dedups():
    bot = _FakeBot()
    msg = _FakeMessage(chat_id=-100, user_id=1, chat_type=ChatType.SUPERGROUP, bot=bot)
    a = await reply_static(msg, "x", "saldo")
    await reply_static(msg, "y", "saldo")
    assert bot.deleted == [(-100, a.message_id)]


async def test_private_does_not_track_or_delete():
    bot = _FakeBot()
    msg = _FakeMessage(chat_id=42, user_id=7, chat_type=ChatType.PRIVATE, bot=bot)
    await reply_static(msg, "a", "saldo")
    await reply_static(msg, "b", "saldo")
    assert bot.deleted == []                        # never deletes in private
    assert len(msg.sent) == 2


async def test_passes_through_reply_markup():
    bot = _FakeBot()
    msg = _FakeMessage(chat_id=-100, user_id=7, chat_type=ChatType.GROUP, bot=bot)
    kb = object()
    await reply_static(msg, "text", "quiz", reply_markup=kb)
    assert msg.sent[0][1] == {"reply_markup": kb}


async def test_delete_failure_is_swallowed():
    class _BoomBot(_FakeBot):
        async def delete_message(self, chat_id, message_id):
            raise RuntimeError("message too old")

    bot = _BoomBot()
    msg = _FakeMessage(chat_id=-100, user_id=7, chat_type=ChatType.GROUP, bot=bot)
    await reply_static(msg, "a", "saldo")
    # Must not raise even if the old message can't be deleted.
    second = await reply_static(msg, "b", "saldo")
    assert second is not None
    assert len(msg.sent) == 2
