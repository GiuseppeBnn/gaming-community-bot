"""
AI gateway — OpenRouter runtime with budgeted text and strict structured output.

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
from decimal import Decimal, DecimalException, ROUND_CEILING
import logging
import re
from typing import Any, Literal

import aiohttp

from config_data.config import settings
from services import ai_budget
from services.ai_routing import ProviderAttempt, ProviderUnavailable, text_router

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_PROVIDER = "openrouter"
AI_FALLBACK_MESSAGE = "I server sono a fuoco, riprova dopo."

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_TEMPERATURE = 0.9
_DEFAULT_MAX_TOKENS = 300
_MAX_DB_INTEGER = 2_147_483_647
_MAX_DB_BIGINT = 9_223_372_036_854_775_807

#: A hybrid-reasoning model writes its chain of thought INSIDE `content` when the
#: reasoning switch is off — unlike `openai/gpt-oss-*`, which puts it in a field
#: of its own. Groq ignores parameters a model does not support *silently*, so a
#: flag that stops working raises nothing: it publishes the model's thinking to
#: the group. `(?:</think>|$)` also catches the block TRUNCATED by `max_tokens`,
#: which is the dangerous shape — pure reasoning with no closing tag.
_THINK_BLOCK = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL | re.IGNORECASE)


class AIServiceError(ProviderUnavailable):
    """Normalized failure from a configured AI provider."""


def parse_model_list(raw: str) -> tuple[str, ...]:
    """Normalize an ordered CSV fallback list and reject an empty route."""
    models = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not models:
        raise AIServiceError("empty model route")
    return models


def gemini_generation_policy(model: str, level: str, answer_tokens: int) -> tuple[str, int]:
    """Separate visible output from thinking on models without minimal effort."""
    if model.startswith(("gemini-3.7-", "gemini-3.8-", "gemini-4-")):
        return ("low" if level == "minimal" else level), answer_tokens + 1024
    return level, answer_tokens


def openrouter_generation_policy(
    models: tuple[str, ...], max_answer_tokens: int,
    *, profile: Literal["default", "chat"] = "default",
) -> tuple[dict[str, Any], int]:
    """Keep mandatory-thinking GLM/GPT-OSS separate from non-thinking routes."""
    mandatory = [
        model in {"z-ai/glm-5.3-flash", "z-ai/glm-5.3"}
        or model.startswith("openai/gpt-oss-")
        for model in models
    ]
    if any(mandatory):
        if not all(mandatory):
            raise AIServiceError("mixed reasoning policies in model route")
        effort = settings.openrouter_chat_reasoning_effort if profile == "chat" else "low"
        allowance = (
            settings.openrouter_chat_reasoning_token_allowance
            if profile == "chat" else settings.openrouter_reasoning_token_allowance
        )
        return {"effort": effort, "exclude": True}, max_answer_tokens + allowance
    return {"effort": "none", "exclude": True}, max_answer_tokens


def _usage_int(value: Any) -> int | None:
    return (
        value
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= _MAX_DB_INTEGER
        )
        else None
    )


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
            return metrics, None
        microusd = int(
            (cost * Decimal("1000000")).to_integral_value(rounding=ROUND_CEILING),
        )
    except (KeyError, DecimalException, OverflowError, ValueError):
        return metrics, None
    return metrics, microusd if microusd <= _MAX_DB_BIGINT else None


def _openrouter_provider_policy(
    *, require_zdr: bool, allow_fallbacks: bool,
) -> dict[str, Any]:
    """Build the shared privacy, routing and price ceiling policy."""
    provider: dict[str, Any] = {
        "allow_fallbacks": allow_fallbacks,
        "require_parameters": True,
        "data_collection": "deny",
        # Keep OpenRouter's adaptive price/uptime load balancing. An explicit
        # sort would disable it; max_price below remains a hard ceiling.
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
    reasoning_profile: Literal["default", "chat"] | None = None,
) -> str:
    """Call an ordered OpenRouter route with privacy, price and budget guards."""
    if not settings.openrouter_api_key:
        logger.error("OPENROUTER_API_KEY non configurata.")
        raise AIServiceError("missing OpenRouter api key")
    if not models:
        raise AIServiceError("empty model route")
    reasoning, total_tokens = openrouter_generation_policy(
        models, max_tokens,
        profile=reasoning_profile or ("chat" if feature == "alduino_chat" else "default"),
    )
    try:
        reservation = await ai_budget.reserve(
            feature=feature,
            provider=OPENROUTER_PROVIDER,
            requested_model=models[0],
            system_prompt=system_prompt,
            user_text=user_text,
            max_output_tokens=total_tokens,
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
        "max_tokens": total_tokens,
        "reasoning": reasoning,
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
                    raise AIServiceError(f"status {response.status}", status=response.status)
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
    except ValueError as exc:
        await _settle_openrouter(
            reservation, status="malformed", actual_microusd=None,
        )
        raise AIServiceError("malformed OpenRouter response") from exc

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
    """Offline eval adapter only; never selected by the bot's runtime gateways.

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
                    logger.log(
                        logging.INFO if resp.status in {429, 500, 502, 503, 504} else logging.ERROR,
                        "Groq ha risposto con status %s (model=%s).",
                        resp.status,
                        settings.groq_model,
                    )
                    raise AIServiceError(f"status {resp.status}", status=resp.status)
                data = await resp.json()
    except asyncio.TimeoutError as exc:
        logger.warning("Timeout nella chiamata a Groq.")
        raise AIServiceError("timeout") from exc
    except aiohttp.ClientError as exc:
        logger.warning("Errore di rete nella chiamata a Groq (model=%s).", settings.groq_model)
        raise AIServiceError("network error") from exc
    except ValueError as exc:
        raise AIServiceError("malformed response") from exc

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
    tier: Literal["standard", "light"] = "standard",
) -> str:
    """One OpenRouter request; upstream endpoint failover stays price bounded."""
    model_route = (
        settings.openrouter_light_models if tier == "light" else settings.openrouter_fun_models
    )

    async def paid() -> str:
        text = await generate_openrouter_completion(
            system_prompt, user_text, max_tokens, temperature=temperature,
            feature="entertainment_light" if tier == "light" else "entertainment",
            models=parse_model_list(model_route),
            require_zdr=False,
        )
        return text if len(text) <= 600 else text[:599].rstrip() + "…"

    try:
        return await text_router.generate(
            [ProviderAttempt(
                "openrouter", model_route, paid,
                settings.openrouter_timeout_seconds,
            )],
            workload="entertainment",
            deadline_seconds=settings.ai_entertainment_deadline_seconds,
        )
    except ProviderUnavailable as exc:
        raise AIServiceError("entertainment providers unavailable") from exc


