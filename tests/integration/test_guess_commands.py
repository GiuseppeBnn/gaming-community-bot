"""/guessTheGame and /soundQuest are user-facing for non-admins (like /quiz): a
player gets a 'play' button for a running round, or a clear 'none active' message
— never silence. Admins get the events-hub management list instead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType

import filters.admin_filter as af
from database.models import GuessRound
from handlers.callbacks import EventCb
from handlers.guess import lifecycle as guess
from services import group_registry
from utils import cooldown, static_reply


class _Sent:
    message_id = 1


class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(username="testbot")


class _FakeMsg:
    def __init__(self, uid: int, bot: _FakeBot):
        self.from_user = SimpleNamespace(id=uid)
        self.bot = bot
        self.chat = SimpleNamespace(id=uid, type=ChatType.PRIVATE)
        self.answers: list[tuple[str, dict]] = []
        self.replies: list[str] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return _Sent()

    async def reply(self, text: str, **kwargs):
        self.replies.append(text)
        return _Sent()


@pytest.fixture(autouse=True)
def _isolate():
    group_registry.set_runtime_group_id(None)  # → is_admin False for non-listed users
    cooldown.reset()
    static_reply.reset()
    af._cache.clear()
    yield
    group_registry.set_runtime_group_id(None)
    cooldown.reset()
    static_reply.reset()


def _round(*, kind: str, status: str, title: str) -> GuessRound:
    return GuessRound(
        kind=kind, title=title, creator_tg_id=1, answer="x",
        media_file_id="f", media_kind="photo" if kind == "guess" else "audio",
        status=status,
    )


def _urls(markup):
    return [b.url for row in markup.inline_keyboard for b in row if b.url]


async def test_non_admin_no_active_round_gets_clear_message(session):
    msg = _FakeMsg(uid=5, bot=_FakeBot())
    await guess.cmd_guess_the_game(msg, session)

    assert len(msg.answers) == 1
    text, _ = msg.answers[0]
    assert "Nessun" in text and "attivo" in text


async def test_non_admin_running_round_gets_play_button(session):
    session.add(_round(kind="guess", status="running", title="Screenshot"))
    session.add(_round(kind="guess", status="ready", title="Bozza"))  # not active
    await session.commit()

    msg = _FakeMsg(uid=6, bot=_FakeBot())
    await guess.cmd_guess_the_game(msg, session)

    text, kwargs = msg.answers[0]
    assert "in corso" in text
    urls = _urls(kwargs["reply_markup"])
    assert any("start=guess_" in u for u in urls) and len(urls) == 1


async def test_sound_quest_uses_its_own_kind(session):
    session.add(_round(kind="sound", status="running", title="Jingle"))
    await session.commit()

    msg = _FakeMsg(uid=8, bot=_FakeBot())
    await guess.cmd_sound_quest(msg, session)

    _text, kwargs = msg.answers[0]
    assert any("start=sound_" in u for u in _urls(kwargs["reply_markup"]))


async def test_admin_gets_the_management_list(session, monkeypatch):
    monkeypatch.setattr(af.settings, "admin_ids", [7])
    msg = _FakeMsg(uid=7, bot=_FakeBot())
    await guess.cmd_guess_the_game(msg, session)

    # Admin in private: the events-hub round list (empty here). `_FakeMsg` has no
    # `edit_text`, so `edit_or_send` falls back to `answer`.
    assert msg.answers
    _text, kwargs = msg.answers[-1]
    cbs = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert EventCb(action="new", task_type="guess").pack() in cbs


async def test_admin_in_group_is_redirected_to_private(session, monkeypatch):
    monkeypatch.setattr(af.settings, "admin_ids", [7])
    msg = _FakeMsg(uid=7, bot=_FakeBot())
    msg.chat = SimpleNamespace(id=-1001, type=ChatType.GROUP)
    await guess.cmd_guess_the_game(msg, session)

    # Not the management list in the group — a deep-link button to private.
    assert msg.replies and not msg.answers
    assert msg.replies[0]  # the redirect notice


async def test_the_cooldown_swallows_a_rapid_second_call(session):
    msg = _FakeMsg(uid=5, bot=_FakeBot())
    await guess.cmd_guess_the_game(msg, session)  # first: answers once
    await guess.cmd_guess_the_game(msg, session)  # within cooldown: no new answer
    assert len(msg.answers) == 1
