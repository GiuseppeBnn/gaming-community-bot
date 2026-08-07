"""An unhandled callback still gets a response.

`common.router` is last (`handlers/__init__.py`), and before this handler it had
**no** callback handler: a button from a keyboard older than the current deploy
did nothing and left Telegram's spinner running until it timed out.

Two cases land here and must be distinguished: an old button — normal, and the
user deserves a response — and a handler that stopped matching by mistake,
which would otherwise remain silent forever without a log.
"""

from __future__ import annotations

import logging

from handlers import common


class _FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


async def test_unhandled_callback_gets_an_answer():
    callback = _FakeCallback("sched:cancel")

    await common.cb_unhandled(callback)

    assert callback.answers, "without an answer, Telegram's spinner keeps running"
    text, _alert = callback.answers[0]
    assert text == common._UNHANDLED_CALLBACK


async def test_unhandled_callback_is_logged_for_the_admins(caplog):
    callback = _FakeCallback("ev:list:quiz")

    with caplog.at_level(logging.WARNING):
        await common.cb_unhandled(callback)

    record = next(r for r in caplog.records if "Unhandled callback" in r.getMessage())
    assert "ev:list:quiz" in record.getMessage()
    assert record.msg == "Unhandled callback: %s", (
        "the payload must remain an argument: utils.alerts deduplicates on the template, "
        "and an f-string would turn every stale-button click into a new alert"
    )
