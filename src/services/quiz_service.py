"""
Quiz service — DB-side operations for private, answer-driven quizzes.

Each participant plays the quiz in their PRIVATE chat with the bot (inline option
buttons), with no per-question time limit. Answers are recorded per user; when a
user has answered every question they are a "finisher". The podium ranks finishers
by correct answers (desc) then by finish time (asc, arrival order) and pays prizes.

Follows STEERING §5: no commits here — the caller owns the transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Integer, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config_data.config import settings
from database.models import Quiz, QuizAnswer, QuizQuestion, TransactionType
from services import economy_service, xp_service
from services.xp_service import XpSource

# Legacy prize split among the podium (1st, 2nd, 3rd) — used only when no explicit
# per-rank prizes are set on the quiz (back-compat with the old single-pool model).
_PRIZE_SPLIT = (0.5, 0.3, 0.2)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def participation_floor(consolation: int) -> int:
    """Derive the guaranteed minimum (last-place consolation) from the 4th-place prize.

    floor = max(floor_min, round(consolation * floor_ratio)), but never above the
    consolation itself and never below 0.
    """
    if consolation <= 0:
        return 0
    floor = max(settings.quiz_participation_floor_min,
                round(consolation * settings.quiz_participation_floor_ratio))
    return max(0, min(floor, consolation))


def consolation_amounts(n: int, top: int, floor: int) -> list[int]:
    """Linear, non-increasing consolation schedule for the `n` non-podium finishers.

    Position 0 (4th place) gets `top`; the last gets `floor`; the rest interpolate
    linearly. Everyone gets at least `floor` (and at least 0). Pure function.
    """
    if n <= 0:
        return []
    if top <= 0:
        return [0] * n
    floor = max(0, min(floor, top))
    if n == 1:
        return [top]
    return [
        max(floor, round(top - (top - floor) * i / (n - 1)))
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def create_quiz(
    session: AsyncSession,
    creator_tg_id: int,
    title: str,
    description: str,
    prize_coins: int = 0,
    *,
    prize_first: int = 0,
    prize_second: int = 0,
    prize_third: int = 0,
    prize_consolation: int = 0,
    prize_min: int = 0,
) -> Quiz:
    quiz = Quiz(
        title=title[:256],
        description=description[:1024],
        creator_tg_id=creator_tg_id,
        status="draft",
        prize_coins=max(0, prize_coins),
        prize_first=max(0, prize_first),
        prize_second=max(0, prize_second),
        prize_third=max(0, prize_third),
        prize_consolation=max(0, prize_consolation),
        prize_min=max(0, prize_min),
    )
    session.add(quiz)
    await session.flush()
    return quiz


async def add_question(
    session: AsyncSession,
    quiz_id: int,
    text: str,
    options: list[str],
    correct_option_id: int,
    explanation: str | None,
) -> QuizQuestion:
    position = (
        await session.execute(
            select(func.count()).select_from(QuizQuestion).where(QuizQuestion.quiz_id == quiz_id)
        )
    ).scalar_one()
    question = QuizQuestion(
        quiz_id=quiz_id,
        position=position,
        text=text[:300],
        options_json=json.dumps(options, ensure_ascii=False),
        correct_option_id=correct_option_id,
        explanation=(explanation or None) and explanation[:200],
        open_period=0,  # no time limit
    )
    session.add(question)
    await session.flush()
    return question


async def delete_last_question(session: AsyncSession, quiz_id: int) -> int:
    """Delete the highest-position question of a draft quiz. Returns remaining count."""
    question = (
        await session.execute(
            select(QuizQuestion)
            .where(QuizQuestion.quiz_id == quiz_id)
            .order_by(QuizQuestion.position.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if question is not None:
        await session.delete(question)
        await session.flush()
    return await total_questions(session, quiz_id)


def question_options(question: QuizQuestion) -> list[str]:
    return json.loads(question.options_json)


async def get_quiz(session: AsyncSession, quiz_id: int) -> Quiz | None:
    result = await session.execute(
        select(Quiz).where(Quiz.id == quiz_id).options(selectinload(Quiz.questions))
    )
    return result.scalar_one_or_none()


async def list_ready(session: AsyncSession) -> list[Quiz]:
    result = await session.execute(
        select(Quiz)
        .where(Quiz.status.in_(("ready", "running")))
        .options(selectinload(Quiz.questions))
        .order_by(Quiz.created_at.desc())
    )
    return list(result.scalars().all())


async def set_status(session: AsyncSession, quiz_id: int, status: str) -> None:
    quiz = (await session.execute(select(Quiz).where(Quiz.id == quiz_id))).scalar_one_or_none()
    if quiz is None:
        return
    quiz.status = status
    if status == "running" and quiz.started_at is None:
        quiz.started_at = _now()
    if status == "finished" and quiz.finished_at is None:
        quiz.finished_at = _now()


# ---------------------------------------------------------------------------
# Play / answers
# ---------------------------------------------------------------------------

async def total_questions(session: AsyncSession, quiz_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(QuizQuestion).where(QuizQuestion.quiz_id == quiz_id)
        )
    ).scalar_one()


async def answered_count(session: AsyncSession, quiz_id: int, user_tg_id: int) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(QuizAnswer)
            .where(QuizAnswer.quiz_id == quiz_id, QuizAnswer.user_tg_id == user_tg_id)
        )
    ).scalar_one()


async def correct_count(session: AsyncSession, quiz_id: int, user_tg_id: int) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(QuizAnswer)
            .where(
                QuizAnswer.quiz_id == quiz_id,
                QuizAnswer.user_tg_id == user_tg_id,
                QuizAnswer.is_correct.is_(True),
            )
        )
    ).scalar_one()


@dataclass
class AnswerOutcome:
    recorded: bool          # False if it was a duplicate / already answered
    is_correct: bool
    correct_option_id: int


async def record_answer(
    session: AsyncSession,
    quiz_id: int,
    question_id: int,
    user_tg_id: int,
    selected_option_id: int,
) -> AnswerOutcome | None:
    """Record a private-chat answer. Returns None if the question is invalid.

    Idempotent per (question, user): a duplicate returns recorded=False.
    """
    question = (
        await session.execute(
            select(QuizQuestion).where(
                QuizQuestion.id == question_id, QuizQuestion.quiz_id == quiz_id
            )
        )
    ).scalar_one_or_none()
    if question is None:
        return None

    is_correct = selected_option_id == question.correct_option_id

    existing = (
        await session.execute(
            select(QuizAnswer.id).where(
                QuizAnswer.question_id == question_id, QuizAnswer.user_tg_id == user_tg_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return AnswerOutcome(recorded=False, is_correct=is_correct,
                             correct_option_id=question.correct_option_id)

    session.add(
        QuizAnswer(
            quiz_id=quiz_id,
            question_id=question_id,
            user_tg_id=user_tg_id,
            selected_option_id=selected_option_id,
            is_correct=is_correct,
            response_ms=0,
            answered_at=_now(),
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        # Concurrent double-tap raced past the existence check — treat as duplicate.
        await session.rollback()
        return AnswerOutcome(recorded=False, is_correct=is_correct,
                             correct_option_id=question.correct_option_id)

    return AnswerOutcome(recorded=True, is_correct=is_correct,
                         correct_option_id=question.correct_option_id)


# ---------------------------------------------------------------------------
# Podium + prizes
# ---------------------------------------------------------------------------

@dataclass
class PodiumRow:
    user_tg_id: int
    correct: int
    finished_at: datetime
    completion_seconds: int | None = None  # finished_at - quiz.started_at


async def podium(session: AsyncSession, quiz_id: int) -> list[PodiumRow]:
    """Finishers (answered every question) ranked by correct DESC, finish time ASC.

    Each row carries ``completion_seconds`` (time from quiz start to the player's
    last answer) when the quiz's ``started_at`` is known.
    """
    total = await total_questions(session, quiz_id)
    if total == 0:
        return []
    quiz = await get_quiz(session, quiz_id)
    started_at = quiz.started_at if quiz else None
    result = await session.execute(
        select(
            QuizAnswer.user_tg_id,
            func.sum(func.cast(QuizAnswer.is_correct, Integer)).label("correct"),
            func.count().label("answered"),
            func.max(QuizAnswer.answered_at).label("finished_at"),
        )
        .where(QuizAnswer.quiz_id == quiz_id)
        .group_by(QuizAnswer.user_tg_id)
    )
    rows: list[PodiumRow] = []
    for r in result.all():
        if int(r[2] or 0) < total:
            continue
        finished_at = r[3]
        secs: int | None = None
        if started_at is not None and finished_at is not None:
            secs = max(0, int((finished_at - started_at).total_seconds()))
        rows.append(
            PodiumRow(user_tg_id=r[0], correct=int(r[1] or 0),
                      finished_at=finished_at, completion_seconds=secs)
        )
    rows.sort(key=lambda r: (-r.correct, r.finished_at))
    return rows


async def user_finished_at(
    session: AsyncSession, quiz_id: int, user_tg_id: int
) -> datetime | None:
    """Timestamp of the player's last answer in a quiz (None if they never played)."""
    return (
        await session.execute(
            select(func.max(QuizAnswer.answered_at)).where(
                QuizAnswer.quiz_id == quiz_id, QuizAnswer.user_tg_id == user_tg_id
            )
        )
    ).scalar_one_or_none()


