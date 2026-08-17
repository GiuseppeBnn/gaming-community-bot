"""The AI commands themselves — `handlers/fun_ai.py`, at 71%.

The hardening helpers (`clip_source`, the content wrapping, the plain-text reply)
are pinned in `test_fun_ai_hardening.py`. What was untested is the guard chain in
front of them, which is where the design decision lives:

**the cooldown is checked, then the input is validated, and only a real call marks
it.** So a `/insulta` with nobody tagged, or a `/maestro` on a photo with no
caption, costs the user nothing and can be retried immediately — instead of burning
the whole AI cooldown on a typo. Each test below asserts both halves: the reply the
user gets, and that the model was never called.

The other property worth holding is that eight commands share one body: each passes
its own prompt and its own token cap, and `/dialetto` additionally lowers the
temperature. A copy-paste that sends `/drama`'s prompt with `/accusa`'s cap would
be invisible in production — the bot would simply answer slightly wrong, forever.
"""

from __future__ import annotations

import types

import pytest

import filters.admin_filter as admin_filter
from handlers import fun_ai
from services import ai_service
from utils import cooldown

USER_ID = 42


class _StubBot:
    id = 999

    def __init__(self) -> None:
        self.actions: list[tuple] = []

    async def send_chat_action(self, *args, **kwargs):
        self.actions.append((args, kwargs))

    async def get_chat_administrators(self, chat_id):
        raise RuntimeError("no telegram in tests")


class _StubMessage:
    def __init__(self, *, chat_type: str = "supergroup", reply_to=None,
                 user_id: int = USER_ID, text: str | None = None,
                 caption: str | None = None, user_is_bot: bool = False,
                 message_id: int = 500) -> None:
        self.bot = _StubBot()
        self.chat = types.SimpleNamespace(id=-100_123, type=chat_type)
        self.from_user = types.SimpleNamespace(id=user_id, username="tizio",
                                               full_name="Tizio Test",
                                               is_bot=user_is_bot)
        self.reply_to_message = reply_to
        self.text = text
        self.caption = caption
        self.message_id = message_id
        self.replies: list[str] = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return types.SimpleNamespace(message_id=900 + len(self.replies))

    @property
    def said(self) -> str:
        return "\n".join(self.replies)


def _replied(text: str | None = "ciao", *, caption: str | None = None,
             author_username: str | None = "vittima",
             author_name: str = "La Vittima", author_id: int = 99,
             message_id: int = 400):
    return types.SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=caption,
        from_user=types.SimpleNamespace(id=author_id, username=author_username,
                                        full_name=author_name),
    )


def _command(args: str | None = None):
    return types.SimpleNamespace(command="insulta", args=args)


@pytest.fixture
def llm(monkeypatch):
    """Records every call to the model; the tests assert on *whether* it was called
    as much as on what with."""
    calls: list[dict] = []

    async def _fake(system_prompt, user_text, max_tokens, *, temperature=None):
        calls.append({"prompt": system_prompt, "text": user_text,
                      "max_tokens": max_tokens, "temperature": temperature})
        return "risposta del modello"

    monkeypatch.setattr(ai_service, "generate_groq_completion", _fake)
    return calls


@pytest.fixture(autouse=True)
def _no_admin_no_cooldown(monkeypatch):
    """Nobody is an admin (admins bypass the cooldown), and no cooldown leaks in."""
    monkeypatch.setattr(admin_filter.settings, "admin_ids", [])
    monkeypatch.setattr(fun_ai.settings, "alduino_provider", "groq")
    admin_filter._cache.clear()
    cooldown.reset()
    yield
    admin_filter._cache.clear()
    cooldown.reset()


# ---------------------------------------------------------------------------
# The shared guard chain
# ---------------------------------------------------------------------------

