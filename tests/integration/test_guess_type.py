"""The two registrations — and the claim the registry makes about itself.

The registry's promise (STEERING §18.2, rule 25) is that a new type appears in
the hub and in the scheduler with **no edits** to `events.py` or `schedule.py`.
The tests below are what turns that from a comment into a fact: they drive the
generic hub callbacks and the generic scheduler dispatch and check the round
actually moved.

`kind` doing triple duty — event-type key, ScheduledTask type, and trophy
game_key — is pinned too. Three strings that happen to match today would
eventually not.
"""

from __future__ import annotations

import types

import pytest
from sqlalchemy import select

from database.models import GuessRound, ScheduledTask
from handlers import event_types
from handlers.callbacks import EventCb
from handlers.event_types.guess_type import GuessType
from services import group_registry
from services import guess_service as gs
from services import schedule_service
from services.guess_judge import Verdict


@pytest.fixture(autouse=True)
def _registry():
    """`register_builtin` is called by `main`, not on import."""
    event_types.clear()
    event_types.register_builtin()
    yield
    event_types.clear()
    event_types.register_builtin()


@pytest.fixture(autouse=True)
def _group():
    group_registry.set_runtime_group_id(-100_123)
    yield
    group_registry.set_runtime_group_id(None)


class _Bot:
    id = 999

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.media: list[str] = []

    async def get_me(self):
        return types.SimpleNamespace(username="testbot")

    async def send_message(self, chat_id, text, **kw):
        self.messages.append(text)

    async def send_photo(self, chat_id, file_id, **kw):
        self.media.append(file_id)

    async def send_audio(self, chat_id, file_id, **kw):
        self.media.append(file_id)


