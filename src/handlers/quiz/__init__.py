"""Quiz mode — private, answer-driven quizzes with a podium.

Creation: an admin builds a quiz in private via an FSM (title → description →
prizes → per-question time limit → shuffling → a loop of questions).

Play: when launched, the bot posts an announcement in the group with a "Gioca"
button (deep-link). Each participant plays in their OWN private chat — questions
are sent one at a time as inline option buttons, advancing on answer and showing
the explanation, with an optional per-question countdown.

Podium: finishers (answered every question) are ranked by correct answers DESC,
then by how long the player took ASC, with arrival order as the final tie-break.
Closing the quiz pays the prizes and publishes the podium. Answers are private, so
nobody can "answer for everyone".

This was one 1800-line module. It is split by phase — creation, editing, lifecycle,
play, try — around a single shared `router`, and this file is the public surface:
importers keep saying `from handlers.quiz import open_quiz` and nothing outside
knows the difference.

**The submodule import order below is the handler registration order** inside that
one router, and it mirrors the order the sections had in the original file. The
callback prefixes are disjoint (`quiz_new:` / `quiz_edit:` / `quiz_ans:` /
`quiz_try:`), so nothing depends on it today — keeping it identical is what makes
that a fact rather than a hope.
"""

from __future__ import annotations

from handlers.quiz._shared import router

# Imported for their side effect: each module registers its handlers on `router`.
from handlers.quiz import creation, editing, lifecycle, play, trying  # noqa: E402,F401

# The surface other modules actually use. `handlers.common` and
# `handlers.event_types.quiz_type` import these by name.
from handlers.quiz.creation import start_quiz_creation  # noqa: E402
from handlers.quiz.lifecycle import close_quiz, open_quiz  # noqa: E402
from handlers.quiz.play import start_quiz_session  # noqa: E402
from handlers.quiz.trying import start_quiz_try  # noqa: E402

__all__ = [
    "close_quiz",
    "open_quiz",
    "router",
    "start_quiz_creation",
    "start_quiz_session",
    "start_quiz_try",
]
