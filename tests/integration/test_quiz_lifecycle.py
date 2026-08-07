"""Launching and closing a quiz — `handlers/quiz.open_quiz` / `close_quiz`.

`close_quiz` is the money one: it ranks the finishers, pays the prize pool, records
trophy progress and publishes the podium. `open_quiz` is the one with an ordering
decision that is easy to reverse by accident.

The service layer is covered in `test_quiz_service.py`; these two functions are the
orchestration on top, and the parts worth pinning are the ones where the order of
operations *is* the guarantee:

  * `open_quiz` announces in the group **before** flipping the status, so a send that
    fails leaves a `ready` quiz instead of a `running` one nobody was told about;
  * `close_quiz` claims the close as a conditional UPDATE **before** paying, so two
    admins closing at once cannot pay the pool twice;
  * trophies and the podium message are announced **after** the commit, and a failure
    there must not turn a paid-out quiz into an error.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import Badge, Quiz, Wallet
from handlers.quiz import lifecycle as quiz_handlers
from services import quiz_service
from utils import cooldown, static_reply

ADMIN_ID = 1
GROUP_ID = -100123


class _FakeBot:
    id = 999_999

    def __init__(self) -> None:
        self.group_messages: list[str] = []
        self.sent: list[tuple[int, str]] = []

    async def get_me(self):
        return SimpleNamespace(username="testbot")

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _FakeMessage:
    def __init__(self, bot=None, chat_type: str = "private") -> None:
        self.bot = bot or _FakeBot()
        self.from_user = SimpleNamespace(id=ADMIN_ID, username="admin", full_name="Admin")
        self.chat = SimpleNamespace(
            id=ADMIN_ID if chat_type == "private" else GROUP_ID, type=chat_type
        )
        self.replies: list[str] = []

    async def reply(self, text, reply_markup=None, **kw):
        self.replies.append(text)
        return SimpleNamespace(message_id=len(self.replies))

    async def answer(self, text, reply_markup=None, **kw):
        self.replies.append(text)
        return SimpleNamespace(message_id=len(self.replies))

    @property
    def said(self) -> str:
        return "\n".join(self.replies)


def _cmd(args: str | None):
    return SimpleNamespace(args=args)


class _Group:
    """Records what would be published in the group, and can be made to fail."""

    def __init__(self, monkeypatch, *, group_id: int = GROUP_ID, failing: bool = False):
        self.messages: list[str] = []
        monkeypatch.setattr(
            quiz_handlers.group_registry, "get_group_id", lambda: group_id
        )

        async def send(bot, db, text, reply_markup=None, **kw):
            if failing:
                raise RuntimeError("Forbidden: bot is not a member of the supergroup chat")
            self.messages.append(text)

        monkeypatch.setattr(quiz_handlers.group_registry, "send_group_message", send)

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


async def _quiz_with_players(session, user_factory, *, prize_first=1000, prize_second=500,
                             players=((10, 2), (11, 1))):
    """A running quiz of 2 questions; `players` is (tg_id, how many correct answers)."""
    await user_factory(tg_id=ADMIN_ID, username="admin")
    quiz = await quiz_service.create_quiz(
        session, ADMIN_ID, "Capitali", "Geo",
        prize_first=prize_first, prize_second=prize_second,
    )
    q1 = await quiz_service.add_question(
        session, quiz.id, "Capitale d'Italia?", ["Roma", "Milano"], 0, "È Roma"
    )
    q2 = await quiz_service.add_question(
        session, quiz.id, "Capitale di Francia?", ["Parigi", "Lione"], 0, None
    )
    await quiz_service.set_status(session, quiz.id, "running")
    await session.commit()

    for tg_id, correct in players:
        await user_factory(tg_id=tg_id, username=f"u{tg_id}", coins=0)
        for i, question in enumerate((q1, q2)):
            chosen = 0 if i < correct else 1
            await quiz_service.record_answer(
                session, quiz.id, question.id, tg_id, chosen, response_ms=1000
            )
    await session.commit()
    return quiz


async def _coins(session, tg_id: int) -> int:
    return (
        await session.execute(select(Wallet.coins).where(Wallet.tg_id == tg_id))
    ).scalar_one()


async def _status(session, quiz_id: int) -> str:
    return (
        await session.execute(select(Quiz.status).where(Quiz.id == quiz_id))
    ).scalar_one()


class TestOpen:
    async def test_launching_announces_and_sets_it_running(
        self, session, user_factory, monkeypatch
    ):
        group = _Group(monkeypatch)
        quiz = await _quiz_with_players(session, user_factory, players=())
        await quiz_service.set_status(session, quiz.id, "ready")
        await session.commit()

        ok, msg = await quiz_handlers.open_quiz(_FakeBot(), session, quiz.id)
        await session.commit()

        assert ok
        assert await _status(session, quiz.id) == "running"
        assert "Capitali" in group.text
        assert "2 domande" in group.text, "players need to know how long it is"

    async def test_the_announcement_carries_the_admin_description(
        self, session, user_factory, monkeypatch
    ):
        """The admin's own description was collected in creation but never shown to
        players — neither in the group nor in private. It belongs under the generic
        invite."""
        group = _Group(monkeypatch)
        quiz = await _quiz_with_players(session, user_factory, players=())
        await quiz_service.set_status(session, quiz.id, "ready")
        await session.commit()

        await quiz_handlers.open_quiz(_FakeBot(), session, quiz.id)

        assert "Geo" in group.text, "the description must reach the group"

    async def test_an_empty_description_adds_no_dangling_line(
        self, session, user_factory, monkeypatch
    ):
        """The description is optional (skippable with «-» in creation), so an empty
        one must not leave a stray «📝» marker in the announcement."""
        group = _Group(monkeypatch)
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await quiz_service.create_quiz(session, ADMIN_ID, "Senza descrizione", "")
        await quiz_service.add_question(session, quiz.id, "Roma?", ["sì", "no"], 0, None)
        await quiz_service.set_status(session, quiz.id, "ready")
        await session.commit()

        await quiz_handlers.open_quiz(_FakeBot(), session, quiz.id)

        assert "📝" not in group.text

    async def test_a_failed_announcement_leaves_the_quiz_ready(
        self, session, user_factory, monkeypatch
    ):
        """The ordering that matters. If the status flipped first, a bot that cannot
        post in the group would leave a `running` quiz nobody was ever told about —
        invisible, unplayable, and blocking the next launch.
        """
        _Group(monkeypatch, failing=True)
        quiz = await _quiz_with_players(session, user_factory, players=())
        await quiz_service.set_status(session, quiz.id, "ready")
        await session.commit()

        ok, msg = await quiz_handlers.open_quiz(_FakeBot(), session, quiz.id)

        assert not ok
        assert await _status(session, quiz.id) == "ready"
        assert "gruppo" in msg

    async def test_without_a_group_nothing_is_launched(
        self, session, user_factory, monkeypatch
    ):
        _Group(monkeypatch, group_id=0)
        quiz = await _quiz_with_players(session, user_factory, players=())

        ok, msg = await quiz_handlers.open_quiz(_FakeBot(), session, quiz.id)

        assert not ok and "GROUP_ID" in msg

    async def test_a_quiz_already_running_or_finished_is_refused(
        self, session, user_factory, monkeypatch
    ):
        group = _Group(monkeypatch)
        quiz = await _quiz_with_players(session, user_factory, players=())

        ok, msg = await quiz_handlers.open_quiz(_FakeBot(), session, quiz.id)
        assert not ok and "già in corso" in msg

        await quiz_service.set_status(session, quiz.id, "finished")
        await session.commit()
        ok, msg = await quiz_handlers.open_quiz(_FakeBot(), session, quiz.id)
        assert not ok and "già stato giocato" in msg

        assert group.messages == [], "a refused launch still announced something"

    async def test_a_quiz_without_questions_is_refused(
        self, session, user_factory, monkeypatch
    ):
        """An empty quiz would announce itself, then hand every player a finished
        scoreboard with no questions."""
        _Group(monkeypatch)
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await quiz_service.create_quiz(session, ADMIN_ID, "Vuoto", "d")
        await quiz_service.set_status(session, quiz.id, "ready")
        await session.commit()

        ok, msg = await quiz_handlers.open_quiz(_FakeBot(), session, quiz.id)

        assert not ok and "senza domande" in msg

    async def test_the_command_validates_its_argument(
        self, session, user_factory, monkeypatch
    ):
        _Group(monkeypatch)
        await user_factory(tg_id=ADMIN_ID, username="admin")

        for args in (None, "", "abc", "12 34"):
            message = _FakeMessage()
            await quiz_handlers.cmd_avvia_quiz(message, _cmd(args), session)
            assert "Uso:" in message.said, args


class TestClose:
    async def test_closing_pays_the_podium_and_publishes_it(
        self, session, user_factory, monkeypatch
    ):
        group = _Group(monkeypatch)
        quiz = await _quiz_with_players(session, user_factory)

        ok, msg = await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert ok
        assert await _status(session, quiz.id) == "finished"
        assert await _coins(session, 10) == 1000, "the winner was not paid first prize"
        assert await _coins(session, 11) == 500
        assert "PODIO" in group.text

    async def test_closing_twice_does_not_pay_twice(
        self, session, user_factory, monkeypatch
    ):
        """`claim_close` is a conditional UPDATE, so only one caller can win the
        transition. The assertion is on the wallets rather than on the message,
        because the message is cosmetic and the balances are not."""
        _Group(monkeypatch)
        quiz = await _quiz_with_players(session, user_factory)

        await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)
        after_first = await _coins(session, 10)

        ok, msg = await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert not ok and "già stato chiuso" in msg
        assert await _coins(session, 10) == after_first

    async def test_a_quiz_that_never_started_cannot_be_closed(
        self, session, user_factory, monkeypatch
    ):
        _Group(monkeypatch)
        quiz = await _quiz_with_players(session, user_factory, players=())
        await quiz_service.set_status(session, quiz.id, "ready")
        await session.commit()

        ok, msg = await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert not ok and "non è in corso" in msg

    async def test_a_missing_quiz_is_reported(self, session, monkeypatch):
        _Group(monkeypatch)

        ok, msg = await quiz_handlers.close_quiz(_FakeBot(), session, 999999)

        assert not ok and "non trovato" in msg

    async def test_a_quiz_nobody_finished_still_closes(
        self, session, user_factory, monkeypatch
    ):
        """Closing has to work even with an empty podium, or a quiz nobody played
        would stay `running` forever and block the next one."""
        group = _Group(monkeypatch)
        quiz = await _quiz_with_players(session, user_factory, players=())

        ok, msg = await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert ok
        assert await _status(session, quiz.id) == "finished"
        assert "Nessun partecipante" in group.text

    async def test_without_a_group_the_podium_comes_back_to_the_caller(
        self, session, user_factory, monkeypatch
    ):
        """With no group configured there is nowhere to publish, so the podium is the
        return value instead — otherwise closing a quiz would silently discard it."""
        _Group(monkeypatch, group_id=0)
        quiz = await _quiz_with_players(session, user_factory)

        ok, msg = await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert ok and "PODIO" in msg

    async def test_a_failed_podium_announcement_does_not_undo_the_payout(
        self, session, user_factory, monkeypatch
    ):
        """The prizes are committed before the announcement. If publishing fails, the
        close still succeeded — reporting an error would invite the admin to close
        again, and only `claim_close` would stand between that and a second payout."""
        _Group(monkeypatch, failing=True)
        quiz = await _quiz_with_players(session, user_factory)

        ok, msg = await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert ok
        assert await _coins(session, 10) == 1000
        assert await _status(session, quiz.id) == "finished"

    async def test_the_podium_names_the_players_and_their_prizes(
        self, session, user_factory, monkeypatch
    ):
        group = _Group(monkeypatch)
        quiz = await _quiz_with_players(session, user_factory)

        await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert "u10" in group.text and "u11" in group.text
        assert "1,000" in group.text or "1000" in group.text

    async def test_the_command_validates_its_argument(
        self, session, user_factory, monkeypatch
    ):
        _Group(monkeypatch)
        await user_factory(tg_id=ADMIN_ID, username="admin")

        for args in (None, "", "abc"):
            message = _FakeMessage()
            await quiz_handlers.cmd_chiudi_quiz(message, _cmd(args), session)
            assert "Uso:" in message.said, args

    async def test_the_command_reports_the_outcome(
        self, session, user_factory, monkeypatch
    ):
        _Group(monkeypatch)
        quiz = await _quiz_with_players(session, user_factory)

        message = _FakeMessage()
        await quiz_handlers.cmd_chiudi_quiz(message, _cmd(str(quiz.id)), session)
        assert "chiuso" in message.said

        again = _FakeMessage()
        await quiz_handlers.cmd_chiudi_quiz(again, _cmd(str(quiz.id)), session)
        assert "⚠️" in again.said


class TestTrophiesOnClose:
    async def test_the_podium_records_progress_for_the_trophy_engine(
        self, session, user_factory, monkeypatch
    ):
        """`record_podium` is what feeds the `podium_count` / `first_place_count`
        trophies. It is idempotent per (user, metric, quiz), which is what lets a
        re-close be harmless — but only if it is called in the first place."""
        _Group(monkeypatch)
        recorded: list[tuple[int, int]] = []

        real = quiz_handlers.progress_service.record_podium

        async def spy(db, tg_id, game, rank, ref_id):
            recorded.append((tg_id, rank))
            return await real(db, tg_id, game, rank, ref_id)

        monkeypatch.setattr(quiz_handlers.progress_service, "record_podium", spy)
        quiz = await _quiz_with_players(session, user_factory)

        await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert recorded == [(10, 1), (11, 2)]

    async def test_the_last_place_of_a_two_player_quiz_is_recorded(
        self, session, user_factory, monkeypatch
    ):
        """There are hidden trophies for coming last, and they only make sense with at
        least two players — otherwise the only participant is both first and last."""
        _Group(monkeypatch)
        events: list[tuple[int, str]] = []

        async def spy(db, tg_id, metric, ref_id):
            events.append((tg_id, metric))

        monkeypatch.setattr(quiz_handlers.progress_service, "record_event", spy)
        quiz = await _quiz_with_players(session, user_factory)

        await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert (11, quiz_handlers.progress_service.TRIVIA_LAST_PLACE) in events

    async def test_a_single_player_is_not_recorded_as_last(
        self, session, user_factory, monkeypatch
    ):
        _Group(monkeypatch)
        events: list[tuple[int, str]] = []

        async def spy(db, tg_id, metric, ref_id):
            events.append((tg_id, metric))

        monkeypatch.setattr(quiz_handlers.progress_service, "record_event", spy)
        quiz = await _quiz_with_players(session, user_factory, players=((10, 2),))

        await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert quiz_handlers.progress_service.TRIVIA_LAST_PLACE not in [m for _, m in events]

    async def test_a_fast_run_is_recorded(self, session, user_factory, monkeypatch):
        """Under 30 seconds of total answering time unlocks the speed trophies. The
        helper answers each question in 1000 ms, so both players qualify."""
        _Group(monkeypatch)
        events: list[tuple[int, str]] = []

        async def spy(db, tg_id, metric, ref_id):
            events.append((tg_id, metric))

        monkeypatch.setattr(quiz_handlers.progress_service, "record_event", spy)
        quiz = await _quiz_with_players(session, user_factory)

        await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        fast = [tg for tg, metric in events
                if metric == quiz_handlers.progress_service.TRIVIA_SUB30]
        assert sorted(fast) == [10, 11]

    async def test_a_trophy_unlocked_by_the_quiz_is_announced_in_the_group(
        self, session, user_factory, monkeypatch, seeded_session
    ):
        """Trophies earned by playing are announced where everyone sees them, after
        the commit. Only `check_and_award_milestones` is stubbed — what it returns is
        a real `Badge` row, because `announce_trophies` reads `icon_emoji`/`name` off
        it and a stand-in object would take a different path than production.

        The failure case is not repeated here: `announce_trophies` swallows its own
        send errors, so it is already covered by the test above that makes the group
        refuse every message.
        """
        group = _Group(monkeypatch)
        badges = list(
            (await seeded_session.execute(select(Badge).limit(1))).scalars().all()
        )
        assert badges, "no badge seeded — nothing would be announced"

        async def award(db, tg_id):
            return badges if tg_id == 10 else []

        monkeypatch.setattr(
            quiz_handlers.badge_service, "check_and_award_milestones", award
        )
        quiz = await _quiz_with_players(session, user_factory)

        await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert any("trofeo" in m for m in group.messages), (
            f"no trophy announcement among {len(group.messages)} group messages"
        )
        assert await _coins(session, 10) == 1000


# ---------------------------------------------------------------------------
# The commands around the two functions
# ---------------------------------------------------------------------------

class _GroupMessage(_FakeMessage):
    """A message in the group, recording the keyboard a reply carries."""

    def __init__(self, bot=None) -> None:
        super().__init__(bot=bot, chat_type="supergroup")
        self.markups: list[object] = []

    async def reply(self, text, reply_markup=None, **kw):
        self.replies.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=len(self.replies))

    async def answer(self, text, reply_markup=None, **kw):
        return await self.reply(text, reply_markup, **kw)


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    """`/quiz` is throttled per user and the store is module-level."""
    cooldown.reset()
    static_reply.reset()
    yield
    cooldown.reset()
    static_reply.reset()


def _as_admin(monkeypatch, value: bool):
    async def _is_admin(bot, uid):
        return value

    monkeypatch.setattr(quiz_handlers, "is_admin", _is_admin)


class TestQuizCommand:
    async def test_a_player_with_no_quiz_running_is_told_so(
        self, session, monkeypatch, user_factory
    ):
        """Never silence: the old admin-only filter simply dropped a member's /quiz,
        which reads as a broken bot."""
        _as_admin(monkeypatch, False)
        await user_factory(tg_id=ADMIN_ID, username="tizio")
        message = _GroupMessage()

        await quiz_handlers.cmd_quiz_list(message, session)

        assert "Nessun quiz attivo" in message.said

    async def test_a_player_gets_a_private_play_link_for_a_running_quiz(
        self, session, monkeypatch, user_factory
    ):
        """The quiz is played in private; the group button is the way in."""
        _as_admin(monkeypatch, False)
        quiz = await _quiz_with_players(session, user_factory, players=())
        message = _GroupMessage()

        await quiz_handlers.cmd_quiz_list(message, session)

        urls = [b.url for row in message.markups[-1].inline_keyboard for b in row]
        assert any(u.endswith(f"?start=quiz_{quiz.id}") for u in urls)

    async def test_the_second_call_within_the_window_says_nothing(
        self, session, monkeypatch, user_factory
    ):
        """A silent throttle on purpose: a «slow down» notice in the group would be
        the flood it is trying to prevent."""
        _as_admin(monkeypatch, False)
        await user_factory(tg_id=ADMIN_ID, username="tizio")
        first, second = _GroupMessage(), _GroupMessage()

        await quiz_handlers.cmd_quiz_list(first, session)
        await quiz_handlers.cmd_quiz_list(second, session)

        assert first.said and second.said == ""

    async def test_an_admin_in_the_group_is_sent_to_the_panel_in_private(
        self, session, monkeypatch, user_factory
    ):
        """The management list has delete and launch buttons on it."""
        _as_admin(monkeypatch, True)
        await user_factory(tg_id=ADMIN_ID, username="admin")
        message = _GroupMessage()

        await quiz_handlers.cmd_quiz_list(message, session)

        urls = [b.url for row in message.markups[0].inline_keyboard for b in row]
        assert any(u.endswith("?start=admin") for u in urls)

    async def test_an_admin_in_private_gets_the_management_list(
        self, session, monkeypatch, user_factory
    ):
        _as_admin(monkeypatch, True)
        await _quiz_with_players(session, user_factory, players=())
        message = _FakeMessage()

        await quiz_handlers.cmd_quiz_list(message, session)

        assert "Quiz" in message.said


class TestLegacyCommands:
    async def test_avvia_quiz_in_the_group_is_redirected(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        message = _GroupMessage()

        await quiz_handlers.cmd_avvia_quiz(message, _cmd("1"), session)

        urls = [b.url for row in message.markups[0].inline_keyboard for b in row]
        assert any(u.endswith("?start=admin") for u in urls)

    async def test_avvia_quiz_starts_a_ready_quiz_in_private(
        self, session, user_factory, monkeypatch
    ):
        group = _Group(monkeypatch)
        await user_factory(tg_id=ADMIN_ID, username="admin")
        quiz = await quiz_service.create_quiz(session, ADMIN_ID, "Capitali", "Geo")
        await quiz_service.add_question(session, quiz.id, "Roma?", ["sì", "no"], 0, None)
        await quiz_service.set_status(session, quiz.id, "ready")
        await session.commit()
        message = _FakeMessage()

        await quiz_handlers.cmd_avvia_quiz(message, _cmd(str(quiz.id)), session)
        await session.rollback()  # only a committed launch survives this

        assert group.messages, "the group must be told"
        status = (await session.execute(
            select(Quiz.status).where(Quiz.id == quiz.id)
        )).scalar_one()
        assert status == "running"
        assert "🎬" in message.said

    async def test_avvia_quiz_reports_a_refusal_without_committing(
        self, session, user_factory, monkeypatch
    ):
        _Group(monkeypatch)
        await user_factory(tg_id=ADMIN_ID, username="admin")
        message = _FakeMessage()

        await quiz_handlers.cmd_avvia_quiz(message, _cmd("999999"), session)

        assert "⚠️" in message.said

    async def test_chiudi_quiz_in_the_group_is_redirected(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        message = _GroupMessage()

        await quiz_handlers.cmd_chiudi_quiz(message, _cmd("1"), session)

        urls = [b.url for row in message.markups[0].inline_keyboard for b in row]
        assert any(u.endswith("?start=admin") for u in urls)

    async def test_closing_a_quiz_deleted_between_the_claim_and_the_payout(
        self, session, user_factory, monkeypatch
    ):
        """`claim_close` flips the status with a conditional UPDATE and only then is
        the quiz read back. A delete landing in between must report «not found»,
        not crash halfway through paying a pool."""
        _Group(monkeypatch)
        quiz = await _quiz_with_players(session, user_factory)

        real_get = quiz_service.get_quiz

        async def _vanished(db, quiz_id):
            if quiz_id == quiz.id:
                return None
            return await real_get(db, quiz_id)

        monkeypatch.setattr(quiz_service, "get_quiz", _vanished)

        ok, msg = await quiz_handlers.close_quiz(_FakeBot(), session, quiz.id)

        assert not ok and "non trovato" in msg
