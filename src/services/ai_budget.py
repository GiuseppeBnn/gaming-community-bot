"""Persistent, atomic cost guard for paid AI providers.

The budget transaction is intentionally independent from Telegram handler
sessions: it finishes before the network call begins, then a second short
transaction settles the provider-reported cost.  No prompt or completion text
is stored in the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import logging
from typing import Literal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.connection import async_session_maker
from database.models import AIBudgetPeriod, AIFeatureBudgetPeriod, AIUsageLog

log = logging.getLogger(__name__)

_MICRO_USD = Decimal("1000000")
_TOKEN_OVERHEAD = 512
BudgetLane = Literal["twentyq", "openrouter_other"]


class AIBudgetError(RuntimeError):
    """Budget state is unavailable, so paid traffic must fail closed."""


class AIBudgetExceeded(AIBudgetError):
    """The next worst-case request would cross the configured monthly cap."""


@dataclass(frozen=True, slots=True)
class Reservation:
    request_id: str
    period: str
    feature: str = ""
    budget_lane: BudgetLane = "openrouter_other"
    estimated_microusd: int = 0

    def __post_init__(self) -> None:
        """Accept the former three-positional-field constructor during rollout."""
        if isinstance(self.feature, int):
            object.__setattr__(self, "estimated_microusd", self.feature)
            object.__setattr__(self, "feature", "")


@dataclass(frozen=True, slots=True)
class UsageMetrics:
    actual_model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    period: str
    cap_microusd: int
    spent_microusd: int
    reserved_microusd: int


def current_period(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    return value.astimezone(UTC).strftime("%Y-%m")


def usd_to_microusd(value: Decimal) -> int:
    return int((value * _MICRO_USD).to_integral_value(rounding=ROUND_FLOOR))


def estimate_input_tokens(system_prompt: str, user_text: str) -> int:
    """Conservative tokenizer-free upper bound, including request framing.

    UTF-8 bytes are an upper bound for normal BPE token counts even with emoji
    and non-Latin text.  The fixed allowance covers role/JSON framing.
    """
    return len(system_prompt.encode("utf-8")) + len(user_text.encode("utf-8")) + _TOKEN_OVERHEAD


def estimate_cost_microusd(
    input_tokens: int,
    output_tokens: int,
    *,
    max_prompt_price: Decimal,
    max_completion_price: Decimal,
) -> int:
    # Prices are USD / 1M tokens. Multiplying by a token count therefore yields
    # micro-USD directly; round upward so the reservation never understates it.
    value = Decimal(input_tokens) * max_prompt_price
    value += Decimal(output_tokens) * max_completion_price
    return max(1, int(value.to_integral_value(rounding=ROUND_CEILING)))


async def _ensure_periods(
    session: AsyncSession,
    period: str,
    global_cap: int,
    budget_lane: BudgetLane,
    lane_cap: int,
) -> None:
    """Create global then lane period rows inside the reservation transaction."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        await session.execute(
            sqlite_insert(AIBudgetPeriod)
            .values(period=period, cap_microusd=global_cap)
            .on_conflict_do_nothing()
        )
        await session.execute(
            sqlite_insert(AIFeatureBudgetPeriod)
            .values(period=period, feature=budget_lane, cap_microusd=lane_cap)
            .on_conflict_do_nothing()
        )
        return
    if dialect == "postgresql":
        await session.execute(
            postgresql_insert(AIBudgetPeriod)
            .values(period=period, cap_microusd=global_cap)
            .on_conflict_do_nothing()
        )
        await session.execute(
            postgresql_insert(AIFeatureBudgetPeriod)
            .values(period=period, feature=budget_lane, cap_microusd=lane_cap)
            .on_conflict_do_nothing()
        )
        return
    raise AIBudgetError(f"unsupported budget storage dialect: {dialect}")


def _configured_lane_cap(budget_lane: BudgetLane) -> int:
    if budget_lane == "twentyq":
        return usd_to_microusd(settings.twentyq_openrouter_budget_usd)
    if budget_lane == "openrouter_other":
        return usd_to_microusd(settings.openrouter_other_budget_usd)
    raise AIBudgetError(f"unsupported OpenRouter budget lane: {budget_lane}")