class TestGuards:
    async def test_outside_a_group_it_refuses(self, llm):
        """These commands roast the person you reply to; in private there is nobody
        to reply to, and the prompt would be pointed at the caller."""
        message = _StubMessage(chat_type="private", reply_to=_replied())

        await fun_ai.cmd_maestro(message)

        assert "nel gruppo" in message.said
        assert llm == []

    async def test_without_a_reply_it_explains_and_costs_nothing(self, llm):
        message = _StubMessage(reply_to=None)

        await fun_ai.cmd_maestro(message)

        assert "rispondere al messaggio" in message.said.lower()
        assert llm == []
        assert cooldown.remaining(fun_ai._AI_BUCKET, USER_ID, 999) == 0, \
            "a refused command must not burn the cooldown"

    async def test_a_message_with_no_text_at_all_is_refused(self, llm):
        """A sticker or a photo without a caption: there is nothing to send."""
        message = _StubMessage(reply_to=_replied(None))

        await fun_ai.cmd_maestro(message)

        assert "non contiene testo" in message.said
        assert llm == []

    async def test_a_photo_caption_counts_as_text(self, llm):
        message = _StubMessage(reply_to=_replied(None, caption="guarda qua"))

        await fun_ai.cmd_maestro(message)

        assert len(llm) == 1 and "guarda qua" in llm[0]["text"]

    async def test_the_second_command_within_the_window_is_refused(self, llm):
        first = _StubMessage(reply_to=_replied())
        second = _StubMessage(reply_to=_replied())

        await fun_ai.cmd_maestro(first)
        await fun_ai.cmd_maestro(second)

        assert len(llm) == 1
        assert "Aspetta" in second.said

    async def test_a_successful_command_shows_the_typing_action(self, llm):
        """The model takes seconds; without it the group sees nothing happening."""
        message = _StubMessage(reply_to=_replied())

        await fun_ai.cmd_maestro(message)

        assert message.bot.actions


# ---------------------------------------------------------------------------
# One body, eight commands
# ---------------------------------------------------------------------------

class TestCommandWiring:
    @pytest.mark.parametrize("handler,prompt_name,max_tokens", [
        ("cmd_maestro", "_PROMPT_MAESTRO", 160),
        ("cmd_complotto", "_PROMPT_COMPLOTTO", 200),
        ("cmd_difendi", "_PROMPT_DIFENDI", 220),
        ("cmd_accusa", "_PROMPT_ACCUSA", 170),
        ("cmd_drama", "_PROMPT_DRAMA", 260),
        ("cmd_dialetto", "_PROMPT_DIALETTO", 240),
    ])
    async def test_each_command_sends_its_own_prompt_and_cap(
        self, llm, handler, prompt_name, max_tokens
    ):
        """A mix-up here is invisible in production: the bot answers, just wrongly."""
        message = _StubMessage(reply_to=_replied())

        await getattr(fun_ai, handler)(message)

        assert llm[0]["prompt"] == getattr(fun_ai, prompt_name)
        assert llm[0]["max_tokens"] == max_tokens

    async def test_only_dialetto_lowers_the_temperature(self, llm):
        """Catanese at the default temperature comes back as invented words."""
        await fun_ai.cmd_dialetto(_StubMessage(reply_to=_replied()))
        cooldown.reset()
        await fun_ai.cmd_maestro(_StubMessage(reply_to=_replied()))

        assert llm[0]["temperature"] == fun_ai._DIALETTO_TEMPERATURE
        assert llm[1]["temperature"] is None


# ---------------------------------------------------------------------------
# /insulta — the one with a target instead of a source
# ---------------------------------------------------------------------------

class TestInsulta:
    async def test_the_tagged_user_is_the_target(self, llm):
        message = _StubMessage()

        await fun_ai.cmd_insulta(message, _command("@vittima"))

        assert "@vittima" in llm[0]["text"]

    async def test_with_no_argument_it_falls_back_to_the_replied_author(self, llm):
        message = _StubMessage(reply_to=_replied())

        await fun_ai.cmd_insulta(message, _command(None))

        assert "@vittima" in llm[0]["text"]

    async def test_an_author_without_a_username_is_named_instead(self, llm):
        """Otherwise the command silently does nothing for anyone who never set a
        Telegram username."""
        message = _StubMessage(reply_to=_replied(author_username=None))

        await fun_ai.cmd_insulta(message, _command(None))

        assert "La Vittima" in llm[0]["text"]

    async def test_the_target_is_clipped(self, llm):
        """The target goes into the prompt; an unbounded one would push the actual
        instructions out of the model's window."""
        message = _StubMessage()

        await fun_ai.cmd_insulta(message, _command("@" + "x" * 500))

        assert len(llm[0]["text"]) < 500

    async def test_with_no_target_at_all_it_hints_and_costs_nothing(self, llm):
        message = _StubMessage(reply_to=None)

        await fun_ai.cmd_insulta(message, _command(None))

        assert "Tagga qualcuno" in message.said
        assert llm == []
        assert cooldown.remaining(fun_ai._AI_BUCKET, USER_ID, 999) == 0

    async def test_outside_a_group_it_refuses(self, llm):
        message = _StubMessage(chat_type="private")

        await fun_ai.cmd_insulta(message, _command("@vittima"))

        assert "nel gruppo" in message.said and llm == []

    async def test_the_cooldown_applies_to_it_too(self, llm):
        await fun_ai.cmd_insulta(_StubMessage(), _command("@vittima"))
        second = _StubMessage()

        await fun_ai.cmd_insulta(second, _command("@vittima"))

        assert len(llm) == 1 and "Aspetta" in second.said


