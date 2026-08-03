"""Typed callback payloads, in one place.

Every screen used to invent its own grammar — `guess_new:edit:<field>`,
`ev:sched:<type>:<id>:close`, `quiz_edit:nav:<id>:<i>` — and every handler
re-parsed it by hand. `CallbackData` is aiogram's own answer to that, and it was
never used here: the filter unpacks the payload and injects it as `callback_data`,
so the parsing, the `isdigit()` guards and the 64-byte discipline all move from
the handler to the type.

They live in a module of their own, not next to their handlers, because the same
payloads are built elsewhere: `event_types/` and `guess/creation.py` render the
Events hub buttons. Keeping the classes in `handlers/events.py` would make those
modules import a handler.

**Optional fields still cost their separator.** `SchedCb(action="cancel")` packs
to `"sched:cancel::"`, not `"sched:cancel"` — which is exactly why a button drawn
by an older deploy no longer matches, and falls through to the catch-all in
`handlers/common.py`.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class SchedCb(CallbackData, prefix="sched"):
    """The scheduling flow — `handlers/schedule.py`.

    `key` carries the one string the action refers to: an event-type key for
    "type" and "pick", a schedulable-action key ("start" / "close") for "act".

    `item_id` is two different primary keys depending on `action`: for "pick"
    it's the id of the item being scheduled (ends up in `ScheduledTask.ref_id`);
    for "del" it's the id of the `ScheduledTask` row itself. Both are small
    ints, so copying the wrong one in — e.g. `SchedCb(action="del", item_id=quiz.id)`
    — cancels an unrelated task and nothing complains.
    """

    #: "cancel" | "cancel_yes" | "cancel_no" | "act" | "type" | "pick" | "del"
    action: str
    key: str | None = None
    item_id: int | None = None
