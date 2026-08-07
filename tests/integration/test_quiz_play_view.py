"""/quiz is user-facing for non-admins (Punto 6): a non-admin gets a 'play'
button for a running quiz, or a clear 'no quiz active' message — never silence
(the old admin-only filter just dropped their /quiz). Admins still get the
management branch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType

import filters.admin_filter as af
from handlers.quiz import lifecycle as quiz
from database.models import Quiz
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


def _urls(markup):
    return [b.url for row in markup.inline_keyboard for b in row if b.url]


async def test_non_admin_no_active_quiz_gets_clear_message(session):
    bot = _FakeBot()
    msg = _FakeMsg(uid=5, bot=bot)
    await quiz.cmd_quiz_list(msg, session)

    assert len(msg.answers) == 1
    text, _ = msg.answers[0]
    assert "Nessun quiz attivo" in text
    assert msg.replies == []  # not the admin management reply


async def test_non_admin_running_quiz_gets_play_button(session):
    session.add(Quiz(title="Storia", description="", creator_tg_id=1, status="running"))
    # A 'ready' (not started) quiz must NOT count as active for a player.
    session.add(Quiz(title="Bozza", description="", creator_tg_id=1, status="ready"))
    await session.commit()

    bot = _FakeBot()
    msg = _FakeMsg(uid=6, bot=bot)
    await quiz.cmd_quiz_list(msg, session)

    text, kwargs = msg.answers[0]
    assert "Quiz in corso" in text
    urls = _urls(kwargs["reply_markup"])
    assert any("start=quiz_" in u for u in urls)
    # Only the running quiz is offered, not the ready one.
    assert len(urls) == 1


async def test_admin_gets_management_branch_not_play_view(session, monkeypatch):
    monkeypatch.setattr(af.settings, "admin_ids", [7])
    bot = _FakeBot()
    msg = _FakeMsg(uid=7, bot=bot)
    await quiz.cmd_quiz_list(msg, session)

    # Admin path: the events-hub quiz management list (tap → detail, never a
    # one-tap launch), not the player view. `_FakeMsg` has no `edit_text`, so
    # `edit_or_send` falls back to `answer`.
    assert msg.answers, "admin should get the management list"
    text, kwargs = msg.answers[-1]
    assert "Quiz" in text and "Creane uno" in text  # empty management list
    cbs = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "ev:new:quiz" in cbs  # events-hub create button → management branch
    assert all("Nessun quiz attivo" not in a[0] for a in msg.answers)
