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
