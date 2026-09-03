from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields
from enum import Enum
import importlib
import logging
from typing import Any, cast

import pytest

from services import ai_budget, structured_ai


def _module():
    return importlib.import_module("services.structured_ai_router")


@pytest.fixture(autouse=True)
def _isolate_cached_factory():
    try:
        module = _module()
    except ModuleNotFoundError:
        yield
        return
    module._reset_twenty_questions_router_for_tests()
    yield
    module._reset_twenty_questions_router_for_tests()


@pytest.fixture
def structured_request() -> structured_ai.StructuredRequest:
    return structured_ai.StructuredRequest(
        operation="twentyq_question",
        system_prompt="system-secret",
        user_prompt='{"dossier":"private","question":"q"}',
        schema_name="twentyq_verdict",
        schema={"type": "object"},
        prompt_version="v2",
        schema_version="v2",
    )


class Verdict(str, Enum):
    si = "si"
    no = "no"


def parse_verdict(value: dict[str, Any]) -> Verdict:
    return Verdict(value.get("verdetto"))


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeProvider:
    def __init__(
        self,
        name: str,
        *behaviors: dict[str, Any] | BaseException,
        configured: bool = True,
        model: str | None = None,
        timeout_seconds: float = 8,
        clock: FakeClock | None = None,
        elapsed: float = 0,
    ) -> None:
        self.name = cast(structured_ai.ProviderName, name)
        self.model = model or f"{name}/test"
        self.timeout_seconds = timeout_seconds
        self.configured = configured
        self.behaviors = behaviors or ({"verdetto": "si"},)
        self.clock = clock
        self.elapsed = elapsed
        self.calls = 0

    async def generate_json(
        self, structured_request: structured_ai.StructuredRequest,
    ) -> structured_ai.StructuredProviderResult:
        del structured_request
        index = min(self.calls, len(self.behaviors) - 1)
        behavior = self.behaviors[index]
        self.calls += 1
        if self.clock is not None:
            self.clock.advance(self.elapsed)
        if isinstance(behavior, BaseException):
            raise behavior
        return structured_ai.StructuredProviderResult(
            behavior,
            self.name,
            self.model,
            ai_budget.UsageMetrics(self.model, 12, 3, 2, 1),
            17 if self.name == "openrouter" else None,
        )


class ForgedMetadataProvider(FakeProvider):
    def __init__(self, name: str, *, raw_provider: str, raw_model: str) -> None:
        super().__init__(name)
        self._raw_provider = raw_provider
        self._raw_model = raw_model

    async def generate_json(
        self, structured_request: structured_ai.StructuredRequest,
    ) -> structured_ai.StructuredProviderResult:
        del structured_request
        self.calls += 1
        return structured_ai.StructuredProviderResult(
            {"verdetto": "si"},
            cast(structured_ai.ProviderName, self._raw_provider),
            self._raw_model,
            ai_budget.UsageMetrics(self._raw_model, 12, 3, 2, 1),
            17,
        )


def test_attempt_contract_is_immutable_and_prompt_free():
    module = _module()
    record = module.ProviderAttemptRecord(
        session_id=7,
        operation="twentyq_question",
        provider="gemini",
        model="gemini/test",
        prompt_version="v2",
        schema_version="v2",
        outcome="success",
        error_kind=None,
        latency_ms=3,
        usage=ai_budget.UsageMetrics(),
        cost_microusd=None,
    )

    assert {field.name for field in fields(record)} == {
        "session_id", "operation", "provider", "model", "prompt_version",
        "schema_version", "outcome", "error_kind", "latency_ms", "usage",
        "cost_microusd",
    }
    with pytest.raises(FrozenInstanceError):
        record.outcome = "changed"


async def test_invalid_enum_falls_through_once_and_free_success_skips_paid(
    structured_request,
):
    module = _module()
    gemini = FakeProvider("gemini", {"verdetto": "sì"})
    groq = FakeProvider("groq", {"verdetto": "si"})
    openrouter = FakeProvider("openrouter", {"verdetto": "no"})
    recorded = []

    async def record(attempt):
        recorded.append(attempt)

    router = module.StructuredAIRouter(
        providers=(gemini, groq, openrouter), deadline_seconds=25, recorder=record,
    )
    got = await router.generate(structured_request, session_id=7, validate=parse_verdict)

    assert got.value is Verdict.si
    assert got.provider == "groq"
    assert (gemini.calls, groq.calls, openrouter.calls) == (1, 1, 0)
    assert [attempt.outcome for attempt in got.attempts] == ["invalid_enum", "success"]
    assert recorded == list(got.attempts)
    assert all(not hasattr(attempt, "prompt") for attempt in recorded)


