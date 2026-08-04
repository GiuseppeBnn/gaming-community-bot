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

import re
from pathlib import Path

import pytest
from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, User

from handlers.callbacks import EventCb, PollCreateCb, SchedCb


def _query(data: str) -> CallbackQuery:
    """A real CallbackQuery: the filter does an `isinstance` check, a fake one
    would return `False` for the wrong reason."""
    return CallbackQuery(
        id="1", from_user=User(id=1, is_bot=False, first_name="A"),
        chat_instance="x", data=data,
    )


@pytest.mark.parametrize("cb, expected", [
    (SchedCb(action="cancel"), "sched:cancel::"),
    (SchedCb(action="type", key="quiz"), "sched:type:quiz:"),
    (SchedCb(action="pick", key="quiz", item_id=7), "sched:pick:quiz:7"),
    (SchedCb(action="del", item_id=7), "sched:del::7"),
])
def test_pack(cb, expected):
    assert cb.pack() == expected


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


@pytest.mark.parametrize("cb, expected", [
    (EventCb(action="home"), "ev:home::"),
    (EventCb(action="list", task_type="quiz"), "ev:list:quiz:"),
    (EventCb(action="item", task_type="quiz", item_id=7), "ev:item:quiz:7"),
    # byte-for-byte the payload we ship today
    (EventCb(action="askstart", task_type="quiz", item_id=7), "ev:askstart:quiz:7"),
    # today's optional 5th segment becomes an action of its own, same length
    (EventCb(action="sched", task_type="quiz", item_id=7), "ev:sched:quiz:7"),
    (EventCb(action="sched_close", task_type="quiz", item_id=7), "ev:sched_close:quiz:7"),
    (PollCreateCb(action="cancel"), "evpt:cancel"),
])
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
    action never gets there and that branch is gone."""
    confirm_filter = EventCb.filter(F.action.in_({"askstart", "askclose", "askdel", "askreset"}))
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

def _typed_callback_prefixes() -> dict[str, type[CallbackData]]:
    """Every `CallbackData` subclass that exists, keyed by its wire prefix.

    Walks `__subclasses__()` instead of listing `SchedCb, EventCb, PollCreateCb`
    by hand: a class a future task adds to `handlers/callbacks.py` — the only
    module allowed to define one — is covered here the moment it exists, with no
    edit to this test required.
    """
    return {cls.__prefix__: cls for cls in CallbackData.__subclasses__()}


def test_the_prefix_scan_actually_finds_callback_classes():
    """Guards the guard: an empty result would make the test below pass forever
    for the wrong reason — nothing left to shadow."""
    assert {"sched", "ev", "evpt"} <= _typed_callback_prefixes().keys()


def test_no_handwritten_payload_shadows_a_typed_prefix():
    """No file under `src/`, other than `callbacks.py` itself, may spell one of its
    prefixes as a hand-rolled string literal (`"ev:..."`, `f"sched:..."`, …). That
    is exactly the shape of the `admin_dashboard_kb.py` bug above, for every screen
    that draws a button in this grammar — not just that one file.
    """
    patterns = {
        prefix: re.compile(rf'''["']{re.escape(prefix)}:''')
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
