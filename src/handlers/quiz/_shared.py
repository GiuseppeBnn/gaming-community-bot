"""Pieces every part of the quiz flow needs.

The `router` lives here so the four sections can register on the same one: the
package re-exports it, so `handlers/__init__.py` still sees a single quiz router and
the registration order is the import order in `__init__.py`.

The caps are the single source of truth for every free-text field. The prompts, the
validation and the edit flow all read them, so the number an admin is told can never
drift from the number actually enforced.
"""

from __future__ import annotations

import logging

from aiogram import Router

from services.poll_service import question_length
from utils.text import esc

log = logging.getLogger(__name__)
router = Router()

_MIN_OPTIONS, _MAX_OPTIONS = 2, 10

# Length caps for every free-text field of a quiz. Single source of truth: the
# prompts, the validation and the edit flow all read these, so the number an admin
# is told can never drift from the number actually enforced. Inputs over the cap
# are REJECTED with the real count, never silently truncated — a truncated
# question only shows up once the quiz is already live.
_MAX_TITLE = 256
_MAX_DESC = 1024
_MAX_QUESTION = 300
# Native poll options wrap in Telegram's dedicated UI (not single-line buttons).
_MAX_OPTION = 100
_MAX_EXPLANATION = 200

# Quiz management (list/start/close) is admin-only AND private-only: in a group it
# redirects to the private dashboard. Creation already redirects via /crea_quiz.
_QUIZ_PRIVATE_NOTICE = "🧠 Gestisci i quiz in chat privata col bot."


def _too_long(text: str, cap: int, subject: str) -> str | None:
    """Native polls count UTF-16 units, including emoji, not Python code points."""
    length = question_length(text)
    if length <= cap:
        return None
    return (
        f"⚠️ {subject}: <b>{length}/{cap}</b> caratteri.\n"
        f"Accorcia di {length - cap} e reinvia."
    )


def _explanation_error(text: str) -> str | None:
    return _too_long(text, _MAX_EXPLANATION, "La spiegazione è troppo lunga") or (
        "⚠️ La spiegazione può contenere al massimo due a capo." if text.count("\n") > 2 else None
    )


def _correct_prompt(options: list[str]) -> str:
    """Admin selectors remain buttons: show full options above their short labels."""
    return "Quale opzione è quella <b>corretta</b>?\n\n" + "\n".join(
        f"<b>{index}.</b> {esc(text)}" for index, text in enumerate(options, start=1)
    )


def _options_error(options: list[str]) -> str | None:
    """Validate a parsed option list (count + per-option length). Shared by the
    creation and the edit flow so both enforce exactly the same rules."""
    if not (_MIN_OPTIONS <= len(options) <= _MAX_OPTIONS):
        return f"⚠️ Servono da {_MIN_OPTIONS} a {_MAX_OPTIONS} opzioni (una per riga)."
    long_ones = [(i, o) for i, o in enumerate(options, start=1) if question_length(o) > _MAX_OPTION]
    if long_ones:
        detail = "\n".join(f"• opzione {i}: {question_length(o)}/{_MAX_OPTION}" for i, o in long_ones)
        return (
            f"⚠️ Ogni opzione può essere al massimo <b>{_MAX_OPTION}</b> caratteri.\n{detail}\n"
            "Accorcia e reinvia tutte le opzioni."
        )
    return None
