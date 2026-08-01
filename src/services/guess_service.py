"""Guess games engine — rounds, per-player sessions, attempts, standings, prizes.

Both games (image and audio) run on this one module, discriminated by
``GuessRound.kind``. Duplicating it would mean duplicating a path that pays coins.

Every state transition is a **conditional UPDATE**, and ``rowcount == 0`` means
"someone else got there first" (STEERING §22). Two of them carry the game: the
close claim (``WHERE status = 'running'``), so two admins closing at once pay the
pool once; and the solve claim (``WHERE solved_at IS NULL``), so a double-tapped
winning answer ranks and pays once.

No commits (STEERING §5): the caller owns the transaction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import (
    GuessAttempt,
    GuessRound,
    GuessSession,
    ScheduledTask,
    TransactionType,
)
from services import economy_service, xp_service
from services.guess_judge import WRONG, Verdict, aliases_of, normalize
from services.prizes import consolation_amounts, participation_floor
from services.xp_service import XpSource

log = logging.getLogger(__name__)

ROUND_MISSING = "missing"


def now() -> datetime:
    """The single definition of "adesso" in the engine — UTC naive, like the
    scheduler (STEERING rule 17). Exported because `handlers.guess.play` compares
    against it and two definitions of now would drift."""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def create_round(
    session: AsyncSession,
    *,
    kind: str,
    creator_tg_id: int,
    title: str,
    media_file_id: str,
    media_kind: str,
    answer: str,
    aliases: list[str],
    hints: list[tuple[int, str]],
    max_attempts: int,
    time_limit_seconds: int,
    round_duration_seconds: int = 0,
    prize_first: int = 0,
    prize_second: int = 0,
    prize_third: int = 0,
    prize_consolation: int = 0,
    group_id: int | None = None,
) -> GuessRound:
    """Create a round as ``draft``. The caller arms it as ``ready`` once it is
    complete, so a half-built round can never be started."""
    round_ = GuessRound(
        kind=kind,
        title=title[:256],
        creator_tg_id=creator_tg_id,
        status="draft",
        group_id=group_id,
        media_file_id=media_file_id[:256],
        media_kind=media_kind,
        answer=answer[:200],
        aliases_json=json.dumps(aliases, ensure_ascii=False) if aliases else None,
        hints_json=(
            json.dumps([{"after": a, "text": t} for a, t in hints], ensure_ascii=False)
            if hints else None
        ),
        max_attempts=max(1, max_attempts),
        time_limit_seconds=max(0, time_limit_seconds),
        round_duration_seconds=max(0, round_duration_seconds),
        prize_first=max(0, prize_first),
        prize_second=max(0, prize_second),
        prize_third=max(0, prize_third),
        prize_consolation=max(0, prize_consolation),
        # Derived, never asked: one floor rule for every game with a podium.
        prize_min=participation_floor(max(0, prize_consolation)),
    )
    session.add(round_)
    await session.flush()
    return round_


async def get_round(session: AsyncSession, round_id: int) -> GuessRound | None:
    """Load a round. **Read-only by contract** — deliberately no ``for_update``:
    locking an entity select reads as a guarantee it cannot give, because the
    values come back from the identity map when the caller already holds the
    row (STEERING §22)."""
    return (
        await session.execute(select(GuessRound).where(GuessRound.id == round_id))
    ).scalar_one_or_none()


async def list_manageable(
    session: AsyncSession, kind: str, *, finished_limit: int = 10
) -> list[GuessRound]:
    """Non-draft rounds of one kind for the admin hub: running → ready →
    finished (recent first, capped so the list cannot grow unbounded)."""
    rows = list((
        await session.execute(
            select(GuessRound)
            .where(GuessRound.kind == kind,
                   GuessRound.status.in_(("ready", "running", "finished")))
            .order_by(GuessRound.created_at.desc(), GuessRound.id.desc())
        )
    ).scalars().all())
    active = [r for r in rows if r.status in ("running", "ready")]
    active.sort(key=lambda r: 0 if r.status == "running" else 1)  # running first (stable)
    return active + [r for r in rows if r.status == "finished"][:finished_limit]


async def list_ready(session: AsyncSession, kind: str) -> list[GuessRound]:
    """Rounds of one kind that can be scheduled right now."""
    return list((
        await session.execute(
            select(GuessRound)
            .where(GuessRound.kind == kind, GuessRound.status == "ready")
            .order_by(GuessRound.created_at.desc(), GuessRound.id.desc())
        )
    ).scalars().all())


async def set_status(session: AsyncSession, round_id: int, status: str) -> None:
    round_ = await get_round(session, round_id)
    if round_ is None:
        return
    round_.status = status
    if status == "running" and round_.started_at is None:
        round_.started_at = now()
    if status == "finished" and round_.finished_at is None:
        round_.finished_at = now()


async def _sync_round_state(session: AsyncSession, round_id: int) -> None:
    """Make the in-session entity reflect a status written in raw SQL.

    Every state write here uses ``synchronize_session=False`` (STEERING §22 rule
    3), which leaves any instance already in the identity map saying the old
    thing. That is not cosmetic: the events hub re-renders the round straight
    after closing or resetting it, and an entity select is answered from that map
    — so without this the admin closes a round and the screen still shows it
    "in corso".

    Rule 3's second half is exactly this refresh, and it is scoped to the columns
    that changed so eagerly-loaded state is left alone.
    """
    round_ = await session.get(GuessRound, round_id)
    if round_ is not None:
        await session.refresh(round_, ["status", "started_at", "finished_at"])


async def claim_close(session: AsyncSession, round_id: int) -> str | None:
    """Take a running round to ``finished`` and report whether *this* call did it.

    ``None`` means this call performed the transition — and at most one caller can
    ever get it for a given round, which is what makes it safe to pay the prizes
    right after. Otherwise: the status that blocked it, or ``ROUND_MISSING``.

    The transition **is** the guard, deliberately. Reading the status, deciding,
    then paying is a read-then-write, and locking the row does not repair it: a
    caller that already holds the round gets its cached status back from the
    locking read (STEERING §22), so two closes could both pass and pay twice.
    """
    changed = (
        await session.execute(
            update(GuessRound)
            .where(GuessRound.id == round_id, GuessRound.status == "running")
            # coalesce: a re-run keeps the original close time.
            .values(status="finished", finished_at=func.coalesce(GuessRound.finished_at, now()))
            .execution_options(synchronize_session=False)
        )
    ).rowcount or 0
    if changed:
        await _sync_round_state(session, round_id)
        return None
    status = (
        await session.execute(select(GuessRound.status).where(GuessRound.id == round_id))
    ).scalar_one_or_none()
    return status or ROUND_MISSING


async def delete_round(session: AsyncSession, round_id: int) -> bool:
    """Remove a round with its sessions and attempts, and cancel any pending
    scheduled task for it.

    Play data is deleted explicitly rather than trusting a cascade: the SQLite
    test DB does not enforce FKs by default. Progress rows (``game_podiums``) are
    left intact — they are history, not state.
    """
    round_ = await get_round(session, round_id)
    if round_ is None:
        return False
    await session.execute(delete(GuessAttempt).where(GuessAttempt.round_id == round_id))
    await session.execute(delete(GuessSession).where(GuessSession.round_id == round_id))
    await session.execute(
        update(ScheduledTask)
        .where(ScheduledTask.task_type == round_.kind,
               ScheduledTask.ref_id == round_id,
               ScheduledTask.status == "pending")
        .values(status="cancelled")
    )
    await session.delete(round_)
    await session.flush()
    return True


async def reset_round(session: AsyncSession, round_id: int) -> bool:
    """Re-run a finished round ("Riproponi"): wipe play data, back to ``ready``.

    Reads the status as a **column** under the lock, never as an entity: this call
    deletes every attempt, and the status check is the only thing between a mistap
    and destroying live play — it has to look at the row, not at whatever the
    caller already believed (STEERING §22).

    **Prizes already paid stay paid**, exactly like ``quiz_service.reset_quiz``: a
    re-run is a new event, not a correction of the old one.
    """
    status = (
        await session.execute(
            select(GuessRound.status).where(GuessRound.id == round_id).with_for_update()
        )
    ).scalar_one_or_none()
    if status != "finished":
        return False
    await session.execute(delete(GuessAttempt).where(GuessAttempt.round_id == round_id))
    await session.execute(delete(GuessSession).where(GuessSession.round_id == round_id))
    await session.execute(
        update(GuessRound)
        .where(GuessRound.id == round_id)
        .values(status="ready", started_at=None, finished_at=None)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    await _sync_round_state(session, round_id)
    return True


#: `GuessRound.aliases_json` is a String(1024): a longer write is an error on
#: Postgres and a silent truncation nowhere. Additions stop at the width.
_MAX_ALIASES_JSON = 1024


async def add_aliases(
    session: AsyncSession, round_id: int, new: list[str]
) -> tuple[int, int] | None:
    """Accept more spellings on a round that is already out. No commit.

    Returns ``(added, skipped)``, or ``None`` if the round is gone. Skipped covers
    both duplicates (normalised, so «GTA SA» twice in two shapes counts once) and
    anything that no longer fits the column.

    Deliberately forward-only: the attempts already judged are left alone. An
    alias is consulted **before** the cached verdict (``guess_judge.judge``), so
    the next player to type it wins — including the one who was turned down, if
    they try again. Re-judging the past would mean paying a podium that was
    already announced.
    """
    round_ = await get_round(session, round_id)
    if round_ is None:
        return None
    current = aliases_of(round_)
    seen = {normalize(a) for a in current} | {normalize(round_.answer)}
    added: list[str] = []
    for alias in new:
        cleaned = alias.strip()[:100]
        key = normalize(cleaned)
        if not key or key in seen:
            continue
        candidate = json.dumps(current + added + [cleaned], ensure_ascii=False)
        if len(candidate) > _MAX_ALIASES_JSON:
            break
        seen.add(key)
        added.append(cleaned)
    if added:
        round_.aliases_json = json.dumps(current + added, ensure_ascii=False)
        await session.flush()
    return len(added), len(new) - len(added)


def hints_of(round_: GuessRound) -> list[tuple[int, str]]:
    """``(after_attempts, text)`` pairs, ordered. Corrupt JSON reads as none — a
    broken hint list must cost the hints, not the whole round."""
    if not round_.hints_json:
        return []
    try:
        raw = json.loads(round_.hints_json)
    except (ValueError, TypeError):
        log.warning("hints_json illeggibile sul round %s", round_.id)
        return []
    if not isinstance(raw, list):
        return []
    out = [
        (int(h["after"]), str(h["text"]))
        for h in raw
        if isinstance(h, dict) and "after" in h and "text" in h
    ]
    return sorted(out)


async def play_stats(session: AsyncSession, round_id: int) -> tuple[int, int]:
    """``(distinct players, total attempts)`` for the admin detail screen."""
    row = (
        await session.execute(
            select(func.count(func.distinct(GuessAttempt.user_tg_id)), func.count())
            .select_from(GuessAttempt)
            .where(GuessAttempt.round_id == round_id)
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


async def recent_rejected(
    session: AsyncSession, round_id: int, *, limit: int = 5
) -> list[str]:
    """The last few answers the judge turned down, most recent first.

    This is the audit trail that makes a bad verdict noticeable. The judge is an
    LLM: it will occasionally reject something it should have accepted, and
    without a list of what it rejected nobody would ever find out — the player
    just loses and says nothing. Seeing them, an admin can add an alias.

    Deduplicated, because ten people typing the same wrong title is one fact.
    """
    rows = (
        await session.execute(
            select(GuessAttempt.raw_answer)
            .where(GuessAttempt.round_id == round_id, GuessAttempt.verdict == WRONG)
            .order_by(GuessAttempt.id.desc())
        )
    ).scalars().all()
    seen: list[str] = []
    for answer in rows:
        if answer not in seen:
            seen.append(answer)
        if len(seen) >= limit:
            break
    return seen


def format_prize_summary(round_: GuessRound) -> str:
    """One-line, human-readable summary of a round's prize structure."""
    parts = []
    if round_.prize_first:
        parts.append(f"🥇 {round_.prize_first}")
    if round_.prize_second:
        parts.append(f"🥈 {round_.prize_second}")
    if round_.prize_third:
        parts.append(f"🥉 {round_.prize_third}")
    if round_.prize_consolation:
        parts.append(f"🎖️ 4°: {round_.prize_consolation} → min {round_.prize_min}")
    return " · ".join(parts) if parts else "nessun premio"


