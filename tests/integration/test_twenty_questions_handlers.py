"""Telegram/event-registry adapters for 20 Domande, driven with duck-typed fakes."""

from __future__ import annotations

import asyncio
import gc
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
import weakref

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from sqlalchemy import func, select

from database.models import AIGameSession, ScheduledTask, TwentyQuestionsGame
from handlers import events
from handlers import schedule as scheduler
from handlers import twenty_questions as handler
from handlers.callbacks import EventCb, TwentyQuestionsCreateCb
from handlers.event_types.twenty_questions_type import TwentyQuestionsType
from services import ai_game_service, group_registry, schedule_service
from services.ai_game_types import (
    FinishReason,
    GameView,
    PersonalQuota,
    QuestionClaim,
    QuestionStartResult,
    QuestionVerdict,
    RewardProjection,
    RewardSummary,
    StartGameResult,
    TerminalResult,
    TurnOutcome,
    TurnRejectReason,
    TurnResult,
    TwentyQuestionsPolicy,
)
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
        self.data = {}

    async def clear(self):
        self.value = None
        self.clears += 1
        self.data = {}

    async def set_state(self, value):
        self.value = value

    async def update_data(self, **values):
        self.data.update(values)

    async def get_data(self):
        return dict(self.data)


class _Message:
    def __init__(
        self,
        text="",
        *,
        group=-1001,
        anchor=77,
        user=42,
        bot=None,
        chat_type="supergroup",
    ):
        self.text = text
        self.from_user = SimpleNamespace(id=user)
        self.chat = SimpleNamespace(id=group, type=chat_type)
        self.reply_to_message = (
            SimpleNamespace(message_id=anchor) if anchor is not None else None
        )
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


class _OrderedSession:
    """Small transaction boundary double used only for handler sequencing."""

    def __init__(self, events):
        self.events = events
        self.pending = False
        self.fail_commit = False

    async def commit(self):
        self.events.append("commit")
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.pending = False

    async def rollback(self):
        self.events.append("rollback")
        self.pending = False


class _PublisherBot:
    def __init__(self, events, *, fail_edit=False):
        self.events = events
        self.fail_edit = fail_edit
        self.edits = []

    async def edit_message_text(self, **kwargs):
        assert not kwargs.pop("_pending", False)
        self.events.append("edit")
        if self.fail_edit:
            raise RuntimeError("not editable")
        self.edits.append(kwargs)


def _v2_view(*, anchor=77, status="running"):
    policy = TwentyQuestionsPolicy(2, 5, 2, 100, 3_000, 600, 2_000, 10)
    projection = RewardProjection(1, 0, 0, 100, 0, 100, 100, 0)
    return GameView(
        session_id=7,
        title="V2",
        status=status,
        group_id=-1001,
        anchor_message_id=anchor,
        expires_at=None,
        finish_reason=None,
        policy=policy,
        projection=projection,
        participant_count=1,
        question_count=0,
        wrong_guess_count=0,
        recent_turns=(),
        revealed_answer=None,
        winner_tg_id=None,
    )


def _terminal(*, anchor=77, winner=42):
    return TerminalResult(
        session_id=7,
        transitioned=True,
        finish_reason=FinishReason.victory,
        group_id=-1001,
        anchor_message_id=anchor,
        title="V2",
        answer="Portal 2",
        winner_tg_id=winner,
        reward=RewardSummary("settled", 1, 0, 0, 100, 0, 100, 100, 100, 0),
        allocations=(),
    )


def _v2_anchor_snapshot():
    return SimpleNamespace(
        session=SimpleNamespace(id=7),
        game=SimpleNamespace(rules_version=2),
    )


def _v1_anchor_snapshot():
    return SimpleNamespace(
        session=SimpleNamespace(id=7),
        game=SimpleNamespace(rules_version=1),
    )


class _Callback:
    def __init__(self, message):
        self.message = message
        self.from_user = message.from_user
        self.answers = 0

    async def answer(self, *args, **kwargs):
        self.answers += 1


class _EventCallback:
    """Callback double for the real generic event hub/TQ adapter boundary."""

    def __init__(self, bot=None):
        self.message = _Message(bot=bot)
        self.bot = self.message.bot
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


async def _ready(session, title="Serata"):
    root = AIGameSession(
        game_type="twentyq", title=title, creator_tg_id=9, status="ready",
    )
    session.add(root)
    await session.flush()
    session.add(TwentyQuestionsGame(
        session_id=root.id,
        catalog_key=TARGET.key,
        answer=TARGET.title,
        aliases_json='["portal two"]',
        dossier_json='{"facts": "Aperture"}',
        rules_version=1,
        question_limit=20,
        guess_limit=3,
    ))
    await session.commit()
    return root.id


async def _v2_ready(session, monkeypatch, title="V2"):
    """Create one real v2 draft while keeping provider availability deterministic."""
    monkeypatch.setattr(ai_game_service.settings, "twentyq_v2_enabled", True)
    monkeypatch.setattr(
        ai_game_service, "has_configured_twenty_questions_provider", lambda: True,
    )
    created = await ai_game_service.create_twenty_questions(
        session,
        creator_tg_id=9,
        title=title,
        duration_seconds=43_200,
        expires_at=None,
        max_coins_per_participant=100,
        target=TARGET,
    )
    await session.commit()
    return created.session_id


async def _running(session, title="Serata", anchor=77):
    session_id = await _ready(session, title)
    assert await ai_game_service.start(
        session, session_id, group_id=-1001, anchor_message_id=anchor,
    )
    await session.commit()
    return session_id


class TestCreationAndScreens:
    async def test_creation_prompt_validation_flag_and_preset_save(self, session, monkeypatch):
        state, message = _State(), _Message()
        monkeypatch.setattr(handler.settings, "twentyq_v2_enabled", False)
        await handler.start_creation(message, state, 9)
        assert state.value is None
        assert "manutenzione" in message.said[-1]

        monkeypatch.setattr(handler.settings, "twentyq_v2_enabled", True)
        await handler.start_creation(message, state, 9)
        assert state.value == handler.TwentyQuestionsCreateStates.title

        message.text = "x" * 121
        await handler.create_from_title(message, state)
        assert "1 a 120" in message.said[-1]

        message.text = "Serata <epica>"
        await handler.create_from_title(message, state)
        assert state.value == handler.TwentyQuestionsCreateStates.duration_choice
        assert (await session.execute(select(func.count(AIGameSession.id)))).scalar_one() == 0

        callback = _Callback(message)
        await handler.choose_creation_duration(
            callback,
            TwentyQuestionsCreateCb(action="duration", value=43_200),
            state,
        )
        assert state.value == handler.TwentyQuestionsCreateStates.coins_choice
        await handler.choose_creation_coins(
            callback,
            TwentyQuestionsCreateCb(action="coins_default"),
            state,
            session,
        )
        assert "Serata &lt;epica&gt;" in message.said[-1]
        assert state.value is None
        assert callback.answers == 2

    async def test_creation_absolute_custom_value_retries_without_a_partial_draft(
        self, session, monkeypatch,
    ):
        monkeypatch.setattr(handler.settings, "twentyq_v2_enabled", True)
        state, message = _State(), _Message()
        await handler.start_creation(message, state, 9)
        message.text = "Scadenza assoluta"
        await handler.create_from_title(message, state)
        callback = _Callback(message)
        await handler.choose_creation_absolute_expiry(callback, state)
        assert state.value == handler.TwentyQuestionsCreateStates.absolute_expiry

        message.text = "12h"
        await handler.receive_absolute_expiry(message, state)
        assert "absolute" in message.said[-1]
        assert state.value == handler.TwentyQuestionsCreateStates.absolute_expiry
        assert (await session.execute(select(func.count(AIGameSession.id)))).scalar_one() == 0

        message.text = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        await handler.receive_absolute_expiry(message, state)
        await handler.choose_creation_coins(
            callback,
            TwentyQuestionsCreateCb(action="coins_custom"),
            state,
            session,
        )
        assert state.value == handler.TwentyQuestionsCreateStates.custom_coins

        message.text = "0"
        await handler.receive_custom_coins(message, state, session)
        assert state.value == handler.TwentyQuestionsCreateStates.custom_coins
        message.text = "37"
        await handler.receive_custom_coins(message, state, session)
        assert state.value is None
        assert "37 CoInn" in message.said[-1]

    async def test_creation_can_be_cancelled_from_each_state(self):
        state, message = _State(), _Message()
        callback = _Callback(message)
        for creation_state in (
            handler.TwentyQuestionsCreateStates.title,
            handler.TwentyQuestionsCreateStates.duration_choice,
            handler.TwentyQuestionsCreateStates.absolute_expiry,
            handler.TwentyQuestionsCreateStates.coins_choice,
            handler.TwentyQuestionsCreateStates.custom_coins,
        ):
            await state.set_state(creation_state)
            await handler.cancel_creation(callback, state)
            assert state.value is None
        assert "annullata" in message.said[-1]
        assert callback.answers == 5

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

    async def test_tq_list_closes_its_read_transaction_before_telegram(
        self, session, monkeypatch,
    ):
        await _v2_ready(session, monkeypatch, "Safe list")
        message = _Message()
        original_edit = message.edit_text
        original_answer = message.answer

        async def edit_text(*args, **kwargs):
            assert not session.in_transaction()
            return await original_edit(*args, **kwargs)

        async def answer(*args, **kwargs):
            assert not session.in_transaction()
            return await original_answer(*args, **kwargs)

        message.edit_text = edit_text
        message.answer = answer
        await TwentyQuestionsType().render_list(message, session)

    async def test_tq_detail_closes_found_and_missing_reads_before_telegram(
        self, session, monkeypatch,
    ):
        session_id = await _v2_ready(session, monkeypatch, "Safe detail")
        message = _Message()
        original_edit = message.edit_text
        original_answer = message.answer

        async def edit_text(*args, **kwargs):
            assert not session.in_transaction()
            return await original_edit(*args, **kwargs)

        async def answer(*args, **kwargs):
            assert not session.in_transaction()
            return await original_answer(*args, **kwargs)

        message.edit_text = edit_text
        message.answer = answer
        spec = TwentyQuestionsType()
        await spec.render_detail(message, session, session_id)
        await spec.render_detail(message, session, 999_999)

    async def test_empty_list_and_delete_contract(self, session):
        spec, message = TwentyQuestionsType(), _Message()
        await spec.render_list(message, session)
        assert "Nessuna partita" in message.said[-1]
        session_id = await _ready(session)
        assert (await spec.delete(session, session_id)).ok
        assert not (await spec.delete(session, session_id)).ok

    async def test_running_anchorless_v2_detail_offers_republish(self, session, monkeypatch):
        session_id = await _v2_ready(session, monkeypatch, "Da ripubblicare")
        assert (await ai_game_service.start(session, session_id, group_id=-1001)).started
        await session.commit()

        message = _Message()
        await TwentyQuestionsType().render_detail(message, session, session_id)

        assert "Ripubblica card" in str(message.markups[-1])
        assert EventCb(action="start", task_type="twentyq", item_id=session_id).pack() \
            in str(message.markups[-1])