@dataclass
class PrizeAward:
    user_tg_id: int
    rank: int
    coins: int
    kind: str = "podium"  # "podium" | "consolation"


def has_explicit_prizes(quiz: Quiz) -> bool:
    """True if the quiz uses the new per-rank prize model (any prize set)."""
    return any((quiz.prize_first, quiz.prize_second, quiz.prize_third, quiz.prize_consolation))


def format_prize_summary(quiz: Quiz) -> str:
    """One-line, human-readable summary of a quiz's prize structure."""
    if has_explicit_prizes(quiz):
        parts = []
        if quiz.prize_first:
            parts.append(f"🥇 {quiz.prize_first}")
        if quiz.prize_second:
            parts.append(f"🥈 {quiz.prize_second}")
        if quiz.prize_third:
            parts.append(f"🥉 {quiz.prize_third}")
        if quiz.prize_consolation:
            parts.append(f"🎖️ 4°: {quiz.prize_consolation} → min {quiz.prize_min}")
        return " · ".join(parts) if parts else "nessun premio"
    if quiz.prize_coins > 0:
        return f"🏆 {quiz.prize_coins} 🪙 al podio (50/30/20)"
    return "nessun premio"


async def _grant_xp(session: AsyncSession, quiz_id: int) -> None:
    """XP for everyone who answered at least one question correctly."""
    xp = settings.quiz_xp_per_correct
    if not xp:
        return
    scorers = await session.execute(
        select(QuizAnswer.user_tg_id, func.sum(func.cast(QuizAnswer.is_correct, Integer)))
        .where(QuizAnswer.quiz_id == quiz_id)
        .group_by(QuizAnswer.user_tg_id)
    )
    for uid, correct in scorers.all():
        if correct:
            # Quiz is an admin-curated event → uncapped XP, via the single XP mutator.
            await xp_service.grant_xp(
                session, uid, int(correct) * xp, XpSource.quiz, capped=False
            )


