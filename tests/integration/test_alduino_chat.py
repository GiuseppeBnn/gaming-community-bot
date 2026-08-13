from __future__ import annotations

from sqlalchemy import func, select

from database.models import AlduinoTurn
from services import alduino_chat
from services.alduino_chat import DialogueTurn, GeneratedReply


async def _record(
    session, *, user_message: int, bot_message: int,
    parent: AlduinoTurn | None = None, text: str = "ciao", answer: str = "salve",
    interaction: str | None = None,
) -> AlduinoTurn:
    row = await alduino_chat.record_turn(
        session,
        group_id=-100,
        user_tg_id=7,
        user_message_id=user_message,
        bot_message_id=bot_message,
        parent=parent,
        user_text=text,
        reply=GeneratedReply(answer, "gemini" if interaction else "groq", interaction),
    )
    await session.commit()
    return row


async def test_turn_is_found_by_telegram_reply_anchor(session):
    root = await _record(
        session, user_message=1, bot_message=2, interaction="gemini-root",
    )

    found = await alduino_chat.find_parent(session, -100, 2)

    assert found is not None and found.id == root.id
    assert found.provider_interaction_id == "gemini-root"
    assert alduino_chat.decode_history(found.history_json) == (DialogueTurn("ciao", "salve"),)
    assert await alduino_chat.find_parent(session, -100, 999) is None


async def test_two_replies_to_one_message_form_independent_branches(session):
    root = await _record(session, user_message=1, bot_message=2, text="root", answer="R")
    left = await _record(
        session, user_message=3, bot_message=4, parent=root, text="sinistra", answer="L",
    )
    right = await _record(
        session, user_message=5, bot_message=6, parent=root, text="destra", answer="D",
    )

    left_history = alduino_chat.decode_history(left.history_json)
    right_history = alduino_chat.decode_history(right.history_json)
    assert left.parent_turn_id == right.parent_turn_id == root.id
    assert [turn.user for turn in left_history] == ["root", "sinistra"]
    assert [turn.user for turn in right_history] == ["root", "destra"]


async def test_old_rows_are_pruned_but_new_snapshot_stays_complete(session, monkeypatch):
    monkeypatch.setattr(alduino_chat.settings, "alduino_memory_rows_per_group", 2)
    first = await _record(session, user_message=1, bot_message=2, text="uno")
    second = await _record(session, user_message=3, bot_message=4, parent=first, text="due")
    third = await _record(session, user_message=5, bot_message=6, parent=second, text="tre")

    rows = list((await session.execute(
        select(AlduinoTurn).where(AlduinoTurn.group_id == -100).order_by(AlduinoTurn.id)
    )).scalars())
    assert [row.id for row in rows] == [second.id, third.id]
    assert [turn.user for turn in alduino_chat.decode_history(third.history_json)] == [
        "uno", "due", "tre",
    ]
    assert (await session.execute(select(func.count(AlduinoTurn.id)))).scalar_one() == 2