# ---------------------------------------------------------------------------
# Per-player session
# ---------------------------------------------------------------------------

async def get_session(
    session: AsyncSession, round_id: int, user_tg_id: int
) -> GuessSession | None:
    return (
        await session.execute(
            select(GuessSession).where(
                GuessSession.round_id == round_id,
                GuessSession.user_tg_id == user_tg_id,
            )
        )
    ).scalar_one_or_none()


async def start_or_resume(
    session: AsyncSession, round_id: int, user_tg_id: int
) -> GuessSession:
    """The player's session, created on first entry and **never restarted**.

    Re-entering must not reset the clock, or a time limit becomes an infinite
    timer for anyone who leaves and comes back.

    Select + conditional add, no ``IntegrityError`` branch — the same shape as
    ``progress_service.record_event`` (STEERING rule 3). Catching it here would
    mean a ``rollback`` that discards the whole request's transaction to repair a
    row that costs nothing; the race that actually matters is the double-tapped
    attempt, and *that* one is caught where the unique key is the expected signal.
    """
    existing = await get_session(session, round_id, user_tg_id)
    if existing is not None:
        return existing
    sess = GuessSession(round_id=round_id, user_tg_id=user_tg_id, started_at=now())
    session.add(sess)
    await session.flush()
    return sess


