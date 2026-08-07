"""Opening and closing a round — the two orderings that are not interchangeable.

`open_round` announces first and flips the status second: a send that fails
leaves a `ready` round rather than a `running` one nobody was told about.
`close_round` does the reverse — it claims the close as a conditional UPDATE
*before* paying — so two admins closing at once cannot pay the pool twice.

The group announcement deliberately carries no medium and no answer. Posting the
image in the group would move the game into the group, where the answer gets
discussed and private play stops meaning anything. The reveal happens at close,
under the podium, when there is nothing left to spoil.
"""

from __future__ import annotations

import types
from datetime import timedelta

import pytest
from sqlalchemy import select

from database.models import GamePodium, GuessRound, ScheduledTask, Wallet
from handlers.guess import lifecycle as lc
from services import group_registry, schedule_service
from services import guess_service as gs
from services.guess_judge import Verdict


class _Bot:
    id = 999

    def __init__(self, *, fail_group: bool = False) -> None:
        self.fail_group = fail_group
        self.messages: list[tuple[int, str, dict]] = []
        self.media: list[tuple[int, str]] = []

    async def get_me(self):
        return types.SimpleNamespace(username="testbot")

    async def send_message(self, chat_id, text, **kw):
        if self.fail_group:
            raise RuntimeError("bot is not a member of the group")
        self.messages.append((chat_id, text, kw))

    async def send_photo(self, chat_id, file_id, **kw):
        self.media.append((chat_id, file_id))

    async def send_audio(self, chat_id, file_id, **kw):
        self.media.append((chat_id, file_id))

    async def send_voice(self, chat_id, file_id, **kw):
        self.media.append((chat_id, file_id))

    @property
    def texts(self) -> str:
        return "\n".join(t for _, t, _ in self.messages)


@pytest.fixture(autouse=True)
def _group():
    group_registry.set_runtime_group_id(-100_123)
    yield
    group_registry.set_runtime_group_id(None)


@pytest.fixture
async def round_(session):
    r = await gs.create_round(
        session, kind="guess", creator_tg_id=1, title="Indovina",
        media_file_id="FILE", media_kind="photo", answer="Doom",
        aliases=[], hints=[], max_attempts=5, time_limit_seconds=0,
        prize_first=100, prize_second=50, prize_third=25, prize_consolation=10,
        group_id=-100_123,
    )
    r.status = "ready"
    await session.flush()
    return r


async def _solve(session, round_, uid, user_factory, wrong_before=0):
    await user_factory(uid, f"u{uid}")
    await gs.start_or_resume(session, round_.id, uid)
    for _ in range(wrong_before):
        await gs.record_attempt(session, round_, uid, "Quake",
                                Verdict(correct=False, source="ai"))
    await gs.record_attempt(session, round_, uid, "Doom",
                            Verdict(correct=True, source="exact"))


async def _coins(session, tg_id: int) -> int:
    return (
        await session.execute(select(Wallet.coins).where(Wallet.tg_id == tg_id))
    ).scalar_one()


async def _status(session, round_id: int) -> str:
    return (
        await session.execute(select(GuessRound.status).where(GuessRound.id == round_id))
    ).scalar_one()


