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
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Integer, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config_data.config import settings
from database.models import (
    Quiz,
    QuizAnswer,
    QuizQuestion,
    ScheduledTask,
    TransactionType,
)
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
    randomize_questions: bool = False,
    randomize_answers: bool = False,
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
        randomize_questions=randomize_questions,
        randomize_answers=randomize_answers,
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
    time_limit_seconds: int = 0,
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
        open_period=max(0, time_limit_seconds),  # per-question time limit (0 = none)
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


async def get_question(session: AsyncSession, question_id: int) -> QuizQuestion | None:
    return (
        await session.execute(select(QuizQuestion).where(QuizQuestion.id == question_id))
    ).scalar_one_or_none()


# Sentinel so ``update_question`` can tell "leave the explanation as is" apart from
# "clear the explanation" (``None``).
_UNSET = object()


async def update_question(
    session: AsyncSession,
    question_id: int,
    *,
    text: str | None = None,
    options: list[str] | None = None,
    correct_option_id: int | None = None,
    explanation=_UNSET,
) -> bool:
    """Edit a single question in place. No commit (STEERING §5).

    Returns ``False`` if the question is missing or its quiz is **not editable**:
    only quizzes still in ``ready`` may be edited, so a running/finished quiz (whose
    recorded answers reference the stored option order) can never be corrupted.

    Partial update — only the passed fields change. ``explanation`` uses a sentinel
    so ``None`` (clear it) is distinguishable from "not passed". When ``options``
    change the caller re-picks ``correct_option_id`` (indices may shift); either way
    the correct index is validated against the resulting option list.
    """
    question = await get_question(session, question_id)
    if question is None:
        return False
    quiz = (
        await session.execute(select(Quiz).where(Quiz.id == question.quiz_id))
    ).scalar_one_or_none()
    if quiz is None or quiz.status != "ready":
        return False

    new_options = question_options(question) if options is None else options
    new_correct = question.correct_option_id if correct_option_id is None else correct_option_id
    if not (0 <= new_correct < len(new_options)):
        return False

    if text is not None:
        question.text = text[:300]
    if options is not None:
        question.options_json = json.dumps(new_options, ensure_ascii=False)
    if options is not None or correct_option_id is not None:
        question.correct_option_id = new_correct
    if explanation is not _UNSET:
        question.explanation = (explanation or None) and explanation[:200]
    await session.flush()
    return True


def question_options(question: QuizQuestion) -> list[str]:
    return json.loads(question.options_json)


def time_limit_seconds(quiz: Quiz) -> int:
    """Per-question time limit of a quiz (uniform across questions; 0 = none).

    Read from the first question's ``open_period`` — the creation flow sets the
    same limit on every question. 0 when the quiz has no questions yet.
    """
    return quiz.questions[0].open_period if quiz.questions else 0


def user_question_order(quiz: Quiz, user_tg_id: int) -> list[QuizQuestion]:
    """Question order as seen by one player.

    Deterministic per (quiz, run, user) so resuming after a bot restart shows the
    same order, but a "Riproponi" re-run (which clears ``started_at``) reshuffles.
    Identity order (creation order) when ``randomize_questions`` is off.
    """
    if not quiz.randomize_questions:
        return quiz.questions
    order = list(quiz.questions)
    seed = f"{quiz.id}:{quiz.started_at.isoformat() if quiz.started_at else ''}:{user_tg_id}"
    random.Random(seed).shuffle(order)
    return order


def user_option_order(quiz: Quiz, question: QuizQuestion, user_tg_id: int) -> list[tuple[int, str]]:
    """Answer options for one player as ``(real_index, text)`` pairs, in display order.

    ``real_index`` always refers to the stored order (matches ``correct_option_id``),
    so callers can use it directly as the answer's callback payload regardless of
    display order. Identity order when ``randomize_answers`` is off.
    """
    pairs = list(enumerate(question_options(question)))
    if not quiz.randomize_answers:
        return pairs
    seed = f"{quiz.id}:{question.id}:{user_tg_id}"
    random.Random(seed).shuffle(pairs)
    return pairs