def deadline(round_: GuessRound, sess: GuessSession) -> datetime | None:
    """When this player's time runs out; ``None`` when the round has no limit.

    Derived, not stored, and checked on submission — no asyncio timer, no
    in-memory map, nothing to lose on a restart. The quiz needs timers because its
    clock is per question; here it is per session, and the simpler shape is the
    safer one.
    """
    if round_.time_limit_seconds <= 0:
        return None
    return sess.started_at + timedelta(seconds=round_.time_limit_seconds)


async def _counters(
    session: AsyncSession, round_id: int, user_tg_id: int
) -> tuple[int, int]:
    """``(rows written, of which un-judged)`` for this player on this round.

    Read as **columns**, so it is the row's truth and not a copy the caller might
    already be holding (STEERING §22 rule 1).

    The two are separate on purpose. ``attempts_used`` counts rows and must stay
    monotonic — it drives ``attempt_no``, which is part of the unique key, so two
    submissions may never share a number. ``unverified_count`` says how many of
    those rows are not a verdict. The budget is the difference.
    """
    row = (
        await session.execute(
            select(GuessSession.attempts_used, GuessSession.unverified_count).where(
                GuessSession.round_id == round_id,
                GuessSession.user_tg_id == user_tg_id,
            )
        )
    ).one_or_none()
    return (0, 0) if row is None else (int(row[0]), int(row[1]))


