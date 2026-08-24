"""Observable v2 lifecycle contracts for Alduino's secret game."""

from __future__ import annotations

import json
import asyncio
import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from database.models import (
    AIGameRewardSettlement,
    AIGameSession,
    AIGameTurn,
    ScheduledTask,
    TwentyQuestionsGame,
)
from services import ai_game_service, schedule_service
from services.ai_game_types import (
    QuestionVerdict,
    TurnKind,
    TurnOutcome,
    TurnRejectReason,
)
from services.structured_ai import StructuredAIError
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


async def _running_v2(
    session,
    monkeypatch,
    *,
    now: datetime = datetime(2030, 8, 23, 10, 0),
    **overrides,
) -> int:
    """Create and persist a running v2 game for authoritative turn tests."""
    monkeypatch.setattr(
        ai_game_service, "has_configured_twenty_questions_provider", lambda: True,
    )
    created = await _create_v2(session, monkeypatch, **overrides)
    started = await ai_game_service.start(
        session, created.session_id, group_id=-1001, now=now,
    )
    assert started.started
    await session.commit()
    return created.session_id


async def _record_yes(
    session, session_id: int, user_tg_id: int, number: int,
) -> object:
    """Persist one distinct question through the short-claim/complete protocol."""
    started = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=user_tg_id,
        question=f"La domanda {number} dell'utente {user_tg_id} riguarda il gameplay?",
    )
    assert started.outcome is TurnOutcome.claimed
    assert started.claim is not None
    await session.commit()
    result = await ai_game_service.complete_question(
        session, claim=started.claim, verdict=QuestionVerdict.si,
    )
    await session.commit()
    return result


async def _turn_count(session, session_id: int) -> int:
    return len((await session.execute(select(AIGameTurn.id).where(
        AIGameTurn.session_id == session_id,
    ))).scalars().all())


async def _pending_fields(session, session_id: int) -> tuple[object, ...]:
    return (await session.execute(select(
        AIGameSession.pending_token,
        AIGameSession.pending_since,
        AIGameSession.pending_user_tg_id,
        AIGameSession.pending_kind,
    ).where(AIGameSession.id == session_id))).one()


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


async def test_v2_questions_and_guesses_use_personal_ledger_quotas(session, monkeypatch):
    """Replacing per-user ledger counts with v1 globals would deny the second player."""
    session_id = await _running_v2(session, monkeypatch)

    for user_tg_id in (10, 20):
        for number in range(5):
            recorded = await _record_yes(session, session_id, user_tg_id, number)
            assert recorded.outcome is TurnOutcome.recorded

        sixth = await ai_game_service.begin_question(
            session,
            session_id=session_id,
            user_tg_id=user_tg_id,
            question=f"La sesta domanda dell'utente {user_tg_id} riguarda i livelli?",
        )
        assert (sixth.outcome, sixth.reason, sixth.quota.questions_left) == (
            TurnOutcome.rejected, TurnRejectReason.question_quota, 0,
        )

        for number in range(2):
            guess = await ai_game_service.submit_guess(
                session,
                session_id=session_id,
                user_tg_id=user_tg_id,
                answer=f"Risposta errata {user_tg_id}-{number}",
            )
            assert (guess.outcome, guess.correct) == (TurnOutcome.recorded, False)
            await session.commit()

        third = await ai_game_service.submit_guess(
            session,
            session_id=session_id,
            user_tg_id=user_tg_id,
            answer=f"Terza risposta errata {user_tg_id}",
        )
        assert (third.outcome, third.reason, third.quota.guesses_left) == (
            TurnOutcome.rejected, TurnRejectReason.guess_quota, 0,
        )

    questions = (await session.execute(select(AIGameTurn.user_tg_id).where(
        AIGameTurn.session_id == session_id,
        AIGameTurn.kind == TurnKind.question.value,
    ))).scalars().all()
    guesses = (await session.execute(select(AIGameTurn.user_tg_id).where(
        AIGameTurn.session_id == session_id,
        AIGameTurn.kind == TurnKind.guess.value,
    ))).scalars().all()
    assert questions.count(10) == questions.count(20) == 5
    assert guesses.count(10) == guesses.count(20) == 2
    assert (await ai_game_service.get_personal_quota(
        session, session_id, 30,
    )).questions_left == 5
    assert (await ai_game_service.get_personal_quota(
        session, session_id, 30,
    )).guesses_left == 2


