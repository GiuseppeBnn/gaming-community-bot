"""The leaderboards — `handlers/leaderboard.py`, at 61%.

Three boards behind one message with a tab switcher. The ranking queries belong to
the services and are tested there; what this layer decides is:

  * **which board a tab means**. `lead:xp` must render levels, `lead:trofei` trophy
    counts and `lead:coins` money — a switcher that shows the right title over the
    wrong numbers is worse than no switcher;
  * **that it stays out of the group**. The message carries a «✖ Chiudi» button, and
    in a group *anyone* could press it on someone else's screen — so the command
    redirects to private (§9);
  * **that a stale tap does nothing bad**. Callback data is user-supplied and the
    keyboard outlives the message: an unknown board and an unmodified edit must both
    end quietly, not raise.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from handlers import leaderboard
from services import badge_service
from utils import cooldown

USER_ID = 7


class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(username="testbot")


class _FakeMessage:
    def __init__(self, *, chat_type: str = "private", editable: bool = True) -> None:
        self.bot = _FakeBot()
        self.editable = editable
        self.from_user = SimpleNamespace(id=USER_ID, username="tizio", full_name="Tizio")
        self.chat = SimpleNamespace(
            id=USER_ID if chat_type == "private" else -100_123, type=chat_type
        )
        self.texts: list[str] = []
        self.markups: list[object] = []
        self.deleted = False

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=len(self.texts))

    async def reply(self, text, reply_markup=None, **kw):
        return await self.answer(text, reply_markup, **kw)

    async def edit_text(self, text, reply_markup=None, **kw):
        if not self.editable:
            raise RuntimeError("message is not modified")
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def delete(self):
        if not self.editable:
            raise RuntimeError("message can't be deleted")
        self.deleted = True

    @property
    def said(self) -> str:
        return "\n".join(self.texts)


class _FakeCallback:
    def __init__(self, data: str, message=None) -> None:
        self.data = data
        self.message = message or _FakeMessage()
        self.bot = self.message.bot
        self.from_user = SimpleNamespace(id=USER_ID, username="tizio")
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    @property
    def said(self) -> str:
        return self.message.said


@pytest.fixture(autouse=True)
def _no_cooldowns():
    cooldown.reset()
    yield
    cooldown.reset()


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def _players(user_factory):
    await user_factory(tg_id=1, username="ricco", coins=10_000, xp=50)
    await user_factory(tg_id=2, username="medio", coins=500, xp=5_000)
    await user_factory(tg_id=3, username=None, coins=10, xp=0,
                       full_name="Senza Username")


class TestBoards:
    async def test_an_empty_board_says_so_instead_of_a_blank_screen(self, session):
        text = await leaderboard.render_board(session, "coins")

        assert "Nessun dato" in text

    async def test_the_money_board_ranks_by_coins(self, session, user_factory):
        await _players(user_factory)

        text = await leaderboard.render_board(session, "coins")

        assert "Ricchezza" in text
        assert text.index("@ricco") < text.index("@medio")
        assert "10,000" in text

    async def test_the_xp_board_shows_levels_not_raw_xp(self, session, user_factory):
        """XP is an implementation detail; the level is the thing players compare."""
        await _players(user_factory)

        text = await leaderboard.render_board(session, "xp")

        assert "Livelli" in text and "Livello" in text
        assert text.index("@medio") < text.index("@ricco")

    async def test_the_trophy_board_counts_trophies(
        self, seeded_session, user_factory
    ):
        await _players(user_factory)
        badges = await badge_service.get_all_badges(seeded_session)
        await badge_service.award_badge(seeded_session, 2, badges[0].slug)
        await seeded_session.commit()

        text = await leaderboard.render_board(seeded_session, "trofei")

        assert "Trofei" in text and "@medio" in text

    async def test_the_top_three_get_medals_and_the_rest_a_number(
        self, session, user_factory
    ):
        await _players(user_factory)
        await user_factory(tg_id=4, username="quarto", coins=1)

        text = await leaderboard.render_board(session, "coins")

        assert text.count("🥇") == 1 and text.count("🥈") == 1 and text.count("🥉") == 1
        assert "4." in text

    async def test_a_player_without_a_username_is_shown_by_name(
        self, session, user_factory
    ):
        """Not everyone has a @username; falling back to the full name is what keeps
        them from appearing as a blank row."""
        await _players(user_factory)

        text = await leaderboard.render_board(session, "coins")

        assert "Senza Username" in text

    async def test_an_active_tag_travels_with_the_name(self, session, user_factory):
        await user_factory(tg_id=1, username="ricco", coins=100,
                           cosmetic_tag="👑 Reietto")

        text = await leaderboard.render_board(session, "coins")

        assert "👑 Reietto" in text


class TestCommand:
    async def test_in_the_group_it_only_hands_back_a_link(self, session, user_factory):
        """The board carries a «✖ Chiudi» button: posted in the group, anyone could
        close someone else's screen."""
        await _players(user_factory)
        message = _FakeMessage(chat_type="supergroup")

        await leaderboard.cmd_classifiche(message, session)

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=classifiche")

    async def test_in_private_it_opens_on_the_money_board(self, session, user_factory):
        await _players(user_factory)
        message = _FakeMessage()

        await leaderboard.cmd_classifiche(message, session)

        assert "Ricchezza" in message.said
        assert set(_callbacks(message.markups[0])) == {
            "lead:coins", "lead:xp", "lead:trofei", "lead:close",
        }

    async def test_the_active_tab_is_marked(self, session, user_factory):
        await _players(user_factory)
        message = _FakeMessage()

        await leaderboard.show_board_private(message, session)

        labels = [b.text for row in message.markups[0].inline_keyboard for b in row]
        assert sum(t.startswith("•") for t in labels) == 1

    async def test_the_second_call_within_the_window_is_refused(
        self, session, user_factory
    ):
        await _players(user_factory)
        first, second = _FakeMessage(), _FakeMessage()

        await leaderboard.show_board_private(first, session)
        await leaderboard.show_board_private(second, session)

        assert first.said and "più piano" in second.said.lower()


