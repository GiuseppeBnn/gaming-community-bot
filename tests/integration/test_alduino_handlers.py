from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from database.models import AlduinoTurn
from handlers import fun_ai
from services import alduino_chat
from services.alduino_chat import GeneratedReply
from services.public_event import PublicEvent
from utils import cooldown


@pytest.fixture(autouse=True)
def isolated_cooldown():
    cooldown.reset()
    yield
    cooldown.reset()


class Bot:
    id = 999

    async def send_chat_action(self, *args, **kwargs):
        return None


class Message:
    def __init__(self, message_id: int, text: str | None = None, reply_to=None):
        self.bot = Bot()
        self.chat = SimpleNamespace(id=-100, type="supergroup")
        self.from_user = SimpleNamespace(
            id=7, is_bot=False, username="utente", full_name="Utente",
        )
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.reply_to_message = reply_to
        self.replies: list[SimpleNamespace] = []

    async def reply(self, text, **kwargs):
        sent = SimpleNamespace(
            message_id=1000 + self.message_id + len(self.replies), text=text, kwargs=kwargs,
        )
        self.replies.append(sent)
        return sent


async def allow(*args, **kwargs):
    return True


async def no_live(_session):
    return ""


async def test_command_then_natural_reply_persists_and_continues_the_same_branch(
    session, monkeypatch,
):
    calls: list[AlduinoTurn | None] = []

    async def generate(**kwargs):
        calls.append(kwargs["parent"])
        number = len(calls)
        return GeneratedReply(f"risposta {number}", "gemini", f"interaction-{number}")

    monkeypatch.setattr(fun_ai, "is_admin", allow)
    monkeypatch.setattr(fun_ai, "_live_context", no_live)
    monkeypatch.setattr(alduino_chat, "generate_reply", generate)
    cooldown.reset()

    first = Message(10)
    await fun_ai.cmd_alduino(first, SimpleNamespace(args="ciao"), session)
    first_bot_message = first.replies[0]
    target = SimpleNamespace(
        message_id=first_bot_message.message_id,
        text=first_bot_message.text,
        caption=None,
        from_user=SimpleNamespace(id=Bot.id),
    )
    second = Message(11, text="e poi?", reply_to=target)
    await fun_ai.reply_to_alduino(second, session)

    rows = list((await session.execute(
        select(AlduinoTurn).order_by(AlduinoTurn.id)
    )).scalars())
    assert len(rows) == 2
    assert calls[0] is None
    assert calls[1] is not None and calls[1].id == rows[0].id
    assert rows[1].parent_turn_id == rows[0].id
    assert rows[1].provider_interaction_id == "interaction-2"
    assert [turn.user for turn in alduino_chat.decode_history(rows[1].history_json)] == [
        "ciao", "e poi?",
    ]
    assert second.replies[0].kwargs["parse_mode"] is None
    cooldown.reset()


async def test_provider_failure_replies_with_fallback_and_records_nothing(session, monkeypatch):
    async def down(**kwargs):
        raise alduino_chat.AlduinoAIError("down")

    monkeypatch.setattr(fun_ai, "is_admin", allow)
    monkeypatch.setattr(fun_ai, "_live_context", no_live)
    monkeypatch.setattr(alduino_chat, "generate_reply", down)
    cooldown.reset()

    message = Message(20)
    await fun_ai.cmd_alduino(message, SimpleNamespace(args="ciao"), session)

    assert message.replies[0].text == fun_ai.ai_service.AI_FALLBACK_MESSAGE
    assert list((await session.execute(select(AlduinoTurn))).scalars()) == []
    cooldown.reset()


async def test_live_context_uses_the_public_event_projection(session, monkeypatch):
    seen = {}

    async def public_events(db_session, *, event_types, limit):
        seen.update(session=db_session, event_types=event_types, limit=limit)
        return [PublicEvent("quiz", 3, "Quiz aperto", "Entra e gioca", "🧠")]

    monkeypatch.setattr(fun_ai.event_types, "all_types", lambda: ("registry",))
    monkeypatch.setattr(fun_ai.event_discovery, "list_public_events", public_events)

    rendered = await fun_ai._live_context(session)

    assert seen == {"session": session, "event_types": ("registry",), "limit": 10}
    assert "Quiz aperto (aperto adesso)" in rendered


