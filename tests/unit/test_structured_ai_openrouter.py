from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiohttp
from aioresponses import aioresponses
import pytest

from services import ai_budget, structured_ai


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
    )


@pytest.fixture
def openrouter(monkeypatch):
    state = SimpleNamespace(reserve_kwargs=None, settlements=[])
    monkeypatch.setattr(structured_ai.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(structured_ai.settings, "openrouter_url", "https://openrouter.test/chat")
    monkeypatch.setattr(
        structured_ai.settings,
        "twentyq_openrouter_model",
        "deepseek/deepseek-v4-flash-0731",
    )
    monkeypatch.setattr(structured_ai.settings, "twentyq_openrouter_timeout_seconds", 5)

    async def reserve(**kwargs):
        state.reserve_kwargs = kwargs
        return ai_budget.Reservation(
            "req", "2026-08", kwargs["feature"], kwargs["budget_lane"], 10,
        )

    async def settle(_reservation, **kwargs):
        state.settlements.append(kwargs)

    monkeypatch.setattr(structured_ai.ai_budget, "reserve", reserve)
    monkeypatch.setattr(structured_ai.ai_budget, "settle", settle)
    return state


def _response(
    content: object = '{"verdetto":"si"}',
    *,
    finish_reason: str = "stop",
    cost=0.000001,
) -> dict:
    return {
        "model": "deepseek/deepseek-v4-flash-0731",
        "choices": [{
            "finish_reason": finish_reason,
            "message": {"content": content},
        }],
        "usage": {
            "cost": cost,
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    }


def _request_count(mocked: aioresponses) -> int:
    return sum(len(calls) for calls in mocked.requests.values())


async def test_openrouter_structured_is_single_model_strict_zdr_and_accounted(
    openrouter, structured_request,
):
    provider = structured_ai.OpenRouterStructuredProvider()
    with aioresponses() as mocked:
        mocked.post(structured_ai.settings.openrouter_url, payload=_response())
        got = await provider.generate_json(structured_request)

    assert got == structured_ai.StructuredProviderResult(
        value={"verdetto": "si"},
        provider="openrouter",
        model="deepseek/deepseek-v4-flash-0731",
        usage=ai_budget.UsageMetrics(
            "deepseek/deepseek-v4-flash-0731", 10, 2, 1, 3,
        ),
        cost_microusd=1,
    )
    assert _request_count(mocked) == 1
    sent = next(iter(mocked.requests.values()))[0].kwargs["json"]
    assert sent["model"] == "deepseek/deepseek-v4-flash-0731"
    assert "models" not in sent
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "twentyq_verdict",
            "strict": True,
            "schema": structured_request.schema,
        },
    }
    assert sent["provider"] == {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "sort": "price",
        "max_price": {"prompt": 0.25, "completion": 0.6},
        "zdr": True,
    }
    assert sent["reasoning"] == {"effort": "none", "exclude": True}
    assert sent["usage"] == {"include": True}
    assert openrouter.reserve_kwargs == {
        "feature": "twentyq_question",
        "budget_lane": "twentyq",
        "provider": "openrouter",
        "requested_model": "deepseek/deepseek-v4-flash-0731",
        "system_prompt": structured_request.system_prompt,
        "user_text": structured_request.user_prompt,
        "max_output_tokens": 64,
    }
    assert openrouter.settlements == [{
        "status": "completed",
        "actual_microusd": 1,
        "metrics": got.usage,
    }]
    assert provider.name == "openrouter"
    assert provider.model == "deepseek/deepseek-v4-flash-0731"
    assert provider.timeout_seconds == 5
    assert provider.configured is True


