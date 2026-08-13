from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from database.models import AlduinoTurn
from handlers import fun_ai
from services import alduino_chat
from services.alduino_chat import GeneratedReply
from services.public_event import PublicEvent
from utils import cooldown


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
