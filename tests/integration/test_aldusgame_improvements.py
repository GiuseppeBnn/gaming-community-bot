"""Full history and deferred title verification keep existing financial fences."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update, func

from database.models import AIGameSession, AIGameTurn, TwentyQuestionsGame, User
from handlers import twenty_questions as ui
from handlers.callbacks import TwentyQuestionsHistoryCb
from services import ai_game_service as game, ai_service
from services.ai_game_types import FinishReason
from services.twenty_questions_catalog import GameDossier


async def _running(session, monkeypatch):
    monkeypatch.setattr(game.settings, "twentyq_v2_enabled", True)
    monkeypatch.setattr(game, "has_configured_twenty_questions_provider", lambda: True)
    created = await game.create_twenty_questions(
        session, creator_tg_id=42, title="Gioco", duration_seconds=3600,
        expires_at=None, max_coins_per_participant=100,
        target=GameDossier("portal_2", "Portal 2", (), "Puzzle cooperativo con portali."),
    )
    await game.start(session, created.session_id, group_id=-1001)
    await game.move_anchor(session, created.session_id, 77)
    session.add_all([
        AIGameTurn(
            session_id=created.session_id, turn_no=n, user_tg_id=42, kind="question",
            input_text=f"Domanda <{n}>?", output_json='{"verdetto":"si"}' if n % 2 else '{"verdetto":"no"}',
        ) for n in range(1, 9)
    ])
    await session.execute(update(AIGameSession).where(AIGameSession.id == created.session_id).values(
        next_turn_no=9,
    ).execution_options(synchronize_session=False))
    await session.commit()
    return created.session_id


@pytest.mark.parametrize("finished", [False, True, "victory"])
async def test_all_history_pages_survive_updates_and_terminal_result_without_repaying(
    session, monkeypatch, user_factory, finished,
):
    await user_factory(42)
    session_id = await _running(session, monkeypatch)
    if finished == "victory":
        won = await game.submit_guess(session, session_id=session_id, user_tg_id=42, answer="Portal2")
        assert won.correct
        await session.commit()
    elif finished:
        await game.terminalize(session, session_id=session_id, reason=FinishReason.admin_closed)
        await session.commit()
    before_xp = await session.scalar(select(User.xp).where(User.tg_id == 42))
    await session.commit()

    async def edit(**kw):
        assert not session.in_transaction()

    bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=edit))
    callback = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=-1001), message_id=77),
        bot=bot, answer=AsyncMock(),
    )
    for page, first, last in ((0, 1, 6), (1, 7, 8), (0, 1, 6)):
        await ui.cb_game_history(callback, TwentyQuestionsHistoryCb(session_id=session_id, page=page), session)
        text = bot.edit_message_text.call_args.kwargs["text"]
        assert f"Domanda &lt;{first}&gt;" in text and f"Domanda &lt;{last}&gt;" in text
        assert "SÌ" in text and "NO" in text
        assert f"{page + 1}/2" in text
        assert ("Portal 2" in text) is bool(finished)
        assert "precedenti" in str(bot.edit_message_text.call_args.kwargs["reply_markup"]).lower() or page == 0
    assert await session.scalar(select(func.count()).select_from(AIGameTurn)) == (9 if finished == "victory" else 8)
    assert await session.scalar(select(User.xp).where(User.tg_id == 42)) == before_xp
    # Appending does not move or overwrite the six entries on the first page.
    if not finished:
        session.add(AIGameTurn(
            session_id=session_id, turn_no=9, user_tg_id=42, kind="question",
            input_text="Nuova domanda", output_json='{"verdetto":"no"}',
        ))
        await session.commit()
        page = await game.get_game_view(session, session_id, history_page=0)
        assert [t.turn_no for t in page.recent_turns] == list(range(1, 7))
        last_page = await game.get_game_view(session, session_id)
        assert [t.turn_no for t in last_page.recent_turns] == [7, 8, 9]


@pytest.mark.parametrize("target,group,anchor,page", [
    ("missing", -1001, 77, 0), ("game", -999, 77, 0),
    ("game", -1001, 88, 0), ("game", -1001, 77, -1), ("game", -1001, 77, 99),
    ("ready", -1001, 77, 0), ("legacy", -1001, 77, 0),
])
async def test_history_rejects_foreign_cards_and_invalid_pages(session, monkeypatch, target, group, anchor, page):
    session_id = await _running(session, monkeypatch)
    if target == "ready":
        await session.execute(update(AIGameSession).where(AIGameSession.id == session_id).values(status="ready"))
    elif target == "legacy":
        await session.execute(update(TwentyQuestionsGame).where(TwentyQuestionsGame.session_id == session_id).values(rules_version=1))
    await session.commit()
    callback = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=group), message_id=anchor),
        bot=SimpleNamespace(edit_message_text=AsyncMock()), answer=AsyncMock(),
    )
    await ui.cb_game_history(callback, TwentyQuestionsHistoryCb(
        session_id=-1 if target == "missing" else session_id, page=page,
    ), session)
    assert callback.answer.call_args.args == ("Storico non disponibile.",)
    callback.bot.edit_message_text.assert_not_awaited()


async def test_history_edit_failure_never_publishes_a_new_card(session, monkeypatch):
    session_id = await _running(session, monkeypatch)
    bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=RuntimeError("test")))
    callback = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=-1001), message_id=77),
        bot=bot, answer=AsyncMock(),
    )
    await ui.cb_game_history(callback, TwentyQuestionsHistoryCb(session_id=session_id, page=0), session)
    assert (await game.get_game_view(session, session_id)).anchor_message_id == 77


@pytest.mark.parametrize("failure", [False, True])
async def test_guess_handler_commits_both_boundaries_and_refunds_provider_failure(
    session, monkeypatch, user_factory, failure,
):
    await user_factory(42)
    session_id = await _running(session, monkeypatch)
    # Seeded history is independent of the user's guess quota.
    async def classify(canonical, answer, *, dossier_json):
        assert not session.in_transaction()
        assert canonical == "Portal 2" and answer == "Portl 2"
        assert "cooperativo" in dossier_json
        if failure:
            raise ai_service.AIServiceError("unavailable")
        return "correct"

    monkeypatch.setattr(ui, "classify_title", classify)
    monkeypatch.setattr(ui, "publish_terminal", AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=-1001), bot=object(),
        reply=AsyncMock(return_value=None),
    )
    await ui._play_v2_guess(message, session, session_id=session_id, answer="Portl 2")
    assert not session.in_transaction()
    quota = await game.get_personal_quota(session, session_id, 42)
    assert quota.guesses_used == (0 if failure else 1)
    assert ui.publish_terminal.await_count == (0 if failure else 1)
    if failure:
        assert "non è stato consumato" in message.reply.call_args.args[0]
