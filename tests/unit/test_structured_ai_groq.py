from __future__ import annotations

import aiohttp
from aioresponses import aioresponses
import pytest

from services import ai_budget, ai_service, structured_ai


@pytest.fixture
def structured_request() -> structured_ai.StructuredRequest:
    return structured_ai.StructuredRequest(
        operation="twentyq_question",
        system_prompt="system-secret",
        user_prompt='{"dossier":"private","question":"q","history":[]}',
        schema_name="twentyq_verdict",
        schema={
            "type": "object",
            "properties": {"verdetto": {"type": "string", "enum": ["si", "no"]}},
            "required": ["verdetto"],
            "additionalProperties": False,
        },
        prompt_version="v2",
        schema_version="v2",
        max_output_tokens=64,
        temperature=0.1,
    )


@pytest.fixture
def groq(monkeypatch) -> structured_ai.GroqStructuredProvider:
    monkeypatch.setattr(structured_ai.settings, "groq_api_key", "test-key")
    monkeypatch.setattr(structured_ai.settings, "twentyq_groq_model", "groq-test")
    monkeypatch.setattr(structured_ai.settings, "twentyq_groq_timeout_seconds", 4)
    return structured_ai.GroqStructuredProvider()


def _response(content: object = '{"verdetto":"si"}', *, finish_reason: str = "stop") -> dict:
    return {
        "model": "groq-test-actual",
        "choices": [{
            "finish_reason": finish_reason,
            "message": {"content": content},
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    }


def _request_count(mocked: aioresponses) -> int:
    return sum(len(calls) for calls in mocked.requests.values())


async def test_groq_is_strict_single_attempt_and_parses_usage(groq, structured_request):
    with aioresponses() as mocked:
        mocked.post(structured_ai.GROQ_URL, payload=_response())
        result = await groq.generate_json(structured_request)

    assert result == structured_ai.StructuredProviderResult(
        value={"verdetto": "si"},
        provider="groq",
        model="groq-test-actual",
        usage=ai_budget.UsageMetrics("groq-test-actual", 10, 2, 1, 3),
    )
    assert _request_count(mocked) == 1
    sent = next(iter(mocked.requests.values()))[0].kwargs["json"]
    assert sent == {
        "model": "groq-test",
        "messages": [
            {"role": "system", "content": structured_request.system_prompt},
            {"role": "user", "content": structured_request.user_prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "twentyq_verdict",
                "strict": True,
                "schema": structured_request.schema,
            },
        },
        "reasoning_effort": "low",
        "temperature": 0.1,
        "max_completion_tokens": 64,
    }
    assert groq.name == "groq"
    assert groq.model == "groq-test"
    assert groq.timeout_seconds == 4
    assert groq.configured is True


async def test_groq_missing_key_fails_before_network(monkeypatch, structured_request):
    monkeypatch.setattr(structured_ai.settings, "groq_api_key", "")
    provider = structured_ai.GroqStructuredProvider()
    assert provider.configured is False
    with pytest.raises(structured_ai.StructuredAIError) as raised:
        await provider.generate_json(structured_request)
    assert raised.value.kind is structured_ai.StructuredAIErrorKind.missing_key
    assert raised.value.provider == "groq"


@pytest.mark.parametrize(
    ("status", "kind"),
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
async def test_groq_status_is_typed_body_safe_and_never_retried(
    groq, structured_request, status, kind, caplog,
):
    secret = "provider-secret-body"
    with aioresponses() as mocked:
        mocked.post(
            structured_ai.GROQ_URL,
            status=status,
            body=secret,
            headers={"Retry-After": "11"},
        )
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await groq.generate_json(structured_request)

    assert raised.value.kind.value == kind
    assert raised.value.status == status
    assert raised.value.retry_after_seconds == 11
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
async def test_groq_transport_failure_is_typed_and_single_attempt(
    groq, structured_request, failure, kind,
):
    with aioresponses() as mocked:
        mocked.post(structured_ai.GROQ_URL, exception=failure)
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await groq.generate_json(structured_request)
    assert raised.value.kind.value == kind
    assert _request_count(mocked) == 1


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        ([], "malformed_json"),
        ({"choices": []}, "malformed_json"),
        ({"choices": [{"message": []}]}, "malformed_json"),
        (_response(42), "malformed_json"),
        (_response("not-json"), "malformed_json"),
        (_response(None), "empty_output"),
        (_response(""), "empty_output"),
        (_response("[]"), "invalid_schema"),
        (_response("{}"), "invalid_schema"),
        (_response('{"verdetto":"mai"}'), "invalid_enum"),
        (_response('{"verdetto":"si","extra":1}'), "invalid_schema"),
        (_response('{"verdetto":"si"}', finish_reason="length"), "output_limit"),
        ({
            "model": "groq-test-actual",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": None, "refusal": "cannot comply"},
            }],
        }, "refusal"),
    ],
)
async def test_groq_normalizes_output_failures(groq, structured_request, response, kind):
    with aioresponses() as mocked:
        mocked.post(structured_ai.GROQ_URL, payload=response)
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await groq.generate_json(structured_request)
    assert raised.value.kind.value == kind


