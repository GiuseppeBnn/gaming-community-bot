from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, MessageEntity, User
import pytest
from sqlalchemy import select

from database.models import AdminAction, AIGameSession
from handlers import images
import filters.admin_filter as admin_filter

PROMPT = "Un pinguino vende gelati sulla luna"


@pytest.fixture
def setup(monkeypatch, session):
    monkeypatch.setattr(images.settings, "telephone_enabled", True)
    monkeypatch.setattr(images.settings, "openrouter_api_key", "fake")
    images._inflight.clear()

    async def generate(*args, **kw):
        assert not session.in_transaction()
        return b"fake-png"

    generator = AsyncMock(side_effect=generate)
    monkeypatch.setattr(images.image_service, "generate", generator)
    yield generator
    images._inflight.clear()


class FakeMessage:
    def __init__(self, session, fail=None):
        self.from_user = SimpleNamespace(id=10)
        self.bot = SimpleNamespace()
        self.session, self.fail = session, fail
        self.texts, self.photos = [], []

    async def answer(self, text, **kw):
        assert not self.session.in_transaction()
        if self.fail == "ack":
            self.fail = None
            raise TelegramBadRequest(method=SendMessage(chat_id=10, text="test"), message="failure")
        self.texts.append(text)

    async def answer_photo(self, photo, **kw):
        assert not self.session.in_transaction()
        if self.fail == "upload":
            raise TelegramBadRequest(method=SendMessage(chat_id=10, text="test"), message="failure")
        self.photos.append((photo, kw))


def command(prompt=PROMPT):
    return CommandObject(command="immagine", args=prompt)


async def test_admin_image_reuses_gateway_without_creating_events(session, setup):
    msg = FakeMessage(session)
    await images.cmd_immagine(msg, command(), session)
    setup.assert_awaited_once()
    assert setup.call_args.args == (PROMPT,)
    assert setup.call_args.kwargs["feature"] == "admin_image"
    assert setup.call_args.kwargs["cap_microusd"] == 12000
    assert len(msg.photos) == 1 and msg.photos[0][0].data == b"fake-png"
    assert msg.photos[0][1]["protect_content"]
    assert PROMPT not in str(msg.texts) + str(msg.photos[0][1])
    assert (await session.execute(select(AIGameSession))).scalars().all() == []
    action = (await session.execute(select(AdminAction))).scalar_one()
    assert action.action_type == "image_generate" and action.admin_tg_id == 10
    assert PROMPT not in action.detail and "prompt_chars=" in action.detail
    assert not images._inflight


@pytest.mark.parametrize("prompt", [None, "", "  ", "x" * 151])
async def test_invalid_input_does_not_spend(session, setup, prompt):
    msg = FakeMessage(session)
    await images.cmd_immagine(msg, command(prompt), session)
    setup.assert_not_awaited()
    assert "150" in msg.texts[-1] and not msg.photos


@pytest.mark.parametrize("field,value", [("telephone_enabled", False), ("openrouter_api_key", "")])
async def test_disabled_command_is_explicit(session, setup, monkeypatch, field, value):
    monkeypatch.setattr(images.settings, field, value)
    msg = FakeMessage(session)
    await images.cmd_immagine(msg, command(), session)
    assert "non abilitata" in msg.texts[-1]
    setup.assert_not_awaited()


async def test_same_admin_cannot_start_two_parallel_images(session, setup):
    images._inflight.add(10)
    msg = FakeMessage(session)
    await images.cmd_immagine(msg, command(), session)
    assert "già un'immagine" in msg.texts[-1]
    setup.assert_not_awaited()


@pytest.mark.parametrize(
    "error,expected",
    [
        (images.ai_budget.AIBudgetExceeded("private-error"), "Budget AI"),
        (images.ai_budget.AIBudgetError("private-error"), "Contabilità"),
        (
            images.image_service.ImageError(
                "private-error", stage="generation", kind="http", http_status=503
            ),
            "HTTP 503",
        ),
        (
            images.image_service.ImageError(
                "private-error", stage="generation", kind="timeout", retryable=True
            ),
            "timeout",
        ),
        (RuntimeError("private-error"), "Operazione interrotta"),
    ],
)
async def test_errors_never_leak_bodies_or_auto_retry(session, setup, error, expected, caplog):
    setup.side_effect = error
    msg = FakeMessage(session)
    await images.cmd_immagine(msg, command(), session)
    assert expected in msg.texts[-1]
    assert "private-error" not in str(msg.texts) + caplog.text
    assert not msg.photos and not images._inflight
    assert setup.await_count == 1


@pytest.mark.parametrize(
    "phase,expected,calls",
    [("ack", "generazione non avviata", 0), ("upload", "invio Telegram fallito", 1)],
)
async def test_telegram_failure_never_regenerates(session, setup, phase, expected, calls):
    msg = FakeMessage(session, phase)
    await images.cmd_immagine(msg, command(), session)
    assert expected in msg.texts[-1] and setup.await_count == calls
    assert not images._inflight


async def test_cancel_frees_the_inflight_slot(session, setup):
    setup.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await images.cmd_immagine(FakeMessage(session), command(), session)
    assert not images._inflight


async def test_audit_storage_failure_blocks_generation(session, setup, monkeypatch):
    monkeypatch.setattr(
        images.admin_service, "log_action", AsyncMock(side_effect=RuntimeError("private-error"))
    )
    msg = FakeMessage(session)
    await images.cmd_immagine(msg, command(), session)
    setup.assert_not_awaited()
    assert "Operazione interrotta" in msg.texts[-1] and not images._inflight


@pytest.mark.parametrize("allowed", [True, False])
async def test_admin_gate_is_mounted_on_the_router(session, setup, monkeypatch, allowed):
    monkeypatch.setattr(admin_filter, "is_admin", AsyncMock(return_value=allowed))
    ok, _ = await images.router.message.check_root_filters(FakeMessage(session))
    assert ok is allowed
    setup.assert_not_awaited()


@pytest.mark.parametrize("name", ["/immagine", "/Immagine", "/IMMAGINE", "/Immagine@testbot"])
async def test_requested_case_and_bot_mentions_are_supported(name):
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=10, type="private"),
        from_user=User(id=10, is_bot=False, first_name="Admin"),
        text=f"{name} {PROMPT}",
        entities=[MessageEntity(type="bot_command", offset=0, length=len(name))],
    )
    bot = SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username="testbot")))
    ok, data = await images.router.message.handlers[0].check(msg, bot=bot)
    assert ok and data["command"].args == PROMPT