class TestOpen:
    async def test_it_announces_and_then_runs(self, session, round_):
        ok, _ = await lc.open_round(_Bot(), session, round_.id)
        await session.commit()

        assert ok is True and await _status(session, round_.id) == "running"

    async def test_the_announcement_carries_a_play_deep_link(self, session, round_):
        bot = _Bot()

        await lc.open_round(bot, session, round_.id)

        kb = bot.messages[0][2]["reply_markup"]
        assert "start=guess_" in kb.inline_keyboard[0][0].url

    async def test_a_sound_round_links_with_its_own_payload(self, session):
        r = await gs.create_round(
            session, kind="sound", creator_tg_id=1, title="Ascolta",
            media_file_id="A", media_kind="audio", answer="Doom",
            aliases=[], hints=[], max_attempts=3, time_limit_seconds=0,
        )
        r.status = "ready"
        await session.flush()
        bot = _Bot()

        await lc.open_round(bot, session, r.id)

        kb = bot.messages[0][2]["reply_markup"]
        assert "start=sound_" in kb.inline_keyboard[0][0].url

    async def test_the_announcement_does_not_reveal_the_medium(self, session, round_):
        """Posting the image in the group moves the game into the group."""
        bot = _Bot()

        await lc.open_round(bot, session, round_.id)

        assert bot.media == []

    async def test_the_announcement_does_not_reveal_the_answer(self, session, round_):
        bot = _Bot()

        await lc.open_round(bot, session, round_.id)

        assert "Doom" not in bot.texts

    async def test_a_failed_announcement_leaves_the_round_ready(self, session, round_):
        """Otherwise the round is running and nobody was told."""
        ok, _ = await lc.open_round(_Bot(fail_group=True), session, round_.id)

        assert ok is False and await _status(session, round_.id) == "ready"

    async def test_a_running_round_cannot_be_opened_again(self, session, round_):
        await lc.open_round(_Bot(), session, round_.id)
        await session.commit()

        ok, msg = await lc.open_round(_Bot(), session, round_.id)

        assert ok is False and "corso" in msg

    async def test_a_finished_round_cannot_be_reopened(self, session, round_):
        round_.status = "finished"
        await session.flush()

        ok, msg = await lc.open_round(_Bot(), session, round_.id)

        assert ok is False and "giocato" in msg

    async def test_a_past_absolute_close_refuses_the_open(self, session, round_):
        """An absolute close is fixed at creation; starting the round after that
        instant would arm the auto-close in the past. Refuse before announcing —
        not schedule a task that fires immediately."""
        round_.closes_at = gs.now() - timedelta(minutes=1)
        await session.flush()
        bot = _Bot()

        ok, msg = await lc.open_round(bot, session, round_.id)

        assert ok is False and "passat" in msg.lower()
        assert bot.messages == [], "must refuse before the announcement is sent"
        assert await _status(session, round_.id) == "ready"

    async def test_a_missing_round_is_reported_not_raised(self, session):
        ok, _ = await lc.open_round(_Bot(), session, 999)
        assert ok is False

    async def test_with_no_group_configured_it_refuses(self, session, round_):
        group_registry.set_runtime_group_id(0)

        ok, msg = await lc.open_round(_Bot(), session, round_.id)

        assert ok is False and "GROUP_ID" in msg


