"""Schema-constrained AI provider boundary for persistent game strategies."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

import aiohttp

from config_data.config import settings

log = logging.getLogger(__name__)
_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class StructuredAIError(RuntimeError):
    pass


class StructuredAIProvider(Protocol):
    async def generate_json(
        self, *, system_prompt: str, user_prompt: str,
        schema: dict[str, Any], max_output_tokens: int = 256,
    ) -> dict[str, Any]: ...


class GeminiStructuredProvider:
    """Gemini REST adapter. Domain validation intentionally stays with strategies."""

    async def generate_json(
        self, *, system_prompt: str, user_prompt: str,
        schema: dict[str, Any], max_output_tokens: int = 256,
    ) -> dict[str, Any]:
        if not settings.gemini_api_key:
            raise StructuredAIError("missing api key")
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "thinkingConfig": {"thinkingLevel": settings.gemini_thinking_level},
            },
        }
        url = f"{_BASE_URL}/{settings.gemini_model}:generateContent"
        timeout = aiohttp.ClientTimeout(total=settings.gemini_timeout_seconds)
        data: dict[str, Any] | None = None
        for attempt in (1, 2):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as client:
                    async with client.post(
                        url,
                        headers={"x-goog-api-key": settings.gemini_api_key},
                        json=payload,
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            break
                        body = await response.text()
                        log.warning("Gemini structured status %s: %s", response.status, body[:300])
                        if response.status not in _RETRYABLE or attempt == 2:
                            raise StructuredAIError(f"status {response.status}")
            except asyncio.TimeoutError as exc:
                if attempt == 2:
                    raise StructuredAIError("timeout") from exc
            except aiohttp.ClientError as exc:
                if attempt == 2:
                    raise StructuredAIError("network error") from exc
            await asyncio.sleep(0.5)

        try:
            parts = data["candidates"][0]["content"]["parts"]  # type: ignore[index]
            text = "".join(
                part["text"] for part in parts
                if isinstance(part, dict) and not part.get("thought") and "text" in part
            )
            value = json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.error("Risposta Gemini strutturata illeggibile: %r", data)
            raise StructuredAIError("malformed response") from exc
        if not isinstance(value, dict):
            raise StructuredAIError("response is not an object")
        return value