async def test_configured_order_skips_unconfigured_and_attempts_each_provider_once(
    structured_request,
):
    module = _module()
    skipped = FakeProvider("gemini", configured=False)
    groq = FakeProvider(
        "groq",
        structured_ai.StructuredAIError(
            "safe failure", kind=structured_ai.StructuredAIErrorKind.refusal,
            provider="groq",
        ),
    )
    paid = FakeProvider("openrouter", {"verdetto": "no"})
    router = module.StructuredAIRouter(
        providers=(skipped, groq, paid), deadline_seconds=25,
    )

    got = await router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    )

    assert got.value is Verdict.no
    assert (skipped.calls, groq.calls, paid.calls) == (0, 1, 1)
    assert [attempt.outcome for attempt in got.attempts] == ["failure", "success"]
    assert got.attempts[0].error_kind == "refusal"


async def test_type_error_from_domain_validation_falls_through(structured_request):
    module = _module()
    first = FakeProvider("gemini", {"unexpected": True})
    second = FakeProvider("groq", {"verdetto": "si"})
    validations = 0

    def validate(value):
        nonlocal validations
        validations += 1
        if "verdetto" not in value:
            raise TypeError("private domain detail")
        return parse_verdict(value)

    got = await module.StructuredAIRouter(
        providers=(first, second), deadline_seconds=25,
    ).generate(structured_request, session_id=None, validate=validate, audit=False)

    assert got.value is Verdict.si
    assert validations == 2
    assert [item.outcome for item in got.attempts] == ["invalid_schema", "success"]


async def test_unexpected_adapter_value_error_propagates_without_audit_or_fallback(
    structured_request,
):
    module = _module()
    sentinel = ValueError("adapter implementation bug")
    broken = FakeProvider("gemini", sentinel)
    fallback = FakeProvider("groq", {"verdetto": "si"})
    recorded = []

    async def record(attempt):
        recorded.append(attempt)

    router = module.StructuredAIRouter(
        providers=(broken, fallback), deadline_seconds=25, recorder=record,
    )
    with pytest.raises(ValueError) as raised:
        await router.generate(
            structured_request, session_id=7, validate=parse_verdict,
        )

    assert raised.value is sentinel
    assert (broken.calls, fallback.calls) == (1, 0)
    assert recorded == []


async def test_expected_provider_errors_fall_through_to_prompt_free_aggregate(structured_request):
    module = _module()
    providers = tuple(
        FakeProvider(
            name,
            structured_ai.StructuredAIError(
                "provider safe failure",
                kind=structured_ai.StructuredAIErrorKind.refusal,
                provider=cast(structured_ai.ProviderName, name),
            ),
        )
        for name in ("gemini", "groq", "openrouter")
    )
    router = module.StructuredAIRouter(providers=providers, deadline_seconds=25)

    with pytest.raises(structured_ai.StructuredAIError) as raised:
        await router.generate(structured_request, session_id=None, validate=parse_verdict, audit=False)

    assert raised.value.kind is structured_ai.StructuredAIErrorKind.providers_unavailable
    assert all(provider.calls == 1 for provider in providers)
    assert structured_request.system_prompt not in str(raised.value)
    assert structured_request.user_prompt not in str(raised.value)


async def test_cancelled_error_propagates_without_audit_or_fallback(structured_request):
    module = _module()
    cancelled = FakeProvider("gemini", asyncio.CancelledError())
    fallback = FakeProvider("groq", {"verdetto": "si"})
    recorded = []

    async def record(attempt):
        recorded.append(attempt)

    router = module.StructuredAIRouter(
        providers=(cancelled, fallback), deadline_seconds=25, recorder=record,
    )
    with pytest.raises(asyncio.CancelledError):
        await router.generate(structured_request, session_id=7, validate=parse_verdict)

    assert (cancelled.calls, fallback.calls) == (1, 0)
    assert recorded == []


