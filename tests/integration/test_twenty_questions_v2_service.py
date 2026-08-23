"""Observable v2 lifecycle contracts for Alduino's secret game."""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from database.models import (
    AIGameRewardSettlement,
    AIGameSession,
    AIGameTurn,
    ScheduledTask,
    TwentyQuestionsGame,
)
from services import ai_game_service, schedule_service
from services.twenty_questions_catalog import GameDossier


TARGET = GameDossier(
    "portal_2", "Portal 2", ("portal two",),
    "Puzzle game in prima persona di Valve. Chell usa portali nei laboratori Aperture Science. "
    "GLaDOS e Wheatley sono personaggi centrali e la campagna include anche una modalità cooperativa.",
)


async def _create_v2(session, monkeypatch, **overrides):
    monkeypatch.setattr(ai_game_service.settings, "twentyq_v2_enabled", True, raising=False)
    values = {
        "creator_tg_id": 9,
        "title": "Serata",
        "duration_seconds": 43_200,
        "expires_at": None,
        "max_coins_per_participant": 100,
        "target": TARGET,
    }
    values.update(overrides)
    try:
        return await ai_game_service.create_twenty_questions(session, **values)
    except TypeError as exc:
        pytest.fail(f"v2 creation contract is not implemented: {exc}")


async def test_v2_flag_false_blocks_creation_without_legacy_fallback(session, monkeypatch):
    """Removing the gate would silently create a legacy 20/3 game during rollout."""
    monkeypatch.setattr(ai_game_service.settings, "twentyq_v2_enabled", False, raising=False)

    try:
        await ai_game_service.create_twenty_questions(
            session,
            creator_tg_id=9,
            title="Disabled",
            duration_seconds=43_200,
            expires_at=None,
            max_coins_per_participant=100,
            target=TARGET,
        )
    except Exception as exc:  # the RED also catches the missing typed signature
        assert getattr(exc, "reason", None) == "feature_disabled"
    else:
        pytest.fail("feature-disabled creation created a game")

    assert (await session.execute(select(AIGameSession.id))).scalar_one_or_none() is None


@pytest.mark.parametrize(
    ("duration_seconds", "expires_at", "max_coins_per_participant"),
    (
        (None, None, 100),
        (43_200, datetime(2026, 8, 24, 10, 0), 100),
        (43_200, None, 0),
        (43_200, None, 1_001),
    ),
)
async def test_v2_invalid_policy_creates_no_partial_root_or_settlement(
    session, monkeypatch, duration_seconds, expires_at, max_coins_per_participant,
):
    """Validation after the first insert would leave an undeletable reward policy behind."""
    monkeypatch.setattr(ai_game_service.settings, "twentyq_v2_enabled", True)

    with pytest.raises(RuntimeError) as error:
        await ai_game_service.create_twenty_questions(
            session,
            creator_tg_id=9,
            title="Invalid",
            duration_seconds=duration_seconds,
            expires_at=expires_at,
            max_coins_per_participant=max_coins_per_participant,
            target=TARGET,
        )

    assert getattr(error.value, "reason", None) == "invalid_policy"
    assert (await session.execute(select(AIGameSession.id))).scalar_one_or_none() is None
    assert (await session.execute(select(AIGameRewardSettlement.session_id))).scalar_one_or_none() is None


async def test_v2_create_snapshots_policy_and_start_schedules_relative_expiry(
    session, monkeypatch,
):
    """Dropping either immutable snapshot or the one internal timer breaks settlement later."""
    monkeypatch.setattr(ai_game_service, "has_configured_twenty_questions_provider", lambda: True)
    now = datetime(2026, 8, 23, 10, 0)

    created = await _create_v2(session, monkeypatch)
    settlement = await session.get(AIGameRewardSettlement, created.session_id)
    game = await session.get(TwentyQuestionsGame, created.session_id)

    assert settlement is not None and settlement.status == "pending"
    assert (settlement.policy_version, settlement.max_coins_per_participant) == (2, 100)
    assert game is not None
    assert (game.rules_version, game.question_limit, game.guess_limit) == (2, None, None)
    assert (game.questions_per_user, game.guesses_per_user) == (5, 2)
    assert json.loads(game.aliases_json) == ["portal two"]
    assert json.loads(game.dossier_json) == TARGET.dossier

    started = await ai_game_service.start(session, created.session_id, group_id=-1001, now=now)
    await session.flush()
    assert (started.started, started.reason, started.expires_at) == (
        True, None, now + timedelta(hours=12),
    )
    root = await session.get(AIGameSession, created.session_id)
    assert root is not None and (root.group_id, root.anchor_message_id) == (-1001, None)
    task = (await session.execute(select(ScheduledTask))).scalar_one()
    assert (task.task_type, task.ref_id, task.run_at) == (
        "twentyq", created.session_id, now + timedelta(hours=12),
    )
    assert schedule_service.task_payload(task) == {"action": "expire", "internal": True}