async def award_prizes(session: AsyncSession, quiz_id: int) -> list[PrizeAward]:
    """Pay prizes to the finishers and grant XP per correct answer. No commit.

    Explicit per-rank model (preferred): podium gets first/second/third; every
    finisher below the podium gets a consolation decreasing linearly from
    `prize_consolation` (4th) down to `prize_min` (last). Legacy model: a single
    pool split 50/30/20 among the top 3.
    """
    quiz = (await session.execute(select(Quiz).where(Quiz.id == quiz_id))).scalar_one_or_none()
    if quiz is None:
        return []

    await _grant_xp(session, quiz_id)
    ranked = await podium(session, quiz_id)
    if not ranked:
        return []

    async def _pay(user_tg_id: int, coins: int, rank: int, kind: str, label: str) -> None:
        await economy_service.credit(
            session, user_tg_id, coins, TransactionType.quiz_reward,
            f"Premio quiz «{quiz.title}» ({label})",
        )
        awards.append(PrizeAward(user_tg_id=user_tg_id, rank=rank, coins=coins, kind=kind))

    awards: list[PrizeAward] = []

    if has_explicit_prizes(quiz):
        podium_prizes = (quiz.prize_first, quiz.prize_second, quiz.prize_third)
        for i, row in enumerate(ranked[:3]):
            coins = podium_prizes[i]
            if coins > 0:
                await _pay(row.user_tg_id, coins, i + 1, "podium", f"podio #{i + 1}")
        others = ranked[3:]
        schedule = consolation_amounts(len(others), quiz.prize_consolation, quiz.prize_min)
        for offset, (row, coins) in enumerate(zip(others, schedule)):
            if coins > 0:
                rank = offset + 4
                await _pay(row.user_tg_id, coins, rank, "consolation", f"consolazione #{rank}")
        return awards

    # Legacy single-pool model (unchanged behaviour).
    if quiz.prize_coins > 0:
        top = ranked[:3]
        distributed = 0
        for i, row in enumerate(top):
            coins = (
                quiz.prize_coins - distributed
                if i == len(top) - 1
                else int(quiz.prize_coins * _PRIZE_SPLIT[i])
            )
            distributed += coins
            if coins > 0:
                await _pay(row.user_tg_id, coins, i + 1, "podium", f"podio #{i + 1}")
    return awards