class TestTheAutoClose:
    """A round used to run forever.

    `guess_type.execute_scheduled` has always had an `action == "close"` branch,
    and STEERING §19.b has always documented it — but nothing ever created that
    task, so the branch was unreachable and every round stayed `running` until an
    admin remembered it. Players sat in rounds that were over for them and never
    closed for anyone.

    The task reuses `task_type = kind` with an action payload, exactly like the
    betting window's auto-lock. No new task type.
    """

    async def _pending(self, session, round_id: int) -> list[ScheduledTask]:
        return list((await session.execute(
            select(ScheduledTask).where(
                ScheduledTask.ref_id == round_id,
                ScheduledTask.status == "pending",
            )
        )).scalars().all())

    async def test_opening_a_round_schedules_its_own_close(self, session, round_):
        round_.round_duration_seconds = 600
        await session.flush()

        await lc.open_round(_Bot(), session, round_.id)

        tasks = await self._pending(session, round_.id)
        assert len(tasks) == 1
        assert tasks[0].task_type == "guess", "reuses the kind, no new task type"
        assert schedule_service.task_payload(tasks[0]) == {"action": "close"}

    async def test_the_close_lands_after_the_round_duration(self, session, round_):
        round_.round_duration_seconds = 600
        await session.flush()

        await lc.open_round(_Bot(), session, round_.id)

        task = (await self._pending(session, round_.id))[0]
        delay = (task.run_at - gs.now()).total_seconds()
        assert 590 <= delay <= 610

    async def test_the_announcement_says_when_the_round_closes(self, session, round_):
        """A deadline nobody is told about is a deadline that ambushes people."""
        round_.round_duration_seconds = 3600
        await session.flush()
        bot = _Bot()

        await lc.open_round(bot, session, round_.id)

        assert "chiude" in bot.texts.lower()

    async def test_a_round_with_no_duration_closes_by_hand(self, session, round_):
        """0 means the admin closes it, and must not leave a task behind."""
        round_.round_duration_seconds = 0
        await session.flush()

        await lc.open_round(_Bot(), session, round_.id)

        assert await self._pending(session, round_.id) == []

    async def test_an_absolute_close_arms_the_task_at_that_instant(
        self, session, round_
    ):
        """A round can auto-close at an admin-picked wall-clock instant instead of
        N seconds after it starts (the relative duration)."""
        target = gs.now() + timedelta(hours=3)
        round_.closes_at = target
        round_.round_duration_seconds = 0
        await session.flush()

        await lc.open_round(_Bot(), session, round_.id)

        task = (await self._pending(session, round_.id))[0]
        assert abs((task.run_at - target).total_seconds()) < 1
        assert schedule_service.task_payload(task) == {"action": "close"}

    async def test_an_absolute_close_wins_over_a_duration(self, session, round_):
        """When both are set the fixed instant wins — the arming must not fall
        back to the relative duration."""
        target = gs.now() + timedelta(hours=5)
        round_.closes_at = target
        round_.round_duration_seconds = 600
        await session.flush()

        await lc.open_round(_Bot(), session, round_.id)

        task = (await self._pending(session, round_.id))[0]
        assert abs((task.run_at - target).total_seconds()) < 1

    async def test_an_absolute_close_is_stated_as_a_date(self, session, round_):
        """The announcement names the day and time, not a "fra …" duration."""
        round_.closes_at = gs.now() + timedelta(days=1)
        await session.flush()
        bot = _Bot()

        await lc.open_round(bot, session, round_.id)

        assert "chiude il" in bot.texts.lower()

    async def test_closing_by_hand_cancels_the_pending_auto_close(
        self, session, round_
    ):
        """Otherwise the scheduler later finds a `finished` round and logs a
        failure for something that went right."""
        round_.round_duration_seconds = 600
        await session.flush()
        await lc.open_round(_Bot(), session, round_.id)

        await lc.close_round(_Bot(), session, round_.id)

        assert await self._pending(session, round_.id) == []


