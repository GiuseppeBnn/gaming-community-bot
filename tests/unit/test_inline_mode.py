"""Public inline event discovery: only live events and real future starts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import BettingEvent, PollTemplate, Quiz, QuizQuestion, ScheduledTask
from handlers import event_types
import handlers.inline_mode as inline_mode


class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(username="test_bot")


class _FakeInlineQuery:
    def __init__(self, text: str = ""):
        self.from_user = SimpleNamespace(id=111, username="cercante", full_name="Cerco")
        self.query = text
        self.bot = _FakeBot()
        self.answers = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)


@pytest.fixture(autouse=True)
def _registry():
    event_types.clear()
    event_types.register_builtin()
    yield
    event_types.clear()


async def _call(text: str, session):
    query = _FakeInlineQuery(text)
    await inline_mode.public_events(query, session)
    return query.answers[0]


async def _seed(session):
    live = Quiz(title="Live <b>", description="", creator_tg_id=1, status="running")
    live.questions.append(QuizQuestion(
        text="Q?", options_json='["a", "b"]', correct_option_id=0, position=0,
    ))
    ready = Quiz(title="Domani", description="", creator_tg_id=1, status="ready")
    ready.questions.append(QuizQuestion(
        text="Q?", options_json='["a", "b"]', correct_option_id=0, position=0,
    ))
    closed = BettingEvent(
        title="Vecchia", description="", creator_tg_id=1, status="resolved",
    )
    poll = PollTemplate(
        question="Serata giochi?", options_json='["Sì", "No"]',
        creator_tg_id=1, status="ready",
    )
    session.add_all([live, ready, closed, poll])
    await session.flush()
    tomorrow = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    session.add_all([
        ScheduledTask(
            task_type="quiz", ref_id=ready.id, run_at=tomorrow,
            status="pending", created_by_tg_id=1,
        ),
        ScheduledTask(
            task_type="poll", ref_id=poll.id, run_at=tomorrow + timedelta(hours=1),
            status="pending", created_by_tg_id=1,
        ),
        ScheduledTask(
            task_type="bet", ref_id=closed.id, run_at=tomorrow,
            status="pending", created_by_tg_id=1,
        ),
        ScheduledTask(
            task_type="quiz", ref_id=live.id, run_at=tomorrow,
            status="pending", created_by_tg_id=1,
            payload_json=json.dumps({"action": "close"}),
        ),
    ])
    await session.commit()


class TestEventSelection:
    async def test_empty_query_shows_open_then_upcoming(self, session):
        await _seed(session)

        answer = await _call("", session)
        ids = [result.id for result in answer["results"]]

        assert ids[0].startswith("open:quiz:")
        assert len([value for value in ids if value.startswith("soon:")]) == 2
        assert all("Vecchia" not in result.title for result in answer["results"])

    async def test_open_filter_excludes_scheduled(self, session):
        await _seed(session)
        answer = await _call("aperti", session)

        assert len(answer["results"]) == 1
        assert answer["results"][0].id.startswith("open:")

    async def test_soon_filter_excludes_open(self, session):
        await _seed(session)
        answer = await _call("prossimi", session)

        assert len(answer["results"]) == 2
        assert all(result.id.startswith("soon:") for result in answer["results"])

    async def test_no_events_means_no_fake_tappable_hint(self, session):
        answer = await _call("", session)

        assert answer["results"] == []


class TestArticle:
    async def test_open_event_has_real_deep_link_and_escaped_html(self, session):
        await _seed(session)
        result = (await _call("live", session))["results"][0]

        assert result.reply_markup.inline_keyboard[0][0].url.startswith(
            "https://t.me/test_bot?start=quiz_"
        )
        assert "Live &lt;b&gt;" in result.input_message_content.message_text
        assert "🟢 APERTO" in result.input_message_content.message_text

    async def test_upcoming_event_contains_local_date(self, session):
        await _seed(session)
        results = (await _call("soon", session))["results"]

        assert all("COMING SOON" in result.title for result in results)
        assert all("In programma il" in result.description for result in results)

    async def test_poll_has_no_fake_private_action(self, session):
        await _seed(session)
        poll = next(
            result for result in (await _call("soon", session))["results"]
            if "Serata giochi" in result.title
        )

        assert poll.reply_markup is None
        assert (await _call("", session))["is_personal"] is True


@pytest.mark.parametrize("payload", ["{", "null", "[]", '"start"', "42"])
async def test_malformed_timer_does_not_hide_healthy_public_events(session, payload):
    await _seed(session)
    session.add(ScheduledTask(
        task_type="quiz", ref_id=999, status="pending", created_by_tg_id=1,
        run_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
        payload_json=payload,
    ))
    await session.commit()

    results = (await _call("", session))["results"]

    assert len(results) == 3
    assert results[0].id.startswith("open:quiz:")


async def test_internal_start_timer_is_not_a_public_event(session):
    await _seed(session)
    ready = await session.scalar(select(Quiz).where(Quiz.title == "Domani"))
    session.add(ScheduledTask(
        task_type="quiz", ref_id=ready.id, status="pending", created_by_tg_id=1,
        run_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
        payload_json='{"internal": true, "action": "start"}',
    ))
    await session.commit()

    assert len((await _call("soon", session))["results"]) == 2


async def test_timer_for_an_unregistered_event_type_does_not_hide_known_events(session):
    await _seed(session)
    session.add(ScheduledTask(
        task_type="removed_plugin", ref_id=1, status="pending", created_by_tg_id=1,
        run_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    ))
    await session.commit()

    assert len((await _call("", session))["results"]) == 3


@pytest.mark.parametrize("mode", ["all", "open"])
async def test_open_event_exposes_earliest_pending_closure_in_its_own_scope(session, mode):
    from services import event_discovery

    await _seed(session)
    live = await session.scalar(select(Quiz).where(Quiz.status == "running"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    closes = now + timedelta(hours=2)
    session.add_all([
        ScheduledTask(task_type="quiz", ref_id=live.id, run_at=closes, status="pending",
                      created_by_tg_id=1, payload_json='{"action":"close"}'),
        ScheduledTask(task_type="quiz", ref_id=live.id, run_at=now, status="cancelled",
                      created_by_tg_id=1, payload_json='{"action":"close"}'),
        ScheduledTask(task_type="poll", ref_id=live.id, run_at=now, status="pending",
                      created_by_tg_id=1, payload_json='{"action":"close"}'),
    ])
    await session.commit()

    events = await event_discovery.list_public_events(
        session, event_types=event_types.all_types(), mode=mode, now=now,
    )
    event = next(e for e in events if e.key == "quiz" and e.item_id == live.id)
    assert event.ends_at == closes
    assert event.is_open
    assert all(e.schedule_id is None or e.starts_at > now for e in events)


async def test_running_poll_is_visible_with_its_deadline_without_fake_private_action(session):
    closes = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    session.add(PollTemplate(
        question="Sondaggio attivo", options_json='["Sì","No"]',
        creator_tg_id=1, status="running", closes_at=closes,
    ))
    await session.commit()

    results = (await _call("aperti", session))["results"]
    assert len(results) == 1
    assert "Sondaggio attivo" in results[0].title
    assert results[0].reply_markup is None

    from services import event_discovery
    event = (await event_discovery.list_public_events(
        session, event_types=event_types.all_types(), mode="open",
    ))[0]
    assert event.ends_at == closes


async def test_overdue_start_is_not_announced_as_upcoming(session):
    await _seed(session)
    ready = await session.scalar(select(Quiz).where(Quiz.status == "ready"))
    session.add(ScheduledTask(
        task_type="quiz", ref_id=ready.id, status="pending", created_by_tg_id=1,
        run_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),
    ))
    await session.commit()
    assert len((await _call("soon", session))["results"]) == 2
