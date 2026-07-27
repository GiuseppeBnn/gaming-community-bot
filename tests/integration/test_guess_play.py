"""Playing a round in private — every guard between a message and a payout.

The order of the guards is the design, and each has a test that would go green
for the wrong reason if it moved:

    round exists → running → cooldown → already solved → deadline →
    attempts left → judge → record

The deadline check is stateless: `started_at + limit`, evaluated now. No asyncio
task, nothing to lose on a restart — and re-entering does not reset it, which is
the difference between a time limit and an infinite timer.

The judge is stubbed here on purpose. What is under test is the guard chain, not
the verdict logic; that lives in `test_guess_judge.py` and would only make these
tests slower and vaguer.
"""

from __future__ import annotations

import types
from datetime import timedelta

import pytest
from sqlalchemy import select

from database.models import GuessAttempt, GuessSession
from handlers.guess import play as pl
from services import guess_judge
from services import guess_service as gs
from services.guess_judge import Verdict, normalize
from utils import cooldown


class _Bot:
    id = 999

    def __init__(self) -> None:
        self.media: list[tuple[int, str]] = []

    async def send_photo(self, chat_id, file_id, **kw):
        self.media.append((chat_id, file_id))

    async def send_audio(self, chat_id, file_id, **kw):
        self.media.append((chat_id, file_id))

    async def get_chat_administrators(self, chat_id):
        return []


class _Msg:
    def __init__(self, text: str | None = None, user_id: int = 7) -> None:
        self.text = text
        self.bot = _Bot()
        self.chat = types.SimpleNamespace(id=user_id, type="private")
        self.from_user = types.SimpleNamespace(id=user_id, full_name="Player")
        self.answers: list[str] = []

    async def answer(self, text, **kw):
        self.answers.append(text)

    async def reply(self, text, **kw):
        self.answers.append(text)

    @property
    def said(self) -> str:
        return "\n".join(self.answers)


@pytest.fixture
def state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=999, chat_id=7, user_id=7))


@pytest.fixture(autouse=True)
def _no_cooldown(monkeypatch):
    """Most tests here submit several answers in a row to reach a guard further
    down the chain, so the throttle is off by default and `TestCooldown` turns it
    back on explicitly. Leaving it on would make every one of them pass for the
    same uninteresting reason."""
    monkeypatch.setattr(pl.settings, "guess_answer_cooldown_seconds", 0)
    cooldown.reset()
    yield
    cooldown.reset()


@pytest.fixture
async def round_(session):
    r = await gs.create_round(
        session, kind="guess", creator_tg_id=1, title="Indovina",
        media_file_id="FILE", media_kind="photo", answer="Doom",
        aliases=[], hints=[(2, "Sparatutto")], max_attempts=3,
        time_limit_seconds=0, prize_first=100,
    )
    r.status = "running"
    await session.flush()
    return r


@pytest.fixture(autouse=True)
def judge(monkeypatch):
    """A local-only judge: exact match wins, everything else loses. No network,
    and no dependence on the verdict logic under test elsewhere.

    It counts its calls, because *not* being called is a property worth asserting:
    a message that a guard already rejected must never reach the model."""
    calls: list[str] = []

    async def _judge(session_, round__, raw):
        calls.append(raw)
        if normalize(raw) == normalize(round__.answer):
            return Verdict(correct=True, source="exact")
        return Verdict(correct=False, source="ai")

    monkeypatch.setattr(guess_judge, "judge", _judge)
    return calls


async def _playing(session, round_, state):
    await pl.start_guess_session(_Msg(), session, state, round_.id)


async def _solved_at(session, uid: int = 7):
    return (await session.execute(
        select(GuessSession.solved_at).where(GuessSession.user_tg_id == uid)
    )).scalar_one()


