"""Router registration — the single place the order is declared.

aiogram tries routers in registration order and stops at the first handler that
matches, so this list is **behaviour**, not bookkeeping. It used to live inline in
`main.py` under a comment saying «admin_betting MUST precede betting», with nothing
enforcing it; `tests/unit/test_router_order.py` now does.

Two constraints are load-bearing, and both are asserted there:

* **`admin_betting` before `betting`** — `admin_betting` ends with a catch-all that
  denies unmatched `admin_bet:*` callbacks. The other way round, `betting` would
  answer admin callbacks first and that deny would be unreachable.
* **`common` last** — it holds the fallbacks (unknown command, loose text). Earlier
  in the list it would answer before the specific handlers get their turn.

The rest of the order is not arbitrary either — specific prefixes before general
ones — but only those two are invariants worth failing a build over.

`errors.py` is intentionally absent: it registers on `dp.errors` (see `main.py`),
not as a router, so it has no `router` attribute for the discovery test to find.
"""

from __future__ import annotations

from aiogram import Dispatcher, Router

# event_types first: `events` and `schedule` both do `from handlers import
# event_types` at import time, and importing the subpackage here means that name is
# already bound on this module when they run.
from handlers import event_types  # noqa: F401
from handlers import (
    admin,
    admin_betting,
    admin_dashboard,
    backup,
    badges,
    betting,
    common,
    economy,
    events,
    fun_ai,
    group_events,
    guess,
    inline_mode,
    leaderboard,
    onboarding,
    quiz,
    schedule,
    shop,
    twenty_questions,
)

#: Every router, in the order the dispatcher must try them. See the module docstring.
ROUTERS: tuple[Router, ...] = (
    group_events.router,
    onboarding.router,
    economy.router,
    admin_betting.router,   # must precede betting (catch-all deny for admin_bet:*)
    betting.router,
    badges.router,
    leaderboard.router,
    shop.router,
    admin.router,
    admin_dashboard.router,
    events.router,
    quiz.router,
    guess.router,          # next to its twin; the callback prefixes are disjoint
    twenty_questions.router,
    schedule.router,
    backup.router,
    fun_ai.router,
    inline_mode.router,
    common.router,          # must stay last (global fallbacks)
)


def register(dp: Dispatcher) -> None:
    """Attach every router to `dp`, in order. Nothing else — no filtering, no
    conditional registration: what the list says is what the bot does."""
    for router in ROUTERS:
        dp.include_router(router)
