from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from services import tavern_reactions as reactions
from utils import cooldown

KEY = "cons_nuka_cola"


@pytest.fixture(autouse=True)
def gateway(monkeypatch):
    cooldown.reset()
    monkeypatch.setattr(reactions.settings, "openrouter_api_key", "fake")
    generate = AsyncMock(return_value="Checkpoint dello spuntino raggiunto!")
    monkeypatch.setattr(reactions.ai_service, "generate_completion", generate)
    yield generate
    cooldown.reset()


async def test_light_route_is_small_and_sends_only_catalog_data(gateway):
    assert await reactions.generate_reaction(KEY, 123456) == gateway.return_value
    args, kwargs = gateway.call_args
    assert kwargs == {"max_tokens": 80, "tier": "light"}
    assert set(json.loads(args[1])) == {"oggetto", "descrizione"}
    assert "123456" not in args[0] + args[1]
    assert "nomi di persone" in args[0]


@pytest.mark.parametrize("reason", ["missing_key", "unknown_item", "cooldown"])
async def test_no_call_when_flavour_is_unavailable(gateway, monkeypatch, reason):
    key = KEY
    if reason == "missing_key":
        monkeypatch.setattr(reactions.settings, "openrouter_api_key", "")
    elif reason == "unknown_item":
        key = "removed"
    else:
        cooldown.mark("tavern_reaction", 1)
    assert await reactions.generate_reaction(key, 1) == reactions.FALLBACK_REACTION
    gateway.assert_not_awaited()


async def test_parallel_consumptions_share_cooldown_without_blocking_other_people(gateway):
    first, second = await asyncio.gather(
        reactions.generate_reaction(KEY, 1), reactions.generate_reaction(KEY, 1),
    )
    assert first == gateway.return_value and second == reactions.FALLBACK_REACTION
    assert await reactions.generate_reaction(KEY, 2) == gateway.return_value
    assert gateway.await_count == 2


@pytest.mark.parametrize("text,expected", [("  \n ", None), ("a\n\tb", "a b"), ("x" * 500, "x" * 180)])
async def test_empty_or_long_output_is_bounded(gateway, text, expected):
    gateway.return_value = text
    assert await reactions.generate_reaction(KEY, 1) == (expected or reactions.FALLBACK_REACTION)


@pytest.mark.parametrize("error", [reactions.ai_service.AIServiceError("SECRET"), RuntimeError("SECRET")])
async def test_failures_do_not_retry_or_leak_provider_content(gateway, error, caplog):
    caplog.set_level("INFO")
    gateway.side_effect = error
    assert await reactions.generate_reaction(KEY, 1) == reactions.FALLBACK_REACTION
    gateway.assert_awaited_once()
    assert "SECRET" not in caplog.text


async def test_cancellation_does_not_turn_into_retry(gateway):
    gateway.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await reactions.generate_reaction(KEY, 1)
    assert await reactions.generate_reaction(KEY, 1) == reactions.FALLBACK_REACTION
    gateway.assert_awaited_once()
