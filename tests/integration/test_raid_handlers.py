from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from database.models import AIGameTurn, ScheduledTask
from handlers import raid as handler
from handlers.callbacks import RaidCb
from handlers.event_types.raid_type import RaidType
from services import group_registry, raid_service, schedule_service


@pytest.fixture(autouse=True)
def _neutral_d20(monkeypatch):
    monkeypatch.setattr(raid_service.dice, "d20", lambda: 10)


class _State:
    def __init__(self):
        self.value = None

    async def clear(self):
        self.value = None

    async def set_state(self, value):
        self.value = value

    async def get_state(self):
        return self.value.state if hasattr(self.value, "state") else self.value


class _Sent:
    def __init__(self, parent=None, message_id=77, fail_edit=False, fail_delete=False):
        self.parent = parent
        self.message_id = message_id
        self.fail_edit = fail_edit
        self.fail_delete = fail_delete
        self.deleted = False

    async def edit_text(self, text, reply_markup=None, **kwargs):
        if self.fail_edit:
            raise RuntimeError("gone")
        if self.parent:
            self.parent.said.append(text)
            self.parent.markups.append(reply_markup)

    async def delete(self):
        if self.fail_delete:
            raise RuntimeError("gone")
        self.deleted = True


class _Message:
    def __init__(self, text="tema epico", *, chat_id=42, chat_type="private", user=9):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.from_user = SimpleNamespace(id=user)
        self.said = []
        self.markups = []
        self.next_answer = None

    async def answer(self, text, reply_markup=None, **kwargs):
        self.said.append(text)
        self.markups.append(reply_markup)
        return self.next_answer or _Sent(self, message_id=999)

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.said.append(text)
        self.markups.append(reply_markup)


class _Bot:
    def __init__(self, fail_edit=False):
        self.fail_edit = fail_edit
        self.edits = []

    async def edit_message_text(self, **kwargs):
        if self.fail_edit:
            raise RuntimeError("deleted")
        self.edits.append(kwargs)


class _Callback:
    def __init__(self, message, *, user=50):
        self.message = message
        self.from_user = SimpleNamespace(id=user)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class _Provider:
    async def generate_json(self, **kwargs):
        value = raid_service.fallback_blueprint("tema", ("a", "d", "i"))
        raw = json.loads(raid_service.blueprint_json(value))
        for phase in raw["phases"]:
            phase.pop("counter")
        return raw


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


async def test_creation_validation_save_cancel_and_fallback_note(session, monkeypatch):
    state, message = _State(), _Message()
    await handler.start_creation(message, state, 9)
    assert state.value == handler.RaidCreateStates.theme
    assert "Nuovo raid" in message.said[-1]

    state.value = handler.RaidCreateStates.building
    await handler.creation_in_progress(message)
    assert "già preparando" in message.said[-1]
    state.value = handler.RaidCreateStates.theme

    message.text = "x"
    await handler.create_from_theme(message, state, session)
    assert "3 a 300" in message.said[-1]

    monkeypatch.setattr(handler, "GeminiStructuredProvider", _Provider)
    message.text = "fortezza nel cielo"
    await handler.create_from_theme(message, state, session)
    assert "pronto" in message.said[-1]
    assert state.value is None

    await handler.start_creation(message, state, 9)
    callback = _Callback(message)
    await handler.cancel_creation(callback, state)
    assert "annullata" in message.said[-1]

    stale = _Callback(message)
    await handler.cancel_creation(stale, state)
    assert "non è più attiva" in stale.answers[-1][0]

    before = len(await raid_service.list_manageable(session))

    async def cancelled_in_flight(*args, **kwargs):
        await state.clear()
        return raid_service.fallback_blueprint("x", ("a", "d", "i")), False

    monkeypatch.setattr(raid_service, "build_blueprint", cancelled_in_flight)
    await handler.start_creation(message, state, 9)
    message.text = "raid poi annullato"
    await handler.create_from_theme(message, state, session)
    assert len(await raid_service.list_manageable(session)) == before

    async def fallback(*args, **kwargs):
        return raid_service.fallback_blueprint("x", ("a", "d", "i")), True
    monkeypatch.setattr(raid_service, "build_blueprint", fallback)
    message.text = "secondo scenario"
    message.next_answer = _Sent(message, fail_edit=True)
    await handler.create_from_theme(message, state, session)
    assert "scenario di riserva" in message.said[-1]