def _budget_left(max_attempts: int, used: int, unverified: int) -> int:
    """The budget rule, in one place.

    An answer the judge never ruled on does **not** count. Not "is refunded up to
    a cap" — does not count, ever. Capping the refund meant that past the cap our
    own outage started spending the player's budget, and with the judge failing on
    every call that is precisely what happened: players ran out of attempts
    without one answer having been judged. The bound now lives in
    `unverified_left`, on how many un-judged answers we accept.
    """
    return max(0, max_attempts - (used - unverified))


async def attempts_left(
    session: AsyncSession, round_: GuessRound, user_tg_id: int
) -> int:
    """Budget remaining for this player."""
    used, unverified = await _counters(session, round_.id, user_tg_id)
    return _budget_left(round_.max_attempts, used, unverified)


async def unverified_left(
    session: AsyncSession, round_: GuessRound, user_tg_id: int
) -> int:
    """How many more un-judged answers we will take from this player.

    At zero the caller must stop **before** the judge: refunding without a bound
    would be an unlimited submission channel at the exact moment the local
    exact-match path is all that stands between a player and brute force.
    """
    _used, unverified = await _counters(session, round_.id, user_tg_id)
    return max(0, settings.guess_max_unverified - unverified)


@dataclass(frozen=True)
class Attempt:
    recorded: bool       # False = this attempt number already existed (double tap)
    verdict: Verdict
    attempt_no: int
    attempts_left: int
    solved: bool         # True only for the call that claimed the solve
    hint: str | None     # hint newly unlocked by this attempt


