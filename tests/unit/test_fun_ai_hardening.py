"""Unit tests for the AI-handler hardening helpers (handlers/fun_ai)."""

from __future__ import annotations

import time
import types

from handlers import fun_ai
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
# _prune_cooldowns — bounded in-memory cooldown dict
# ---------------------------------------------------------------------------

def test_prune_noop_below_threshold():
    fun_ai._last_used.clear()
    now = time.monotonic()
    fun_ai._last_used[1] = now - 10_000  # expired but under threshold → kept
    fun_ai._prune_cooldowns(now)
    assert 1 in fun_ai._last_used
    fun_ai._last_used.clear()


def test_prune_drops_expired_keeps_recent():
    fun_ai._last_used.clear()
    now = time.monotonic()
    expired = now - fun_ai.settings.ai_cooldown_seconds - 100
    for i in range(fun_ai._COOLDOWN_PRUNE_THRESHOLD + 50):
        fun_ai._last_used[1000 + i] = expired
    fun_ai._last_used[42] = now  # recent
    fun_ai._prune_cooldowns(now)
    assert 42 in fun_ai._last_used
    assert 1000 not in fun_ai._last_used
    fun_ai._last_used.clear()


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
    fun_ai._last_used.clear()

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
    fun_ai._last_used.clear()


async def test_temperature_is_forwarded(monkeypatch):
    """Per-command temperature (e.g. /dialetto) reaches the Groq client."""
    captured: dict[str, object] = {}

    async def fake_completion(system_prompt, user_text, max_tokens, *, temperature=None):
        captured["temperature"] = temperature
        return "ok"

    monkeypatch.setattr(ai_service, "generate_completion", fake_completion)
    fun_ai._last_used.clear()

    msg = _StubMessage()
    await fun_ai._generate_and_reply(
        msg, "SYSTEM", "ciao", 100, temperature=fun_ai._DIALETTO_TEMPERATURE
    )

    assert captured["temperature"] == fun_ai._DIALETTO_TEMPERATURE
    assert fun_ai._DIALETTO_TEMPERATURE < ai_service._TEMPERATURE  # genuinely lower
    fun_ai._last_used.clear()


async def test_fallback_message_on_ai_error(monkeypatch):
    async def boom(system_prompt, user_text, max_tokens, *, temperature=None):
        raise ai_service.AIServiceError("down")

    monkeypatch.setattr(ai_service, "generate_completion", boom)
    fun_ai._last_used.clear()

    msg = _StubMessage()
    await fun_ai._generate_and_reply(msg, "SYSTEM", "ciao", 100)
    text, _kwargs = msg.replies[-1]
    assert text == ai_service.AI_FALLBACK_MESSAGE
    fun_ai._last_used.clear()
