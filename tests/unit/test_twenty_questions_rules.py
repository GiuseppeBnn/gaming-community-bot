from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from services.ai_game_types import (
    DEFAULT_DURATION_SECONDS,
    DEFAULT_MAX_COINS_PER_PARTICIPANT,
    DURATION_PRESETS_SECONDS,
    FinishReason,
    QuestionVerdict,
    RewardProjection,
    TurnKind,
    TurnOutcome,
    TurnRejectReason,
)
from services.twenty_questions_rules import (
    MAX_BIGINT,
    compute_reward_projection,
    looks_like_direct_guess,
    normalize_turn_input,
    normalized_input_hash,
    v2_policy,
)


@pytest.mark.parametrize(
    ("participants", "questions", "wrong", "pool", "share", "remainder"),
    [
        (5, 5, 0, 470, 94, 0),
        (10, 20, 10, 680, 68, 0),
        (50, 250, 99, 1_520, 30, 20),
    ],
)
def test_default_reward_examples(participants, questions, wrong, pool, share, remainder):
    got = compute_reward_projection(
        v2_policy(100),
        participants=participants,
        questions=questions,
        wrong_guesses=wrong,
    )
    assert (got.computed_pool, got.share, got.remainder) == (pool, share, remainder)


def test_zero_participants_is_void_math():
    got = compute_reward_projection(v2_policy(100), participants=0, questions=0, wrong_guesses=0)
    assert got == RewardProjection(0, 0, 0, 0, 0, 0, 0, 0)


def test_normalization_is_conservative_and_hash_is_fixed_length():
    assert normalize_turn_input("  «PORTAL ２?!»  ") == "portal 2"
    assert normalize_turn_input("Spider-Man") != normalize_turn_input("Spider Man")
    assert normalize_turn_input("C++") != normalize_turn_input("C")
    assert len(normalized_input_hash("Portal 2?")) == 64


def test_v2_policy_centralizes_the_game_limits_and_reward_rates():
    policy = v2_policy(73)
    assert (
        policy.version,
        policy.questions_per_user,
        policy.guesses_per_user,
        policy.max_coins_per_participant,
        policy.minimum_bps,
        policy.question_penalty_bps,
        policy.wrong_guess_penalty_bps,
        policy.xp_per_participant,
    ) == (
        2,
        5,
        2,
        73,
        3_000,
        600,
        2_000,
        10,
    )
    assert (
        DEFAULT_DURATION_SECONDS,
        DURATION_PRESETS_SECONDS,
        DEFAULT_MAX_COINS_PER_PARTICIPANT,
    ) == (43_200, (7_200, 21_600, 43_200, 86_400), 100)


def test_game_enums_retain_the_historical_legacy_finish_reason():
    assert FinishReason("legacy") is FinishReason.legacy
    assert [member.value for member in TurnKind] == ["question", "guess"]
    assert [member.value for member in TurnOutcome] == [
        "claimed",
        "reused",
        "recorded",
        "rejected",
    ]
    assert [member.value for member in QuestionVerdict] == ["si", "no", "forse", "usa_risposta"]
    assert [member.value for member in TurnRejectReason] == [
        "busy",
        "closed",
        "expired",
        "question_quota",
        "guess_quota",
        "duplicate_guess",
        "lost_claim",
        "invalid_input",
        "providers_unavailable",
        "answer_confirmation_required",
        "hash_collision",
    ]


def test_policy_dto_is_immutable_and_slot_backed():
    policy = v2_policy(100)
    assert not hasattr(policy, "__dict__")
    with pytest.raises(FrozenInstanceError):
        policy.version = 3  # type: ignore[misc]


@pytest.mark.parametrize(
    ("participants", "expected_minimum"),
    [(1, 1), (2, 1), (3, 1), (4, 2), (7, 3)],
)
def test_minimum_pool_uses_ceiling_basis_point_math(participants, expected_minimum):
    got = compute_reward_projection(
        v2_policy(1), participants=participants, questions=10_000, wrong_guesses=0
    )
    assert got.computed_pool == expected_minimum