class TestEntry:
    async def test_entering_sends_the_medium_and_arms_the_state(
        self, session, round_, state
    ):
        m = _Msg()

        await pl.start_guess_session(m, session, state, round_.id)

        assert m.bot.media == [(7, "FILE")]
        assert await state.get_state() == pl.GuessPlayStates.answering.state
        assert (await state.get_data())["round_id"] == round_.id

    async def test_the_player_is_told_how_many_attempts_they_have(
        self, session, round_, state
    ):
        m = _Msg()

        await pl.start_guess_session(m, session, state, round_.id)

        assert "3" in m.said

    async def test_a_time_limit_is_shown_as_a_wall_clock_deadline(
        self, session, round_, state
    ):
        """Nobody should sit waiting for a "time's up!" that no timer will send."""
        round_.time_limit_seconds = 600
        await session.flush()
        m = _Msg()

        await pl.start_guess_session(m, session, state, round_.id)

        assert "fino alle" in m.said

    async def test_a_missing_round_says_so(self, session, state):
        m = _Msg()

        await pl.start_guess_session(m, session, state, 999)

        assert "non trovato" in m.said.lower() and await state.get_state() is None

    async def test_a_round_not_yet_started_says_so(self, session, round_, state):
        round_.status = "ready"
        await session.flush()
        m = _Msg()

        await pl.start_guess_session(m, session, state, round_.id)

        assert "non è ancora" in m.said and await state.get_state() is None

    async def test_a_finished_round_says_so(self, session, round_, state):
        round_.status = "finished"
        await session.flush()
        m = _Msg()

        await pl.start_guess_session(m, session, state, round_.id)

        assert "terminato" in m.said.lower() and await state.get_state() is None

    async def test_re_entering_does_not_reset_the_clock(self, session, round_, state):
        """Otherwise leaving and coming back is an infinite timer."""
        round_.time_limit_seconds = 600
        await session.flush()
        await pl.start_guess_session(_Msg(), session, state, round_.id)
        started = (await gs.get_session(session, round_.id, 7)).started_at

        await pl.start_guess_session(_Msg(), session, state, round_.id)

        assert (await gs.get_session(session, round_.id, 7)).started_at == started

    async def test_a_player_who_already_solved_it_is_told_so(
        self, session, round_, state
    ):
        await _playing(session, round_, state)
        await pl.fsm_answer(_Msg("Doom"), session, state)
        m = _Msg()

        await pl.start_guess_session(m, session, state, round_.id)

        assert "già" in m.said and m.bot.media == [], "no need to resend the medium"

    async def test_a_player_out_of_attempts_cannot_re_enter(
        self, session, round_, state
    ):
        await _playing(session, round_, state)
        for _ in range(3):
            await pl.fsm_answer(_Msg("Quake"), session, state)
        m = _Msg()

        await pl.start_guess_session(m, session, state, round_.id)

        assert "esaurito" in m.said.lower() and await state.get_state() is None

    async def test_a_medium_that_cannot_be_sent_does_not_arm_the_state(
        self, session, round_, state
    ):
        """Answering a round whose image never arrived would be guessing blind."""
        m = _Msg()

        async def _boom(*a, **kw):
            raise RuntimeError("wrong file identifier")
        m.bot.send_photo = _boom

        await pl.start_guess_session(m, session, state, round_.id)

        assert "admin" in m.said.lower()
        assert await state.get_state() is None


