"""Unit tests for the quiz per-question countdown guard (handlers/quiz).

Regression (note.txt): when the LAST question's timer fires, the running task IS
``_PLAY[key].timer``. The finish path calls ``_forget_play`` → ``ctx.timer.cancel()``,
so the timer used to cancel *itself*: a CancelledError (a BaseException, missed by
``except Exception``) aborted the coroutine before it sent "Quiz completato!".
``_cancel_task`` must never cancel the currently-running task.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from handlers.quiz.play import _PLAY, _PlayCtx, _cancel_task, _forget_play, _play_key


async def test_cancel_task_never_cancels_current_task():
    # If the guard were broken, .cancel() on the current task would raise
    # CancelledError at the await below and this test would error out.
    _cancel_task(asyncio.current_task())
    await asyncio.sleep(0)
    assert not asyncio.current_task().cancelled()


async def test_cancel_task_cancels_a_different_task():
    other = asyncio.create_task(asyncio.sleep(100))
    await asyncio.sleep(0)  # let it start running
    _cancel_task(other)
    with pytest.raises(asyncio.CancelledError):
        await other
    assert other.cancelled()


async def test_forget_play_does_not_self_cancel_on_finish():
    # Reproduce the last-question finish: the running task is the stored timer.
    key = _play_key(quiz_id=4242, user_tg_id=99)
    _PLAY[key] = _PlayCtx(
        question_id=1, shown_at=time.monotonic(), message_id=1, chat_id=1,
        timer=asyncio.current_task(),
    )
    try:
        _forget_play(4242, 99)
        await asyncio.sleep(0)  # would raise here if the running task got cancelled
        assert not asyncio.current_task().cancelled()
        assert key not in _PLAY  # entry still cleared
    finally:
        _PLAY.pop(key, None)
