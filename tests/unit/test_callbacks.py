"""The callback payloads, typed.

Before this module every screen invented its own grammar and re-parsed it by
hand: `_, _, task_type, raw = callback.data.split(":")`, repeated, with
scattered `isdigit()` guards and the 64-byte limit respected by eye.

This pins the three things hand-parsing never guaranteed: that the payload
produced is the one expected, that a malformed payload **never reaches** the
handler, and that Telegram's limits show up as errors in tests instead of as
broken buttons in chat.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from aiogram import F
from aiogram.filters.callback_data import CallbackData, CallbackQueryFilter
from aiogram.types import CallbackQuery, User

import handlers
from handlers.callbacks import (
    AdminBetCb,
    AdminCb,
    BetAmountCb,
    BetCb,
    BetConfirmCb,
    BetCustomCb,
    BetEventCb,
    BetOptionCb,
    EventCb,
    GuessAliasCb,
    GuessNewCb,
    GuessPlayCb,
    LeaderboardCb,
    PollCreateCb,
    QuizAnswerCb,
    QuizEditCb,
    QuizNewCb,
    QuizTryCb,
    SchedCb,
    ShopCb,
)
from handlers.events import _CONFIRM


def _query(data: str) -> CallbackQuery:
    """A real CallbackQuery: the filter does an `isinstance` check, a fake one
    would return `False` for the wrong reason."""
    return CallbackQuery(
        id="1",
        from_user=User(id=1, is_bot=False, first_name="A"),
        chat_instance="x",
        data=data,
    )


@pytest.mark.parametrize(
    "cb, expected",
    [
        (SchedCb(action="cancel"), "sched:cancel::"),
        (SchedCb(action="type", key="quiz"), "sched:type:quiz:"),
        (SchedCb(action="pick", key="quiz", item_id=7), "sched:pick:quiz:7"),
        (SchedCb(action="del", item_id=7), "sched:del::7"),
    ],
)
def test_pack(cb, expected):
    assert cb.pack() == expected


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (ShopCb(action="home"), "shop:home:"),
        (ShopCb(action="exec", key="tag_dragon"), "shop:exec:tag_dragon"),
    ],
)
def test_pack_shop_callbacks(cb, packed):
    assert cb.pack() == packed


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (LeaderboardCb(action="show", board="coins"), "lead:show:coins"),
        (LeaderboardCb(action="close"), "lead:close:"),
    ],
)
def test_pack_leaderboard_callbacks(cb, packed):
    assert cb.pack() == packed


async def test_leaderboard_callback_is_unpacked_by_its_filter():
    assert await LeaderboardCb.filter()(_query("lead:show:coins")) == {
        "callback_data": LeaderboardCb(action="show", board="coins")
    }


def test_leaderboard_callback_values_cannot_contain_the_separator():
    with pytest.raises(ValueError, match="Separator symbol"):
        LeaderboardCb(action="show", board="coins:xp").pack()


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (QuizNewCb(action="cancel"), "quiz_new:cancel::"),
        (QuizNewCb(action="time_limit", value=60), "quiz_new:time_limit::60"),
        (QuizNewCb(action="randomize", key="both"), "quiz_new:randomize:both:"),
        (QuizNewCb(action="correct", value=2), "quiz_new:correct::2"),
    ],
)
def test_pack_quiz_creation_callbacks(cb, packed):
    assert cb.pack() == packed


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (GuessNewCb(action="cancel"), "guess_new:cancel::"),
        (GuessNewCb(action="edit", key="title"), "guess_new:edit:title:"),
        (GuessNewCb(action="hint_at", value=3), "guess_new:hint_at::3"),
    ],
)
def test_pack_guess_creation_callbacks(cb, packed):
    assert cb.pack() == packed


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (GuessAliasCb(action="add", round_id=7), "guess_alias:add:7"),
        (GuessAliasCb(action="cancel"), "guess_alias:cancel:"),
    ],
)
def test_pack_guess_alias_callbacks(cb, packed):
    assert cb.pack() == packed


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (GuessPlayCb(action="quit"), "guess_play:quit:"),
        (GuessPlayCb(action="resume", round_id=7), "guess_play:resume:7"),
    ],
)
def test_pack_guess_play_callbacks(cb, packed):
    assert cb.pack() == packed


@pytest.mark.parametrize(
    "data",
    [
        "guess_play:resume:not-a-round-id",
        "guess_play:resume:1.0",
        "guess_play:resume:+1",
    ],
)
async def test_guess_play_round_id_must_contain_only_digits(data):
    assert await GuessPlayCb.filter()(_query(data)) is False


@pytest.mark.parametrize(
    "data",
    [
        "guess_alias:add:not-a-round-id",
        "guess_alias:add:-",
        "guess_alias:add:1.0",
        "guess_alias:add:+1",
    ],
)
async def test_guess_alias_round_id_must_contain_only_digits(data):
    assert await GuessAliasCb.filter()(_query(data)) is False


@pytest.mark.parametrize("data", ["guess_new:hint_at::not-a-number", "guess_new:hint_at::-"])
async def test_guess_creation_threshold_is_typed_by_the_filter(data):
    assert await GuessNewCb.filter()(_query(data)) is False


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (QuizEditCb(action="cancel"), "quiz_edit:cancel::"),
        (QuizEditCb(action="nav", quiz_id=7, index=2), "quiz_edit:nav:7:2"),
        (QuizEditCb(action="correct", index=1), "quiz_edit:correct::1"),
    ],
)
def test_pack_quiz_edit_callbacks(cb, packed):
    assert cb.pack() == packed


def test_pack_quiz_answer_callback():
    """A quiz answer carries its action and all three database identifiers.

    Removing the action segment or serialising one identifier under a different
    field must make this public callback incompatible with its router filter.
    """
    assert (
        QuizAnswerCb(action="answer", quiz_id=7, question_id=8, option_id=2).pack()
        == "quiz_ans:answer:7:8:2"
    )


async def test_quiz_answer_numeric_fields_are_typed_by_the_filter():
    assert await QuizAnswerCb.filter()(_query("quiz_ans:answer:7:8:not-an-option")) is False


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (QuizTryCb(action="start", quiz_id=7), "quiz_try:start:7::"),
        (QuizTryCb(action="stop", quiz_id=7), "quiz_try:stop:7::"),
        (
            QuizTryCb(action="answer", quiz_id=7, question_id=8, option_id=2),
            "quiz_try:answer:7:8:2",
        ),
    ],
)
def test_pack_quiz_try_callbacks(cb, packed):
    assert cb.pack() == packed


@pytest.mark.parametrize(
    "data",
    [
        "quiz_try:start:not-an-quiz::",
        "quiz_try:answer:7:not-a-question:2",
        "quiz_try:answer:7:8:not-an-option",
    ],
)
async def test_quiz_try_numeric_fields_are_typed_by_the_filter(data):
    assert await QuizTryCb.filter()(_query(data)) is False


@pytest.mark.parametrize(
    "data",
    [
        "quiz_edit:nav:not-an-id:2",
        "quiz_edit:nav:7:not-an-index",
        "quiz_edit:correct::not-an-index",
    ],
)
async def test_quiz_edit_numeric_fields_are_typed_by_the_filter(data):
    assert await QuizEditCb.filter()(_query(data)) is False


@pytest.mark.parametrize(
    "data",
    [
        "quiz_new:time_limit::not-a-number",
        "quiz_new:correct::not-a-number",
    ],
)
async def test_quiz_creation_numeric_fields_are_typed_by_the_filter(data):
    assert await QuizNewCb.filter()(_query(data)) is False


@pytest.mark.parametrize(("action", "key"), [("bad:action", None), ("exec", "bad:key")])
def test_shop_callback_values_cannot_contain_the_separator(action, key):
    with pytest.raises(ValueError, match="Separator symbol"):
        ShopCb(action=action, key=key).pack()


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (AdminCb(action="home"), "adm:home::"),
        (AdminCb(action="lead_board", key="coins"), "adm:lead_board:coins:"),
        (AdminCb(action="users", item_id=2), "adm:users::2"),
        (AdminCb(action="act", key="credit", item_id=42), "adm:act:credit:42"),
    ],
)
def test_pack_admin_callbacks(cb, packed):
    assert cb.pack() == packed


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (AdminBetCb(action="list"), "admin_bet:list::"),
        (AdminBetCb(action="event", event_id=7), "admin_bet:event:7:"),
        (AdminBetCb(action="pick_winner", event_id=7, option_id=9), "admin_bet:pick_winner:7:9"),
    ],
)
def test_pack_admin_betting_callbacks(cb, packed):
    assert cb.pack() == packed


async def test_admin_bet_numeric_fields_are_typed_by_the_filter():
    assert await AdminBetCb.filter()(_query("admin_bet:event:x:")) is False


@pytest.mark.parametrize(
    ("cb", "packed"),
    [
        (BetCb(action="cancel_creation"), "bet:cancel_creation:"),
        (BetCb(action="window", seconds=60), "bet:window:60"),
        (BetEventCb(action="view", event_id=7), "event:view:7"),
        (BetOptionCb(action="pick", event_id=7, option_id=9), "bet_option:pick:7:9"),
        (
            BetAmountCb(action="pick", event_id=7, option_id=9, amount=100),
            "bet_amount:pick:7:9:100",
        ),
        (BetCustomCb(action="open", event_id=7, option_id=9), "bet_custom:open:7:9"),
        (
            BetConfirmCb(action="place", event_id=7, option_id=9, amount=100),
            "bet_confirm:place:7:9:100",
        ),
    ],
)
def test_pack_betting_callbacks(cb, packed):
    assert cb.pack() == packed


@pytest.mark.parametrize(
    ("callback_class", "data"),
    [
        (BetCb, "bet:window:not-a-number"),
        (BetEventCb, "event:view:not-an-id"),
        (BetOptionCb, "bet_option:pick:not-an-id:9"),
        (BetAmountCb, "bet_amount:pick:7:9:not-an-amount"),
        (BetCustomCb, "bet_custom:open:7:not-an-id"),
        (BetConfirmCb, "bet_confirm:place:not-an-id:9:100"),
    ],
)
async def test_betting_numeric_fields_are_typed_by_the_filter(callback_class, data):
    assert await callback_class.filter()(_query(data)) is False


async def test_admin_numeric_field_is_typed_by_the_filter():
    assert await AdminCb.filter()(_query("adm:users::two")) is False


def test_unpack_restores_the_types():
    cb = SchedCb.unpack("sched:pick:quiz:7")
    assert cb.action == "pick"
    assert cb.key == "quiz"
    assert cb.item_id == 7, "the id must come back as int, not str"


async def test_a_non_numeric_id_never_reaches_the_handler():
    """Today `cb_pick_event` defends itself with `raw_id.isdigit()`. Tomorrow it never arrives."""
    assert await SchedCb.filter()(_query("sched:pick:quiz:abc")) is False


async def test_a_well_formed_payload_is_injected():
    result = await SchedCb.filter()(_query("sched:pick:quiz:7"))
    assert result == {"callback_data": SchedCb(action="pick", key="quiz", item_id=7)}


async def test_a_payload_from_an_older_deploy_falls_through():
    """Optional fields leave the separators: the old payload is shorter.

    This isn't a flaw to hide — it's the reason the catch-all in `common`
    (Task 1) exists and comes first.
    """
    assert await SchedCb.filter()(_query("sched:cancel")) is False


def test_the_separator_cannot_hide_in_a_value():
    with pytest.raises(ValueError, match="Separator symbol"):
        SchedCb(action="type", key="a:b").pack()


def test_the_64_byte_ceiling_shows_up_in_tests_not_in_chat():
    with pytest.raises(ValueError, match="too long"):
        SchedCb(action="x" * 70).pack()


@pytest.mark.parametrize(
    "cb, expected",
    [
        (EventCb(action="home"), "ev:home::"),
        (EventCb(action="list", task_type="quiz"), "ev:list:quiz:"),
        (EventCb(action="item", task_type="quiz", item_id=7), "ev:item:quiz:7"),
        # byte-for-byte the payload we ship today
        (EventCb(action="askstart", task_type="quiz", item_id=7), "ev:askstart:quiz:7"),
        # today's optional 5th segment becomes an action of its own, same length
        (EventCb(action="sched", task_type="quiz", item_id=7), "ev:sched:quiz:7"),
        (EventCb(action="sched_close", task_type="quiz", item_id=7), "ev:sched_close:quiz:7"),
        (PollCreateCb(action="cancel"), "evpt:cancel"),
    ],
)
def test_pack_events(cb, expected):
    assert cb.pack() == expected


async def test_the_poll_triangle_does_not_answer_to_the_hub_prefix():
    """`evpt` is a family of its own: a hub payload must not match it."""
    assert await PollCreateCb.filter()(_query("ev:item:quiz:7")) is False
    assert await EventCb.filter()(_query("evpt:cancel")) is False


def test_the_longest_real_event_payload_fits():
    """The ceiling is 64 bytes, and event-type keys are chosen by whoever writes the code."""
    packed = EventCb(action="sched_close", task_type="guess_sound", item_id=999_999).pack()
    assert len(packed.encode()) <= 64, packed


@pytest.mark.parametrize("data", ["ev:item:fake:abc", "ev:close:fake:x"])
async def test_a_non_numeric_event_id_never_reaches_the_handler(data):
    """`cb_item` and `cb_close` used to defend themselves with `raw.isdigit()`. Tomorrow it never arrives."""
    assert await EventCb.filter()(_query(data)) is False


async def test_an_unknown_confirm_action_never_reaches_the_handler():
    """`cb_confirm` used to default an unrecognised `ask*` action to a silent no-op
    (`conf is None`); now the filter only admits the four known ones, so a fifth
    action never gets there and that branch is gone.

    Built from `_CONFIRM` itself, not a copy of its keys: a fifth key added there
    without a matching update here would otherwise leave this test asserting
    something no longer true.
    """
    confirm_filter = EventCb.filter(F.action.in_(_CONFIRM))
    assert await confirm_filter(_query("ev:askqualcosa:fake:7")) is False


# ---------------------------------------------------------------------------
# Wiring: nothing else may spell a typed prefix by hand.
#
# The `admin_dashboard_kb.py` bug this section pins: the "🎬 Eventi" button kept
# drawing `callback_data="ev:home"` after `EventCb` shipped. `EventCb.unpack`
# wants two more (empty) fields — `"ev:home::"` — so the literal never matched,
# the button fell through to the Task-1 catch-all, and the suite stayed green
# throughout, because no test tied the payload a keyboard writes to the `unpack`
# that has to accept it later.
# ---------------------------------------------------------------------------


def test_valid_action_probe_fills_required_fields():
    class RequiredProbeCb(CallbackData, prefix="required_probe"):
        action: str
        event_id: int
        option_id: int

    probe = _valid_action_probe(RequiredProbeCb, "open")

    assert probe == RequiredProbeCb(action="open", event_id=1, option_id=1)


_REQUIRED_FIELD_PROBES: dict[object, object] = {
    str: "probe",
    int: 1,
    bool: True,
}


def _central_callback_classes() -> list[type[CallbackData]]:
    return [cls for cls in CallbackData.__subclasses__() if cls.__module__ == "handlers.callbacks"]


def _valid_action_probe(cls: type[CallbackData], action: str) -> CallbackData:
    values: dict[str, object] = {"action": action}
    for name, field in cls.model_fields.items():
        if name == "action" or not field.is_required():
            continue
        try:
            values[name] = _REQUIRED_FIELD_PROBES[field.annotation]
        except KeyError as exc:
            raise AssertionError(
                f"no deterministic probe for required field "
                f"{cls.__name__}.{name}: {field.annotation!r}"
            ) from exc
    return cls(**values)


def _typed_callback_prefixes() -> dict[str, type[CallbackData]]:
    """Direct `CallbackData` subclasses already imported from the central module.

    Walks `__subclasses__()` instead of listing `SchedCb, EventCb, PollCreateCb`
    by hand: a class a future task adds to `handlers/callbacks.py` — the only
    module allowed to define one — is covered once that module has been imported,
    with no edit to this test required. This is deliberately neither recursive nor
    a claim about subclasses in modules the test process has not imported.
    """
    return {cls.__prefix__: cls for cls in _central_callback_classes()}


def test_the_prefix_scan_actually_finds_callback_classes():
    """Guards the guard: an empty result would make the test below pass forever
    for the wrong reason — nothing left to shadow."""
    assert {
        "adm",
        "admin_bet",
        "bet",
        "event",
        "bet_option",
        "bet_amount",
        "bet_custom",
        "bet_confirm",
        "sched",
        "ev",
        "evpt",
        "quiz_new",
        "quiz_edit",
        "quiz_ans",
        "quiz_try",
        "guess_new",
        "shop",
        "lead",
    } <= _typed_callback_prefixes().keys()


def test_no_handwritten_payload_shadows_a_typed_prefix():
    """No file under `src/`, other than `callbacks.py` itself, may spell one of its
    prefixes as a hand-rolled string literal (`"ev:..."`, `f"sched:..."`, …). That
    is exactly the shape of the `admin_dashboard_kb.py` bug above, for every screen
    that draws a button in this grammar — not just that one file.
    """
    patterns = {
        prefix: re.compile(rf"""["']{re.escape(prefix)}:""")
        for prefix in _typed_callback_prefixes()
    }
    src_dir = Path(__file__).resolve().parents[2] / "src"

    offenders = []
    for path in src_dir.rglob("*.py"):
        if path.name == "callbacks.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(pattern.search(line) for pattern in patterns.values()):
                offenders.append(f"{path.relative_to(src_dir.parent)}:{lineno}: {line.strip()}")

    assert offenders == [], (
        "hand-rolled payload(s) shadow a typed CallbackData prefix — pack the "
        "class instead of writing the string:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Wiring, round 2: nothing else may spell a typed *action* the router disowns.
#
# The scan above catches a hand-rolled prefix, but a button built entirely
# through the class — `EventCb(action="askdel", ...).pack()`, no literal in
# sight — is just as dead if the router's own filter no longer accepts that
# action (e.g. `_CONFIRM`'s key renamed to `"ask_del"` and a producer missed).
# No prefix ever goes hand-rolled in that scenario, so the scan above stays
# green while every 🗑️ Elimina button in the project stops responding.
# ---------------------------------------------------------------------------


def _constructed_actions() -> dict[type[CallbackData], set[str]]:
    """Every `action="..."` literal passed to `<Class>(action=...)` anywhere under
    `src/`, plus the executor actions that `cb_confirm` derives from `_CONFIRM`,
    grouped by class.

    The AST scan finds literal string keyword arguments regardless of keyword
    order. `cb_confirm` instead
    forwards the first value of each `_CONFIRM` tuple as `EventCb.action`; add
    those values directly from their production source so an executor renamed to
    an unclaimed action creates a failing dead-button check rather than another
    hand-maintained action list.
    """
    classes = {cls.__name__: cls for cls in _central_callback_classes()}
    src_dir = Path(__file__).resolve().parents[2] / "src"

    found: dict[type[CallbackData], set[str]] = {cls: set() for cls in classes.values()}
    for path in src_dir.rglob("*.py"):
        if path.name == "callbacks.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: dict[str, type[CallbackData]] = {}
        module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "handlers.callbacks":
                for alias in node.names:
                    callback_class = classes.get(alias.name)
                    if callback_class is not None:
                        imported[alias.asname or alias.name] = callback_class
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "handlers.callbacks" and alias.asname is not None:
                        module_aliases.add(alias.asname)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callback_class = imported.get(node.func.id) if isinstance(node.func, ast.Name) else None
            if (
                callback_class is None
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_aliases
            ):
                callback_class = classes.get(node.func.attr)
            if callback_class is None:
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "action"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    found[callback_class].add(keyword.value.value)
    found[EventCb].update(exec_action for exec_action, _, _ in _CONFIRM.values())
    return found


def _registered_action_filters() -> dict[type[CallbackData], list[CallbackQueryFilter]]:
    """Every `CallbackQueryFilter` actually registered on a real router, grouped by
    the class it guards — the live objects the dispatcher itself runs, not a
    re-parsed guess at what `F.action == "..."` or `F.action.in_(...)` means. That
    is what makes this robust to whatever a filter expression looks like next
    (`&`, `~`, a set built elsewhere, …) instead of a regex that only understands
    today's two spellings.
    """
    classes = _central_callback_classes()
    found: dict[type[CallbackData], list[CallbackQueryFilter]] = {cls: [] for cls in classes}
    for router in handlers.ROUTERS:
        for handler in router.callback_query.handlers:
            for filter_obj in handler.filters or []:
                cb_filter = filter_obj.callback
                if isinstance(cb_filter, CallbackQueryFilter) and cb_filter.callback_data in found:
                    found[cb_filter.callback_data].append(cb_filter)
    return found


def test_the_action_scan_actually_finds_something():
    """Guards the guard: an empty result on either side would make the test below
    pass forever for the wrong reason — nothing left to compare."""
    constructed = _constructed_actions()
    registered = _registered_action_filters()
    assert any(constructed.values()), "the constructor scan found no actions — it broke"
    assert any(registered.values()), "no registered CallbackQueryFilter found — it broke"


async def test_every_constructed_action_reaches_a_registered_filter():
    """No `action` a builder actually constructs may be one a real router filter
    disowns — that is a button no tap will ever reach, with no hand-rolled string
    anywhere to catch it. Rename `_CONFIRM`'s executor value `"del"` to
    `"delete"` in `events.py`, and this turns red because `cb_confirm` builds
    exactly that otherwise-unclaimed Yes button.
    """
    constructed = _constructed_actions()
    registered = _registered_action_filters()

    dead = []
    for cls, actions in constructed.items():
        filters = registered[cls]
        for action in sorted(actions):
            probe = _query(_valid_action_probe(cls, action).pack())
            if not any([await f(probe) for f in filters]):
                dead.append(f"{cls.__name__}(action={action!r})")

    assert dead == [], (
        "constructed action(s) with no registered filter that accepts them — "
        "dead button(s):\n" + "\n".join(dead)
    )


def test_callback_declarations_have_no_project_local_imports():
    src_dir = Path(__file__).resolve().parents[2] / "src"
    callback_module = src_dir / "handlers" / "callbacks.py"
    local_roots = {path.stem for path in src_dir.glob("*.py")}
    local_roots.update(
        path.name for path in src_dir.iterdir() if path.is_dir() and (path / "__init__.py").exists()
    )
    tree = ast.parse(callback_module.read_text(encoding="utf-8"), filename=str(callback_module))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name for alias in node.names if alias.name.split(".", 1)[0] in local_roots
            )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if node.level or root in local_roots:
                offenders.append("." * node.level + (node.module or ""))
    assert offenders == [], (
        "handlers/callbacks.py must remain a project-import-free leaf module; "
        f"project imports: {offenders}"
    )
