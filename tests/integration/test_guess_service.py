"""The guess engine — attempts, the solve claim, the standings and the payout.

Three properties carry the whole game, and each has its own class below:

  * **the attempt counter is a budget** — it is spent at submission, before the
    verdict is known, because that is the only accounting a brute-forcer cannot
    argue with. The one exception (an unreachable judge) is refunded, and the
    refund is capped, because an uncapped one voids the budget exactly when the
    AI is down;
  * **the solve is claimed in SQL** — `WHERE solved_at IS NULL`, so a double tap
    on the winning answer ranks and pays once, not twice;
  * **the standings order is the product decision** — fewest attempts first, and
    only on a tie does the clock decide.

Balances are read with `select(Wallet.coins)` and never off an entity: with
`expire_on_commit=False` an entity select can be answered from the identity map
and would show a stale copy (STEERING §22).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from database.models import GuessAttempt, GuessRound, GuessSession, User, Wallet
from services import guess_service as gs
from services.guess_judge import Verdict


def _ok(source: str = "exact") -> Verdict:
    return Verdict(correct=True, source=source)


def _no(source: str = "ai") -> Verdict:
    return Verdict(correct=False, source=source)


def _unverified() -> Verdict:
    return Verdict(correct=False, source="unavailable", verified=False)


@pytest.fixture
async def round_(session):
    r = await gs.create_round(
        session, kind="guess", creator_tg_id=1, title="Indovina",
        media_file_id="F", media_kind="photo", answer="Doom",
        aliases=[], hints=[(3, "È sparatutto"), (4, "Anni 90")],
        max_attempts=5, time_limit_seconds=0,
        prize_first=100, prize_second=50, prize_third=25, prize_consolation=10,
        group_id=None,
    )
    r.status = "running"
    await session.flush()
    return r


async def _coins(session, tg_id: int) -> int:
    return (
        await session.execute(select(Wallet.coins).where(Wallet.tg_id == tg_id))
    ).scalar_one()


async def _solve(session, round_, uid, wrong_before=0):
    await gs.start_or_resume(session, round_.id, uid)
    for _ in range(wrong_before):
        await gs.record_attempt(session, round_, uid, "Quake", _no())
    await gs.record_attempt(session, round_, uid, "Doom", _ok())


class TestCreate:
    async def test_a_new_round_is_a_draft(self, session):
        """A half-built round must not be startable just because it exists."""
        r = await gs.create_round(
            session, kind="sound", creator_tg_id=1, title="T",
            media_file_id="F", media_kind="audio", answer="Doom",
            aliases=[], hints=[], max_attempts=3, time_limit_seconds=0,
        )
        assert r.status == "draft"

    async def test_the_floor_is_derived_not_asked(self, session):
        """One floor rule for every game with a podium — never a fifth question
        in the creation flow."""
        r = await gs.create_round(
            session, kind="guess", creator_tg_id=1, title="T",
            media_file_id="F", media_kind="photo", answer="Doom",
            aliases=[], hints=[], max_attempts=3, time_limit_seconds=0,
            prize_consolation=100,
        )
        assert 0 < r.prize_min <= 100

    async def test_zero_attempts_is_clamped_to_one(self, session):
        """A round nobody may ever answer is not a game."""
        r = await gs.create_round(
            session, kind="guess", creator_tg_id=1, title="T",
            media_file_id="F", media_kind="photo", answer="Doom",
            aliases=[], hints=[], max_attempts=0, time_limit_seconds=-5,
        )
        assert r.max_attempts == 1 and r.time_limit_seconds == 0

    async def test_aliases_and_hints_round_trip(self, session):
        from services.guess_judge import aliases_of

        r = await gs.create_round(
            session, kind="guess", creator_tg_id=1, title="T",
            media_file_id="F", media_kind="photo", answer="Doom",
            aliases=["Doom 1993"], hints=[(2, "sparatutto")],
            max_attempts=3, time_limit_seconds=0,
        )

        assert aliases_of(r) == ["Doom 1993"]
        assert gs.hints_of(r) == [(2, "sparatutto")]


class TestHintsParsing:
    def _round(self, hints_json):
        return GuessRound(id=1, kind="guess", title="T", creator_tg_id=1,
                          media_file_id="F", media_kind="photo", answer="D",
                          hints_json=hints_json)

    def test_no_hints_reads_as_empty(self):
        assert gs.hints_of(self._round(None)) == []

    def test_corrupt_json_reads_as_empty_instead_of_raising(self):
        """A broken hint list must cost the hints, not the whole round."""
        assert gs.hints_of(self._round("{not json")) == []

    def test_valid_json_of_the_wrong_shape_reads_as_empty(self):
        assert gs.hints_of(self._round('{"a": 1}')) == []

    def test_malformed_entries_are_skipped_not_fatal(self):
        got = gs.hints_of(self._round('[{"after": 2, "text": "ok"}, {"nope": 1}]'))
        assert got == [(2, "ok")]

    def test_hints_come_back_ordered(self):
        got = gs.hints_of(self._round('[{"after": 5, "text": "b"}, {"after": 2, "text": "a"}]'))
        assert got == [(2, "a"), (5, "b")]


class TestAttemptBudget:
    async def test_a_wrong_answer_spends_one_attempt(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)

        r = await gs.record_attempt(session, round_, 7, "Quake", _no())

        assert (r.recorded, r.attempt_no, r.attempts_left) == (True, 1, 4)

    async def test_attempts_run_out(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)
        for _ in range(5):
            await gs.record_attempt(session, round_, 7, "Quake", _no())

        assert await gs.attempts_left(session, round_, 7) == 0

    async def test_a_player_who_never_started_has_the_full_budget(self, session, round_):
        assert await gs.attempts_left(session, round_, 7) == 5

    async def test_an_unverified_attempt_is_refunded(self, session, round_):
        """The judge being down is our problem, not the player's."""
        await gs.start_or_resume(session, round_.id, 7)

        r = await gs.record_attempt(session, round_, 7, "Quake", _unverified())

        assert r.attempts_left == 5, "spent, then given back"

    async def test_an_unverified_attempt_NEVER_costs_a_real_attempt(
        self, session, round_, monkeypatch
    ):
        """Not "refunded up to a cap" — never.

        The old rule capped the *refund*, so past the cap our own outage started
        eating the player's budget. With the judge returning 400 on every call
        that is exactly what happened in production: players burned every attempt
        they had without a single answer ever being judged.

        The cap still exists, but it now limits how many un-judged answers we
        ACCEPT (see the test below), not how many we charge for.
        """
        monkeypatch.setattr(gs.settings, "guess_max_unverified", 3)
        await gs.start_or_resume(session, round_.id, 7)
        for _ in range(10):
            await gs.record_attempt(session, round_, 7, "Quake", _unverified())

        assert await gs.attempts_left(session, round_, 7) == 5

    async def test_the_number_of_unjudged_answers_is_capped(
        self, session, round_, monkeypatch
    ):
        """Refunding without a bound would open an unlimited submission channel
        at the exact moment the local exact-match is all that stands between a
        player and brute force. The bound moved here: we stop accepting, rather
        than start charging."""
        monkeypatch.setattr(gs.settings, "guess_max_unverified", 3)
        await gs.start_or_resume(session, round_.id, 7)

        assert await gs.unverified_left(session, round_, 7) == 3
        for _ in range(3):
            await gs.record_attempt(session, round_, 7, "Quake", _unverified())

        assert await gs.unverified_left(session, round_, 7) == 0

    async def test_a_judged_answer_does_not_eat_the_unjudged_allowance(
        self, session, round_, monkeypatch
    ):
        monkeypatch.setattr(gs.settings, "guess_max_unverified", 3)
        await gs.start_or_resume(session, round_.id, 7)

        await gs.record_attempt(session, round_, 7, "Quake", _no())

        assert await gs.unverified_left(session, round_, 7) == 3

    async def test_an_unverified_attempt_is_still_stored(self, session, round_):
        """The row is what bounds brute force even when the counter is refunded."""
        await gs.start_or_resume(session, round_.id, 7)
        await gs.record_attempt(session, round_, 7, "Quake", _unverified())

        stored = (await session.execute(
            select(GuessAttempt.verdict).where(GuessAttempt.round_id == round_.id)
        )).scalars().all()
        assert stored == ["unverified"]

    async def test_attempts_are_per_user(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)
        await gs.start_or_resume(session, round_.id, 8)
        await gs.record_attempt(session, round_, 7, "Quake", _no())

        assert await gs.attempts_left(session, round_, 8) == 5

    async def test_the_raw_answer_is_kept_for_the_admin_audit(self, session, round_):
        await gs.record_attempt(session, round_, 7, "GTA: San Andreas!", _no())

        row = (await session.execute(
            select(GuessAttempt.raw_answer, GuessAttempt.normalized)
        )).one()
        assert row == ("GTA: San Andreas!", "gta san andreas")


