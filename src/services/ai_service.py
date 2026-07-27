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
import json
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


# ---------------------------------------------------------------------------
# The judge — deterministic, schema-constrained. It shares nothing with
# `generate_completion` on purpose: that one is a creative call whose worst
# failure is a flat joke, this one decides whether somebody gets paid.
# ---------------------------------------------------------------------------

#: Constrained-decoding schema. One boolean, no free-text field — there is
#: deliberately nothing in the reply that could carry a correct answer back to a
#: player who tried to talk the model into leaking it.
JUDGE_SCHEMA: dict = {
    "name": "verdetto",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"corretta": {"type": "boolean"}},
        "required": ["corretta"],
        "additionalProperties": False,
    },
}

_JUDGE_MAX_TOKENS = 20
_JUDGE_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_JUDGE_RETRY_DELAY = 1.0


async def judge_equivalence(system_prompt: str, user_text: str) -> bool:
    """Ask the judge model a single yes/no question and return its boolean.

    Raises `AIServiceError` on a missing key, a transport failure, an exhausted
    retry, or a body that is not exactly one boolean. There is deliberately no
    third value: the caller must treat a raise as "not proven correct", never as
    "correct".

    Retries **once** on 429/5xx because a rate limit is the expected failure on
    the Groq free tier and charging it to a player's attempt would be our bug
    billed to them. A 4xx other than 429 means the request itself is wrong —
    sending it again would only burn quota.
    """
    if not settings.groq_api_key:
        logger.error("GROQ_API_KEY non configurata — impossibile interpellare il giudice.")
        raise AIServiceError("missing api key")

    payload = {
        "model": settings.groq_judge_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0,
        "max_tokens": _JUDGE_MAX_TOKENS,
        "response_format": {"type": "json_schema", "json_schema": JUDGE_SCHEMA},
    }
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=settings.guess_judge_timeout_seconds)

    data: dict | None = None
    for attempt in (1, 2):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(GROQ_URL, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        break
                    body = await resp.text()
                    logger.warning("Giudice: status %s — %s", resp.status, body[:300])
                    if resp.status not in _JUDGE_RETRY_STATUSES or attempt == 2:
                        raise AIServiceError(f"status {resp.status}")
        except asyncio.TimeoutError as exc:
            logger.warning("Timeout nella chiamata al giudice (tentativo %s).", attempt)
            if attempt == 2:
                raise AIServiceError("timeout") from exc
        except aiohttp.ClientError as exc:
            logger.warning("Errore di rete verso il giudice: %s", exc)
            if attempt == 2:
                raise AIServiceError("network error") from exc
        await asyncio.sleep(_JUDGE_RETRY_DELAY)

    try:
        content = data["choices"][0]["message"]["content"]  # type: ignore[index]
        verdict = json.loads(content)["corretta"]
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        logger.error("Verdetto del giudice illeggibile: %s", data)
        raise AIServiceError("malformed verdict") from exc
    # `isinstance(True, int)` is True in Python, so an int would slip past a
    # bool() cast. Only a real boolean counts as a verdict.
    if not isinstance(verdict, bool):
        logger.error("Verdetto del giudice non booleano: %r", verdict)
        raise AIServiceError("non-boolean verdict")
    return verdict
