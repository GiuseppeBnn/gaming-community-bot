from __future__ import annotations

from sqlalchemy import func, select

from database.models import AlduinoGroupMessage
from services import group_context


async def _record(session, message_id: int, text: str, *, reply_to=None):
    result = await group_context.record_message(
        session,
        group_id=-100,
        message_id=message_id,
        user_tg_id=message_id + 100,
        display_name=f"Utente {message_id}",
        username=f"u{message_id}",
        text=text,
        reply_to_message_id=reply_to,
    )
    await session.commit()
    return result


async def test_messages_are_deduplicated_pruned_and_rendered_chronologically(
    session, monkeypatch,
):
    monkeypatch.setattr(group_context.settings, "alduino_group_memory_rows", 2)
    monkeypatch.setattr(group_context.settings, "alduino_group_context_messages", 10)
    monkeypatch.setattr(group_context.settings, "alduino_group_context_chars", 5000)
    assert await _record(session, 1, "uno") is True
    assert await _record(session, 1, "duplicato") is False
    assert await _record(session, 2, "due", reply_to=1) is True
    assert await _record(session, 3, "tre") is True

    lines = await group_context.recent_messages(
        session, group_id=-100, exclude_message_id=3,
    )
    assert [line.message_id for line in lines] == [2]
    assert group_context.render_context(lines) == "#2 ↪ #1 · Utente 2 (@u2): due"
    count = (await session.execute(select(func.count(AlduinoGroupMessage.id)))).scalar_one()
    assert count == 2


async def test_context_keeps_newest_complete_lines_under_character_cap(session, monkeypatch):
    monkeypatch.setattr(group_context.settings, "alduino_group_context_messages", 10)
    monkeypatch.setattr(group_context.settings, "alduino_group_context_chars", 2000)
    await _record(session, 1, "a" * 1300)
    await _record(session, 2, "b" * 1300)
    lines = await group_context.recent_messages(session, group_id=-100)
    assert [line.message_id for line in lines] == [2]
    assert len(group_context.clip_message("x" * 5000)) == 1500

    # Defensive first-line clipping is reachable even if a test/operator
    # bypasses the validated Settings bounds at runtime.
    monkeypatch.setattr(group_context.settings, "alduino_group_context_chars", 20)
    clipped = await group_context.recent_messages(session, group_id=-100)
    assert clipped and len(clipped[0].text) <= 20


async def test_empty_message_is_not_stored(session):
    assert await group_context.record_message(
        session,
        group_id=-100,
        message_id=1,
        user_tg_id=2,
        display_name="",
        username=None,
        text="   ",
        reply_to_message_id=None,
    ) is False
