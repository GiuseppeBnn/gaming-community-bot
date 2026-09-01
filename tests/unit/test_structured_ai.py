from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import json
from types import SimpleNamespace

import aiohttp
from aioresponses import aioresponses
import pytest

from services import ai_budget, ai_game_service, structured_ai


@pytest.fixture
def structured_request() -> structured_ai.StructuredRequest:
    return structured_ai.StructuredRequest(
        operation="twentyq_question",
        system_prompt="system-secret",
        user_prompt='{"dossier":"private","question":"q","history":[]}',
        schema_name="twentyq_verdict",
        schema={
            "type": "object",
            "properties": {
                "verdetto": {"type": "string", "enum": ["si", "no", "forse"]},
            },
            "required": ["verdetto"],
            "additionalProperties": False,
        },
        prompt_version="v2",
        schema_version="v2",
        max_output_tokens=72,
        temperature=0.2,
        thinking_level="minimal",
    )


@pytest.fixture
def gemini(monkeypatch) -> structured_ai.GeminiStructuredProvider:
    monkeypatch.setattr(structured_ai.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(structured_ai.settings, "twentyq_gemini_model", "gemini-test")
    monkeypatch.setattr(structured_ai.settings, "twentyq_gemini_timeout_seconds", 3)
    return structured_ai.GeminiStructuredProvider()


def _url(model: str = "gemini-test") -> str:
    return f"{structured_ai.GEMINI_BASE_URL}/{model}:generateContent"


def _response(
    value: str = '{"verdetto":"si"}',
    *,
    finish_reason: str = "STOP",
) -> dict:
    return {
        "modelVersion": "gemini-test-actual",
        "candidates": [{
            "finishReason": finish_reason,
            "content": {"parts": [
                {"thought": True, "text": "private reasoning"},
                {"text": value},
            ]},
        }],
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 3,
            "thoughtsTokenCount": 5,
            "cachedContentTokenCount": 2,
        },
    }


def _request_count(mocked: aioresponses) -> int:
    return sum(len(calls) for calls in mocked.requests.values())


def test_provider_neutral_contracts_are_immutable_and_complete(structured_request):
    assert structured_request.operation == "twentyq_question"
    with pytest.raises(FrozenInstanceError):
        structured_request.operation = "changed"

    assert {kind.value for kind in structured_ai.StructuredAIErrorKind} == {
        "missing_key",
        "authentication",
        "configuration",
        "quota",
        "rate_limit",
        "timeout",
        "network",
        "server",
        "refusal",
        "empty_output",
        "malformed_json",
        "invalid_schema",
        "invalid_enum",
        "output_limit",
        "budget_exhausted",
        "budget_unavailable",
        "deadline",
        "providers_unavailable",
    }
    error = structured_ai.StructuredAIError(
        "slow down",
        kind=structured_ai.StructuredAIErrorKind.rate_limit,
        provider="gemini",
        status=429,
        retry_after_seconds=9,
    )
    assert (error.kind, error.provider, error.status, error.retry_after_seconds) == (
        structured_ai.StructuredAIErrorKind.rate_limit,
        "gemini",
        429,
        9,
    )


@pytest.mark.parametrize(
    ("budget_feature", "expected_feature"),
    [(None, "twentyq_question"), ("eval-twentyq-a1b2c3d4e5f6", "eval-twentyq-a1b2c3d4e5f6")],
)
async def test_openrouter_preserves_runtime_budget_feature_or_uses_eval_override(
    monkeypatch, structured_request, budget_feature, expected_feature,
):
    """Ignoring the override loses per-run attribution; changing the default breaks live games."""
    monkeypatch.setattr(structured_ai.settings, "openrouter_api_key", "test-key")
    captured: dict[str, object] = {}

    async def reserve(**kwargs):
        captured.update(kwargs)
        return ai_budget.Reservation("r" * 36, "2026-09", expected_feature, "twentyq", 10)

    async def post_json_once(**_kwargs):
        raise structured_ai.StructuredAIError(
            "offline", kind=structured_ai.StructuredAIErrorKind.network, provider="openrouter",
        )

    async def settle(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ai_budget, "reserve", reserve)
    monkeypatch.setattr(structured_ai, "_post_json_once", post_json_once)
    monkeypatch.setattr(structured_ai, "_settle_openrouter_authoritatively", settle)

    with pytest.raises(structured_ai.StructuredAIError):
        await structured_ai.OpenRouterStructuredProvider(
            budget_feature=budget_feature,
        ).generate_json(structured_request)

    assert captured["feature"] == expected_feature
    assert captured["budget_lane"] == "twentyq"


