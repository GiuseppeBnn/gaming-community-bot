"""A disabled rollout has no discovery, entry points, posts or paid requests."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.dispatcher.event.bases import SkipHandler
import pytest

from config_data.config import settings
from handlers import event_types, events, help_content, images, schedule, telephone
from handlers.callbacks import SchedCb, TelephoneCb
from handlers.event_types.base import _REGISTRY
from main import _ADMIN_COMMANDS, _enabled_commands
from services import event_discovery, schedule_service, telephone_service as game


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    snapshot = dict(_REGISTRY)
    monkeypatch.setattr(settings, "telephone_enabled", False)
    event_types.register_builtin()
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


@pytest.mark.parametrize("enabled", [False, True])
async def test_rollout_controls_all_discovery_surfaces(monkeypatch, enabled):
    monkeypatch.setattr(settings, "telephone_enabled", enabled)
    assert (event_types.get("telephone") is not None) is enabled
    assert event_types.is_disabled("telephone") is not enabled
    assert not event_types.is_disabled("unknown")
    assert ("telephone" in [spec.key for spec in event_types.all_types()]) is enabled
    assert ("immagine" in [cmd.command for cmd in _enabled_commands(_ADMIN_COMMANDS)]) is enabled
    assert (help_content.lookup("immagine") is not None) is enabled
    assert (help_content.render_command("immagine", is_admin=True) is not None) is enabled
    assert ("immagine" in help_content.suggestions("immagine")) is enabled
    assert ("/immagine" in help_content.render_legend(is_admin=True)) is enabled
    assert ("Telefono senza fili" in help_content.render_command("eventi", is_admin=True)) is enabled
    assert "telefono" not in help_content.render_alduino_reference().lower()
    assert "staffett" not in help_content.render_legend(is_admin=True).lower()
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    await events.show_hub(message)
    assert ("telephone" in str(message.edit_text.call_args)) is enabled
    state = SimpleNamespace(clear=AsyncMock())
    await schedule.start_schedule_flow(message, state)
    assert ("telephone" in str(message.answer.call_args)) is enabled


async def test_disabled_handlers_do_not_read_state_database_or_call_telegram_or_ai():
    # Empty objects deliberately fail on any unexpected read. Only generic acks
    # for old links/buttons are allowed; even membership requests must not occur.
    message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(answer=AsyncMock())
    await telephone.start_creation(message, object(), 1, object())
    await telephone.join(message, object(), object(), 42)
    assert "Link non disponibile" in message.answer.call_args.args[0]
    with pytest.raises(SkipHandler):
        await telephone.reply(message, object())
    await telephone.publish_card(object(), object(), 42)
    await telephone.create(callback, object(), object())
    for action in ("retry", "leave"):
        await telephone.retry_control(
            callback, TelephoneCb(action=action, game_id=42), object(), object()
        )
    await telephone.gallery(callback, TelephoneCb(action="view", game_id=42), object())
    assert callback.answer.await_count == 4
    assert all(call.args == ("Azione non disponibile.",) for call in callback.answer.call_args_list)
    await images.cmd_immagine(object(), object(), object())
    allowed, _ = await images.router.message.check_root_filters(object())
    assert not allowed


@pytest.mark.parametrize("corrupt_payload", [False, True])
async def test_disabled_existing_games_and_tasks_are_hidden_and_never_executed(
    session, monkeypatch, corrupt_payload,
):
    monkeypatch.setattr(settings, "telephone_enabled", True)
    monkeypatch.setattr(settings, "openrouter_api_key", "fake")
    game_id = await game.create(session, 1)
    await game.start(session, game_id, -1001234)
    now = schedule_service.utcnow()
    due = await schedule_service.schedule_task(
        session, "telephone", now - timedelta(seconds=1), 1, -1001234,
        ref_id=game_id,
    )
    future = await schedule_service.schedule_task(
        session, "telephone", now + timedelta(hours=1), 1, -1001234,
        ref_id=game_id,
    )
    if corrupt_payload:
        due.payload_json = "not-json"
    await session.commit()
    monkeypatch.setattr(settings, "telephone_enabled", False)
    assert (await schedule._programmati_view(session))[0] == "🗓️ Nessun evento programmato."
    assert await event_discovery.list_public_events(
        session, event_types=event_types.all_types()
    ) == []
    callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    await schedule.cb_sched_view(callback, SchedCb(action="view", item_id=future.id), session)
    assert "Nessun evento" in callback.message.edit_text.call_args.args[0]
    bot = SimpleNamespace(send_message=AsyncMock())
    await schedule._run_due_task(bot, session, due)
    await session.refresh(due)
    assert due.status == "done"
    bot.send_message.assert_not_awaited()
    # No feature rows are deleted or implicitly closed; a re-enabled test
    # instance may manage its own retained history.
    assert (await game.get_view(session, game_id)).status == "running"


async def test_disabled_scheduling_draft_cannot_be_resumed():
    message = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(
        clear=AsyncMock(), get_data=AsyncMock(return_value={"sched_type": "telephone"})
    )
    await schedule.start_schedule_for(message, state, "telephone", 42, "hidden label")
    await schedule.fsm_event_runat(message, state, object())
    callback = SimpleNamespace(answer=AsyncMock())
    await schedule.cb_action(callback, SchedCb(action="act", key="start"), state)
    assert state.clear.await_count == 3
    assert all("hidden label" not in str(call) for call in message.answer.call_args_list)
    callback.answer.assert_awaited_once_with("Azione non disponibile.")
