"""Contract tests for the local, aggregate-only twenty questions eval."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

from services import ai_budget
from services.ai_game_types import QuestionVerdict
from services.twenty_questions_eval import EvalCase, EvalObservation
import services.twenty_questions_eval as twenty_questions_eval


def _record(*, case_id: str = "case-1", expected: str = "si") -> dict[str, object]:
    return {
        "case_id": case_id,
        "dossier": {"facts": ["Il faro di vetro usa mappe stellari."]},
        "history": [{
            "turn_no": 1,
            "normalized_hash": "mappe",
            "question": "Usa mappe?",
            "verdict": "si",
        }],
        "question": "Ci sono mappe stellari?",
        "expected": expected,
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _cli_module():
    path = Path(__file__).resolve().parents[2] / "scripts/eval_twenty_questions.py"
    spec = importlib.util.spec_from_file_location("twentyq_eval_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openrouter_requires_explicit_paid_flag():
    """Removing the paid guard would allow an accidental charge."""
    with pytest.raises(ValueError, match="allow-paid-openrouter"):
        twenty_questions_eval.provider_names("openrouter", allow_paid_openrouter=False)


def test_chain_excludes_paid_lane_without_flag():
    """Appending OpenRouter unconditionally would turn a free eval into a paid one."""
    assert twenty_questions_eval.provider_names(
        "chain", allow_paid_openrouter=False,
    ) == ("gemini", "groq")
    assert twenty_questions_eval.provider_names(
        "chain", allow_paid_openrouter=True,
    ) == ("gemini", "groq", "openrouter")


def test_loader_decodes_a_bounded_valid_jsonl_case(tmp_path):
    """Dropping the JSONL-to-runtime conversion would prevent the shared builder from running."""
    cases = twenty_questions_eval.load_cases(_write_jsonl(tmp_path / "cases.jsonl", [_record()]))

    assert cases == (
        EvalCase(
            "case-1",
            '{"facts":["Il faro di vetro usa mappe stellari."]}',
            cases[0].history,
            "Ci sono mappe stellari?",
            QuestionVerdict.si,
        ),
    )
    assert cases[0].history[0].verdict is QuestionVerdict.si


@pytest.mark.parametrize(
    ("records", "match"),
    [
        ([_record(), _record()], "duplicate case_id"),
        ([_record(expected="unknown")], "expected"),
        ([_record() | {"history": [{"turn_no": "one"}]}], "history"),
        ([_record() | {"question": "x" * 501}], "question"),
    ],
)
def test_loader_rejects_invalid_records_loudly(tmp_path, records, match):
    """Ignoring malformed or duplicate samples would silently bias calibration results."""
    path = _write_jsonl(tmp_path / "invalid.jsonl", records)

    with pytest.raises(ValueError, match=match):
        twenty_questions_eval.load_cases(path)


async def test_report_contains_aggregates_not_case_material():
    """Returning raw cases would leak calibration prompts and synthetic answers to stdout."""
    cases = (
        EvalCase("yes-1", '"dossier uno"', (), "Domanda uno?", QuestionVerdict.si),
        EvalCase("no-1", '"dossier due"', (), "Domanda due?", QuestionVerdict.no),
    )

    async def fake_runner(case: EvalCase) -> EvalObservation:
        return EvalObservation(
            case.expected,
            True,
            True,
            "gemini",
            10,
            0,
            None,
            ai_budget.UsageMetrics("gemini/test", 2, 3, 0, 1),
            7,
        )

    report = await twenty_questions_eval.run_cases(cases, fake_runner)
    rendered = json.dumps(asdict(report), sort_keys=True)

    assert (report.total, report.schema_compliant, report.correct) == (2, 2, 2)
    assert report.latency_ms == {"gemini": 20}
    assert report.usage.prompt_tokens == 4
    assert report.cost_microusd == 14
    for case in cases:
        assert case.question not in rendered
        assert case.dossier_json not in rendered
        assert case.case_id not in rendered


async def test_report_aggregates_failures_and_inconsistent_fallbacks():
    """Forgetting a failed observation would overstate provider quality."""
    cases = (
        EvalCase("one", "{}", (), "Q1", QuestionVerdict.si),
        EvalCase("two", "{}", (), "Q2", QuestionVerdict.no),
    )

    async def fake_runner(case: EvalCase) -> EvalObservation:
        if case.case_id == "one":
            return EvalObservation(
                QuestionVerdict.si, True, False, "groq", 8, 1, None,
                ai_budget.UsageMetrics("groq/test", 4, 2, None, None), 0,
            )
        return EvalObservation(
            None, False, False, None, 3, 0, "timeout", ai_budget.UsageMetrics(), 0,
        )

    report = await twenty_questions_eval.run_cases(cases, fake_runner)

    assert report.correct == 1
    assert report.consistency_failures == 2
    assert report.fallbacks == {"groq": 1}
    assert report.errors == {"timeout": 1}
    assert report.latency_ms == {"groq": 8, "unknown": 3}


async def test_paid_report_uses_durable_twentyq_spend_delta_after_failed_call(monkeypatch):
    """Hard-coding failed paid observations to zero would hide a conservatively settled charge."""
    snapshots = iter((
        ai_budget.BudgetSnapshot("2026-09", 4_000_000, 100, 0),
        ai_budget.BudgetSnapshot("2026-09", 4_000_000, 119, 0),
    ))

    async def feature_snapshot(feature: str):
        assert feature == "twentyq"
        return next(snapshots)

    async def failed_runner(_case: EvalCase) -> EvalObservation:
        return EvalObservation(
            None, False, False, "openrouter", 8, 0, "network", ai_budget.UsageMetrics(), 0,
        )

    monkeypatch.setattr(ai_budget, "feature_snapshot", feature_snapshot)
    report = await twenty_questions_eval.run_cases(
        (EvalCase("paid-failure", "{}", (), "Q", QuestionVerdict.si),),
        failed_runner,
        paid=True,
    )

    assert report.cost_microusd == 19
    assert report.errors == {"network": 1}


async def test_free_report_never_reads_paid_budget(monkeypatch):
    """Querying budget during free calibration would unnecessarily couple it to paid storage."""
    async def feature_snapshot(_feature: str):
        raise AssertionError("free run must not read the paid budget")

    async def free_runner(case: EvalCase) -> EvalObservation:
        return EvalObservation(
            case.expected, True, True, "gemini", 1, 0, None, ai_budget.UsageMetrics(), 0,
        )

    monkeypatch.setattr(ai_budget, "feature_snapshot", feature_snapshot)
    report = await twenty_questions_eval.run_cases(
        (EvalCase("free", "{}", (), "Q", QuestionVerdict.si),), free_runner,
    )

    assert report.cost_microusd == 0


async def test_paid_report_replaces_success_attempt_cost_with_durable_delta(monkeypatch):
    """Adding both values would double-count a successful OpenRouter settlement."""
    snapshots = iter((
        ai_budget.BudgetSnapshot("2026-09", 4_000_000, 100, 0),
        ai_budget.BudgetSnapshot("2026-09", 4_000_000, 119, 0),
    ))

    async def feature_snapshot(_feature: str):
        return next(snapshots)

    async def successful_runner(case: EvalCase) -> EvalObservation:
        return EvalObservation(
            case.expected, True, True, "openrouter", 1, 0, None,
            ai_budget.UsageMetrics(), 19,
        )

    monkeypatch.setattr(ai_budget, "feature_snapshot", feature_snapshot)
    report = await twenty_questions_eval.run_cases(
        (EvalCase("paid-success", "{}", (), "Q", QuestionVerdict.si),),
        successful_runner,
        paid=True,
    )

    assert report.cost_microusd == 19


def test_provider_without_key_names_only_its_environment_variable(monkeypatch):
    """An error that prints a key value would disclose a credential in local diagnostics."""
    monkeypatch.setattr(twenty_questions_eval.settings, "gemini_api_key", "secret-value")
    monkeypatch.setattr(twenty_questions_eval.settings, "groq_api_key", "")

    with pytest.raises(ValueError, match=r"^GROQ_API_KEY$") as raised:
        twenty_questions_eval.require_provider_keys(("groq",))

    assert "secret-value" not in str(raised.value)


def test_explicit_provider_is_built_even_when_runtime_order_omits_it(monkeypatch):
    """Filtering a configured runtime order would make an explicit eval provider silently do nothing."""
    from services.structured_ai_router import _reset_twenty_questions_router_for_tests

    monkeypatch.setattr(twenty_questions_eval.settings, "twentyq_provider_order", "groq")
    _reset_twenty_questions_router_for_tests()
    try:
        router = twenty_questions_eval.build_runtime_router(("gemini",))
    finally:
        _reset_twenty_questions_router_for_tests()

    assert tuple(provider.name for provider in router.providers) == ("gemini",)


async def test_openrouter_runner_uses_runtime_lane_and_disables_audit(monkeypatch):
    """Changing the eval route to a new provider path could skip twentyq accounting or audit privacy."""
    calls: dict[str, object] = {}

    class FakeRouter:
        async def generate(self, request, *, session_id, validate, audit):
            calls["request"] = request
            calls["session_id"] = session_id
            calls["audit"] = audit
            return type("Result", (), {
                "value": validate({"verdetto": "si"}),
                "provider": "openrouter",
                "attempts": (type("Attempt", (), {
                    "usage": ai_budget.UsageMetrics("paid", 3, 2, None, None),
                    "cost_microusd": 19,
                })(),),
            })()

    case = EvalCase("paid", '{"facts":["sintetico"]}', (), "Ha mappe?", QuestionVerdict.si)
    observation = await twenty_questions_eval.evaluate_case(case, FakeRouter())

    assert observation.provider == "openrouter"
    assert observation.cost_microusd == 19
    assert calls["session_id"] is None
    assert calls["audit"] is False


def test_versioned_dataset_is_balanced_and_loadable():
    """Removing a verdict class would make accuracy look better on a biased corpus."""
    root = Path(__file__).resolve().parents[2]
    cases = twenty_questions_eval.load_cases(root / "evals/twentyq/v1.jsonl")

    assert len(cases) >= 36
    assert Counter(case.expected for case in cases) == {
        QuestionVerdict.si: 9,
        QuestionVerdict.no: 9,
        QuestionVerdict.forse: 9,
        QuestionVerdict.usa_risposta: 9,
    }
    masked_title = next(case for case in cases if case.case_id == "masked-title-01")
    assert masked_title.expected is QuestionVerdict.usa_risposta
    assert "Archivio Nebula" not in masked_title.dossier_json
    assert "mappe stellari" in masked_title.dossier_json
    assert "osservatorio" in masked_title.dossier_json
    assert "Archivio Nebula" in masked_title.question


def test_cli_help_is_available_without_loading_a_provider():
    """Importing provider clients before argument parsing would make help depend on credentials."""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/eval_twenty_questions.py", "--help"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    assert "--allow-paid-openrouter" in result.stdout
    assert result.stderr == ""


async def test_cli_rejects_unflagged_openrouter_before_database_or_router_work(monkeypatch):
    """Moving the paid guard below setup could create tables or route a paid request accidentally."""
    cli = _cli_module()
    calls: list[str] = []
    fake_connection = ModuleType("database.connection")

    async def create_tables():
        calls.append("tables")

    fake_connection.create_tables = create_tables
    monkeypatch.setitem(sys.modules, "database.connection", fake_connection)
    monkeypatch.setattr(
        twenty_questions_eval,
        "build_runtime_router",
        lambda _names: calls.append("router"),
    )

    with pytest.raises(ValueError, match="allow-paid-openrouter"):
        await cli._run(argparse.Namespace(
            provider="openrouter", allow_paid_openrouter=False, dataset="unused",
        ))

    assert calls == []


async def test_cli_initializes_budget_tables_before_a_paid_eval(monkeypatch):
    """Removing additive table setup would make a valid paid eval fail before reservation."""
    cli = _cli_module()
    calls: list[str] = []
    fake_connection = ModuleType("database.connection")

    async def create_tables():
        calls.append("tables")

    async def run_cases(_cases, _runner, *, paid):
        assert paid is True
        calls.append("eval")
        return twenty_questions_eval.EvalSummary(
            0, 0, 0, 0, {}, {}, {}, ai_budget.UsageMetrics(), 0,
        )

    fake_connection.create_tables = create_tables
    monkeypatch.setitem(sys.modules, "database.connection", fake_connection)
    monkeypatch.setattr(twenty_questions_eval, "require_provider_keys", lambda _names: None)
    monkeypatch.setattr(twenty_questions_eval, "build_runtime_router", lambda _names: object())
    monkeypatch.setattr(twenty_questions_eval, "load_cases", lambda _path: ())
    monkeypatch.setattr(twenty_questions_eval, "run_cases", run_cases)

    result = await cli._run(argparse.Namespace(
        provider="openrouter", allow_paid_openrouter=True, dataset="unused",
    ))

    assert calls == ["tables", "eval"]
    assert result["total"] == 0


async def test_cli_missing_key_is_prompt_free_and_does_not_create_tables(monkeypatch):
    """Initializing storage before key validation would do avoidable work and risk noisy diagnostics."""
    cli = _cli_module()
    calls: list[str] = []
    fake_connection = ModuleType("database.connection")

    async def create_tables():
        calls.append("tables")

    fake_connection.create_tables = create_tables
    monkeypatch.setitem(sys.modules, "database.connection", fake_connection)
    monkeypatch.setattr(twenty_questions_eval.settings, "openrouter_api_key", "")

    with pytest.raises(ValueError, match=r"^OPENROUTER_API_KEY$") as raised:
        await cli._run(argparse.Namespace(
            provider="openrouter", allow_paid_openrouter=True, dataset="unused",
        ))

    assert calls == []
    assert "unused" not in str(raised.value)