async def test_audit_false_suppresses_records_but_keeps_returned_attempts(structured_request):
    module = _module()
    recorded = []

    async def record(attempt):
        recorded.append(attempt)

    got = await module.StructuredAIRouter(
        providers=(FakeProvider("gemini"),),
        deadline_seconds=25,
        recorder=record,
    ).generate(structured_request, session_id=7, validate=parse_verdict, audit=False)

    assert [item.outcome for item in got.attempts] == ["success"]
    assert recorded == []


async def test_audit_failure_never_replaces_valid_result_or_leaks_exception(
    structured_request, caplog,
):
    module = _module()
    secret = "audit-db-private-body"
    forged_model = "provider-model-secret\nforged-log-line"

    async def broken(_attempt):
        raise RuntimeError(secret)

    with caplog.at_level(logging.ERROR):
        got = await module.StructuredAIRouter(
            providers=(FakeProvider("gemini", model=forged_model),),
            deadline_seconds=25,
            recorder=broken,
        ).generate(structured_request, session_id=7, validate=parse_verdict)

    assert got.value is Verdict.si
    assert secret not in caplog.text
    assert forged_model not in caplog.text
    assert "forged-log-line" not in caplog.text
    assert structured_request.system_prompt not in caplog.text
    assert structured_request.user_prompt not in caplog.text
    assert "provider=gemini" in caplog.text


async def test_audit_log_allowlists_a_malformed_adapter_provider(structured_request, caplog):
    module = _module()

    async def broken(_attempt):
        raise RuntimeError("db unavailable")

    with caplog.at_level(logging.ERROR):
        await module.StructuredAIRouter(
            providers=(FakeProvider("gemini\nforged-log-line"),),
            deadline_seconds=25,
            recorder=broken,
        ).generate(structured_request, session_id=7, validate=parse_verdict)

    assert "forged-log-line" not in caplog.text
    assert "provider=unknown" in caplog.text


async def test_audit_failure_does_not_stop_provider_fallback(structured_request):
    module = _module()

    async def broken(_attempt):
        raise RuntimeError("db unavailable")

    first = FakeProvider(
        "gemini",
        structured_ai.StructuredAIError(
            "down", kind=structured_ai.StructuredAIErrorKind.network,
            provider="gemini",
        ),
    )
    second = FakeProvider("groq", {"verdetto": "si"})
    got = await module.StructuredAIRouter(
        providers=(first, second), deadline_seconds=25, recorder=broken,
    ).generate(structured_request, session_id=7, validate=parse_verdict)

    assert got.value is Verdict.si
    assert (first.calls, second.calls) == (1, 1)


async def test_hanging_audit_cannot_delay_a_valid_result_past_route_deadline(
    structured_request,
):
    module = _module()
    never = asyncio.Event()

    async def hanging(_attempt):
        await never.wait()

    got = await asyncio.wait_for(
        module.StructuredAIRouter(
            providers=(FakeProvider("gemini"),), deadline_seconds=0.01,
            recorder=hanging,
        ).generate(structured_request, session_id=7, validate=parse_verdict),
        timeout=0.1,
    )

    assert got.value is Verdict.si


async def test_domain_structured_error_retains_raw_usage_without_opening_breaker(
    structured_request,
):
    module = _module()
    first = FakeProvider("gemini", {"verdetto": "sì"}, {"verdetto": "si"})
    second = FakeProvider("groq", {"verdetto": "no"})

    def domain_validate(value):
        if value["verdetto"] == "sì":
            raise structured_ai.StructuredAIError(
                "invalid verdict", kind=structured_ai.StructuredAIErrorKind.invalid_enum,
            )
        return parse_verdict(value)

    router = module.StructuredAIRouter(
        providers=(first, second), deadline_seconds=25,
    )
    first_result = await router.generate(
        structured_request, session_id=None, validate=domain_validate, audit=False,
    )
    second_result = await router.generate(
        structured_request, session_id=None, validate=domain_validate, audit=False,
    )

    assert first_result.attempts[0].outcome == "invalid_enum"
    assert first_result.attempts[0].usage.prompt_tokens == 12
    assert first_result.attempts[0].cost_microusd is None
    assert (first_result.value, second_result.value) == (Verdict.no, Verdict.si)
    assert (first.calls, second.calls) == (2, 1)


