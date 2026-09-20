from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from services.ai_routing import ProviderAttempt, ProviderUnavailable, TextAIRouter


def attempt(name, generate, timeout=1):
    return ProviderAttempt(name, "model", generate, timeout)


async def test_free_success_never_invokes_paid_provider():
    free, paid = AsyncMock(return_value="free"), AsyncMock(return_value="paid")
    assert await TextAIRouter().generate(
        [attempt("free", free), attempt("paid", paid)],
        workload="chat", deadline_seconds=2,
    ) == "free"
    paid.assert_not_awaited()


async def test_breaker_skips_failures_until_expiry_and_isolates_workloads():
    now = [0.0]
    router = TextAIRouter(clock=lambda: now[0])
    free = AsyncMock(side_effect=ProviderUnavailable("private error body"))
    paid = AsyncMock(return_value="paid")
    chain = [attempt("free", free), attempt("paid", paid)]
    for _ in range(2):
        assert await router.generate(chain, workload="chat", deadline_seconds=2) == "paid"
    assert free.await_count == 1
    await router.generate(chain, workload="fun", deadline_seconds=2)
    assert free.await_count == 2
    now[0] = 61
    await router.generate(chain, workload="chat", deadline_seconds=2)
    assert free.await_count == 3
    router.reset()
    await router.generate(chain, workload="chat", deadline_seconds=2)
    assert free.await_count == 4


async def test_provider_timeout_fails_over_without_waiting_indefinitely():
    async def stalled():
        await asyncio.Event().wait()

    paid = AsyncMock(return_value="paid")
    assert await TextAIRouter().generate(
        [attempt("free", stalled, .01), attempt("paid", paid)],
        workload="chat", deadline_seconds=1,
    ) == "paid"


async def test_absolute_deadline_prevents_an_extra_paid_attempt():
    now = [0.0]

    async def down():
        now[0] = 3
        raise ProviderUnavailable("down")

    paid = AsyncMock(return_value="paid")
    with pytest.raises(ProviderUnavailable, match="all text providers"):
        await TextAIRouter(clock=lambda: now[0]).generate(
            [attempt("free", down), attempt("paid", paid)],
            workload="chat", deadline_seconds=2,
        )
    paid.assert_not_awaited()


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), ValueError("bug")])
async def test_cancellation_and_programming_errors_never_trigger_paid(failure):
    paid = AsyncMock()
    with pytest.raises(type(failure)):
        await TextAIRouter().generate(
            [attempt("free", AsyncMock(side_effect=failure)), attempt("paid", paid)],
            workload="chat", deadline_seconds=2,
        )
    paid.assert_not_awaited()


async def test_all_providers_down_raise_sanitized_error(caplog):
    router = TextAIRouter()
    chain = [attempt("free", AsyncMock(side_effect=ProviderUnavailable("secret")))]
    for _ in range(2):
        with pytest.raises(ProviderUnavailable, match="all text providers"):
            await router.generate(chain, workload="chat", deadline_seconds=1)
    assert "secret" not in caplog.text
