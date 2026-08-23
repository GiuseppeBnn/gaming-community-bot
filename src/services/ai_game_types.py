"""Pure value types shared by the Alduino secret-game services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, TypeAlias


DEFAULT_DURATION_SECONDS = 43_200
DURATION_PRESETS_SECONDS = (7_200, 21_600, 43_200, 86_400)
DEFAULT_MAX_COINS_PER_PARTICIPANT = 100


class FinishReason(str, Enum):
    victory = "victory"
    expired = "expired"
    admin_closed = "admin_closed"
    legacy = "legacy"


SettlementFinishReason: TypeAlias = Literal[
    FinishReason.victory,
    FinishReason.expired,
    FinishReason.admin_closed,
]


class TurnKind(str, Enum):
    question = "question"
    guess = "guess"


class TurnOutcome(str, Enum):
    claimed = "claimed"
    reused = "reused"
    recorded = "recorded"
    rejected = "rejected"


class QuestionVerdict(str, Enum):
    si = "si"
    no = "no"
    forse = "forse"
    usa_risposta = "usa_risposta"


class TurnRejectReason(str, Enum):
    busy = "busy"
    closed = "closed"
    expired = "expired"
    question_quota = "question_quota"
    guess_quota = "guess_quota"
    duplicate_guess = "duplicate_guess"
    lost_claim = "lost_claim"
    invalid_input = "invalid_input"
    providers_unavailable = "providers_unavailable"
    answer_confirmation_required = "answer_confirmation_required"
    hash_collision = "hash_collision"


class StartRejectReason(str, Enum):
    not_ready = "not_ready"
    absolute_expiry_elapsed = "absolute_expiry_elapsed"
    providers_unavailable = "providers_unavailable"


class GameCreationError(RuntimeError):
    def __init__(self, reason: Literal["feature_disabled", "invalid_policy"]):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class TwentyQuestionsPolicy:
    version: int
    questions_per_user: int
    guesses_per_user: int
    max_coins_per_participant: int
    minimum_bps: int
    question_penalty_bps: int
    wrong_guess_penalty_bps: int
    xp_per_participant: int


@dataclass(frozen=True, slots=True)
class RewardProjection:
    participant_count: int
    question_count: int
    wrong_guess_count: int
    base_amount: int
    penalty_amount: int
    computed_pool: int
    share: int
    remainder: int


@dataclass(frozen=True, slots=True)
class CreatedGame:
    session_id: int
    title: str


@dataclass(frozen=True, slots=True)
class StartGameResult:
    started: bool
    reason: StartRejectReason | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class TurnView:
    turn_no: int
    user_tg_id: int
    kind: TurnKind
    input_text: str
    verdict: QuestionVerdict | None
    correct: bool | None


@dataclass(frozen=True, slots=True)
class GameView:
    session_id: int
    title: str
    status: str
    group_id: int | None
    anchor_message_id: int | None
    expires_at: datetime | None
    finish_reason: FinishReason | None
    policy: TwentyQuestionsPolicy
    projection: RewardProjection
    participant_count: int
    question_count: int
    wrong_guess_count: int
    recent_turns: tuple[TurnView, ...]
    revealed_answer: str | None
    winner_tg_id: int | None


@dataclass(frozen=True, slots=True)
class TerminalAllocation:
    user_tg_id: int
    coins: int
    xp: int


@dataclass(frozen=True, slots=True)
class RewardSummary:
    settlement_status: Literal["settled", "void"]
    participant_count: int
    question_count: int
    wrong_guess_count: int
    base_amount: int
    penalty_amount: int
    computed_pool: int
    paid_pool: int
    share: int
    remainder: int


@dataclass(frozen=True, slots=True)
class TerminalResult:
    session_id: int
    transitioned: bool
    finish_reason: FinishReason
    group_id: int | None
    anchor_message_id: int | None
    title: str
    answer: str
    winner_tg_id: int | None
    reward: RewardSummary
    allocations: tuple[TerminalAllocation, ...]


@dataclass(frozen=True, slots=True)
class PersonalQuota:
    questions_used: int
    questions_left: int
    guesses_used: int
    guesses_left: int
    participant: bool


@dataclass(frozen=True, slots=True)
class QuestionContextTurn:
    turn_no: int
    normalized_hash: str | None
    question: str
    verdict: QuestionVerdict


@dataclass(frozen=True, slots=True)
class QuestionClaim:
    session_id: int
    token: str
    user_tg_id: int
    input_text: str
    normalized_text: str
    normalized_hash: str
    dossier_json: str
    context: tuple[QuestionContextTurn, ...]


@dataclass(frozen=True, slots=True)
class QuestionStartResult:
    session_id: int
    outcome: TurnOutcome
    reason: TurnRejectReason | None
    quota: PersonalQuota
    claim: QuestionClaim | None = None
    cached_verdict: QuestionVerdict | None = None
    terminal: TerminalResult | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    session_id: int
    outcome: TurnOutcome
    reason: TurnRejectReason | None
    quota: PersonalQuota
    verdict: QuestionVerdict | None = None
    correct: bool | None = None
    terminal: TerminalResult | None = None
