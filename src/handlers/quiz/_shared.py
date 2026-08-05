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
# The options are inline buttons in private play, and a full-width button shows
# only a short single line before Telegram truncates it. 30 is what fits without
# the answer being visually cut — and `_question_kb` slices to exactly this, so
# the validated cap and the displayed cap can no longer diverge (they did: the
# button hard-cut at 40 while this allowed 100, so answers 31–100 were cut).
_MAX_OPTION = 30
_MAX_EXPLANATION = 200

# Quiz management (list/start/close) is admin-only AND private-only: in a group it
# redirects to the private dashboard. Creation already redirects via /crea_quiz.
_QUIZ_PRIVATE_NOTICE = "🧠 Gestisci i quiz in chat privata col bot."


def _too_long(text: str, cap: int, subject: str) -> str | None:
    """Error message if `text` exceeds `cap`, else None. `subject` is the already
    gender-agreed Italian phrase (e.g. "Il titolo è troppo lungo"). Reports the
    actual length and how much to cut, so the admin doesn't have to count."""
    if len(text) <= cap:
        return None
    return (
        f"⚠️ {subject}: <b>{len(text)}/{cap}</b> caratteri.\n"
        f"Accorcia di {len(text) - cap} e reinvia."
    )


def _options_error(options: list[str]) -> str | None:
    """Validate a parsed option list (count + per-option length). Shared by the
    creation and the edit flow so both enforce exactly the same rules."""
    if not (_MIN_OPTIONS <= len(options) <= _MAX_OPTIONS):
        return f"⚠️ Servono da {_MIN_OPTIONS} a {_MAX_OPTIONS} opzioni (una per riga)."
    long_ones = [(i, o) for i, o in enumerate(options, start=1) if len(o) > _MAX_OPTION]
    if long_ones:
        detail = "\n".join(f"• opzione {i}: {len(o)}/{_MAX_OPTION}" for i, o in long_ones)
        return (
            f"⚠️ Ogni opzione può essere al massimo <b>{_MAX_OPTION}</b> caratteri.\n{detail}\n"
            "Accorcia e reinvia tutte le opzioni."
        )
    return None