async def test_v2_duplicate_question_reuses_verdict_and_duplicate_guess_is_free(
    session, monkeypatch,
):
    """Dropping global hash reuse would consume quotas and bill an AI call again."""
    session_id = await _running_v2(session, monkeypatch)
    first = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=10,
        question="È un puzzle in prima persona?",
    )
    assert first.claim is not None
    await session.commit()
    recorded = await ai_game_service.complete_question(
        session, claim=first.claim, verdict=QuestionVerdict.si,
    )
    assert recorded.outcome is TurnOutcome.recorded
    await session.commit()

    reused = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=20,
        question="  è un puzzle in prima persona?! ",
    )
    assert (reused.outcome, reused.cached_verdict, reused.claim) == (
        TurnOutcome.reused, QuestionVerdict.si, None,
    )
    assert reused.quota.questions_used == 0
    assert await _turn_count(session, session_id) == 1
    assert await _pending_fields(session, session_id) == (None, None, None, None)

    wrong = await ai_game_service.submit_guess(
        session,
        session_id=session_id,
        user_tg_id=20,
        answer="Half-Life 2",
    )
    assert (wrong.outcome, wrong.correct, wrong.quota.guesses_used) == (
        TurnOutcome.recorded, False, 1,
    )
    await session.commit()
    duplicate = await ai_game_service.submit_guess(
        session,
        session_id=session_id,
        user_tg_id=10,
        answer=" half-life 2?! ",
    )
    assert (duplicate.outcome, duplicate.reason, duplicate.quota.guesses_used) == (
        TurnOutcome.rejected, TurnRejectReason.duplicate_guess, 0,
    )
    assert await _turn_count(session, session_id) == 2


async def test_v2_invalid_raw_inputs_do_not_hash_claim_or_consume_quota(session, monkeypatch):
    """Validating after a claim could strand a lease or charge empty input."""
    session_id = await _running_v2(session, monkeypatch)

    for raw in ("", " \n\t ", "x" * 501):
        question = await ai_game_service.begin_question(
            session, session_id=session_id, user_tg_id=10, question=raw,
        )
        assert (question.outcome, question.reason) == (
            TurnOutcome.rejected, TurnRejectReason.invalid_input,
        )
        guess = await ai_game_service.submit_guess(
            session, session_id=session_id, user_tg_id=10, answer=raw,
        )
        assert (guess.outcome, guess.reason) == (
            TurnOutcome.rejected, TurnRejectReason.invalid_input,
        )

    quota = await ai_game_service.get_personal_quota(session, session_id, 10)
    assert (quota.questions_left, quota.guesses_left, quota.participant) == (5, 2, False)
    assert await _turn_count(session, session_id) == 0
    assert await _pending_fields(session, session_id) == (None, None, None, None)


async def test_v2_hash_collision_fails_closed_after_rechecking_persisted_raw_input(
    session, monkeypatch,
):
    """Trusting a digest alone would turn a SHA collision into someone else's verdict."""
    monkeypatch.setattr(
        ai_game_service, "normalized_input_hash", lambda _text: "c" * 64, raising=False,
    )
    session_id = await _running_v2(session, monkeypatch)
    first = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=10,
        question="Il gioco ha una modalità cooperativa?",
    )
    assert first.claim is not None
    await session.commit()
    assert (await ai_game_service.complete_question(
        session, claim=first.claim, verdict=QuestionVerdict.si,
    )).outcome is TurnOutcome.recorded
    await session.commit()

    collision = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=20,
        question="Il gioco è ambientato nello spazio?",
    )
    assert (collision.outcome, collision.reason) == (
        TurnOutcome.rejected, TurnRejectReason.hash_collision,
    )
    assert collision.quota.questions_used == 0
    assert await _turn_count(session, session_id) == 1
    assert await _pending_fields(session, session_id) == (None, None, None, None)


async def test_v2_direct_title_and_ai_answer_confirmation_release_without_a_turn(
    session, monkeypatch,
):
    """A title hidden in a question must not spend a question or retain the lease."""
    session_id = await _running_v2(session, monkeypatch)
    direct = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=10,
        question="Il gioco è Portal 2?",
    )
    assert (direct.outcome, direct.reason) == (
        TurnOutcome.rejected, TurnRejectReason.answer_confirmation_required,
    )
    assert await _pending_fields(session, session_id) == (None, None, None, None)

    started = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=10,
        question="Il gioco è un puzzle con una protagonista?",
    )
    assert started.claim is not None
    await session.commit()
    refused = await ai_game_service.complete_question(
        session, claim=started.claim, verdict=QuestionVerdict.usa_risposta,
    )
    assert (refused.outcome, refused.reason) == (
        TurnOutcome.rejected, TurnRejectReason.answer_confirmation_required,
    )
    quota = await ai_game_service.get_personal_quota(session, session_id, 10)
    assert (quota.questions_used, quota.participant) == (0, False)
    assert await _turn_count(session, session_id) == 0
    assert await _pending_fields(session, session_id) == (None, None, None, None)