async def test_unexpected_validation_structured_error_propagates_without_fallback(
    structured_request,
):
    module = _module()
    first = FakeProvider("gemini")
    second = FakeProvider("groq")
    sentinel = structured_ai.StructuredAIError(
        "validator infrastructure failure", kind=structured_ai.StructuredAIErrorKind.network,
    )

    def fail_validation(_value):
        raise sentinel

    with pytest.raises(structured_ai.StructuredAIError) as raised:
        await module.StructuredAIRouter(
            providers=(first, second), deadline_seconds=25,
        ).generate(structured_request, session_id=None, validate=fail_validation, audit=False)

    assert raised.value is sentinel
    assert (first.calls, second.calls) == (1, 0)


async def test_forged_result_metadata_is_rejected_or_canonicalized_before_audit(
    structured_request,
):
    module = _module()
    forged = ForgedMetadataProvider(
        "gemini", raw_provider="openrouter", raw_model="forged\nmodel",
    )
    fallback = FakeProvider("groq")
    recorded = []

    async def record(attempt):
        recorded.append(attempt)

    got = await module.StructuredAIRouter(
        providers=(forged, fallback), deadline_seconds=25, recorder=record,
    ).generate(structured_request, session_id=7, validate=parse_verdict)

    assert got.value is Verdict.si
    assert (forged.calls, fallback.calls) == (1, 1)
    assert [(item.provider, item.model) for item in recorded] == [
        ("gemini", "gemini/test"), ("groq", "groq/test"),
    ]
    assert recorded[0].outcome == "invalid_schema"


async def test_success_uses_attempted_adapter_metadata_not_raw_model(structured_request):
    module = _module()
    provider = ForgedMetadataProvider(
        "gemini", raw_provider="gemini", raw_model="forged\nmodel",
    )
    recorded = []

    async def record(attempt):
        recorded.append(attempt)

    got = await module.StructuredAIRouter(
        providers=(provider,), deadline_seconds=25, recorder=record,
    ).generate(structured_request, session_id=7, validate=parse_verdict)

    assert (got.provider, got.model) == ("gemini", "gemini/test")
    assert (recorded[0].provider, recorded[0].model) == ("gemini", "gemini/test")


async def test_concurrent_breaker_failures_keep_the_longest_open_deadline(
    structured_request,
):
    module = _module()
    clock = FakeClock()

    class ConcurrentProvider(FakeProvider):
        def __init__(self):
            super().__init__("gemini")
            self.waiters: list[asyncio.Future[BaseException]] = []
            self.ready = asyncio.Event()

        async def generate_json(self, _request):
            self.calls += 1
            if self.calls <= 2:
                waiter = asyncio.get_running_loop().create_future()
                self.waiters.append(waiter)
                if len(self.waiters) == 2:
                    self.ready.set()
                raise await waiter
            return structured_ai.StructuredProviderResult(
                {"verdetto": "si"}, self.name, self.model,
                ai_budget.UsageMetrics(), None,
            )

    provider = ConcurrentProvider()
    router = module.StructuredAIRouter(
        providers=(provider,), deadline_seconds=25, clock=clock,
    )
    first = asyncio.create_task(router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    ))
    second = asyncio.create_task(router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    ))
    await provider.ready.wait()
    provider.waiters[0].set_result(structured_ai.StructuredAIError(
        "quota", kind=structured_ai.StructuredAIErrorKind.quota,
    ))
    await asyncio.sleep(0)
    clock.advance(1)
    provider.waiters[1].set_result(structured_ai.StructuredAIError(
        "network", kind=structured_ai.StructuredAIErrorKind.network,
    ))
    await asyncio.gather(first, second, return_exceptions=True)
    clock.advance(60)

    with pytest.raises(structured_ai.StructuredAIError) as raised:
        await router.generate(
            structured_request, session_id=None, validate=parse_verdict, audit=False,
        )

    assert raised.value.kind is structured_ai.StructuredAIErrorKind.providers_unavailable
    assert provider.calls == 2


