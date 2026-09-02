from types import SimpleNamespace

import pytest

from services import ai_game_service
from services.ai_game_service import GameSnapshot
from services.ai_game_types import QuestionVerdict, TurnKind
from database.models import AIGameSession, AIGameTurn, TwentyQuestionsGame
from handlers.twenty_questions import _guess, render_card


def _snapshot(status="running"):
    root = AIGameSession(id=3, game_type="twentyq", title="Serata <3", creator_tg_id=1,
                         status=status, next_turn_no=2)
    game = TwentyQuestionsGame(
        session_id=3, catalog_key="x", answer="Portal 2", aliases_json="[]",
        dossier_json="{}", question_limit=20, guess_limit=3,
        questions_used=1, guesses_used=0,
    )
    turn = AIGameTurn(
        session_id=3, turn_no=1, user_tg_id=4, kind="question",
        input_text="È <b>3D?", output_json='{"verdetto":"si","risposta":"Sì"}',
    )
    return GameSnapshot(root, game, (turn,))


def test_guess_syntax_is_explicit_and_case_insensitive():
    assert _guess(" RISPOSTA: Portal 2 ") == "Portal 2"
    assert _guess("è Portal 2?") is None
    assert _guess("RISPOSTA:") == ""


def test_live_card_escapes_content_and_never_reveals_secret():
    card = render_card(_snapshot())
    assert "Serata &lt;3" in card
    assert "È &lt;b&gt;3D?" in card
    assert "Portal 2" not in card
    assert "RISPOSTA:" in card


def test_finished_card_reveals_answer():
    card = render_card(_snapshot("finished"))
    assert "Portal 2" in card


async def test_game_service_defensive_pure_boundaries_reject_corrupt_state():
    """Malformed persisted turn/catalog state must degrade safely instead of inventing a result."""
    with pytest.raises(ValueError, match="catalog is empty"):
        await ai_game_service._balanced_target(None, ())

    legacy = ai_game_service._legacy_view_policy(None, None, None, None)
    assert (legacy.version, legacy.questions_per_user, legacy.guesses_per_user) == (1, 0, 0)

    turn = ai_game_service._turn_view(1, 3, "unknown", "Q", "not json")
    assert (turn.kind, turn.verdict, turn.correct) == (TurnKind.question, None, None)

    corrupt = AIGameTurn(output_json='{"verdetto":"broken"}')
    assert ai_game_service._cached_question_verdict(corrupt) is None


async def test_classification_overloads_reject_ambiguous_v2_and_legacy_inputs():
    """Accepting an ambiguous adapter call could send a v1 snapshot down the wrong provider route."""
    with pytest.raises(TypeError, match="legacy classification"):
        await ai_game_service.classify_question()

    claim = ai_game_service.QuestionClaim(1, "token", 2, "Q", "q", "hash", "{}", ())
    with pytest.raises(TypeError, match="v2 question classification"):
        await ai_game_service.classify_question(claim, "wrong")


async def test_v2_duplicate_and_failed_claim_results_do_not_consume_quota(monkeypatch):
    """A dedupe/hash race must return a rejection without creating a new turn."""
    claim = ai_game_service.QuestionClaim(1, "token", 2, "Q", "q", "hash", "{}", ())
    quota = SimpleNamespace(questions_left=4, guesses_left=2)

    async def personal(*_args):
        return quota

    duplicate = SimpleNamespace(output_json='{"verdetto":"si"}')
    monkeypatch.setattr(ai_game_service, "get_personal_quota", personal)
    async def find_duplicate(*_args):
        return duplicate

    monkeypatch.setattr(ai_game_service, "_find_normalized_turn", find_duplicate)
    monkeypatch.setattr(ai_game_service, "_same_normalized_input", lambda *_args, **_kwargs: True)
    reused = await ai_game_service._question_duplicate_result(None, claim)
    assert (reused.outcome, reused.verdict) == (ai_game_service.TurnOutcome.reused, QuestionVerdict.si)

    duplicate.output_json = "{}"
    collision = await ai_game_service._question_duplicate_result(None, claim)
    assert collision.reason is ai_game_service.TurnRejectReason.lost_claim

    monkeypatch.setattr(ai_game_service, "_same_normalized_input", lambda *_args, **_kwargs: False)
    rejected = await ai_game_service._question_duplicate_result(None, claim)
    assert rejected.reason is ai_game_service.TurnRejectReason.hash_collision