async def get_quiz(session: AsyncSession, quiz_id: int) -> Quiz | None:
    """Load a quiz with its questions. **Read-only by contract.**

    There is deliberately no ``for_update`` here any more. Locking an entity select
    reads as a guarantee it cannot give: the row does get locked on Postgres, but
    the values come back from the identity map when the caller already holds the
    quiz, so the check that follows can be decided on stale data (STEERING §5).
    Both callers that used to do that — ``claim_close`` and ``reset_quiz`` — now
    decide on a column read instead, which is the only form that cannot be cached.
    """
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


async def list_manageable(session: AsyncSession, *, finished_limit: int = 10) -> list[Quiz]:
    """All non-``draft`` quizzes for the admin events hub — quizzes persist as
    standalone objects instead of vanishing after a run.

    Ordered running → ready → finished (recent first). Finished quizzes are kept
    as an archive but capped to ``finished_limit`` so the list can't grow unbounded.
    """
    result = await session.execute(
        select(Quiz)
        .where(Quiz.status.in_(("ready", "running", "finished")))
        .options(selectinload(Quiz.questions))
        .order_by(Quiz.created_at.desc())
    )
    quizzes = list(result.scalars().all())
    active = [q for q in quizzes if q.status in ("running", "ready")]
    active.sort(key=lambda q: 0 if q.status == "running" else 1)  # running first (stable)
    finished = [q for q in quizzes if q.status == "finished"][:finished_limit]
    return active + finished


QUIZ_MISSING = "missing"


async def claim_close(session: AsyncSession, quiz_id: int) -> str | None:
    """Take a running quiz to ``finished``, and report whether *this* call did it.

    Returns ``None`` when this call performed the transition — and at most one
    caller can ever get ``None`` for a given quiz, which is what makes it safe to
    pay the prizes right after. Otherwise returns whatever blocked it: the current
    status (``"finished"``, ``"draft"``, …) or ``QUIZ_MISSING``.

    The transition **is** the guard, deliberately. Reading the status, deciding,
    and then paying is a read-then-write, and locking the row does not repair it:
    a caller that already holds the quiz gets its cached status back from the
    locking read (see STEERING §5), so two closes could both pass the check and
    pay the pool twice.
    """
    changed = (
        await session.execute(
            update(Quiz)
            .where(Quiz.id == quiz_id, Quiz.status == "running")
            # coalesce, so a re-run keeps the original close time (same rule as
            # set_status, which only fills finished_at when it is still empty).
            .values(status="finished", finished_at=func.coalesce(Quiz.finished_at, _now()))
            .execution_options(synchronize_session=False)
        )
    ).rowcount or 0
    if changed:
        return None

    status = (
        await session.execute(select(Quiz.status).where(Quiz.id == quiz_id))
    ).scalar_one_or_none()
    return status or QUIZ_MISSING


async def set_status(session: AsyncSession, quiz_id: int, status: str) -> None:
    quiz = (await session.execute(select(Quiz).where(Quiz.id == quiz_id))).scalar_one_or_none()
    if quiz is None:
        return
    quiz.status = status
    if status == "running" and quiz.started_at is None:
        quiz.started_at = _now()
    if status == "finished" and quiz.finished_at is None:
        quiz.finished_at = _now()


async def delete_quiz(session: AsyncSession, quiz_id: int) -> bool:
    """Permanently remove a quiz and its play data. Returns True if it existed.

    Answers are deleted explicitly (they have no ORM relationship to the quiz, only
    an indexed ``quiz_id`` — so we can't rely on the SQLite FK cascade being on);
    questions cascade via the ``Quiz.questions`` relationship. Any *pending* scheduled
    launch of this quiz is cancelled so the scheduler won't fire on a missing quiz.
    Historical progress rows (``game_podiums`` / ``user_metrics``, keyed by ``ref_id``)
    are intentionally left intact.
    """
    quiz = await get_quiz(session, quiz_id)
    if quiz is None:
        return False
    await session.execute(delete(QuizAnswer).where(QuizAnswer.quiz_id == quiz_id))
    await session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.task_type == "quiz",
            ScheduledTask.ref_id == quiz_id,
            ScheduledTask.status == "pending",
        )
        .values(status="cancelled")
    )
    await session.delete(quiz)
    await session.flush()
    return True


