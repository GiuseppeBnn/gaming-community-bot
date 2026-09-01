#!/usr/bin/env python3
"""Run the explicitly requested local evaluation of Alduino question verdicts."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys

# Make `src/` importable when run as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Valuta i verdetti del gioco segreto di Alduino.")
    parser.add_argument(
        "--dataset",
        default="evals/twentyq/v1.jsonl",
        help="dataset JSONL sintetico (default: evals/twentyq/v1.jsonl)",
    )
    parser.add_argument(
        "--provider",
        choices=("gemini", "groq", "openrouter", "chain"),
        required=True,
        help="provider singolo o catena ordinata",
    )
    parser.add_argument(
        "--allow-paid-openrouter",
        action="store_true",
        help="abilita esplicitamente la corsia OpenRouter a pagamento",
    )
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    from database.connection import create_tables
    from services.twenty_questions_eval import (
        build_runtime_router,
        evaluate_case,
        load_cases,
        new_budget_feature,
        provider_names,
        require_provider_keys,
        run_cases,
    )

    names = provider_names(args.provider, allow_paid_openrouter=args.allow_paid_openrouter)
    require_provider_keys(names)
    budget_feature = new_budget_feature() if "openrouter" in names else None
    if "openrouter" in names:
        await create_tables()
    router = build_runtime_router(names, budget_feature=budget_feature)
    summary = await run_cases(
        load_cases(Path(args.dataset)),
        lambda case: evaluate_case(case, router),
        budget_feature=budget_feature,
    )
    return asdict(summary)


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        summary = asyncio.run(_run(args))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