@pytest.mark.parametrize("feature", ["", "x" * 33])
def test_openrouter_rejects_unattributable_budget_feature_overrides(feature):
    """Accepting an empty or truncated identifier would make paid eval cost silently disappear."""
    with pytest.raises(ValueError, match="budget_feature"):
        structured_ai.OpenRouterStructuredProvider(budget_feature=feature)


def test_retry_after_parses_seconds_dates_and_rejects_invalid_values():
    assert structured_ai._retry_after_seconds(None) is None
    assert structured_ai._retry_after_seconds({"Retry-After": 3}) is None
    assert structured_ai._retry_after_seconds({"Retry-After": "-1"}) is None
    assert structured_ai._retry_after_seconds({"Retry-After": "not-a-date"}) is None
    assert structured_ai._retry_after_seconds({
        "Retry-After": "Wed, 31 Dec 2099 23:59:59 GMT",
    }) > 0
    assert structured_ai._retry_after_seconds({
        "Retry-After": "31 Dec 2099 23:59:59",
    }) > 0


@pytest.mark.parametrize(
    ("value", "schema", "failure"),
    [
        ({}, {"type": "object"}, None),
        ([], {"type": "array"}, None),
        (True, {"type": "boolean"}, None),
        (1, {"type": "integer"}, None),
        (True, {"type": "integer"}, "invalid_schema"),
        (1.5, {"type": "number"}, None),
        (float("inf"), {"type": "number"}, "invalid_schema"),
        (None, {"type": "null"}, None),
        ("x", {"type": "unknown"}, "invalid_schema"),
        ("x", ["not", "a", "schema"], "invalid_schema"),
        ("x", {"type": ["string", "null"]}, None),
        ("x", {"type": [42]}, "invalid_schema"),
        ({}, {"type": "object", "properties": []}, "invalid_schema"),
        ({}, {"type": "object", "required": "key"}, "invalid_schema"),
        (
            {"items": [1, "bad"]},
            {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "integer"}},
                },
            },
            "invalid_schema",
        ),
    ],
)
def test_local_schema_validation_covers_supported_json_types(value, schema, failure):
    got = structured_ai._schema_failure(value, schema)
    assert (got.value if got is not None else None) == failure