class TestAnswering:
    async def test_a_wrong_answer_is_told_how_many_are_left(
        self, session, round_, state
    ):
        await _playing(session, round_, state)
        m = _Msg("Quake")

        await pl.fsm_answer(m, session, state)

        assert "2" in m.said

    async def test_a_right_answer_wins_and_clears_the_state(
        self, session, round_, state
    ):
        await _playing(session, round_, state)
        m = _Msg("Doom")

        await pl.fsm_answer(m, session, state)

        assert await _solved_at(session) is not None
        assert await state.get_state() is None
        assert "indovinato" in m.said.lower()

    async def test_the_winning_attempt_count_is_reported(self, session, round_, state):
        await _playing(session, round_, state)
        await pl.fsm_answer(_Msg("Quake"), session, state)
        m = _Msg("Doom")

        await pl.fsm_answer(m, session, state)

        assert "2 tentativi" in m.said

    async def test_a_first_try_win_is_singular(self, session, round_, state):
        await _playing(session, round_, state)
        m = _Msg("Doom")

        await pl.fsm_answer(m, session, state)

        assert "1 tentativo" in m.said

    async def test_the_answer_is_persisted_even_when_wrong(
        self, session, round_, state
    ):
        await _playing(session, round_, state)

        await pl.fsm_answer(_Msg("Quake"), session, state)

        rows = (await session.execute(
            select(GuessAttempt.raw_answer).where(GuessAttempt.round_id == round_.id)
        )).scalars().all()
        assert rows == ["Quake"]

    async def test_a_hint_is_delivered_at_its_threshold(self, session, round_, state):
        await _playing(session, round_, state)
        await pl.fsm_answer(_Msg("Quake"), session, state)
        m = _Msg("Wolfenstein")

        await pl.fsm_answer(m, session, state)

        assert "Sparatutto" in m.said

    async def test_running_out_of_attempts_ends_the_session(
        self, session, round_, state
    ):
        await _playing(session, round_, state)
        for _ in range(2):
            await pl.fsm_answer(_Msg("Quake"), session, state)
        m = _Msg("Quake")

        await pl.fsm_answer(m, session, state)

        assert "esauriti" in m.said.lower()
        assert await state.get_state() is None

    async def test_an_expired_deadline_refuses_the_answer(
        self, session, round_, state
    ):
        round_.time_limit_seconds = 60
        await session.flush()
        await _playing(session, round_, state)
        sess = await gs.get_session(session, round_.id, 7)
        sess.started_at = sess.started_at - timedelta(seconds=120)
        await session.flush()
        m = _Msg("Doom")

        await pl.fsm_answer(m, session, state)

        assert "tempo" in m.said.lower()
        assert await _solved_at(session) is None, "a late correct answer must not win"

    async def test_a_round_closed_mid_play_stops_accepting(
        self, session, round_, state
    ):
        await _playing(session, round_, state)
        round_.status = "finished"
        await session.flush()
        m = _Msg("Doom")

        await pl.fsm_answer(m, session, state)

        assert "chiuso" in m.said.lower()
        assert await state.get_state() is None

    async def test_an_unverified_verdict_says_so_and_does_not_charge(
        self, session, round_, state, monkeypatch
    ):
        await _playing(session, round_, state)

        async def _down(session_, round__, raw):
            return Verdict(correct=False, source="unavailable", verified=False)
        monkeypatch.setattr(guess_judge, "judge", _down)
        m = _Msg("qualcosa altro")

        await pl.fsm_answer(m, session, state)

        assert "verificare" in m.said.lower()
        assert await gs.attempts_left(session, round_, 7) == 3

    async def test_the_correct_answer_is_never_echoed_on_a_wrong_guess(
        self, session, round_, state
    ):
        """A player must not learn the answer from the rejection."""
        await _playing(session, round_, state)
        m = _Msg("Quake")

        await pl.fsm_answer(m, session, state)

        assert "Doom" not in m.said

    async def test_an_empty_message_is_not_an_attempt(self, session, round_, state):
        await _playing(session, round_, state)

        await pl.fsm_answer(_Msg(None), session, state)

        assert await gs.attempts_left(session, round_, 7) == 3

    async def test_a_stale_state_pointing_at_nothing_is_survived(
        self, session, round_, state
    ):
        """FSM state has no TTL: a state left over from a deleted round must end
        the session, not raise."""
        await state.set_state(pl.GuessPlayStates.answering)
        await state.update_data(round_id=999)
        m = _Msg("Doom")

        await pl.fsm_answer(m, session, state)

        assert "chiuso" in m.said.lower() and await state.get_state() is None

    async def test_a_state_with_no_round_id_at_all_is_survived(
        self, session, state
    ):
        await state.set_state(pl.GuessPlayStates.answering)
        m = _Msg("Doom")

        await pl.fsm_answer(m, session, state)

        assert await state.get_state() is None

    async def test_a_stale_state_after_solving_accepts_nothing_more(
        self, session, round_, state
    ):
        """FSM state has no TTL (STEERING §8). A state re-armed after a win — a
        leftover from another chat, a restart, a manual deep-link — must not let
        the round be re-solved."""
        await _playing(session, round_, state)
        await pl.fsm_answer(_Msg("Doom"), session, state)
        solved_first = await _solved_at(session)
        await state.set_state(pl.GuessPlayStates.answering)
        await state.update_data(round_id=round_.id)
        m = _Msg("Doom")

        await pl.fsm_answer(m, session, state)

        assert "già" in m.said
        assert await _solved_at(session) == solved_first
        assert await state.get_state() is None

    async def test_a_stale_state_after_running_out_accepts_nothing_more(
        self, session, round_, state
    ):
        """Same shape, the other terminal state: out of attempts must stay out."""
        await _playing(session, round_, state)
        for _ in range(3):
            await pl.fsm_answer(_Msg("Quake"), session, state)
        await state.set_state(pl.GuessPlayStates.answering)
        await state.update_data(round_id=round_.id)
        m = _Msg("Doom")

        await pl.fsm_answer(m, session, state)

        assert "esauriti" in m.said.lower()
        assert await _solved_at(session) is None, "a spent budget cannot buy a win"
        assert await state.get_state() is None

    async def test_a_duplicate_attempt_number_is_reported_not_charged(
        self, session, round_, state, monkeypatch
    ):
        """Two taps landing on the same attempt number."""
        await _playing(session, round_, state)

        async def _dup(*a, **kw):
            return gs.Attempt(recorded=False, verdict=Verdict(False, "ai"),
                              attempt_no=1, attempts_left=0, solved=False, hint=None)
        monkeypatch.setattr(gs, "record_attempt", _dup)
        m = _Msg("Quake")

        await pl.fsm_answer(m, session, state)

        assert "valutando" in m.said.lower()