class TestTheSolveClaim:
    async def test_the_first_correct_answer_solves_it(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)

        r = await gs.record_attempt(session, round_, 7, "Doom", _ok())

        assert r.solved is True

    async def test_a_second_correct_answer_does_not_solve_it_again(self, session, round_):
        """A double tap must rank and pay once. The claim is a conditional
        UPDATE; the second one loses the race and says so."""
        await gs.start_or_resume(session, round_.id, 7)
        await gs.record_attempt(session, round_, 7, "Doom", _ok())

        again = await gs.record_attempt(session, round_, 7, "Doom", _ok())

        assert again.solved is False

    async def test_the_winning_attempt_number_is_recorded(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)
        await gs.record_attempt(session, round_, 7, "Quake", _no())
        await gs.record_attempt(session, round_, 7, "Doom", _ok())

        solved = (await session.execute(
            select(GuessSession.solved_attempts).where(GuessSession.user_tg_id == 7)
        )).scalar_one()
        assert solved == 2

    async def test_an_unverified_correct_never_claims_the_solve(self, session, round_):
        """"Could not decide" must never be spent as "yes"."""
        await gs.start_or_resume(session, round_.id, 7)

        r = await gs.record_attempt(
            session, round_, 7, "Doom",
            Verdict(correct=True, source="unavailable", verified=False),
        )

        assert r.solved is False

    async def test_a_duplicate_attempt_number_is_reported_not_raised(
        self, session, round_
    ):
        """Two taps landing on the same attempt number: the unique constraint
        catches it, and the rollback that follows must not blow up on a lazy
        load — every value used after it was read before the flush."""
        await gs.start_or_resume(session, round_.id, 7)
        session.add(GuessAttempt(
            round_id=round_.id, user_tg_id=7, attempt_no=1,
            raw_answer="x", normalized="x", verdict="wrong", source="ai",
        ))
        await session.flush()

        r = await gs.record_attempt(session, round_, 7, "Quake", _no())

        assert r.recorded is False