async def test_gemini_sends_schema_and_returns_provider_metadata(
    gemini, structured_request,
):
    with aioresponses() as mocked:
        mocked.post(_url(), payload=_response())
        result = await gemini.generate_json(structured_request)

    assert result == structured_ai.StructuredProviderResult(
        value={"verdetto": "si"},
        provider="gemini",
        model="gemini-test-actual",
        usage=ai_budget.UsageMetrics("gemini-test-actual", 12, 3, 5, 2),
    )
    assert _request_count(mocked) == 1
    request = next(iter(mocked.requests.values()))[0]
    sent = request.kwargs["json"]
    assert sent == {
        "systemInstruction": {"parts": [{"text": structured_request.system_prompt}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": structured_request.user_prompt}],
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 72,
            "responseMimeType": "application/json",
            "responseJsonSchema": structured_request.schema,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }
    assert request.kwargs["headers"]["x-goog-api-key"] == "test-key"
    assert gemini.name == "gemini"
    assert gemini.model == "gemini-test"
    assert gemini.timeout_seconds == 3
    assert gemini.configured is True


async def test_gemini_uses_default_thinking_level_when_request_omits_it(
    gemini, structured_request,
):
    request = structured_ai.StructuredRequest(
        operation=structured_request.operation,
        system_prompt=structured_request.system_prompt,
        user_prompt=structured_request.user_prompt,
        schema_name=structured_request.schema_name,
        schema=structured_request.schema,
        prompt_version=structured_request.prompt_version,
        schema_version=structured_request.schema_version,
    )
    with aioresponses() as mocked:
        mocked.post(_url(), payload=_response())
        await gemini.generate_json(request)
    sent = next(iter(mocked.requests.values()))[0].kwargs["json"]
    assert sent["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "medium"}


async def test_gemini_legacy_service_path_returns_verdict_and_is_single_attempt(gemini):
    snapshot = SimpleNamespace(
        game=SimpleNamespace(dossier_json=json.dumps({"facts": "Aperture"})),
        turns=(),
    )
    with aioresponses() as mocked:
        mocked.post(_url(), payload=_response('{"verdetto":"forse"}'))
        verdict = await ai_game_service.classify_question(
            snapshot,  # type: ignore[arg-type]
            "È in prima persona?",
            gemini,  # type: ignore[arg-type]
        )

    assert verdict == ai_game_service.QuestionVerdict("forse")
    assert _request_count(mocked) == 1


async def test_gemini_legacy_shim_rejects_incomplete_keyword_call(gemini):
    with pytest.raises(TypeError, match="requires prompts and schema"):
        await gemini.generate_json()


async def test_gemini_missing_key_fails_before_network(monkeypatch, structured_request):
    monkeypatch.setattr(structured_ai.settings, "gemini_api_key", "")
    provider = structured_ai.GeminiStructuredProvider()
    assert provider.configured is False
    with pytest.raises(structured_ai.StructuredAIError) as raised:
        await provider.generate_json(structured_request)
    assert raised.value.kind is structured_ai.StructuredAIErrorKind.missing_key
    assert raised.value.provider == "gemini"


@pytest.mark.parametrize(
    ("status", "expected_kind"),
    [
        (400, "configuration"),
        (401, "authentication"),
        (403, "authentication"),
        (402, "quota"),
        (408, "timeout"),
        (422, "invalid_schema"),
        (429, "rate_limit"),
        (500, "server"),
        (503, "server"),
    ],
)
async def test_gemini_normalizes_status_retry_after_without_retrying(
    gemini, structured_request, status, expected_kind, caplog,
):
    secret = "provider-secret-body"
    with aioresponses() as mocked:
        mocked.post(_url(), status=status, body=secret, headers={"Retry-After": "7"})
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await gemini.generate_json(structured_request)

    assert raised.value.kind.value == expected_kind
    assert raised.value.provider == "gemini"
    assert raised.value.status == status
    assert raised.value.retry_after_seconds == 7
    assert _request_count(mocked) == 1
    assert secret not in caplog.text
    assert structured_request.system_prompt not in caplog.text
    assert structured_request.user_prompt not in caplog.text


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (TimeoutError(), "timeout"),
        (aiohttp.ClientConnectionError("down"), "network"),
    ],
)
async def test_gemini_transport_failure_is_typed_and_single_attempt(
    gemini, structured_request, failure, kind,
):
    with aioresponses() as mocked:
        mocked.post(_url(), exception=failure)
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await gemini.generate_json(structured_request)
    assert raised.value.kind.value == kind
    assert _request_count(mocked) == 1


async def test_gemini_cancellation_is_not_normalized(gemini, structured_request):
    with aioresponses() as mocked:
        mocked.post(_url(), exception=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await gemini.generate_json(structured_request)


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        ([], "malformed_json"),
        ({"candidates": []}, "malformed_json"),
        ({"candidates": [{}]}, "malformed_json"),
        (_response("not-json"), "malformed_json"),
        (_response('{"verdetto":NaN}'), "malformed_json"),
        (_response("[]"), "invalid_schema"),
        (_response("{}"), "invalid_schema"),
        (_response('{"verdetto":"mai"}'), "invalid_enum"),
        (_response('{"verdetto":"si","extra":1}'), "invalid_schema"),
        (_response("", finish_reason="STOP"), "empty_output"),
        (_response('{"verdetto":"si"}', finish_reason="MAX_TOKENS"), "output_limit"),
        (_response('{"verdetto":"si"}', finish_reason="SAFETY"), "refusal"),
    ],
)
async def test_gemini_normalizes_output_failures(
    gemini, structured_request, response, kind,
):
    with aioresponses() as mocked:
        mocked.post(_url(), payload=response)
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await gemini.generate_json(structured_request)
    assert raised.value.kind.value == kind


