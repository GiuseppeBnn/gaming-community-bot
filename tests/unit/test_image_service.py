from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from services import ai_budget, image_service as image

PNG = b"\x89PNG\r\n\x1a\nfake-test-image"
MODEL = "black-forest-labs/flux-3-image-klein"
PROVIDER = "black-forest-labs"


def discovery(**changes):
    entry = {
        "provider_tag": PROVIDER,
        "supported_parameters": {
            "resolution": {"values": ["1K"]},
            "output_format": {"values": ["png"]},
        },
        "pricing": [
            {"billable": "output_image", "unit": "image", "cost_usd": "0.009", "variant": "1k"}
        ],
    }
    entry.update(changes)
    return {"endpoints": [entry]}


class Response:
    def __init__(self, data=None, *, status=200, error=None, body=None):
        self.status, self.error = status, error
        self.body = body if body is not None else json.dumps(data).encode()
        self.content = self

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, _size):
        yield self.body


class Client:
    def __init__(self):
        self.preflight = Response(discovery())
        self.output = Response(
            {
                "data": [{"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}],
                "usage": {"cost": 0.009},
            }
        )
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.preflight

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.output


@pytest.fixture
def api(monkeypatch):
    client = Client()
    monkeypatch.setattr(image.settings, "telephone_enabled", True)
    monkeypatch.setattr(image.settings, "openrouter_api_key", "fake")
    monkeypatch.setattr(image.aiohttp, "ClientSession", lambda **kw: client)
    reserve = AsyncMock(
        return_value=ai_budget.Reservation(
            "id", "2026-11", "telephone_image", "openrouter_other", 12000
        )
    )
    settle = AsyncMock()
    monkeypatch.setattr(ai_budget, "reserve_cost", reserve)
    monkeypatch.setattr(ai_budget, "settle", settle)
    return SimpleNamespace(client=client, reserve=reserve, settle=settle)


async def generate(text="a penguin selling kebab", **kwargs):
    return await image.generate(
        text, model=kwargs.get("model", MODEL), provider=PROVIDER, cap_microusd=12000
    )


async def test_one_isolated_prompt_one_image_and_same_budget(api):
    assert await generate() == PNG
    assert [m for m, _, _ in api.client.calls] == ["GET", "POST"]
    request = api.client.calls[1][2]
    payload = request["json"]
    assert payload["n"] == 1 and payload["resolution"] == "1K"
    assert payload["provider"] == {
        "only": [PROVIDER],
        "allow_fallbacks": False,
    }
    # ImageGenerationProviderPreferences does NOT expose the text route's
    # max_price. Do not send an undocumented field and pretend it is enforced.
    assert "max_price" not in payload["provider"]
    assert set(payload) == {
        "model",
        "prompt",
        "n",
        "resolution",
        "aspect_ratio",
        "output_format",
        "provider",
    }
    assert "a penguin selling kebab" in payload["prompt"]
    assert request["allow_redirects"] is False
    assert "headers" not in api.client.calls[0][2]  # public discovery never needs a secret
    assert api.reserve.call_args.kwargs["estimated_microusd"] == 12000
    assert api.settle.call_args.kwargs["actual_microusd"] == 9000
    assert api.settle.call_args.kwargs["status"] == "completed"


@pytest.mark.parametrize("field,value", [("telephone_enabled", False), ("openrouter_api_key", "")])
async def test_disabled_makes_no_network_calls(api, monkeypatch, field, value):
    monkeypatch.setattr(image.settings, field, value)
    with pytest.raises(image.ImageError):
        await generate()
    assert not api.client.calls


@pytest.mark.parametrize(
    "text,model",
    [
        ("", MODEL),
        ("x" * 151, MODEL),
        ("hello", "https://evil.invalid/x"),
        ("hello", "../../evil"),
        ("hello", "../.."),
    ],
)
async def test_invalid_inputs_make_no_calls(api, text, model):
    with pytest.raises(image.ImageError):
        await generate(text, model=model)
    assert not api.client.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_tag": "other"},
        {"supported_parameters": {}},
        {"supported_parameters": {"resolution": {"values": ["1K"]}}},
        {"pricing": []},
        {"pricing": [{"billable": "output_image", "unit": "megapixel", "cost_usd": 0.001}]},
        {"pricing": [{"billable": "output_image", "unit": "image", "cost_usd": 0.02}]},
        {"pricing": [{"billable": "output_image", "unit": "image", "cost_usd": 0}]},
        {
            "pricing": [
                {"billable": "output_image", "unit": "image", "cost_usd": 0.001, "variant": "4k"}
            ]
        },
    ],
)
async def test_preflight_fails_closed_for_unknown_or_expensive_billing(api, changes):
    api.client.preflight = Response(discovery(**changes))
    with pytest.raises(image.ImageError):
        await generate()
    api.reserve.assert_not_awaited()
    assert len(api.client.calls) == 1


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "-0.1", "broken"])
def test_invalid_prices(value):
    with pytest.raises(image.ImageError):
        image._money(value)


