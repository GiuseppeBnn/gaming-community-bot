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


class AdminCb(CallbackData, prefix="adm"):
    """Button-driven admin dashboard."""

    action: str
    key: str | None = None
    item_id: int | None = None


class ShopCb(CallbackData, prefix="shop"):
    """Navigation and purchases in La Locanda del Drago."""

    action: str
    key: str | None = None


class AdminBetCb(CallbackData, prefix="admin_bet"):
    """Admin management of betting events."""

    action: str
    event_id: int | None = None
    option_id: int | None = None


class BetCb(CallbackData, prefix="bet"):
    """Navigation and creation-window controls for player betting."""

    #: "cancel_creation" | "cancel_yes" | "cancel_no" | "window"
    #: | "window_custom" | "back" | "close"
    action: str
    seconds: int | None = None


class BetEventCb(CallbackData, prefix="event"):
    """Open one betting event from the private player list."""

    #: "view"
    action: str
    event_id: int


class BetOptionCb(CallbackData, prefix="bet_option"):
    """Pick one option of a betting event."""

    #: "pick"
    action: str
    event_id: int
    option_id: int


class BetAmountCb(CallbackData, prefix="bet_amount"):
    """Choose one preset stake amount."""

    #: "pick"
    action: str
    event_id: int
    option_id: int
    amount: int


class BetCustomCb(CallbackData, prefix="bet_custom"):
    """Open the custom-stake FSM."""

    #: "open"
    action: str
    event_id: int
    option_id: int


class BetConfirmCb(CallbackData, prefix="bet_confirm"):
    """Place a confirmed player bet."""

    #: "place"
    action: str
    event_id: int
    option_id: int
    amount: int


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


class EventCb(CallbackData, prefix="ev"):
    """The Events hub — `handlers/events.py`, plus the buttons that `event_types/`
    and `guess/creation.py` draw for it.

    `action` absorbs two things the old grammar smeared across segments. The
    confirm step glued its verb to the prefix (`ev:askstart`), which is simply an
    action name here. And scheduling used an optional 5th segment to pin *what* to
    schedule (`ev:sched:<t>:<id>:close`); that is an action of its own now —
    "sched_close" — because a field only one action in ten fills would be an empty
    separator in every other payload, and a field whose meaning depends on the
    action is the same dishonesty the hand-rolled parsing allowed itself.
    """

    #: "home" | "list" | "item" | "new"
    #: | "ask{start,del,close,reset}" | "start" | "close" | "del" | "reset"
    #: | "sched" | "sched_close"
    action: str
    task_type: str | None = None
    item_id: int | None = None


class PollCreateCb(CallbackData, prefix="evpt"):
    """Cancelling poll creation — `handlers/events.py`, the `ev:pt:*` triangle.

    It squatted under the `ev` prefix without sharing any of its fields. Given a
    prefix of its own, `EventCb` stays at three fields instead of four and every
    other hub payload loses a separator.
    """

    #: "cancel" | "cancel_yes" | "cancel_no"
    action: str


class QuizNewCb(CallbackData, prefix="quiz_new"):
    """Quiz-creation flow controls — `handlers/quiz/creation.py`."""

    #: "cancel" | "cancel_yes" | "cancel_no" | "back" | "review"
    #: | "quickprize" | "customprize" | "noprize" | "usedefault"
    #: | "time_limit" | "time_limit_custom" | "randomize" | "correct"
    #: | "skip_explanation" | "add" | "remove_last" | "publish"
    action: str
    key: str | None = None
    value: int | None = None


class QuizEditCb(CallbackData, prefix="quiz_edit"):
    """Quiz-question editing controls — `handlers/quiz/editing.py`.

    ``quiz_id`` and ``index`` locate a question for editor navigation and its
    field-edit/redo actions. ``correct`` deliberately carries only ``index``:
    the question being edited is already held by the FSM.
    """

    #: "noop" | "cancel" | "redo_skip_explanation"
    #: | "nav" | "text" | "options" | "explanation" | "redo" | "correct"
    action: str
    quiz_id: int | None = None
    index: int | None = None
