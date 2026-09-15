#!/usr/bin/env python3
"""Evaluate the eight real text prompts on synthetic input, with a bounded paid run."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@dataclass(frozen=True, slots=True)
class Case:
    command: str
    system: str
    user: str
    answer_tokens: int
    temperature: float | None = None


def build_cases() -> tuple[Case, ...]:
    from handlers import fun_ai
    from services.alduino_chat import DialogueTurn, render_model_input

    inputs = (
        ("maestro", "Ho perso contro il tutorial dopo tre ore di allenamento.", 160),
        ("complotto", "Marco perde sempre quando ospita lui la partita.", 200),
        ("difendi", "Ho rubato tutto il loot e lasciato la squadra senza munizioni.", 220),
        ("accusa", "Ho disinstallato il gioco prima di finire la campagna.", 170),
        ("drama", "Mi è caduto il controller mentre bevevo l'acqua.", 260),
        ("dialetto", "Guarda, lo sapevo che uscivi senza aspettarmi. Dove vai?", 240),
        ("insulta", "Marco (personaggio immaginario della nostra squadra)", 120),
    )
    cases = [Case(
        name, getattr(fun_ai, f"_PROMPT_{name.upper()}"),
        f"{fun_ai._CONTENT_OPEN}\n{text}\n{fun_ai._CONTENT_CLOSE}", tokens,
        fun_ai._DIALETTO_TEMPERATURE if name == "dialetto" else None,
    ) for name, text, tokens in inputs]
    cases.append(Case(
        "alduino", fun_ai._PROMPT_ALDUINO,
        render_model_input(
            "Di quale gioco parlava Marco e quando possiamo unirci?",
            history=(DialogueTurn("Mi unisco alla prossima partita.", "Ti teniamo un posto."),),
            live_context="Serata Monster Hunter programmata domani alle 21:00.",
            group_context="Marco: Preferisco Monster Hunter, ci sono domani alle 21:00.\n"
            + "Giulia: Stiamo preparando la squadra per la serata, manca un posto.\n" * 80,
        ), 280,
    ))
    return tuple(cases)


def run_estimate(
    cases: tuple[Case, ...], models: tuple[str, ...], chat_models: tuple[str, ...],
) -> int:
    from config_data.config import settings
    from services import ai_budget, ai_service

    return sum(ai_budget.estimate_cost_microusd(
        ai_budget.estimate_input_tokens(case.system, case.user),
        ai_service.openrouter_generation_policy(
            chat_models if case.command == "alduino" else models, case.answer_tokens,
        )[1],
        max_prompt_price=settings.openrouter_max_prompt_price,
        max_completion_price=settings.openrouter_max_completion_price,
    ) for case in cases)


async def run(args: argparse.Namespace) -> bool:
    from config_data.config import settings
    from services import ai_budget, ai_service

    cases = build_cases()
    models = ai_service.parse_model_list(settings.openrouter_fun_models)
    chat_models = ai_service.parse_model_list(settings.openrouter_chat_models)
    feature = "text_eval_" + uuid4().hex[:16]
    if args.provider == "openrouter":
        if not args.allow_paid_openrouter:
            raise ValueError("OpenRouter requires --allow-paid-openrouter")
        if not settings.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY missing")
        limit = Decimal(args.max_cost_usd)
        if not limit.is_finite() or not 0 < limit <= Decimal("0.05"):
            raise ValueError("max-cost-usd must be >0 and <=0.05")
        estimate = run_estimate(cases, models, chat_models)
        if estimate > ai_budget.usd_to_microusd(limit):
            raise ValueError("run estimate exceeds max-cost-usd; no requests sent")
        # Use a local/test DB_URL as documented; never start polling or Telegram.
        from database.connection import create_tables

        await create_tables()
        print(json.dumps({"maximum_estimated_usd": str(Decimal(estimate) / 1_000_000), "run": feature}), flush=True)
    elif not settings.groq_api_key:
        raise ValueError("GROQ_API_KEY missing")
    successful = 0
    for case in cases:
        started = time.monotonic()
        try:
            if args.provider == "openrouter":
                text = await ai_service.generate_openrouter_completion(
                    case.system, case.user, case.answer_tokens,
                    temperature=case.temperature, feature=feature,
                    models=chat_models if case.command == "alduino" else models,
                    require_zdr=True,
                )
            else:
                text = await ai_service.generate_groq_completion(
                    case.system, case.user, case.answer_tokens, temperature=case.temperature,
                )
        except ai_service.AIServiceError:
            print(json.dumps({"command": case.command, "error": "provider unavailable"}), flush=True)
        else:
            successful += 1
            print(json.dumps({
                "command": case.command, "seconds": round(time.monotonic() - started, 2),
                "chars": len(text), "text": text,
            }, ensure_ascii=False), flush=True)
    if args.provider == "openrouter":
        spend = await ai_budget.feature_spend_microusd(feature)
        print(json.dumps({"run": feature, "charged_usd": str(Decimal(spend) / 1_000_000)}), flush=True)
    return successful == len(cases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("groq", "openrouter"), required=True)
    parser.add_argument("--allow-paid-openrouter", action="store_true")
    parser.add_argument("--max-cost-usd", default="0.02")
    args = parser.parse_args()
    try:
        complete = asyncio.run(run(args))
    except (ValueError, ArithmeticError) as exc:
        parser.exit(2, f"{exc}\n")
    if not complete:
        parser.exit(1, "Evaluation incomplete: one or more provider requests failed.\n")


if __name__ == "__main__":
    main()