async def test_asyncio_timeout_uses_8_8_9_budget_and_deadline_is_typed(
    monkeypatch, structured_request,
):
    module = _module()
    clock = FakeClock()
    timeouts: list[float] = []

    class TimeoutAtExit:
        def __init__(self, seconds: float) -> None:
            self.seconds = seconds

        async def __aenter__(self):
            timeouts.append(self.seconds)
            clock.advance(self.seconds)

        async def __aexit__(self, exc_type, exc, traceback):
            del exc, traceback
            if exc_type is None:
                raise TimeoutError
            return False

    monkeypatch.setattr(module.asyncio, "timeout", TimeoutAtExit)
    providers = (
        FakeProvider("gemini", timeout_seconds=8),
        FakeProvider("groq", timeout_seconds=8),
        FakeProvider("openrouter", timeout_seconds=12),
    )
    recorded = []

    async def record(attempt):
        recorded.append(attempt)

    router = module.StructuredAIRouter(
        providers=providers, deadline_seconds=25, recorder=record, clock=clock,
    )
    with pytest.raises(structured_ai.StructuredAIError) as raised:
        await router.generate(structured_request, session_id=7, validate=parse_verdict)

    assert raised.value.kind is structured_ai.StructuredAIErrorKind.deadline
    assert timeouts == [8, 8, 9]
    assert [attempt.error_kind for attempt in recorded] == ["timeout"] * 2
    assert [attempt.latency_ms for attempt in recorded] == [8000, 8000]


async def test_absolute_deadline_stops_before_next_provider(structured_request):
    module = _module()
    clock = FakeClock()
    first = FakeProvider(
        "gemini",
        structured_ai.StructuredAIError(
            "down", kind=structured_ai.StructuredAIErrorKind.network,
            provider="gemini",
        ),
        clock=clock,
        elapsed=25,
    )
    second = FakeProvider("groq")

    with pytest.raises(structured_ai.StructuredAIError) as raised:
        await module.StructuredAIRouter(
            providers=(first, second), deadline_seconds=25, clock=clock,
        ).generate(structured_request, session_id=None, validate=parse_verdict, audit=False)

    assert raised.value.kind is structured_ai.StructuredAIErrorKind.deadline
    assert (first.calls, second.calls) == (1, 0)


@pytest.mark.parametrize(
    ("kind", "delay"),
    [
        ("rate_limit", 60),
        ("timeout", 60),
        ("network", 60),
        ("server", 60),
        ("quota", 900),
        ("configuration", 900),
        ("authentication", 900),
        ("missing_key", 900),
        ("budget_exhausted", 900),
        ("budget_unavailable", 900),
    ],
)
async def test_breaker_default_delay_matrix_is_observable(structured_request, kind, delay):
    module = _module()
    clock = FakeClock()
    error_kind = structured_ai.StructuredAIErrorKind(kind)
    primary = FakeProvider(
        "gemini",
        structured_ai.StructuredAIError(
            "safe", kind=error_kind, provider="gemini",
        ),
        {"verdetto": "si"},
    )
    fallback = FakeProvider("groq", {"verdetto": "no"})
    router = module.StructuredAIRouter(
        providers=(primary, fallback), deadline_seconds=25, clock=clock,
    )

    first = await router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    )
    second = await router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    )
    clock.advance(delay)
    third = await router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    )

    assert (first.value, second.value, third.value) == (
        Verdict.no, Verdict.no, Verdict.si,
    )
    assert (primary.calls, fallback.calls) == (2, 2)


async def test_retry_after_is_authoritative_for_breaker(structured_request):
    module = _module()
    clock = FakeClock()
    primary = FakeProvider(
        "gemini",
        structured_ai.StructuredAIError(
            "slow",
            kind=structured_ai.StructuredAIErrorKind.rate_limit,
            provider="gemini",
            retry_after_seconds=7,
        ),
        {"verdetto": "si"},
    )
    fallback = FakeProvider("groq", {"verdetto": "no"})
    router = module.StructuredAIRouter(
        providers=(primary, fallback), deadline_seconds=25, clock=clock,
    )

    assert (await router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    )).value is Verdict.no
    clock.advance(6.999)
    assert (await router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    )).value is Verdict.no
    clock.advance(0.001)
    assert (await router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    )).value is Verdict.si
    assert (primary.calls, fallback.calls) == (2, 2)


