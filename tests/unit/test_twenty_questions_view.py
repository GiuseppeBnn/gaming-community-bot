"""Behavioral tests for the typed presentation of Alduino's secret game."""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser

import pytest

from services.ai_game_types import (
    FinishReason,
    GameView,
    PersonalQuota,
    QuestionStartResult,
    QuestionVerdict,
    RewardProjection,
    RewardSummary,
    TerminalResult,
    TerminalAllocation,
    TurnKind,
    TurnOutcome,
    TurnRejectReason,
    TurnResult,
    TurnView,
    TwentyQuestionsPolicy,
)
from utils.twenty_questions_view import (
    render_live_card,
    render_personal_status,
    render_personal_turn,
    render_policy,
    render_public_help,
    render_question_start,
    render_terminal_card,
)


POLICY = TwentyQuestionsPolicy(
    version=2,
    questions_per_user=7,
    guesses_per_user=3,
    max_coins_per_participant=123,
    minimum_bps=2_500,
    question_penalty_bps=475,
    wrong_guess_penalty_bps=1_750,
    xp_per_participant=13,
)
PROJECTION = RewardProjection(
    participant_count=2,
    question_count=4,
    wrong_guess_count=1,
    base_amount=246,
    penalty_amount=29,
    computed_pool=217,
    share=108,
    remainder=1,
)
NOW = datetime(2026, 8, 24, 12, 0)


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _telegram_visible_utf16_units(text: str) -> int:
    parser = _VisibleHTML()
    parser.feed(text)
    parser.close()
    return len("".join(parser.parts).encode("utf-16-le")) // 2


def _view(
    *,
    status: str = "running",
    recent: tuple[TurnView, ...] = (),
    title: str = "Serata <epica>",
) -> GameView:
    return GameView(
        session_id=77,
        title=title,
        status=status,
        group_id=-1001,
        anchor_message_id=55,
        expires_at=datetime(2026, 8, 24, 14, 30),
        finish_reason=None,
        policy=POLICY,
        projection=PROJECTION,
        participant_count=2,
        question_count=4,
        wrong_guess_count=1,
        recent_turns=recent,
        revealed_answer="Segreto <mai live>",
        winner_tg_id=None,
    )


def _quota(*, participant: bool = True) -> PersonalQuota:
    return PersonalQuota(
        questions_used=2,
        questions_left=5,
        guesses_used=1,
        guesses_left=2,
        participant=participant,
    )


def _terminal(
    reason: FinishReason,
    *,
    settlement_status: str = "settled",
    winner_tg_id: int | None = 9,
) -> TerminalResult:
    allocations = (
        TerminalAllocation(user_tg_id=winner_tg_id or 8, coins=108, xp=13),
    ) if settlement_status == "settled" else ()
    return TerminalResult(
        session_id=77,
        transitioned=True,
        finish_reason=reason,
        group_id=-1001,
        anchor_message_id=55,
        title="Serata <epica>",
        answer="Portal <2>",
        winner_tg_id=winner_tg_id,
        reward=RewardSummary(
            settlement_status=settlement_status,  # type: ignore[arg-type]
            participant_count=2 if settlement_status == "settled" else 0,
            question_count=4,
            wrong_guess_count=1,
            base_amount=246,
            penalty_amount=29,
            computed_pool=937,
            paid_pool=216,
            share=108,
            remainder=1,
        ),
        allocations=allocations,
    )


def test_policy_and_public_help_derive_every_visible_rule_from_policy():
    policy = render_policy(POLICY)
    help_text = render_public_help(POLICY)

    for text in (policy, help_text):
        assert "7 domande" in text
        assert "3 tentativi" in text
        assert "123 CoInn" in text
        assert "13 XP" in text
        assert "4,75%" in text
        assert "17,5%" in text
    assert "RISPOSTA: titolo del gioco" in help_text


def test_live_card_escapes_data_hides_secret_and_keeps_only_last_six_turns():
    turns = tuple(
        TurnView(
            turn_no=index,
            user_tg_id=index,
            kind=TurnKind.question,
            input_text="vecchia <domanda>" if index == 1 else f"turno-{index} <x>",
            verdict=QuestionVerdict.si,
            correct=None,
        )
        for index in range(1, 8)
    )

    card = render_live_card(_view(recent=turns), now=NOW, open_preview=True)

    assert "Il gioco segreto di Alduino" in card
    assert "Serata &lt;epica&gt;" in card
    assert "24/08/2026 14:30 UTC" in card
    assert "2h 30min" in card
    assert "7 domande · 3 tentativi" in card
    assert "Partecipanti: <b>2</b> · Domande: <b>4</b> · Tentativi errati: <b>1</b>" in card
    assert "Massimo: <b>123 CoInn</b> per partecipante" in card
    assert "minimo attuale: <b>31 CoInn</b> per partecipante" in card
    assert "Quota stimata: <b>108 CoInn</b>" in card
    assert "proiezione" in card.casefold()
    assert "non è un saldo maturato" in card
    assert "13 XP" in card
    assert "vecchia &lt;domanda&gt;" not in card
    assert "turno-2 &lt;x&gt;" in card
    assert "turno-7 &lt;x&gt;" in card
    assert "Segreto" not in card
    assert "RISPOSTA: titolo del gioco" in card


