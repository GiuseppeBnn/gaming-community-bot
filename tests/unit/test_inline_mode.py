"""Public inline event discovery: only live events and real future starts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

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