async def reset_quiz(session: AsyncSession, quiz_id: int) -> bool:
    """Re-run a finished quiz ("Riproponi"): wipe answers/timestamps and set it
    back to ``ready`` so it can be launched again. Only allowed on a ``finished``
    quiz. Returns False if the quiz is missing or not finished.

    A reset deletes every recorded answer, so the status check is the only thing
    between a mistap and destroying live play. It therefore reads the **column**
    under the lock rather than the entity: an entity select can be served from the
    identity map (STEERING §5), which would make this a lookup of whatever the
    caller already believed instead of what the row says.

    The writes below stay ORM mutations on purpose. Every one of them assigns a
    constant — never a delta — so they are correct even on a stale instance, and
    keeping them on the entity is what lets the caller re-render the quiz right
    after (``cb_reset`` does) without reloading anything.
    """
    status = (
        await session.execute(
            select(Quiz.status).where(Quiz.id == quiz_id).with_for_update()
        )
    ).scalar_one_or_none()
    if status != "finished":
        return False
    quiz = await get_quiz(session, quiz_id)
    if quiz is None:  # pragma: no cover - the locked row cannot vanish under us
        return False
    await session.execute(delete(QuizAnswer).where(QuizAnswer.quiz_id == quiz_id))
    for question in quiz.questions:
        question.tg_poll_id = None
        question.sent_at = None
    quiz.started_at = None
    quiz.finished_at = None
    quiz.status = "ready"
    await session.flush()
    return True