async def test_groq_invalid_json_envelope_is_malformed_and_not_logged(
    groq, structured_request, caplog,
):
    secret = "not-json-provider-secret"
    with aioresponses() as mocked:
        mocked.post(
            structured_ai.GROQ_URL,
            status=200,
            body=secret,
            content_type="application/json",
        )
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await groq.generate_json(structured_request)
    assert raised.value.kind is structured_ai.StructuredAIErrorKind.malformed_json
    assert secret not in caplog.text


async def test_groq_rejects_untrusted_usage_values(groq, structured_request):
    response = _response()
    response["usage"] = {
        "prompt_tokens": True,
        "completion_tokens": -1,
        "reasoning_tokens": 3.5,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    with aioresponses() as mocked:
        mocked.post(structured_ai.GROQ_URL, payload=response)
        result = await groq.generate_json(structured_request)
    assert result.usage == ai_budget.UsageMetrics("groq-test-actual", None, None, None, 0)


async def test_groq_extreme_usage_never_leaks_numeric_errors(groq, structured_request):
    response = _response()
    response["usage"] = {
        "cost": "1e999999999",
        "prompt_tokens": 2**31,
        "completion_tokens": 2**63,
        "reasoning_tokens": 10**100,
        "prompt_tokens_details": {"cached_tokens": 2**31},
    }
    with aioresponses() as mocked:
        mocked.post(structured_ai.GROQ_URL, payload=response)
        result = await groq.generate_json(structured_request)

    assert result.usage == ai_budget.UsageMetrics("groq-test-actual")


async def test_groq_provider_metadata_is_bounded_and_logs_requested_model_only(
    groq, structured_request, caplog,
):
    model_secret = "provider-model-secret\nforged-log-line"
    finish_secret = "provider-finish-secret\nforged-finish-line"
    response = _response("not-json", finish_reason=finish_secret)
    response["model"] = model_secret
    with aioresponses() as mocked:
        mocked.post(structured_ai.GROQ_URL, payload=response)
        with caplog.at_level("WARNING"), pytest.raises(structured_ai.StructuredAIError):
            await groq.generate_json(structured_request)

    assert model_secret not in caplog.text
    assert finish_secret not in caplog.text
    assert "forged-log-line" not in caplog.text
    assert "forged-finish-line" not in caplog.text
    assert "model=groq-test" in caplog.text
    assert "finish_reason=unknown" in caplog.text

    valid = _response()
    valid["model"] = "x" * 129
    with aioresponses() as mocked:
        mocked.post(structured_ai.GROQ_URL, payload=valid)
        result = await groq.generate_json(structured_request)
    assert result.model == "groq-test"
    assert result.usage.actual_model == "groq-test"


async def test_textual_groq_never_logs_provider_error_body(monkeypatch, caplog):
    secret = "textual-provider-secret-body"
    monkeypatch.setattr(ai_service.settings, "groq_api_key", "test-key")
    with aioresponses() as mocked:
        mocked.post(ai_service.GROQ_URL, status=400, body=secret)
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.generate_groq_completion("system-secret", "user-secret")
    assert secret not in caplog.text
    assert "system-secret" not in caplog.text
    assert "user-secret" not in caplog.text


async def test_textual_groq_never_logs_malformed_provider_envelope(monkeypatch, caplog):
    secret = "textual-malformed-secret"
    monkeypatch.setattr(ai_service.settings, "groq_api_key", "test-key")
    with aioresponses() as mocked:
        mocked.post(ai_service.GROQ_URL, payload={"provider_material": secret})
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.generate_groq_completion("system-secret", "user-secret")
    assert secret not in caplog.text


async def test_judge_never_logs_generic_provider_error_body(monkeypatch, caplog):
    secret = "judge-provider-secret-body"
    monkeypatch.setattr(ai_service.settings, "groq_api_key", "test-key")
    with aioresponses() as mocked:
        mocked.post(ai_service.GROQ_URL, status=400, body=secret)
        with pytest.raises(ai_service.AIServiceError):
            await ai_service.judge_equivalence("system-secret", "user-secret")
    assert secret not in caplog.text
    assert "system-secret" not in caplog.text
    assert "user-secret" not in caplog.text
