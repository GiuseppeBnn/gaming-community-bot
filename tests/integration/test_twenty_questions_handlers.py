"""Telegram/event-registry adapters for 20 Domande, driven with duck-typed fakes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from database.models import AIGameSession, ScheduledTask, TwentyQuestionsGame
from handlers import twenty_questions as handler
from handlers.event_types.twenty_questions_type import TwentyQuestionsType
from services import ai_game_service, group_registry
from services.ai_game_types import (
    FinishReason,
    GameView,
    PersonalQuota,
    QuestionClaim,
    QuestionStartResult,
    QuestionVerdict,
    RewardProjection,
    RewardSummary,
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


class _Callback:
    def __init__(self, message):
        self.message = message
        self.answers = 0

    async def answer(self):
        self.answers += 1


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


async def _running(session, title="Serata", anchor=77):
    session_id = await _ready(session, title)
    assert await ai_game_service.start(
        session, session_id, group_id=-1001, anchor_message_id=anchor,
    )
    await session.commit()
    return session_id


class TestCreationAndScreens:
    async def test_creation_prompt_validation_flag_and_save(self, session, monkeypatch):
        state, message = _State(), _Message()
        await handler.start_creation(message, state, 9)
        assert state.value == handler.TwentyQuestionsCreateStates.title

        message.text = "x" * 121
        await handler.create_from_title(message, state, session)
        assert "1 a 120" in message.said[-1]

        message.text = "Serata <epica>"
        await handler.create_from_title(message, state, session)
        assert "manutenzione" in message.said[-1]
        assert state.value == handler.TwentyQuestionsCreateStates.title

        monkeypatch.setattr(handler.settings, "twentyq_v2_enabled", True)
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
