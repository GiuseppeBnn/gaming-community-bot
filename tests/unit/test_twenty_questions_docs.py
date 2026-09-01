"""Regression checks for the public secret-game documentation contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PUBLIC_AND_TECHNICAL_DOCS = (
    "README.md",
    "STEERING.md",
    "INDEX.md",
    ".env.example",
)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_public_docs_and_env_describe_the_v2_contract():
    """A v2 regression must not quietly restore 20/3 rules or expiry payouts."""
    readme = _read("README.md")
    env_example = _read(".env.example")

    for text in (
        "### Il gioco segreto di Alduino",
        "5 domande valide e 2 tentativi validi",
        "10 XP",
        "RISPOSTA: nome del gioco",
        "/gioco_alduino",
        "Il resto della divisione non viene assegnato a nessuno.",
        "12 ore",
    ):
        assert text in readme

    for line in (
        "TWENTYQ_V2_ENABLED=false",
        "TWENTYQ_PROVIDER_ORDER=gemini,groq,openrouter",
        "TWENTYQ_GEMINI_MODEL=gemini-3.5-flash",
        "TWENTYQ_GROQ_MODEL=openai/gpt-oss-20b",
        "TWENTYQ_OPENROUTER_MODEL=deepseek/deepseek-v4-flash-0731",
        "TWENTYQ_GEMINI_TIMEOUT_SECONDS=8",
        "TWENTYQ_GROQ_TIMEOUT_SECONDS=8",
        "TWENTYQ_OPENROUTER_TIMEOUT_SECONDS=12",
        "TWENTYQ_PROVIDER_DEADLINE_SECONDS=25",
        "TWENTYQ_CONTEXT_TURNS=24",
        "TWENTYQ_CONTEXT_CHARS=12000",
        "TWENTYQ_OPENROUTER_BUDGET_USD=4.00",
        "OPENROUTER_OTHER_BUDGET_USD=1.00",
        "AI_MONTHLY_BUDGET_USD=5.00",
        "TWENTYQ_MAX_COINS_PER_PARTICIPANT=1000",
    ):
        assert line in env_example


def test_historical_twenty_questions_references_are_explicitly_legacy():
    """Only marked historical v1 material may still name its former 20/3 rules."""
    for relative_path in PUBLIC_AND_TECHNICAL_DOCS:
        text = _read(relative_path)
        for stale_reference in ("20 Domande", "20 domande", "3 tentativi"):
            start = 0
            while (match := text.find(stale_reference, start)) != -1:
                context = text[max(0, match - 80): match + len(stale_reference) + 80].lower()
                assert "legacy" in context, (
                    f"{relative_path} has an unmarked historical reference: "
                    f"{stale_reference!r}"
                )
                start = match + len(stale_reference)


def test_templates_document_only_safe_configuration_examples():
    """Examples distinguish runtime and destructive-test databases without real secrets."""
    env_example = _read(".env.example")
    readme = _read("README.md")

    assert "DB_URL=" in env_example
    assert "TEST_PG_URL" in env_example
    assert "TEST_PG_URL" in readme
    assert "sk-or-v1-" not in env_example
    assert "gsk_" not in env_example
