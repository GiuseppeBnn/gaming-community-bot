"""Unit tests for services/ai_service.py (aiohttp mocked via aioresponses)."""

from __future__ import annotations

import asyncio

import pytest
from aioresponses import aioresponses

from services import ai_service
from services.ai_service import AIServiceError, GROQ_URL, generate_completion


@pytest.fixture
def with_api_key(monkeypatch):
    monkeypatch.setattr(ai_service.settings, "groq_api_key", "test-key")


async def test_generate_completion_success(with_api_key):
    payload = {
        "choices": [{"message": {"content": "  Risposta dal modello.  "}}]
    }
    with aioresponses() as m:
        m.post(GROQ_URL, status=200, payload=payload)
        result = await generate_completion("system prompt", "user text")

        assert result == "Risposta dal modello."

        # Verify the outgoing payload: system + user prompt, NO moderation fields.
        request = next(iter(m.requests.values()))[0]
        sent = request.kwargs["json"]
        assert sent["messages"] == [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user text"},
        ]
        assert "moderation" not in sent
        assert all("moderat" not in key.lower() for key in sent)


async def test_default_temperature_in_payload(with_api_key):
    payload = {"choices": [{"message": {"content": "ok"}}]}
    with aioresponses() as m:
        m.post(GROQ_URL, status=200, payload=payload)
        await generate_completion("sys", "user")
        sent = next(iter(m.requests.values()))[0].kwargs["json"]
        assert sent["temperature"] == ai_service._TEMPERATURE


async def test_custom_temperature_overrides_default(with_api_key):
    payload = {"choices": [{"message": {"content": "ok"}}]}
    with aioresponses() as m:
        m.post(GROQ_URL, status=200, payload=payload)
        await generate_completion("sys", "user", temperature=0.5)
        sent = next(iter(m.requests.values()))[0].kwargs["json"]
        assert sent["temperature"] == 0.5


async def test_generate_completion_http_error(with_api_key):
    with aioresponses() as m:
        m.post(GROQ_URL, status=500, body="boom")
        with pytest.raises(AIServiceError):
            await generate_completion("sys", "user")


async def test_generate_completion_timeout(with_api_key):
    with aioresponses() as m:
        m.post(GROQ_URL, exception=asyncio.TimeoutError())
        with pytest.raises(AIServiceError):
            await generate_completion("sys", "user")


async def test_generate_completion_malformed_body(with_api_key):
    with aioresponses() as m:
        m.post(GROQ_URL, status=200, payload={"unexpected": "shape"})
        with pytest.raises(AIServiceError):
            await generate_completion("sys", "user")


async def test_generate_completion_missing_key(monkeypatch):
    monkeypatch.setattr(ai_service.settings, "groq_api_key", "")
    # No aioresponses context → if a network call were attempted it would error
    # differently; AIServiceError here proves we bail out before any request.
    with pytest.raises(AIServiceError):
        await generate_completion("sys", "user")


async def test_a_closed_think_block_never_reaches_the_caller(with_api_key):
    """qwen3.6 scrive il ragionamento dentro `content`: va tolto, non mostrato."""
    payload = {"choices": [{"message": {
        "content": "<think>Valuto il tono richiesto.</think>La risposta vera."
    }}]}
    with aioresponses() as m:
        m.post(GROQ_URL, status=200, payload=payload)

        assert await generate_completion("sys", "user") == "La risposta vera."


async def test_an_unterminated_think_block_is_stripped_too(with_api_key):
    """La forma pericolosa: `max_tokens` tronca il ragionamento PRIMA di `</think>`,
    quindi non c'è nessun tag di chiusura a cui appoggiarsi."""
    payload = {"choices": [{"message": {
        "content": "Ecco.\n<think>sto ancora ragionando e non ho finito"
    }}]}
    with aioresponses() as m:
        m.post(GROQ_URL, status=200, payload=payload)

        assert await generate_completion("sys", "user") == "Ecco."


async def test_a_reply_that_is_only_reasoning_is_an_error_not_an_empty_message(
    with_api_key,
):
    """Meglio il messaggio di fallback che una risposta vuota in chat."""
    payload = {"choices": [{"message": {"content": "<think>solo pensieri</think>"}}]}
    with aioresponses() as m:
        m.post(GROQ_URL, status=200, payload=payload)

        with pytest.raises(AIServiceError):
            await generate_completion("sys", "user")
