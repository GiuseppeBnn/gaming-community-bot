"""Local, bounded evaluation helpers for the secret-game question classifier."""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import json
from pathlib import Path
from time import monotonic
from typing import Literal, cast

from config_data.config import settings
from services import ai_budget
from services.ai_game_types import QuestionContextTurn, QuestionVerdict
from services.structured_ai import (
    GeminiStructuredProvider,
    GroqStructuredProvider,
    OpenRouterStructuredProvider,
    ProviderName,
    StructuredAIError,
    StructuredAIProvider,
)
from services.structured_ai_router import StructuredAIRouter
from services.twenty_questions_ai import (
    build_question_request,
    parse_question_verdict,
    select_question_context,
)
from services.twenty_questions_rules import normalize_turn_input

ProviderChoice = Literal["gemini", "groq", "openrouter", "chain"]
_MAX_CASES = 10_000
_MAX_LINE_BYTES = 32_768
_MAX_CASE_ID_CHARS = 128
_MAX_DOSSIER_BYTES = 16_384
_MAX_HISTORY_TURNS = 24
_MAX_QUESTION_CHARS = 500


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    dossier_json: str
    history: tuple[QuestionContextTurn, ...]
    question: str
    expected: QuestionVerdict


@dataclass(frozen=True, slots=True)
class EvalSummary:
    total: int
    schema_compliant: int
    correct: int
    consistency_failures: int
    latency_ms: Mapping[str, int]
    fallbacks: Mapping[str, int]
    errors: Mapping[str, int]
    usage: ai_budget.UsageMetrics
    cost_microusd: int


@dataclass(frozen=True, slots=True)
class EvalObservation:
    verdict: QuestionVerdict | None
    schema_compliant: bool
    consistent: bool
    provider: ProviderName | None
    latency_ms: int
    fallback_count: int
    error_kind: str | None
    usage: ai_budget.UsageMetrics
    cost_microusd: int


EvalRunner = Callable[[EvalCase], Awaitable[EvalObservation]]


def provider_names(
    choice: ProviderChoice, *, allow_paid_openrouter: bool,
) -> tuple[ProviderName, ...]:
    """Return the explicit provider sequence permitted for this eval run."""
    if choice == "openrouter":
        if not allow_paid_openrouter:
            raise ValueError("OpenRouter requires --allow-paid-openrouter")
        return ("openrouter",)
    if choice == "chain":
        return (
            ("gemini", "groq", "openrouter")
            if allow_paid_openrouter
            else ("gemini", "groq")
        )
    return (choice,)


def _invalid(line_no: int, message: str) -> ValueError:
    return ValueError(f"eval dataset line {line_no}: {message}")


def _bounded_text(
    value: object, *, line_no: int, field: str, limit: int,
) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _invalid(line_no, field)
    return value


def _load_history(value: object, *, line_no: int) -> tuple[QuestionContextTurn, ...]:
    if not isinstance(value, list) or len(value) > _MAX_HISTORY_TURNS:
        raise _invalid(line_no, "history")
    history: list[QuestionContextTurn] = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {
            "turn_no", "normalized_hash", "question", "verdict",
        }:
            raise _invalid(line_no, "history")
        turn_no = raw["turn_no"]
        normalized_hash = raw["normalized_hash"]
        if not isinstance(turn_no, int) or isinstance(turn_no, bool) or turn_no < 1:
            raise _invalid(line_no, "history")
        if normalized_hash is not None and (
            not isinstance(normalized_hash, str) or len(normalized_hash) > 128
        ):
            raise _invalid(line_no, "history")
        question = _bounded_text(
            raw["question"], line_no=line_no, field="history", limit=_MAX_QUESTION_CHARS,
        )
        try:
            verdict = QuestionVerdict(raw["verdict"])
        except (TypeError, ValueError) as exc:
            raise _invalid(line_no, "history") from exc
        history.append(QuestionContextTurn(turn_no, normalized_hash, question, verdict))
    if len({turn.turn_no for turn in history}) != len(history):
        raise _invalid(line_no, "history")
    return tuple(history)


def load_cases(path: Path) -> tuple[EvalCase, ...]:
    """Load a finite, strictly validated synthetic JSONL corpus."""
    cases: list[EvalCase] = []
    case_ids: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line_no, line in enumerate(source, start=1):
            if not line.strip() or len(line.encode("utf-8")) > _MAX_LINE_BYTES:
                raise _invalid(line_no, "record")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _invalid(line_no, "record") from exc
            if not isinstance(raw, dict) or set(raw) != {
                "case_id", "dossier", "history", "question", "expected",
            }:
                raise _invalid(line_no, "record")
            case_id = _bounded_text(
                raw["case_id"], line_no=line_no, field="case_id", limit=_MAX_CASE_ID_CHARS,
            )
            if case_id in case_ids:
                raise _invalid(line_no, "duplicate case_id")
            if not isinstance(raw["dossier"], dict):
                raise _invalid(line_no, "dossier")
            dossier_json = json.dumps(raw["dossier"], ensure_ascii=False, separators=(",", ":"))
            if len(dossier_json.encode("utf-8")) > _MAX_DOSSIER_BYTES:
                raise _invalid(line_no, "dossier")
            question = _bounded_text(
                raw["question"], line_no=line_no, field="question", limit=_MAX_QUESTION_CHARS,
            )
            try:
                expected = QuestionVerdict(raw["expected"])
            except (TypeError, ValueError) as exc:
                raise _invalid(line_no, "expected") from exc
            cases.append(EvalCase(
                case_id, dossier_json, _load_history(raw["history"], line_no=line_no), question,
                expected,
            ))
            case_ids.add(case_id)
            if len(cases) > _MAX_CASES:
                raise _invalid(line_no, "too many cases")
    if not cases:
        raise ValueError("eval dataset is empty")
    return tuple(cases)