class TestPublicSecretGameCommand:
    async def test_private_command_only_shows_public_rules(self, session):
        message = _Message(group=42, anchor=None, chat_type="private")

        await handler.cmd_gioco_alduino(message, session)

        assert "Il gioco segreto di Alduino" in message.said[-1]
        assert TARGET.title not in message.said[-1]

    async def test_group_without_a_running_game_shows_only_rules(self, session):
        message = _Message(anchor=None)
        original_answer = message.answer

        async def answer(*args, **kwargs):
            assert not session.in_transaction()
            return await original_answer(*args, **kwargs)

        message.answer = answer
        await handler.cmd_gioco_alduino(message, session)

        assert "Il gioco segreto di Alduino" in message.said[-1]
        assert "Per te" not in message.said[-1]
        assert TARGET.title not in message.said[-1]

    async def test_group_reply_selects_its_anchor_without_exposing_the_secret(
        self, session, monkeypatch,
    ):
        older = await _v2_ready(session, monkeypatch, "Prima partita")
        desired = await _v2_ready(session, monkeypatch, "Partita scelta")
        assert (await ai_game_service.start(session, older, group_id=-1001)).started
        assert await ai_game_service.move_anchor_if_current(
            session, older, expected_message_id=None, new_message_id=66,
        )
        assert (await ai_game_service.start(session, desired, group_id=-1001)).started
        assert await ai_game_service.move_anchor_if_current(
            session, desired, expected_message_id=None, new_message_id=77,
        )
        await session.commit()
        message = _Message(anchor=77)
        original_answer = message.answer

        async def answer(*args, **kwargs):
            assert not session.in_transaction()
            return await original_answer(*args, **kwargs)

        message.answer = answer
        await handler.cmd_gioco_alduino(message, session)

        assert "Partita scelta" in message.said[-1]
        assert "Prima partita" not in message.said[-1]
        assert TARGET.title not in message.said[-1]
        assert "Per te" in message.said[-1]
        assert "anche <b>1</b> altre partite" in message.said[-1]

    async def test_group_without_a_reply_uses_most_recently_started_game_and_counts_alternatives(
        self, session, monkeypatch,
    ):
        created_first = await _v2_ready(session, monkeypatch, "Creata prima")
        created_last = await _v2_ready(session, monkeypatch, "Creata dopo")
        assert (await ai_game_service.start(
            session,
            created_last,
            group_id=-1001,
            now=datetime(2030, 1, 1, 10, 0),
        )).started
        assert await ai_game_service.move_anchor_if_current(
            session, created_last, expected_message_id=None, new_message_id=66,
        )
        assert (await ai_game_service.start(
            session,
            created_first,
            group_id=-1001,
            now=datetime(2030, 1, 1, 11, 0),
        )).started
        assert await ai_game_service.move_anchor_if_current(
            session, created_first, expected_message_id=None, new_message_id=77,
        )
        await session.commit()
        message = _Message(anchor=None)

        await handler.cmd_gioco_alduino(message, session)

        assert "Creata prima" in message.said[-1]
        assert "Creata dopo" not in message.said[-1]
        assert "anche <b>1</b> altre partite" in message.said[-1]

    async def test_anchorless_running_command_republishes_after_read_commit(self, session, monkeypatch):
        session_id = await _v2_ready(session, monkeypatch, "Recupera card")
        assert (await ai_game_service.start(session, session_id, group_id=-1001)).started
        await session.commit()
        message = _Message(anchor=None)
        calls = []

        async def refresh(bot, db_session, view, *, strict=False):
            assert not db_session.in_transaction()
            assert view.revealed_answer is None
            calls.append((view.session_id, strict))

        async def forbidden_start(*args, **kwargs):
            raise AssertionError("public recovery must not start or extend a session")

        monkeypatch.setattr(handler, "refresh_group_card", refresh)
        monkeypatch.setattr(ai_game_service, "start", forbidden_start)
        await handler.cmd_gioco_alduino(message, session)

        assert calls == [(session_id, False)]
        assert "Recupera card" in message.said[-1]


class TestEventLifecycle:
    async def test_legacy_start_closes_snapshot_read_before_telegram(
        self, session, monkeypatch,
    ):
        spec, session_id = TwentyQuestionsType(), await _ready(session, "Legacy tx")
        monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)

        async def send(*args, **kwargs):
            assert not session.in_transaction()
            return _Sent()

        monkeypatch.setattr(group_registry, "send_group_message", send)

        result = await spec.start_now(_Bot(), session, session_id)

        assert result.ok

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


