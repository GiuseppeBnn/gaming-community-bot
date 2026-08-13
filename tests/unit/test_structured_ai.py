from __future__ import annotations

import pytest
import aiohttp
from aioresponses import aioresponses

from services import structured_ai
from services.structured_ai import GeminiStructuredProvider, StructuredAIError


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(structured_ai.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(structured_ai.settings, "gemini_model", "gemini-test")
    return GeminiStructuredProvider()


def _url():
    return f"{structured_ai._BASE_URL}/gemini-test:generateContent"


async def test_gemini_sends_schema_thinking_and_parses_only_non_thought_text(gemini):
    response = {"candidates": [{"content": {"parts": [
        {"thought": True, "text": "private reasoning"},
        {"text": '{"verdetto":"si"}'},
    ]}}]}
    schema = {"type": "object", "properties": {"verdetto": {"type": "string"}}}
    with aioresponses() as mocked:
        mocked.post(_url(), status=200, payload=response)
        result = await gemini.generate_json(
            system_prompt="system", user_prompt="user", schema=schema,
            thinking_level="minimal", temperature=0.7,
        )

        assert result == {"verdetto": "si"}
        request = next(iter(mocked.requests.values()))[0]
        sent = request.kwargs["json"]
        assert sent["generationConfig"]["responseJsonSchema"] == schema
        assert sent["generationConfig"]["responseMimeType"] == "application/json"
        assert sent["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "minimal"
        assert sent["generationConfig"]["temperature"] == 0.7
        assert request.kwargs["headers"]["x-goog-api-key"] == "test-key"


async def test_gemini_rejects_malformed_json(gemini):
    with aioresponses() as mocked:
        mocked.post(_url(), status=200, payload={
            "candidates": [{"content": {"parts": [{"text": "not-json"}]}}],
        })
        with pytest.raises(StructuredAIError, match="malformed"):
            await gemini.generate_json(system_prompt="s", user_prompt="u", schema={})


async def test_max_tokens_has_safe_diagnostics_without_model_material(gemini, caplog):
    signature = "secret-thought-signature-that-must-never-be-logged"
    with aioresponses() as mocked:
        mocked.post(_url(), status=200, payload={
            "modelVersion": "gemini-test",
            "candidates": [{
                "finishReason": "MAX_TOKENS",
                "content": {"parts": [{"thoughtSignature": signature, "text": "partial"}]},
            }],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 3,
                "thoughtsTokenCount": 241,
            },
        })
        with pytest.raises(StructuredAIError, match="max tokens"):
            await gemini.generate_json(system_prompt="s", user_prompt="u", schema={})

    assert signature not in caplog.text
    assert "partial" not in caplog.text
    assert "241" in caplog.text


async def test_gemini_missing_key_fails_before_network(monkeypatch):
    monkeypatch.setattr(structured_ai.settings, "gemini_api_key", "")
    with pytest.raises(StructuredAIError, match="missing"):
        await GeminiStructuredProvider().generate_json(
            system_prompt="s", user_prompt="u", schema={},
        )


async def test_non_retryable_status_and_non_object_are_rejected(gemini):
    with aioresponses() as mocked:
        mocked.post(_url(), status=400, body="bad request")
        with pytest.raises(StructuredAIError, match="status 400"):
            await gemini.generate_json(system_prompt="s", user_prompt="u", schema={})
    with aioresponses() as mocked:
        mocked.post(_url(), status=200, payload={
            "candidates": [{"content": {"parts": [{"text": "[]"}]}}],
        })
        with pytest.raises(StructuredAIError, match="not an object"):
            await gemini.generate_json(system_prompt="s", user_prompt="u", schema={})


async def test_retryable_status_recovers_once(gemini, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(structured_ai.asyncio, "sleep", no_sleep)
    with aioresponses() as mocked:
        mocked.post(_url(), status=503, body="busy")
        mocked.post(_url(), status=200, payload={
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
        })
        assert await gemini.generate_json(system_prompt="s", user_prompt="u", schema={}) == {}


@pytest.mark.parametrize("failure", [TimeoutError(), aiohttp.ClientConnectionError("down")])
async def test_transport_failure_is_normalized_after_retry(gemini, monkeypatch, failure):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(structured_ai.asyncio, "sleep", no_sleep)
    with aioresponses() as mocked:
        mocked.post(_url(), exception=failure)
        mocked.post(_url(), exception=failure)
        with pytest.raises(StructuredAIError):
            await gemini.generate_json(system_prompt="s", user_prompt="u", schema={})
