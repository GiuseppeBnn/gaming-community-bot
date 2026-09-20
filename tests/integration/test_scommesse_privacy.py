"""/scommesse must not answer in the group.

The list marks which events the caller already bet on («✅»). Posted in the group
that is personal data about the caller, readable by everyone — including people
who never placed a bet. So the command redirects to private, like /classifiche (§9).
"""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from database.models import EventStatus
from handlers.betting import cmd_scommesse
from handlers.callbacks import BetEventCb
from services import bet_service

USER_ID = 7


class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(username="mybot")

    async def delete_message(self, **kw):  # pragma: no cover - nothing to delete here
        pass


class _FakeMessage:
    def __init__(self, chat_type):
        self.bot = _FakeBot()
        self.from_user = SimpleNamespace(id=USER_ID)
        self.chat = SimpleNamespace(id=-100 if chat_type != "private" else USER_ID, type=chat_type)
        self.replies = []

    async def answer(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))
        return SimpleNamespace(message_id=len(self.replies))

    async def reply(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))
        return SimpleNamespace(message_id=len(self.replies))


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=USER_ID))


def _buttons(markup):
    if markup is None:
        return []
    return [b for row in markup.inline_keyboard for b in row]


async def _open_event(session, user_factory, title="Match"):
    await user_factory(9, "creator")
    event = await bet_service.create_event(
        session, creator_tg_id=9, title=title, description="d",
        options=[{"label": "A"}, {"label": "B"}],
        status=EventStatus.open.value,
    )
    await session.commit()
    return event


class TestScommesseInGroup:
    async def test_group_gets_a_deep_link_instead_of_the_list(self, session, user_factory):
        event = await _open_event(session, user_factory)
        message = _FakeMessage("supergroup")

        await cmd_scommesse(message, session, _state())

        text, markup = message.replies[-1]
        assert "privata" in text
        buttons = _buttons(markup)
        assert len(buttons) == 1
        assert buttons[0].url == "https://t.me/mybot?start=scommesse"
        # The event list itself never reaches the group.
        assert str(event.id) not in text

    async def test_group_reply_never_reveals_the_callers_bets(self, session, user_factory):
        event = await _open_event(session, user_factory)
        await user_factory(USER_ID, "bettor", coins=1000)
        detail = await bet_service.get_event_detail(session, event.id)
        opts = detail.options
        await bet_service.place_bet(session, USER_ID, event.id, opts[0].id, 100)
        await session.commit()

        message = _FakeMessage("supergroup")
        await cmd_scommesse(message, session, _state())

        blob = " ".join(t for t, _ in message.replies)
        blob += " ".join(
            b.text for _, m in message.replies for b in _buttons(m)
        )
        assert "✅" not in blob
        assert "già scommesso" not in blob


class TestScommesseInPrivate:
    async def test_private_still_shows_the_list(self, session, user_factory):
        event = await _open_event(session, user_factory, title="Derby")
        message = _FakeMessage("private")

        await cmd_scommesse(message, session, _state())

        text, markup = message.replies[-1]
        assert "scommess" in text
        cbs = [b.callback_data for b in _buttons(markup) if b.callback_data]
        assert BetEventCb(action="view", event_id=event.id).pack() in cbs

    async def test_private_marks_events_the_user_bet_on(self, session, user_factory):
        event = await _open_event(session, user_factory)
        await user_factory(USER_ID, "bettor", coins=1000)
        detail = await bet_service.get_event_detail(session, event.id)
        opts = detail.options
        await bet_service.place_bet(session, USER_ID, event.id, opts[0].id, 100)
        await session.commit()

        message = _FakeMessage("private")
        await cmd_scommesse(message, session, _state())

        labels = " ".join(b.text for b in _buttons(message.replies[-1][1]))
        assert "✅" in labels