class TestV2EventLifecycle:
    @staticmethod
    def _wire_real_type(monkeypatch) -> TwentyQuestionsType:
        spec = TwentyQuestionsType()
        monkeypatch.setattr(
            events.event_types,
            "get",
            lambda key: spec if key == "twentyq" else None,
        )
        monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)
        return spec

    async def test_real_v2_start_send_failure_reports_republish_after_durable_start(
        self, session, monkeypatch,
    ):
        self._wire_real_type(monkeypatch)
        session_id = await _v2_ready(session, monkeypatch, "Send fails")

        async def fail_send(*args, **kwargs):
            raise RuntimeError("telegram unavailable")

        monkeypatch.setattr(group_registry, "send_group_message", fail_send)
        callback = _EventCallback()
        data = EventCb(action="start", task_type="twentyq", item_id=session_id)

        await events.cb_start_now(callback, data, session)

        status = (await session.execute(
            select(AIGameSession.status).where(AIGameSession.id == session_id)
        )).scalar_one()
        assert status == "running"
        assert callback.answers == [("⚠️ Stato salvato, ma la card va ripubblicata.", True)]

    async def test_real_v2_start_reread_failure_reports_republish_after_durable_start(
        self, session, monkeypatch,
    ):
        self._wire_real_type(monkeypatch)
        session_id = await _v2_ready(session, monkeypatch, "Reread fails")

        async def fail_reread(*args, **kwargs):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(ai_game_service, "get_game_view", fail_reread)
        callback = _EventCallback()
        data = EventCb(action="start", task_type="twentyq", item_id=session_id)

        await events.cb_start_now(callback, data, session)

        status = (await session.execute(
            select(AIGameSession.status).where(AIGameSession.id == session_id)
        )).scalar_one()
        assert status == "running"
        assert callback.answers == [("⚠️ Stato salvato, ma la card va ripubblicata.", True)]

    async def test_real_v2_start_cas_failure_cleans_orphan_and_reports_republish(
        self, session, monkeypatch,
    ):
        self._wire_real_type(monkeypatch)
        session_id = await _v2_ready(session, monkeypatch, "CAS fails")
        sent = _Sent(message_id=88)

        async def send(*args, **kwargs):
            return sent

        async def fail_cas(*args, **kwargs):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(group_registry, "send_group_message", send)
        monkeypatch.setattr(ai_game_service, "move_anchor_if_current", fail_cas)
        callback = _EventCallback()
        data = EventCb(action="start", task_type="twentyq", item_id=session_id)

        await events.cb_start_now(callback, data, session)

        stored = (await session.execute(
            select(AIGameSession.status, AIGameSession.anchor_message_id).where(
                AIGameSession.id == session_id,
            )
        )).one()
        assert stored == ("running", None)
        assert sent.deleted
        assert callback.answers == [("⚠️ Stato salvato, ma la card va ripubblicata.", True)]

    async def test_real_v2_close_send_failure_reports_republish_after_durable_settlement(
        self, session, monkeypatch,
    ):
        self._wire_real_type(monkeypatch)
        session_id = await _v2_ready(session, monkeypatch, "Close send fails")
        started = await ai_game_service.start(session, session_id, group_id=-1001)
        assert started.started
        await session.commit()

        async def fail_send(*args, **kwargs):
            raise RuntimeError("telegram unavailable")

        monkeypatch.setattr(group_registry, "send_group_message", fail_send)
        callback = _EventCallback()
        data = EventCb(action="close", task_type="twentyq", item_id=session_id)

        await events.cb_close(callback, data, session)

        stored = (await session.execute(
            select(AIGameSession.status, AIGameSession.finish_reason).where(
                AIGameSession.id == session_id,
            )
        )).one()
        assert stored == ("finished", FinishReason.admin_closed.value)
        assert callback.answers == [("⚠️ Stato salvato, ma la card va ripubblicata.", True)]

    async def test_v2_start_commits_before_reread_and_never_passes_snapshot_to_publisher(
        self, monkeypatch,
    ):
        """The initial card is post-commit and takes the v2 GameView-only path."""
        events = []
        db_session = _OrderedSession(events)
        snapshot = SimpleNamespace(
            session=SimpleNamespace(id=7, status="ready"),
            game=SimpleNamespace(rules_version=2),
        )

        async def get_snapshot(*args, **kwargs):
            db_session.pending = True
            events.append("snapshot")
            return snapshot

        async def start(*args, **kwargs):
            assert db_session.pending
            events.append("start")
            return StartGameResult(True, None, None)

        async def get_game_view(*args, **kwargs):
            assert not db_session.pending
            events.append("view")
            db_session.pending = True
            return _v2_view(anchor=None)

        async def refresh(bot, session, view, *, strict=False):
            assert not db_session.pending
            assert isinstance(view, GameView)
            assert strict
            events.append("refresh")

        async def sent_before_commit(*args, **kwargs):
            raise AssertionError("v2 start sent Telegram before its caller commit")

        monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)
        monkeypatch.setattr(group_registry, "send_group_message", sent_before_commit)
        monkeypatch.setattr(ai_game_service, "get_snapshot", get_snapshot)
        monkeypatch.setattr(ai_game_service, "start", start)
        monkeypatch.setattr(ai_game_service, "get_game_view", get_game_view)
        monkeypatch.setattr(handler, "refresh_group_card", refresh)

        result = await TwentyQuestionsType().start_now(_Bot(), db_session, 7)

        assert result.ok and result.post_commit is not None
        assert events == ["snapshot", "start"]
        await db_session.commit()
        await result.post_commit()
        assert events == ["snapshot", "start", "commit", "view", "commit", "refresh"]

    async def test_v2_null_anchor_recovery_does_not_start_or_schedule_twice(
        self, session, monkeypatch,
    ):
        """A committed start that lost Telegram can be repaired without a second expiry."""
        spec = TwentyQuestionsType()
        session_id = await _v2_ready(session, monkeypatch)
        published = []

        async def send(*args, **kwargs):
            published.append(kwargs.get("text", ""))
            return _Sent(message_id=88)

        monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)
        monkeypatch.setattr(group_registry, "send_group_message", send)

        first = await spec.start_now(_Bot(), session, session_id)
        assert first.ok and first.post_commit is not None
        assert published == []
        await session.commit()
        initial_timers = (await session.execute(
            select(func.count()).select_from(ScheduledTask).where(
                ScheduledTask.task_type == "twentyq", ScheduledTask.ref_id == session_id,
            )
        )).scalar_one()
        await session.commit()

        recovery = await spec.start_now(_Bot(), session, session_id)
        assert recovery.ok and recovery.post_commit is not None
        assert published == []
        duplicate_timers = (await session.execute(
            select(func.count()).select_from(ScheduledTask).where(
                ScheduledTask.task_type == "twentyq", ScheduledTask.ref_id == session_id,
            )
        )).scalar_one()
        assert duplicate_timers == initial_timers == 1
        await session.commit()

        await recovery.post_commit()
        assert len(published) == 1
        assert (await session.execute(
            select(AIGameSession.anchor_message_id).where(AIGameSession.id == session_id)
        )).scalar_one() == 88

    async def test_v2_close_terminalizes_before_its_post_commit_publisher(self, monkeypatch):
        """`finish()` would skip settlement; terminal publication must wait for commit."""
        events = []
        db_session = _OrderedSession(events)
        snapshot = SimpleNamespace(
            session=SimpleNamespace(id=7, status="running"),
            game=SimpleNamespace(rules_version=2),
        )
        terminal = _terminal()

        async def get_snapshot(*args, **kwargs):
            db_session.pending = True
            events.append("snapshot")
            return snapshot

        async def terminalize(session, *, session_id, reason):
            assert db_session.pending
            assert session_id == 7 and reason is FinishReason.admin_closed
            events.append("terminalize")
            return terminal

        async def finish(*args, **kwargs):
            raise AssertionError("v2 close must never call legacy finish()")

        async def publish(bot, session, result, *, strict=False):
            assert not db_session.pending
            assert result is terminal
            assert strict
            events.append("publish")

        monkeypatch.setattr(ai_game_service, "get_snapshot", get_snapshot)
        monkeypatch.setattr(ai_game_service, "terminalize", terminalize)
        monkeypatch.setattr(ai_game_service, "finish", finish)
        monkeypatch.setattr(handler, "publish_terminal", publish)

        result = await TwentyQuestionsType().close_now(_Bot(), db_session, 7)

        assert result is not None and result.ok and result.post_commit is not None
        assert events == ["snapshot", "terminalize"]
        await db_session.commit()
        await result.post_commit()
        assert events == ["snapshot", "terminalize", "commit", "publish"]

    async def test_v2_scheduled_start_close_expire_return_hooks_and_skip_duplicates(
        self, session, monkeypatch,
    ):
        """Every lifecycle action returns post-commit presentation; terminal replay is a skip."""
        spec = TwentyQuestionsType()
        session_id = await _v2_ready(session, monkeypatch, "Sched start")
        calls = []

        async def refresh(bot, db_session, view, *, strict=False):
            assert strict
            calls.append(("start", view.session_id))

        async def publish(bot, db_session, terminal, *, strict=False):
            assert strict
            calls.append((terminal.finish_reason.value, terminal.session_id))

        monkeypatch.setattr(handler, "refresh_group_card", refresh)
        monkeypatch.setattr(handler, "publish_terminal", publish)
        monkeypatch.setattr(group_registry, "get_group_id", lambda: -1001)

        start_task = SimpleNamespace(ref_id=session_id, payload_json=None)
        start_hook = await spec.execute_scheduled(_Bot(), session, start_task, -1001)
        assert start_hook is not None
        await session.commit()
        await start_hook()
        assert calls == [("start", session_id)]

        close_id = await _v2_ready(session, monkeypatch, "Sched close")
        started_close = await ai_game_service.start(session, close_id, group_id=-1001)
        assert started_close.started
        await session.commit()
        close_task = SimpleNamespace(ref_id=close_id, payload_json='{"action":"close"}')
        close_hook = await spec.execute_scheduled(_Bot(), session, close_task, -1001)
        assert close_hook is not None
        await session.commit()
        await close_hook()
        assert calls[-1] == (FinishReason.admin_closed.value, close_id)
        with pytest.raises(schedule_service.TaskSkip):
            await spec.execute_scheduled(_Bot(), session, close_task, -1001)
        await session.rollback()

        expire_id = await _v2_ready(session, monkeypatch, "Sched expire")
        started_expire = await ai_game_service.start(session, expire_id, group_id=-1001)
        assert started_expire.started
        await session.commit()
        expiry_task = SimpleNamespace(
            ref_id=expire_id, payload_json='{"internal":true,"action":"expire"}',
        )
        expiry_hook = await spec.execute_scheduled(_Bot(), session, expiry_task, -1001)
        assert expiry_hook is not None
        await session.commit()
        await expiry_hook()
        assert calls[-1] == (FinishReason.expired.value, expire_id)
        with pytest.raises(schedule_service.TaskSkip):
            await spec.execute_scheduled(_Bot(), session, expiry_task, -1001)
        await session.rollback()

    async def test_current_expiry_cancelled_by_terminalize_finishes_done_in_scheduler(
        self, session, monkeypatch,
    ):
        """Terminalization cancels every pending timer, including the executing one;
        scheduler ownership must still leave that current row durably ``done``.
        """
        session_id = await _v2_ready(session, monkeypatch, "Current expiry")
        started = await ai_game_service.start(session, session_id, group_id=-1001)
        assert started.started
        await session.commit()
        task = (await session.execute(
            select(ScheduledTask).where(
                ScheduledTask.task_type == "twentyq",
                ScheduledTask.ref_id == session_id,
                ScheduledTask.status == "pending",
            )
        )).scalar_one()
        task_id = task.id
        await session.commit()
        published: list[tuple[int, str]] = []

        async def execute(bot, db_session, current_task):
            return await TwentyQuestionsType().execute_scheduled(
                bot, db_session, current_task, -1001,
            )

        async def publish(bot, db_session, terminal, *, strict=False):
            assert strict
            assert not db_session.in_transaction(), "terminal publisher ran before commit"
            published.append((terminal.session_id, terminal.finish_reason.value))

        async def notify(*args, **kwargs):
            raise AssertionError("successful internal expiry must not notify its creator")

        monkeypatch.setattr(scheduler, "execute_task", execute)
        monkeypatch.setattr(scheduler, "_notify_creator", notify)
        monkeypatch.setattr(handler, "publish_terminal", publish)

        await scheduler._run_due_task(_Bot(), session, task)

        stored_status = (await session.execute(
            select(ScheduledTask.status).where(ScheduledTask.id == task_id)
        )).scalar_one()
        game_status = (await session.execute(
            select(AIGameSession.status).where(AIGameSession.id == session_id)
        )).scalar_one()
        assert stored_status == "done"
        assert game_status == "finished"
        assert published == [(session_id, FinishReason.expired.value)]

    async def test_v2_delete_archive_and_legacy_delete_contract(self, session, monkeypatch):
        """Finished v2 history is hidden, never deleted; historical v1 stays deletable."""
        spec, message = TwentyQuestionsType(), _Message()
        ready_id = await _v2_ready(session, monkeypatch, "Ready v2")
        assert (await spec.delete(session, ready_id)).ok
        await session.commit()

        running_id = await _v2_ready(session, monkeypatch, "Running v2")
        started = await ai_game_service.start(session, running_id, group_id=-1001)
        assert started.started
        await session.commit()
        assert not (await spec.delete(session, running_id)).ok
        await session.rollback()

        terminal = await ai_game_service.terminalize(
            session, session_id=running_id, reason=FinishReason.admin_closed,
        )
        assert terminal.transitioned
        await session.commit()
        await spec.render_detail(message, session, running_id)
        detail = message.said[-1].casefold()
        assert "archivia" in detail or "nascondi" in detail
        assert EventCb(action="askarchive", task_type="twentyq", item_id=running_id).pack() \
            in str(message.markups[-1])
        assert (await spec.archive(session, running_id)).ok
        await session.commit()
        assert (await session.execute(
            select(AIGameSession.archived_at).where(AIGameSession.id == running_id)
        )).scalar_one() is not None

        legacy_id = await _ready(session, "Finished v1")
        assert await ai_game_service.start(
            session, legacy_id, group_id=-1001, anchor_message_id=77,
        )
        await session.commit()
        assert await ai_game_service.finish(session, legacy_id)
        await session.commit()
        await spec.render_detail(message, session, legacy_id)
        assert EventCb(action="askdel", task_type="twentyq", item_id=legacy_id).pack() \
            in str(message.markups[-1])
        assert (await spec.delete(session, legacy_id)).ok


