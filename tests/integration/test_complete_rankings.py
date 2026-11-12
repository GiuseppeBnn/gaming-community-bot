"""No implicit top-ten cap, safe Telegram pages, no missing participants."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from database.models import User, UserBadge, Wallet
from handlers import admin, admin_dashboard, leaderboard
from handlers.callbacks import AdminCb, LeaderboardPageCb
from handlers.guess import lifecycle as guess_lifecycle
from handlers.quiz import lifecycle as quiz_lifecycle
from services import admin_service, badge_service, xp_service
from services.guess_service import RankedPlayer
from services.quiz_service import PodiumRow
from utils.text import chunk_html_lines


async def _players(session, count=25, *, long_names=False):
    for uid in range(1, count + 1):
        session.add(User(tg_id=uid, username=None if long_names else f"player_{uid:03d}",
                         full_name=f"Giocatore {uid:03d}" + (" 🎮" * 80 if long_names else ""),
                         xp=100))
        session.add(Wallet(tg_id=uid, coins=100))
    await session.commit()


@pytest.mark.parametrize("board", ["coins", "xp", "trofei"])
async def test_services_return_every_eligible_user_unless_explicitly_limited(
    seeded_session, board,
):
    session = seeded_session
    await _players(session)
    badges = await badge_service.get_all_badges(session)
    session.add_all(UserBadge(user_tg_id=uid, badge_id=badges[0].id) for uid in range(1, 26))
    await session.commit()
    query = {"coins": admin_service.leaderboard, "xp": xp_service.leaderboard_xp,
             "trofei": badge_service.leaderboard_trophies}[board]

    rows = await query(session)
    assert [user.tg_id for user, _ in rows] == list(range(1, 26))
    assert len(await query(session, limit=10)) == 10
    assert await query(session, limit=0) == []
    text = await leaderboard.render_board(session, board)
    assert "@player_025" in text and "25." in text


@pytest.mark.parametrize("board", ["coins", "xp", "trofei"])
async def test_every_player_is_reachable_in_long_interactive_boards(seeded_session, board):
    session = seeded_session
    await _players(session, count=45, long_names=True)
    badges = await badge_service.get_all_badges(session)
    session.add_all(UserBadge(user_tg_id=uid, badge_id=badges[0].id) for uid in range(1, 46))
    await session.commit()
    first, _, pages = await leaderboard.render_board_page(session, board)
    assert pages > 1 and "Pagina 1/" in first
    content = []
    for index in range(pages):
        text, page, total = await leaderboard.render_board_page(session, board, index)
        assert page == index and total == pages
        assert len(text.encode("utf-16-le")) // 2 <= 4096
        content.append(text)
        markup = leaderboard._kb(board, page, total)
        nav = [LeaderboardPageCb.unpack(button.callback_data)
               for row in markup.inline_keyboard for button in row
               if button.callback_data.startswith("lead_page:")]
        assert {item.page for item in nav} == (
            ({page - 1} if page else set()) | ({page + 1} if page + 1 < total else set())
        )
        assert all(item.board == board for item in nav)
    for uid in range(1, 46):
        assert "\n".join(content).count(f"Giocatore {uid:03d}") == 1


async def test_navigation_clamps_stale_pages_and_rejects_unknown_boards(session):
    await _players(session, count=45, long_names=True)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    for requested, expected in [(-1, 0), (10**12, None)]:
        payload = LeaderboardPageCb(board="coins", page=requested)
        await leaderboard.cb_lead_page(callback, payload, session)
        text, page, total = await leaderboard.render_board_page(session, "coins", requested)
        assert page == (total - 1 if expected is None else expected)
        assert callback.message.edit_text.await_args.args[0] == text
    callback.message.edit_text.reset_mock()
    await leaderboard.cb_lead_page(callback, LeaderboardPageCb(board="junk", page=0), session)
    callback.message.edit_text.assert_not_awaited()


async def test_admin_dashboard_navigation_reaches_the_last_page(session):
    await _players(session, count=45, long_names=True)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await admin_dashboard.cb_lead(callback, session)
    text, _, total = await leaderboard.render_board_page(session, "coins")
    assert callback.message.edit_text.await_args.args[0] == text
    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    next_page = [AdminCb.unpack(button.callback_data)
                 for row in markup.inline_keyboard for button in row
                 if button.callback_data.startswith("adm:lead_board:coins:1")]
    assert len(next_page) == 1
    payload = AdminCb(action="lead_board", key="coins", item_id=total - 1)
    await admin_dashboard.cb_lead_board(callback, payload, session)
    last = callback.message.edit_text.await_args
    assert "Giocatore 045" in last.args[0]
    assert any("Precedenti" in button.text for row in last.kwargs["reply_markup"].inline_keyboard
               for button in row)


async def test_admin_text_command_sends_all_pages_not_an_oversized_message(session):
    await _players(session, count=45, long_names=True)
    message = SimpleNamespace(reply=AsyncMock())
    await admin.cmd_classifica(message, session)
    pages = [call.args[0] for call in message.reply.await_args_list]
    assert len(pages) > 1
    assert all(len(page.encode("utf-16-le")) // 2 <= 4096 for page in pages)
    assert "Giocatore 045" in pages[-1]


@pytest.mark.parametrize("kind", ["quiz", "guess", "sound"])
async def test_event_results_include_all_participants_with_original_positions(session, kind):
    await _players(session, count=45, long_names=True)
    if kind == "quiz":
        ranked = [PodiumRow(uid, 2, datetime(2026, 1, 1), completion_seconds=10)
                  for uid in range(1, 46)]
        text = await quiz_lifecycle._podium_text(session, "Titolo\ncompleto", ranked, [])
    else:
        ranked = [RankedPlayer(uid, uid <= 15, 2, 1000 if uid <= 15 else None)
                  for uid in range(1, 46)]
        round_ = SimpleNamespace(kind=kind, title="Titolo\ncompleto", answer="Doom")
        text = await guess_lifecycle._podium_text(session, round_, ranked, [])
        assert text.count("non indovinato") == 30
    pages = chunk_html_lines(text)
    assert len(pages) > 1
    assert all(len(page.encode("utf-16-le")) // 2 <= 4096 for page in pages)
    assert "45." in pages[-1] and "Giocatore 045" in pages[-1]
    for uid in range(1, 46):
        assert text.count(f"Giocatore {uid:03d}") == 1


def test_new_pagination_payload_fits_telegram_and_round_trips():
    payload = LeaderboardPageCb(board="trofei", page=123456)
    assert len(payload.pack().encode()) < 64
    assert LeaderboardPageCb.unpack(payload.pack()) == payload
