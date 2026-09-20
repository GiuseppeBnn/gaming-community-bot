"""Pure policy, reward, and input rules for the Alduino secret game."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from services.ai_game_types import RewardProjection, TwentyQuestionsPolicy


MAX_BIGINT = 2**63 - 1

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCTUATION_SPACING_RE = re.compile(r"\s*([,;:!?\u2026.\-])\s*")
_TERMINAL_PUNCTUATION = "?!.\u2026"
_QUOTE_PAIRS = {'"': '"', "'": "'"}
_CHARACTER_TRANSLATION = str.maketrans(
    {
        "\u00ab": '"',
        "\u00bb": '"',
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)
_DIRECT_GAME_PREFIX_RE = re.compile(r"^il gioco\s+(?:è|e')\s+(.+)$")
_QUOTED_ANSWER_RE = re.compile(r"^(?:la risposta\s+)?(?:è|e')\s+([\"'])(.+)\1[?!.\u2026]*$")


def v2_policy(max_coins_per_participant: int) -> TwentyQuestionsPolicy:
    """Return the immutable version-two rules for a chosen CoInn maximum."""
    return TwentyQuestionsPolicy(
        version=2,
        questions_per_user=5,
        guesses_per_user=2,
        max_coins_per_participant=max_coins_per_participant,
        minimum_bps=3_000,
        question_penalty_bps=600,
        wrong_guess_penalty_bps=2_000,
        xp_per_participant=10,
    )


def _checked_mul(left: int, right: int) -> int:
    if left < 0 or right < 0 or (left and right > MAX_BIGINT // left):
        raise ValueError("reward arithmetic outside BIGINT")
    return left * right


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def compute_reward_projection(
    policy: TwentyQuestionsPolicy,
    *,
    participants: int,
    questions: int,
    wrong_guesses: int,
) -> RewardProjection:
    """Project the equal-share reward pool using checked integer arithmetic."""
    if min(participants, questions, wrong_guesses) < 0:
        raise ValueError("reward counts must be non-negative")
    if participants == 0:
        return RewardProjection(0, 0, 0, 0, 0, 0, 0, 0)
    base = _checked_mul(participants, policy.max_coins_per_participant)
    weighted = _checked_mul(policy.question_penalty_bps, questions)
    wrong_weighted = _checked_mul(policy.wrong_guess_penalty_bps, wrong_guesses)
    if weighted > MAX_BIGINT - wrong_weighted:
        raise ValueError("reward arithmetic outside BIGINT")
    minimum = _ceil_div(_checked_mul(base, policy.minimum_bps), 10_000)
    penalty = _checked_mul(policy.max_coins_per_participant, weighted + wrong_weighted) // 10_000
    pool = max(minimum, base - penalty)
    share, remainder = divmod(pool, participants)
    return RewardProjection(
        participants,
        questions,
        wrong_guesses,
        base,
        penalty,
        pool,
        share,
        remainder,
    )


def _format_input(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_CHARACTER_TRANSLATION)
    folded = _WHITESPACE_RE.sub(" ", folded).strip()
    return _PUNCTUATION_SPACING_RE.sub(r"\1", folded)


def normalize_turn_input(text: str) -> str:
    """Normalize formatting while preserving meaningful title punctuation and words."""
    normalized = _format_input(text)
    while True:
        previous = normalized
        normalized = normalized.rstrip().rstrip(_TERMINAL_PUNCTUATION).rstrip()
        if len(normalized) >= 2 and _QUOTE_PAIRS.get(normalized[0]) == normalized[-1]:
            normalized = normalized[1:-1].strip()
        if normalized == previous:
            return normalized


def normalized_input_hash(text: str) -> str:
    """Return the SHA-256 digest of the conservative normalized input."""
    return hashlib.sha256(normalize_turn_input(text).encode("utf-8")).hexdigest()


def looks_like_direct_guess(text: str) -> bool:
    """Catch only syntactically explicit attempts disguised as questions."""
    formatted = _format_input(text)
    if _QUOTED_ANSWER_RE.fullmatch(formatted):
        return True
    match = _DIRECT_GAME_PREFIX_RE.fullmatch(formatted.rstrip(_TERMINAL_PUNCTUATION))
    if match is None:
        return False
    return not match.group(1).startswith(("un ", "una ", "uno ", "un'"))