# ---------------------------------------------------------------------------
# The judge — deterministic, schema-constrained. It shares nothing with
# `generate_completion` on purpose: that one is a creative call whose worst
# failure is a flat joke, this one decides whether somebody gets paid.
# ---------------------------------------------------------------------------

#: Constrained-decoding schema. One enum, no free-text field — there is
#: deliberately nothing in the reply that could carry a correct answer back to a
#: player who tried to talk the model into leaking it.
JUDGE_SCHEMA: dict = {
    "name": "verdetto",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"verdetto": {"type": "string", "enum": ["correct", "series", "wrong"]}},
        "required": ["verdetto"],
        "additionalProperties": False,
    },
}


async def judge_equivalence(system_prompt: str, user_text: str) -> str:
    """Classify exact game, same series or wrong on the metered OpenRouter lane.

    No application retry after an ambiguous timeout: the reservation remains
    charged and the game treats the answer as unverified, never as incorrect.
    """
    # Local import avoids a cycle: structured adapters share the usage/policy
    # helpers in this module.
    from services.structured_ai import (
        OpenRouterStructuredProvider, StructuredAIError, StructuredRequest,
    )

    provider = OpenRouterStructuredProvider(
        model=settings.openrouter_judge_model,
        timeout_seconds=settings.guess_judge_timeout_seconds,
        budget_lane="openrouter_other",
    )
    request = StructuredRequest(
        operation="guess_judge",
        system_prompt=system_prompt,
        user_prompt=user_text,
        schema_name=JUDGE_SCHEMA["name"],
        schema=JUDGE_SCHEMA["schema"],
        prompt_version="guess-equivalence-v2",
        schema_version="game-match-v2",
        max_output_tokens=32,
        temperature=0,
    )
    try:
        result = await provider.generate_json(request)
    except StructuredAIError as exc:
        raise AIServiceError("OpenRouter judge unavailable", status=exc.status) from exc
    return result.value["verdetto"]