@pytest.mark.parametrize(
    ("coins", "questions", "wrong_guesses", "penalty"),
    [(7, 1, 0, 0), (7, 0, 1, 1), (37, 3, 2, 21)],
)
def test_penalty_uses_floor_basis_point_math(coins, questions, wrong_guesses, penalty):
    got = compute_reward_projection(
        v2_policy(coins),
        participants=100,
        questions=questions,
        wrong_guesses=wrong_guesses,
    )
    assert got.penalty_amount == penalty
    assert got.computed_pool == coins * 100 - penalty


def test_custom_maximum_coins_scales_the_entire_projection():
    got = compute_reward_projection(v2_policy(37), participants=3, questions=1, wrong_guesses=0)
    assert got == RewardProjection(3, 1, 0, 111, 2, 109, 36, 1)


def test_remainder_is_not_distributed_to_any_participant():
    got = compute_reward_projection(v2_policy(100), participants=4, questions=1, wrong_guesses=0)
    assert (got.computed_pool, got.share, got.remainder) == (394, 98, 2)


@pytest.mark.parametrize(
    ("participants", "questions", "wrong_guesses"),
    [(-1, 0, 0), (0, -1, 0), (0, 0, -1)],
)
def test_negative_reward_counts_are_rejected(participants, questions, wrong_guesses):
    with pytest.raises(ValueError, match="reward counts must be non-negative"):
        compute_reward_projection(
            v2_policy(1),
            participants=participants,
            questions=questions,
            wrong_guesses=wrong_guesses,
        )


def test_reward_math_rejects_bigint_overflow_before_multiplication():
    with pytest.raises(ValueError, match="reward arithmetic outside BIGINT"):
        compute_reward_projection(
            v2_policy(1), participants=MAX_BIGINT + 1, questions=0, wrong_guesses=0
        )
    with pytest.raises(ValueError, match="reward arithmetic outside BIGINT"):
        compute_reward_projection(
            v2_policy(1),
            participants=1,
            questions=MAX_BIGINT // 600 + 1,
            wrong_guesses=0,
        )


def test_reward_math_accepts_the_exact_safe_bigint_boundary():
    participants = MAX_BIGINT // 3_000
    got = compute_reward_projection(
        v2_policy(1), participants=participants, questions=0, wrong_guesses=0
    )
    assert got.base_amount == participants
    assert got.computed_pool == participants


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  «PORTAL ２?!»  ", "portal 2"),
        ("\u201cPortal\u20132\u201d?!", "portal-2"),
        ("  Portal   2  ?!\u2026  ", "portal 2"),
        ("Spider - Man", "spider-man"),
    ],
)
def test_normalization_folds_unicode_and_formatting_without_semantic_matching(raw, expected):
    assert normalize_turn_input(raw) == expected


def test_normalization_preserves_meaningful_internal_punctuation():
    assert normalize_turn_input("C++") != normalize_turn_input("C")
    assert normalize_turn_input("Spider-Man") != normalize_turn_input("Spider Man")
    assert normalize_turn_input("Portal 2") != normalize_turn_input("Half-Life 2")


def test_normalized_hash_is_sha256_of_the_conservative_normalized_text():
    digest = normalized_input_hash("  PORTAL ２?! ")
    assert digest == normalized_input_hash("portal 2")
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)


@pytest.mark.parametrize(
    "text",
    ["Il gioco è Portal 2?", 'La risposta è "Portal 2"?', 'È "Portal 2"?'],
)
def test_direct_guess_guard_catches_unambiguous_disguised_answers(text):
    assert looks_like_direct_guess(text)


@pytest.mark.parametrize("text", ["È un RPG?", "Ha dei portali?", "È Portal 2?"])
def test_direct_guess_guard_leaves_property_and_ambiguous_questions_to_the_classifier(text):
    assert not looks_like_direct_guess(text)
