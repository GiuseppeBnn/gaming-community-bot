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

import pytest
from aiogram.types import CallbackQuery, User

from handlers.callbacks import SchedCb


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
