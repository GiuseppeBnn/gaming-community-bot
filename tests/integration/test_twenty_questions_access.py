"""Quick commands and durable reply targets share the authoritative game rules."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters.command import CommandObject
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AIGameMessage, AIGameSession, AIGameTurn
from handlers import twenty_questions as handler
from handlers.event_types.twenty_questions_type import TwentyQuestionsType
from services import ai_game_service
from services.ai_game_types import FinishReason, QuestionVerdict, StartRejectReason
from tests.integration.test_twenty_questions_handlers import _Bot, _Message, _ready, _v2_ready


class ReplyMessage(_Message):
    async def reply(self, text, **kwargs):
        await super().reply(text, **kwargs)
        return SimpleNamespace(message_id=1000 + len(self.said))


async def running(session, monkeypatch):
    session_id = await _v2_ready(session, monkeypatch)
    assert (await ai_game_service.start(session, session_id, group_id=-1001)).started
    await ai_game_service.move_anchor(session, session_id, 77)
    await session.commit()
    monkeypatch.setattr(handler, "refresh_group_card", AsyncMock())
    return session_id


async def test_status_then_reply_to_verdict_then_guess_share_the_game(session, monkeypatch):
    session_id = await running(session, monkeypatch)
    ai = AsyncMock(return_value=SimpleNamespace(value=QuestionVerdict.si))
    monkeypatch.setattr(ai_game_service, "classify_question", ai)
    status = _Message("/gioco", anchor=None)
    await handler.cmd_gioco_alduino(status, session, CommandObject(command="gioco"))
    assert (await session.get(AIGameMessage, (-1001, 999))).session_id == session_id
    await session.rollback()

    question = ReplyMessage("Ci sono enigmi?", anchor=999)
    await handler.play_turn(question, session)
    assert "SÌ" in question.said[-1]
    mapped = await session.get(AIGameMessage, (-1001, 1001))
    assert mapped.session_id == session_id
    await session.rollback()

    guess = _Message("RISPOSTA: Un altro videogioco", anchor=1001)
    await handler.play_turn(guess, session)
    assert "Non è lui" in guess.said[-1]
    assert ai.await_count == 1
    assert (await session.execute(select(func.count(AIGameTurn.id)))).scalar_one() == 2


@pytest.mark.parametrize("args,is_guess", [
    ("Ha una campagna cooperativa?", False),
    ("RISPOSTA: Un altro gioco", True),
])
async def test_command_plays_without_reply_or_command_text_in_ai(session, monkeypatch, args, is_guess):
    await running(session, monkeypatch)
    ai = AsyncMock(return_value=SimpleNamespace(value=QuestionVerdict.no))
    monkeypatch.setattr(ai_game_service, "classify_question", ai)
    message = ReplyMessage("/gioco " + args, anchor=None)
    await handler.cmd_gioco_alduino(message, session, CommandObject(command="gioco", args=args))
    if is_guess:
        ai.assert_not_awaited()
        assert "Non è lui" in message.said[-1]
    else:
        assert ai.call_args.args[0].input_text == args
        assert "NO" in message.said[-1]


async def test_old_reply_never_targets_new_game_and_cross_group_is_ignored(session, monkeypatch):
    old = await running(session, monkeypatch)
    await ai_game_service.remember_game_message(session, session_id=old, group_id=-1001, message_id=888)
    await ai_game_service.terminalize(session, session_id=old, reason=FinishReason.admin_closed)
    await session.commit()
    new = await _v2_ready(session, monkeypatch, "Nuova partita")
    assert (await ai_game_service.start(session, new, group_id=-1001)).started
    await session.commit()
    ai = AsyncMock()
    monkeypatch.setattr(ai_game_service, "classify_question", ai)
    old_reply = _Message("Ha enigmi?", anchor=888)
    await handler.play_turn(old_reply, session)
    assert "conclusa" in old_reply.said[-1]
    for message in (_Message("SÌ", anchor=888, group=-1002), _Message("NO", anchor=444)):
        with pytest.raises(SkipHandler):
            await handler.play_turn(message, session)
        await session.rollback()
    ai.assert_not_awaited()
    assert (await session.execute(select(func.count(AIGameTurn.id)))).scalar_one() == 0


async def test_mapping_failure_does_not_undo_completed_question(session, monkeypatch, caplog):
    session_id = await running(session, monkeypatch)
    monkeypatch.setattr(ai_game_service, "classify_question", AsyncMock(return_value=SimpleNamespace(value=QuestionVerdict.si)))
    monkeypatch.setattr(ai_game_service, "remember_game_message", AsyncMock(side_effect=RuntimeError("private detail")))
    message = ReplyMessage("Ci sono enigmi?")
    await handler.play_turn(message, session)
    assert len(message.said) == 1 and "SÌ" in message.said[0]
    assert "private detail" not in caplog.text
    snapshot = await ai_game_service.get_snapshot(session, session_id)
    assert snapshot.game.questions_used == 1


async def test_single_active_per_group_releases_after_close(session, monkeypatch):
    first = await running(session, monkeypatch)
    second = await _v2_ready(session, monkeypatch, "Seconda")
    blocked = await ai_game_service.start(session, second, group_id=-1001)
    assert not blocked.started and blocked.reason is StartRejectReason.active_game
    await session.rollback()
    assert (await session.get(AIGameSession, second)).status == "ready"
    await ai_game_service.terminalize(session, session_id=first, reason=FinishReason.admin_closed)
    await session.commit()
    assert (await ai_game_service.start(session, second, group_id=-1001)).started
    await session.commit()
    third = await _v2_ready(session, monkeypatch, "Altro gruppo")
    assert (await ai_game_service.start(session, third, group_id=-1002)).started


async def test_reply_mapping_survives_new_session_and_rejects_wrong_group(session, monkeypatch):
    session_id = await running(session, monkeypatch)
    await ai_game_service.remember_game_message(
        session, session_id=session_id, group_id=-1001, message_id=888,
    )
    # An inconsistent/imported mapping must not give access to another group.
    await ai_game_service.remember_game_message(
        session, session_id=session_id, group_id=-1002, message_id=889,
    )
    await session.commit()
    async with AsyncSession(session.bind) as fresh:
        snapshot = await ai_game_service.find_by_game_message(fresh, -1001, 888)
        assert snapshot.session.id == session_id
        assert await ai_game_service.find_by_game_message(fresh, -1002, 889) is None


async def test_existing_game_blocks_legacy_start_and_event_ui_explains(session, monkeypatch):
    await running(session, monkeypatch)
    legacy = await _ready(session, "Legacy")
    assert not await ai_game_service.start(
        session, legacy, group_id=-1001, anchor_message_id=123,
    )
    await session.rollback()
    newer = await _v2_ready(session, monkeypatch, "Nuova")
    result = await TwentyQuestionsType()._open(_Bot(), session, newer, group_id=-1001)
    assert not result.ok and result.alert
    assert "già una partita" in result.message


@pytest.mark.parametrize("legacy", [False, True])
async def test_unknown_is_free_and_can_be_followed_by_a_question(session, monkeypatch, legacy):
    if legacy:
        session_id = await _ready(session)
        assert await ai_game_service.start(session, session_id, group_id=-1001, anchor_message_id=77)
        await session.commit()
        value = QuestionVerdict.non_lo_so
    else:
        session_id = await running(session, monkeypatch)
        value = SimpleNamespace(value=QuestionVerdict.non_lo_so)
    ai = AsyncMock(return_value=value)
    monkeypatch.setattr(ai_game_service, "classify_question", ai)
    question = ReplyMessage("È difficile da finire?")
    await handler.play_turn(question, session)
    assert "NON LO SO" in question.said[-1] and "non consumata" in question.said[-1]
    assert "FORSE" not in question.said[-1]
    snapshot = await ai_game_service.get_snapshot(session, session_id)
    assert snapshot.game.questions_used == 0 and not snapshot.turns
    assert snapshot.session.pending_token is None
    if not legacy:
        ai.return_value = SimpleNamespace(value=QuestionVerdict.si)
        await handler.play_turn(_Message("Ci sono enigmi?", anchor=1001), session)
        assert ai.await_count == 2