def test_live_card_bounds_visible_utf16_content_after_html_entity_parsing():
    turns = tuple(
        TurnView(
            turn_no=index,
            user_tg_id=index,
            kind=TurnKind.question,
            input_text="<😀" * 500,
            verdict=QuestionVerdict.si,
            correct=None,
        )
        for index in range(1, 7)
    )

    card = render_live_card(_view(title="😀" * 120, recent=turns), now=NOW)

    assert _telegram_visible_utf16_units(card) <= 4096
    assert "&lt;" in card
    assert "…" in card


def test_personal_status_reuses_typed_card_and_escapes_user_visible_turns():
    view = _view(recent=(TurnView(
        turn_no=1,
        user_tg_id=3,
        kind=TurnKind.guess,
        input_text="<titolo>",
        verdict=None,
        correct=False,
    ),))

    status = render_personal_status(view, _quota(), now=NOW)

    assert "&lt;titolo&gt;" in status
    assert "Per te" in status
    assert "5 domande" in status
    assert "2 tentativi" in status


def test_terminal_victory_renders_formula_remainder_and_already_escaped_winner_once():
    winner_html = '<a href="tg://user?id=9">Aldo &amp; Lea</a>'

    card = render_terminal_card(_terminal(FinishReason.victory), winner_html=winner_html)

    assert "Portal &lt;2&gt;" in card
    assert winner_html in card
    assert "&amp;amp;" not in card
    assert "Base 246" in card
    assert "penalità 29" in card
    assert "quota 108" in card
    assert "resto non distribuito: 1 CoInn" in card
    assert "CoInn pagati: <b>216</b>" in card
    assert "XP partecipazione: <b>13</b>" in card


@pytest.mark.parametrize(
    ("reason", "settlement_status", "expected"),
    (
        (FinishReason.expired, "settled", "Tempo scaduto"),
        (FinishReason.admin_closed, "settled", "chiusa dallo staff"),
        (FinishReason.expired, "void", "Nessun partecipante valido"),
    ),
)
def test_non_victory_terminal_never_advertises_the_computed_pool_as_prize(
    reason: FinishReason,
    settlement_status: str,
    expected: str,
):
    card = render_terminal_card(
        _terminal(reason, settlement_status=settlement_status, winner_tg_id=None),
    )

    assert expected in card
    assert "CoInn: 0 — gioco non indovinato" in card
    assert "937" not in card
    assert "CoInn pagati" not in card


@pytest.mark.parametrize(
    ("result", "expected"),
    (
        (
            QuestionStartResult(
                77,
                TurnOutcome.reused,
                None,
                _quota(),
                cached_verdict=QuestionVerdict.no,
            ),
            "già fatta",
        ),
        (
            QuestionStartResult(
                77,
                TurnOutcome.rejected,
                TurnRejectReason.busy,
                _quota(),
            ),
            "un'altra domanda",
        ),
        (
            QuestionStartResult(
                77,
                TurnOutcome.rejected,
                TurnRejectReason.question_quota,
                _quota(),
            ),
            "domande valide",
        ),
    ),
)
def test_question_start_feedback_covers_cached_busy_and_personal_quota(
    result: QuestionStartResult,
    expected: str,
):
    text = render_question_start(result)

    assert expected in text
    assert "5 domande" in text
    assert "2 tentativi" in text


@pytest.mark.parametrize(
    ("result", "expected"),
    (
        (
            TurnResult(77, TurnOutcome.recorded, None, _quota(), verdict=QuestionVerdict.forse),
            "FORSE",
        ),
        (
            TurnResult(77, TurnOutcome.recorded, None, _quota(), correct=True),
            "indovinato",
        ),
        (
            TurnResult(77, TurnOutcome.rejected, TurnRejectReason.guess_quota, _quota()),
            "tentativi validi",
        ),
        (
            TurnResult(77, TurnOutcome.rejected, TurnRejectReason.duplicate_guess, _quota()),
            "già tentato",
        ),
        (
            TurnResult(77, TurnOutcome.rejected, TurnRejectReason.providers_unavailable, _quota()),
            "non è stata consumata",
        ),
        (
            TurnResult(77, TurnOutcome.rejected, TurnRejectReason.hash_collision, _quota()),
            "incoerente",
        ),
        (
            TurnResult(77, TurnOutcome.rejected, TurnRejectReason.lost_claim, _quota()),
            "riprova",
        ),
        (
            TurnResult(
                77,
                TurnOutcome.rejected,
                TurnRejectReason.answer_confirmation_required,
                _quota(),
            ),
            "RISPOSTA:",
        ),
        (
            TurnResult(77, TurnOutcome.rejected, TurnRejectReason.invalid_input, _quota()),
            "1 e 500",
        ),
    ),
)
def test_personal_turn_feedback_covers_typed_outcomes_without_raw_input(
    result: TurnResult,
    expected: str,
):
    text = render_personal_turn(result)

    assert expected in text
    assert "5 domande" in text
    assert "2 tentativi" in text