class TestClose:
    async def test_the_podium_is_announced_even_if_the_medium_will_not_send(
        self, session, round_, user_factory
    ):
        """The reveal is a bonus; the podium is the point.

        Both used to sit in one `try`, so a dead `file_id` swallowed the podium
        with it: the prizes were paid and the group was never told who won.
        """
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)

        class _NoMedia(_Bot):
            async def send_photo(self, chat_id, file_id, **kw):
                raise RuntimeError("file_id is dead")

        bot = _NoMedia()
        ok, _ = await lc.close_round(bot, session, round_.id)

        assert ok is True
        assert "PODIO" in bot.texts

    async def test_closing_pays_the_podium(self, session, round_, user_factory):
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)

        ok, _ = await lc.close_round(_Bot(), session, round_.id)

        assert ok is True and await _coins(session, 7) == 100

    async def test_closing_twice_pays_once(self, session, round_, user_factory):
        """The close claim is the guard, not a status read followed by a write."""
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)
        await lc.close_round(_Bot(), session, round_.id)

        ok, msg = await lc.close_round(_Bot(), session, round_.id)

        assert ok is False and "chiuso" in msg
        assert await _coins(session, 7) == 100

    async def test_the_podium_reveals_the_medium_and_the_answer(
        self, session, round_, user_factory
    ):
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)
        bot = _Bot()

        await lc.close_round(bot, session, round_.id)

        assert bot.media and bot.media[0][1] == "FILE"
        assert "Doom" in bot.texts

    async def test_the_podium_shows_the_attempt_count(
        self, session, round_, user_factory
    ):
        """Fewest attempts is what the ranking is *for*; hiding it hides the game."""
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory, wrong_before=2)
        bot = _Bot()

        await lc.close_round(bot, session, round_.id)

        assert "3 tentativi" in bot.texts

    async def test_one_attempt_is_singular(self, session, round_, user_factory):
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)
        bot = _Bot()

        await lc.close_round(bot, session, round_.id)

        assert "1 tentativo" in bot.texts and "1 tentativi" not in bot.texts

    async def test_a_podium_finish_is_recorded_for_the_trophies(
        self, session, round_, user_factory
    ):
        """`kind` is the game_key, so the trophies the engine already declares
        for guess/sound light up with no extra wiring."""
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)

        await lc.close_round(_Bot(), session, round_.id)

        row = (await session.execute(
            select(GamePodium.game_key, GamePodium.rank)
            .where(GamePodium.user_tg_id == 7)
        )).one()
        assert row == ("guess", 1)

    async def test_a_sound_round_records_its_own_game_key(self, session, user_factory):
        r = await gs.create_round(
            session, kind="sound", creator_tg_id=1, title="Ascolta",
            media_file_id="A", media_kind="audio", answer="Doom",
            aliases=[], hints=[], max_attempts=3, time_limit_seconds=0,
            prize_first=10,
        )
        r.status = "running"
        await session.flush()
        await _solve(session, r, 7, user_factory)

        await lc.close_round(_Bot(), session, r.id)

        key = (await session.execute(
            select(GamePodium.game_key).where(GamePodium.user_tg_id == 7)
        )).scalar_one()
        assert key == "sound"

    async def test_closing_a_round_nobody_solved_still_works(
        self, session, round_, user_factory
    ):
        round_.status = "running"
        await session.flush()
        await user_factory(7, "u7")
        await gs.start_or_resume(session, round_.id, 7)
        await gs.record_attempt(session, round_, 7, "Quake",
                                Verdict(correct=False, source="ai"))
        bot = _Bot()

        ok, _ = await lc.close_round(bot, session, round_.id)

        assert ok is True and "nessuno" in bot.texts.lower()

    async def test_even_then_the_answer_is_revealed(self, session, round_):
        """A round that ends unsolved still owes everyone the answer."""
        round_.status = "running"
        await session.flush()
        bot = _Bot()

        await lc.close_round(bot, session, round_.id)

        assert "Doom" in bot.texts

    async def test_a_ready_round_cannot_be_closed(self, session, round_):
        ok, msg = await lc.close_round(_Bot(), session, round_.id)
        assert ok is False and "corso" in msg

    async def test_a_missing_round_is_reported_not_raised(self, session):
        ok, msg = await lc.close_round(_Bot(), session, 999)
        assert ok is False and "non trovato" in msg

    async def test_a_failed_podium_announcement_does_not_undo_the_payout(
        self, session, round_, user_factory
    ):
        """Prizes are committed before the announcement, so a send that fails
        never turns a paid-out round into an error."""
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)

        ok, _ = await lc.close_round(_Bot(fail_group=True), session, round_.id)

        assert ok is True and await _coins(session, 7) == 100

    async def test_with_no_group_the_podium_comes_back_as_text(
        self, session, round_, user_factory
    ):
        """An admin running without a group still gets to see who won."""
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)
        group_registry.set_runtime_group_id(0)

        ok, msg = await lc.close_round(_Bot(), session, round_.id)

        assert ok is True and "Doom" in msg

    async def test_a_round_deleted_between_the_claim_and_the_payout_is_survived(
        self, session, round_, monkeypatch
    ):
        """The claim wins, then the row is gone. Defensive, but the alternative
        is an AttributeError on None in the middle of paying people."""
        round_.status = "running"
        await session.flush()

        async def _vanished(_session, _round_id):
            return None
        monkeypatch.setattr(lc.guess_service, "get_round", _vanished)

        ok, msg = await lc.close_round(_Bot(), session, round_.id)

        assert ok is False and "non trovato" in msg