@pytest.mark.parametrize("duration_seconds", (7_200, 21_600, 43_200, 86_400))
async def test_v2_relative_expiry_is_measured_from_the_successful_start(
    session, monkeypatch, duration_seconds,
):
    """Using the creation time would shorten drafts left in the admin hub."""
    monkeypatch.setattr(ai_game_service, "has_configured_twenty_questions_provider", lambda: True)
    now = datetime(2026, 8, 23, 10, 0)
    created = await _create_v2(session, monkeypatch, duration_seconds=duration_seconds)

    started = await ai_game_service.start(session, created.session_id, group_id=-1001, now=now)

    assert started.expires_at == now + timedelta(seconds=duration_seconds)


async def test_v2_start_rejects_unavailable_provider_and_elapsed_absolute_deadline(
    session, monkeypatch,
):
    """A failed preflight or expired draft must leave the ready row untouched."""
    now = datetime(2026, 8, 23, 10, 0)
    waiting = await _create_v2(session, monkeypatch)
    monkeypatch.setattr(ai_game_service, "has_configured_twenty_questions_provider", lambda: False)

    unavailable = await ai_game_service.start(session, waiting.session_id, group_id=-1001, now=now)
    assert (unavailable.started, unavailable.reason) == (False, "providers_unavailable")

    deadline = now - timedelta(seconds=1)
    elapsed = await _create_v2(
        session, monkeypatch, duration_seconds=None, expires_at=deadline,
    )
    monkeypatch.setattr(ai_game_service, "has_configured_twenty_questions_provider", lambda: True)
    refused = await ai_game_service.start(session, elapsed.session_id, group_id=-1001, now=now)
    root = await session.get(AIGameSession, elapsed.session_id)
    assert (refused.started, refused.reason, refused.expires_at) == (
        False, "absolute_expiry_elapsed", None,
    )
    assert root is not None and root.status == "ready" and root.expires_at == deadline


async def test_v2_absolute_expiry_is_immutable_and_schedules_that_exact_deadline(
    session, monkeypatch,
):
    """Replacing an admin's absolute deadline with a duration changes the agreed window."""
    monkeypatch.setattr(ai_game_service, "has_configured_twenty_questions_provider", lambda: True)
    now = datetime(2026, 8, 23, 10, 0)
    deadline = datetime(2026, 8, 24, 18, 30)
    created = await _create_v2(
        session, monkeypatch, duration_seconds=None, expires_at=deadline,
    )

    started = await ai_game_service.start(session, created.session_id, group_id=-1001, now=now)
    root = await session.get(AIGameSession, created.session_id)
    task = (await session.execute(select(ScheduledTask))).scalar_one()

    assert (started.started, started.expires_at, root.expires_at, task.run_at) == (
        True, deadline, deadline, deadline,
    )


async def test_v2_aware_absolute_expiry_is_stored_and_scheduled_as_naive_utc(
    session, monkeypatch,
):
    """Keeping the source offset would compare aware and DB-naive expiry values wrongly."""
    monkeypatch.setattr(ai_game_service, "has_configured_twenty_questions_provider", lambda: True)
    absolute = datetime(2026, 8, 24, 18, 30, tzinfo=timezone(timedelta(hours=2)))
    expected = datetime(2026, 8, 24, 16, 30)
    created = await _create_v2(
        session, monkeypatch, duration_seconds=None, expires_at=absolute,
    )

    draft = await session.get(AIGameSession, created.session_id)
    started = await ai_game_service.start(
        session,
        created.session_id,
        group_id=-1001,
        now=datetime(2026, 8, 23, 12, 0, tzinfo=timezone(timedelta(hours=2))),
    )
    task = (await session.execute(select(ScheduledTask))).scalar_one()

    assert draft is not None and draft.expires_at == expected
    assert (started.started, started.expires_at, task.run_at) == (True, expected, expected)


async def test_v2_aware_start_time_calculates_relative_expiry_in_naive_utc(
    session, monkeypatch,
):
    """Adding a duration before UTC normalization shifts the persisted timer by its offset."""
    monkeypatch.setattr(ai_game_service, "has_configured_twenty_questions_provider", lambda: True)
    created = await _create_v2(session, monkeypatch)
    aware_now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    expected_started = datetime(2026, 8, 23, 10, 0)
    expected_expiry = datetime(2026, 8, 23, 22, 0)

    started = await ai_game_service.start(
        session, created.session_id, group_id=-1001, now=aware_now,
    )
    root = await session.get(AIGameSession, created.session_id)
    task = (await session.execute(select(ScheduledTask))).scalar_one()

    assert root is not None and (root.started_at, root.expires_at) == (
        expected_started, expected_expiry,
    )
    assert (started.expires_at, task.run_at) == (expected_expiry, expected_expiry)