@pytest.mark.parametrize(
    "kind",
    ["refusal", "empty_output", "malformed_json", "invalid_schema",
     "invalid_enum", "output_limit"],
)
async def test_output_failures_do_not_open_global_breaker(structured_request, kind):
    module = _module()
    primary = FakeProvider(
        "gemini",
        structured_ai.StructuredAIError(
            "bad output",
            kind=structured_ai.StructuredAIErrorKind(kind),
            provider="gemini",
        ),
        {"verdetto": "si"},
    )
    fallback = FakeProvider("groq", {"verdetto": "no"})
    router = module.StructuredAIRouter(
        providers=(primary, fallback), deadline_seconds=25,
    )

    first = await router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    )
    second = await router.generate(
        structured_request, session_id=None, validate=parse_verdict, audit=False,
    )

    assert (first.value, second.value) == (Verdict.no, Verdict.si)
    assert (primary.calls, fallback.calls) == (2, 1)


async def test_breaker_key_includes_model(structured_request):
    module = _module()
    model_a = FakeProvider(
        "gemini",
        structured_ai.StructuredAIError(
            "down", kind=structured_ai.StructuredAIErrorKind.network,
            provider="gemini",
        ),
        model="gemini/model-a",
    )
    model_b = FakeProvider(
        "gemini", {"verdetto": "si"}, model="gemini/model-b",
    )
    router = module.StructuredAIRouter(
        providers=(model_a, model_b), deadline_seconds=25,
    )

    await router.generate(structured_request, session_id=None, validate=parse_verdict, audit=False)
    await router.generate(structured_request, session_id=None, validate=parse_verdict, audit=False)

    assert (model_a.calls, model_b.calls) == (1, 2)


async def test_factory_is_cached_preserves_breaker_and_has_explicit_reset(
    monkeypatch, structured_request,
):
    module = _module()
    gemini = FakeProvider(
        "gemini",
        structured_ai.StructuredAIError(
            "down", kind=structured_ai.StructuredAIErrorKind.network,
            provider="gemini",
        ),
    )
    groq = FakeProvider("groq", {"verdetto": "si"})
    paid = FakeProvider("openrouter", configured=False)
    monkeypatch.setattr(module, "GeminiStructuredProvider", lambda: gemini)
    monkeypatch.setattr(module, "GroqStructuredProvider", lambda: groq)
    monkeypatch.setattr(module, "OpenRouterStructuredProvider", lambda: paid)
    monkeypatch.setattr(module.settings, "twentyq_provider_order", "gemini,groq,openrouter")
    monkeypatch.setattr(module.settings, "twentyq_provider_deadline_seconds", 25)
    module._reset_twenty_questions_router_for_tests()

    first = module.get_twenty_questions_router()
    await first.generate(structured_request, session_id=None, validate=parse_verdict, audit=False)
    second = module.get_twenty_questions_router()
    await second.generate(structured_request, session_id=None, validate=parse_verdict, audit=False)

    assert first is second
    assert (gemini.calls, groq.calls, paid.calls) == (1, 2, 0)
    module._reset_twenty_questions_router_for_tests()
    assert module.get_twenty_questions_router() is not first


def test_factory_follows_normalized_settings_order_and_configuration(monkeypatch):
    module = _module()
    providers = {
        "gemini": FakeProvider("gemini", configured=False),
        "groq": FakeProvider("groq", configured=True),
        "openrouter": FakeProvider("openrouter", configured=False),
    }
    monkeypatch.setattr(module, "GeminiStructuredProvider", lambda: providers["gemini"])
    monkeypatch.setattr(module, "GroqStructuredProvider", lambda: providers["groq"])
    monkeypatch.setattr(
        module, "OpenRouterStructuredProvider", lambda: providers["openrouter"],
    )
    monkeypatch.setattr(module.settings, "twentyq_provider_order", "groq,gemini,openrouter")
    module._reset_twenty_questions_router_for_tests()

    router = module.get_twenty_questions_router()

    assert [provider.name for provider in router.providers] == [
        "groq", "gemini", "openrouter",
    ]
    assert module.has_configured_twenty_questions_provider() is True