class _Msg:
    """Enough Message for `edit_or_send` to work against."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

    @property
    def said(self) -> str:
        return "\n".join(self.texts)


async def _ready(session, kind: str = "guess", **kw) -> GuessRound:
    fields = dict(
        title="T", answer="Doom", aliases=[], hints=[], max_attempts=3,
        time_limit_seconds=0, prize_first=10, group_id=-100_123,
    )
    fields.update(kw)
    r = await gs.create_round(
        session, kind=kind, creator_tg_id=1,
        media_file_id="F", media_kind="photo" if kind == "guess" else "audio",
        **fields,
    )
    r.status = "ready"
    await session.flush()
    return r


async def _status(session, round_id: int) -> str:
    return (
        await session.execute(select(GuessRound.status).where(GuessRound.id == round_id))
    ).scalar_one()


class TestRegistration:
    @pytest.mark.parametrize("key", ["guess", "sound"])
    def test_both_games_are_registered(self, key):
        assert event_types.get(key) is not None

    def test_they_are_distinct_instances_with_distinct_labels(self):
        guess, sound = event_types.get("guess"), event_types.get("sound")
        assert guess is not sound
        assert guess.hub_label != sound.hub_label

    @pytest.mark.parametrize("key", ["guess", "sound"])
    def test_each_satisfies_the_event_type_contract(self, key):
        assert isinstance(event_types.get(key), event_types.EventType)

    def test_registering_two_more_displaces_nothing(self):
        for key in ("quiz", "poll", "bet"):
            assert event_types.get(key) is not None

    def test_an_unknown_kind_fails_loudly(self):
        """A typo in a callback must not quietly render the wrong game."""
        with pytest.raises(KeyError):
            GuessType(kind="suoni")


class TestListsAreScopedToTheirKind:
    async def test_a_sound_round_does_not_show_up_under_guess(self, session):
        await _ready(session, "sound")

        assert await GuessType(kind="guess").schedulable_items(session) == []

    async def test_a_round_shows_up_under_its_own_kind(self, session):
        r = await _ready(session, "sound")

        items = await GuessType(kind="sound").schedulable_items(session)

        assert [i[0] for i in items] == [r.id]

    async def test_only_ready_rounds_are_schedulable(self, session):
        r = await _ready(session)
        r.status = "running"
        await session.flush()

        assert await GuessType(kind="guess").schedulable_items(session) == []

    async def test_the_list_screen_only_shows_its_own_kind(self, session):
        await _ready(session, "sound", title="Solo audio")
        m = _Msg()

        await GuessType(kind="guess").render_list(m, session)

        assert "Solo audio" not in m.said


class TestListScreen:
    async def test_an_empty_list_invites_creating_one(self, session):
        m = _Msg()

        await GuessType(kind="guess").render_list(m, session)

        assert "Nessun round" in m.said

    async def test_a_round_is_listed_with_its_status(self, session):
        await _ready(session, title="Il Round")
        m = _Msg()

        await GuessType(kind="guess").render_list(m, session)

        assert "Il Round" in m.said and "pronto" in m.said


class TestDetailScreen:
    async def test_it_shows_the_answer_to_the_admin(self, session):
        """An admin who cannot see the answer cannot tell a bad AI verdict from
        a good one. This screen is admin-only at the router level."""
        r = await _ready(session)
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        assert "Doom" in m.said

    async def test_a_missing_round_says_so_instead_of_raising(self, session):
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, 999)

        assert "non trovato" in m.said.lower()

    async def test_a_round_of_the_other_kind_is_not_shown(self, session):
        """`ev:item:guess:<id>` pointing at a sound round is a wrong link, not a
        window into the other game."""
        r = await _ready(session, "sound")
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        assert "non trovato" in m.said.lower()

    async def test_a_running_round_shows_the_rejected_answers(self, session):
        """The only way anyone notices the judge turning down something it should
        have accepted."""
        r = await _ready(session)
        r.status = "running"
        await session.flush()
        await gs.record_attempt(session, r, 7, "DOOOM eternal",
                                Verdict(correct=False, source="ai"))
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        assert "DOOOM eternal" in m.said

    async def test_it_shows_the_hint_thresholds(self, session):
        """The admin has to be able to check a hint is not set past the attempt
        limit without reopening the creation flow."""
        r = await gs.create_round(
            session, kind="guess", creator_tg_id=1, title="T",
            media_file_id="F", media_kind="photo", answer="Doom",
            aliases=[], hints=[(2, "sparatutto")], max_attempts=3,
            time_limit_seconds=0,
        )
        r.status = "ready"
        await session.flush()
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        assert "dopo 2" in m.said

    async def test_it_shows_the_time_limit_when_there_is_one(self, session):
        r = await _ready(session, time_limit_seconds=90)
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        assert "1m 30s" in m.said

    async def test_it_shows_the_start_and_end_timestamps(self, session):
        r = await _ready(session)
        await GuessType(kind="guess").start_now(_Bot(), session, r.id)
        # The spec never commits (STEERING §5); `cb_start_now` does it right
        # after. Without it the transition to `running` is still a pending ORM
        # mutation, and the conditional UPDATE in `claim_close` would find no row.
        await session.commit()
        await GuessType(kind="guess").close_now(_Bot(), session, r.id)
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        assert "Avviato:" in m.said and "Concluso:" in m.said

    async def test_the_screen_is_not_stale_right_after_closing(self, session):
        """The hub re-renders the round the moment it is closed, and an entity
        select is answered from the identity map — so the status written in raw
        SQL has to be synced back onto the instance (STEERING §22 rule 3), or the
        admin closes a round and the screen still says «in corso»."""
        r = await _ready(session)
        await GuessType(kind="guess").start_now(_Bot(), session, r.id)
        await session.commit()
        await GuessType(kind="guess").close_now(_Bot(), session, r.id)
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        assert "concluso" in m.said and "in corso" not in m.said

    async def test_the_screen_is_not_stale_right_after_a_reset(self, session):
        r = await _ready(session)
        r.status = "finished"
        await session.commit()
        await GuessType(kind="guess").reset(session, r.id)
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        assert "pronto" in m.said and "concluso" not in m.said

    async def test_a_ready_round_offers_start_and_schedule(self, session):
        r = await _ready(session)
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        data = [btn.callback_data for row in m.markups[0].inline_keyboard for btn in row]
        assert EventCb(action="askstart", task_type="guess", item_id=r.id).pack() in data
        assert EventCb(action="sched", task_type="guess", item_id=r.id).pack() in data

    async def test_every_impactful_action_goes_through_a_confirmation(self, session):
        """STEERING §18.2: no one-tap launch, close or delete."""
        r = await _ready(session)
        r.status = "running"
        await session.flush()
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        data = [btn.callback_data for row in m.markups[0].inline_keyboard for btn in row]
        assert EventCb(action="askclose", task_type="guess", item_id=r.id).pack() in data
        assert not any(d.startswith("ev:close:") or d.startswith("ev:del:") for d in data)

    async def test_a_finished_round_offers_a_rerun(self, session):
        r = await _ready(session)
        r.status = "finished"
        await session.flush()
        m = _Msg()

        await GuessType(kind="guess").render_detail(m, session, r.id)

        data = [btn.callback_data for row in m.markups[0].inline_keyboard for btn in row]
        assert EventCb(action="askreset", task_type="guess", item_id=r.id).pack() in data


class TestStartAndClose:
    async def test_start_now_opens_the_round(self, session):
        r = await _ready(session)

        res = await GuessType(kind="guess").start_now(_Bot(), session, r.id)
        await session.commit()

        assert res.ok is True and await _status(session, r.id) == "running"

    async def test_start_now_on_a_missing_round_is_an_alert_not_a_crash(self, session):
        res = await GuessType(kind="guess").start_now(_Bot(), session, 999)

        assert res.ok is False and res.alert is True

    async def test_close_now_finishes_the_round(self, session):
        r = await _ready(session)
        await GuessType(kind="guess").start_now(_Bot(), session, r.id)
        await session.commit()

        res = await GuessType(kind="guess").close_now(_Bot(), session, r.id)

        assert res.ok is True and await _status(session, r.id) == "finished"

    async def test_close_now_on_a_ready_round_is_an_alert(self, session):
        r = await _ready(session)

        res = await GuessType(kind="guess").close_now(_Bot(), session, r.id)

        assert res.ok is False and res.alert is True


class TestDeleteAndReset:
    async def test_delete_removes_it(self, session):
        r = await _ready(session)

        res = await GuessType(kind="guess").delete(session, r.id)

        assert res.ok is True and await gs.get_round(session, r.id) is None

    async def test_deleting_a_missing_round_is_an_alert(self, session):
        res = await GuessType(kind="guess").delete(session, 999)

        assert res.ok is False and res.alert is True

    async def test_only_a_finished_round_can_be_reset(self, session):
        r = await _ready(session)

        res = await GuessType(kind="guess").reset(session, r.id)

        assert res.ok is False and res.alert is True

    async def test_a_finished_round_can_be_reset(self, session):
        r = await _ready(session)
        r.status = "finished"
        await session.flush()

        res = await GuessType(kind="guess").reset(session, r.id)

        assert res.ok is True and await _status(session, r.id) == "ready"


class TestCreation:
    async def test_it_enters_the_fsm_with_its_own_kind(self, session):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage

        state = FSMContext(storage=MemoryStorage(),
                           key=StorageKey(bot_id=999, chat_id=1, user_id=1))
        m = _Msg()

        await GuessType(kind="sound").start_creation(m, state, creator_id=42)

        data = await state.get_data()
        assert data["kind"] == "sound" and data["creator_id"] == 42


class TestScheduled:
    async def test_a_due_task_opens_the_round(self, session):
        r = await _ready(session)
        task = await schedule_service.schedule_task(
            session, "guess", schedule_service.utcnow(), 1, -100_123, ref_id=r.id
        )

        await GuessType(kind="guess").execute_scheduled(_Bot(), session, task, -100_123)
        await session.commit()

        assert await _status(session, r.id) == "running"

    async def test_a_round_already_running_is_skipped_not_failed(self, session):
        """An admin who started it by hand before the scheduled time reached the
        intended end state; that is not an error."""
        r = await _ready(session)
        await GuessType(kind="guess").start_now(_Bot(), session, r.id)
        await session.commit()
        task = await schedule_service.schedule_task(
            session, "guess", schedule_service.utcnow(), 1, -100_123, ref_id=r.id
        )

        with pytest.raises(schedule_service.TaskSkip):
            await GuessType(kind="guess").execute_scheduled(
                _Bot(), session, task, -100_123
            )

    async def test_a_task_for_a_missing_round_fails_loudly(self, session):
        task = await schedule_service.schedule_task(
            session, "guess", schedule_service.utcnow(), 1, -100_123, ref_id=999
        )

        with pytest.raises(RuntimeError):
            await GuessType(kind="guess").execute_scheduled(
                _Bot(), session, task, -100_123
            )

    async def test_the_close_action_payload_closes_the_round(self, session):
        """Auto-close reuses the same task_type with an action payload — the same
        pattern the betting window already uses. No new task type."""
        r = await _ready(session)
        await GuessType(kind="guess").start_now(_Bot(), session, r.id)
        await session.commit()
        task = await schedule_service.schedule_task(
            session, "guess", schedule_service.utcnow(), 1, -100_123,
            ref_id=r.id, payload={"action": "close"},
        )

        await GuessType(kind="guess").execute_scheduled(_Bot(), session, task, -100_123)

        assert await _status(session, r.id) == "finished"

    async def test_an_auto_close_on_an_already_closed_round_is_skipped(self, session):
        r = await _ready(session)
        task = await schedule_service.schedule_task(
            session, "guess", schedule_service.utcnow(), 1, -100_123,
            ref_id=r.id, payload={"action": "close"},
        )

        with pytest.raises(schedule_service.TaskSkip):
            await GuessType(kind="guess").execute_scheduled(
                _Bot(), session, task, -100_123
            )

    async def test_deleting_a_round_cancels_its_pending_task(self, session):
        r = await _ready(session)
        await schedule_service.schedule_task(
            session, "guess", schedule_service.utcnow(), 1, -100_123, ref_id=r.id
        )

        await GuessType(kind="guess").delete(session, r.id)

        status = (await session.execute(select(ScheduledTask.status))).scalar_one()
        assert status == "cancelled"

    async def test_deleting_a_guess_round_does_not_cancel_a_sound_task(self, session):
        """`task_type` is the kind; cancelling by ref_id alone would reach across
        games, because their ids are independent sequences."""
        guess_round = await _ready(session, "guess")
        await schedule_service.schedule_task(
            session, "sound", schedule_service.utcnow(), 1, -100_123,
            ref_id=guess_round.id,
        )

        await GuessType(kind="guess").delete(session, guess_round.id)

        status = (await session.execute(select(ScheduledTask.status))).scalar_one()
        assert status == "pending"


class TestTheHubNeededNoEdits:
    """The registry's actual promise: these run through the *generic* hub
    handlers, which have no idea the guess games exist."""

    async def test_the_hub_lists_both_new_types(self):
        from handlers.events import _hub_kb

        data = [btn.callback_data for row in _hub_kb().inline_keyboard for btn in row]

        assert EventCb(action="list", task_type="guess").pack() in data
        assert EventCb(action="list", task_type="sound").pack() in data

    async def test_the_generic_start_callback_starts_a_guess_round(self, session):
        from handlers.events import cb_start_now

        r = await _ready(session)
        cb = EventCb(action="start", task_type="guess", item_id=r.id)

        class _Cb:
            data = cb.pack()
            bot = _Bot()
            message = _Msg()

            async def answer(self, *a, **kw):
                pass

        await cb_start_now(_Cb(), cb, session)

        assert await _status(session, r.id) == "running"

    async def test_the_generic_scheduler_dispatch_reaches_the_spec(self, session):
        """`execute_task` looks the type up in the registry — no if/elif."""
        from handlers.schedule import execute_task

        r = await _ready(session, "sound")
        task = await schedule_service.schedule_task(
            session, "sound", schedule_service.utcnow(), 1, -100_123, ref_id=r.id
        )

        await execute_task(_Bot(), session, task)
        # The spec mutates but never commits (STEERING §5); `scheduler_loop` does
        # it right after. Without it the column read below cannot see the pending
        # ORM change — `autoflush` is off, which is the same §22 lesson inverted.
        await session.commit()

        assert await _status(session, r.id) == "running"
