from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TransactionType(str, Enum):
    deposit = "deposit"
    withdrawal = "withdrawal"
    transfer_out = "transfer_out"
    transfer_in = "transfer_in"
    bet_placed = "bet_placed"
    bet_won = "bet_won"
    bet_refund = "bet_refund"
    admin_credit = "admin_credit"
    admin_debit = "admin_debit"
    daily_reward = "daily_reward"
    shop_purchase = "shop_purchase"
    quiz_reward = "quiz_reward"


class EventStatus(str, Enum):
    draft = "draft"        # pre-created by an admin, not yet announced/open
    open = "open"
    locked = "locked"
    resolved = "resolved"
    cancelled = "cancelled"


class BetStatus(str, Enum):
    pending = "pending"
    won = "won"
    lost = "lost"
    refunded = "refunded"


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    xp: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Bot-level ban (set by /ban, the warn auto-ban and the dashboard; cleared by
    # /sban). A banned user's updates are dropped silently by BannedUserMiddleware —
    # the bot never replies, anywhere — but their data (wallet, trophies, …) stays.
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_daily_claim: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    daily_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bets_won: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transfers_made: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Progression / cosmetics (XP is a merit metric, kept separate from coins).
    # cosmetic_tag: a flair bought from the curated shop (display-only, no real perms).
    # rank_slug: last XP-rank seen, used to detect & announce rank-ups.
    # xp_today / xp_today_date: server-side daily cap on farmable participation XP.
    cosmetic_tag: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Multiple simultaneously-active shop tags (JSON list of catalog item keys,
    # ordered). cosmetic_tag is kept in sync as a legacy single-tag fallback.
    active_tags_json: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    rank_slug: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    xp_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    xp_today_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    wallet: Mapped[Optional["Wallet"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    badges: Mapped[list["UserBadge"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    bets: Mapped[list["UserBet"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), unique=True, nullable=False
    )
    coins: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    user: Mapped["User"] = relationship(back_populates="wallet")


class LedgerEntry(Base):
    __tablename__ = "ledger"

    # `ledger` is the fastest-growing table and had no index beyond the PK, so
    # every /storico was a full scan + sort. get_history filters
    # `from_tg_id = X OR to_tg_id = X` then orders by created_at desc, so the
    # useful shape is TWO composite indexes (one per side of the OR) rather than
    # three single-column ones: Postgres can BitmapOr the two, and each already
    # carries created_at for the sort.
    __table_args__ = (
        Index("ix_ledger_from_created", "from_tg_id", "created_at"),
        Index("ix_ledger_to_created", "to_tg_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    to_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tx_type: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    reference_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("betting_events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Badge(Base):
    __tablename__ = "badges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    icon_emoji: Mapped[str] = mapped_column(String(8), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    # PlayStation-style rarity tier: bronze | silver | gold | platinum.
    rarity: Mapped[str] = mapped_column(String(16), default="bronze", nullable=False)
    xp_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    condition_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    condition_value: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Scope for parametrized conditions: a consumable item key (item_purchases), a
    # category key (category_purchases), a game key (podium_count/first_place_count),
    # an event metric key (event_count), or a ``;``-separated list of prerequisite
    # trophy slugs (collection). NULL for the plain counter conditions (xp/balance/…).
    condition_param: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Hidden ("secret") trophy: masked in the catalog until the user earns it.
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user_badges: Mapped[list["UserBadge"]] = relationship(
        back_populates="badge", cascade="all, delete-orphan"
    )


class UserBadge(Base):
    __tablename__ = "user_badges"
    __table_args__ = (UniqueConstraint("user_tg_id", "badge_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_tg_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), nullable=False
    )
    badge_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("badges.id", ondelete="CASCADE"), nullable=False
    )
    earned_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped["User"] = relationship(back_populates="badges")
    badge: Mapped["Badge"] = relationship(back_populates="user_badges")


class BettingEvent(Base):
    __tablename__ = "betting_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), nullable=False)
    creator_tg_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    resolution_option_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Betting window: chosen at creation (NULL/0 = illimitata, only manual lock).
    # ``closes_at`` is armed at open-time (= utcnow() + window) and auto-locks the
    # event via a ScheduledTask; NULL while draft or if the window is unlimited.
    betting_window_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    closes_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    options: Mapped[list["BettingOption"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    user_bets: Mapped[list["UserBet"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class BettingOption(Base):
    __tablename__ = "betting_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("betting_events.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    odds_multiplier: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    total_wagered: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    event: Mapped["BettingEvent"] = relationship(back_populates="options")
    bets: Mapped[list["UserBet"]] = relationship(back_populates="option")


class UserBet(Base):
    __tablename__ = "user_bets"
    __table_args__ = (UniqueConstraint("user_tg_id", "event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_tg_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("betting_events.id", ondelete="CASCADE"), nullable=False
    )
    option_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("betting_options.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    potential_win: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="bets")
    event: Mapped["BettingEvent"] = relationship(back_populates="user_bets")
    option: Mapped["BettingOption"] = relationship(back_populates="bets")


class ShopPurchase(Base):
    __tablename__ = "shop_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    cost: Mapped[int] = mapped_column(Integer, nullable=False)
    purchased_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)


class GamePodium(Base):
    """A podium finish (rank 1–3) by a user in a community game.

    ``game_key`` identifies the game: ``trivia`` (the quiz, live today) and the
    forward-declared ``guess`` / ``sound`` (built later). Trophy conditions
    ``podium_count`` / ``first_place_count`` count rows here — recorded once per
    user per game event (e.g. one quiz run) by ``progress_service.record_podium``."""

    __tablename__ = "game_podiums"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    game_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)  # 1=first, 2=second, 3=third
    ref_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # e.g. quiz_id
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserProgressEvent(Base):
    """A generic per-user "did action X in event Y" progress row.

    The trophy engine's ``event_count`` condition counts rows by ``metric_key``
    (e.g. ``trivia_last_place`` / ``trivia_sub30``), exactly like ``game_podiums``
    counts podium finishes. Recorded once per source event via
    ``progress_service.record_event``; the ``(user, metric, ref)`` unique key makes
    re-processing the same event idempotent. Adding a new "do X N times" trophy
    needs only a ``record_event`` call at the action site + a CSV row — no schema
    change."""

    __tablename__ = "user_progress_events"
    __table_args__ = (UniqueConstraint("user_tg_id", "metric_key", "ref_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    metric_key: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    ref_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # e.g. quiz_id
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Warning(Base):
    """Persistent moderation warning (strike) issued by an admin."""

    __tablename__ = "warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    issued_by_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AdminAction(Base):
    """Audit log of admin actions (currency + moderation)."""

    __tablename__ = "admin_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Quiz(Base):
    """A multi-question quiz (QuizBot-style), run as native quiz polls."""

    __tablename__ = "quizzes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    creator_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Legacy single pool (split 50/30/20 among the top 3). Kept for back-compat:
    # when the explicit per-rank prizes below are all 0, this pool is used instead.
    prize_coins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Explicit per-rank prizes. The podium gets first/second/third; everyone who
    # *finishes* below the podium gets a consolation that decreases linearly from
    # `prize_consolation` (4th place) down to `prize_min` (guaranteed floor, last place).
    prize_first: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prize_second: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prize_third: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prize_consolation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prize_min: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Per-user display randomization, chosen once at creation (§19).
    randomize_questions: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    randomize_answers: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    questions: Mapped[list["QuizQuestion"]] = relationship(
        back_populates="quiz", cascade="all, delete-orphan", order_by="QuizQuestion.position"
    )


class QuizQuestion(Base):
    __tablename__ = "quiz_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quiz_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String(300), nullable=False)
    options_json: Mapped[str] = mapped_column(String(2048), nullable=False)  # JSON list[str]
    correct_option_id: Mapped[int] = mapped_column(Integer, nullable=False)
    explanation: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    open_period: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    tg_poll_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    quiz: Mapped["Quiz"] = relationship(back_populates="questions")


class QuizAnswer(Base):
    __tablename__ = "quiz_answers"
    __table_args__ = (UniqueConstraint("question_id", "user_tg_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quiz_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    question_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("quiz_questions.id", ondelete="CASCADE"), nullable=False
    )
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    selected_option_id: Mapped[int] = mapped_column(Integer, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    response_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PollTemplate(Base):
    """A pre-created poll (question + options) that an admin can later start in
    the group immediately or schedule — mirroring how quizzes are pre-created.

    Status: ``ready`` (usable) → ``used`` (already sent). One-shot, like a quiz run.
    """

    __tablename__ = "poll_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(String(300), nullable=False)
    options_json: Mapped[str] = mapped_column(String(2048), nullable=False)  # JSON list[str]
    creator_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ready", nullable=False)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class BotState(Base):
    """Small key-value store for runtime state that must survive restarts
    (e.g. the effective group id after a Telegram chat migration)."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), nullable=False)


class GuessRound(Base):
    """One "guess the subject from a medium" round.

    ``kind`` discriminates the two games — ``guess`` (from an image) and ``sound``
    (from an audio clip) — and is **also** the ``game_key`` passed to
    ``progress_service.record_podium``, so the trophies ``GAME_LABELS`` already
    forward-declares light up with no extra wiring.

    One model serves both because they differ only in which medium is stored and
    which Bot API method resends it. Duplicating the round would mean duplicating
    a path that pays coins.

    Status: ``draft`` (being built) → ``ready`` → ``running`` → ``finished``.
    """

    __tablename__ = "guess_rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    creator_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Telegram file_id, resent at play time. Never downloaded: the bot keeps no
    # media on disk. Validated at creation by sending it straight back to the
    # admin, which is the only moment a dead file_id can still be fixed.
    media_file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    media_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # photo|audio|voice

    # The canonical answer, admin-authored (trusted input). ``aliases_json`` holds
    # extra spellings the admin wants accepted without asking the model at all.
    answer: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases_json: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    # JSON list of {"after": int, "text": str} — a hint delivered once the player
    # has used ``after`` attempts. JSON and not a child table for the same reason
    # as QuizQuestion.options_json: small, always read together, never queried alone.
    hints_json: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)

    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # Per-PLAYER limit, counted from when they open the game. 0 = no limit.
    time_limit_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    prize_first: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prize_second: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prize_third: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prize_consolation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prize_min: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class GuessSession(Base):
    """One player's run at one round: the clock, the counters, the outcome.

    It is a table and not in-memory state because the clock starts when the
    player *opens* the game — before the first attempt — and has to survive a
    restart. The unique key is the anti-cheat: a second session would be a fresh
    clock and a second set of attempts for the same player.

    ``solved_attempts`` and ``solve_ms`` are written **once**, by the conditional
    UPDATE that claims the solve (``WHERE solved_at IS NULL``), so a double tap
    cannot re-rank or re-pay anyone.
    """

    __tablename__ = "guess_sessions"
    __table_args__ = (UniqueConstraint("round_id", "user_tg_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("guess_rounds.id", ondelete="CASCADE"), nullable=False
    )
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    solved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    solved_attempts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    solve_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    attempts_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Submissions the judge could not verify (AI unreachable). They are recorded
    # but refunded as bonus attempts, capped — see guess_service.attempts_left.
    unverified_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class GuessAttempt(Base):
    """One submitted answer, kept forever.

    Three jobs at once: it bounds brute force (the row exists even when the
    attempt is refunded), it is the admin's audit trail of what got rejected, and
    ``(round_id, normalized)`` is the **verdict cache** — two players who type the
    same thing must get the same answer, and the second one costs no API call.
    """

    __tablename__ = "guess_attempts"
    __table_args__ = (
        UniqueConstraint("round_id", "user_tg_id", "attempt_no"),
        Index("ix_guess_attempts_cache", "round_id", "normalized"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("guess_rounds.id", ondelete="CASCADE"), nullable=False
    )
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based, per user
    raw_answer: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized: Mapped[str] = mapped_column(String(200), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)  # correct|wrong|unverified
    # exact|alias|shape|ai|cache|unavailable — how the verdict was reached.
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ScheduledTask(Base):
    """A future action (open a bet, start a quiz, send a poll) run by the in-process scheduler."""

    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(16), nullable=False)  # quiz | poll | bet
    ref_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # e.g. quiz_id
    payload_json: Mapped[Optional[str]] = mapped_column(String(4096), nullable=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_by_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