async def test_card_is_readable_hides_live_distribution_and_shows_results(session):
    session_id = await _running(session)
    snapshot = await raid_service.get_snapshot(session, session_id)
    text, keyboard = handler.render_card(snapshot)
    assert "chi arriva tardi" in text
    assert "Scelte:" not in text
    assert len(keyboard.inline_keyboard) == 3
    assert all(len(row) == 1 for row in keyboard.inline_keyboard)
    assert all(len(button.callback_data.encode()) <= 64 for row in keyboard.inline_keyboard for button in row)

    await raid_service.record_action(
        session, session_id=session_id, phase_no=1, user_tg_id=1, tactic="a",
    )
    await session.commit()
    result = await raid_service.advance_phase(session, session_id, manual=True)
    await session.commit()
    text, _ = handler.render_card(result.snapshot)
    assert "Cronaca" in text and "Scelte:" in text and "-40 HP" in text

    # Finish with two setbacks to exercise the non-victory ending.
    for phase in (2, 3):
        await raid_service.record_action(
            session, session_id=session_id, phase_no=phase,
            user_tg_id=phase, tactic="a",
        )
        await session.commit()
        result = await raid_service.advance_phase(session, session_id, manual=True)
        await session.commit()
    text, keyboard = handler.render_card(result.snapshot)
    assert "IL BOSS RESISTE" in text
    assert keyboard is None
    result.snapshot.game.result = "victory"
    text, _ = handler.render_card(result.snapshot)
    assert "VITTORIA" in text


async def test_card_abandoned_invalid_turn_and_anchor_recovery(session, monkeypatch):
    session_id = await _running(session)
    root = await raid_service.get_snapshot(session, session_id)
    root.session.next_turn_no += 1
    session.add(AIGameTurn(
        session_id=session_id, turn_no=root.session.next_turn_no - 1,
        user_tg_id=0, kind="phase", input_text="bad",
        output_json=json.dumps({"phase": 99}),
    ))
    await session.commit()
    await raid_service.close(session, session_id)
    await session.commit()
    snapshot = await raid_service.get_snapshot(session, session_id)
    text, _ = handler.render_card(snapshot)
    assert "SPEDIZIONE CONCLUSA" in text

    async def send(*args, **kwargs):
        return _Sent(message_id=88)
    monkeypatch.setattr(group_registry, "send_group_message", send)
    await handler.refresh_group_card(_Bot(fail_edit=True), session, snapshot)
    await session.commit()
    moved = (await raid_service.get_snapshot(session, session_id)).session
    assert moved.anchor_message_id == 88
    assert moved.group_id == group_registry.get_group_id()

    empty_id = await _running(session)
    await raid_service.advance_phase(session, empty_id, expected_phase=1)
    await session.commit()
    empty = await raid_service.advance_phase(session, empty_id, expected_phase=1)
    await session.commit()
    text, _ = handler.render_card(empty.snapshot)
    assert "nessuna risposta" in text


async def test_card_refresh_handles_already_current_and_total_delivery_failure(
    session, monkeypatch,
):
    session_id = await _running(session)
    snapshot = await raid_service.get_snapshot(session, session_id)

    class AlreadyCurrent(_Bot):
        async def edit_message_text(self, **kwargs):
            raise RuntimeError("Bad Request: message is not modified")

    async def must_not_send(*args, **kwargs):
        raise AssertionError("an already-current card must not be duplicated")

    monkeypatch.setattr(group_registry, "send_group_message", must_not_send)
    assert await handler.refresh_group_card(AlreadyCurrent(), session, snapshot)

    async def fail_send(*args, **kwargs):
        raise RuntimeError("telegram offline")

    monkeypatch.setattr(group_registry, "send_group_message", fail_send)
    assert not await handler.refresh_group_card(_Bot(fail_edit=True), session, snapshot)


async def test_vote_accepts_changes_and_rejects_wrong_group_or_stale_phase(session, monkeypatch):
    session_id = await _running(session)
    message = _Message(chat_id=-1001, chat_type="supergroup")
    callback = _Callback(message)
    data = RaidCb(action="vote", session_id=session_id, phase_no=1, tactic="a")

    monkeypatch.setattr(group_registry, "get_group_id", lambda: -2000)
    await handler.vote(callback, data, session)
    assert callback.answers[-1][1]

    monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)
    await handler.vote(callback, data, session)
    confirmation = callback.answers[-1][0]
    assert confirmation.startswith("✅ Scelta salvata:")
    assert "d20 della fase: 10" in confirmation
    assert "resta fisso se cambi tattica" in confirmation

    stale = RaidCb(action="vote", session_id=session_id, phase_no=2, tactic="a")
    await handler.vote(callback, stale, session)
    assert callback.answers[-1][1]