def _sum_usage(metrics: Sequence[ai_budget.UsageMetrics]) -> ai_budget.UsageMetrics:
    def total(field: str) -> int | None:
        values = [getattr(metric, field) for metric in metrics]
        known = [value for value in values if value is not None]
        return sum(known) if known else None

    return ai_budget.UsageMetrics(
        prompt_tokens=total("prompt_tokens"),
        completion_tokens=total("completion_tokens"),
        reasoning_tokens=total("reasoning_tokens"),
        cached_tokens=total("cached_tokens"),
    )


async def run_cases(
    cases: Sequence[EvalCase], runner: EvalRunner, *, paid: bool = False,
) -> EvalSummary:
    """Run serially and retain only aggregate, prompt-free measurements.

    A paid run replaces per-attempt cost with the durable ``twentyq`` spent delta.
    This assumes the local eval is the only writer to that lane for its duration.
    """
    before = await ai_budget.feature_snapshot("twentyq") if paid else None
    observations = [await runner(case) for case in cases]
    latency: Counter[str] = Counter()
    fallbacks: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    for observation in observations:
        key = observation.provider or "unknown"
        latency[key] += observation.latency_ms
        if observation.fallback_count:
            fallbacks[key] += observation.fallback_count
        if observation.error_kind is not None:
            errors[observation.error_kind] += 1
    summary = EvalSummary(
        total=len(cases),
        schema_compliant=sum(observation.schema_compliant for observation in observations),
        correct=sum(
            observation.verdict is case.expected
            for case, observation in zip(cases, observations, strict=True)
        ),
        consistency_failures=sum(not observation.consistent for observation in observations),
        latency_ms=dict(latency),
        fallbacks=dict(fallbacks),
        errors=dict(errors),
        usage=_sum_usage([observation.usage for observation in observations]),
        cost_microusd=sum(observation.cost_microusd for observation in observations),
    )
    if not paid:
        return summary
    after = await ai_budget.feature_snapshot("twentyq")
    before_spent = before.spent_microusd if before is not None else 0
    after_spent = after.spent_microusd if after is not None else before_spent
    return replace(summary, cost_microusd=max(0, after_spent - before_spent))


def _is_consistent(case: EvalCase, verdict: QuestionVerdict) -> bool:
    question = normalize_turn_input(case.question)
    prior = [turn.verdict for turn in case.history if normalize_turn_input(turn.question) == question]
    return not prior or all(previous is verdict for previous in prior)


async def evaluate_case(case: EvalCase, router: StructuredAIRouter) -> EvalObservation:
    """Evaluate one case through the runtime builder/router without persistent audit."""
    context = select_question_context(
        case.history,
        case.question,
        max_turns=_MAX_HISTORY_TURNS,
        max_chars=settings.twentyq_context_chars,
    )
    request = build_question_request(
        dossier_json=case.dossier_json, current_question=case.question, context=context,
    )
    started = monotonic()
    try:
        result = await router.generate(
            request, session_id=None, validate=parse_question_verdict, audit=False,
        )
    except StructuredAIError as exc:
        return EvalObservation(
            None, False, False, exc.provider, max(0, int((monotonic() - started) * 1000)), 0,
            exc.kind.value, ai_budget.UsageMetrics(), 0,
        )
    attempts = result.attempts
    usage = _sum_usage([attempt.usage for attempt in attempts])
    return EvalObservation(
        result.value,
        True,
        _is_consistent(case, result.value),
        result.provider,
        max(0, int((monotonic() - started) * 1000)),
        max(0, len(attempts) - 1),
        None,
        usage,
        sum(attempt.cost_microusd or 0 for attempt in attempts),
    )


def require_provider_keys(names: Sequence[ProviderName]) -> None:
    """Reject an explicitly selected unconfigured provider without exposing a value."""
    variables = {
        "gemini": ("gemini_api_key", "GEMINI_API_KEY"),
        "groq": ("groq_api_key", "GROQ_API_KEY"),
        "openrouter": ("openrouter_api_key", "OPENROUTER_API_KEY"),
    }
    for name in names:
        setting_name, variable = variables[name]
        if not getattr(settings, setting_name):
            raise ValueError(variable)


def build_runtime_router(names: Sequence[ProviderName]) -> StructuredAIRouter:
    """Reuse runtime adapters while limiting the eval to its approved provider list."""
    available: dict[ProviderName, StructuredAIProvider] = {
        "gemini": cast(StructuredAIProvider, GeminiStructuredProvider()),
        "groq": cast(StructuredAIProvider, GroqStructuredProvider()),
        "openrouter": cast(StructuredAIProvider, OpenRouterStructuredProvider()),
    }
    return StructuredAIRouter(
        providers=tuple(available[name] for name in names),
        deadline_seconds=settings.twentyq_provider_deadline_seconds,
    )
