"""Poll service + poll_answer tracking — the pieces the prize/close feature adds.

The money path (pay every voter at close) and the lifecycle guard (`claim_close`)
are the parts that must not misbehave, so they are exercised directly here rather
than only through the hub. The ``poll_answer`` handler is what feeds the payout,
so its accept/ignore rules are pinned too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import PollTemplate, PollVote, ScheduledTask, User
from handlers.event_types.poll_type import close_poll, open_poll
from handlers.poll_vote import on_poll_answer
from services import economy_service, group_registry, poll_service

ADMIN_ID = 1
GROUP_ID = -100_555


def _future() -> datetime:
    """A naive-UTC instant in the future — turns a poll into a *managed* one
    (tracked, auto-closing) rather than a plain fire-and-forget publish."""
    return datetime.now(tz=timezone.utc).replace(tzinfo=None) + timedelta(days=1)


@pytest.fixture
def in_group():
    """A configured group id, restored afterwards (it is module-level state)."""
    group_registry.set_runtime_group_id(GROUP_ID)
    yield GROUP_ID
    group_registry.set_runtime_group_id(None)


class _FakeBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []
        self.polls: list[dict] = []
        self.stopped: list[tuple[int, int]] = []
        self._seq = 0
        self.stop_result = None
        self.fail_send = False
        self.fail_intro = False
        self.fail_stop = False

    async def send_message(self, chat_id, text, **kw):
        if self.fail_intro:
            raise RuntimeError("Bad Request: chat not found")
        self.messages.append((chat_id, text))

    async def send_poll(self, chat_id, question, options, **kw):
        if self.fail_send:
            raise RuntimeError("Bad Request: chat not found")
        self.polls.append({"chat_id": chat_id, "question": question, "options": options})
        self._seq += 1
        return SimpleNamespace(
            message_id=2000 + self._seq,
            chat=SimpleNamespace(id=chat_id),
            poll=SimpleNamespace(id=f"tg{self._seq}"),
        )

    async def stop_poll(self, chat_id, message_id, **kw):
        self.stopped.append((chat_id, message_id))
        if self.fail_stop:
            raise RuntimeError("Bad Request: poll can't be stopped")
        return self.stop_result


def _final(counts: list[tuple[str, int]]):
    """A fake final Poll as returned by stop_poll."""
    return SimpleNamespace(
        total_voter_count=sum(c for _t, c in counts),
        options=[SimpleNamespace(text=t, voter_count=c) for t, c in counts],
    )


def _answer(poll_id: str, user_id: int, option_ids, is_bot=False):
    return SimpleNamespace(
        poll_id=poll_id,
        user=SimpleNamespace(id=user_id, is_bot=is_bot),
        option_ids=option_ids,
    )


async def _status(session, poll_id: int) -> str:
    return (
        await session.execute(select(PollTemplate.status).where(PollTemplate.id == poll_id))
    ).scalar_one()


class _Panel:
    """Minimal message for edit_or_send (render_detail/render_list)."""

    def __init__(self):
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

    @property
    def said(self) -> str:
        return "\n".join(self.texts)

    def callbacks(self) -> list[str]:
        out = []
        for m in self.markups:
            if m is not None:
                out += [b.callback_data for row in m.inline_keyboard for b in row if b.callback_data]
        return out


# ---------------------------------------------------------------------------
# Pure/service helpers
# ---------------------------------------------------------------------------

class TestPrizeSummary:
    async def test_no_prize(self, session):
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        assert not poll_service.has_prize(poll)
        assert poll_service.format_prize_summary(poll) == "nessun premio"

    async def test_both_halves(self, session):
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], prize_coins=25, prize_xp=10
        )
        assert poll_service.has_prize(poll)
        s = poll_service.format_prize_summary(poll)
        assert "25" in s and "10" in s and "votante" in s

    async def test_only_coins(self, session):
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"], prize_coins=5)
        s = poll_service.format_prize_summary(poll)
        assert "5" in s and "XP" not in s

    async def test_negative_amounts_are_clamped(self, session):
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], prize_coins=-9, prize_xp=-1
        )
        assert poll.prize_coins == 0 and poll.prize_xp == 0


class TestListManageable:
    async def test_orders_running_then_ready_then_finished(self, session):
        ready = await poll_service.create_template(session, ADMIN_ID, "ready", ["A", "B"])
        running = await poll_service.create_template(session, ADMIN_ID, "running", ["A", "B"])
        finished = await poll_service.create_template(session, ADMIN_ID, "finished", ["A", "B"])
        await poll_service.mark_running(
            session, running.id, message_id=1, chat_id=GROUP_ID, tg_poll_id="x"
        )
        finished.status = "finished"
        await session.commit()

        order = [p.id for p in await poll_service.list_manageable(session)]
        assert order[0] == running.id
        assert order.index(ready.id) < order.index(finished.id)

    async def test_finished_are_capped(self, session):
        for i in range(15):
            p = await poll_service.create_template(session, ADMIN_ID, f"q{i}", ["A", "B"])
            p.status = "finished"
        await session.commit()
        assert len(await poll_service.list_manageable(session, finished_limit=10)) == 10


class TestClaimClose:
    async def test_only_one_caller_wins(self, session):
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await poll_service.mark_running(
            session, poll.id, message_id=1, chat_id=GROUP_ID, tg_poll_id="x"
        )
        await session.commit()

        assert await poll_service.claim_close(session, poll.id) is None
        assert await poll_service.claim_close(session, poll.id) == "finished"

    async def test_missing_poll(self, session):
        assert await poll_service.claim_close(session, 999) == poll_service.POLL_MISSING


class TestMarkRunningMissing:
    async def test_mark_running_missing_is_a_no_op(self, session):
        # Defensive: no row, no crash.
        await poll_service.mark_running(session, 999, message_id=1, chat_id=1, tg_poll_id="x")


class TestVoterCountCorruptRow:
    async def test_corrupt_option_ids_json_counts_as_empty(self, session):
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        session.add(PollVote(poll_id=poll.id, user_tg_id=7, option_ids_json="not-json"))
        await session.commit()
        assert await poll_service.voter_count(session, poll.id) == 0

    async def test_ready_poll_is_blocked(self, session):
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await session.commit()
        assert await poll_service.claim_close(session, poll.id) == "ready"


class TestDelete:
    async def test_delete_removes_poll_and_votes(self, session):
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        session.add(PollVote(poll_id=poll.id, user_tg_id=7, option_ids_json="[0]"))
        await session.commit()

        assert await poll_service.delete_poll(session, poll.id) is True
        await session.commit()
        assert (await session.execute(select(PollTemplate))).scalars().all() == []
        assert (await session.execute(select(PollVote))).scalars().all() == []

    async def test_delete_missing(self, session):
        assert await poll_service.delete_poll(session, 999) is False


class TestRecordVote:
    async def test_insert_then_update_then_retract(self, session):
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await session.commit()

        await poll_service.record_vote(session, poll.id, 7, [0])
        await poll_service.record_vote(session, poll.id, 7, [1])  # changed mind
        await session.commit()
        assert await poll_service.voter_count(session, poll.id) == 1

        await poll_service.record_vote(session, poll.id, 7, [])  # retracted
        await session.commit()
        assert await poll_service.voter_count(session, poll.id) == 0


class TestPayVoters:
    def test_active_voter_projection_deduplicates_to_one_payout_candidate(self):
        """Would fail if duplicate projected active rows could pay one voter twice."""
        rows = ((10, "[0]"), (10, "[1]"), (11, "[]"))

        assert poll_service._active_voter_ids(rows) == (10,)

    async def test_pays_active_voters_only(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="a")
        await user_factory(tg_id=10, username="v1")
        await user_factory(tg_id=11, username="v2")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], prize_coins=25, prize_xp=10
        )
        session.add(PollVote(poll_id=poll.id, user_tg_id=10, option_ids_json="[0]"))
        session.add(PollVote(poll_id=poll.id, user_tg_id=11, option_ids_json="[]"))  # retracted
        await session.commit()

        paid = await poll_service.pay_voters(session, poll)
        await session.commit()

        assert paid == [10]
        assert await economy_service.get_balance(session, 10) == 25
        assert await economy_service.get_balance(session, 11) == 0

    async def test_no_prize_pays_nobody(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="a")
        await user_factory(tg_id=10, username="v1")
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        session.add(PollVote(poll_id=poll.id, user_tg_id=10, option_ids_json="[0]"))
        await session.commit()

        assert await poll_service.pay_voters(session, poll) == []

    async def test_missing_wallet_is_skipped_not_fatal(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="a")
        # user 99 has NO row/wallet (never upserted) → credit raises, must be skipped.
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], prize_coins=25
        )
        session.add(PollVote(poll_id=poll.id, user_tg_id=99, option_ids_json="[0]"))
        await session.commit()

        assert await poll_service.pay_voters(session, poll) == []

    async def test_missing_wallet_skips_only_that_voter_not_the_remaining_prizes(
        self, session, user_factory,
    ):
        """Would fail if ordered prelocking turned poll payout into global validation."""
        await user_factory(tg_id=ADMIN_ID, username="a")
        await user_factory(tg_id=10, username="valid")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], prize_coins=25, prize_xp=10,
        )
        session.add_all([
            PollVote(poll_id=poll.id, user_tg_id=10, option_ids_json="[0]"),
            PollVote(poll_id=poll.id, user_tg_id=99, option_ids_json="[1]"),
        ])
        await session.commit()

        assert await poll_service.pay_voters(session, poll) == [10]
        await session.commit()

        assert await economy_service.get_balance(session, 10) == 25
        assert (await session.execute(select(User.xp).where(User.tg_id == 10))).scalar_one() == 10


# ---------------------------------------------------------------------------
# poll_answer handler
# ---------------------------------------------------------------------------

class TestPollAnswerHandler:
    async def test_records_a_vote_on_a_running_poll(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="a")
        await user_factory(tg_id=10, username="v")
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await poll_service.mark_running(
            session, poll.id, message_id=1, chat_id=GROUP_ID, tg_poll_id="tgABC"
        )
        await session.commit()

        await on_poll_answer(_answer("tgABC", 10, [1]), session)

        assert await poll_service.voter_count(session, poll.id) == 1

    async def test_ignores_unknown_poll_id(self, session):
        # No poll with this tg id → no crash, nothing recorded.
        await on_poll_answer(_answer("nope", 10, [0]), session)
        assert (await session.execute(select(PollVote))).scalars().all() == []

    async def test_ignores_a_finished_poll(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await poll_service.mark_running(
            session, poll.id, message_id=1, chat_id=GROUP_ID, tg_poll_id="tgDONE"
        )
        poll.status = "finished"
        await session.commit()

        await on_poll_answer(_answer("tgDONE", 10, [0]), session)
        assert (await session.execute(select(PollVote))).scalars().all() == []

    async def test_ignores_bot_and_missing_user(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await poll_service.mark_running(
            session, poll.id, message_id=1, chat_id=GROUP_ID, tg_poll_id="tgBOT"
        )
        await session.commit()

        await on_poll_answer(_answer("tgBOT", 5, [0], is_bot=True), session)
        # user=None (anonymous channel vote)
        await on_poll_answer(
            SimpleNamespace(poll_id="tgBOT", user=None, option_ids=[0]), session
        )
        assert (await session.execute(select(PollVote))).scalars().all() == []


# ---------------------------------------------------------------------------
# open_poll / close_poll edges not covered by the hub specs
# ---------------------------------------------------------------------------

class TestOpenPollEdges:
    async def test_short_managed_poll_folds_everything_into_the_question(
        self, session, user_factory, in_group
    ):
        """Description + prize + close all fit in 300 chars → they go in the poll
        question itself, under the title, with no separate intro message."""
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"],
            description="Vota!", prize_coins=25, prize_xp=10,
            closes_at=datetime.now(tz=timezone.utc).replace(tzinfo=None) + timedelta(days=1),
        )
        await session.commit()
        bot = _FakeBot()

        ok, _msg = await open_poll(bot, session, poll.id)
        await session.commit()

        assert ok
        assert len(bot.polls) == 1
        q = bot.polls[0]["question"]
        assert "Vota!" in q and "Premio" in q and "Si chiude" in q
        assert bot.messages == []  # no separate intro — everything is in the poll
        # The absolute close armed a scheduled auto-close task.
        tasks = (await session.execute(
            select(ScheduledTask).where(ScheduledTask.task_type == "poll")
        )).scalars().all()
        assert any(t.ref_id == poll.id for t in tasks)

    async def test_long_managed_poll_sends_the_info_block_separately(
        self, session, user_factory, in_group
    ):
        """When title + description + prize/close exceed 300, the info block goes as a
        separate message and the poll question keeps only title + description."""
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"],
            description="D" * 280, prize_coins=25, prize_xp=10,
            closes_at=datetime.now(tz=timezone.utc).replace(tzinfo=None) + timedelta(days=1),
        )
        await session.commit()
        bot = _FakeBot()

        ok, _msg = await open_poll(bot, session, poll.id)
        await session.commit()

        assert ok
        assert any(
            "Premio" in text and "Si chiude" in text for _cid, text in bot.messages
        )
        assert len(bot.polls) == 1
        assert "Premio" not in bot.polls[0]["question"]

    async def test_a_past_close_date_refuses_to_start(self, session, user_factory, in_group):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"],
            closes_at=datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
        )
        await session.commit()

        ok, msg = await open_poll(_FakeBot(), session, poll.id)
        assert not ok and "già passata" in msg

    async def test_a_failed_send_leaves_it_ready(self, session, user_factory, in_group):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await session.commit()
        bot = _FakeBot()
        bot.fail_send = True

        ok, _msg = await open_poll(bot, session, poll.id)
        assert not ok
        assert await _status(session, poll.id) == "ready"

    async def test_plain_poll_folds_description_into_the_question(
        self, session, user_factory, in_group
    ):
        """A plain poll (no prize/close) sends no separate intro: the description
        lives inside the poll question, under the title."""
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], description="ctx"
        )
        await session.commit()
        bot = _FakeBot()

        ok, _msg = await open_poll(bot, session, poll.id)
        assert ok
        assert any("ctx" in p["question"] and "Q" in p["question"] for p in bot.polls)
        assert bot.messages == []  # no separate intro message

    async def test_a_finished_poll_cannot_be_restarted(self, session, user_factory, in_group):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        poll.status = "finished"
        await session.commit()

        ok, msg = await open_poll(_FakeBot(), session, poll.id)
        assert not ok and "usato" in msg

    async def test_no_group_configured_refuses(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="a")
        group_registry.set_runtime_group_id(0)
        try:
            poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
            await session.commit()
            ok, msg = await open_poll(_FakeBot(), session, poll.id)
            assert not ok and "GROUP_ID" in msg
        finally:
            group_registry.set_runtime_group_id(None)

    async def test_a_running_poll_cannot_be_opened_again(self, session, user_factory, in_group):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], closes_at=_future()
        )
        await session.commit()
        bot = _FakeBot()
        await open_poll(bot, session, poll.id)
        await session.commit()

        ok, msg = await open_poll(bot, session, poll.id)
        assert not ok and "in corso" in msg

    async def test_managed_intro_failure_is_best_effort(self, session, user_factory, in_group):
        """A long poll sends the info block separately; if that send fails, the poll
        still goes up (the info block is a bonus, not the poll)."""
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"],
            description="D" * 280, prize_coins=25, closes_at=_future(),
        )
        await session.commit()
        bot = _FakeBot()
        bot.fail_intro = True

        ok, _msg = await open_poll(bot, session, poll.id)
        assert ok and await _status(session, poll.id) == "running"

    async def test_managed_send_failure_leaves_it_ready(self, session, user_factory, in_group):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], closes_at=_future()
        )
        await session.commit()
        bot = _FakeBot()
        bot.fail_send = True

        ok, _msg = await open_poll(bot, session, poll.id)
        assert not ok and await _status(session, poll.id) == "ready"


class TestRenderDetail:
    async def test_ready_poll_offers_start_and_delete(self, session):
        from handlers.callbacks import EventCb
        from handlers.event_types.poll_type import PollType

        poll = await poll_service.create_template(
            session, ADMIN_ID, "Meglio?", ["A", "B"], description="ctx", prize_coins=25,
            closes_at=datetime.now(tz=timezone.utc).replace(tzinfo=None) + timedelta(days=1),
        )
        await session.commit()
        panel = _Panel()

        await PollType().render_detail(panel, session, poll.id)

        assert "Meglio?" in panel.said and "ctx" in panel.said
        assert "Chiusura automatica" in panel.said
        cbs = panel.callbacks()
        assert EventCb(action="askstart", task_type="poll", item_id=poll.id).pack() in cbs
        assert EventCb(action="askdel", task_type="poll", item_id=poll.id).pack() in cbs

    async def test_running_poll_offers_close(self, session):
        from handlers.callbacks import EventCb
        from handlers.event_types.poll_type import PollType

        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await poll_service.mark_running(
            session, poll.id, message_id=1, chat_id=GROUP_ID, tg_poll_id="x"
        )
        await session.commit()
        panel = _Panel()

        await PollType().render_detail(panel, session, poll.id)

        cbs = panel.callbacks()
        assert EventCb(action="askclose", task_type="poll", item_id=poll.id).pack() in cbs
        assert EventCb(action="sched_close", task_type="poll", item_id=poll.id).pack() in cbs

    async def test_finished_poll_offers_only_delete(self, session):
        from handlers.callbacks import EventCb
        from handlers.event_types.poll_type import PollType

        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        poll.status = "finished"
        await session.commit()
        panel = _Panel()

        await PollType().render_detail(panel, session, poll.id)

        cbs = panel.callbacks()
        assert EventCb(action="askdel", task_type="poll", item_id=poll.id).pack() in cbs
        assert EventCb(action="askstart", task_type="poll", item_id=poll.id).pack() not in cbs

    async def test_missing_poll_shows_a_notice(self, session):
        from handlers.event_types.poll_type import PollType

        panel = _Panel()
        await PollType().render_detail(panel, session, 999)
        assert "non trovato" in panel.said


class TestDeleteSpec:
    async def test_delete_via_spec(self, session):
        from handlers.event_types.poll_type import PollType

        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await session.commit()

        res = await PollType().delete(session, poll.id)
        assert res.ok
        await session.commit()  # the hub commits after a successful delete
        res2 = await PollType().delete(session, poll.id)
        assert not res2.ok

    async def test_options_of_corrupt_json_is_empty(self, session):
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        poll.options_json = "not-json"
        await session.commit()
        assert poll_service.options_of(poll) == []


class TestClosePollEdges:
    async def test_close_missing(self, session):
        ok, _msg = await close_poll(_FakeBot(), session, 999)
        assert not ok

    async def test_close_announces_a_tie(self, session, user_factory, in_group):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], closes_at=_future()
        )
        await session.commit()
        bot = _FakeBot()
        await open_poll(bot, session, poll.id)
        await session.commit()
        bot.stop_result = _final([("A", 3), ("B", 3)])

        ok, _msg = await close_poll(bot, session, poll.id)

        assert ok
        assert any("Pareggio" in text for _cid, text in bot.messages)

    async def test_close_with_no_votes(self, session, user_factory, in_group):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], closes_at=_future()
        )
        await session.commit()
        bot = _FakeBot()
        await open_poll(bot, session, poll.id)
        await session.commit()
        bot.stop_result = _final([("A", 0), ("B", 0)])

        ok, _msg = await close_poll(bot, session, poll.id)
        assert ok
        assert any("Nessun voto" in text for _cid, text in bot.messages)

    async def test_close_a_prize_poll_with_no_voters_says_so(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], prize_coins=25, closes_at=_future()
        )
        await session.commit()
        bot = _FakeBot()
        await open_poll(bot, session, poll.id)
        await session.commit()
        bot.stop_result = _final([("A", 0), ("B", 0)])

        ok, _msg = await close_poll(bot, session, poll.id)
        assert ok
        assert any("Nessun votante da premiare" in text for _cid, text in bot.messages)

    async def test_a_failing_stop_poll_still_closes_and_pays(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="a")
        await user_factory(tg_id=20, username="v")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], prize_coins=25, closes_at=_future()
        )
        await session.commit()
        bot = _FakeBot()
        await open_poll(bot, session, poll.id)
        await session.commit()
        session.add(PollVote(poll_id=poll.id, user_tg_id=20, option_ids_json="[0]"))
        await session.commit()
        bot.fail_stop = True

        ok, _msg = await close_poll(bot, session, poll.id)

        assert ok
        assert await _status(session, poll.id) == "finished"
        assert await economy_service.get_balance(session, 20) == 25

    async def test_close_dms_each_paid_voter(self, session, user_factory, in_group):
        """Every paid voter gets a private reward notification, like an admin grant."""
        await user_factory(tg_id=ADMIN_ID, username="a")
        await user_factory(tg_id=20, username="v")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], prize_coins=25, prize_xp=10,
            closes_at=_future(),
        )
        await session.commit()
        bot = _FakeBot()
        await open_poll(bot, session, poll.id)
        await session.commit()
        session.add(PollVote(poll_id=poll.id, user_tg_id=20, option_ids_json="[0]"))
        await session.commit()
        bot.stop_result = _final([("A", 1), ("B", 0)])

        ok, _msg = await close_poll(bot, session, poll.id)

        assert ok
        # A message addressed to the voter's own chat id (not the group) = the DM.
        assert any(cid == 20 and "votato" in text for cid, text in bot.messages)

    async def test_a_failing_voter_dm_never_breaks_the_close(
        self, session, user_factory, in_group
    ):
        """A voter who never started the bot makes send_message raise: the payout and
        the close must still succeed."""
        await user_factory(tg_id=ADMIN_ID, username="a")
        await user_factory(tg_id=20, username="v")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], prize_coins=25, closes_at=_future()
        )
        await session.commit()
        bot = _FakeBot()
        await open_poll(bot, session, poll.id)
        await session.commit()
        session.add(PollVote(poll_id=poll.id, user_tg_id=20, option_ids_json="[0]"))
        await session.commit()
        bot.stop_result = _final([("A", 1), ("B", 0)])
        bot.fail_intro = True  # every send_message raises (DM + announce)

        ok, _msg = await close_poll(bot, session, poll.id)
        assert ok
        assert await _status(session, poll.id) == "finished"
        assert await economy_service.get_balance(session, 20) == 25

    async def test_a_failing_announce_still_reports_success(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], closes_at=_future()
        )
        await session.commit()
        bot = _FakeBot()
        await open_poll(bot, session, poll.id)
        await session.commit()
        bot.stop_result = _final([("A", 1), ("B", 0)])
        bot.fail_intro = True  # send_message (the announce) raises

        ok, _msg = await close_poll(bot, session, poll.id)
        assert ok  # a failed announce never turns a paid-out close into an error
        assert await _status(session, poll.id) == "finished"

    async def test_close_without_a_group_returns_the_podium_text(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="a")
        group_registry.set_runtime_group_id(GROUP_ID)
        poll = await poll_service.create_template(
            session, ADMIN_ID, "Q", ["A", "B"], closes_at=_future()
        )
        await session.commit()
        bot = _FakeBot()
        await open_poll(bot, session, poll.id)
        await session.commit()
        bot.stop_result = _final([("A", 1), ("B", 0)])
        # Group vanishes before the close (e.g. bot removed): no group to announce to,
        # so the podium text is returned to the caller instead.
        group_registry.set_runtime_group_id(0)
        try:
            ok, msg = await close_poll(bot, session, poll.id)
            assert ok and "chiuso" in msg
        finally:
            group_registry.set_runtime_group_id(None)

    async def test_closing_an_already_finished_poll_is_refused(
        self, session, user_factory, in_group
    ):
        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await poll_service.mark_running(
            session, poll.id, message_id=1, chat_id=GROUP_ID, tg_poll_id="x"
        )
        await session.commit()
        assert await poll_service.claim_close(session, poll.id) is None  # first close wins
        await session.commit()

        ok, msg = await close_poll(_FakeBot(), session, poll.id)
        assert not ok and "già stato chiuso" in msg


class TestDescribeScheduled:
    async def test_ready_poll_is_describable(self, session):
        from handlers.event_types.poll_type import PollType

        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await session.commit()
        ev = await PollType().describe_scheduled(session, poll.id)
        assert ev is not None and ev.title == "Q"

    async def test_a_running_poll_is_not_describable(self, session):
        from handlers.event_types.poll_type import PollType

        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await poll_service.mark_running(
            session, poll.id, message_id=1, chat_id=GROUP_ID, tg_poll_id="x"
        )
        await session.commit()
        assert await PollType().describe_scheduled(session, poll.id) is None


class TestScheduledStart:
    async def test_scheduled_start_of_a_running_poll_skips(
        self, session, user_factory, in_group
    ):
        from database.models import ScheduledTask
        from handlers.event_types.poll_type import PollType
        from services.schedule_service import TaskSkip

        await user_factory(tg_id=ADMIN_ID, username="a")
        poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
        await poll_service.mark_running(
            session, poll.id, message_id=1, chat_id=GROUP_ID, tg_poll_id="x"
        )
        await session.commit()
        task = ScheduledTask(task_type="poll", ref_id=poll.id, status="pending",
                             created_by_tg_id=ADMIN_ID)

        with pytest.raises(TaskSkip):
            await PollType().execute_scheduled(_FakeBot(), session, task, GROUP_ID)

    async def test_scheduled_start_that_fails_raises(self, session, user_factory):
        from database.models import ScheduledTask
        from handlers.event_types.poll_type import PollType

        # No group configured → open_poll fails → the scheduler must see a hard error.
        group_registry.set_runtime_group_id(0)
        try:
            await user_factory(tg_id=ADMIN_ID, username="a")
            poll = await poll_service.create_template(session, ADMIN_ID, "Q", ["A", "B"])
            await session.commit()
            task = ScheduledTask(task_type="poll", ref_id=poll.id, status="pending",
                                 created_by_tg_id=ADMIN_ID)
            with pytest.raises(RuntimeError):
                await PollType().execute_scheduled(_FakeBot(), session, task, 0)
        finally:
            group_registry.set_runtime_group_id(None)
