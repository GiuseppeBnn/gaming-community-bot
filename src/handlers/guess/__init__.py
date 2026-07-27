"""Guess games — Guess The Game (image) and Sound Quest (audio).

One engine, two games. They differ only in which medium is stored and which Bot
API method resends it, so the whole difference lives in `_shared.KINDS` and the
registry holds two instances of one spec. Duplicating the game would mean
duplicating a path that pays coins.

Creation: an admin builds a round in private (title → medium → answer → aliases
→ attempts → time → hints → prizes). Play: the group gets an announcement with a
deep-link — never the medium itself, which would move the game into the group
where the answer gets discussed — and each player guesses in their own private
chat. Close: solvers are ranked by fewest attempts, then by time; the podium
reveals the medium and the answer.

**The submodule import order below is the handler registration order** on the
single shared router, the same arrangement as `handlers/quiz`.
"""

from __future__ import annotations

from handlers.guess._shared import router

# Imported for their side effect: each registers its handlers on `router`.
from handlers.guess import creation  # noqa: E402,F401

# The surface other modules use by name.
from handlers.guess.creation import start_guess_creation  # noqa: E402

__all__ = [
    "router",
    "start_guess_creation",
]
