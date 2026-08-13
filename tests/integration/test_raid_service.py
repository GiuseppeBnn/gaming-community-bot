from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from database.models import AIGameTurn, RaidAction, ScheduledTask
from services import raid_service, schedule_service


async def _ready(session):
    root = await raid_service.create_raid(
        session,
        creator_tg_id=9,
        blueprint=raid_service.fallback_blueprint("tema", ("a", "d", "i")),
    )
    await session.commit()
    return root.id


async def _running(session):
    session_id = await _ready(session)
    assert await raid_service.start(
        session, session_id, group_id=-1001, anchor_message_id=77,
    )
    await session.commit()
    return session_id


async def test_manual_start_cancels_a_future_duplicate_start(session):
    session_id = await _ready(session)
    future = await schedule_service.schedule_task(
        session, "raid", raid_service._now(), 9, -1001, ref_id=session_id,
    )
    await session.commit()
    assert await raid_service.start(
        session, session_id, group_id=-1001, anchor_message_id=77,
    )
    await session.commit()
    await session.refresh(future)
    assert future.status == "cancelled"


async def test_full_raid_supports_changed_votes_and_late_joiners(session):
    session_id = await _running(session)
    tasks = (await session.execute(select(ScheduledTask))).scalars().all()
    assert len(tasks) == 1
    assert schedule_service.task_payload(tasks[0]) == {
        "action": "phase", "phase": 1, "internal": True,
    }
    assert await schedule_service.list_pending(session) == []
    assert await schedule_service.due_tasks(session, tasks[0].run_at) == tasks

    assert (await raid_service.record_action(
        session, session_id=session_id, phase_no=1, user_tg_id=1, tactic="d",
    ))[0]
    # Same user changes their mind: unique/upsert, never a second vote.
    assert (await raid_service.record_action(
        session, session_id=session_id, phase_no=1, user_tg_id=1, tactic="a",
    ))[0]
    assert (await raid_service.record_action(
        session, session_id=session_id, phase_no=1, user_tg_id=2, tactic="a",
    ))[0]
    await session.commit()
    phase_one = await raid_service.advance_phase(session, session_id, manual=True)
    await session.commit()
    assert phase_one.ok and not phase_one.finished
    assert phase_one.snapshot.game.boss_hp == 50
    assert phase_one.snapshot.game.current_phase == 2
    action_count = (await session.execute(select(RaidAction))).scalars().all()
    assert len(action_count) == 2

    # User 3 joins only now; missing phase 1 carries no penalty.
    assert (await raid_service.record_action(
        session, session_id=session_id, phase_no=2, user_tg_id=3, tactic="d",
    ))[0]
    await session.commit()
    phase_two = await raid_service.advance_phase(session, session_id, manual=True)
    await session.commit()
    assert phase_two.snapshot.game.boss_hp == 10
    assert phase_two.snapshot.total_participants == 3

    assert (await raid_service.record_action(
        session, session_id=session_id, phase_no=3, user_tg_id=4, tactic="i",
    ))[0]
    await session.commit()
    final = await raid_service.advance_phase(session, session_id, manual=True)
    await session.commit()
    assert final.ok and final.finished
    assert final.snapshot.session.status == "finished"
    assert final.snapshot.game.result == "victory"
    turns = (await session.execute(
        select(AIGameTurn).order_by(AIGameTurn.turn_no)
    )).scalars().all()
    assert [json.loads(turn.output_json)["damage"] for turn in turns] == [40, 40, 40]
    assert not (await raid_service.record_action(
        session, session_id=session_id, phase_no=3, user_tg_id=5, tactic="i",
    ))[0]


async def test_manual_empty_phase_refuses_but_automatic_extends_once_then_abandons(
    session, monkeypatch,
):
    monkeypatch.setattr(raid_service.settings, "raid_empty_extension_minutes", 5)
    session_id = await _running(session)
    manual = await raid_service.advance_phase(session, session_id, manual=True)
    assert not manual.ok and "Nessuna scelta" in manual.message

    first = await raid_service.advance_phase(session, session_id, expected_phase=1)
    await session.commit()
    assert first.extended and first.snapshot.game.empty_extensions == 1

    second = await raid_service.advance_phase(session, session_id, expected_phase=1)
    await session.commit()
    assert second.finished
    assert second.snapshot.game.result == "abandoned"
    assert second.snapshot.session.status == "finished"


async def test_stale_phase_task_is_a_clean_skip(session):
    session_id = await _running(session)
    await raid_service.record_action(
        session, session_id=session_id, phase_no=1, user_tg_id=1, tactic="a",
    )
    await session.commit()
    await raid_service.advance_phase(session, session_id, manual=True)
    await session.commit()
    with pytest.raises(schedule_service.TaskSkip, match="già risolta"):
        await raid_service.advance_phase(session, session_id, expected_phase=1)


async def test_close_delete_and_missing_values(session):
    ready = await _ready(session)
    assert not (await raid_service.record_action(
        session, session_id=ready, phase_no=1, user_tg_id=1, tactic="x",
    ))[0]
    assert await raid_service.delete_raid(session, ready)
    await session.commit()
    assert await raid_service.get_snapshot(session, ready) is None

    running = await _running(session)
    assert not await raid_service.delete_raid(session, running)
    assert await raid_service.close(session, running)
    assert not await raid_service.close(session, running)
    await session.commit()
    assert (await raid_service.get_snapshot(session, running)).game.result == "abandoned"


async def test_lists_duplicate_start_missing_advance_and_anchor_move(session):
    ready = await _ready(session)
    other = await _ready(session)
    assert {row.id for row in await raid_service.list_ready(session)} == {ready, other}
    assert {row.id for row in await raid_service.list_manageable(session)} == {ready, other}
    assert await raid_service.start(
        session, ready, group_id=-1001, anchor_message_id=10,
    )
    assert not await raid_service.start(
        session, ready, group_id=-1001, anchor_message_id=11,
    )
    assert not await raid_service.start(
        session, other, group_id=-1001, anchor_message_id=12,
    )
    await raid_service.move_anchor(session, ready, 99)
    await session.commit()
    assert (await raid_service.get_snapshot(session, ready)).session.anchor_message_id == 99
    assert [row.id for row in await raid_service.list_manageable(session)][0] == ready

    missing = await raid_service.advance_phase(session, 99999)
    assert not missing.ok
    with pytest.raises(schedule_service.TaskSkip, match="non più in corso"):
        await raid_service.advance_phase(session, other, expected_phase=1)