async def reserve(
    *,
    feature: str,
    budget_lane: BudgetLane = "openrouter_other",
    provider: str,
    requested_model: str,
    system_prompt: str,
    user_text: str,
    max_output_tokens: int,
) -> Reservation:
    """Atomically reserve the worst-case cost before a paid network call."""
    global_cap = usd_to_microusd(settings.ai_monthly_budget_usd)
    lane_cap = _configured_lane_cap(budget_lane)
    if global_cap == 0 or lane_cap == 0:
        raise AIBudgetExceeded("OpenRouter budget exhausted")

    period = current_period()
    estimate = estimate_cost_microusd(
        estimate_input_tokens(system_prompt, user_text),
        max_output_tokens,
        max_prompt_price=settings.openrouter_max_prompt_price,
        max_completion_price=settings.openrouter_max_completion_price,
    )
    request_id = str(uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)

    try:
        async with async_session_maker.begin() as session:
            await _ensure_periods(session, period, global_cap, budget_lane, lane_cap)
            global_result = await session.execute(
                update(AIBudgetPeriod)
                .where(
                    AIBudgetPeriod.period == period,
                    AIBudgetPeriod.spent_microusd
                    + AIBudgetPeriod.reserved_microusd
                    + estimate
                    <= global_cap,
                )
                .values(
                    cap_microusd=global_cap,
                    reserved_microusd=AIBudgetPeriod.reserved_microusd + estimate,
                    updated_at=now,
                )
            )
            lane_result = await session.execute(
                update(AIFeatureBudgetPeriod)
                .where(
                    AIFeatureBudgetPeriod.period == period,
                    AIFeatureBudgetPeriod.feature == budget_lane,
                    AIFeatureBudgetPeriod.spent_microusd
                    + AIFeatureBudgetPeriod.reserved_microusd
                    + estimate
                    <= lane_cap,
                )
                .values(
                    cap_microusd=lane_cap,
                    reserved_microusd=AIFeatureBudgetPeriod.reserved_microusd + estimate,
                    updated_at=now,
                )
            )
            if (
                getattr(global_result, "rowcount", 0) != 1
                or getattr(lane_result, "rowcount", 0) != 1
            ):
                raise AIBudgetExceeded("OpenRouter budget exhausted")
            session.add(AIUsageLog(
                request_id=request_id,
                period=period,
                feature=feature[:32],
                provider=provider[:32],
                requested_model=requested_model[:128],
                status="reserved",
                reserved_microusd=estimate,
            ))
    except AIBudgetExceeded:
        raise
    except SQLAlchemyError as exc:
        raise AIBudgetError("budget storage unavailable") from exc

    return Reservation(
        request_id=request_id,
        period=period,
        feature=feature,
        budget_lane=budget_lane,
        estimated_microusd=estimate,
    )


async def settle(
    reservation: Reservation,
    *,
    status: str,
    actual_microusd: int | None,
    metrics: UsageMetrics | None = None,
) -> None:
    """Release a reservation and charge it exactly once.

    For an ambiguous timeout/network failure the caller passes ``None``: the
    whole reservation is charged conservatively because the provider may have
    completed the request after the client disconnected.
    """
    metrics = metrics or UsageMetrics()
    charge = reservation.estimated_microusd if actual_microusd is None else max(0, actual_microusd)
    final_status = status[:16]
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with async_session_maker.begin() as session:
            result = await session.execute(
                update(AIUsageLog)
                .where(
                    AIUsageLog.request_id == reservation.request_id,
                    AIUsageLog.status == "reserved",
                )
                .values(
                    status=final_status,
                    actual_model=(metrics.actual_model or "")[:128] or None,
                    actual_microusd=charge,
                    prompt_tokens=metrics.prompt_tokens,
                    completion_tokens=metrics.completion_tokens,
                    reasoning_tokens=metrics.reasoning_tokens,
                    cached_tokens=metrics.cached_tokens,
                    completed_at=now,
                )
            )
            if getattr(result, "rowcount", 0) != 1:
                return
            global_result = await session.execute(
                update(AIBudgetPeriod)
                .where(AIBudgetPeriod.period == reservation.period)
                .values(
                    reserved_microusd=(
                        AIBudgetPeriod.reserved_microusd - reservation.estimated_microusd
                    ),
                    spent_microusd=AIBudgetPeriod.spent_microusd + charge,
                    updated_at=now,
                )
            )
            lane_result = await session.execute(
                update(AIFeatureBudgetPeriod)
                .where(
                    AIFeatureBudgetPeriod.period == reservation.period,
                    AIFeatureBudgetPeriod.feature == reservation.budget_lane,
                )
                .values(
                    reserved_microusd=(
                        AIFeatureBudgetPeriod.reserved_microusd
                        - reservation.estimated_microusd
                    ),
                    spent_microusd=AIFeatureBudgetPeriod.spent_microusd + charge,
                    updated_at=now,
                )
            )
            if (
                getattr(global_result, "rowcount", 0) != 1
                or getattr(lane_result, "rowcount", 0) != 1
            ):
                raise AIBudgetError("budget settlement unavailable")
    except SQLAlchemyError as exc:
        # The reservation remains charged against the cap until repaired. That
        # is safer than silently freeing money after an accounting failure.
        log.exception("Impossibile chiudere la prenotazione AI %s.", reservation.request_id)
        raise AIBudgetError("budget settlement unavailable") from exc


async def snapshot(period: str | None = None) -> BudgetSnapshot | None:
    """Read-only operational view for logs/admin surfaces."""
    target = period or current_period()
    try:
        async with async_session_maker() as session:
            row = (await session.execute(
                select(AIBudgetPeriod).where(AIBudgetPeriod.period == target)
            )).scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise AIBudgetError("budget storage unavailable") from exc
    if row is None:
        return None
    return BudgetSnapshot(
        row.period, row.cap_microusd, row.spent_microusd, row.reserved_microusd,
    )


async def feature_snapshot(
    feature: str, period: str | None = None,
) -> BudgetSnapshot | None:
    """Read one configured budget lane without conflating it with request features."""
    target = period or current_period()
    try:
        async with async_session_maker() as session:
            row = (await session.execute(
                select(AIFeatureBudgetPeriod).where(
                    AIFeatureBudgetPeriod.period == target,
                    AIFeatureBudgetPeriod.feature == feature,
                )
            )).scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise AIBudgetError("budget storage unavailable") from exc
    if row is None:
        return None
    return BudgetSnapshot(
        row.period, row.cap_microusd, row.spent_microusd, row.reserved_microusd,
    )
