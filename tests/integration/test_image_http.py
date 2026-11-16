"""Exercise the real aiohttp transport against a local fault-injection server."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
import pytest

from services import ai_budget, image_service as image

PNG = b"\x89PNG\r\n\x1a\nfake-local-http-image"


@pytest.fixture
async def http_api(monkeypatch):
    control = SimpleNamespace(
        status=200, discovery_status=200, delay=False, calls=[], release=asyncio.Event()
    )

    async def discovery(request):
        return web.json_response(
            {
                "endpoints": [
                    {
                        "provider_tag": "test",
                        "supported_parameters": {
                            "resolution": {"values": ["1K"]},
                            "output_format": {"values": ["png"]},
                        },
                        "pricing": [
                            {"billable": "output_image", "unit": "image", "cost_usd": 0.009}
                        ],
                    }
                ]
            },
            status=control.discovery_status,
        )

    async def generate(request):
        control.calls.append(await request.json())
        if control.delay:
            await control.release.wait()
            return web.Response(status=503)
        if control.status != 200:
            return web.json_response(
                {"error": {"message": "SECRET-DO-NOT-LOG private scene"}}, status=control.status
            )
        body = json.dumps(
            {
                "data": [{"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}],
                "usage": {"cost": 0.009},
            }
        ).encode()
        r = web.StreamResponse(headers={"Content-Type": "application/json"})
        await r.prepare(request)
        await r.write(b" \n" + body[:40])
        await r.write(body[40:])
        await r.write_eof()
        return r

    app = web.Application()
    app.router.add_get("/images/models/test/image/endpoints", discovery)
    app.router.add_post("/images", generate)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(image, "_API", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(image.settings, "telephone_enabled", True)
    monkeypatch.setattr(image.settings, "openrouter_api_key", "SECRET-DO-NOT-LOG")
    control.reserve = AsyncMock(
        return_value=ai_budget.Reservation(
            "http-test", "2026-11", "telephone_image", "openrouter_other", 12000
        )
    )
    control.settle = AsyncMock()
    monkeypatch.setattr(ai_budget, "reserve_cost", control.reserve)
    monkeypatch.setattr(ai_budget, "settle", control.settle)
    try:
        yield control
    finally:
        control.release.set()
        await runner.cleanup()


async def call():
    return await image.generate(
        "private scene", model="test/image", provider="test", cap_microusd=12000
    )


async def test_real_chunked_json_transport(http_api):
    assert await call() == PNG
    assert len(http_api.calls) == 1
    assert http_api.settle.call_args.kwargs["actual_microusd"] == 9000


@pytest.mark.parametrize(
    "status,retryable", [(400, False), (401, False), (429, True), (502, True), (503, True)]
)
async def test_real_http_errors_are_safe_classified_and_never_auto_retried(
    http_api, status, retryable, caplog
):
    http_api.status = status
    with pytest.raises(image.ImageError) as caught:
        await call()
    assert caught.value.stage == "generation" and caught.value.http_status == status
    assert caught.value.kind == "http" and caught.value.retryable is retryable
    assert len(http_api.calls) == 1
    assert http_api.settle.await_count == 1
    assert "SECRET-DO-NOT-LOG" not in caplog.text and "private scene" not in caplog.text


async def test_real_transport_timeout_is_recoverable_but_not_automatically_retried(
    http_api, monkeypatch
):
    http_api.delay = True
    monkeypatch.setattr(image.settings, "telephone_image_timeout_seconds", 0.05)
    with pytest.raises(image.ImageError) as caught:
        await call()
    assert caught.value.kind == "timeout" and caught.value.retryable
    assert len(http_api.calls) == 1
    assert http_api.settle.call_args.kwargs["actual_microusd"] is None


async def test_real_discovery_error_reserves_nothing(http_api):
    http_api.discovery_status = 503
    with pytest.raises(image.ImageError) as caught:
        await call()
    assert caught.value.stage == "discovery" and caught.value.retryable
    http_api.reserve.assert_not_awaited()
    assert not http_api.calls