async def test_v2_question_lease_is_owned_by_token_user_and_kind_and_recovers_after_timeout(
    session, monkeypatch,
):
    """A stale completion must not append after a replacement lease takes ownership."""
    monkeypatch.setattr(ai_game_service.settings, "ai_game_claim_timeout_seconds", 45)
    session_id = await _running_v2(session, monkeypatch)
    now = datetime(2026, 8, 23, 11, 0)
    first = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=10,
        question="La prima richiesta riguarda i portali?",
        now=now,
    )
    assert first.claim is not None and first.claim.kind is TurnKind.question
    assert (await _pending_fields(session, session_id))[2:] == (10, TurnKind.question.value)

    busy = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=20,
        question="Un altro utente prova mentre la lease è viva?",
        now=now + timedelta(seconds=1),
    )
    assert (busy.outcome, busy.reason) == (TurnOutcome.rejected, TurnRejectReason.busy)

    recovered = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=20,
        question="La lease scaduta può essere recuperata?",
        now=now + timedelta(seconds=46),
    )
    assert recovered.claim is not None
    assert (await _pending_fields(session, session_id))[2:] == (20, TurnKind.question.value)
    assert recovered.claim.token != first.claim.token

    stale = await ai_game_service.complete_question(
        session, claim=first.claim, verdict=QuestionVerdict.si,
    )
    assert (stale.outcome, stale.reason) == (TurnOutcome.rejected, TurnRejectReason.lost_claim)
    wrong_owner = await ai_game_service.abandon_claim(
        session,
        claim=replace(recovered.claim, user_tg_id=99),
        reason=TurnRejectReason.providers_unavailable,
    )
    assert (wrong_owner.outcome, wrong_owner.reason) == (
        TurnOutcome.rejected, TurnRejectReason.lost_claim,
    )
    await session.execute(update(AIGameSession).where(
        AIGameSession.id == session_id,
    ).values(pending_kind=TurnKind.guess.value))
    wrong_kind = await ai_game_service.complete_question(
        session, claim=recovered.claim, verdict=QuestionVerdict.si,
    )
    assert (wrong_kind.outcome, wrong_kind.reason) == (
        TurnOutcome.rejected, TurnRejectReason.lost_claim,
    )
    forged_kind = await ai_game_service.abandon_claim(
        session,
        claim=replace(recovered.claim, kind=TurnKind.guess),
        reason=TurnRejectReason.providers_unavailable,
    )
    assert (forged_kind.outcome, forged_kind.reason) == (
        TurnOutcome.rejected, TurnRejectReason.lost_claim,
    )
    assert await _turn_count(session, session_id) == 0


async def test_v2_legacy_release_adapter_cannot_clear_an_owned_typed_lease(
    session, monkeypatch,
):
    """The v1 compatibility adapter must not bypass v2 owner/kind checks."""
    session_id = await _running_v2(session, monkeypatch)
    started = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=10,
        question="La release legacy può svuotare una lease tipizzata?",
    )
    assert started.claim is not None

    await ai_game_service.release_turn(session, session_id, started.claim.token)

    assert (await _pending_fields(session, session_id))[2:] == (10, TurnKind.question.value)


async def test_v2_expiry_precedes_invalid_input_and_late_completion_never_appends(
    session, monkeypatch,
):
    """Resolving expiry after validation or append would create post-deadline turns."""
    started_at = datetime(2026, 8, 23, 10, 0)
    expired_at = started_at + timedelta(seconds=61)
    first_id = await _running_v2(
        session, monkeypatch, now=started_at, duration_seconds=60,
    )
    expired_question = await ai_game_service.begin_question(
        session,
        session_id=first_id,
        user_tg_id=10,
        question="   ",
        now=expired_at,
    )
    assert (expired_question.outcome, expired_question.reason) == (
        TurnOutcome.rejected, TurnRejectReason.expired,
    )
    assert expired_question.terminal is not None
    assert await _turn_count(session, first_id) == 0

    second_id = await _running_v2(
        session, monkeypatch, now=started_at, duration_seconds=60,
    )
    expired_guess = await ai_game_service.submit_guess(
        session,
        session_id=second_id,
        user_tg_id=10,
        answer="   ",
        now=expired_at,
    )
    assert (expired_guess.outcome, expired_guess.reason) == (
        TurnOutcome.rejected, TurnRejectReason.expired,
    )
    assert expired_guess.terminal is not None
    assert await _turn_count(session, second_id) == 0

    third_id = await _running_v2(
        session, monkeypatch, now=started_at, duration_seconds=60,
    )
    claim = await ai_game_service.begin_question(
        session,
        session_id=third_id,
        user_tg_id=10,
        question="La campagna include una cooperativa?",
        now=started_at,
    )
    assert claim.claim is not None
    late = await ai_game_service.complete_question(
        session,
        claim=claim.claim,
        verdict=QuestionVerdict.si,
        now=expired_at,
    )
    assert (late.outcome, late.reason) == (TurnOutcome.rejected, TurnRejectReason.expired)
    assert late.terminal is not None
    assert await _turn_count(session, third_id) == 0


