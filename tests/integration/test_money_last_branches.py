"""The last uncovered branches on the money and gameplay paths.

Everything here is a small guard or a rounding rule that the broad tests never
happen to hit, but that decides where coins end up:

  * **the rounding leftover**. A proportional payout is computed with integer
    division, so the pot never divides exactly. The remainder is not dropped — it
    goes to the largest winning bet. Coins that vanish in a rounding gap are coins
    the bot minted out of existence, and nobody would ever notice;
  * **a second resolve, a second answer, a second tap**. Each has a guard that only
    fires under a race, which means it only fires in production;
  * **the per-user shuffles** (§19). The seed is deterministic per user *and* per
    quiz run, so a player who reloads sees the same order and cannot re-roll into
    an easier one — and two players do not share an order that could be leaked.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import services.bet_service as bet_svc
from database.models import BettingEvent, User, Wallet
from exceptions.economy import EventAlreadySettledError, EventNotFoundError
from services import quiz_service, shop_service, xp_service

ADMIN_ID = 1


async def _event_with_options(session, user_factory) -> BettingEvent:
    await user_factory(tg_id=ADMIN_ID, username="admin")
    event = await bet_svc.create_event(
        session, creator_tg_id=ADMIN_ID, title="Derby", description="chi vince",
        options=[{"label": "Casa"}, {"label": "Trasferta"}],
    )
    await session.commit()
    return (await session.execute(
        select(BettingEvent).where(BettingEvent.id == event.id)
        .options(selectinload(BettingEvent.options))
    )).scalar_one()


async def _coins(session, tg_id: int) -> int:
    return (await session.execute(
        select(Wallet.coins).where(Wallet.tg_id == tg_id)
    )).scalar_one()


class TestPayoutRounding:
    async def test_the_rounding_leftover_goes_to_the_biggest_winner(
        self, session, user_factory
    ):
        """Three winners splitting a pot that does not divide by three: the floor
        payouts leave a remainder, and every coin of it must leave the pot."""
        event = await _event_with_options(session, user_factory)
        winners = [(10, 100), (11, 100), (12, 101)]
        for tg_id, amount in winners:
            await user_factory(tg_id=tg_id, username=f"u{tg_id}", coins=1000)
            await bet_svc.place_bet(session, tg_id, event.id, event.options[0].id, amount)
        await user_factory(tg_id=20, username="perdente", coins=1000)
        await bet_svc.place_bet(session, 20, event.id, event.options[1].id, 100)
        await session.commit()

        result = await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        assert result["total_pot"] == 401
        assert result["total_distributed"] == 401, "no coin may be lost to rounding"
        payouts = {w["tg_id"]: w["payout"] for w in result["winners_data"]}
        assert payouts[12] == max(payouts.values()), \
            "the leftover belongs to the largest stake"

    async def test_every_coin_wagered_ends_up_in_a_wallet(self, session, user_factory):
        """The invariant behind the rule: the pot in equals the payouts out."""
        event = await _event_with_options(session, user_factory)
        for tg_id, amount, option in ((10, 77, 0), (11, 13, 0), (12, 111, 1)):
            await user_factory(tg_id=tg_id, username=f"u{tg_id}", coins=1000)
            await bet_svc.place_bet(
                session, tg_id, event.id, event.options[option].id, amount
            )
        await session.commit()
        before = sum([await _coins(session, t) for t in (10, 11, 12)])

        result = await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        after = sum([await _coins(session, t) for t in (10, 11, 12)])
        assert after - before == result["total_pot"]


class TestSettlementGuards:
    async def test_resolving_a_second_time_is_refused(self, session, user_factory):
        """Two admins tapping «risolvi» at once: the second must not pay the pot
        out again."""
        event = await _event_with_options(session, user_factory)
        await user_factory(tg_id=10, username="u10", coins=1000)
        await bet_svc.place_bet(session, 10, event.id, event.options[0].id, 100)
        await session.commit()
        await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()

        with pytest.raises(EventAlreadySettledError):
            await bet_svc.resolve_event(session, event.id, event.options[0].id)

        assert await _coins(session, 10) == 1000

    async def test_resolving_on_an_option_of_another_event_is_refused(
        self, session, user_factory
    ):
        """The winning option id comes from a button; pointing it at a foreign
        option would settle the event with an empty winner set and keep the pot."""
        event = await _event_with_options(session, user_factory)
        other = await bet_svc.create_event(
            session, creator_tg_id=ADMIN_ID, title="Altra", description="x",
            options=[{"label": "A"}, {"label": "B"}],
        )
        await session.commit()
        other = (await session.execute(
            select(BettingEvent).where(BettingEvent.id == other.id)
            .options(selectinload(BettingEvent.options))
        )).scalar_one()

        with pytest.raises(EventNotFoundError):
            await bet_svc.resolve_event(session, event.id, other.options[0].id)

    async def test_betting_on_an_option_of_another_event_is_refused(
        self, session, user_factory
    ):
        event = await _event_with_options(session, user_factory)
        await user_factory(tg_id=10, username="u10", coins=1000)

        with pytest.raises(EventNotFoundError):
            await bet_svc.place_bet(session, 10, event.id, 999_999, 100)

        assert await _coins(session, 10) == 1000

    async def test_asking_about_no_events_at_all_costs_no_query(self, session):
        assert await bet_svc.get_user_placed_event_ids(session, 10, []) == set()


class TestQuizShuffles:
    async def _quiz(self, session, **flags):
        quiz = await quiz_service.create_quiz(session, ADMIN_ID, "Q", "d", **flags)
        for i in range(6):
            await quiz_service.add_question(
                session, quiz.id, f"Domanda {i}?", [f"a{i}", f"b{i}", f"c{i}"], 0, None
            )
        await session.commit()
        return await quiz_service.get_quiz(session, quiz.id)

    async def test_without_randomisation_everyone_sees_the_catalog_order(self, session):
        quiz = await self._quiz(session)

        order = quiz_service.user_question_order(quiz, 10)

        assert [q.id for q in order] == [q.id for q in quiz.questions]

    async def test_with_randomisation_the_order_is_stable_for_one_player(self, session):
        """A player who closes and reopens the chat must land on the same question,
        not re-roll into a different one."""
        quiz = await self._quiz(session, randomize_questions=True)

        first = [q.id for q in quiz_service.user_question_order(quiz, 10)]
        second = [q.id for q in quiz_service.user_question_order(quiz, 10)]

        assert first == second
        assert first != [q.id for q in quiz.questions]

    async def test_two_players_get_different_orders(self, session):
        quiz = await self._quiz(session, randomize_questions=True)

        a = [q.id for q in quiz_service.user_question_order(quiz, 10)]
        b = [q.id for q in quiz_service.user_question_order(quiz, 11)]

        assert a != b
        assert sorted(a) == sorted(b), "same questions, different order"

    async def test_the_answer_order_follows_the_same_rules(self, session):
        quiz = await self._quiz(session, randomize_answers=True)
        question = quiz.questions[0]

        a = quiz_service.user_option_order(quiz, question, 10)
        again = quiz_service.user_option_order(quiz, question, 10)
        b = quiz_service.user_option_order(quiz, question, 11)

        assert a == again and a != b
        assert sorted(a) == sorted(b)

    async def test_the_answer_order_carries_the_original_indexes(self, session):
        """The index is what gets compared against `correct_option_id`; shuffling
        the labels without carrying it would mark the right answer wrong."""
        quiz = await self._quiz(session, randomize_answers=True)
        question = quiz.questions[0]

        pairs = quiz_service.user_option_order(quiz, question, 10)

        assert sorted(i for i, _ in pairs) == [0, 1, 2]
        assert dict(pairs)[0] == quiz_service.question_options(question)[0]

    async def test_without_randomisation_the_options_keep_their_order(self, session):
        quiz = await self._quiz(session)
        question = quiz.questions[0]

        assert [i for i, _ in quiz_service.user_option_order(quiz, question, 10)] == [0, 1, 2]


class TestQuizServiceGuards:
    async def test_setting_the_status_of_a_deleted_quiz_is_a_no_op(self, session):
        await quiz_service.set_status(session, 999_999, "running")  # must not raise

    async def test_the_podium_of_a_quiz_without_questions_is_empty(self, session):
        quiz = await quiz_service.create_quiz(session, ADMIN_ID, "Vuoto", "d")
        await session.commit()

        assert await quiz_service.podium(session, quiz.id) == []

    async def test_prizes_for_a_deleted_quiz_are_no_prizes(self, session):
        assert await quiz_service.award_prizes(session, 999_999) == []

    async def test_a_flat_pool_is_described_as_a_podium_split(self, session):
        quiz = await quiz_service.create_quiz(session, ADMIN_ID, "Q", "d", prize_coins=300)

        assert "50/30/20" in quiz_service.format_prize_summary(quiz)

    async def test_a_double_tap_that_races_past_the_check_is_not_counted_twice(
        self, session, user_factory, monkeypatch
    ):
        """The existence check and the insert are not atomic; the unique constraint
        is. Losing that race must read as "already answered", not as an error.

        The IntegrityError is injected at the flush, which is exactly the condition
        a real race produces. Written RED first: the branch rolled back and then
        read `question.correct_option_id` off an instance the rollback had expired,
        so the recovery path raised MissingGreenlet instead of recovering.
        """
        from sqlalchemy.exc import IntegrityError

        await user_factory(tg_id=10, username="u10")
        quiz = await quiz_service.create_quiz(session, ADMIN_ID, "Q", "d")
        question = await quiz_service.add_question(
            session, quiz.id, "Domanda?", ["giusta", "sbagliata"], 0, None
        )
        await quiz_service.set_status(session, quiz.id, "running")
        await session.commit()

        real_flush = session.flush
        calls = {"n": 0}

        async def _racing_flush(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("insert", {}, Exception("UNIQUE constraint failed"))
            return await real_flush(*a, **kw)

        monkeypatch.setattr(session, "flush", _racing_flush)

        outcome = await quiz_service.record_answer(
            session, quiz.id, question.id, 10, 0, response_ms=100
        )

        assert outcome is not None
        assert outcome.recorded is False
        assert outcome.is_correct is True


class TestShopServiceGuards:
    async def test_a_corrupt_tag_list_reads_as_no_tags(self, session, user_factory):
        """`active_tags_json` is free-form text in the DB; a bad value must cost the
        user their flair, not their profile screen."""
        await user_factory(tg_id=10, username="u10")
        user = await session.get(User, 10)
        user.active_tags_json = "{non è json"
        await session.commit()

        assert shop_service.active_tag_keys(user) == []
        assert shop_service.render_active_tags(user) == ""

    async def test_a_json_value_that_is_not_a_list_reads_as_no_tags(
        self, session, user_factory
    ):
        await user_factory(tg_id=10, username="u10")
        user = await session.get(User, 10)
        user.active_tags_json = json.dumps({"chiave": "valore"})
        await session.commit()

        assert shop_service.active_tag_keys(user) == []

    async def test_toggling_a_tag_for_an_unknown_user_is_refused(self, session):
        assert await shop_service.toggle_tag(session, 999_999, "qualsiasi", 3) == "notowned"

    async def test_applying_a_cosmetic_to_an_unknown_user_is_a_no_op(self, session):
        """Reachable only if the User row vanished between purchase and apply, but
        raising there would abort a purchase that was already paid for."""
        from services import catalog_loader

        item = next(iter(catalog_loader.get_cosmetics().values()))
        await shop_service.apply_cosmetic(session, 999_999, item)  # must not raise


class TestXpGuards:
    async def test_granting_zero_xp_changes_nothing(self, session, user_factory):
        await user_factory(tg_id=10, username="u10", xp=100)

        result = await xp_service.grant_xp(
            session, 10, 0, xp_service.XpSource.daily, capped=True
        )

        assert result.granted == 0 and result.new_rank is None

    async def test_setting_the_xp_of_an_unknown_user_grants_nothing(self, session):
        assert await xp_service.set_xp(session, 999_999, 50) == 0

    async def test_a_non_positive_airdrop_is_refused(self, session, user_factory):
        await user_factory(tg_id=10, username="u10", xp=100)

        assert await xp_service.airdrop_xp(session, 0) == 0
        assert (await session.execute(
            select(User.xp).where(User.tg_id == 10)
        )).scalar_one() == 100


class TestTransferValidation:
    async def test_an_amount_the_service_rejects_is_reported_to_the_user(
        self, session, user_factory
    ):
        """`transfer` raises ValueError for an amount outside its own bounds; the
        handler has to show that reason rather than a generic failure."""
        from handlers import economy

        await user_factory(tg_id=1, username="mittente", coins=10_000_000)
        await user_factory(tg_id=2, username="mario", coins=0)

        class _Msg:
            def __init__(self, text):
                self.text = text
                self.bot = SimpleNamespace(id=999)
                self.from_user = SimpleNamespace(id=1, username="mittente",
                                                 full_name="Mittente")
                self.chat = SimpleNamespace(id=1, type="private")
                self.answers: list[str] = []

            async def answer(self, text, **kw):
                self.answers.append(text)
                return SimpleNamespace(message_id=1)

            async def reply(self, text, **kw):
                return await self.answer(text)

        message = _Msg("/trasferisci @mario 9999999")

        await economy.cmd_trasferisci(message, session)

        assert message.answers and "⚠️" in message.answers[0]
        assert await _coins(session, 2) == 0


class TestResolveRace:
    async def test_a_resolve_that_loses_the_conditional_update_pays_nobody(
        self, session, user_factory, monkeypatch
    ):
        """`resolve_event` re-reads the status under the lock and *then* settles with
        a conditional UPDATE. The UPDATE is the real guard: if it matches no rows,
        someone else settled the event in between, and this call must raise instead
        of paying a second time.

        The window is forced by making the first `refresh` a no-op, so the status
        the check sees stays `open` while the row is already `resolved` — exactly
        what a concurrent settlement produces. On SQLite two sessions share one
        transaction, so the genuine two-session race is not expressible here.
        """
        event = await _event_with_options(session, user_factory)
        await user_factory(tg_id=10, username="u10", coins=1000)
        await bet_svc.place_bet(session, 10, event.id, event.options[0].id, 100)
        await session.commit()
        await bet_svc.resolve_event(session, event.id, event.options[0].id)
        await session.commit()
        paid = await _coins(session, 10)

        # Put the in-session copy back to `open` and let the first refresh keep it
        # that way; the second (in the failure branch) reads the truth.
        stale = await session.get(BettingEvent, event.id)
        stale.status = "open"
        real_refresh = session.refresh
        calls = {"n": 0}

        async def _lagging_refresh(obj, attrs=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return await real_refresh(obj, attrs, **kw)

        monkeypatch.setattr(session, "refresh", _lagging_refresh)

        with pytest.raises(EventAlreadySettledError):
            await bet_svc.resolve_event(session, event.id, event.options[0].id)

        assert await _coins(session, 10) == paid
