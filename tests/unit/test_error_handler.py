"""Exercise the dp.errors fallback with real aiogram Update envelopes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    Chat,
    ErrorEvent,
    InaccessibleMessage,
    InlineQuery,
    Message,
    PollAnswer,
    Update,
    User,
)

from handlers import errors
from handlers.callbacks import BetCb


def _user(user_id: int = 42) -> User:
    return User(id=user_id, is_bot=False, first_name="Tester", username="tester")


def _message(*, user=True, text: str | None = "/daily") -> Message:
    return Message(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=Chat(id=-100, type="supergroup"),
        from_user=_user() if user else None,
        text=text,
    )


def _callback(data: str = "quiz:start:7", message=None) -> CallbackQuery:
    return CallbackQuery(id="callback-7", from_user=_user(), chat_instance="test",
                         data=data, message=message)


def _event(exc: Exception, **payload) -> ErrorEvent:
    return ErrorEvent(update=Update(update_id=123, **payload), exception=exc)


@pytest.fixture(autouse=True)
def replies(monkeypatch):
    """No Telegram calls; keep Update/Message/CallbackQuery validation real."""
    message_reply = AsyncMock()
    callback_reply = AsyncMock()
    monkeypatch.setattr(Message, "answer", message_reply)
    monkeypatch.setattr(CallbackQuery, "answer", callback_reply)
    return message_reply, callback_reply


async def test_message_error_replies_to_user(replies, caplog):
    handled = await errors.on_error(_event(ValueError("boom"), message=_message()))
    assert handled is True
    replies[0].assert_awaited_once_with(errors._USER_MESSAGE)
    replies[1].assert_not_awaited()
    assert "user_id=42" in caplog.text
    assert "chat_id=-100" in caplog.text


async def test_callback_error_uses_alert(replies):
    handled = await errors.on_error(_event(KeyError("boom"), callback_query=_callback()))
    assert handled is True
    replies[1].assert_awaited_once_with(errors._USER_MESSAGE, show_alert=True)
    replies[0].assert_not_awaited()


async def test_logs_user_and_callback_context(caplog):
    data = BetCb(action="window", seconds=999).pack()
    # The sender is the clicking human, not the author of the callback's message.
    bot_message = _message().model_copy(update={
        "from_user": User(id=99, is_bot=True, first_name="Bot"),
    })
    cb = _callback(data=data, message=bot_message)
    with caplog.at_level("ERROR"):
        await errors.on_error(_event(RuntimeError("boom"), callback_query=cb))
    assert "user_id=42" in caplog.text
    assert "user_id=99" not in caplog.text
    assert "chat_id=-100" in caplog.text
    assert data in caplog.text


async def test_survives_a_user_that_cannot_be_messaged(replies, caplog):
    replies[0].side_effect = RuntimeError("bot blocked by user")
    original = ValueError("original")
    assert await errors.on_error(_event(original, message=_message())) is True
    assert "Could not deliver" in caplog.text
    assert any(r.exc_info and r.exc_info[1] is original for r in caplog.records)


async def test_missing_user_and_message_does_not_crash(replies, caplog):
    assert await errors.on_error(_event(ValueError("boom"))) is True
    assert "user_id=None" in caplog.text
    replies[0].assert_not_awaited()
    replies[1].assert_not_awaited()


async def test_message_without_user_or_text(replies, caplog):
    assert await errors.on_error(
        _event(ValueError("boom"), message=_message(user=False, text=None)),
    ) is True
    assert "user_id=None" in caplog.text
    assert "text=None" in caplog.text
    replies[0].assert_awaited_once_with(errors._USER_MESSAGE)


async def test_poll_vote_error_has_user_context_without_sending_a_message(replies, caplog):
    vote = PollAnswer(poll_id="poll-7", user=_user(), option_ids=[0],
                      option_persistent_ids=["option-0"])
    assert await errors.on_error(_event(ValueError("boom"), poll_answer=vote)) is True
    assert "user_id=42" in caplog.text
    replies[0].assert_not_awaited()
    replies[1].assert_not_awaited()


async def test_inline_error_does_not_invent_a_chat(replies, caplog):
    query = InlineQuery(id="inline-7", from_user=_user(), query="", offset="")
    assert await errors.on_error(_event(ValueError("boom"), inline_query=query)) is True
    assert "user_id=42" in caplog.text
    assert "chat_id=None" in caplog.text
    replies[0].assert_not_awaited()
    replies[1].assert_not_awaited()


async def test_inaccessible_callback_message_is_safe(replies, caplog):
    message = InaccessibleMessage(message_id=7, chat=Chat(id=-100, type="supergroup"),
                                  date=0)
    assert await errors.on_error(
        _event(ValueError("boom"), callback_query=_callback(message=message)),
    ) is True
    assert "chat_id=-100" in caplog.text
    replies[1].assert_awaited_once_with(errors._USER_MESSAGE, show_alert=True)


def _bad_request(text: str) -> TelegramBadRequest:
    from aiogram.methods import AnswerCallbackQuery

    return TelegramBadRequest(method=AnswerCallbackQuery(callback_query_id="callback-7"),
                              message=text)


@pytest.mark.parametrize("text", [
    "Bad Request: message is not modified",
    "Bad Request: query is too old and response timeout expired",
    "Bad Request: message to edit not found",
    "Bad Request: message can't be deleted",
])
async def test_benign_rejections_do_not_alert_the_user(text, replies, caplog):
    handled = await errors.on_error(_event(_bad_request(text), callback_query=_callback()))
    assert handled is True
    replies[1].assert_awaited_once_with()
    replies[0].assert_not_awaited()
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_benign_message_error_does_not_reply(replies):
    assert await errors.on_error(
        _event(_bad_request("Bad Request: message is not modified"), message=_message()),
    ) is True
    replies[0].assert_not_awaited()
    replies[1].assert_not_awaited()


async def test_benign_expired_callback_answer_failure_is_ignored(replies, caplog):
    replies[1].side_effect = _bad_request("Bad Request: query is too old")
    assert await errors.on_error(
        _event(_bad_request("Bad Request: query is too old"), callback_query=_callback()),
    ) is True
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_unexpected_bad_request_is_not_treated_as_benign(replies):
    await errors.on_error(_event(_bad_request("Bad Request: chat not found"),
                                 callback_query=_callback()))
    replies[1].assert_awaited_once_with(errors._USER_MESSAGE, show_alert=True)


@pytest.mark.parametrize("kind", ["message", "callback_query", "poll_answer"])
async def test_dispatcher_keeps_original_exception_and_continues(kind, replies, caplog):
    """Reproduce production routing, not just a direct call with a fake Update."""
    dp = Dispatcher()
    original = ValueError("original handler failure")

    async def broken_handler(event):
        raise original

    dp.observers[kind].register(broken_handler)
    dp.errors.register(errors.on_error)
    payload = {
        "message": _message(),
        "callback_query": _callback(message=_message()),
        "poll_answer": PollAnswer(poll_id="poll-7", user=_user(), option_ids=[0],
                                  option_persistent_ids=["option-0"]),
    }
    async with Bot(token="1234567890:AAFtesttoken_ci") as bot:
        try:
            for update_id in (123, 124):
                assert await dp.feed_update(
                    bot, Update(update_id=update_id, **{kind: payload[kind]}),
                ) is True
        finally:
            await dp.storage.close()
    records = [r for r in caplog.records if r.name == errors.__name__]
    assert len(records) == 2
    assert all(r.exc_info[1] is original for r in records)
    assert all("user_id=42" in r.getMessage() for r in records)
    if kind == "message":
        assert replies[0].await_count == 2
    elif kind == "callback_query":
        assert replies[1].await_count == 2
    else:
        replies[0].assert_not_awaited()
        replies[1].assert_not_awaited()
