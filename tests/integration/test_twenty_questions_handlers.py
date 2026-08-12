"""Telegram/event-registry adapters for 20 Domande, driven with duck-typed fakes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from database.models import ScheduledTask
from handlers import twenty_questions as handler
from handlers.event_types.twenty_questions_type import TwentyQuestionsType
from services import ai_game_service, group_registry
from services.structured_ai import StructuredAIError
from services.twenty_questions_catalog import GameDossier

TARGET = GameDossier(
    "portal_2", "Portal 2", ("portal two",),
    "Puzzle game in prima persona di Valve ambientato nei laboratori Aperture Science. "
    "Chell usa una portal gun; GLaDOS e Wheatley sono personaggi centrali. Include il cooperativo.",
)


class _State:
    def __init__(self):
        self.value = None
        self.clears = 0

    async def clear(self):
        self.value = None
        self.clears += 1

    async def set_state(self, value):
        self.value = value


class _Message:
    def __init__(self, text="", *, group=-1001, anchor=77, user=42, bot=None):
        self.text = text
        self.from_user = SimpleNamespace(id=user)
        self.chat = SimpleNamespace(id=group, type="supergroup")
        self.reply_to_message = SimpleNamespace(message_id=anchor)
        self.bot = bot or _Bot()
        self.said = []
        self.markups = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.said.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=999)

    async def reply(self, text, **kwargs):
        self.said.append(text)

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


class _Sent:
    def __init__(self, message_id=77, fail_delete=False):
        self.message_id = message_id
        self.fail_delete = fail_delete
        self.deleted = False

    async def delete(self):
        if self.fail_delete:
            raise RuntimeError("old")
        self.deleted = True


class _Callback:
    def __init__(self, message):
        self.message = message
        self.answers = 0

    async def answer(self):
        self.answers += 1


async def _ready(session, title="Serata"):
    root = await ai_game_service.create_twenty_questions(
        session, creator_tg_id=9, title=title, target=TARGET,
    )
    await session.commit()
    return root.id


async def _running(session, title="Serata", anchor=77):
    session_id = await _ready(session, title)
    assert await ai_game_service.start(
        session, session_id, group_id=-1001, anchor_message_id=anchor,
    )
    await session.commit()
    return session_id


class TestCreationAndScreens:
    async def test_creation_prompt_validation_and_save(self, session):
        state, message = _State(), _Message()
        await handler.start_creation(message, state, 9)
        assert state.value == handler.TwentyQuestionsCreateStates.title

        message.text = "x" * 121
        await handler.create_from_title(message, state, session)
        assert "1 a 120" in message.said[-1]

        message.text = "Serata <epica>"
        await handler.create_from_title(message, state, session)
        assert "Serata &lt;epica&gt;" in message.said[-1]
        assert state.value is None

    async def test_creation_can_be_cancelled(self):
        state, message = _State(), _Message()
        await handler.start_creation(message, state, 9)
        callback = _Callback(message)
        await handler.cancel_creation(callback, state)
        assert state.value is None
        assert "annullata" in message.said[-1]
        assert callback.answers == 1

    async def test_registry_views_and_capabilities(self, session):
        spec, message = TwentyQuestionsType(), _Message()
        ready_id = await _ready(session, "Pronta")
        running_id = await _running(session, "Live")

        assert (await spec.describe_scheduled(session, ready_id)).title == "Pronta"
        assert await spec.describe_scheduled(session, 99999) is None
        assert [event.item_id for event in await spec.discover_open(session)] == [running_id]
        assert (ready_id, "Pronta") in await spec.schedulable_items(session)

        await spec.render_list(message, session)
        assert "Pronta" in message.said[-1] and "Live" in message.said[-1]
        await spec.render_detail(message, session, ready_id)
        assert "Segreto admin" in message.said[-1] and "Avvia ora" in str(message.markups[-1])
        await spec.render_detail(message, session, running_id)
        assert "Chiudi" in str(message.markups[-1])
        await spec.render_detail(message, session, 99999)
        assert "non trovata" in message.said[-1]

    async def test_empty_list_and_delete_contract(self, session):
        spec, message = TwentyQuestionsType(), _Message()
        await spec.render_list(message, session)
        assert "Nessuna partita" in message.said[-1]
        session_id = await _ready(session)
        assert (await spec.delete(session, session_id)).ok
        assert not (await spec.delete(session, session_id)).ok


class TestEventLifecycle:
    async def test_open_requires_group_and_recovers_announcement_failure(
        self, session, monkeypatch,
    ):
        spec, session_id = TwentyQuestionsType(), await _ready(session)
        monkeypatch.setattr(group_registry, "get_group_id", lambda: 0)
        assert not (await spec.start_now(_Bot(), session, session_id)).ok

        monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)
        async def fail(*args, **kwargs):
            raise RuntimeError("telegram")
        monkeypatch.setattr(group_registry, "send_group_message", fail)
        assert "annunciare" in (await spec.start_now(_Bot(), session, session_id)).message

    async def test_open_close_and_already_started(self, session, monkeypatch):
        spec, session_id = TwentyQuestionsType(), await _ready(session)
        sent = _Sent()
        monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)
        async def send(*args, **kwargs):
            return sent
        monkeypatch.setattr(group_registry, "send_group_message", send)

        assert (await spec.start_now(_Bot(), session, session_id)).ok
        await session.commit()
        assert not (await spec.start_now(_Bot(), session, session_id)).ok
        assert (await spec.close_now(_Bot(), session, session_id)).ok
        assert not (await spec.close_now(_Bot(), session, session_id)).ok

    async def test_lost_start_deletes_or_tolerates_old_announcement(
        self, session, monkeypatch,
    ):
        spec, session_id = TwentyQuestionsType(), await _ready(session)
        monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)
        sent = _Sent(fail_delete=True)
        async def send(*args, **kwargs):
            return sent
        async def lose(*args, **kwargs):
            return False
        monkeypatch.setattr(group_registry, "send_group_message", send)
        monkeypatch.setattr(ai_game_service, "start", lose)
        assert "già" in (await spec.start_now(_Bot(), session, session_id)).message

    async def test_scheduled_start_close_and_invalid_task(self, session, monkeypatch):
        spec, session_id = TwentyQuestionsType(), await _ready(session)
        monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)
        async def send(*args, **kwargs):
            return _Sent()
        monkeypatch.setattr(group_registry, "send_group_message", send)
        task = ScheduledTask(
            task_type="twentyq", ref_id=session_id, run_at=ai_game_service._now(),
            created_by_tg_id=9, status="pending",
        )
        await spec.execute_scheduled(_Bot(), session, task, -1001)
        await session.commit()
        task.payload_json = json.dumps({"action": "close"})
        await spec.execute_scheduled(_Bot(), session, task, -1001)

        task.ref_id = None
        with pytest.raises(RuntimeError, match="ref_id"):
            await spec.execute_scheduled(_Bot(), session, task, -1001)


class _Provider:
    async def generate_json(self, **kwargs):
        return {"verdetto": "si", "risposta": "Sì, proprio così."}


class _BrokenProvider:
    async def generate_json(self, **kwargs):
        raise StructuredAIError("down")


class TestPlayHandler:
    async def test_non_anchor_is_skipped_and_bad_inputs_do_not_claim(self, session):
        with pytest.raises(SkipHandler):
            await handler.play_turn(_Message("Domanda?"), session)
        await _running(session)
        short = _Message(" " * 2)
        await handler.play_turn(short, session)
        assert "1 e 500" in short.said[-1]
        empty_guess = _Message("RISPOSTA:")
        await handler.play_turn(empty_guess, session)
        assert "dopo" in empty_guess.said[-1]

    async def test_question_success_and_provider_failure(self, session, monkeypatch):
        await _running(session)
        monkeypatch.setattr(handler, "GeminiStructuredProvider", _Provider)
        message = _Message("È in prima persona?")
        await handler.play_turn(message, session)
        assert "SÌ" in message.said[-1]
        assert message.bot.edits

        second_id = await _running(session, "Seconda", anchor=78)
        second = await ai_game_service.get_snapshot(session, second_id)
        monkeypatch.setattr(handler, "GeminiStructuredProvider", _BrokenProvider)
        failed = _Message("Ha il multiplayer?", anchor=second.session.anchor_message_id)
        await handler.play_turn(failed, session)
        assert "non è stata consumata" in failed.said[-1]
        assert (await ai_game_service.get_snapshot(session, second_id)).game.questions_used == 0

    async def test_guess_busy_and_record_refusal_paths(self, session, monkeypatch):
        session_id = await _running(session)
        busy_token = await ai_game_service.claim_turn(session, session_id)
        await session.commit()
        busy = _Message("RISPOSTA: Portal 2")
        await handler.play_turn(busy, session)
        assert "già rispondendo" in busy.said[-1]
        await ai_game_service.release_turn(session, session_id, busy_token)
        await session.commit()

        async def refuse(*args, **kwargs):
            return False
        monkeypatch.setattr(ai_game_service, "record_guess", refuse)
        refused = _Message("RISPOSTA: sbagliato")
        await handler.play_turn(refused, session)
        assert "non è più disponibile" in refused.said[-1]

    async def test_correct_guess_reveals_and_refresh_fallback_moves_anchor(
        self, session, monkeypatch,
    ):
        session_id = await _running(session)
        sent = _Sent(message_id=88)
        async def send(*args, **kwargs):
            return sent
        monkeypatch.setattr(group_registry, "send_group_message", send)
        message = _Message("RISPOSTA: portal two", bot=_Bot(fail_edit=True))
        await handler.play_turn(message, session)
        assert "Preso" in message.said[-1]
        snapshot = await ai_game_service.get_snapshot(session, session_id)
        assert snapshot.session.anchor_message_id == 88
        assert snapshot.session.status == "finished"
