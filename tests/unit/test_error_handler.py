"""Unit tests for the global dp.errors fallback (handlers/errors)."""

from __future__ import annotations

import types

import pytest
from aiogram.exceptions import TelegramBadRequest

from handlers import errors


def _message(chat_id: int = -100, text: str | None = "/daily"):
    sent: list[str] = []

    async def answer(body, **kwargs):
        sent.append(body)

    return types.SimpleNamespace(
        chat=types.SimpleNamespace(id=chat_id),
        text=text,
        answer=answer,
        _sent=sent,
    )


def _callback(data: str = "quiz:start:7", message=None):
    answered: list[dict] = []

    async def answer(body=None, **kwargs):
        answered.append({"text": body, **kwargs})

    return types.SimpleNamespace(
        data=data,
        message=message,
        answer=answer,
        _answered=answered,
    )


def _event(exc: Exception, *, message=None, callback=None, user_id: int | None = 42):
    update = types.SimpleNamespace(
        message=message,
        callback_query=callback,
        event_from_user=(
            types.SimpleNamespace(id=user_id, username="tester")
            if user_id is not None
            else None
        ),
    )
    return types.SimpleNamespace(update=update, exception=exc)


# ---------------------------------------------------------------------------
# real bugs → logged loudly + the user gets told
# ---------------------------------------------------------------------------

async def test_message_error_replies_to_user():
    msg = _message()
    handled = await errors.on_error(_event(ValueError("boom"), message=msg))
    assert handled is True
    assert msg._sent == [errors._USER_MESSAGE]


async def test_callback_error_uses_alert():
    cb = _callback()
    handled = await errors.on_error(_event(KeyError("boom"), callback=cb))
    assert handled is True
    assert cb._answered[0]["text"] == errors._USER_MESSAGE
    assert cb._answered[0]["show_alert"] is True


async def test_logs_user_and_callback_context(caplog):
    cb = _callback(data="bet:win:999")
    with caplog.at_level("ERROR"):
        await errors.on_error(_event(RuntimeError("boom"), callback=cb))
    record = "\n".join(r.getMessage() for r in caplog.records)
    # The whole point of the handler: the log line must be actionable on its own.
    assert "user_id=42" in record
    assert "bet:win:999" in record


async def test_survives_a_user_that_cannot_be_messaged():
    """The notify path itself failing must not replace a logged bug with a crash."""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("bot blocked by user")

    msg = _message()
    msg.answer = boom
    assert await errors.on_error(_event(ValueError("original"), message=msg)) is True


async def test_missing_user_and_message_does_not_crash():
    assert await errors.on_error(_event(ValueError("boom"), user_id=None)) is True


# ---------------------------------------------------------------------------
# benign Telegram rejections → silent, but the spinner still stops
# ---------------------------------------------------------------------------

def _bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=types.SimpleNamespace(), message=text)


@pytest.mark.parametrize(
    "text",
    [
        "Bad Request: message is not modified",
        "Bad Request: query is too old and response timeout expired",
        "Bad Request: message to edit not found",
    ],
)
async def test_benign_rejections_do_not_alert_the_user(text):
    cb = _callback()
    handled = await errors.on_error(_event(_bad_request(text), callback=cb))
    assert handled is True
    # answered to clear the loading spinner, but with no scary text
    assert cb._answered == [{"text": None}]


async def test_unexpected_bad_request_is_not_treated_as_benign():
    cb = _callback()
    await errors.on_error(_event(_bad_request("Bad Request: chat not found"), callback=cb))
    assert cb._answered[0]["text"] == errors._USER_MESSAGE
