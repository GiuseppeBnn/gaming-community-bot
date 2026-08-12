from services.ai_game_service import GameSnapshot
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