async def test_v2_guards_return_closed_and_lost_claim_without_database_mutation(monkeypatch):
    """Absent state and non-question completions are safe rejections, never partial writes."""
    quota = SimpleNamespace(questions_left=4, guesses_left=2)

    async def no_terminal(*_args):
        return None

    async def no_state(*_args):
        return None

    async def personal(*_args):
        return quota

    monkeypatch.setattr(ai_game_service, "_terminalize_if_due", no_terminal)
    monkeypatch.setattr(ai_game_service, "_v2_state", no_state)
    monkeypatch.setattr(ai_game_service, "get_personal_quota", personal)
    closed = await ai_game_service.submit_guess(None, session_id=1, user_tg_id=2, answer="Portal")
    assert closed.reason is ai_game_service.TurnRejectReason.closed

    guess_claim = ai_game_service.QuestionClaim(1, "token", 2, "Q", "q", "hash", "{}", (), kind=TurnKind.guess)
    lost = await ai_game_service.complete_question(None, claim=guess_claim, verdict=QuestionVerdict.si)
    assert lost.reason is ai_game_service.TurnRejectReason.lost_claim


async def test_legacy_classifier_skips_corrupt_and_non_question_history(monkeypatch):
    """Only valid historical question verdicts may influence a legacy provider request."""
    turns = (
        SimpleNamespace(kind="guess", output_json='{"verdetto":"si"}', turn_no=1, normalized_input_hash=None, input_text="G"),
        SimpleNamespace(kind="question", output_json="bad", turn_no=2, normalized_input_hash=None, input_text="Q"),
    )
    snapshot = SimpleNamespace(game=SimpleNamespace(dossier_json="{}"), turns=turns)
    captured = {}

    class Provider:
        async def generate_json(self, request):
            captured["request"] = request
            return SimpleNamespace(value={"verdetto": "no"})

    verdict = await ai_game_service.classify_question(snapshot, "Domanda?", Provider())
    assert verdict is QuestionVerdict.no
    assert captured["request"].user_prompt


async def test_v2_completion_and_guess_race_branches_preserve_the_claim_contract(monkeypatch):
    """A lost append or missing strategy row must reject safely after releasing the owned lease."""
    quota = SimpleNamespace(questions_left=4, guesses_left=2)
    claim = ai_game_service.QuestionClaim(1, "token", 2, "Q", "q", "hash", "{}", ())

    async def personal(*_args):
        return quota

    async def lock(*_args):
        return ai_game_service._now(), None

    async def duplicate(*_args):
        return SimpleNamespace(reason=ai_game_service.TurnRejectReason.lost_claim)

    async def no_terminal(*_args):
        return None

    monkeypatch.setattr(ai_game_service, "get_personal_quota", personal)
    monkeypatch.setattr(ai_game_service, "_lock_v2_action", lock)
    monkeypatch.setattr(ai_game_service, "_question_duplicate_result", duplicate)
    monkeypatch.setattr(ai_game_service, "_terminalize_if_due", no_terminal)
    monkeypatch.setattr(ai_game_service, "_append_v2_turn", lambda *_args, **_kwargs: _async_value(None))
    lost_to_duplicate = await ai_game_service.complete_question(None, claim=claim, verdict=QuestionVerdict.si)
    assert lost_to_duplicate.reason is ai_game_service.TurnRejectReason.lost_claim

    monkeypatch.setattr(ai_game_service, "_append_v2_turn", lambda *_args, **_kwargs: _async_value(False))
    lost = await ai_game_service.complete_question(None, claim=claim, verdict=QuestionVerdict.si)
    assert lost.reason is ai_game_service.TurnRejectReason.lost_claim

    async def terminal(*_args):
        return None

    async def state(*_args):
        return ("running", None, None, None)

    async def find(*_args):
        return None

    async def token(*_args, **_kwargs):
        return "claim"

    released: list[object] = []

    async def release(*args):
        released.append(args)
        return True

    class NoTargetSession:
        async def execute(self, _statement):
            return SimpleNamespace(one_or_none=lambda: None)

    monkeypatch.setattr(ai_game_service, "_terminalize_if_due", terminal)
    monkeypatch.setattr(ai_game_service, "_v2_state", state)
    monkeypatch.setattr(ai_game_service, "_find_normalized_turn", find)
    monkeypatch.setattr(ai_game_service, "_claim_v2_turn", token)
    monkeypatch.setattr(ai_game_service, "_release_v2_claim", release)
    closed = await ai_game_service.submit_guess(NoTargetSession(), session_id=1, user_tg_id=2, answer="Portal")
    assert closed.reason is ai_game_service.TurnRejectReason.closed and released


async def _async_value(value):
    return value