class TestTrophies:
    """Closing a round runs the trophy engine for everyone it could affect, and
    announces what was unlocked. `guess` and `sound` were forward-declared in
    `progress_service.GAME_LABELS`, so a CSV row is all a trophy needs."""

    @pytest.fixture
    async def podium_trophy(self, session):
        from database.models import Badge

        badge = Badge(
            slug="primo_podio_guess", name="Occhio Clinico",
            description="Podio nel Guess The Game", icon_emoji="👁️",
            category="giochi", rarity="bronze", xp_reward=0,
            condition_type="podium_count", condition_value=1, condition_param="guess",
        )
        session.add(badge)
        await session.flush()
        return badge

    async def test_a_podium_finish_unlocks_its_trophy(
        self, session, round_, user_factory, podium_trophy
    ):
        from database.models import UserBadge

        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)

        await lc.close_round(_Bot(), session, round_.id)

        owned = (await session.execute(
            select(UserBadge.badge_id).where(UserBadge.user_tg_id == 7)
        )).scalars().all()
        assert owned == [podium_trophy.id]

    async def test_the_unlocked_trophy_is_announced_in_the_group(
        self, session, round_, user_factory, podium_trophy
    ):
        bot = _Bot()
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)

        await lc.close_round(bot, session, round_.id)

        assert "Occhio Clinico" in bot.texts


class TestProgressEvents:
    """Closing a round records the same "finished last" / "solved under 30s"
    progress events the quiz does, so guess/sound earn `event_count` trophies too.
    The metric keys are chosen from `round_.kind`, so a sound round records the
    sound ones."""

    async def test_the_last_solver_gets_a_last_place_event(
        self, session, round_, user_factory
    ):
        from services import progress_service as ps

        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)                 # 1 attempt → 1st
        await _solve(session, round_, 8, user_factory, wrong_before=2)  # 3 attempts → last

        await lc.close_round(_Bot(), session, round_.id)

        assert (await ps.event_counts(session, 8)).get(ps.GUESS_LAST_PLACE) == 1
        assert ps.GUESS_LAST_PLACE not in await ps.event_counts(session, 7)

    async def test_a_lone_solver_is_never_last(self, session, round_, user_factory):
        """Last place needs ≥2 solvers — a single winner is first, not last."""
        from services import progress_service as ps

        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)

        await lc.close_round(_Bot(), session, round_.id)

        assert ps.GUESS_LAST_PLACE not in await ps.event_counts(session, 7)

    async def test_a_fast_solve_gets_a_sub30_event(
        self, session, round_, user_factory
    ):
        from services import progress_service as ps

        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)  # solved instantly in tests

        await lc.close_round(_Bot(), session, round_.id)

        assert (await ps.event_counts(session, 7)).get(ps.GUESS_SUB30) == 1

    async def test_a_sound_round_records_the_sound_metric_keys(
        self, session, user_factory
    ):
        from services import progress_service as ps

        r = await gs.create_round(
            session, kind="sound", creator_tg_id=1, title="Ascolta",
            media_file_id="A", media_kind="audio", answer="Doom",
            aliases=[], hints=[], max_attempts=3, time_limit_seconds=0,
            prize_first=10,
        )
        r.status = "running"
        await session.flush()
        await _solve(session, r, 7, user_factory)
        await _solve(session, r, 8, user_factory, wrong_before=1)

        await lc.close_round(_Bot(), session, r.id)

        assert (await ps.event_counts(session, 8)).get(ps.SOUND_LAST_PLACE) == 1
        assert (await ps.event_counts(session, 7)).get(ps.SOUND_SUB30) == 1

    async def test_a_hidden_last_place_trophy_unlocks_at_its_threshold(
        self, session, round_, user_factory
    ):
        """The full chain: the recorded event feeds the `event_count` engine and
        awards the (hidden) trophy — exactly like the `last_trivia_*` ones."""
        from database.models import Badge, UserBadge

        session.add(Badge(
            slug="ultimo_guess_1", name="Schermo Nero",
            description="Arriva ultimo nel Guess The Game", icon_emoji="📴",
            category="guess", rarity="bronze", xp_reward=0, hidden=True,
            condition_type="event_count", condition_value=1,
            condition_param="guess_last_place",
        ))
        round_.status = "running"
        await session.flush()
        await _solve(session, round_, 7, user_factory)
        await _solve(session, round_, 8, user_factory, wrong_before=2)  # last

        await lc.close_round(_Bot(), session, round_.id)

        owned = (await session.execute(
            select(UserBadge.user_tg_id).where(UserBadge.user_tg_id == 8)
        )).scalars().all()
        assert owned == [8]