class _Provider:
    async def generate_json(self, request):
        return SimpleNamespace(value={"verdetto": "si"})


class _BrokenProvider:
    async def generate_json(self, request):
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
        assert message.said[-1] == "🐲 <b>SÌ</b>"
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


class TestV2PostCommitPresentation:
    async def test_strict_live_reread_rolls_back_before_propagating(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)

        async def fail_reread(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(ai_game_service, "get_game_view", fail_reread)

        with pytest.raises(handler.CardPublicationError, match="rilettura"):
            await handler.refresh_group_card(
                _PublisherBot(events), db_session, _v2_view(), strict=True,
            )

        assert events == ["view", "rollback"]
        assert not db_session.pending

    async def test_v1_invalid_input_rolls_back_anchor_read_before_reply(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)

        async def find(*args, **kwargs):
            db_session.pending = True
            events.append("find")
            return _v1_anchor_snapshot()

        async def claim(*args, **kwargs):
            raise AssertionError("invalid input must not claim a v1 turn")

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "claim_turn", claim)
        message = _Message(" ")

        async def reply(text, **kwargs):
            assert not db_session.pending
            assert "1 e 500" in text
            events.append("reply")

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == ["find", "rollback", "reply"]

    async def test_v1_empty_answer_rolls_back_anchor_read_before_reply(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)

        async def find(*args, **kwargs):
            db_session.pending = True
            events.append("find")
            return _v1_anchor_snapshot()

        async def claim(*args, **kwargs):
            raise AssertionError("an empty answer must not claim a v1 turn")

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "claim_turn", claim)
        message = _Message("RISPOSTA:")

        async def reply(text, **kwargs):
            assert not db_session.pending
            assert "dopo" in text
            events.append("reply")

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == ["find", "rollback", "reply"]

    async def test_v1_busy_lease_rolls_back_claim_before_reply(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)

        async def find(*args, **kwargs):
            db_session.pending = True
            events.append("find")
            return _v1_anchor_snapshot()

        async def claim(*args, **kwargs):
            assert db_session.pending
            events.append("claim")
            return None

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "claim_turn", claim)
        message = _Message("RISPOSTA: Portal 2")

        async def reply(text, **kwargs):
            assert not db_session.pending
            assert "già rispondendo" in text
            events.append("reply")

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == ["find", "claim", "rollback", "reply"]

    async def test_card_publish_lock_serializes_concurrent_same_session_callers(self):
        session_id = 10_001
        lock = handler._card_publish_lock(session_id)
        assert handler._card_publish_lock(session_id) is lock
        await lock.acquire()
        entered = asyncio.Event()

        async def contender():
            async with handler._card_publish_lock(session_id):
                entered.set()

        task = asyncio.create_task(contender())
        try:
            await asyncio.sleep(0)
            assert not entered.is_set()
        finally:
            lock.release()
        await task

        assert entered.is_set()

    def test_card_publish_lock_registry_evicts_unused_lock_after_collection(self):
        session_id = 10_002
        lock = handler._card_publish_lock(session_id)
        lock_ref = weakref.ref(lock)
        assert handler._card_publish_locks.get(session_id) is lock

        del lock
        gc.collect()

        assert lock_ref() is None
        assert handler._card_publish_locks.get(session_id) is None

    async def test_question_commits_before_network_reply_and_refresh(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        quota = PersonalQuota(0, 5, 0, 2, False)
        claim = QuestionClaim(7, "token", 42, "Domanda?", "domanda", "a" * 64, '"d"', ())
        started = QuestionStartResult(7, TurnOutcome.claimed, None, quota, claim=claim)
        completed = TurnResult(
            7,
            TurnOutcome.recorded,
            None,
            PersonalQuota(1, 4, 0, 2, True),
            verdict=QuestionVerdict.si,
        )

        async def find(*args, **kwargs):
            return _v2_anchor_snapshot()

        async def begin(*args, **kwargs):
            db_session.pending = True
            return started

        async def classify(*args, **kwargs):
            assert not db_session.pending
            events.append("network")
            return SimpleNamespace(value=QuestionVerdict.si)

        async def complete(*args, **kwargs):
            db_session.pending = True
            return completed

        async def latest(*args, **kwargs):
            assert not db_session.pending
            events.append("view")
            db_session.pending = True
            return _v2_view()

        async def refresh(*args, **kwargs):
            assert not db_session.pending
            events.append("refresh")

        async def legacy_claim(*args, **kwargs):
            raise AssertionError("v2 invoked legacy claim_turn")

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "begin_question", begin)
        monkeypatch.setattr(ai_game_service, "classify_question", classify)
        monkeypatch.setattr(ai_game_service, "complete_question", complete)
        monkeypatch.setattr(ai_game_service, "get_game_view", latest)
        monkeypatch.setattr(ai_game_service, "claim_turn", legacy_claim)
        monkeypatch.setattr(handler, "refresh_group_card", refresh)
        monkeypatch.setattr(
            handler,
            "GeminiStructuredProvider",
            lambda: (_ for _ in ()).throw(AssertionError("v2 constructed Gemini")),
        )
        message = _Message("Domanda?")

        async def reply(text, **kwargs):
            assert not db_session.pending
            events.append("reply")
            message.said.append(text)

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == ["commit", "network", "commit", "reply", "view", "commit", "refresh"]

    async def test_reused_question_replies_after_commit_without_refresh(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        result = QuestionStartResult(
            7,
            TurnOutcome.reused,
            None,
            PersonalQuota(1, 4, 0, 2, True),
            cached_verdict=QuestionVerdict.no,
        )

        async def find(*args, **kwargs):
            return _v2_anchor_snapshot()

        async def begin(*args, **kwargs):
            db_session.pending = True
            return result

        async def refresh(*args, **kwargs):
            raise AssertionError("reused question must not refresh the public card")

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "begin_question", begin)
        monkeypatch.setattr(handler, "refresh_group_card", refresh)
        message = _Message("La domanda già fatta?")

        async def reply(text, **kwargs):
            assert not db_session.pending
            events.append("reply")
            assert "già fatta" in text

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == ["commit", "reply"]

    async def test_terminal_rejection_commits_before_terminal_publish_and_reply(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        result = QuestionStartResult(
            7,
            TurnOutcome.rejected,
            TurnRejectReason.expired,
            PersonalQuota(0, 5, 0, 2, False),
            terminal=_terminal(winner=None),
        )

        async def find(*args, **kwargs):
            return _v2_anchor_snapshot()

        async def begin(*args, **kwargs):
            db_session.pending = True
            return result

        async def publish(*args, **kwargs):
            assert not db_session.pending
            events.append("publish")

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "begin_question", begin)
        monkeypatch.setattr(handler, "publish_terminal", publish)
        message = _Message(" ")

        async def reply(text, **kwargs):
            assert not db_session.pending
            events.append("reply")

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == ["commit", "publish", "reply"]

    async def test_provider_failure_commits_abandon_before_reply_without_refresh(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        claim = QuestionClaim(7, "token", 42, "Domanda?", "domanda", "a" * 64, '"d"', ())
        started = QuestionStartResult(
            7,
            TurnOutcome.claimed,
            None,
            PersonalQuota(0, 5, 0, 2, False),
            claim=claim,
        )
        failed = TurnResult(
            7,
            TurnOutcome.rejected,
            TurnRejectReason.providers_unavailable,
            PersonalQuota(0, 5, 0, 2, False),
        )

        async def find(*args, **kwargs):
            return _v2_anchor_snapshot()

        async def begin(*args, **kwargs):
            db_session.pending = True
            return started

        async def classify(*args, **kwargs):
            assert not db_session.pending
            events.append("network")
            raise StructuredAIError("down")

        async def abandon(*args, **kwargs):
            db_session.pending = True
            events.append("abandon")
            return failed

        async def refresh(*args, **kwargs):
            events.append("refresh")

        async def legacy_claim(*args, **kwargs):
            raise AssertionError("v2 invoked legacy claim_turn")

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "begin_question", begin)
        monkeypatch.setattr(ai_game_service, "classify_question", classify)
        monkeypatch.setattr(ai_game_service, "abandon_claim", abandon)
        monkeypatch.setattr(ai_game_service, "claim_turn", legacy_claim)
        monkeypatch.setattr(handler, "refresh_group_card", refresh)
        message = _Message("Domanda?")

        async def reply(text, **kwargs):
            assert not db_session.pending
            events.append("reply")

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == ["commit", "network", "abandon", "commit", "reply"]

    async def test_guess_commits_before_success_reply_and_terminal_publish(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        result = TurnResult(
            7,
            TurnOutcome.recorded,
            None,
            PersonalQuota(0, 5, 1, 1, True),
            correct=True,
            terminal=_terminal(),
        )

        async def find(*args, **kwargs):
            return _v2_anchor_snapshot()

        async def submit(*args, **kwargs):
            db_session.pending = True
            events.append("submit")
            return result

        async def publish(*args, **kwargs):
            assert not db_session.pending
            events.append("publish")

        async def legacy_claim(*args, **kwargs):
            raise AssertionError("v2 invoked legacy claim_turn")

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "submit_guess", submit)
        monkeypatch.setattr(ai_game_service, "claim_turn", legacy_claim)
        monkeypatch.setattr(handler, "publish_terminal", publish)
        message = _Message("RISPOSTA: Portal 2")

        async def reply(text, **kwargs):
            assert not db_session.pending
            events.append("reply")

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == ["submit", "commit", "reply", "publish"]

    async def test_recorded_guess_closes_read_before_refreshing_live_card(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        result = TurnResult(
            7,
            TurnOutcome.recorded,
            None,
            PersonalQuota(0, 5, 1, 1, True),
            correct=False,
        )

        async def find(*args, **kwargs):
            return _v2_anchor_snapshot()

        async def submit(*args, **kwargs):
            db_session.pending = True
            events.append("submit")
            return result

        async def latest(*args, **kwargs):
            assert not db_session.pending
            events.append("view")
            db_session.pending = True
            return _v2_view()

        async def refresh(*args, **kwargs):
            assert not db_session.pending
            events.append("refresh")

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "submit_guess", submit)
        monkeypatch.setattr(ai_game_service, "get_game_view", latest)
        monkeypatch.setattr(handler, "refresh_group_card", refresh)
        message = _Message("RISPOSTA: non è Portal 2")

        async def reply(text, **kwargs):
            assert not db_session.pending
            events.append("reply")

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == ["submit", "commit", "reply", "view", "commit", "refresh"]

    async def test_guess_commit_failure_rolls_back_without_reply_or_publish(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        db_session.fail_commit = True
        result = TurnResult(
            7,
            TurnOutcome.recorded,
            None,
            PersonalQuota(0, 5, 1, 1, True),
            correct=True,
            terminal=_terminal(),
        )

        async def find(*args, **kwargs):
            return _v2_anchor_snapshot()

        async def submit(*args, **kwargs):
            db_session.pending = True
            return result

        async def publish(*args, **kwargs):
            events.append("publish")

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "submit_guess", submit)
        monkeypatch.setattr(handler, "publish_terminal", publish)
        message = _Message("RISPOSTA: Portal 2")

        async def reply(text, **kwargs):
            events.append("reply")

        message.reply = reply
        with pytest.raises(RuntimeError, match="commit failed"):
            await handler.play_turn(message, db_session)

        assert events == ["commit", "rollback"]

    async def test_initial_null_anchor_sends_then_cas_commits(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        sent = _Sent(message_id=88)

        async def latest(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            return _v2_view(anchor=None)

        async def send(bot, session, text, **kwargs):
            assert not db_session.pending
            events.append("send")
            return sent

        async def cas(session, session_id, *, expected_message_id, new_message_id):
            assert expected_message_id is None
            assert new_message_id == 88
            db_session.pending = True
            events.append("cas")
            return True

        monkeypatch.setattr(group_registry, "send_group_message", send)
        monkeypatch.setattr(ai_game_service, "get_game_view", latest)
        monkeypatch.setattr(ai_game_service, "move_anchor_if_current", cas)
        await handler.refresh_group_card(_PublisherBot(events), db_session, _v2_view(anchor=None))

        assert events == ["view", "commit", "send", "cas", "commit"]
        assert not sent.deleted

    async def test_live_refresh_rereads_and_skips_a_terminal_state_before_telegram(
        self, monkeypatch,
    ):
        events = []
        db_session = _OrderedSession(events)

        async def latest(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            return _v2_view(status="finished")

        monkeypatch.setattr(ai_game_service, "get_game_view", latest)

        await handler.refresh_group_card(_PublisherBot(events), db_session, _v2_view())

        assert events == ["view", "commit"]

    async def test_terminal_without_anchor_reuses_send_cas_without_terminalizing(
        self, monkeypatch,
    ):
        events = []
        db_session = _OrderedSession(events)
        sent = _Sent(message_id=88)

        async def latest(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            return _v2_view(anchor=None, status="finished")

        async def send(bot, session, text, **kwargs):
            assert not db_session.pending
            events.append("send")
            return sent

        async def cas(session, session_id, *, expected_message_id, new_message_id):
            assert expected_message_id is None
            assert new_message_id == 88
            db_session.pending = True
            events.append("cas")
            return True

        async def terminalize(*args, **kwargs):
            raise AssertionError("terminal publisher must not settle again")

        monkeypatch.setattr(group_registry, "send_group_message", send)
        monkeypatch.setattr(ai_game_service, "get_game_view", latest)
        monkeypatch.setattr(ai_game_service, "move_anchor_if_current", cas)
        monkeypatch.setattr(ai_game_service, "terminalize", terminalize)
        await handler.publish_terminal(_PublisherBot(events), db_session, _terminal(anchor=None, winner=None))

        assert events == ["commit", "view", "commit", "send", "cas", "commit"]
        assert not sent.deleted

    async def test_edit_failure_falls_back_to_cas_and_loser_deletes_only_its_orphan(
        self, monkeypatch,
    ):
        events = []
        db_session = _OrderedSession(events)
        sent = _Sent(message_id=88)

        async def latest(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            return _v2_view(anchor=77)

        async def send(bot, session, text, **kwargs):
            assert not db_session.pending
            events.append("send")
            return sent

        async def cas(session, session_id, *, expected_message_id, new_message_id):
            assert expected_message_id == 77
            assert new_message_id == 88
            db_session.pending = True
            events.append("cas")
            return False

        async def delete():
            assert not db_session.pending
            events.append("delete-orphan")
            sent.deleted = True

        sent.delete = delete
        monkeypatch.setattr(group_registry, "send_group_message", send)
        monkeypatch.setattr(ai_game_service, "get_game_view", latest)
        monkeypatch.setattr(ai_game_service, "move_anchor_if_current", cas)
        await handler.refresh_group_card(
            _PublisherBot(events, fail_edit=True), db_session, _v2_view(anchor=77),
        )

        assert events == [
            "view", "commit", "edit", "send", "cas", "commit", "delete-orphan",
        ]
        assert sent.deleted

    async def test_strict_cas_loser_accepts_an_existing_winning_anchor(
        self, monkeypatch,
    ):
        """CAS loss is concurrent success when reread finds another durable anchor."""
        events = []
        db_session = _OrderedSession(events)
        sent = _Sent(message_id=88)
        views = iter((_v2_view(anchor=None), _v2_view(anchor=99)))

        async def latest(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            return next(views)

        async def send(*args, **kwargs):
            assert not db_session.pending
            events.append("send")
            return sent

        async def lose_cas(*args, **kwargs):
            db_session.pending = True
            events.append("cas")
            return False

        monkeypatch.setattr(ai_game_service, "get_game_view", latest)
        monkeypatch.setattr(group_registry, "send_group_message", send)
        monkeypatch.setattr(ai_game_service, "move_anchor_if_current", lose_cas)

        await handler.refresh_group_card(
            _PublisherBot(events), db_session, _v2_view(anchor=None), strict=True,
        )

        assert sent.deleted
        assert events == [
            "view", "commit", "send", "cas", "commit", "view", "commit",
        ]

    async def test_refresh_send_failure_is_best_effort_after_a_committed_game_state(
        self, monkeypatch,
    ):
        events = []
        db_session = _OrderedSession(events)

        async def latest(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            return _v2_view()

        async def send(*args, **kwargs):
            raise RuntimeError("telegram unavailable")

        monkeypatch.setattr(group_registry, "send_group_message", send)
        monkeypatch.setattr(ai_game_service, "get_game_view", latest)

        await handler.refresh_group_card(_PublisherBot(events, fail_edit=True), db_session, _v2_view())

        assert events == ["view", "commit", "edit"]

    async def test_terminal_mention_is_resolved_and_committed_before_telegram(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        bot = _PublisherBot(events)

        async def mention(session, tg_id):
            assert tg_id == 42
            db_session.pending = True
            events.append("mention")
            return '<a href="tg://user?id=42">Aldo &amp; Lea</a>'

        async def latest(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            return _v2_view(status="finished")

        original_edit = bot.edit_message_text

        async def edit(**kwargs):
            assert not db_session.pending
            assert "Aldo &amp; Lea" in kwargs["text"]
            await original_edit(**kwargs)

        bot.edit_message_text = edit
        monkeypatch.setattr(handler._mentions, "mention", mention)
        monkeypatch.setattr(ai_game_service, "get_game_view", latest)
        await handler.publish_terminal(bot, db_session, _terminal(winner=42))

        assert events == ["mention", "commit", "view", "commit", "edit"]

    async def test_terminal_rereads_current_anchor_before_telegram(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        bot = _PublisherBot(events)

        async def latest(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            return _v2_view(anchor=88, status="finished")

        original_edit = bot.edit_message_text

        async def edit(**kwargs):
            assert not db_session.pending
            assert kwargs["message_id"] == 88
            await original_edit(**kwargs)

        bot.edit_message_text = edit
        monkeypatch.setattr(ai_game_service, "get_game_view", latest)
        await handler.publish_terminal(bot, db_session, _terminal(anchor=77, winner=None))

        assert events == ["commit", "view", "commit", "edit"]

    async def test_terminal_mention_lookup_failure_rolls_back_then_publishes_generic_card(
        self, monkeypatch,
    ):
        events = []
        db_session = _OrderedSession(events)
        bot = _PublisherBot(events)

        async def mention(session, tg_id):
            db_session.pending = True
            events.append("mention")
            raise RuntimeError("lookup unavailable")

        async def latest(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            return _v2_view(status="finished")

        original_edit = bot.edit_message_text

        async def edit(**kwargs):
            assert not db_session.pending
            assert "un partecipante" in kwargs["text"]
            await original_edit(**kwargs)

        bot.edit_message_text = edit
        monkeypatch.setattr(handler._mentions, "mention", mention)
        monkeypatch.setattr(ai_game_service, "get_game_view", latest)
        await handler.publish_terminal(bot, db_session, _terminal(winner=42))

        assert events == ["mention", "rollback", "view", "commit", "edit"]

    async def test_missing_winner_user_uses_safe_existing_mention_fallback(self, session):
        events = []
        bot = _PublisherBot(events)
        result = _terminal(winner=987654)

        await handler.publish_terminal(bot, session, result)

        assert bot.edits
        assert 'href="tg://user?id=987654">giocatore</a>' in bot.edits[-1]["text"]

    async def test_terminal_without_winner_skips_lookup_and_still_publishes(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)

        async def mention(*args, **kwargs):
            raise AssertionError("winner-less terminal must not query a mention")

        async def latest(*args, **kwargs):
            db_session.pending = True
            events.append("view")
            return _v2_view(status="finished")

        monkeypatch.setattr(handler._mentions, "mention", mention)
        monkeypatch.setattr(ai_game_service, "get_game_view", latest)
        await handler.publish_terminal(_PublisherBot(events), db_session, _terminal(winner=None))

        assert events == ["commit", "view", "commit", "edit"]

    async def test_v1_snapshot_read_commits_before_legacy_card_network(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        root = AIGameSession(
            id=7,
            game_type="twentyq",
            title="Legacy",
            creator_tg_id=1,
            status="running",
            next_turn_no=1,
            group_id=-1001,
            anchor_message_id=77,
        )
        game = TwentyQuestionsGame(
            session_id=7,
            catalog_key="legacy",
            answer="Portal 2",
            aliases_json="[]",
            dossier_json="{}",
            question_limit=20,
            guess_limit=3,
            questions_used=0,
            guesses_used=0,
            rules_version=1,
        )
        snapshot = ai_game_service.GameSnapshot(root, game, ())

        async def find(*args, **kwargs):
            return snapshot

        async def claim(*args, **kwargs):
            db_session.pending = True
            events.append("claim")
            return "legacy-token"

        async def record(*args, **kwargs):
            db_session.pending = True
            events.append("record")
            return True

        async def fresh(*args, **kwargs):
            assert not db_session.pending
            db_session.pending = True
            events.append("snapshot")
            return snapshot

        class Bot:
            async def edit_message_text(self, **kwargs):
                assert not db_session.pending
                events.append("edit")

            async def send_message(self, *args, **kwargs):
                assert not db_session.pending
                events.append("send")
                return _Sent()

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "claim_turn", claim)
        monkeypatch.setattr(ai_game_service, "record_guess", record)
        monkeypatch.setattr(ai_game_service, "guess_is_correct", lambda *args: False)
        monkeypatch.setattr(ai_game_service, "get_snapshot", fresh)
        message = _Message("RISPOSTA: Portal 2", bot=Bot())

        async def reply(text, **kwargs):
            assert not db_session.pending
            events.append("reply")

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == [
            "claim", "commit", "record", "commit", "reply", "snapshot", "commit", "edit",
            "commit",
        ]

    async def test_v1_legacy_fallback_commits_its_new_anchor_after_send(self, monkeypatch):
        events = []
        db_session = _OrderedSession(events)
        root = AIGameSession(
            id=7,
            game_type="twentyq",
            title="Legacy",
            creator_tg_id=1,
            status="running",
            next_turn_no=1,
            group_id=-1001,
            anchor_message_id=77,
        )
        game = TwentyQuestionsGame(
            session_id=7,
            catalog_key="legacy",
            answer="Portal 2",
            aliases_json="[]",
            dossier_json="{}",
            question_limit=20,
            guess_limit=3,
            questions_used=0,
            guesses_used=0,
            rules_version=1,
        )
        snapshot = ai_game_service.GameSnapshot(root, game, ())

        async def find(*args, **kwargs):
            return snapshot

        async def claim(*args, **kwargs):
            db_session.pending = True
            events.append("claim")
            return "legacy-token"

        async def record(*args, **kwargs):
            db_session.pending = True
            events.append("record")
            return True

        async def fresh(*args, **kwargs):
            db_session.pending = True
            events.append("snapshot")
            return snapshot

        async def move_anchor(*args, **kwargs):
            db_session.pending = True
            events.append("move-anchor")

        class Bot:
            async def edit_message_text(self, **kwargs):
                assert not db_session.pending
                events.append("edit")
                raise RuntimeError("anchor deleted")

            async def send_message(self, *args, **kwargs):
                assert not db_session.pending
                events.append("send")
                return _Sent(message_id=88)

        monkeypatch.setattr(ai_game_service, "find_by_anchor", find)
        monkeypatch.setattr(ai_game_service, "claim_turn", claim)
        monkeypatch.setattr(ai_game_service, "record_guess", record)
        monkeypatch.setattr(ai_game_service, "guess_is_correct", lambda *args: False)
        monkeypatch.setattr(ai_game_service, "get_snapshot", fresh)
        monkeypatch.setattr(ai_game_service, "move_anchor", move_anchor)
        message = _Message("RISPOSTA: Portal 2", bot=Bot())

        async def reply(text, **kwargs):
            assert not db_session.pending
            events.append("reply")

        message.reply = reply
        await handler.play_turn(message, db_session)

        assert events == [
            "claim", "commit", "record", "commit", "reply", "snapshot", "commit",
            "edit", "send", "move-anchor", "commit",
        ]

    async def test_v1_running_game_still_uses_the_isolated_legacy_path(self, session, monkeypatch):
        await _running(session)
        monkeypatch.setattr(handler, "GeminiStructuredProvider", _Provider)
        message = _Message("È in prima persona?")

        await handler.play_turn(message, session)

        assert message.said[-1] == "🐲 <b>SÌ</b>"


async def test_tq_type_post_commit_hooks_commit_before_publish_and_roll_back_on_failure(monkeypatch):
    """A hook must never publish from an open read transaction or hide a failed reread."""
    event_log: list[str] = []
    session = _OrderedSession(event_log)
    event_type = TwentyQuestionsType()

    async def missing_view(_session, _item_id):
        return None

    monkeypatch.setattr(ai_game_service, "get_game_view", missing_view)
    with pytest.raises(RuntimeError, match="non piu' pubblicabile"):
        await event_type._start_hook(_Bot(), session, 7)()
    assert event_log == ["commit"]

    async def broken_refresh(_bot, _session, _snapshot):
        raise RuntimeError("network")

    monkeypatch.setattr(handler, "refresh_group_card", broken_refresh)
    with pytest.raises(RuntimeError, match="network"):
        await event_type._legacy_refresh_hook(_Bot(), session, _v1_anchor_snapshot())()
    assert event_log == ["commit", "rollback"]


async def test_tq_type_delegates_creation_and_rejects_unavailable_open_paths(monkeypatch):
    """The registry adapter must preserve the handler entry point and fail closed."""
    event_type = TwentyQuestionsType()
    called: list[tuple[object, object, int]] = []

    async def create(message, state, creator_id):
        called.append((message, state, creator_id))

    monkeypatch.setattr(handler, "start_creation", create)
    message, state = _Message(), _State()
    await event_type.start_creation(message, state, 42)
    assert called == [(message, state, 42)]

    async def no_snapshot(_session, _item_id):
        return None

    monkeypatch.setattr(ai_game_service, "get_snapshot", no_snapshot)
    unavailable = await event_type._open(_Bot(), _OrderedSession([]), 7, group_id=-1001)
    assert (unavailable.ok, unavailable.alert) == (False, True)


async def test_tq_type_scheduled_unknown_and_stale_actions_are_distinguished(monkeypatch):
    """A bad schedule fails loudly while a stale one becomes an idempotent skip."""
    event_type = TwentyQuestionsType()
    task = SimpleNamespace(ref_id=7)
    session = _OrderedSession([])

    monkeypatch.setattr(schedule_service, "task_payload", lambda _task: {"action": "other"})
    with pytest.raises(RuntimeError, match="non supportata"):
        await event_type.execute_scheduled(_Bot(), session, task, -1001)

    monkeypatch.setattr(schedule_service, "task_payload", lambda _task: {"action": "expire"})

    async def stale(_session, _item_id):
        return None

    monkeypatch.setattr(ai_game_service, "get_snapshot", stale)
    with pytest.raises(schedule_service.TaskSkip, match="non piu' in corso"):
        await event_type.execute_scheduled(_Bot(), session, task, -1001)


async def test_tq_creation_guards_reject_disabled_invalid_duration_and_bad_coins(monkeypatch):
    """FSM input errors must stop before any game service or transaction mutation."""
    state = _State()
    message = _Message("Titolo")
    monkeypatch.setattr(handler.settings, "twentyq_v2_enabled", False)
    await handler.start_creation(message, state, 42)
    assert "manutenzione" in message.said[-1]

    await handler.create_from_title(message, state)
    assert state.clears >= 2

    monkeypatch.setattr(handler.settings, "twentyq_v2_enabled", True)
    callback = _Callback(_Message())
    await handler.choose_creation_duration(
        callback, SimpleNamespace(value=-1), state,
    )
    assert callback.answers == 1

    bad_coins = _Message("non un numero")
    await handler.receive_custom_coins(bad_coins, state, _OrderedSession([]))
    assert "numero intero" in bad_coins.said[-1]


async def test_tq_creation_and_public_lookup_fail_closed_without_service_work(monkeypatch):
    """Lost FSM data or a failed public read must leave the chat with a safe recovery message."""
    state = _State()
    message = _Message()
    monkeypatch.setattr(handler.settings, "twentyq_v2_enabled", True)
    await handler._finish_creation(
        message, state, _OrderedSession([]), creator_id=42, max_coins_per_participant=100,
    )
    assert "non sono più disponibili" in message.said[-1]

    async def broken_list(_session):
        raise RuntimeError("db")

    monkeypatch.setattr(ai_game_service, "list_manageable", broken_list)
    session = _OrderedSession([])
    assert await handler._group_game_status(_Message(), session) == (None, None, 0)
    assert session.events == ["rollback"]


async def test_tq_card_cleanup_and_strict_missing_group_are_explicit(monkeypatch):
    """A publisher cannot hide an orphan cleanup failure or pretend an unknown group is valid."""
    assert not await handler._delete_orphan(_Sent(fail_delete=True), session_id=7)

    session = _OrderedSession([])
    with pytest.raises(handler.CardPublicationError, match="gruppo"):
        await handler._publish_rendered_card(
            _Bot(), session, session_id=7, group_id=None, anchor_message_id=None,
            text="card", strict=True,
        )
    assert session.events == ["rollback"]


async def test_tq_type_v2_and_legacy_non_success_paths_are_explicit(monkeypatch):
    """Registry callers need distinct outcomes for stale, misconfigured, and lost terminal races."""
    event_type = TwentyQuestionsType()
    session = _OrderedSession([])

    def snapshot(status="ready", *, anchor=None, version=2):
        return SimpleNamespace(
            session=SimpleNamespace(status=status, anchor_message_id=anchor),
            game=SimpleNamespace(rules_version=version),
        )

    async def get_running(*_args):
        return snapshot("running", anchor=77)

    monkeypatch.setattr(ai_game_service, "get_snapshot", get_running)
    already = await event_type._open(_Bot(), session, 7, group_id=-1001)
    assert (already.ok, already.alert) == (False, True)

    async def get_finished(*_args):
        return snapshot("finished")

    monkeypatch.setattr(ai_game_service, "get_snapshot", get_finished)
    unavailable = await event_type._open(_Bot(), session, 7, group_id=-1001)
    assert (unavailable.ok, unavailable.alert) == (False, True)

    async def get_ready(*_args):
        return snapshot("ready")

    monkeypatch.setattr(ai_game_service, "get_snapshot", get_ready)
    missing_group = await event_type._open(_Bot(), session, 7, group_id=0)
    assert "GROUP_ID" in missing_group.message

    async def start_refused(*_args, **_kwargs):
        return SimpleNamespace(started=False, reason=SimpleNamespace(value="providers_unavailable"))

    monkeypatch.setattr(ai_game_service, "start", start_refused)
    refused = await event_type._open(_Bot(), session, 7, group_id=-1001)
    assert "Provider" in refused.message

    task = SimpleNamespace(ref_id=7)
    monkeypatch.setattr(schedule_service, "task_payload", lambda _task: {"action": "start"})
    monkeypatch.setattr(ai_game_service, "get_snapshot", get_finished)
    with pytest.raises(schedule_service.TaskSkip):
        await event_type.execute_scheduled(_Bot(), session, task, -1001)

    async def terminal_not_claimed(*_args, **_kwargs):
        return SimpleNamespace(transitioned=False)

    monkeypatch.setattr(ai_game_service, "get_snapshot", get_running)
    monkeypatch.setattr(ai_game_service, "terminalize", terminal_not_claimed)
    closed = await event_type.close_now(_Bot(), session, 7)
    assert closed is not None and not closed.ok

    async def legacy_running(*_args):
        return snapshot("running", version=1)

    async def cannot_finish(*_args):
        return False

    monkeypatch.setattr(ai_game_service, "get_snapshot", legacy_running)
    monkeypatch.setattr(ai_game_service, "finish", cannot_finish)
    legacy = await event_type.close_now(_Bot(), session, 7)
    assert legacy is not None and not legacy.ok


async def test_tq_finish_creation_preserves_or_clears_fsm_on_the_right_failures(monkeypatch):
    """Maintenance clears stale FSM data; persistence failures preserve it for an explicit retry."""
    data = {"title": "Serata", "duration_seconds": 3_600, "expires_at": None}
    state = _State()
    state.data = data.copy()
    message = _Message()
    monkeypatch.setattr(handler.settings, "twentyq_v2_enabled", False)
    await handler._finish_creation(message, state, _OrderedSession([]), creator_id=42, max_coins_per_participant=100)
    assert state.clears == 1 and "manutenzione" in message.said[-1]

    monkeypatch.setattr(handler.settings, "twentyq_v2_enabled", True)
    state.data = data.copy()

    async def domain_failure(*_args, **_kwargs):
        raise handler.GameCreationError("policy")

    monkeypatch.setattr(ai_game_service, "create_twenty_questions", domain_failure)
    events: list[str] = []
    await handler._finish_creation(message, state, _OrderedSession(events), creator_id=42, max_coins_per_participant=100)
    assert events == ["rollback"] and state.data == data
    assert "Puoi riprovare" in message.said[-1]

    async def storage_failure(*_args, **_kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(ai_game_service, "create_twenty_questions", storage_failure)
    events.clear()
    await handler._finish_creation(message, state, _OrderedSession(events), creator_id=42, max_coins_per_participant=100)
    assert events == ["rollback"] and state.data == data


async def test_tq_card_recovery_failure_modes_are_not_silently_successful(monkeypatch):
    """Failed rereads/CAS races must roll back and make strict publishers report the recovery failure."""
    session = _OrderedSession([])

    async def broken_view(*_args):
        raise RuntimeError("db")

    monkeypatch.setattr(ai_game_service, "get_game_view", broken_view)
    with pytest.raises(handler.CardPublicationError, match="card concorrente"):
        await handler._has_winning_anchor(session, session_id=7)
    assert session.events == ["rollback"]

    async def broken_move(*_args, **_kwargs):
        raise RuntimeError("cas")

    monkeypatch.setattr(ai_game_service, "move_anchor_if_current", broken_move)
    with pytest.raises(handler.CardPublicationError, match="anchor"):
        await handler._move_sent_anchor(
            session, session_id=7, expected_message_id=None, sent=_Sent(), strict=True,
        )

    monkeypatch.setattr(ai_game_service, "get_game_view", broken_view)
    with pytest.raises(handler.CardPublicationError, match="rilettura"):
        await handler.refresh_group_card(_Bot(), session, _v2_view(), strict=True)

    async def missing_view(*_args):
        return None

    monkeypatch.setattr(ai_game_service, "get_game_view", missing_view)
    with pytest.raises(handler.CardPublicationError, match="non disponibile"):
        await handler.refresh_group_card(_Bot(), session, _v2_view(), strict=True)


async def test_tq_non_strict_card_recovery_returns_cleanly_for_every_stale_state(monkeypatch):
    """Best-effort refreshes must not leak an open transaction when their durable view vanished."""
    session = _OrderedSession([])

    async def missing(*_args):
        return None

    monkeypatch.setattr(ai_game_service, "get_game_view", missing)
    await handler._publish_rendered_card(
        _Bot(), session, session_id=7, group_id=None, anchor_message_id=None, text="card",
    )
    await handler.refresh_group_card(_Bot(), session, _v2_view())

    async def terminal(*_args):
        return _v2_view(status="finished")

    monkeypatch.setattr(ai_game_service, "get_game_view", terminal)
    await handler.refresh_group_card(_Bot(), session, _v2_view())
    assert session.events == ["commit", "commit"]


async def test_tq_anchor_loser_requires_a_winner_and_cleans_its_orphan(monkeypatch):
    """A CAS loser without a durable winner is a publication error, not a successful refresh."""
    session = _OrderedSession([])

    async def lost(*_args, **_kwargs):
        return False

    async def no_winner(*_args):
        return None

    monkeypatch.setattr(ai_game_service, "move_anchor_if_current", lost)
    monkeypatch.setattr(ai_game_service, "get_game_view", no_winner)
    orphan = _Sent()
    with pytest.raises(handler.CardPublicationError, match="nessuna card"):
        await handler._move_sent_anchor(
            session, session_id=7, expected_message_id=None, sent=orphan, strict=True,
        )
    assert orphan.deleted


async def test_tq_v2_question_and_guess_missing_followup_paths_do_not_publish(monkeypatch):
    """An impossible claimed-without-lease and a missing refreshed DTO are contained locally."""
    session = _OrderedSession([])
    message = _Message("Domanda")
    quota = PersonalQuota(0, 5, 0, 2, False)

    async def impossible_claim(*_args, **_kwargs):
        return QuestionStartResult(7, TurnOutcome.claimed, None, quota, claim=None)

    monkeypatch.setattr(ai_game_service, "begin_question", impossible_claim)
    with pytest.raises(RuntimeError, match="claim missing"):
        await handler._play_turn_v2(message, session, session_id=7)

    async def recorded_guess(*_args, **_kwargs):
        return TurnResult(7, TurnOutcome.recorded, None, quota, correct=False)

    async def missing_view(*_args):
        return None

    monkeypatch.setattr(ai_game_service, "submit_guess", recorded_guess)
    monkeypatch.setattr(ai_game_service, "get_game_view", missing_view)
    await handler._play_v2_guess(message, session, session_id=7, answer="Portal")
    assert session.events == ["commit", "commit"]
