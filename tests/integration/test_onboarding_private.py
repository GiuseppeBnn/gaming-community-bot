"""Onboarding acceptance is strictly PRIVATE.

The rules-acceptance card must never live in the group: there it is a single
shared message that any bystander could tap, completing THEIR onboarding (and
grabbing the welcome `first_steps` trophy) in place of the user who ran /start.

Covers both defense layers:
  * cmd_start (common): in a group a non-onboarded, non-admin user gets only a
    deep-link to private — never the rules card; in private they get the card.
  * cb_accept_rules (onboarding): the callback is honored only in a private
    chat; from a group it just alerts and changes nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType
from sqlalchemy import select

import handlers.common as common
import handlers.onboarding as onboarding
from database.models import User, UserBadge
from services import badge_service, group_registry


class _Sent:
    message_id = 1


class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(username="ArenaBot")

    async def send_message(self, *args, **kwargs):
        return _Sent()


class _FakeMsg:
    def __init__(self, uid: int, chat_type: ChatType):
        self.from_user = SimpleNamespace(id=uid, username="mario", first_name="Mario")
        self.bot = _FakeBot()
        self.chat = SimpleNamespace(
            id=-100 if chat_type != ChatType.PRIVATE else uid, type=chat_type
        )
        self.answers: list[tuple[str, dict]] = []
        self.replies: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return _Sent()

    async def reply(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return _Sent()


class _FakeCbMessage:
    def __init__(self, chat_type: ChatType):
        self.chat = SimpleNamespace(type=chat_type, id=-100)
        self.edits: list[tuple[str, dict]] = []

    async def edit_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))
        return _Sent()


class _FakeCallback:
    def __init__(self, uid: int, chat_type: ChatType):
        self.from_user = SimpleNamespace(id=uid, first_name="Mario")
        self.message = _FakeCbMessage(chat_type)
        self.bot = _FakeBot()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))


@pytest.fixture(autouse=True)
def _isolate():
    # group_id 0 → trophy announcement is a no-op (no Telegram calls in tests).
    group_registry.set_runtime_group_id(0)
    yield
    group_registry.set_runtime_group_id(None)


async def _async_false(*args, **kwargs):
    return False


# ---------------------------------------------------------------------------
# Layer 2 — cb_accept_rules
# ---------------------------------------------------------------------------

async def test_accept_in_group_is_rejected(seeded_session, user_factory):
    await user_factory(tg_id=321, onboarding_completed=False)
    cb = _FakeCallback(uid=321, chat_type=ChatType.GROUP)

    await onboarding.cb_accept_rules(cb, seeded_session)

    # Alerted, and nothing changed: onboarding still pending, no trophy.
    assert cb.answers and cb.answers[0][1] is True  # show_alert
    assert cb.message.edits == []
    user = (
        await seeded_session.execute(select(User).where(User.tg_id == 321))
    ).scalar_one()
    assert user.onboarding_completed is False
    badges = (
        await seeded_session.execute(
            select(UserBadge).where(UserBadge.user_tg_id == 321)
        )
    ).scalars().all()
    assert badges == []


async def test_accept_in_private_completes_onboarding(seeded_session, user_factory):
    await user_factory(tg_id=321, onboarding_completed=False)
    cb = _FakeCallback(uid=321, chat_type=ChatType.PRIVATE)

    await onboarding.cb_accept_rules(cb, seeded_session)

    user = (
        await seeded_session.execute(select(User).where(User.tg_id == 321))
    ).scalar_one()
    assert user.onboarding_completed is True
    earned = await badge_service.get_user_badges(seeded_session, 321)
    assert any(ub.badge.slug == badge_service.BADGE_FIRST_STEPS for ub in earned)
    assert cb.message.edits, "expected the welcome message to be shown"


# ---------------------------------------------------------------------------
# Layer 1 — cmd_start gate
# ---------------------------------------------------------------------------

async def test_start_in_group_redirects_to_private(session, monkeypatch):
    monkeypatch.setattr(common, "is_bot_admin", _async_false)
    msg = _FakeMsg(uid=321, chat_type=ChatType.GROUP)
    cmd = SimpleNamespace(args="")

    await common.cmd_start(msg, cmd, SimpleNamespace(), session)

    # A deep-link reply (button) — NOT the rules card answered in the group.
    assert msg.replies, "expected a deep-link redirect reply"
    assert msg.answers == [], "the rules card must not be posted in the group"
    assert msg.replies[0][1].get("reply_markup") is not None
    # The numbered rules body (the actual card) is absent from the redirect.
    assert "1️⃣" not in msg.replies[0][0]


async def test_start_in_private_shows_rules(session, monkeypatch):
    monkeypatch.setattr(common, "is_bot_admin", _async_false)
    msg = _FakeMsg(uid=321, chat_type=ChatType.PRIVATE)
    cmd = SimpleNamespace(args="")

    await common.cmd_start(msg, cmd, SimpleNamespace(), session)

    # Rules card answered in private, carrying the accept keyboard.
    assert msg.answers, "expected the rules prompt in private"
    assert "Regole" in msg.answers[0][0]
    assert msg.answers[0][1].get("reply_markup") is not None