async def test_context_db_failure_degrades_to_memoryless_reply(session, monkeypatch):
    async def broken_read(*args, **kwargs):
        raise SQLAlchemyError("read failed")

    async def generate(**kwargs):
        assert kwargs["parent"] is None
        assert kwargs["live_context"] == ""
        return GeneratedReply("continuo lo stesso", "groq")

    monkeypatch.setattr(fun_ai, "is_admin", allow)
    monkeypatch.setattr(alduino_chat, "find_parent", broken_read)
    monkeypatch.setattr(alduino_chat, "generate_reply", generate)
    cooldown.reset()

    message = Message(30)
    await fun_ai.cmd_alduino(message, SimpleNamespace(args="ciao"), session)

    assert message.replies[0].text == "continuo lo stesso"
    assert len(list((await session.execute(select(AlduinoTurn))).scalars())) == 1
    cooldown.reset()


async def test_persistence_failure_does_not_replace_the_sent_reply(session, monkeypatch):
    async def generate(**kwargs):
        return GeneratedReply("risposta già inviata", "groq")

    async def broken_write(*args, **kwargs):
        raise SQLAlchemyError("write failed")

    monkeypatch.setattr(fun_ai, "is_admin", allow)
    monkeypatch.setattr(fun_ai, "_live_context", no_live)
    monkeypatch.setattr(alduino_chat, "generate_reply", generate)
    monkeypatch.setattr(alduino_chat, "record_turn", broken_write)
    cooldown.reset()

    message = Message(40)
    await fun_ai.cmd_alduino(message, SimpleNamespace(args="ciao"), session)

    assert [reply.text for reply in message.replies] == ["risposta già inviata"]
    assert list((await session.execute(select(AlduinoTurn))).scalars()) == []
    cooldown.reset()


@pytest.mark.parametrize("command", ["maestro", "complotto", "difendi", "accusa", "drama", "dialetto", "insulta"])
async def test_fun_output_is_registered_and_can_start_a_natural_conversation(session, monkeypatch, command):
    monkeypatch.setattr(fun_ai, "is_admin", allow)
    monkeypatch.setattr(fun_ai, "_live_context", no_live)
    monkeypatch.setattr(fun_ai.ai_service, "generate_completion", AsyncMock(return_value="battuta fun"))
    generate = AsyncMock(return_value=GeneratedReply("seguito Alduino", "groq"))
    monkeypatch.setattr(alduino_chat, "generate_reply", generate)
    source = SimpleNamespace(text="aneddoto utente", caption=None)
    first = Message(50, text=f"/{command}", reply_to=source)
    if command == "insulta":
        await fun_ai.cmd_insulta(first, SimpleNamespace(args="Marco"), session)
    else:
        await getattr(fun_ai, f"cmd_{command}")(first, session)
    row = (await session.execute(select(AlduinoTurn))).scalar_one()
    assert row.provider == "fun" and row.provider_interaction_id is None
    assert row.bot_message_id == first.replies[0].message_id
    target = SimpleNamespace(message_id=row.bot_message_id, text="battuta fun", caption=None,
                             from_user=SimpleNamespace(id=Bot.id))
    second = Message(51, text="spiegami la battuta", reply_to=target)
    await fun_ai.reply_to_alduino(second, session)
    assert [reply.text for reply in second.replies] == ["seguito Alduino"]
    assert generate.await_args.kwargs["parent"].id == row.id
    rows = (await session.execute(select(AlduinoTurn).order_by(AlduinoTurn.id))).scalars().all()
    assert len(rows) == 2 and rows[1].parent_turn_id == row.id
    assert alduino_chat.decode_history(rows[1].history_json)[0].alduino == "battuta fun"