class TestHints:
    async def test_a_hint_arrives_at_its_threshold(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)
        for _ in range(2):
            await gs.record_attempt(session, round_, 7, "Quake", _no())

        third = await gs.record_attempt(session, round_, 7, "Quake", _no())

        assert third.hint == "È sparatutto"

    async def test_no_hint_before_its_threshold(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)

        first = await gs.record_attempt(session, round_, 7, "Quake", _no())

        assert first.hint is None

    async def test_each_hint_arrives_once(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)
        seen = [
            (await gs.record_attempt(session, round_, 7, "Quake", _no())).hint
            for _ in range(5)
        ]

        assert seen == [None, None, "È sparatutto", "Anni 90", None]

    async def test_a_hint_survives_an_unverified_attempt_on_its_threshold(
        self, session, round_
    ):
        """The hint after 3 belongs to the 3rd *judged* answer.

        Keying it to the row number meant an unverified attempt landing on the
        threshold consumed the hint and nobody ever saw it — a hint silently lost
        to an outage the player did not cause.
        """
        await gs.start_or_resume(session, round_.id, 7)
        await gs.record_attempt(session, round_, 7, "Quake", _no())
        await gs.record_attempt(session, round_, 7, "Quake", _no())
        swallowed = await gs.record_attempt(session, round_, 7, "Quake", _unverified())

        third_judged = await gs.record_attempt(session, round_, 7, "Quake", _no())

        assert swallowed.hint is None, "an unjudged answer earns no hint"
        assert third_judged.hint == "È sparatutto"

    async def test_solving_it_does_not_also_deliver_a_hint(self, session, round_):
        """A hint after the win is noise about a question already answered."""
        await gs.start_or_resume(session, round_.id, 7)
        for _ in range(2):
            await gs.record_attempt(session, round_, 7, "Quake", _no())

        won = await gs.record_attempt(session, round_, 7, "Doom", _ok())

        assert won.solved is True and won.hint is None


