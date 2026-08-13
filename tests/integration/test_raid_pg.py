"""PostgreSQL-only concurrency checks for large-group raid voting."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from database.models import RaidAction
from services import raid_service

pytestmark = pytest.mark.pg


async def test_many_simultaneous_voters_are_all_kept(pg_sessions):
    async with pg_sessions() as setup:
        root = await raid_service.create_raid(
            setup,
            creator_tg_id=9,
            blueprint=raid_service.fallback_blueprint("concorrenza", ("a", "d", "i")),
        )
        await setup.commit()
        assert await raid_service.start(
            setup, root.id, group_id=-1001, anchor_message_id=77,
        )
        await setup.commit()
        session_id = root.id

    async def vote(user_id: int):
        async with pg_sessions() as session:
            result = await raid_service.record_action(
                session,
                session_id=session_id,
                phase_no=1,
                user_tg_id=user_id,
                tactic="a" if user_id % 2 else "d",
            )
            await session.commit()
            return result

    results = await asyncio.gather(*(vote(user_id) for user_id in range(1, 9)))
    assert all(result.ok for result in results)

    async with pg_sessions() as check:
        count = (await check.execute(select(func.count(RaidAction.id)).where(
            RaidAction.session_id == session_id,
            RaidAction.phase_no == 1,
        ))).scalar_one()
        assert count == 8


async def test_postgres_upsert_changes_choice_without_duplicate(pg_sessions):
    async with pg_sessions() as session:
        root = await raid_service.create_raid(
            session,
            creator_tg_id=9,
            blueprint=raid_service.fallback_blueprint("upsert", ("a", "d", "i")),
        )
        await session.commit()
        assert await raid_service.start(
            session, root.id, group_id=-1001, anchor_message_id=77,
        )
        await session.commit()
        results = []
        for tactic in ("a", "i"):
            result = await raid_service.record_action(
                session,
                session_id=root.id,
                phase_no=1,
                user_tg_id=42,
                tactic=tactic,
            )
            assert result.ok
            results.append(result)
            await session.commit()

        actions = list((await session.execute(select(RaidAction).where(
            RaidAction.session_id == root.id,
        ))).scalars().all())
        assert len(actions) == 1
        assert actions[0].tactic == "i"
        assert results[0].roll == results[1].roll == actions[0].roll
