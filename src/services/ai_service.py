"""
AI entertainment service — async Groq (OpenAI-compatible) client.

All LLM traffic goes through `aiohttp` (never blocking libraries) so the
aiogram event loop is never stalled. Failures (timeout, network, non-200,
malformed body, missing API key, empty reply once reasoning is stripped) are
normalised into `AIServiceError` so handlers can fall back to
`AI_FALLBACK_MESSAGE`.

No moderation fields are sent in the payload by design — only the system
and user prompts.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import logging
import re
from typing import Any

import aiohttp

from config_data.config import settings
from services import ai_budget

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_PROVIDER = "openrouter"
AI_FALLBACK_MESSAGE = "I server sono a fuoco, riprova dopo."

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_TEMPERATURE = 0.9
_DEFAULT_MAX_TOKENS = 300

#: A hybrid-reasoning model writes its chain of thought INSIDE `content` when the
#: reasoning switch is off — unlike `openai/gpt-oss-*`, which puts it in a field
#: of its own. Groq ignores parameters a model does not support *silently*, so a
#: flag that stops working raises nothing: it publishes the model's thinking to
#: the group. `(?:</think>|$)` also catches the block TRUNCATED by `max_tokens`,
#: which is the dangerous shape — pure reasoning with no closing tag.
_THINK_BLOCK = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL | re.IGNORECASE)


class AIServiceError(Exception):
    """Normalized failure from a configured AI provider."""


def parse_model_list(raw: str) -> tuple[str, ...]:
    """Normalize an ordered CSV fallback list and reject an empty route."""
    models = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not models:
        raise AIServiceError("empty model route")
    return models


def _usage_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _openrouter_usage(data: Any) -> tuple[ai_budget.UsageMetrics, int | None]:
    if not isinstance(data, dict):
        return ai_budget.UsageMetrics(), None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    model = data.get("model")
    metrics = ai_budget.UsageMetrics(
        actual_model=model if isinstance(model, str) else None,
        prompt_tokens=_usage_int(usage.get("prompt_tokens")),
        completion_tokens=_usage_int(usage.get("completion_tokens")),
        reasoning_tokens=_usage_int(
            usage.get("reasoning_tokens", completion_details.get("reasoning_tokens"))
        ),
        cached_tokens=_usage_int(prompt_details.get("cached_tokens")),
    )
    try:
        cost = Decimal(str(usage["cost"]))
        if not cost.is_finite() or cost < 0:
            raise InvalidOperation
    except (KeyError, InvalidOperation, ValueError):
        return metrics, None
    microusd = int((cost * Decimal("1000000")).to_integral_value(rounding=ROUND_CEILING))
    return metrics, microusd


def _openrouter_provider_policy(
    *, require_zdr: bool, allow_fallbacks: bool,
) -> dict[str, Any]:
    """Build the shared privacy, routing and price ceiling policy."""
    provider: dict[str, Any] = {
        "allow_fallbacks": allow_fallbacks,
        "require_parameters": True,
        "data_collection": "deny",
        "sort": "price",
        "max_price": {
            "prompt": float(settings.openrouter_max_prompt_price),
            "completion": float(settings.openrouter_max_completion_price),
        },
    }
    if require_zdr:
        provider["zdr"] = True
    return provider


async def _settle_openrouter(
    reservation: ai_budget.Reservation,
    *,
    status: str,
    actual_microusd: int | None,
    metrics: ai_budget.UsageMetrics | None = None,
) -> None:
    try:
        await ai_budget.settle(
            reservation,
            status=status,
            actual_microusd=actual_microusd,
            metrics=metrics or ai_budget.UsageMetrics(),
        )
    except ai_budget.AIBudgetError:
        # The budget module deliberately keeps the reservation charged when
        # settlement fails. The already-generated answer is still safe to use.
        logger.exception("Contabilità OpenRouter non chiusa; prenotazione mantenuta.")


async def generate_openrouter_completion(
    system_prompt: str,
    user_text: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    *,
    temperature: float | None = None,
    feature: str,
    models: tuple[str, ...],
    require_zdr: bool,
) -> str:
    """Call an ordered OpenRouter route with privacy, price and budget guards."""
    if not settings.openrouter_api_key:
        logger.error("OPENROUTER_API_KEY non configurata.")
        raise AIServiceError("missing OpenRouter api key")
    if not models:
        raise AIServiceError("empty model route")
    try:
        reservation = await ai_budget.reserve(
            feature=feature,
            provider=OPENROUTER_PROVIDER,
            requested_model=models[0],
            system_prompt=system_prompt,
            user_text=user_text,
            max_output_tokens=max_tokens,
        )
    except ai_budget.AIBudgetExceeded as exc:
        logger.warning("Budget AI mensile esaurito: blocco %s prima della rete.", feature)
        raise AIServiceError("monthly budget exhausted") from exc
    except ai_budget.AIBudgetError as exc:
        logger.exception("Budget AI non verificabile: chiamata paid bloccata.")
        raise AIServiceError("budget unavailable") from exc

    payload: dict[str, Any] = {
        "models": list(models),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": _TEMPERATURE if temperature is None else temperature,
        "max_tokens": max_tokens,
        "reasoning": {"effort": "none", "exclude": True},
        "provider": _openrouter_provider_policy(
            require_zdr=require_zdr, allow_fallbacks=True,
        ),
        "usage": {"include": True},
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "X-Title": settings.openrouter_app_name,
    }
    timeout = aiohttp.ClientTimeout(total=settings.openrouter_timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                settings.openrouter_url, headers=headers, json=payload,
            ) as response:
                if response.status != 200:
                    await response.read()
                    await _settle_openrouter(
                        reservation, status="failed", actual_microusd=0,
                    )
                    logger.warning(
                        "OpenRouter %s: status %s (model=%s).",
                        feature, response.status, models[0],
                    )
                    raise AIServiceError(f"status {response.status}")
                data = await response.json()
    except asyncio.CancelledError:
        # Graceful shutdown can cancel a request after the provider accepted it.
        # Charge the conservative reservation, then preserve cancellation.
        await _settle_openrouter(
            reservation, status="uncertain", actual_microusd=None,
        )
        raise
    except asyncio.TimeoutError as exc:
        await _settle_openrouter(
            reservation, status="uncertain", actual_microusd=None,
        )
        raise AIServiceError("OpenRouter timeout") from exc
    except aiohttp.ClientError as exc:
        await _settle_openrouter(
            reservation, status="uncertain", actual_microusd=None,
        )
        raise AIServiceError("OpenRouter network error") from exc

    metrics, actual_microusd = _openrouter_usage(data)
    try:
        content = data["choices"][0]["message"]["content"]
        text = _THINK_BLOCK.sub("", content).strip()
        if not text:
            raise ValueError("empty")
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        await _settle_openrouter(
            reservation,
            status="malformed",
            actual_microusd=actual_microusd,
            metrics=metrics,
        )
        raise AIServiceError("malformed OpenRouter response") from exc
    await _settle_openrouter(
        reservation,
        status="completed",
        actual_microusd=actual_microusd,
        metrics=metrics,
    )
    return text


async def generate_groq_completion(
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
    non-200 status, a malformed response body, or a reply that is empty
    once the model's reasoning block has been stripped out.
    """
    if not settings.groq_api_key:
        logger.error("GROQ_API_KEY non configurata — impossibile chiamare Groq.")
        raise AIServiceError("missing api key")

    payload: dict = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": _TEMPERATURE if temperature is None else temperature,
        "max_tokens": max_tokens,
    }
    # Only when set: it is model-specific, and a model that does not take it
    # would answer 400. Emptying the variable is the escape hatch.
    if settings.groq_reasoning_effort:
        payload["reasoning_effort"] = settings.groq_reasoning_effort
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(GROQ_URL, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    await resp.read()
                    logger.error(
                        "Groq ha risposto con status %s (model=%s).",
                        resp.status,
                        settings.groq_model,
                    )
                    raise AIServiceError(f"status {resp.status}")
                data = await resp.json()
    except asyncio.TimeoutError as exc:
        logger.warning("Timeout nella chiamata a Groq.")
        raise AIServiceError("timeout") from exc
    except aiohttp.ClientError as exc:
        logger.warning("Errore di rete nella chiamata a Groq (model=%s).", settings.groq_model)
        raise AIServiceError("network error") from exc

    try:
        content = data["choices"][0]["message"]["content"]
        text = _THINK_BLOCK.sub("", content).strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        logger.error("Risposta Groq malformata (model=%s).", settings.groq_model)
        raise AIServiceError("malformed response") from exc

    # Everything the model produced was reasoning. An empty reply in the group
    # reads as a broken bot; the fallback message at least says what happened.
    if not text:
        logger.error(
            "Risposta AI vuota dopo aver tolto il ragionamento (modello=%s). "
            "Il modello sta ragionando in chiaro: controlla GROQ_REASONING_EFFORT.",
            settings.groq_model,
        )
        raise AIServiceError("empty completion")
    return text


async def generate_completion(
    system_prompt: str,
    user_text: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    *,
    temperature: float | None = None,
) -> str:
    """Route one-shot entertainment without affecting the strict judge lane."""
    if settings.ai_entertainment_provider == "openrouter":
        return await generate_openrouter_completion(
            system_prompt,
            user_text,
            max_tokens,
            temperature=temperature,
            feature="entertainment",
            models=parse_model_list(settings.openrouter_fun_models),
            require_zdr=False,
        )
    return await generate_groq_completion(
        system_prompt, user_text, max_tokens, temperature=temperature,
    )


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

#: The judge model reasons before it answers, and on `openai/gpt-oss-*` those
#: reasoning tokens are drawn from THIS budget. Sizing it for the answer alone
#: (`{"corretta": true}` is ~10 tokens) leaves the content channel empty, Groq
#: validates that empty generation against the strict schema, and every call comes
#: back `400 json_validate_failed` — which is correctly not retried, so every
#: answer that reached the model was `unverified`. That was the production bug.
_JUDGE_MAX_TOKENS = 512
#: Short chains, not no chains: the question is binary and about two short
#: strings, and every reasoning token is latency a player waits through.
_JUDGE_REASONING_EFFORT = "low"
_JUDGE_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_JUDGE_RETRY_DELAY = 1.0


def _log_judge_http_failure(status: int, body: str) -> None:
    """Say what actually went wrong, at the level it deserves.

    `json_validate_failed` with an EMPTY `failed_generation` has exactly one
    cause: the token budget ran out inside the model's reasoning, so nothing
    reached the content channel and Groq validated an empty string against the
    strict schema. That diagnosis cost real time to make once, from logs that
    only said "status 400"; it must not cost it twice.

    This is for whoever reads the logs. The caller still gets a plain
    `AIServiceError`, because for the game "I have no verdict" is one thing and
    splitting it would only add a branch nothing acts on differently.
    """
    try:
        error = json.loads(body).get("error", {})
    except (ValueError, TypeError, AttributeError):
        error = {}

    if error.get("code") == "json_validate_failed" and not error.get("failed_generation"):
        logger.error(
            "Giudice: generazione VUOTA rifiutata dallo schema (modello=%s, "
            "max_tokens=%s). Il budget di token non basta per il reasoning del "
            "modello: alzalo, oppure passa a un modello non-reasoning.",
            settings.groq_judge_model, _JUDGE_MAX_TOKENS,
        )
        return
    logger.warning("Giudice: status %s (model=%s).", status, settings.groq_judge_model)


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
        "reasoning_effort": _JUDGE_REASONING_EFFORT,
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
                    _log_judge_http_failure(resp.status, body)
                    if resp.status not in _JUDGE_RETRY_STATUSES or attempt == 2:
                        raise AIServiceError(f"status {resp.status}")
        except asyncio.TimeoutError as exc:
            logger.warning("Timeout nella chiamata al giudice (tentativo %s).", attempt)
            if attempt == 2:
                raise AIServiceError("timeout") from exc
        except aiohttp.ClientError as exc:
            logger.warning(
                "Errore di rete verso il giudice (model=%s).",
                settings.groq_judge_model,
            )
            if attempt == 2:
                raise AIServiceError("network error") from exc
        await asyncio.sleep(_JUDGE_RETRY_DELAY)

    try:
        content = data["choices"][0]["message"]["content"]  # type: ignore[index]
        verdict = json.loads(content)["corretta"]
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        logger.error(
            "Verdetto del giudice illeggibile (model=%s).",
            settings.groq_judge_model,
        )
        raise AIServiceError("malformed verdict") from exc
    # `isinstance(True, int)` is True in Python, so an int would slip past a
    # bool() cast. Only a real boolean counts as a verdict.
    if not isinstance(verdict, bool):
        logger.error(
            "Verdetto del giudice non booleano (model=%s).",
            settings.groq_judge_model,
        )
        raise AIServiceError("non-boolean verdict")
    return verdict