async def test_v2_start_is_idempotent_and_anchor_compare_and_swap_is_lossless(
    session, monkeypatch,
):
    """A duplicate start must not create a second timer or replace a published card."""
    monkeypatch.setattr(ai_game_service, "has_configured_twenty_questions_provider", lambda: True)
    created = await _create_v2(session, monkeypatch)
    now = datetime(2026, 8, 23, 10, 0)
    assert (await ai_game_service.start(
        session, created.session_id, group_id=-1001, now=now,
    )).started
    again = await ai_game_service.start(session, created.session_id, group_id=-1002, now=now)

    assert (again.started, again.reason) == (False, "not_ready")
    assert len((await session.execute(select(ScheduledTask))).scalars().all()) == 1
    assert await ai_game_service.move_anchor_if_current(
        session, created.session_id, expected_message_id=None, new_message_id=70,
    )
    assert not await ai_game_service.move_anchor_if_current(
        session, created.session_id, expected_message_id=None, new_message_id=71,
    )
    assert await ai_game_service.move_anchor_if_current(
        session, created.session_id, expected_message_id=70, new_message_id=72,
    )
    root = await session.get(AIGameSession, created.session_id)
    assert root is not None and root.anchor_message_id == 72


async def test_v2_view_is_bounded_and_reveals_only_finished_answer(session, monkeypatch):
    """Returning live ORM rows or the live answer would leak the secret to presenters."""
    created = await _create_v2(session, monkeypatch)
    for turn_no in range(1, 9):
        session.add(AIGameTurn(
            session_id=created.session_id, turn_no=turn_no, user_tg_id=turn_no % 2 + 1,
            kind="question" if turn_no % 2 else "guess", input_text=f"turn {turn_no}",
            output_json=(
                json.dumps({"verdetto": "si"}) if turn_no % 2
                else '{"correct":false}' if turn_no == 2 else json.dumps({"correct": False})
            ),
        ))
    await session.flush()

    live = await ai_game_service.get_game_view(session, created.session_id, recent_turns=99)
    assert live is not None
    assert (live.participant_count, live.question_count, live.wrong_guess_count) == (2, 4, 4)
    assert [turn.turn_no for turn in live.recent_turns] == [3, 4, 5, 6, 7, 8]
    assert live.revealed_answer is None

    await session.execute(
        AIGameSession.__table__.update()
        .where(AIGameSession.id == created.session_id)
        .values(status="finished")
    )
    finished = await ai_game_service.get_game_view(session, created.session_id)
    assert finished is not None and finished.revealed_answer == "Portal 2"


async def test_v2_delete_only_allows_drafts_and_finished_games_are_archived(session, monkeypatch):
    """Deleting a started v2 game would strand its immutable audit and reward policy."""
    draft = await _create_v2(session, monkeypatch)
    session.add(ScheduledTask(
        task_type="twentyq",
        ref_id=draft.session_id,
        payload_json=json.dumps({"action": "expire", "internal": True}),
        run_at=datetime(2026, 8, 24, 10, 0),
        created_by_tg_id=9,
        group_id=-1001,
        status="pending",
    ))
    await session.flush()
    assert await ai_game_service.delete_game(session, draft.session_id)
    assert await session.get(AIGameRewardSettlement, draft.session_id) is None
    assert not (await session.execute(select(ScheduledTask))).scalars().all()

    started = await _create_v2(session, monkeypatch)
    await session.execute(
        AIGameSession.__table__.update()
        .where(AIGameSession.id == started.session_id)
        .values(status="running")
    )
    assert not await ai_game_service.delete_game(session, started.session_id)
    await session.execute(
        AIGameSession.__table__.update()
        .where(AIGameSession.id == started.session_id)
        .values(status="finished")
    )
    assert await ai_game_service.archive_game(session, started.session_id)
    assert not await ai_game_service.delete_game(session, started.session_id)
    assert started.session_id not in [row.id for row in await ai_game_service.list_manageable(session)]


def _synchronize_start_lifecycle(monkeypatch, sessions) -> None:
    """Force two real PostgreSQL transactions through the pre-CAS read together."""
    barrier = asyncio.Barrier(2)
    for db_session in sessions:
        original_execute = db_session.execute

        async def execute(statement, *args, _original=original_execute, **kwargs):
            result = await _original(statement, *args, **kwargs)
            if getattr(statement, "is_select", False) and "duration_seconds" in str(statement):
                await barrier.wait()
            return result

        monkeypatch.setattr(db_session, "execute", execute)