class TestSwitcher:
    @pytest.mark.parametrize("board,expected", [
        ("coins", "Ricchezza"),
        ("xp", "Livelli"),
        ("trofei", "Trofei"),
    ])
    async def test_each_tab_renders_its_own_board(
        self, session, user_factory, board, expected
    ):
        await _players(user_factory)
        callback = _FakeCallback(f"lead:{board}")

        await leaderboard.cb_lead(callback, session)

        assert expected in callback.said

    async def test_the_switched_board_marks_its_own_tab(self, session, user_factory):
        await _players(user_factory)
        callback = _FakeCallback("lead:xp")

        await leaderboard.cb_lead(callback, session)

        labels = [b.text for row in callback.message.markups[0].inline_keyboard for b in row]
        assert any(t.startswith("•") and "XP" in t for t in labels)

    async def test_an_unknown_board_is_ignored(self, session, user_factory):
        """Callback data is user-supplied; an old or forged tab must do nothing."""
        await _players(user_factory)
        callback = _FakeCallback("lead:inventata")

        await leaderboard.cb_lead(callback, session)

        assert callback.said == "" and callback.answers == [(None, False)]

    async def test_close_deletes_the_message(self, session):
        callback = _FakeCallback("lead:close")

        await leaderboard.cb_lead(callback, session)

        assert callback.message.deleted

    async def test_close_on_a_message_that_cannot_be_deleted_still_answers(self, session):
        """Telegram refuses to delete messages older than 48h; leaving the callback
        unanswered would spin the client's loading indicator forever."""
        callback = _FakeCallback("lead:close", message=_FakeMessage(editable=False))

        await leaderboard.cb_lead(callback, session)

        assert callback.answers == [(None, False)]

    async def test_re_tapping_the_current_tab_is_harmless(self, session, user_factory):
        """Telegram rejects an edit that changes nothing; that error must not reach
        the user as a failed button."""
        await _players(user_factory)
        callback = _FakeCallback("lead:coins", message=_FakeMessage(editable=False))

        await leaderboard.cb_lead(callback, session)

        assert callback.answers == [(None, False)]