async def test_openrouter_missing_key_fails_before_reservation(monkeypatch, structured_request):
    monkeypatch.setattr(structured_ai.settings, "openrouter_api_key", "")
    called = False

    async def reserve(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(structured_ai.ai_budget, "reserve", reserve)
    provider = structured_ai.OpenRouterStructuredProvider()
    assert provider.configured is False
    with pytest.raises(structured_ai.StructuredAIError) as raised:
        await provider.generate_json(structured_request)
    assert raised.value.kind is structured_ai.StructuredAIErrorKind.missing_key
    assert raised.value.provider == "openrouter"
    assert called is False


@pytest.mark.parametrize(
    ("budget_error", "kind"),
    [
        (ai_budget.AIBudgetExceeded("full"), "budget_exhausted"),
        (ai_budget.AIBudgetError("db"), "budget_unavailable"),
    ],
)
async def test_openrouter_budget_failure_blocks_before_http(
    monkeypatch, structured_request, budget_error, kind,
):
    monkeypatch.setattr(structured_ai.settings, "openrouter_api_key", "test-key")

    async def reserve(**kwargs):
        raise budget_error

    monkeypatch.setattr(structured_ai.ai_budget, "reserve", reserve)
    with pytest.raises(structured_ai.StructuredAIError) as raised:
        await structured_ai.OpenRouterStructuredProvider().generate_json(structured_request)
    assert raised.value.kind.value == kind


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
async def test_openrouter_http_failure_is_typed_settled_body_safe_and_single_attempt(
    openrouter, structured_request, status, kind, caplog,
):
    secret = "provider-secret-body"
    with aioresponses() as mocked:
        mocked.post(
            structured_ai.settings.openrouter_url,
            status=status,
            body=secret,
            headers={"Retry-After": "13"},
        )
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await structured_ai.OpenRouterStructuredProvider().generate_json(structured_request)

    assert raised.value.kind.value == kind
    assert raised.value.status == status
    assert raised.value.retry_after_seconds == 13
    assert _request_count(mocked) == 1
    assert openrouter.settlements == [{
        "status": "failed",
        "actual_microusd": 0,
        "metrics": ai_budget.UsageMetrics(),
    }]
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
async def test_openrouter_ambiguous_transport_is_typed_and_conservatively_settled(
    openrouter, structured_request, failure, kind,
):
    with aioresponses() as mocked:
        mocked.post(structured_ai.settings.openrouter_url, exception=failure)
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await structured_ai.OpenRouterStructuredProvider().generate_json(structured_request)
    assert raised.value.kind.value == kind
    assert _request_count(mocked) == 1
    assert openrouter.settlements == [{
        "status": "uncertain",
        "actual_microusd": None,
        "metrics": ai_budget.UsageMetrics(),
    }]


async def test_openrouter_cancellation_is_settled_then_propagated(
    openrouter, structured_request,
):
    with aioresponses() as mocked:
        mocked.post(
            structured_ai.settings.openrouter_url,
            exception=asyncio.CancelledError(),
        )
        with pytest.raises(asyncio.CancelledError):
            await structured_ai.OpenRouterStructuredProvider().generate_json(structured_request)
    assert openrouter.settlements == [{
        "status": "uncertain",
        "actual_microusd": None,
        "metrics": ai_budget.UsageMetrics(),
    }]


async def test_openrouter_cancellation_still_propagates_if_settlement_fails(
    monkeypatch, openrouter, structured_request, caplog,
):
    async def broken(*args, **kwargs):
        raise ai_budget.AIBudgetError("db unavailable")

    monkeypatch.setattr(structured_ai.ai_budget, "settle", broken)
    with aioresponses() as mocked:
        mocked.post(
            structured_ai.settings.openrouter_url,
            exception=asyncio.CancelledError(),
        )
        with pytest.raises(asyncio.CancelledError):
            await structured_ai.OpenRouterStructuredProvider().generate_json(structured_request)
    assert "status=uncertain" in caplog.text
    assert structured_request.system_prompt not in caplog.text
    assert structured_request.user_prompt not in caplog.text


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (_response("not-json"), "malformed_json"),
        (_response(None), "empty_output"),
        (_response("[]"), "invalid_schema"),
        (_response("{}"), "invalid_schema"),
        (_response('{"verdetto":"mai"}'), "invalid_enum"),
        (_response('{"verdetto":"si"}', finish_reason="length"), "output_limit"),
        ({
            "model": "deepseek/deepseek-v4-flash-0731",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": None, "refusal": "cannot comply"},
            }],
            "usage": {"cost": 0.000001},
        }, "refusal"),
    ],
)
async def test_openrouter_output_failures_are_settled(
    openrouter, structured_request, response, kind,
):
    with aioresponses() as mocked:
        mocked.post(structured_ai.settings.openrouter_url, payload=response)
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await structured_ai.OpenRouterStructuredProvider().generate_json(structured_request)
    assert raised.value.kind.value == kind
    assert openrouter.settlements[0]["status"] == "malformed"
    assert len(openrouter.settlements) == 1