# ---------------------------------------------------------------------------
# /alduino — the mascot, whose input is what the user typed
# ---------------------------------------------------------------------------

class TestAlduino:
    async def test_the_typed_text_is_the_input(self, llm):
        message = _StubMessage()

        await fun_ai.cmd_alduino(message, types.SimpleNamespace(args="come butta?"))

        assert "come butta?" in llm[0]["text"]

    async def test_with_no_argument_it_falls_back_to_the_replied_message(self, llm):
        message = _StubMessage(reply_to=_replied("qualcuno ha detto questo"))

        await fun_ai.cmd_alduino(message, types.SimpleNamespace(args=None))

        assert "qualcuno ha detto questo" in llm[0]["text"]

    async def test_the_fallback_reads_a_caption_too(self, llm):
        message = _StubMessage(reply_to=_replied(None, caption="foto con didascalia"))

        await fun_ai.cmd_alduino(message, types.SimpleNamespace(args=None))

        assert "foto con didascalia" in llm[0]["text"]

    async def test_replying_to_something_empty_is_treated_as_no_input(self, llm):
        message = _StubMessage(reply_to=_replied(None))

        await fun_ai.cmd_alduino(message, types.SimpleNamespace(args=None))

        assert "Scrivimi qualcosa" in message.said and llm == []

    async def test_the_cooldown_applies_to_it_too(self, llm):
        await fun_ai.cmd_alduino(_StubMessage(), types.SimpleNamespace(args="ciao"))
        second = _StubMessage()

        await fun_ai.cmd_alduino(second, types.SimpleNamespace(args="ciao"))

        assert len(llm) == 1 and "Aspetta" in second.said


class TestNaturalAlduinoReplies:
    async def test_reply_to_bot_continues_without_command_and_includes_context(self, llm):
        message = _StubMessage(
            text="perché?",
            reply_to=_replied("Ti consiglio Hades.", author_id=_StubBot.id),
        )

        await fun_ai.reply_to_alduino(message)

        assert len(llm) == 1
        assert "Ti consiglio Hades." in llm[0]["text"]
        assert "perché?" in llm[0]["text"]
        assert "MESSAGGIO DEL BOT A CUI RISPONDE" in llm[0]["text"]

    async def test_caption_is_valid_user_text(self, llm):
        message = _StubMessage(
            caption="che ne pensi?",
            reply_to=_replied("Mandami pure la foto.", author_id=_StubBot.id),
        )

        await fun_ai.reply_to_alduino(message)

        assert len(llm) == 1 and "che ne pensi?" in llm[0]["text"]

    @pytest.mark.parametrize("shape", ["other_user", "command", "media_only", "bot"])
    async def test_irrelevant_replies_are_skipped(self, llm, shape):
        kwargs = {
            "text": "ciao",
            "reply_to": _replied("test", author_id=_StubBot.id),
        }
        if shape == "other_user":
            kwargs["reply_to"] = _replied("test", author_id=123)
        elif shape == "command":
            kwargs["text"] = "/daily"
        elif shape == "media_only":
            kwargs["text"] = None
        elif shape == "bot":
            kwargs["user_is_bot"] = True
        message = _StubMessage(**kwargs)

        with pytest.raises(fun_ai.SkipHandler):
            await fun_ai.reply_to_alduino(message)

        assert llm == []

    async def test_natural_replies_share_the_ai_cooldown(self, llm):
        target = _replied("Ciao.", author_id=_StubBot.id)
        await fun_ai.reply_to_alduino(_StubMessage(text="uno", reply_to=target))
        second = _StubMessage(text="due", reply_to=target)

        await fun_ai.reply_to_alduino(second)

        assert len(llm) == 1 and "Aspetta" in second.said