async def record_attempt(
    session: AsyncSession,
    round_: GuessRound,
    user_tg_id: int,
    raw_answer: str,
    verdict: Verdict,
) -> Attempt:
    """Persist one submission and apply its consequences. No commit.

    Everything read off an ORM instance is read **before** the flush below. The
    ``IntegrityError`` branch rolls back, and a rollback expires every instance in
    the session: touching an attribute afterwards would trigger a lazy reload,
    which in async is a ``MissingGreenlet``. This is the same defect that was
    found and fixed in ``quiz_service.record_answer`` — do not reorder these reads.
    """
    round_id = round_.id
    hints = hints_of(round_)

    sess = await start_or_resume(session, round_id, user_tg_id)
    attempt_no = sess.attempts_used + 1
    elapsed_ms = max(0, round((now() - sess.started_at).total_seconds() * 1000))

    session.add(GuessAttempt(
        round_id=round_id,
        user_tg_id=user_tg_id,
        attempt_no=attempt_no,
        raw_answer=raw_answer[:200],
        normalized=normalize(raw_answer),
        verdict=verdict.stored_verdict,
        source=verdict.source,
        elapsed_ms=elapsed_ms,
        created_at=now(),
    ))
    try:
        await session.flush()
    except IntegrityError:
        # A concurrent double tap raced past the counter read. The unique key on
        # (round, user, attempt_no) is the *expected* signal here, so this is a
        # duplicate to report, not an error to raise.
        await session.rollback()
        return Attempt(recorded=False, verdict=verdict, attempt_no=attempt_no,
                       attempts_left=0, solved=False, hint=None)

    # Counters in SQL, relative: `attempts_used + 1` sums; an assignment computed
    # from a possibly-stale base overwrites (STEERING §22 rule 2).
    await session.execute(
        update(GuessSession)
        .where(GuessSession.round_id == round_id, GuessSession.user_tg_id == user_tg_id)
        .values(
            attempts_used=GuessSession.attempts_used + 1,
            unverified_count=GuessSession.unverified_count + (0 if verdict.verified else 1),
        )
        .execution_options(synchronize_session=False)
    )

    solved = False
    if verdict.correct and verdict.verified:
        # The claim IS the transition: `WHERE solved_at IS NULL` means at most one
        # call can ever win it, so a double tap ranks and pays once.
        solved = bool((
            await session.execute(
                update(GuessSession)
                .where(GuessSession.round_id == round_id,
                       GuessSession.user_tg_id == user_tg_id,
                       GuessSession.solved_at.is_(None))
                .values(solved_at=now(), solved_attempts=attempt_no, solve_ms=elapsed_ms)
                .execution_options(synchronize_session=False)
            )
        ).rowcount or 0)

    await session.flush()
    # Hints unlock on the count of JUDGED answers, not on the row number. Keyed to
    # `attempt_no`, an unverified attempt landing on a threshold consumed that
    # hint and nobody ever saw it — a hint lost to an outage the player did not
    # cause. `judged` has no gaps, so the exact match is still the right test.
    used, unverified = await _counters(session, round_id, user_tg_id)
    judged = used - unverified
    hint = next((text for after, text in hints if after == judged), None)
    left = _budget_left(round_.max_attempts, used, unverified)
    return Attempt(recorded=True, verdict=verdict, attempt_no=attempt_no,
                   attempts_left=left, solved=solved,
                   hint=None if solved else hint)


# ---------------------------------------------------------------------------
# Standings + prizes
# ---------------------------------------------------------------------------

@dataclass
class Standing:
    user_tg_id: int
    attempts: int
    solve_ms: int
    solved_at: datetime


