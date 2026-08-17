"""Event-type registry package — see :mod:`handlers.event_types.base`.

Public surface: ``register``, ``get``, ``all_types``, ``clear``, ``edit_or_send``,
``StartResult``, ``EventType`` and :func:`register_builtin` (called once at
startup from ``main``). Adding a new event type = a new spec module + one line in
``register_builtin`` — nothing else in the hub/scheduler changes.
"""

from __future__ import annotations

from .base import (
    EventType,
    StartResult,
    all_types,
    clear,
    edit_or_send,
    get,
    register,
)


def register_builtin() -> None:
    """Register the built-in event types in hub order.

    Idempotent: ``register`` replaces by key, so calling it again is harmless.

    The two guess games are the same spec twice, parametrised by ``kind`` — they
    are one engine with different labels, so a class each would be a copy of a
    path that pays coins.
    """
    from .bet_type import BetType
    from .guess_type import GuessType
    from .poll_type import PollType
    from .quiz_type import QuizType
    from .twenty_questions_type import TwentyQuestionsType

    register(QuizType())
    register(GuessType(kind="guess"))
    register(GuessType(kind="sound"))
    register(PollType())
    register(BetType())
    register(TwentyQuestionsType())


__all__ = [
    "EventType",
    "StartResult",
    "all_types",
    "clear",
    "edit_or_send",
    "get",
    "register",
    "register_builtin",
]
