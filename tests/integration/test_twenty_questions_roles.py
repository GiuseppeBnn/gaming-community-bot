"""Explicit play/management commands and a strictly public status projection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters.command import CommandObject
from sqlalchemy import func, select, update

from database.models import AIGameMessage, AIGameSession, AIGameTurn
from handlers import twenty_questions as handler
from handlers.callbacks import TwentyQuestionsStatusCb
from handlers.event_types.twenty_questions_type import TwentyQuestionsType
from services import ai_game_service, group_registry
from services.ai_game_types import QuestionVerdict
from tests.integration.test_twenty_questions_access import running
from tests.integration.test_twenty_questions_handlers import TARGET, _Message, _v2_ready


@pytest.fixture(autouse=True)
def public_actor(monkeypatch):
    monkeypatch.setattr(handler, "is_admin", AsyncMock(return_value=False))
    monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)


async def test_admin_group_command_only_redirects_to_personal_management(session, monkeypatch):
    await _v2_ready(session, monkeypatch)
    monkeypatch.setattr(handler, "is_admin", AsyncMock(return_value=True))
    manage = AsyncMock()
    monkeypatch.setattr(TwentyQuestionsType, "render_list", manage)
    message = _Message("/gestisci_aldusgame")
    message.reply = AsyncMock()
    message.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="testbot"))
    await handler.cmd_gestisci_gioco_alduino(message, session)
    manage.assert_not_awaited()
    text = message.reply.call_args.args[0]
    assert "continua in privato" in text
    assert TARGET.title not in text and "V2" not in text
    kb = message.reply.call_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].url == "https://t.me/testbot?start=manage_twentyq"


async def test_admin_private_command_reuses_existing_management_with_status_button(session, monkeypatch):
    await _v2_ready(session, monkeypatch, "Partita pronta")
    monkeypatch.setattr(handler, "is_admin", AsyncMock(return_value=True))
    start = AsyncMock(side_effect=AssertionError("a command must not launch a game"))
    monkeypatch.setattr(ai_game_service, "start", start)
    message = _Message("/gestisci_aldusgame", chat_type="private", group=42, anchor=None)
    await handler.cmd_gestisci_gioco_alduino(message, session)
    assert "Partita pronta" in message.said[-1]
    assert "Crea il gioco segreto" in str(message.markups[-1])
    assert TwentyQuestionsStatusCb(action="show").pack() in str(message.markups[-1])
    assert TARGET.title not in message.said[-1]
    assert not session.in_transaction()
    start.assert_not_awaited()


@pytest.mark.parametrize("admin", [True, False])
async def test_public_group_command_only_reports_state_for_every_role(session, monkeypatch, admin):
    session_id = await running(session, monkeypatch)
    role_check = AsyncMock(return_value=admin)
    monkeypatch.setattr(handler, "is_admin", role_check)
    ai = AsyncMock(side_effect=AssertionError("status must not call AI"))
    manage = AsyncMock()
    monkeypatch.setattr(ai_game_service, "classify_question", ai)
    monkeypatch.setattr(TwentyQuestionsType, "render_list", manage)
    message = _Message("/aldusgame", anchor=None)
    await handler.cmd_gioco_alduino(message, session)
    assert "Per te" in message.said[-1]
    assert TARGET.title not in message.said[-1]
    assert message.markups[-1] is None
    assert message.said[-1].endswith("Digita /aldusgame per avere le informazioni aggiornate")
    assert (await session.get(AIGameMessage, (-1001, 999))).session_id == session_id
    assert (await session.execute(select(func.count(AIGameTurn.id)))).scalar_one() == 0
    manage.assert_not_awaited()
    ai.assert_not_awaited()
    role_check.assert_not_awaited()  # Role must never change the play command's purpose.


@pytest.mark.parametrize("admin", [True, False])
async def test_private_play_command_never_opens_management(session, monkeypatch, admin):
    session_id = await running(session, monkeypatch)
    role_check = AsyncMock(return_value=admin)
    monkeypatch.setattr(handler, "is_admin", role_check)
    await session.execute(update(AIGameSession).where(AIGameSession.id == session_id).values(anchor_message_id=None))
    await session.commit()
    ai = AsyncMock(side_effect=AssertionError("private status must not call AI"))
    monkeypatch.setattr(ai_game_service, "classify_question", ai)
    message = _Message("/aldusgame Ha enigmi?", group=42, chat_type="private", anchor=None)
    await handler.cmd_gioco_alduino(message, session, CommandObject(command="aldusgame", args="Ha enigmi?"))
    text = message.said[-1]
    assert "Per te" in text and "La partita si gioca nel gruppo" in text
    assert "sempre e solo a questo messaggio" not in text
    assert TARGET.title not in text
    assert text.endswith("Digita /aldusgame per avere le informazioni aggiornate")
    handler.refresh_group_card.assert_not_awaited()
    role_check.assert_not_awaited()
    ai.assert_not_awaited()
    assert (await session.execute(select(func.count(AIGameTurn.id)))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(AIGameMessage))).scalar_one() == 0


async def test_private_status_callback_uses_clicker_quota_not_bot_message_author(session, monkeypatch):
    session_id = await running(session, monkeypatch)
    started = await ai_game_service.begin_question(session, session_id=session_id, user_tg_id=42, question="Ha enigmi?")
    await session.commit()
    await ai_game_service.complete_question(session, claim=started.claim, verdict=QuestionVerdict.si)
    await session.commit()
    # No admin gate here: this button is only the public projection, even when
    # it was originally drawn on an admin's management panel.
    monkeypatch.setattr(handler, "is_admin", AsyncMock(side_effect=AssertionError("public status")))
    message = _Message(group=42, chat_type="private", user=999999)
    callback = SimpleNamespace(message=message, from_user=SimpleNamespace(id=42), answer=AsyncMock())
    await handler.cb_game_status(callback, session)
    assert "4 domande" in message.said[-1]
    assert TARGET.title not in message.said[-1]
    callback.answer.assert_awaited_once()


@pytest.mark.parametrize("admin", [True, False])
async def test_explicit_group_turn_arguments_keep_identical_rules_for_both_roles(session, monkeypatch, admin):
    await running(session, monkeypatch)
    monkeypatch.setattr(handler, "is_admin", AsyncMock(return_value=admin))
    ai = AsyncMock(return_value=SimpleNamespace(value=QuestionVerdict.no))
    monkeypatch.setattr(ai_game_service, "classify_question", ai)
    message = _Message("/aldusgame Ha enigmi?", anchor=None)
    await handler.cmd_gioco_alduino(message, session, CommandObject(command="aldusgame", args="Ha enigmi?"))
    assert "NO" in message.said[-1]
    assert ai.call_args.args[0].input_text == "Ha enigmi?"
    assert (await session.execute(select(func.count(AIGameTurn.id)))).scalar_one() == 1


async def test_no_active_game_is_explicit_and_does_not_offer_management(session):
    message = _Message("/aldusgame", anchor=None)
    await handler.cmd_gioco_alduino(message, session)
    assert "non c'è una partita attiva" in message.said[-1]
    assert TARGET.title not in message.said[-1]
    assert message.markups[-1] is None
    assert message.said[-1].endswith("Digita /aldusgame per avere le informazioni aggiornate")


@pytest.mark.parametrize("kind", ["no_user", "bot", "anonymous"])
async def test_public_command_rejects_non_personal_actors_without_ai_or_turns(session, kind):
    message = _Message("/aldusgame Ha enigmi?", anchor=None)
    if kind == "no_user":
        message.from_user = None
    elif kind == "bot":
        message.from_user.is_bot = True
    else:
        message.sender_chat = SimpleNamespace(id=-1001)
    await handler.cmd_gioco_alduino(message, session, CommandObject(command="aldusgame", args="Ha enigmi?"))
    assert "profilo personale" in message.said[-1]
    assert not session.in_transaction()


@pytest.mark.parametrize("anonymous", [True, False])
async def test_management_programmatic_entry_rechecks_authority(session, monkeypatch, anonymous):
    message = _Message("/gestisci_aldusgame", group=42, chat_type="private")
    if anonymous:
        message.from_user = None
    monkeypatch.setattr(handler, "is_admin", AsyncMock(return_value=False))
    manage = AsyncMock()
    monkeypatch.setattr(TwentyQuestionsType, "render_list", manage)
    await handler.cmd_gestisci_gioco_alduino(message, session)
    assert "non autorizzato" in message.said[-1]
    manage.assert_not_awaited()


@pytest.mark.parametrize("command,admin,allowed", [
    ("/gestisci_aldusgame", True, True),
    ("/gestisci_aldusgame", False, False),
    ("/gestisci_aldusgame@testbot", True, True),
    ("/gestisci_aldusgame@testbot", False, False),
    ("/gestisci_gioco_alduino", True, False),
    ("/gestisci_gioco_alduino", False, False),
])
async def test_management_command_is_admin_gated_at_router_boundary(monkeypatch, command, admin, allowed):
    from datetime import datetime
    from aiogram import Bot
    from aiogram.types import Chat, Message, User
    from filters import admin_filter

    monkeypatch.setattr(admin_filter, "is_admin", AsyncMock(return_value=admin))
    bot = Bot(token="1234567890:AAFtesttoken_ci")
    monkeypatch.setattr(bot, "get_me", AsyncMock(return_value=SimpleNamespace(username="testbot")))
    message = Message(
        message_id=1, date=datetime(2026, 11, 16), text=command,
        chat=Chat(id=42, type="private"), from_user=User(id=42, first_name="Player", is_bot=False),
    ).as_(bot)
    registered = next(h for h in handler.router.message.handlers if h.callback is handler.cmd_gestisci_gioco_alduino)
    try:
        matched, _ = await registered.check(message, bot=bot)
        assert matched is allowed
    finally:
        await bot.session.close()


@pytest.mark.parametrize("chat_type,allowed", [("private", True), ("supergroup", False)])
async def test_public_status_button_is_private_and_disjoint_from_admin_callbacks(chat_type, allowed):
    from datetime import datetime
    from aiogram.types import CallbackQuery, Chat, Message, User

    data = TwentyQuestionsStatusCb(action="show")
    assert len(data.pack().encode()) < 64
    assert TwentyQuestionsStatusCb.unpack(data.pack()) == data
    callback = CallbackQuery(
        id="test", chat_instance="test", from_user=User(id=42, first_name="Player", is_bot=False),
        data=data.pack(), message=Message(message_id=1, date=datetime(2026, 11, 16),
                                         chat=Chat(id=42 if allowed else -1001, type=chat_type)),
    )
    registered = next(h for h in handler.router.callback_query.handlers if h.callback is handler.cb_game_status)
    matched, _ = await registered.check(callback)
    assert matched is allowed
    if allowed:
        forged = callback.model_copy(update={"data": "ev:item:twentyq:1"})
        matched, _ = await registered.check(forged)
        assert not matched
