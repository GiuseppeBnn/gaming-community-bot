"""
Poll templates — pre-created polls an admin starts in the group or schedules,
mirroring how quizzes are pre-created.

Beyond a bare question + options, a poll can carry a **participation prize**
(CoInn + XP paid to every voter at close), an optional **description** shown in
the group next to the poll, and an optional absolute **auto-close** instant. A
poll has no "right" answer, so the prize is for taking part; the winning *option*
is announced at close from the poll's final tallies.

Votes are tracked in ``PollVote`` from ``poll_answer`` updates — the Bot API tells
us the per-option counts on ``stopPoll`` but never who voted, so a non-anonymous
poll is the only way to know whom to pay.

Lifecycle: ``ready`` → ``running`` → ``finished``. No-commit convention: callers
own the transaction (STEERING §5). The money path uses SQL-side arithmetic via
``economy_service`` / ``xp_service`` (STEERING §22).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import PollTemplate, PollVote, TransactionType
from exceptions.economy import WalletNotFoundError
from services import economy_service, xp_service

log = logging.getLogger(__name__)

POLL_MISSING = "missing"

#: Telegram's hard cap on a native poll question. The description is folded into
#: the question (a native poll has no description field), so the combined text is
#: capped here as a defensive backstop — the creation flow already validates it.
POLL_QUESTION_MAX = 300
#: Separator between the question and the folded-in description.
_DESC_SEP = "\n\n"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


def question_length(text: str) -> int:
    """Length of a poll question the way Telegram counts it: **UTF-16 code units**,
    not Python code points. An emoji like 🪙 is one code point but two UTF-16 units,
    so counting code points would under-count and let an over-limit question through
    to a rejected ``sendPoll``. Used to decide whether the prize/close info block
    still fits inside the poll question (STEERING §18.2)."""
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# Creation & reads
# ---------------------------------------------------------------------------

async def create_template(
    session: AsyncSession,
    creator_tg_id: int,
    question: str,
    options: list[str],
    group_id: int | None = None,
    *,
    description: str | None = None,
    prize_coins: int = 0,
    prize_xp: int = 0,
    closes_at: datetime | None = None,
) -> PollTemplate:
    poll = PollTemplate(
        question=question[:300],
        options_json=json.dumps(options, ensure_ascii=False),
        creator_tg_id=creator_tg_id,
        status="ready",
        group_id=group_id,
        description=(description or None),
        prize_coins=max(0, prize_coins),
        prize_xp=max(0, prize_xp),
        closes_at=closes_at,
    )
    session.add(poll)
    await session.flush()
    return poll


def render_question(poll: PollTemplate) -> str:
    """The text sent as the native poll's question: the question and, when set, the
    description on the lines below it (a native poll has no separate description
    field, so it lives *inside* the question — STEERING §18.2). Capped at Telegram's
    300-char limit as a backstop; the creation flow validates the combined length."""
    if poll.description:
        return f"{poll.question}{_DESC_SEP}{poll.description}"[:POLL_QUESTION_MAX]
    return poll.question[:POLL_QUESTION_MAX]


def format_reward_dm(poll: PollTemplate) -> str:
    """The private notification each voter gets when a rewarded poll closes,
    mirroring the admin manual-grant DM. Only called for a poll with a prize, so
    at least one half is non-empty."""
    parts: list[str] = []
    if poll.prize_coins > 0:
        parts.append(f"<b>{poll.prize_coins:,} CoInn</b> 🪙")
    if poll.prize_xp > 0:
        parts.append(f"<b>{poll.prize_xp:,} XP</b> ⚡")
    return "🏆 Hai ricevuto " + " + ".join(parts) + " per aver votato al sondaggio!"


def options_of(poll: PollTemplate) -> list[str]:
    try:
        data = json.loads(poll.options_json)
        return [str(o) for o in data] if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def has_prize(poll: PollTemplate) -> bool:
    return poll.prize_coins > 0 or poll.prize_xp > 0


def format_prize_summary(poll: PollTemplate) -> str:
    if not has_prize(poll):
        return "nessun premio"
    parts: list[str] = []
    if poll.prize_coins > 0:
        parts.append(f"{poll.prize_coins} 🪙 CoInn")
    if poll.prize_xp > 0:
        parts.append(f"{poll.prize_xp} ⚡ XP")
    return " + ".join(parts) + " a ogni votante"


async def get(session: AsyncSession, poll_id: int) -> PollTemplate | None:
    return (
        await session.execute(select(PollTemplate).where(PollTemplate.id == poll_id))
    ).scalar_one_or_none()


async def get_by_tg_poll_id(session: AsyncSession, tg_poll_id: str) -> PollTemplate | None:
    """The running poll a Telegram ``poll_answer`` update belongs to, if any."""
    return (
        await session.execute(
            select(PollTemplate).where(PollTemplate.tg_poll_id == tg_poll_id)
        )
    ).scalar_one_or_none()


async def list_ready(session: AsyncSession) -> list[PollTemplate]:
    """Only ``ready`` polls — a running or finished one cannot be (re)started, so
    this is what the scheduler and the «Programma» picker offer."""
    result = await session.execute(
        select(PollTemplate)
        .where(PollTemplate.status == "ready")
        .order_by(PollTemplate.created_at.desc())
    )
    return list(result.scalars().all())


async def list_manageable(
    session: AsyncSession, *, finished_limit: int = 10
) -> list[PollTemplate]:
    """All non-terminal polls plus a capped archive of finished ones, for the
    admin events hub. Ordered running → ready → finished (recent first).

    The legacy ``used`` status (rows sent fire-and-forget before this feature) is
    treated as finished so old polls still appear and can be deleted.
    """
    result = await session.execute(
        select(PollTemplate)
        .where(PollTemplate.status.in_(("ready", "running", "finished", "used")))
        .order_by(PollTemplate.created_at.desc())
    )
    polls = list(result.scalars().all())
    active = [p for p in polls if p.status in ("running", "ready")]
    active.sort(key=lambda p: 0 if p.status == "running" else 1)  # running first (stable)
    terminal = [p for p in polls if p.status in ("finished", "used")][:finished_limit]
    return active + terminal


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------

async def mark_running(
    session: AsyncSession,
    poll_id: int,
    *,
    message_id: int,
    chat_id: int,
    tg_poll_id: str,
) -> None:
    """Record the live-poll handles and flip ``ready`` → ``running``. These handles
    are what ``stopPoll`` and the payout need; without them a poll cannot be closed."""
    poll = await get(session, poll_id)
    if poll is None:
        return
    poll.status = "running"
    poll.message_id = message_id
    poll.chat_id = chat_id
    poll.tg_poll_id = tg_poll_id
    poll.used_at = _now()


async def mark_used(session: AsyncSession, poll_id: int) -> None:
    """Legacy terminal transition, kept for scheduled sends that never tracked a
    live poll (no prize, no close). New starts use ``mark_running`` instead."""
    poll = await get(session, poll_id)
    if poll is not None:
        poll.status = "used"
        poll.used_at = _now()


async def claim_close(session: AsyncSession, poll_id: int) -> str | None:
    """Take a running poll to ``finished``, and report whether *this* call did it.

    Returns ``None`` when this call performed the transition — at most one caller
    can ever win it for a given poll, which is what makes it safe to pay the prize
    right after. Otherwise returns whatever blocked it: the current status or
    ``POLL_MISSING``. The transition **is** the guard (STEERING §22).
    """
    changed = (
        await session.execute(
            update(PollTemplate)
            .where(PollTemplate.id == poll_id, PollTemplate.status == "running")
            .values(status="finished", used_at=func.coalesce(PollTemplate.used_at, _now()))
            .execution_options(synchronize_session=False)
        )
    ).rowcount or 0
    if changed:
        return None
    status = (
        await session.execute(select(PollTemplate.status).where(PollTemplate.id == poll_id))
    ).scalar_one_or_none()
    return status or POLL_MISSING


async def delete_poll(session: AsyncSession, poll_id: int) -> bool:
    """Delete a poll and its recorded votes. Returns False if it was already gone.

    The vote rows are removed explicitly rather than relying on the FK's
    ``ON DELETE CASCADE``: SQLite does not enforce it without a per-connection
    pragma, and there is no ORM relationship to cascade through.
    """
    poll = await get(session, poll_id)
    if poll is None:
        return False
    await session.execute(delete(PollVote).where(PollVote.poll_id == poll_id))
    await session.delete(poll)
    return True


# ---------------------------------------------------------------------------
# Votes
# ---------------------------------------------------------------------------

async def record_vote(
    session: AsyncSession, poll_id: int, user_tg_id: int, option_ids: list[int]
) -> None:
    """Upsert one user's current choice. A retraction arrives as an empty
    ``option_ids`` → stored as ``"[]"`` so that user drops out of the payout.

    Portable select-then-insert/update (works on SQLite and PostgreSQL), the same
    pattern the user upsert uses; concurrent double-votes from one user are not a
    real scenario (a person taps one poll), so no lock is needed.
    """
    payload = json.dumps([int(o) for o in option_ids])
    existing = (
        await session.execute(
            select(PollVote).where(
                PollVote.poll_id == poll_id, PollVote.user_tg_id == user_tg_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            PollVote(poll_id=poll_id, user_tg_id=user_tg_id, option_ids_json=payload)
        )
        # Autoflush is off: flush so a second vote in the same transaction (or a
        # concurrent one) sees this row and updates it instead of inserting a
        # duplicate that would trip the (poll_id, user_tg_id) unique constraint.
        await session.flush()
    else:
        existing.option_ids_json = payload
        existing.voted_at = _now()


async def voter_count(session: AsyncSession, poll_id: int) -> int:
    """How many distinct users currently have a non-empty vote on the poll."""
    rows = (
        await session.execute(
            select(PollVote.option_ids_json).where(PollVote.poll_id == poll_id)
        )
    ).scalars().all()
    return sum(1 for j in rows if _nonempty(j))


def _nonempty(option_ids_json: str) -> bool:
    try:
        return bool(json.loads(option_ids_json))
    except (ValueError, TypeError):
        return False


def _active_voter_ids(rows: Iterable[tuple[int, str]]) -> tuple[int, ...]:
    """Project active vote rows to one deterministic payout candidate per user."""
    return tuple(sorted({uid for uid, opts_json in rows if _nonempty(opts_json)}))


async def pay_voters(session: AsyncSession, poll: PollTemplate) -> list[int]:
    """Pay the participation prize to every current voter. Returns the tg ids that
    were actually paid (so the caller can notify each one). No-commit (STEERING §5).

    A voter with a retracted (empty) choice is not a participant. Each user's
    payment is isolated: a missing wallet on one row (should not happen — voters
    are upserted by the poll_answer path) is logged and skipped rather than
    aborting everyone else's prize. Amounts are minted (like ``quiz_reward``).
    """
    coins = poll.prize_coins
    xp = poll.prize_xp
    if coins <= 0 and xp <= 0:
        return []
    # The close claim normally already owns this root row. Re-lock it here so
    # direct callers follow the same root → Users → Wallets order before payout.
    await session.execute(
        select(PollTemplate.id).where(PollTemplate.id == poll.id).with_for_update()
    )
    rows = (
        await session.execute(
            select(PollVote.user_tg_id, PollVote.option_ids_json).where(
                PollVote.poll_id == poll.id
            )
        )
    ).all()
    participants = _active_voter_ids(
        (uid, opts_json) for uid, opts_json in rows
    )
    # Do not prevalidate: a missing wallet is deliberately handled per voter in
    # the loop below, so it cannot abort another voter's prize.
    await economy_service.lock_users_then_wallets(session, participants)
    paid: list[int] = []
    for uid in participants:
        try:
            if coins > 0:
                await economy_service.credit(
                    session, uid, coins, TransactionType.poll_reward,
                    f"Premio sondaggio: {poll.question[:64]}", reference_id=poll.id,
                )
            if xp > 0:
                await xp_service.grant_xp(
                    session, uid, xp, xp_service.XpSource.poll_vote, capped=False
                )
        except WalletNotFoundError:
            log.warning("Votante %s del sondaggio %s senza wallet, premio saltato.", uid, poll.id)
            continue
        paid.append(uid)
    return paid