async def standings(session: AsyncSession, round_id: int) -> list[Standing]:
    """Solvers only, ranked by **fewest attempts**, then by time, with arrival
    order as the final tie-break.

    Non-solvers are not ranked: here "finisher" means "guessed it", which is what
    makes fewer attempts worth something.
    """
    rows = (
        await session.execute(
            select(
                GuessSession.user_tg_id,
                GuessSession.solved_attempts,
                GuessSession.solve_ms,
                GuessSession.solved_at,
            ).where(
                GuessSession.round_id == round_id,
                GuessSession.solved_at.is_not(None),
            )
        )
    ).all()
    out = [
        Standing(user_tg_id=r[0], attempts=int(r[1] or 0),
                 solve_ms=int(r[2] or 0), solved_at=r[3])
        for r in rows
    ]
    out.sort(key=lambda s: (s.attempts, s.solve_ms, s.solved_at))
    return out


@dataclass
class PrizeAward:
    user_tg_id: int
    rank: int
    coins: int
    kind: str = "podium"  # "podium" | "consolation"


async def _grant_xp(session: AsyncSession, round_id: int, ranked: list[Standing]) -> None:
    """Event XP for a closed round (uncapped — admin-gated, not farmable):

    * everyone who submitted at least one answer gets ``guess_xp_participation``;
    * solvers get ``guess_xp_solved`` on top;
    * the top three get a podium bonus.

    Rewards showing up, not just winning — the same shape as the quiz.
    """
    players = (
        await session.execute(
            select(GuessAttempt.user_tg_id)
            .where(GuessAttempt.round_id == round_id)
            .group_by(GuessAttempt.user_tg_id)
        )
    ).scalars().all()
    solvers = {s.user_tg_id for s in ranked}

    for uid in players:
        amount = max(0, settings.guess_xp_participation)
        if uid in solvers:
            amount += max(0, settings.guess_xp_solved)
        if amount > 0:
            await xp_service.grant_xp(session, uid, amount, XpSource.guess, capped=False)

    bonuses = (settings.guess_xp_podium_first, settings.guess_xp_podium_second,
               settings.guess_xp_podium_third)
    for i, row in enumerate(ranked[:3]):
        bonus = max(0, bonuses[i])
        if bonus > 0:
            await xp_service.grant_xp(session, row.user_tg_id, bonus,
                                      XpSource.guess, capped=False)


async def award_prizes(session: AsyncSession, round_id: int) -> list[PrizeAward]:
    """Pay the solvers and grant event XP. No commit.

    The podium takes first/second/third; every solver below it gets a consolation
    decreasing linearly from ``prize_consolation`` to ``prize_min`` — the shared
    schedule in ``services.prizes``.
    """
    round_ = await get_round(session, round_id)
    if round_ is None:
        return []

    ranked = await standings(session, round_id)
    # XP first: participation reaches everyone who played, even when nobody solved it.
    await _grant_xp(session, round_id, ranked)
    if not ranked:
        return []

    awards: list[PrizeAward] = []

    async def _pay(user_tg_id: int, coins: int, rank: int, kind: str, label: str) -> None:
        # `quiz_reward` is reused deliberately: it already reads as "prize from a
        # community game" in the ledger and in /storico. A new enum member would
        # be a data migration for a distinction no screen makes.
        await economy_service.credit(
            session, user_tg_id, coins, TransactionType.quiz_reward,
            f"Premio «{round_.title}» ({label})",
        )
        awards.append(PrizeAward(user_tg_id=user_tg_id, rank=rank, coins=coins, kind=kind))

    for i, row in enumerate(ranked[:3]):
        coins = (round_.prize_first, round_.prize_second, round_.prize_third)[i]
        if coins > 0:
            await _pay(row.user_tg_id, coins, i + 1, "podium", f"podio #{i + 1}")

    others = ranked[3:]
    schedule = consolation_amounts(len(others), round_.prize_consolation, round_.prize_min)
    # strict=True: `schedule` is built with len(others) entries, so a length
    # mismatch would mean silently paying a subset of the solvers.
    for offset, (row, coins) in enumerate(zip(others, schedule, strict=True)):
        if coins > 0:
            rank = offset + 4
            await _pay(row.user_tg_id, coins, rank, "consolation", f"consolazione #{rank}")
    return awards
