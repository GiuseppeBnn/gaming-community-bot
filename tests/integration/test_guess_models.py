"""The three guess tables — what the schema itself has to guarantee.

Only the constraints that carry weight are pinned here, and two of them are the
whole anti-cheat story at the storage layer:

  * **one session per (round, user)** — two sessions would mean two clocks and
    two attempt counters for the same player, i.e. a second set of attempts for
    free;
  * **one attempt per (round, user, attempt_no)** — the attempt number is what
    the podium ranks by, so a duplicate would be a player who "solved it in 2"
    twice.

The defaults matter too: a session arriving with `attempts_used = NULL` because
a column had no default would turn every arithmetic on it into a crash.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.models import GuessAttempt, GuessRound, GuessSession


def _round(**kw) -> GuessRound:
    base = dict(
        kind="guess", title="Indovina", creator_tg_id=1, status="ready",
        media_file_id="FILE123", media_kind="photo", answer="GTA San Andreas",
        max_attempts=5, time_limit_seconds=300,
    )
    base.update(kw)
    return GuessRound(**base)


class TestRound:
    async def test_a_minimal_round_gets_sane_defaults(self, session):
        r = _round()
        session.add(r)
        await session.commit()

        assert r.status == "ready"
        assert r.aliases_json is None and r.hints_json is None
        assert (r.prize_first, r.prize_consolation, r.prize_min) == (0, 0, 0)
        assert r.started_at is None and r.finished_at is None

    @pytest.mark.parametrize("kind", ["guess", "sound"])
    async def test_both_kinds_persist(self, session, kind):
        """`kind` is also the trophy `game_key` — the two the engine already
        forward-declares in progress_service.GAME_LABELS."""
        session.add(_round(kind=kind))
        await session.commit()

        got = (await session.execute(select(GuessRound.kind))).scalar_one()
        assert got == kind

    async def test_a_draft_is_the_default_status(self, session):
        """A half-built round must not be startable just because it exists."""
        r = GuessRound(
            kind="guess", title="T", creator_tg_id=1,
            media_file_id="F", media_kind="photo", answer="Doom",
        )
        session.add(r)
        await session.commit()

        assert r.status == "draft"


class TestSession:
    async def test_a_user_cannot_have_two_sessions_on_one_round(self, session):
        """Two sessions = two clocks and two attempt counters for one player."""
        r = _round()
        session.add(r)
        await session.flush()
        session.add(GuessSession(round_id=r.id, user_tg_id=7))
        await session.commit()

        session.add(GuessSession(round_id=r.id, user_tg_id=7))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_two_users_may_each_have_one(self, session):
        r = _round()
        session.add(r)
        await session.flush()
        for uid in (7, 8):
            session.add(GuessSession(round_id=r.id, user_tg_id=uid))
        await session.commit()

        n = (await session.execute(select(GuessSession.id))).scalars().all()
        assert len(n) == 2

    async def test_counters_start_at_zero_not_null(self, session):
        r = _round()
        session.add(r)
        await session.flush()
        s = GuessSession(round_id=r.id, user_tg_id=7)
        session.add(s)
        await session.commit()

        assert s.attempts_used == 0 and s.unverified_count == 0
        assert s.solved_at is None and s.solved_attempts is None and s.solve_ms is None


class TestAttempt:
    async def test_the_attempt_number_is_unique_per_user_and_round(self, session):
        """The podium ranks by attempt count; a duplicate number is a player who
        solved it in 2 twice."""
        r = _round()
        session.add(r)
        await session.flush()
        session.add(GuessAttempt(round_id=r.id, user_tg_id=7, attempt_no=1,
                                 raw_answer="gta", normalized="gta",
                                 verdict="wrong", source="ai"))
        await session.commit()

        session.add(GuessAttempt(round_id=r.id, user_tg_id=7, attempt_no=1,
                                 raw_answer="altro", normalized="altro",
                                 verdict="wrong", source="ai"))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_two_users_may_share_an_attempt_number(self, session):
        r = _round()
        session.add(r)
        await session.flush()
        for uid in (7, 8):
            session.add(GuessAttempt(round_id=r.id, user_tg_id=uid, attempt_no=1,
                                     raw_answer="gta", normalized="gta",
                                     verdict="wrong", source="ai"))
        await session.commit()

        n = (await session.execute(select(GuessAttempt.id))).scalars().all()
        assert len(n) == 2

    async def test_the_same_player_may_submit_the_same_answer_twice(self, session):
        """Only the attempt NUMBER is unique. Re-typing a rejected answer is a
        wasted attempt, not a constraint violation."""
        r = _round()
        session.add(r)
        await session.flush()
        for n in (1, 2):
            session.add(GuessAttempt(round_id=r.id, user_tg_id=7, attempt_no=n,
                                     raw_answer="gta", normalized="gta",
                                     verdict="wrong", source="ai"))
        await session.commit()

        rows = (await session.execute(select(GuessAttempt.id))).scalars().all()
        assert len(rows) == 2