async def test_registry_views_start_advance_close_and_schedule(session, monkeypatch):
    spec, message, bot = RaidType(), _Message(), _Bot()
    ready = await _ready(session)
    await spec.start_creation(message, _State(), 9)
    assert (await spec.describe_scheduled(session, ready)).item_id == ready
    assert await spec.describe_scheduled(session, 99999) is None
    assert ready in [item_id for item_id, _ in await spec.schedulable_items(session)]

    await spec.render_list(message, session)
    assert "Raid:" in message.said[-1]
    await spec.render_detail(message, session, ready)
    assert "Avvia ora" in str(message.markups[-1])
    await spec.render_detail(message, session, 99999)
    assert "non trovato" in message.said[-1]

    monkeypatch.setattr(group_registry, "get_group_id", lambda: 0)
    assert not (await spec.start_now(bot, session, ready)).ok
    monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)

    async def send(*args, **kwargs):
        return _Sent(message_id=77)
    monkeypatch.setattr(group_registry, "send_group_message", send)
    assert (await spec.start_now(bot, session, ready)).ok
    await session.commit()
    assert [event.item_id for event in await spec.discover_open(session)] == [ready]
    assert not (await spec.start_now(bot, session, ready)).ok
    await spec.render_detail(message, session, ready)
    assert "Risolvi fase ora" in str(message.markups[-1])

    empty = await spec.advance_now(bot, session, ready)
    assert not empty.ok
    await raid_service.record_action(
        session, session_id=ready, phase_no=1, user_tg_id=1, tactic="a",
    )
    await session.commit()
    assert (await spec.advance_now(bot, session, ready)).ok
    await session.commit()
    assert (await spec.close_now(bot, session, ready)).ok
    assert not (await spec.close_now(bot, session, ready)).ok
    await spec.render_detail(message, session, ready)
    assert "Elimina" in str(message.markups[-1])


async def test_registry_failures_delete_and_scheduled_paths(session, monkeypatch):
    spec, bot = RaidType(), _Bot()
    ready = await _ready(session)
    monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)

    async def fail_send(*args, **kwargs):
        raise RuntimeError("telegram")
    monkeypatch.setattr(group_registry, "send_group_message", fail_send)
    assert "annunciare" in (await spec.start_now(bot, session, ready)).message
    task = ScheduledTask(
        task_type="raid", ref_id=ready, run_at=raid_service._now(), created_by_tg_id=9,
    )
    with pytest.raises(RuntimeError, match="annunciare"):
        await spec.execute_scheduled(bot, session, task, -1001)

    await spec.render_list(_Message(), session)

    deleted = await _ready(session)
    assert (await spec.delete(session, deleted)).ok
    assert not (await spec.delete(session, deleted)).ok

    task = ScheduledTask(task_type="raid", ref_id=None, run_at=raid_service._now(), created_by_tg_id=9)
    with pytest.raises(RuntimeError, match="ref_id"):
        await spec.execute_scheduled(bot, session, task, -1001)
    task.ref_id = ready
    task.payload_json = json.dumps({"action": "phase", "phase": "bad"})
    with pytest.raises(RuntimeError, match="non valido"):
        await spec.execute_scheduled(bot, session, task, -1001)

    async def send(*args, **kwargs):
        return _Sent(message_id=77)
    monkeypatch.setattr(group_registry, "send_group_message", send)
    task.payload_json = None
    await spec.execute_scheduled(bot, session, task, -1001)
    await session.commit()
    await raid_service.record_action(
        session, session_id=ready, phase_no=1, user_tg_id=1, tactic="a",
    )
    await session.commit()
    task.payload_json = json.dumps({"action": "phase", "phase": 1})
    await spec.execute_scheduled(bot, session, task, -1001)

    missing_refresh = ScheduledTask(
        task_type="raid", ref_id=99999, run_at=raid_service._now(), created_by_tg_id=9,
        payload_json=json.dumps({"action": "refresh", "attempt": 1, "internal": True}),
    )
    with pytest.raises(schedule_service.TaskSkip, match="non più disponibile"):
        await spec.execute_scheduled(bot, session, missing_refresh, -1001)

    async def fail_send_again(*args, **kwargs):
        raise RuntimeError("telegram offline")

    monkeypatch.setattr(group_registry, "send_group_message", fail_send_again)
    failed_bot = _Bot(fail_edit=True)
    retry = ScheduledTask(
        task_type="raid", ref_id=ready, run_at=raid_service._now(), created_by_tg_id=9,
        payload_json=json.dumps({"action": "refresh", "attempt": 1, "internal": True}),
    )
    await spec.execute_scheduled(failed_bot, session, retry, -1001)
    retry.payload_json = json.dumps({"action": "refresh", "attempt": 2, "internal": True})
    with pytest.raises(RuntimeError, match="3 tentativi"):
        await spec.execute_scheduled(failed_bot, session, retry, -1001)


@pytest.mark.parametrize("delete_fails", [False, True])
async def test_lost_start_removes_temporary_announcement(
    session, monkeypatch, delete_fails,
):
    spec, ready = RaidType(), await _ready(session)
    monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)
    sent = _Sent(message_id=77, fail_delete=delete_fails)

    async def send(*args, **kwargs):
        return sent

    async def lose(*args, **kwargs):
        return False

    monkeypatch.setattr(group_registry, "send_group_message", send)
    monkeypatch.setattr(raid_service, "start", lose)
    assert "già in corso" in (await spec.start_now(_Bot(), session, ready)).message
    assert sent.deleted is (not delete_fails)


async def test_empty_registry_list(session):
    message = _Message()
    await RaidType().render_list(message, session)
    assert "Nessun raid" in message.said[-1]