@pytest.mark.parametrize(
    "response",
    [
        Response(status=503),
        Response(error=TimeoutError()),
        Response(body=b"[]"),
        Response(body=b"not json"),
    ],
)
async def test_discovery_errors_never_reserve(api, response):
    api.client.preflight = response
    with pytest.raises(image.ImageError):
        await generate()
    api.reserve.assert_not_awaited()


async def test_budget_failure_never_posts(api):
    api.reserve.side_effect = ai_budget.AIBudgetExceeded("test")
    with pytest.raises(ai_budget.AIBudgetExceeded):
        await generate()
    assert len(api.client.calls) == 1
    api.settle.assert_not_awaited()


async def test_admin_command_has_distinct_audit_feature_but_same_budget(api):
    assert await image.generate("a penguin",model=MODEL,provider=PROVIDER,cap_microusd=12000,feature="admin_image") == PNG
    assert api.reserve.call_args.kwargs["feature"] == "admin_image"


@pytest.mark.parametrize(
    "response,charge,status",
    [
        (Response(status=429), 0, "rejected"),
        (Response(status=503), None, "uncertain"),
        (Response(error=TimeoutError()), None, "uncertain"),
        (Response(error=aiohttp.ClientError()), None, "uncertain"),
        (Response(body=b"not json"), None, "uncertain"),
    ],
)
async def test_generation_failure_never_retries_and_settles(api, response, charge, status):
    api.client.output = response
    with pytest.raises(image.ImageError):
        await generate()
    assert len(api.client.calls) == 2
    assert api.settle.call_args.kwargs["actual_microusd"] == charge
    assert api.settle.call_args.kwargs["status"] == status


async def test_cancellation_conservatively_settles(api):
    api.client.output = Response(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await generate()
    api.settle.assert_awaited_once()
    assert api.settle.call_args.kwargs["actual_microusd"] is None


@pytest.mark.parametrize("cost", [None, 0.02])
async def test_missing_usage_is_conservative_and_overrun_is_recorded(api, cost, caplog):
    api.client.output = Response(
        {"data": [{"b64_json": base64.b64encode(PNG).decode()}], "usage": {"cost": cost}}
    )
    assert await generate() == PNG
    assert api.settle.call_args.kwargs["actual_microusd"] == (None if cost is None else 20000)
    if cost:
        assert "exceeded preflight" in caplog.text


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"data": []},
        {"data": [{"b64_json": "bad"}]},
        {"data": [{"b64_json": "YWJjZA=="}]},
        {"data": [{"b64_json": "bad", "media_type": "image/svg+xml"}]},
    ],
)
def test_invalid_images_are_not_telegram_media(data):
    with pytest.raises(image.ImageError):
        image._decode(data)


async def test_bounded_response_and_non_object_json(api, monkeypatch):
    monkeypatch.setattr(image, "_MAX_RESPONSE", 3)
    with pytest.raises(image.ImageError, match="too large"):
        await image._read_json(Response(body=b"1234"))
    with pytest.raises(image.ImageError, match="Invalid image response"):
        await image._read_json(Response(body=b"[]"))