@pytest.mark.parametrize("text", ["Risultati quiz", "Evento aperto", "Classifica", "Notifica", "battuta fun"])
async def test_unregistered_bot_output_never_triggers_ai_typing_or_cooldown(session, monkeypatch, text):
    generate = AsyncMock()
    check = AsyncMock()
    monkeypatch.setattr(alduino_chat, "generate_reply", generate)
    monkeypatch.setattr(fun_ai, "_check_cooldown", check)
    target = SimpleNamespace(message_id=1234, text=text, caption=None, from_user=SimpleNamespace(id=Bot.id))
    message = Message(60, text="che partita!", reply_to=target)
    message.bot.send_chat_action = AsyncMock()
    with pytest.raises(fun_ai.SkipHandler):
        await fun_ai.reply_to_alduino(message, session)
    generate.assert_not_awaited()
    check.assert_not_awaited()
    message.bot.send_chat_action.assert_not_awaited()
    assert message.replies == [] and not session.in_transaction()


@pytest.mark.parametrize("failure", ["read", "commit"])
async def test_eligibility_read_failure_is_silent_and_fail_closed(session, monkeypatch, failure):
    if failure == "read":
        monkeypatch.setattr(alduino_chat, "find_parent", AsyncMock(side_effect=SQLAlchemyError("db down")))
    else:
        monkeypatch.setattr(session, "commit", AsyncMock(side_effect=SQLAlchemyError("db down")))
    generate = AsyncMock()
    monkeypatch.setattr(alduino_chat, "generate_reply", generate)
    message = Message(70, text="ciao", reply_to=SimpleNamespace(
        message_id=1234, text="Quiz", caption=None, from_user=SimpleNamespace(id=Bot.id),
    ))
    with pytest.raises(fun_ai.SkipHandler):
        await fun_ai.reply_to_alduino(message, session)
    generate.assert_not_awaited()
    assert message.replies == [] and cooldown.remaining("ai", 7, fun_ai.settings.ai_cooldown_seconds) == 0


async def test_explicit_alduino_can_still_discuss_an_unregistered_event(session, monkeypatch):
    monkeypatch.setattr(fun_ai, "is_admin", allow)
    monkeypatch.setattr(fun_ai, "_live_context", no_live)
    generate = AsyncMock(return_value=GeneratedReply("spiego il risultato", "groq"))
    monkeypatch.setattr(alduino_chat, "generate_reply", generate)
    message = Message(80, reply_to=SimpleNamespace(message_id=1234, text="Risultati quiz", caption=None))
    await fun_ai.cmd_alduino(message, SimpleNamespace(args="perché?"), session)
    assert generate.await_args.kwargs["quoted_bot_text"] == "Risultati quiz"
    assert generate.await_args.kwargs["current"] == "perché?"
    assert message.replies[0].text == "spiego il risultato"


async def test_fun_provider_failure_does_not_register_fallback_as_conversation(session, monkeypatch):
    monkeypatch.setattr(fun_ai, "is_admin", allow)
    monkeypatch.setattr(fun_ai.ai_service, "generate_completion", AsyncMock(side_effect=fun_ai.ai_service.AIServiceError("down")))
    message = Message(90, reply_to=SimpleNamespace(text="aneddoto", caption=None))
    await fun_ai.cmd_drama(message, session)
    assert message.replies[0].text == fun_ai.ai_service.AI_FALLBACK_MESSAGE
    assert (await session.execute(select(AlduinoTurn))).scalars().all() == []


async def test_message_id_from_another_group_is_not_an_eligible_target(session, monkeypatch):
    await alduino_chat.record_turn(session, group_id=-200, user_tg_id=7, user_message_id=1,
        bot_message_id=1234, parent=None, user_text="ciao", reply=GeneratedReply("fun", "fun"))
    await session.commit()
    generate = AsyncMock()
    monkeypatch.setattr(alduino_chat, "generate_reply", generate)
    message = Message(100, text="ciao", reply_to=SimpleNamespace(
        message_id=1234, text="fun", caption=None, from_user=SimpleNamespace(id=Bot.id),
    ))
    with pytest.raises(fun_ai.SkipHandler):
        await fun_ai.reply_to_alduino(message, session)
    generate.assert_not_awaited()
