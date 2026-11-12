from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from database.models import AIGameSession, AIGameTurn, ScheduledTask, TelephoneVisit
from services import telephone_service as game

NOW = datetime(2026, 11, 12, 0, 0)
GROUP = -1001234


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(game.settings, "telephone_enabled", True)
    monkeypatch.setattr(game.settings, "openrouter_api_key", "fake-never-sent")


async def running(session):
    game_id = await game.create(session, 1)
    assert await game.start(session, game_id, GROUP, now=NOW)
    return game_id


async def writing(session, game_id, user=10, now=NOW):
    claim, reason = await game.claim(session, game_id, user, f"User {user}", GROUP, now=now)
    assert reason == "claimed" and claim is not None
    assert await game.delivered(session, claim, user * 100, now=now)
    return claim


async def test_policy_and_lifecycle_are_durable_and_no_implicit_commit(session):
    assert await game.get_view(session, -1) is None
    game_id = await running(session)
    view = await game.get_view(session, game_id)
    assert view.expires_at == NOW + timedelta(minutes=30)
    assert view.cap == 12000 and view.stage == 1
    assert await game.list_games(session) == [(game_id, "running")]
    assert await game.start(session, game_id, GROUP, now=NOW + timedelta(minutes=1))
    assert (await game.get_view(session, game_id)).expires_at == view.expires_at
    assert not await game.start(session, game_id, GROUP - 1, now=NOW)
    other = await game.create(session, 2)
    assert not await game.start(session, other, GROUP, now=NOW)
    await session.rollback()
    assert await game.get_view(session, game_id) is None


@pytest.mark.parametrize("missing_key", [True, False])
async def test_disabled_creation(session, monkeypatch, missing_key):
    monkeypatch.setattr(game.settings, "openrouter_api_key", "" if missing_key else "fake")
    monkeypatch.setattr(game.settings, "telephone_enabled", missing_key)
    with pytest.raises(ValueError):
        await game.create(session, 1)
    assert not await game.start(session, 1, 0)


async def test_four_different_people_see_only_latest_image(session):
    game_id = await running(session)
    for user in range(10, 14):
        c = await writing(session, game_id, user)
        assert c.stage == user - 9
        assert c.previous_file_id == (None if user == 10 else f"image-{user - 1}")
        assert await game.reply_claim(session, user, user * 100) == game.Claim(
            game_id, c.token, user, c.stage, None
        )
        assert await game.reply_claim(session, user + 100, user * 100) is None
        view = await game.begin_generation(session, c, f"scene {user}", now=NOW)
        assert view.kind == "generating"
        assert await game.begin_generation(session, c, "duplicate", now=NOW) is None
        assert not await game.release(session, game_id, c.token)
        assert await game.complete(session, c, f"image-{user}", now=NOW)
        assert not await game.complete(session, c, "duplicate", now=NOW)
        await session.commit()
        assert (await game.claim(session, game_id, user, "again", GROUP, now=NOW))[0] is None
    view = await game.get_view(session, game_id)
    assert view.status == "finished" and view.reason == "completed"
    assert view.stage == 5 and len(view.stages) == 4
    assert [s.description for s in view.stages] == [f"scene {n}" for n in range(10, 14)]
    assert not await game.finish(session, game_id, "duplicate")
    assert not await game.tick(session, game_id)


async def test_claims_busy_resume_seen_and_expired_tokens(session, monkeypatch):
    game_id = await running(session)
    c = await writing(session, game_id)
    assert (await game.claim(session, game_id, 10, "same", GROUP, now=NOW))[1] == "resume"
    assert (await game.claim(session, game_id, 11, "other", GROUP, now=NOW))[1] == "busy"
    later = NOW + timedelta(seconds=121)
    replacement = await writing(session, game_id, 11, later)
    assert not await game.delivered(session, c, 99, now=later)
    assert not await game.release(session, game_id, c.token)
    assert await game.begin_generation(session, c, "late", now=later) is None
    assert (await game.claim(session, game_id, 10, "again", GROUP, now=later))[1] == "seen"
    assert await game.release(session, game_id, replacement.token)
    assert (await game.claim(session, game_id, 11, "again", GROUP, now=later))[1] == "seen"
    assert (await game.claim(session, game_id, 12, "outsider", GROUP - 1, now=later))[1] == "closed"
    monkeypatch.setattr(game.settings, "telephone_enabled", False)
    assert (await game.claim(session, game_id, 12, "new", GROUP, now=later))[1] == "disabled"
    assert await game.begin_generation(session, replacement, "hi", now=later) is None