class TestDeadline:
    async def test_no_limit_means_no_deadline(self, session, round_):
        sess = await gs.start_or_resume(session, round_.id, 7)
        assert gs.deadline(round_, sess) is None

    async def test_the_deadline_is_the_start_plus_the_limit(self, session, round_):
        round_.time_limit_seconds = 120
        sess = await gs.start_or_resume(session, round_.id, 7)

        assert gs.deadline(round_, sess) == sess.started_at + timedelta(seconds=120)

    async def test_resuming_does_not_restart_the_clock(self, session, round_):
        """Otherwise leaving and re-entering is an infinite timer."""
        round_.time_limit_seconds = 120
        started = (await gs.start_or_resume(session, round_.id, 7)).started_at

        again = await gs.start_or_resume(session, round_.id, 7)

        assert again.started_at == started


class TestStandings:
    async def test_fewer_attempts_ranks_higher(self, session, round_):
        await _solve(session, round_, 7, wrong_before=3)
        await _solve(session, round_, 8, wrong_before=0)

        order = [s.user_tg_id for s in await gs.standings(session, round_.id)]

        assert order == [8, 7]

    async def test_on_equal_attempts_the_faster_player_wins(self, session, round_):
        await _solve(session, round_, 7, wrong_before=1)
        await _solve(session, round_, 8, wrong_before=1)
        # Decide the gap explicitly instead of relying on wall-clock ordering.
        await session.execute(update(GuessSession)
                              .where(GuessSession.user_tg_id == 8).values(solve_ms=10))
        await session.execute(update(GuessSession)
                              .where(GuessSession.user_tg_id == 7).values(solve_ms=9999))

        order = [s.user_tg_id for s in await gs.standings(session, round_.id)]

        assert order == [8, 7]

    async def test_a_player_who_never_solved_it_is_not_ranked(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)
        await gs.record_attempt(session, round_, 7, "Quake", _no())

        assert await gs.standings(session, round_.id) == []

    async def test_an_empty_round_has_empty_standings(self, session, round_):
        assert await gs.standings(session, round_.id) == []


class TestPrizes:
    async def test_the_podium_is_paid_in_order(self, session, round_, user_factory):
        for i, uid in enumerate((7, 8, 9)):
            await user_factory(uid, f"u{uid}")
            await _solve(session, round_, uid, wrong_before=i)

        await gs.award_prizes(session, round_.id)
        await session.commit()

        assert await _coins(session, 7) == 100
        assert await _coins(session, 8) == 50
        assert await _coins(session, 9) == 25

    async def test_solvers_below_the_podium_get_a_consolation(
        self, session, round_, user_factory
    ):
        for i, uid in enumerate((7, 8, 9, 10)):
            await user_factory(uid, f"u{uid}")
            await _solve(session, round_, uid, wrong_before=i)

        await gs.award_prizes(session, round_.id)
        await session.commit()

        assert await _coins(session, 10) == 10

    async def test_a_player_who_ran_out_of_attempts_gets_no_coins(
        self, session, round_, user_factory
    ):
        """Here "finisher" means "guessed it" — that is what makes fewer attempts
        worth something."""
        await user_factory(7, "u7")
        await user_factory(8, "u8")
        await _solve(session, round_, 7)
        await gs.start_or_resume(session, round_.id, 8)
        for _ in range(5):
            await gs.record_attempt(session, round_, 8, "Quake", _no())

        await gs.award_prizes(session, round_.id)
        await session.commit()

        assert await _coins(session, 8) == 0

    async def test_everyone_who_played_gets_xp(self, session, round_, user_factory):
        await user_factory(7, "u7")
        await user_factory(8, "u8")
        await _solve(session, round_, 7)
        await gs.start_or_resume(session, round_.id, 8)
        await gs.record_attempt(session, round_, 8, "Quake", _no())

        await gs.award_prizes(session, round_.id)
        await session.commit()

        xp = (await session.execute(select(User.xp).where(User.tg_id == 8))).scalar_one()
        assert xp > 0, "showing up pays XP even when you never got it"

    async def test_solving_pays_more_xp_than_just_playing(
        self, session, round_, user_factory
    ):
        await user_factory(7, "u7")
        await user_factory(8, "u8")
        await _solve(session, round_, 7)
        await gs.start_or_resume(session, round_.id, 8)
        await gs.record_attempt(session, round_, 8, "Quake", _no())

        await gs.award_prizes(session, round_.id)
        await session.commit()

        rows = dict((await session.execute(select(User.tg_id, User.xp))).all())
        assert rows[7] > rows[8]

    async def test_xp_reaches_players_even_when_nobody_solved_it(
        self, session, round_, user_factory
    ):
        await user_factory(7, "u7")
        await gs.start_or_resume(session, round_.id, 7)
        await gs.record_attempt(session, round_, 7, "Quake", _no())

        awards = await gs.award_prizes(session, round_.id)
        await session.commit()

        xp = (await session.execute(select(User.xp).where(User.tg_id == 7))).scalar_one()
        assert awards == [] and xp > 0

    async def test_awarding_on_a_missing_round_is_empty_not_a_crash(self, session):
        assert await gs.award_prizes(session, 999) == []