async def test_openrouter_invalid_json_envelope_is_conservatively_settled_and_not_logged(
    openrouter, structured_request, caplog,
):
    secret = "not-json-provider-secret"
    with aioresponses() as mocked:
        mocked.post(
            structured_ai.settings.openrouter_url,
            status=200,
            body=secret,
            content_type="application/json",
        )
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await structured_ai.OpenRouterStructuredProvider().generate_json(structured_request)
    assert raised.value.kind is structured_ai.StructuredAIErrorKind.malformed_json
    assert openrouter.settlements == [{
        "status": "malformed",
        "actual_microusd": None,
        "metrics": ai_budget.UsageMetrics(),
    }]
    assert secret not in caplog.text


async def test_openrouter_settlement_failure_discards_valid_response(
    monkeypatch, openrouter, structured_request,
):
    async def broken(*args, **kwargs):
        raise ai_budget.AIBudgetError("db unavailable")

    monkeypatch.setattr(structured_ai.ai_budget, "settle", broken)
    with aioresponses() as mocked:
        mocked.post(structured_ai.settings.openrouter_url, payload=_response())
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await structured_ai.OpenRouterStructuredProvider().generate_json(structured_request)
    assert raised.value.kind is structured_ai.StructuredAIErrorKind.budget_unavailable
    assert raised.value.provider == "openrouter"


async def test_openrouter_settlement_failure_overrides_provider_error(
    monkeypatch, openrouter, structured_request,
):
    async def broken(*args, **kwargs):
        raise ai_budget.AIBudgetError("db unavailable")

    monkeypatch.setattr(structured_ai.ai_budget, "settle", broken)
    with aioresponses() as mocked:
        mocked.post(structured_ai.settings.openrouter_url, status=429, body="secret")
        with pytest.raises(structured_ai.StructuredAIError) as raised:
            await structured_ai.OpenRouterStructuredProvider().generate_json(structured_request)
    assert raised.value.kind is structured_ai.StructuredAIErrorKind.budget_unavailable


@pytest.mark.parametrize("cost", [True, -0.1, "nan", "inf", "-inf"])
async def test_openrouter_never_trusts_invalid_cost(
    openrouter, structured_request, cost,
):
    with aioresponses() as mocked:
        mocked.post(structured_ai.settings.openrouter_url, payload=_response(cost=cost))
        result = await structured_ai.OpenRouterStructuredProvider().generate_json(
            structured_request,
        )
    assert result.cost_microusd is None
    assert openrouter.settlements[0]["actual_microusd"] is None


async def test_openrouter_never_trusts_invalid_token_counters(
    openrouter, structured_request,
):
    response = _response()
    response["usage"].update({
        "prompt_tokens": True,
        "completion_tokens": -1,
        "reasoning_tokens": 2.5,
    })
    with aioresponses() as mocked:
        mocked.post(structured_ai.settings.openrouter_url, payload=response)
        result = await structured_ai.OpenRouterStructuredProvider().generate_json(
            structured_request,
        )
    assert result.usage == ai_budget.UsageMetrics(
        "deepseek/deepseek-v4-flash-0731", None, None, None, 3,
    )
