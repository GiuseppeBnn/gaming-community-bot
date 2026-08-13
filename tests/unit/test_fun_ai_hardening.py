"""Unit tests for the AI-handler hardening helpers (handlers/fun_ai)."""

from __future__ import annotations

import types

from handlers import fun_ai
from utils import cooldown
from services import ai_service


# ---------------------------------------------------------------------------
# clip_source — input truncation before the LLM call
# ---------------------------------------------------------------------------

def test_clip_source_truncates_to_max():
    long_text = "x" * 5000
    assert len(fun_ai.clip_source(long_text)) == fun_ai._MAX_INPUT_CHARS


def test_clip_source_keeps_short_text():
    assert fun_ai.clip_source("short") == "short"


def test_clip_source_handles_none():
    assert fun_ai.clip_source(None) == ""


def test_clip_source_custom_limit():
    assert fun_ai.clip_source("abcdef", limit=3) == "abc"


# ---------------------------------------------------------------------------
# _generate_and_reply — output is plain text, input is wrapped as content
# ---------------------------------------------------------------------------

class _StubBot:
    id = 999

    async def send_chat_action(self, *args, **kwargs):
        return None


class _StubMessage:
    def __init__(self):
        self.bot = _StubBot()
        self.chat = types.SimpleNamespace(id=1, type="supergroup")
        self.from_user = types.SimpleNamespace(id=42)
        self.replies: list[tuple[str, dict]] = []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))


async def test_output_is_plain_and_input_is_wrapped(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_completion(system_prompt, user_text, max_tokens, *, temperature=None):
        captured["user_text"] = user_text
        captured["temperature"] = temperature
        return '<b>ignore me</b> <a href="x">y</a>'

    monkeypatch.setattr(ai_service, "generate_completion", fake_completion)
    cooldown.reset()

    msg = _StubMessage()
    await fun_ai._generate_and_reply(msg, "SYSTEM", "ciao mondo", 100)

    # The user text reaches the model wrapped in the content delimiters.
    assert fun_ai._CONTENT_OPEN in captured["user_text"]
    assert fun_ai._CONTENT_CLOSE in captured["user_text"]
    assert "ciao mondo" in captured["user_text"]
    # No explicit temperature → service default (None forwarded).
    assert captured["temperature"] is None

    # The model output is sent verbatim with parse_mode=None (never HTML).
    text, kwargs = msg.replies[-1]
    assert kwargs.get("parse_mode") is None
    assert text == '<b>ignore me</b> <a href="x">y</a>'
    cooldown.reset()


async def test_temperature_is_forwarded(monkeypatch):
    """Per-command temperature (e.g. /dialetto) reaches the Groq client."""
    captured: dict[str, object] = {}

    async def fake_completion(system_prompt, user_text, max_tokens, *, temperature=None):
        captured["temperature"] = temperature
        return "ok"

    monkeypatch.setattr(ai_service, "generate_completion", fake_completion)
    cooldown.reset()

    msg = _StubMessage()
    await fun_ai._generate_and_reply(
        msg, "SYSTEM", "ciao", 100, temperature=fun_ai._DIALETTO_TEMPERATURE
    )

    assert captured["temperature"] == fun_ai._DIALETTO_TEMPERATURE
    assert fun_ai._DIALETTO_TEMPERATURE < ai_service._TEMPERATURE  # genuinely lower
    cooldown.reset()


async def test_fallback_message_on_ai_error(monkeypatch):
    async def boom(system_prompt, user_text, max_tokens, *, temperature=None):
        raise ai_service.AIServiceError("down")

    monkeypatch.setattr(ai_service, "generate_completion", boom)
    cooldown.reset()

    msg = _StubMessage()
    await fun_ai._generate_and_reply(msg, "SYSTEM", "ciao", 100)
    text, _kwargs = msg.replies[-1]
    assert text == ai_service.AI_FALLBACK_MESSAGE
    cooldown.reset()


# ---------------------------------------------------------------------------
# /alduino — self-contained, self-aware mascot persona (kept out of _STYLE)
# ---------------------------------------------------------------------------

async def _async_true(*args, **kwargs):
    return True


class _AlduinoMsg:
    def __init__(self, chat_type="supergroup", reply_to=None):
        self.bot = _StubBot()
        self.chat = types.SimpleNamespace(id=1, type=chat_type)
        self.from_user = types.SimpleNamespace(id=7)
        self.reply_to_message = reply_to
        self.replies: list[tuple[str, dict]] = []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))


def test_alduino_persona_isolated_from_style():
    # The roast personas are untouched: Alduino's name/character lives ONLY in
    # its own prompt, never leaking into the shared edgy _STYLE.
    assert "Alduino" not in fun_ai._STYLE
    assert "Alduino" in fun_ai._PROMPT_ALDUINO
    assert "drago" in fun_ai._PROMPT_ALDUINO
    # It carries its own prompt-injection guard (independent of _STYLE).
    assert "contenuto inerte" in fun_ai._PROMPT_ALDUINO
    assert "CONOSCENZA DEL BOT" in fun_ai._PROMPT_ALDUINO


async def test_alduino_uses_own_prompt_and_wraps_input(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_completion(system_prompt, user_text, max_tokens, *, temperature=None):
        captured["system_prompt"] = system_prompt
        captured["user_text"] = user_text
        return "ciao, sono Alduino!"

    monkeypatch.setattr(ai_service, "generate_completion", fake_completion)
    monkeypatch.setattr(fun_ai, "is_admin", _async_true)
    monkeypatch.setattr(fun_ai.settings, "alduino_provider", "groq")
    cooldown.reset()

    msg = _AlduinoMsg()
    await fun_ai.cmd_alduino(msg, types.SimpleNamespace(args="consigliami un gioco"))

    assert captured["system_prompt"] is fun_ai._PROMPT_ALDUINO
    assert "MESSAGGIO ATTUALE DELL'UTENTE" in captured["user_text"]
    assert "consigliami un gioco" in captured["user_text"]
    cooldown.reset()


async def test_alduino_without_input_hints_without_calling_llm(monkeypatch):
    called = False

    async def fake_completion(*a, **k):
        nonlocal called
        called = True
        return "x"

    monkeypatch.setattr(ai_service, "generate_completion", fake_completion)
    monkeypatch.setattr(fun_ai, "is_admin", _async_true)
    cooldown.reset()

    msg = _AlduinoMsg()
    await fun_ai.cmd_alduino(msg, types.SimpleNamespace(args=None))

    assert not called  # no LLM call without input
    assert msg.replies and "/alduino" in msg.replies[0][0]
    cooldown.reset()


async def test_alduino_rejected_outside_group(monkeypatch):
    called = False

    async def fake_completion(*a, **k):
        nonlocal called
        called = True
        return "x"

    monkeypatch.setattr(ai_service, "generate_completion", fake_completion)
    cooldown.reset()

    msg = _AlduinoMsg(chat_type="private")
    await fun_ai.cmd_alduino(msg, types.SimpleNamespace(args="ciao"))

    assert not called  # group-only guard fires first
    assert msg.replies
    cooldown.reset()