class TestClaimClose:
    async def test_the_first_close_wins_and_the_second_is_told(self, session, round_):
        assert await gs.claim_close(session, round_.id) is None
        assert await gs.claim_close(session, round_.id) == "finished"

    async def test_a_missing_round_says_so(self, session):
        assert await gs.claim_close(session, 999) == gs.ROUND_MISSING

    async def test_a_ready_round_cannot_be_closed(self, session, round_):
        round_.status = "ready"
        await session.flush()

        assert await gs.claim_close(session, round_.id) == "ready"


class TestListing:
    async def test_only_ready_rounds_are_schedulable(self, session, round_):
        assert await gs.list_ready(session, "guess") == []

    async def test_a_ready_round_is_listed(self, session, round_):
        round_.status = "ready"
        await session.flush()

        assert [r.id for r in await gs.list_ready(session, "guess")] == [round_.id]

    async def test_listings_are_scoped_to_their_kind(self, session, round_):
        """A Sound Quest round in the Guess The Game list is a wrong game
        started by mistake."""
        round_.status = "ready"
        await session.flush()

        assert await gs.list_ready(session, "sound") == []

    async def test_manageable_shows_running_before_ready(self, session, round_):
        ready = await gs.create_round(
            session, kind="guess", creator_tg_id=1, title="Pronto",
            media_file_id="F", media_kind="photo", answer="X",
            aliases=[], hints=[], max_attempts=3, time_limit_seconds=0,
        )
        ready.status = "ready"
        await session.flush()

        got = [r.status for r in await gs.list_manageable(session, "guess")]

        assert got[0] == "running"

    async def test_manageable_hides_drafts(self, session):
        """A draft has no media or answer yet; showing it invites starting it."""
        await gs.create_round(
            session, kind="guess", creator_tg_id=1, title="Bozza",
            media_file_id="F", media_kind="photo", answer="X",
            aliases=[], hints=[], max_attempts=3, time_limit_seconds=0,
        )

        assert await gs.list_manageable(session, "guess") == []

    async def test_the_finished_archive_is_capped(self, session):
        for i in range(4):
            r = await gs.create_round(
                session, kind="guess", creator_tg_id=1, title=f"R{i}",
                media_file_id="F", media_kind="photo", answer="X",
                aliases=[], hints=[], max_attempts=3, time_limit_seconds=0,
            )
            r.status = "finished"
        await session.flush()

        got = await gs.list_manageable(session, "guess", finished_limit=2)

        assert len(got) == 2


class TestSetStatus:
    async def test_running_stamps_the_start_time_once(self, session, round_):
        round_.status = "ready"
        round_.started_at = None
        await session.flush()

        await gs.set_status(session, round_.id, "running")
        first = round_.started_at
        await gs.set_status(session, round_.id, "running")

        assert first is not None and round_.started_at == first

    async def test_finished_stamps_the_end_time(self, session, round_):
        await gs.set_status(session, round_.id, "finished")
        assert round_.finished_at is not None

    async def test_a_missing_round_is_a_no_op(self, session):
        await gs.set_status(session, 999, "running")