async def test_v2_provider_failure_abandons_claim_without_charging_a_question(
    session, monkeypatch,
):
    """A failed router must release rather than burn a participant's quota."""
    session_id = await _running_v2(session, monkeypatch)
    started = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=10,
        question="Il gioco ha una modalità cooperativa?",
    )
    assert started.claim is not None

    class AllProvidersFail:
        async def generate(self, *_args, **_kwargs):
            raise StructuredAIError("all providers failed")

    with pytest.raises(StructuredAIError):
        await ai_game_service.classify_question(started.claim, AllProvidersFail())
    abandoned = await ai_game_service.abandon_claim(
        session,
        claim=started.claim,
        reason=TurnRejectReason.providers_unavailable,
    )
    assert (abandoned.outcome, abandoned.reason) == (
        TurnOutcome.rejected, TurnRejectReason.providers_unavailable,
    )
    assert (await ai_game_service.get_personal_quota(
        session, session_id, 10,
    )).questions_left == 5
    assert await _turn_count(session, session_id) == 0
    assert await _pending_fields(session, session_id) == (None, None, None, None)


async def test_v2_submit_guess_is_local_and_winning_alias_flushes_before_settlement(
    session, monkeypatch, user_factory,
):
    """Trusting a caller's boolean could award a winner without a locally matched alias."""
    await user_factory(10)
    session_id = await _running_v2(session, monkeypatch)
    assert "correct" not in inspect.signature(ai_game_service.submit_guess).parameters

    won = await ai_game_service.submit_guess(
        session,
        session_id=session_id,
        user_tg_id=10,
        answer="  PORTAL TWO?! ",
    )
    assert (won.outcome, won.correct) == (TurnOutcome.recorded, True)
    assert won.terminal is not None and won.terminal.transitioned
    await session.commit()

    turn = (await session.execute(select(AIGameTurn).where(
        AIGameTurn.session_id == session_id,
    ))).scalar_one()
    root = (await session.execute(select(AIGameSession.status).where(
        AIGameSession.id == session_id,
    ))).scalar_one()
    winner = (await session.execute(select(TwentyQuestionsGame.winner_tg_id).where(
        TwentyQuestionsGame.session_id == session_id,
    ))).scalar_one()
    assert (turn.kind, json.loads(turn.output_json), root, winner) == (
        TurnKind.guess.value, {"correct": True}, "finished", 10,
    )


async def test_v2_question_claim_context_queries_only_recent_candidates_then_bounds_payload(
    session, monkeypatch,
):
    """Loading the whole ledger could send unbounded history to a provider."""
    # Settings can choose a smaller payload, but the protocol's privacy budget
    # is never widened by a permissive deployment value.
    monkeypatch.setattr(ai_game_service.settings, "twentyq_context_turns", 96)
    monkeypatch.setattr(ai_game_service.settings, "twentyq_context_chars", 30_000)
    session_id = await _running_v2(session, monkeypatch)
    session.add_all([
        AIGameTurn(
            session_id=session_id,
            turn_no=turn_no,
            user_tg_id=10,
            kind=TurnKind.question.value,
            input_text=(
                "Does the oldest unicorn detail matter?" if turn_no == 1
                else f"Generic historic question {turn_no}?"
            ),
            output_json='{"verdetto":"si"}',
            normalized_input_hash=f"{turn_no:064x}",
        )
        for turn_no in range(1, 101)
    ])
    await session.flush()
    await session.execute(update(AIGameSession).where(
        AIGameSession.id == session_id,
    ).values(next_turn_no=101))
    await session.commit()

    started = await ai_game_service.begin_question(
        session,
        session_id=session_id,
        user_tg_id=99,
        question="Does a unicorn appear in this game?",
    )
    assert started.claim is not None
    context_numbers = [turn.turn_no for turn in started.claim.context]
    serialized_context = json.dumps([
        {
            "turn_no": turn.turn_no,
            "normalized_hash": turn.normalized_hash,
            "question": turn.question,
            "verdict": turn.verdict.value,
        }
        for turn in started.claim.context
    ], ensure_ascii=False, separators=(",", ":"))
    assert context_numbers == list(range(77, 101))
    assert len(started.claim.context) == 24
    assert len(serialized_context.encode("utf-8")) <= 12_000
