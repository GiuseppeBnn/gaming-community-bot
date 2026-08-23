"""Single-attempt, schema-constrained AI provider adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum
import json
import logging
import math
import re
from typing import Any, Literal, Protocol, overload

import aiohttp

from config_data.config import settings
from services import ai_budget, ai_service

log = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_URL = ai_service.GROQ_URL
ThinkingLevel = Literal["minimal", "low", "medium", "high"]
ProviderName = Literal["gemini", "groq", "openrouter"]
_DEFAULT_GEMINI_THINKING_LEVEL: ThinkingLevel = "medium"
_SAFE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}\Z")
_SAFE_FINISH_REASONS = frozenset({
    "BLOCKLIST",
    "IMAGE_SAFETY",
    "LANGUAGE",
    "MAX_TOKENS",
    "OTHER",
    "PROHIBITED_CONTENT",
    "RECITATION",
    "SAFETY",
    "SPII",
    "STOP",
    "content_filter",
    "length",
    "max_tokens",
    "refusal",
    "safety",
    "stop",
    "tool_calls",
})


@dataclass(frozen=True, slots=True)
class StructuredRequest:
    operation: str
    system_prompt: str
    user_prompt: str
    schema_name: str
    schema: dict[str, Any]
    prompt_version: str
    schema_version: str
    max_output_tokens: int = 64
    temperature: float = 0.1
    thinking_level: ThinkingLevel | None = None


@dataclass(frozen=True, slots=True)
class StructuredProviderResult:
    value: dict[str, Any]
    provider: ProviderName
    model: str
    usage: ai_budget.UsageMetrics
    cost_microusd: int | None = None


class StructuredAIErrorKind(str, Enum):
    missing_key = "missing_key"
    authentication = "authentication"
    configuration = "configuration"
    quota = "quota"
    rate_limit = "rate_limit"
    timeout = "timeout"
    network = "network"
    server = "server"
    refusal = "refusal"
    empty_output = "empty_output"
    malformed_json = "malformed_json"
    invalid_schema = "invalid_schema"
    invalid_enum = "invalid_enum"
    output_limit = "output_limit"
    budget_exhausted = "budget_exhausted"
    budget_unavailable = "budget_unavailable"
    deadline = "deadline"
    providers_unavailable = "providers_unavailable"


class StructuredAIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        # Temporary compatibility for ai_game_service until its Task 8 migration.
        kind: StructuredAIErrorKind = StructuredAIErrorKind.invalid_schema,
        provider: ProviderName | None = None,
        status: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.provider = provider
        self.status = status
        self.retry_after_seconds = retry_after_seconds


class StructuredAIProvider(Protocol):
    name: ProviderName
    model: str
    timeout_seconds: float
    configured: bool

    async def generate_json(
        self, request: StructuredRequest,
    ) -> StructuredProviderResult: ...


def _status_kind(status: int) -> StructuredAIErrorKind:
    if status in {401, 403}:
        return StructuredAIErrorKind.authentication
    if status == 402:
        return StructuredAIErrorKind.quota
    if status == 408:
        return StructuredAIErrorKind.timeout
    if status == 422:
        return StructuredAIErrorKind.invalid_schema
    if status == 429:
        return StructuredAIErrorKind.rate_limit
    if status >= 500:
        return StructuredAIErrorKind.server
    return StructuredAIErrorKind.configuration


def _retry_after_seconds(headers: Any) -> int | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    try:
        seconds = int(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = math.ceil((retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if seconds >= 0 else None


async def _post_json_once(
    *,
    provider: ProviderName,
    model: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> Any:
    """Make one physical request and never expose provider-controlled material."""
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(url, headers=headers, json=payload) as response:
                if response.status != 200:
                    await response.read()
                    retry_after = _retry_after_seconds(response.headers)
                    log.warning(
                        "Structured AI HTTP failure provider=%s model=%s status=%s "
                        "retry_after=%s",
                        provider,
                        model,
                        response.status,
                        retry_after,
                    )
                    raise StructuredAIError(
                        f"{provider} status {response.status}",
                        kind=_status_kind(response.status),
                        provider=provider,
                        status=response.status,
                        retry_after_seconds=retry_after,
                    )
                try:
                    return await response.json()
                except (aiohttp.ContentTypeError, TypeError, ValueError) as exc:
                    log.warning(
                        "Structured AI malformed envelope provider=%s model=%s status=200",
                        provider,
                        model,
                    )
                    raise StructuredAIError(
                        "malformed provider JSON",
                        kind=StructuredAIErrorKind.malformed_json,
                        provider=provider,
                        status=200,
                    ) from exc
    except StructuredAIError:
        raise
    except asyncio.TimeoutError as exc:
        raise StructuredAIError(
            f"{provider} timeout",
            kind=StructuredAIErrorKind.timeout,
            provider=provider,
        ) from exc
    except aiohttp.ClientError as exc:
        raise StructuredAIError(
            f"{provider} network error",
            kind=StructuredAIErrorKind.network,
            provider=provider,
        ) from exc


def _strict_json(text: str, *, provider: ProviderName) -> Any:
    if not text.strip():
        raise StructuredAIError(
            "empty structured output",
            kind=StructuredAIErrorKind.empty_output,
            provider=provider,
        )

    def reject_constant(_value: str) -> None:
        raise ValueError("non-standard JSON constant")

    try:
        return json.loads(text, parse_constant=reject_constant)
    except (TypeError, ValueError) as exc:
        raise StructuredAIError(
            "malformed structured JSON",
            kind=StructuredAIErrorKind.malformed_json,
            provider=provider,
        ) from exc


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if expected == "null":
        return value is None
    return False


def _schema_failure(value: Any, schema: Any) -> StructuredAIErrorKind | None:
    if not isinstance(schema, dict):
        return StructuredAIErrorKind.invalid_schema
    expected = schema.get("type")
    if isinstance(expected, str) and not _matches_type(value, expected):
        return StructuredAIErrorKind.invalid_schema
    if isinstance(expected, list):
        allowed = [item for item in expected if isinstance(item, str)]
        if not allowed or not any(_matches_type(value, item) for item in allowed):
            return StructuredAIErrorKind.invalid_schema
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or value not in enum:
            return StructuredAIErrorKind.invalid_enum

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            return StructuredAIErrorKind.invalid_schema
        if any(not isinstance(key, str) or key not in value for key in required):
            return StructuredAIErrorKind.invalid_schema
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            return StructuredAIErrorKind.invalid_schema
        for key, child in properties.items():
            if key in value:
                failure = _schema_failure(value[key], child)
                if failure is not None:
                    return failure
    if isinstance(value, list) and "items" in schema:
        for item in value:
            failure = _schema_failure(item, schema["items"])
            if failure is not None:
                return failure
    return None


def _validated_value(
    text: str, *, request: StructuredRequest, provider: ProviderName,
) -> dict[str, Any]:
    value = _strict_json(text, provider=provider)
    failure = _schema_failure(value, request.schema)
    if failure is not None:
        raise StructuredAIError(
            "structured output violates schema",
            kind=failure,
            provider=provider,
        )
    if not isinstance(value, dict):
        raise StructuredAIError(
            "structured output is not an object",
            kind=StructuredAIErrorKind.invalid_schema,
            provider=provider,
        )
    return value


def _safe_model(data: Any, fallback: str, *, key: str = "model") -> str:
    if isinstance(data, dict):
        actual = data.get(key)
        if isinstance(actual, str) and _SAFE_MODEL.fullmatch(actual) is not None:
            return actual
    return fallback


def _safe_finish_reason(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in _SAFE_FINISH_REASONS else "unknown"


def _metrics_for_model(
    metrics: ai_budget.UsageMetrics, model: str,
) -> ai_budget.UsageMetrics:
    return ai_budget.UsageMetrics(
        actual_model=model,
        prompt_tokens=metrics.prompt_tokens,
        completion_tokens=metrics.completion_tokens,
        reasoning_tokens=metrics.reasoning_tokens,
        cached_tokens=metrics.cached_tokens,
    )


def _openai_finish_reason(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    finish_reason = choices[0].get("finish_reason")
    return finish_reason if isinstance(finish_reason, str) else None


def _openai_value(
    data: Any, *, request: StructuredRequest, provider: ProviderName,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise StructuredAIError(
            "malformed provider envelope",
            kind=StructuredAIErrorKind.malformed_json,
            provider=provider,
        )
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise StructuredAIError(
            "malformed provider choices",
            kind=StructuredAIErrorKind.malformed_json,
            provider=provider,
        )
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason in {"length", "max_tokens"}:
        raise StructuredAIError(
            "structured output limit reached",
            kind=StructuredAIErrorKind.output_limit,
            provider=provider,
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise StructuredAIError(
            "malformed provider message",
            kind=StructuredAIErrorKind.malformed_json,
            provider=provider,
        )
    if message.get("refusal") or finish_reason in {"content_filter", "safety", "refusal"}:
        raise StructuredAIError(
            "provider refusal",
            kind=StructuredAIErrorKind.refusal,
            provider=provider,
        )
    content = message.get("content")
    if content is None or content == "":
        raise StructuredAIError(
            "empty structured output",
            kind=StructuredAIErrorKind.empty_output,
            provider=provider,
        )
    if not isinstance(content, str):
        raise StructuredAIError(
            "malformed structured content",
            kind=StructuredAIErrorKind.malformed_json,
            provider=provider,
        )
    return _validated_value(content, request=request, provider=provider)


def _log_output_failure(
    *,
    provider: ProviderName,
    model: str,
    finish_reason: str | None,
    metrics: ai_budget.UsageMetrics,
    kind: StructuredAIErrorKind,
) -> None:
    log.warning(
        "Structured AI output rejected provider=%s model=%s finish_reason=%s "
        "prompt_tokens=%s output_tokens=%s reasoning_tokens=%s kind=%s",
        provider,
        model,
        _safe_finish_reason(finish_reason),
        metrics.prompt_tokens,
        metrics.completion_tokens,
        metrics.reasoning_tokens,
        kind.value,
    )


class GeminiStructuredProvider:
    """Gemini REST adapter; legacy keyword calls remain until Task 8."""

    name: ProviderName = "gemini"

    @property
    def model(self) -> str:
        return settings.twentyq_gemini_model

    @property
    def timeout_seconds(self) -> float:
        return settings.twentyq_gemini_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(settings.gemini_api_key)

    @overload
    async def generate_json(
        self, request: StructuredRequest,
    ) -> StructuredProviderResult: ...

    @overload
    async def generate_json(
        self,
        request: None = None,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        max_output_tokens: int = 256,
        thinking_level: ThinkingLevel | None = None,
        temperature: float = 0.1,
    ) -> dict[str, Any]: ...

    async def generate_json(
        self,
        request: StructuredRequest | None = None,
        *,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        schema: dict[str, Any] | None = None,
        max_output_tokens: int = 256,
        thinking_level: ThinkingLevel | None = None,
        temperature: float = 0.1,
    ) -> StructuredProviderResult | dict[str, Any]:
        legacy = request is None
        if request is None:
            if system_prompt is None or user_prompt is None or schema is None:
                raise TypeError("legacy structured request requires prompts and schema")
            request = StructuredRequest(
                operation="twentyq_question",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema_name="twentyq_verdict",
                schema=schema,
                prompt_version="legacy",
                schema_version="legacy",
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                thinking_level=thinking_level,
            )
        result = await self._generate(request)
        return result.value if legacy else result

    async def _generate(self, request: StructuredRequest) -> StructuredProviderResult:
        if not self.configured:
            raise StructuredAIError(
                "missing Gemini api key",
                kind=StructuredAIErrorKind.missing_key,
                provider=self.name,
            )
        payload = {
            "systemInstruction": {"parts": [{"text": request.system_prompt}]},
            "contents": [{
                "role": "user",
                "parts": [{"text": request.user_prompt}],
            }],
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_output_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": request.schema,
                "thinkingConfig": {
                    "thinkingLevel": (
                        request.thinking_level or _DEFAULT_GEMINI_THINKING_LEVEL
                    ),
                },
            },
        }
        data = await _post_json_once(
            provider=self.name,
            model=self.model,
            url=f"{GEMINI_BASE_URL}/{self.model}:generateContent",
            headers={"x-goog-api-key": settings.gemini_api_key},
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        model = _safe_model(data, self.model, key="modelVersion")
        usage = data.get("usageMetadata") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        metrics = ai_budget.UsageMetrics(
            actual_model=model,
            prompt_tokens=ai_service._usage_int(usage.get("promptTokenCount")),
            completion_tokens=ai_service._usage_int(usage.get("candidatesTokenCount")),
            reasoning_tokens=ai_service._usage_int(usage.get("thoughtsTokenCount")),
            cached_tokens=ai_service._usage_int(usage.get("cachedContentTokenCount")),
        )
        finish_reason: str | None = None
        try:
            if not isinstance(data, dict):
                raise StructuredAIError(
                    "malformed Gemini envelope",
                    kind=StructuredAIErrorKind.malformed_json,
                    provider=self.name,
                )
            prompt_feedback = data.get("promptFeedback")
            if (
                isinstance(prompt_feedback, dict)
                and isinstance(prompt_feedback.get("blockReason"), str)
                and prompt_feedback["blockReason"]
            ):
                raise StructuredAIError(
                    "provider refusal",
                    kind=StructuredAIErrorKind.refusal,
                    provider=self.name,
                )
            candidates = data.get("candidates")
            if (
                not isinstance(candidates, list)
                or not candidates
                or not isinstance(candidates[0], dict)
            ):
                raise StructuredAIError(
                    "malformed Gemini candidates",
                    kind=StructuredAIErrorKind.malformed_json,
                    provider=self.name,
                )
            candidate = candidates[0]
            raw_finish = candidate.get("finishReason")
            finish_reason = raw_finish if isinstance(raw_finish, str) else None
            if finish_reason == "MAX_TOKENS":
                raise StructuredAIError(
                    "structured output limit reached",
                    kind=StructuredAIErrorKind.output_limit,
                    provider=self.name,
                )
            if finish_reason in {
                "SAFETY",
                "BLOCKLIST",
                "PROHIBITED_CONTENT",
                "RECITATION",
                "SPII",
                "LANGUAGE",
                "IMAGE_SAFETY",
                "OTHER",
            }:
                raise StructuredAIError(
                    "provider refusal",
                    kind=StructuredAIErrorKind.refusal,
                    provider=self.name,
                )
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                raise StructuredAIError(
                    "malformed Gemini content",
                    kind=StructuredAIErrorKind.malformed_json,
                    provider=self.name,
                )
            text = "".join(
                part["text"]
                for part in parts
                if (
                    isinstance(part, dict)
                    and not part.get("thought")
                    and isinstance(part.get("text"), str)
                )
            )
            value = _validated_value(text, request=request, provider=self.name)
        except StructuredAIError as exc:
            _log_output_failure(
                provider=self.name,
                model=self.model,
                finish_reason=finish_reason,
                metrics=metrics,
                kind=exc.kind,
            )
            raise
        return StructuredProviderResult(value, self.name, model, metrics)


class GroqStructuredProvider:
    """One-request OpenAI-compatible Groq structured adapter."""

    name: ProviderName = "groq"

    @property
    def model(self) -> str:
        return settings.twentyq_groq_model

    @property
    def timeout_seconds(self) -> float:
        return settings.twentyq_groq_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(settings.groq_api_key)

    async def generate_json(self, request: StructuredRequest) -> StructuredProviderResult:
        if not self.configured:
            raise StructuredAIError(
                "missing Groq api key",
                kind=StructuredAIErrorKind.missing_key,
                provider=self.name,
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.schema,
                },
            },
            "reasoning_effort": "low",
            "temperature": request.temperature,
            "max_completion_tokens": request.max_output_tokens,
        }
        data = await _post_json_once(
            provider=self.name,
            model=self.model,
            url=GROQ_URL,
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        model = _safe_model(data, self.model)
        metrics, _cost = ai_service._openrouter_usage(data)
        metrics = _metrics_for_model(metrics, model)
        try:
            value = _openai_value(data, request=request, provider=self.name)
        except StructuredAIError as exc:
            _log_output_failure(
                provider=self.name,
                model=self.model,
                finish_reason=_openai_finish_reason(data),
                metrics=metrics,
                kind=exc.kind,
            )
            raise
        return StructuredProviderResult(value, self.name, model, metrics)


async def _settle_openrouter_authoritatively(
    reservation: ai_budget.Reservation,
    *,
    status: str,
    actual_microusd: int | None,
    metrics: ai_budget.UsageMetrics,
) -> None:
    settlement = asyncio.create_task(ai_budget.settle(
        reservation,
        status=status,
        actual_microusd=actual_microusd,
        metrics=metrics,
    ))
    try:
        await asyncio.shield(settlement)
    except asyncio.CancelledError:
        while not settlement.done():
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                continue
        try:
            settlement.result()
        except (Exception, asyncio.CancelledError):
            log.error(
                "Structured OpenRouter settlement failed during cancellation "
                "provider=openrouter status=%s",
                status,
            )
        raise
    except Exception as exc:
        log.error(
            "Structured OpenRouter settlement failed provider=openrouter status=%s",
            status,
        )
        raise StructuredAIError(
            "OpenRouter budget settlement unavailable",
            kind=StructuredAIErrorKind.budget_unavailable,
            provider="openrouter",
        ) from exc


class OpenRouterStructuredProvider:
    """One-model paid adapter with fail-closed settlement accounting."""

    name: ProviderName = "openrouter"

    @property
    def model(self) -> str:
        return settings.twentyq_openrouter_model

    @property
    def timeout_seconds(self) -> float:
        return settings.twentyq_openrouter_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(settings.openrouter_api_key)

    async def generate_json(self, request: StructuredRequest) -> StructuredProviderResult:
        if not self.configured:
            raise StructuredAIError(
                "missing OpenRouter api key",
                kind=StructuredAIErrorKind.missing_key,
                provider=self.name,
            )
        try:
            reservation = await ai_budget.reserve(
                feature=request.operation,
                budget_lane="twentyq",
                provider=self.name,
                requested_model=self.model,
                system_prompt=request.system_prompt,
                user_text=request.user_prompt,
                max_output_tokens=request.max_output_tokens,
            )
        except ai_budget.AIBudgetExceeded as exc:
            raise StructuredAIError(
                "OpenRouter budget exhausted",
                kind=StructuredAIErrorKind.budget_exhausted,
                provider=self.name,
            ) from exc
        except ai_budget.AIBudgetError as exc:
            raise StructuredAIError(
                "OpenRouter budget unavailable",
                kind=StructuredAIErrorKind.budget_unavailable,
                provider=self.name,
            ) from exc

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.schema,
                },
            },
            "provider": ai_service._openrouter_provider_policy(
                require_zdr=True,
                allow_fallbacks=False,
            ),
            "reasoning": {"effort": "none", "exclude": True},
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "usage": {"include": True},
        }
        try:
            data = await _post_json_once(
                provider=self.name,
                model=self.model,
                url=settings.openrouter_url,
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                    "X-Title": settings.openrouter_app_name,
                },
                payload=payload,
                timeout_seconds=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            try:
                await _settle_openrouter_authoritatively(
                    reservation,
                    status="uncertain",
                    actual_microusd=None,
                    metrics=ai_budget.UsageMetrics(),
                )
            except StructuredAIError:
                pass
            raise
        except StructuredAIError as exc:
            known_http_failure = exc.status is not None and exc.status != 200
            malformed_envelope = exc.status == 200
            await _settle_openrouter_authoritatively(
                reservation,
                status=(
                    "failed" if known_http_failure
                    else "malformed" if malformed_envelope
                    else "uncertain"
                ),
                actual_microusd=0 if known_http_failure else None,
                metrics=ai_budget.UsageMetrics(),
            )
            raise
        except Exception as exc:
            await _settle_openrouter_authoritatively(
                reservation,
                status="malformed",
                actual_microusd=None,
                metrics=ai_budget.UsageMetrics(),
            )
            raise StructuredAIError(
                "malformed OpenRouter provider envelope",
                kind=StructuredAIErrorKind.malformed_json,
                provider=self.name,
            ) from exc

        metrics = ai_budget.UsageMetrics()
        actual_microusd: int | None = None
        model = self.model
        try:
            metrics, actual_microusd = ai_service._openrouter_usage(data)
            model = _safe_model(data, self.model)
            metrics = _metrics_for_model(metrics, model)
            value = _openai_value(data, request=request, provider=self.name)
        except StructuredAIError as exc:
            _log_output_failure(
                provider=self.name,
                model=self.model,
                finish_reason=_openai_finish_reason(data),
                metrics=metrics,
                kind=exc.kind,
            )
            await _settle_openrouter_authoritatively(
                reservation,
                status="malformed",
                actual_microusd=actual_microusd,
                metrics=metrics,
            )
            raise
        except Exception as exc:
            await _settle_openrouter_authoritatively(
                reservation,
                status="malformed",
                actual_microusd=None,
                metrics=ai_budget.UsageMetrics(),
            )
            raise StructuredAIError(
                "malformed OpenRouter accounting metadata",
                kind=StructuredAIErrorKind.malformed_json,
                provider=self.name,
            ) from exc
        await _settle_openrouter_authoritatively(
            reservation,
            status="completed",
            actual_microusd=actual_microusd,
            metrics=metrics,
        )
        return StructuredProviderResult(
            value,
            self.name,
            model,
            metrics,
            cost_microusd=actual_microusd,
        )