async def answer_stats(session: AsyncSession, quiz_id: int) -> tuple[int, int]:
    """(distinct participants, total answers) recorded for a quiz — for the detail view."""
    row = (
        await session.execute(
            select(func.count(func.distinct(QuizAnswer.user_tg_id)), func.count())
            .select_from(QuizAnswer)
            .where(QuizAnswer.quiz_id == quiz_id)
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


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
    response_ms: int = 0,
) -> AnswerOutcome | None:
    """Record a private-chat answer. Returns None if the question is invalid.

    ``response_ms`` is how long the user took on this question; ``selected_option_id
    = -1`` marks a timeout (no option chosen → wrong). Idempotent per (question,
    user): a duplicate returns recorded=False.
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
            response_ms=max(0, response_ms),
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
    completion_ms: int = 0                  # sum of per-question response times (ms)
    completion_seconds: int | None = None   # same, rounded to seconds, for display


async def podium(session: AsyncSession, quiz_id: int) -> list[PodiumRow]:
    """Finishers (answered every question) ranked by correct DESC, then by the
    player's **completion time ASC** (fastest first), with the absolute
    ``finished_at`` (arrival order) only as a final tie-break.

    Each row carries ``completion_ms``/``completion_seconds`` = the **sum of the
    player's per-question response times** (so it includes the first question and
    works for a single-question quiz), measuring how long *that user* actually
    took. On equal correct answers the faster player ranks higher (§19).
    """
    total = await total_questions(session, quiz_id)
    if total == 0:
        return []
    result = await session.execute(
        select(
            QuizAnswer.user_tg_id,
            func.sum(func.cast(QuizAnswer.is_correct, Integer)).label("correct"),
            func.count().label("answered"),
            func.max(QuizAnswer.answered_at).label("finished_at"),
            func.sum(QuizAnswer.response_ms).label("total_ms"),
        )
        .where(QuizAnswer.quiz_id == quiz_id)
        .group_by(QuizAnswer.user_tg_id)
    )
    rows: list[PodiumRow] = []
    for r in result.all():
        answered = int(r[2] or 0)
        if answered < total:
            continue
        total_ms = int(r[4] or 0)
        rows.append(
            PodiumRow(user_tg_id=r[0], correct=int(r[1] or 0),
                      finished_at=r[3], completion_ms=total_ms,
                      completion_seconds=round(total_ms / 1000))
        )
    # Most correct first; on a tie the faster player (lower completion_ms) wins,
    # and only if those also tie do we fall back to who arrived first.
    rows.sort(key=lambda r: (-r.correct, r.completion_ms, r.finished_at))
    return rows


async def user_completion_seconds(
    session: AsyncSession, quiz_id: int, user_tg_id: int
) -> int | None:
    """Seconds the user actually spent playing: the sum of their per-question
    response times. Works with a single question too; ``None`` only when the user
    has not answered anything yet.
    """
    total_ms, n = (
        await session.execute(
            select(func.sum(QuizAnswer.response_ms), func.count()).where(
                QuizAnswer.quiz_id == quiz_id, QuizAnswer.user_tg_id == user_tg_id
            )
        )
    ).one()
    if int(n or 0) < 1:
        return None
    return round(int(total_ms or 0) / 1000)


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


async def _grant_xp(session: AsyncSession, quiz_id: int, ranked: list[PodiumRow]) -> None:
    """Event XP for a closed quiz (uncapped) — rewards participation, not just wins:

    * everyone who answered at least one question gets ``quiz_xp_participation``;
    * each correct answer adds ``quiz_xp_per_correct``;
    * the top-3 finishers get an extra podium bonus (1st/2nd/3rd).

    Quiz is an admin-curated event, so all of it is uncapped, via the single XP mutator.
    """
    participation = max(0, settings.quiz_xp_participation)
    per_correct = max(0, settings.quiz_xp_per_correct)

    if participation or per_correct:
        stats = await session.execute(
            select(
                QuizAnswer.user_tg_id,
                func.sum(func.cast(QuizAnswer.is_correct, Integer)),
            )
            .where(QuizAnswer.quiz_id == quiz_id)
            .group_by(QuizAnswer.user_tg_id)
        )
        for uid, correct in stats.all():
            amount = participation + int(correct or 0) * per_correct
            if amount > 0:
                await xp_service.grant_xp(session, uid, amount, XpSource.quiz, capped=False)

    podium_bonus = (
        settings.quiz_xp_podium_first,
        settings.quiz_xp_podium_second,
        settings.quiz_xp_podium_third,
    )
    for i, row in enumerate(ranked[:3]):
        bonus = max(0, podium_bonus[i])
        if bonus > 0:
            await xp_service.grant_xp(session, row.user_tg_id, bonus, XpSource.quiz, capped=False)


async def award_prizes(session: AsyncSession, quiz_id: int) -> list[PrizeAward]:
    """Pay coin prizes to the finishers and grant event XP (participation +
    correct answers + podium bonus, see ``_grant_xp``). No commit.

    Explicit per-rank model (preferred): podium gets first/second/third; every
    finisher below the podium gets a consolation decreasing linearly from
    `prize_consolation` (4th) down to `prize_min` (last). Legacy model: a single
    pool split 50/30/20 among the top 3.
    """
    quiz = (await session.execute(select(Quiz).where(Quiz.id == quiz_id))).scalar_one_or_none()
    if quiz is None:
        return []

    ranked = await podium(session, quiz_id)
    # Grant XP first — participation/correctness XP reaches every player who answered,
    # even when nobody finished (ranked == []); the podium bonus uses `ranked`.
    await _grant_xp(session, quiz_id, ranked)
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
        # strict=True: `schedule` is built with len(others) entries, so a length
        # mismatch would mean silently paying a subset of the finishers.
        for offset, (row, coins) in enumerate(zip(others, schedule, strict=True)):
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