async def test_delivery_lease_and_writer_lease_have_separate_deadlines(session):
    game_id = await running(session)
    c, _ = await game.claim(session, game_id, 10, "name", GROUP, now=NOW)
    assert await game.delivered(session, c, 1, now=NOW + timedelta(seconds=25))
    assert not await game.tick(session, game_id, now=NOW + timedelta(seconds=31))
    assert not await game.tick(session, game_id, now=NOW + timedelta(seconds=144))
    assert await game.tick(session, game_id, now=NOW + timedelta(seconds=145))
    view = await game.get_view(session, game_id)
    assert view.status == "running" and view.token is None
    d, _ = await game.claim(session, game_id, 11, "next", GROUP, now=NOW + timedelta(seconds=150))
    assert not await game.delivered(session, d, 2, now=NOW + timedelta(seconds=180))
    assert await game.tick(session, game_id, now=NOW + timedelta(seconds=180))


async def test_expiry_and_crashed_generation_never_reopen_a_paid_job(session):
    game_id = await running(session)
    c = await writing(session, game_id)
    await game.begin_generation(session, c, "scene", now=NOW)
    assert not await game.tick(session, game_id, now=NOW + timedelta(seconds=179))
    assert await game.tick(session, game_id, now=NOW + timedelta(seconds=180))
    assert (await game.get_view(session, game_id)).status == "finished"
    assert not await game.complete(session, c, "orphan", now=NOW)
    assert (await game.claim(session, game_id, 11, "other", GROUP, now=NOW))[1] == "closed"
    assert not await game.tick(session, -1)


async def test_overall_deadline_allows_an_already_admitted_generation_to_finish(session):
    game_id = await running(session)
    almost = NOW + timedelta(seconds=1790)
    c = await writing(session, game_id, now=almost)
    await game.begin_generation(session, c, "scene", now=almost)
    assert not await game.tick(session, game_id, now=NOW + timedelta(seconds=1801))
    assert await game.complete(session, c, "image", now=NOW + timedelta(seconds=1810))
    view = await game.get_view(session, game_id)
    assert view.status == "finished" and len(view.stages) == 1


async def test_empty_expiry_and_invalid_input_do_not_create_turns(session):
    game_id = await running(session)
    c = await writing(session, game_id)
    for value in ("", "x" * 151):
        assert await game.begin_generation(session, c, value, now=NOW) is None
    assert await game.tick(session, game_id, now=NOW + timedelta(minutes=31))
    assert (await game.claim(session, game_id, 11, "late", GROUP, now=NOW + timedelta(minutes=31)))[
        1
    ] == "closed"
    assert (await session.execute(select(AIGameTurn))).scalars().all() == []
    assert (await session.execute(select(TelephoneVisit))).scalar_one().description is None
    assert len((await session.execute(select(ScheduledTask))).scalars().all()) == 3


async def test_anchor_and_snapshot_bypass_stale_orm_state(session):
    game_id = await running(session)
    stale = await session.get(AIGameSession, game_id)
    await game.set_anchor(session, game_id, 42)
    await game.set_anchor(session, game_id, 99)
    await session.execute(
        update(AIGameSession)
        .where(AIGameSession.id == game_id)
        .values(next_turn_no=3)
        .execution_options(synchronize_session=False)
    )
    assert stale.next_turn_no == 1
    assert (await game.get_view(session, game_id)).stage == 3
    assert (await game.get_view(session, game_id)).anchor == 42


@pytest.mark.pg
async def test_concurrent_claims_have_one_winner_on_postgres(pg_sessions):
    import asyncio

    async with pg_sessions() as s:
        game_id = await running(s)
        await s.commit()

    async def claim(user):
        async with pg_sessions() as s:
            result = await game.claim(s, game_id, user, str(user), GROUP, now=NOW)
            await s.commit()
            return result

    results = await asyncio.gather(claim(10), claim(11))
    assert sorted(r for _, r in results) == ["busy", "claimed"]
    async with pg_sessions() as s:
        assert len((await s.execute(select(TelephoneVisit))).scalars().all()) == 1


@pytest.mark.pg
async def test_concurrent_starts_and_duplicate_paid_submissions_on_postgres(pg_sessions):
    import asyncio

    async with pg_sessions() as s:
        games = [await game.create(s, 1), await game.create(s, 2)]
        await s.commit()

    async def start(i):
        async with pg_sessions() as s:
            ok = await game.start(s, i, GROUP, now=NOW)
            await s.commit()
            return ok

    outcomes = await asyncio.gather(*(start(i) for i in games))
    assert sum(outcomes) == 1
    active = games[outcomes.index(True)]
    async with pg_sessions() as s:
        c = await writing(s, active)
        await s.commit()

    async def submit():
        async with pg_sessions() as s:
            result = await game.begin_generation(s, c, "scene", now=NOW)
            await s.commit()
            return result

    assert sum(v is not None for v in await asyncio.gather(submit(), submit())) == 1