async def test_gemini_prompt_feedback_without_candidates_is_refusal_and_private(
    gemini, structured_request, caplog,
):
    feedback_secret = "feedback-secret-must-not-be-logged"
    response = {
        "modelVersion": "gemini-test-actual",
        "promptFeedback": {
            "blockReason": "SAFETY",
            "blockReasonMessage": feedback_secret,
        },
        "candidates": [],
    }
    with aioresponses() as mocked:
        mocked.post(_url(), payload=response)
        with caplog.at_level("WARNING"), pytest.raises(
            structured_ai.StructuredAIError,
        ) as raised:
            await gemini.generate_json(structured_request)

    assert raised.value.kind is structured_ai.StructuredAIErrorKind.refusal
    assert feedback_secret not in caplog.text


@pytest.mark.parametrize("finish_reason", ["LANGUAGE", "IMAGE_SAFETY", "OTHER"])
async def test_gemini_real_refusal_finish_reasons(
    gemini, structured_request, finish_reason,
):
    with aioresponses() as mocked:
        mocked.post(
            _url(),
            payload=_response('{"verdetto":"si"}', finish_reason=finish_reason),
        )
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await gemini.generate_json(structured_request)

    assert raised.value.kind is structured_ai.StructuredAIErrorKind.refusal


async def test_gemini_provider_metadata_is_bounded_and_logs_requested_model_only(
    gemini, structured_request, caplog,
):
    model_secret = "provider-model-secret\nforged-log-line"
    finish_secret = "provider-finish-secret\nforged-finish-line"
    response = _response("not-json", finish_reason=finish_secret)
    response["modelVersion"] = model_secret
    with aioresponses() as mocked:
        mocked.post(_url(), payload=response)
        with caplog.at_level("WARNING"), pytest.raises(structured_ai.StructuredAIError):
            await gemini.generate_json(structured_request)

    assert model_secret not in caplog.text
    assert finish_secret not in caplog.text
    assert "forged-log-line" not in caplog.text
    assert "forged-finish-line" not in caplog.text
    assert "model=gemini-test" in caplog.text
    assert "finish_reason=unknown" in caplog.text

    valid = _response()
    valid["modelVersion"] = "x" * 129
    with aioresponses() as mocked:
        mocked.post(_url(), payload=valid)
        result = await gemini.generate_json(structured_request)
    assert result.model == "gemini-test"
    assert result.usage.actual_model == "gemini-test"


async def test_gemini_malformed_envelope_logs_only_safe_metadata(
    gemini, structured_request, caplog,
):
    signature = "secret-thought-signature"
    partial = "secret-partial-output"
    response = {
        "modelVersion": "gemini-test-actual",
        "candidates": [{
            "finishReason": "MAX_TOKENS",
            "content": {"parts": [{"thoughtSignature": signature, "text": partial}]},
        }],
        "usageMetadata": {"promptTokenCount": 12, "thoughtsTokenCount": 241},
    }
    with aioresponses() as mocked:
        mocked.post(_url(), payload=response)
        with caplog.at_level("WARNING"), pytest.raises(structured_ai.StructuredAIError):
            await gemini.generate_json(structured_request)

    assert signature not in caplog.text
    assert partial not in caplog.text
    assert structured_request.system_prompt not in caplog.text
    assert structured_request.user_prompt not in caplog.text
    assert "model=gemini-test " in caplog.text
    assert "gemini-test-actual" not in caplog.text
    assert "MAX_TOKENS" in caplog.text
    assert "241" in caplog.text


async def test_gemini_rejects_untrusted_usage_values(gemini, structured_request):
    response = _response()
    response["usageMetadata"] = {
        "promptTokenCount": True,
        "candidatesTokenCount": -1,
        "thoughtsTokenCount": 4.5,
        "cachedContentTokenCount": 0,
    }
    with aioresponses() as mocked:
        mocked.post(_url(), payload=response)
        result = await gemini.generate_json(structured_request)
    assert result.usage == ai_budget.UsageMetrics("gemini-test-actual", None, None, None, 0)