class TestTheGuardOrderIsLoadBearing:
    """Every guard runs *before* the judge, and the reason is the same each time:
    the judge is a paid, rate-limited external call. A message a guard already
    rejected must not cost quota, or anyone who has run out of attempts gets an
    unlimited free channel to Groq just by typing.

    These assertions are what makes the order a fact instead of a comment — they
    were added after a mutation that moved the attempts check *after* the judge
    left the whole file green.
    """

    async def test_a_player_out_of_attempts_never_reaches_the_model(
        self, session, round_, state, judge
    ):
        await _playing(session, round_, state)
        for _ in range(3):
            await pl.fsm_answer(_Msg("Quake"), session, state)
        await state.set_state(pl.GuessPlayStates.answering)
        await state.update_data(round_id=round_.id)
        judge.clear()

        await pl.fsm_answer(_Msg("Doom"), session, state)

        assert judge == [], "a spent budget must not buy an API call"

    async def test_a_throttled_message_never_reaches_the_model(
        self, session, round_, state, judge, monkeypatch
    ):
        monkeypatch.setattr(pl.settings, "guess_answer_cooldown_seconds", 60)
        await _playing(session, round_, state)
        await pl.fsm_answer(_Msg("Quake"), session, state)
        judge.clear()

        await pl.fsm_answer(_Msg("Wolfenstein"), session, state)

        assert judge == []

    async def test_a_late_message_never_reaches_the_model(
        self, session, round_, state, judge
    ):
        round_.time_limit_seconds = 60
        await session.flush()
        await _playing(session, round_, state)
        sess = await gs.get_session(session, round_.id, 7)
        sess.started_at = sess.started_at - timedelta(seconds=120)
        await session.flush()
        judge.clear()

        await pl.fsm_answer(_Msg("Doom"), session, state)

        assert judge == []

    async def test_a_message_to_a_closed_round_never_reaches_the_model(
        self, session, round_, state, judge
    ):
        await _playing(session, round_, state)
        round_.status = "finished"
        await session.flush()
        judge.clear()

        await pl.fsm_answer(_Msg("Doom"), session, state)

        assert judge == []

    async def test_an_already_solved_player_never_reaches_the_model(
        self, session, round_, state, judge
    ):
        await _playing(session, round_, state)
        await pl.fsm_answer(_Msg("Doom"), session, state)
        await state.set_state(pl.GuessPlayStates.answering)
        await state.update_data(round_id=round_.id)
        judge.clear()

        await pl.fsm_answer(_Msg("Doom"), session, state)

        assert judge == []

    async def test_an_empty_message_never_reaches_the_model(
        self, session, round_, state, judge
    ):
        await _playing(session, round_, state)
        judge.clear()

        await pl.fsm_answer(_Msg(None), session, state)

        assert judge == []

    async def test_a_real_attempt_does_reach_the_model(
        self, session, round_, state, judge
    ):
        """The mirror assertion: with every guard passed, the judge must run —
        otherwise the tests above would also pass on a handler that judges nothing."""
        await _playing(session, round_, state)
        judge.clear()

        await pl.fsm_answer(_Msg("Wolfenstein"), session, state)

        assert judge == ["Wolfenstein"]


class TestCooldown:
    async def test_a_throttled_message_does_not_spend_an_attempt(
        self, session, round_, state, monkeypatch
    ):
        monkeypatch.setattr(pl.settings, "guess_answer_cooldown_seconds", 60)
        await _playing(session, round_, state)
        await pl.fsm_answer(_Msg("Quake"), session, state)

        await pl.fsm_answer(_Msg("Wolfenstein"), session, state)

        assert await gs.attempts_left(session, round_, 7) == 2

    async def test_admins_are_not_exempt(self, session, round_, state, monkeypatch):
        """A game is a game: an admin who can hammer the judge has a better shot
        at the podium than everyone else."""
        monkeypatch.setattr(pl.settings, "guess_answer_cooldown_seconds", 60)
        monkeypatch.setattr("filters.admin_filter.settings.admin_ids", [7])
        await _playing(session, round_, state)
        await pl.fsm_answer(_Msg("Quake"), session, state)

        await pl.fsm_answer(_Msg("Wolfenstein"), session, state)

        assert await gs.attempts_left(session, round_, 7) == 2


class TestQuit:
    async def test_quitting_clears_the_state(self, session, round_, state):
        await _playing(session, round_, state)

        class _Cb:
            def __init__(self) -> None:
                self.message = _Msg()
                self.answered = 0

            async def answer(self, *a, **kw):
                self.answered += 1

        cb = _Cb()
        await pl.cb_quit(cb, state)

        assert await state.get_state() is None and cb.answered == 1

    async def test_quitting_does_not_stop_the_clock(self, session, round_, state):
        """Otherwise «esci» is a pause button and the time limit means nothing."""
        round_.time_limit_seconds = 600
        await session.flush()
        await _playing(session, round_, state)
        started = (await gs.get_session(session, round_.id, 7)).started_at

        class _Cb:
            def __init__(self) -> None:
                self.message = _Msg()

            async def answer(self, *a, **kw):
                pass

        await pl.cb_quit(_Cb(), state)
        await pl.start_guess_session(_Msg(), session, state, round_.id)

        assert (await gs.get_session(session, round_.id, 7)).started_at == started
