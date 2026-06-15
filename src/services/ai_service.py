"""
AI entertainment service — async Groq (OpenAI-compatible) client.

All LLM traffic goes through `aiohttp` (never blocking libraries) so the
aiogram event loop is never stalled. Failures (timeout, network, non-200,
malformed body, missing API key) are normalised into `AIServiceError` so
handlers can fall back to `AI_FALLBACK_MESSAGE`.

No moderation fields are sent in the payload by design — only the system
and user prompts.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from config_data.config import settings

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
AI_FALLBACK_MESSAGE = "I server sono a fuoco, riprova dopo."

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_TEMPERATURE = 0.9
_DEFAULT_MAX_TOKENS = 300


class AIServiceError(Exception):
    """Raised on any failure while talking to the Groq API."""


async def generate_completion(
    system_prompt: str,
    user_text: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    *,
    temperature: float | None = None,
) -> str:
    """Send a system + user prompt to Groq and return the assistant text.

    `max_tokens` is a hard cap on the reply length (per-command) to avoid
    walls of text even if the model ignores the prompt instructions.

    `temperature` is per-command: ``None`` uses the default (`_TEMPERATURE`,
    high → varied/creative). A lower value (e.g. /dialetto) makes the model
    more conservative so it invents fewer non-existent words.

    Raises AIServiceError on missing API key, timeout, network error,
    non-200 status, or a malformed response body.
    """
    if not settings.groq_api_key:
        logger.error("GROQ_API_KEY non configurata — impossibile chiamare Groq.")
        raise AIServiceError("missing api key")

    payload = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": _TEMPERATURE if temperature is None else temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(GROQ_URL, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        "Groq ha risposto con status %s: %s", resp.status, body[:500]
                    )
                    raise AIServiceError(f"status {resp.status}")
                data = await resp.json()
    except asyncio.TimeoutError as exc:
        logger.warning("Timeout nella chiamata a Groq.")
        raise AIServiceError("timeout") from exc
    except aiohttp.ClientError as exc:
        logger.warning("Errore di rete nella chiamata a Groq: %s", exc)
        raise AIServiceError("network error") from exc

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        logger.error("Risposta Groq malformata: %s", data)
        raise AIServiceError("malformed response") from exc