@pytest.mark.pg
async def test_pg_simultaneous_v2_starts_leave_one_timer_and_one_settlement(
    pg_sessions, monkeypatch,
):
    """Without the ready-to-running CAS, two workers could create duplicate timers."""
    monkeypatch.setattr(ai_game_service.settings, "twentyq_v2_enabled", True)
    monkeypatch.setattr(ai_game_service, "has_configured_twenty_questions_provider", lambda: True)
    async with pg_sessions() as setup:
        created = await ai_game_service.create_twenty_questions(
            setup,
            creator_tg_id=9,
            title="Concorrenza",
            duration_seconds=43_200,
            expires_at=None,
            max_coins_per_participant=100,
            target=TARGET,
        )
        await setup.commit()

    async with pg_sessions() as first, pg_sessions() as second:
        _synchronize_start_lifecycle(monkeypatch, (first, second))

        async def run(db_session, group_id):
            result = await ai_game_service.start(
                db_session,
                created.session_id,
                group_id=group_id,
                now=datetime(2026, 8, 23, 10, 0),
            )
            await db_session.commit()
            return result

        first_result, second_result = await asyncio.gather(run(first, -1001), run(second, -1002))

    async with pg_sessions() as observe:
        root = (await observe.execute(select(AIGameSession).where(
            AIGameSession.id == created.session_id,
        ))).scalar_one()
        settlement = await observe.get(AIGameRewardSettlement, created.session_id)
        tasks = list((await observe.execute(select(ScheduledTask).where(
            ScheduledTask.ref_id == created.session_id,
        ))).scalars())

    assert sorted(result.started for result in (first_result, second_result)) == [False, True]
    assert root.status == "running"
    assert settlement is not None and settlement.status == "pending"
    assert len(tasks) == 1


@pytest.mark.pg
async def test_pg_start_delete_race_leaves_only_a_complete_v2_outcome(pg_sessions, monkeypatch):
    """Deleting settlement between a start read and its CAS must never leave a live orphan."""
    monkeypatch.setattr(ai_game_service.settings, "twentyq_v2_enabled", True)
    monkeypatch.setattr(ai_game_service, "has_configured_twenty_questions_provider", lambda: True)
    async with pg_sessions() as setup:
        created = await ai_game_service.create_twenty_questions(
            setup,
            creator_tg_id=9,
            title="Start o elimina",
            duration_seconds=43_200,
            expires_at=None,
            max_coins_per_participant=100,
            target=TARGET,
        )
        await setup.commit()

    settlement_removed = asyncio.Event()
    start_update_attempted = asyncio.Event()
    async with pg_sessions() as starter, pg_sessions() as deleter:
        original_delete_execute = deleter.execute

        async def delete_execute(statement, *args, **kwargs):
            result = await original_delete_execute(statement, *args, **kwargs)
            table = getattr(statement, "table", None)
            if getattr(table, "name", None) == AIGameRewardSettlement.__tablename__:
                settlement_removed.set()
                await start_update_attempted.wait()
            return result

        original_start_execute = starter.execute

        async def start_execute(statement, *args, **kwargs):
            table = getattr(statement, "table", None)
            if getattr(statement, "is_update", False) and getattr(table, "name", None) == AIGameSession.__tablename__:
                start_update_attempted.set()
            return await original_start_execute(statement, *args, **kwargs)

        monkeypatch.setattr(deleter, "execute", delete_execute)
        monkeypatch.setattr(starter, "execute", start_execute)

        async def run_delete():
            deleted = await ai_game_service.delete_game(deleter, created.session_id)
            await deleter.commit()
            return deleted

        async def run_start():
            await settlement_removed.wait()
            started = await ai_game_service.start(
                starter,
                created.session_id,
                group_id=-1001,
                now=datetime(2026, 8, 23, 10, 0),
            )
            await starter.commit()
            return started

        deleted, started = await asyncio.gather(run_delete(), run_start())

    async with pg_sessions() as observe:
        root = (await observe.execute(select(AIGameSession).where(
            AIGameSession.id == created.session_id,
        ))).scalar_one_or_none()
        settlement = await observe.get(AIGameRewardSettlement, created.session_id)
        tasks = list((await observe.execute(select(ScheduledTask).where(
            ScheduledTask.ref_id == created.session_id,
        ))).scalars())

    if root is None:
        assert deleted and not started.started
        assert settlement is None and tasks == []
    else:
        assert not deleted and started.started and root.status == "running"
        assert settlement is not None and settlement.status == "pending"
        assert len(tasks) == 1
