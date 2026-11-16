from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from database.models import ScheduledTask
from handlers.event_types.telephone_type import TelephoneType
from services import schedule_service, telephone_service as game


@pytest.fixture
def context(monkeypatch):
    from handlers import telephone

    monkeypatch.setattr(game.settings, "telephone_enabled", True)
    monkeypatch.setattr(game.settings, "openrouter_api_key", "fake")
    monkeypatch.setattr(telephone.group_registry, "get_group_id", lambda: -1001234)
    publish = AsyncMock()
    monkeypatch.setattr(telephone, "publish_card", publish)
    message = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    return TelephoneType(), message, publish


async def test_lists_details_discovery_and_ready_creation(session, context, monkeypatch):
    spec, message, _ = context
    await spec.render_list(message, session)
    await spec.render_detail(message, session, -1)
    assert "non trovata" in message.edit_text.call_args.args[0]
    assert await spec.describe_scheduled(session, -1) is None
    assert await spec.schedulable_items(session) == []
    state = SimpleNamespace(clear=AsyncMock())
    await spec.start_creation(message, state, 1)
    state.clear.assert_awaited_once()
    assert "non genera" in message.edit_text.call_args.args[0]
    game_id = await game.create(session, 1)
    await session.commit()
    assert (
        await spec.describe_scheduled(session, game_id)
    ).deep_link_payload == f"telefono_{game_id}"
    assert await spec.schedulable_items(session) == [(game_id, f"Telefono senza fili #{game_id}")]
    await session.commit()
    await spec.render_list(message, session)
    await spec.render_detail(message, session, game_id)
    assert "nessun premio" in message.edit_text.call_args.args[0].lower()
    monkeypatch.setattr(game.settings, "telephone_enabled", False)
    await spec.render_list(message, session)
    assert "non abilitata" in message.edit_text.call_args.args[0]


async def test_start_close_republish_are_post_commit_and_recoverable(session, context):
    spec, message, publish = context
    game_id = await game.create(session, 1)
    await session.commit()
    assert not (await spec.start_now(None, session, -1)).ok
    result = await spec.start_now(None, session, game_id)
    assert result.ok
    publish.assert_not_awaited()
    await session.commit()
    await result.post_commit()
    publish.assert_awaited_once()
    assert len(await spec.discover_open(session)) == 1
    assert await spec.describe_scheduled(session, game_id) is None
    await spec.render_detail(message, session, game_id)
    assert "Chiudi" in str(message.edit_text.call_args.kwargs["reply_markup"])
    result = await spec.close_now(None, session, game_id)
    assert result.ok
    await session.commit()
    await result.post_commit()
    assert not (await spec.close_now(None, session, game_id)).ok
    assert not await spec.discover_open(session)
    result = await spec.start_now(None, session, game_id)
    assert result.ok
    await session.commit()
    await result.post_commit()
    await spec.render_detail(message, session, game_id)


def task(game_id, action="start"):
    import json

    return ScheduledTask(
        ref_id=game_id, created_by_tg_id=5, payload_json=json.dumps({"action": action})
    )


async def test_scheduled_dispatch_and_expiry_keep_core_scheduler_generic(session, context):
    spec, _, publish = context
    with pytest.raises(ValueError, match="Missing"):
        await spec.execute_scheduled(None, session, task(None), -1001234)
    with pytest.raises(ValueError, match="Unknown"):
        await spec.execute_scheduled(None, session, task(99, "unexpected"), -1001234)
    with pytest.raises(schedule_service.TaskSkip):
        await spec.execute_scheduled(None, session, task(-1), -1001234)
    game_id = await game.create(session, 1)
    hook = await spec.execute_scheduled(None, session, task(game_id), -1001234)
    await session.commit()
    await hook()
    hook = await spec.execute_scheduled(None, session, task(game_id, "expire"), -1001234)
    await session.commit()
    await hook()
    hook = await spec.execute_scheduled(None, session, task(game_id, "close"), -1001234)
    await session.commit()
    await hook()
    assert publish.await_count == 3


async def test_expired_game_is_not_inline_available(session, context, monkeypatch):
    spec, _, _ = context
    game_id = await game.create(session, 1)
    await game.start(session, game_id, -1001234, now=schedule_service.utcnow() - timedelta(hours=1))
    await session.commit()
    assert await spec.discover_open(session) == []


async def test_admin_actor_is_audited_by_generic_caller_not_guessed_from_creator(session, context):
    from database.models import AdminAction
    from handlers import events
    from sqlalchemy import select

    spec, _, _ = context
    game_id = await game.create(session, 1)
    result = await spec.start_now(None, session, game_id)
    cb = SimpleNamespace(from_user=SimpleNamespace(id=99), answer=AsyncMock())
    assert await events._commit_then_present(cb, session, result)
    action = (await session.execute(select(AdminAction))).scalar_one()
    assert action.admin_tg_id == 99 and action.action_type == "telephone_start"
    assert action.detail == f"game={game_id}"