class TestResetAndDelete:
    async def test_reset_wipes_play_data_and_re_arms(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)
        await gs.record_attempt(session, round_, 7, "Quake", _no())
        await gs.claim_close(session, round_.id)

        assert await gs.reset_round(session, round_.id) is True

        status = (await session.execute(
            select(GuessRound.status).where(GuessRound.id == round_.id)
        )).scalar_one()
        left = (await session.execute(
            select(GuessAttempt.id).where(GuessAttempt.round_id == round_.id)
        )).scalars().all()
        sessions = (await session.execute(
            select(GuessSession.id).where(GuessSession.round_id == round_.id)
        )).scalars().all()
        assert status == "ready" and left == [] and sessions == []

    async def test_only_a_finished_round_can_be_reset(self, session, round_):
        """The reset deletes every attempt; the status check is the only thing
        between a mistap and destroying live play."""
        assert await gs.reset_round(session, round_.id) is False

    async def test_resetting_a_missing_round_is_false_not_a_crash(self, session):
        assert await gs.reset_round(session, 999) is False

    async def test_delete_removes_the_round_and_its_play_data(self, session, round_):
        await gs.start_or_resume(session, round_.id, 7)
        await gs.record_attempt(session, round_, 7, "Quake", _no())

        assert await gs.delete_round(session, round_.id) is True

        assert await gs.get_round(session, round_.id) is None
        left = (await session.execute(
            select(GuessAttempt.id).where(GuessAttempt.round_id == round_.id)
        )).scalars().all()
        assert left == []

    async def test_deleting_a_missing_round_is_false_not_a_crash(self, session):
        assert await gs.delete_round(session, 999) is False


class TestTheRejectedAudit:
    """The list an admin scans to notice the judge turning down something it
    should have accepted. Without it a bad verdict is invisible: the player just
    loses and says nothing."""

    async def test_it_lists_the_rejected_answers_most_recent_first(
        self, session, round_
    ):
        for answer in ("Quake", "Wolfenstein"):
            await gs.record_attempt(session, round_, 7, answer, _no())

        assert await gs.recent_rejected(session, round_.id) == ["Wolfenstein", "Quake"]

    async def test_correct_answers_are_not_in_it(self, session, round_):
        await gs.record_attempt(session, round_, 7, "Doom", _ok())

        assert await gs.recent_rejected(session, round_.id) == []

    async def test_unverified_answers_are_not_in_it(self, session, round_):
        """They were never judged, so they say nothing about the judge."""
        await gs.record_attempt(session, round_, 7, "Boh", _unverified())

        assert await gs.recent_rejected(session, round_.id) == []

    async def test_the_same_wrong_answer_from_many_players_is_one_line(
        self, session, round_
    ):
        """Ten people typing the same wrong title is one fact, not ten."""
        for uid in (7, 8, 9):
            await gs.record_attempt(session, round_, uid, "Quake", _no())

        assert await gs.recent_rejected(session, round_.id) == ["Quake"]

    async def test_it_is_capped(self, session, round_):
        for i, uid in enumerate(range(100, 110)):
            await gs.record_attempt(session, round_, uid, f"Gioco {i}", _no())

        assert len(await gs.recent_rejected(session, round_.id, limit=3)) == 3


class TestPlayStats:
    async def test_it_counts_players_and_attempts(self, session, round_):
        await gs.record_attempt(session, round_, 7, "Quake", _no())
        await gs.record_attempt(session, round_, 7, "Doom", _ok())
        await gs.record_attempt(session, round_, 8, "Quake", _no())

        assert await gs.play_stats(session, round_.id) == (2, 3)

    async def test_an_untouched_round_is_all_zeros(self, session, round_):
        assert await gs.play_stats(session, round_.id) == (0, 0)


class TestPrizeSummary:
    async def test_a_round_with_no_prizes_says_so(self, session):
        r = await gs.create_round(
            session, kind="guess", creator_tg_id=1, title="T",
            media_file_id="F", media_kind="photo", answer="X",
            aliases=[], hints=[], max_attempts=3, time_limit_seconds=0,
        )
        assert gs.format_prize_summary(r) == "nessun premio"

    async def test_it_lists_every_rank_that_pays(self, session, round_):
        got = gs.format_prize_summary(round_)
        assert "100" in got and "50" in got and "25" in got and "10" in got
