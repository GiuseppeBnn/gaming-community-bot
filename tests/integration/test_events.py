"""Integration tests for the unified Events model: draft bets + poll templates."""

from __future__ import annotations

import pytest

from database.models import EventStatus
from exceptions.economy import EventAlreadySettledError, EventNotFoundError
from services import bet_service, poll_service


class TestDraftBets:
    async def test_create_draft_and_list(self, session, user_factory):
        await user_factory(1, "creator")
        event = await bet_service.create_event(
            session, creator_tg_id=1, title="Match", description="d",
            options=[{"label": "A"}, {"label": "B"}],
            status=EventStatus.draft.value,
        )
        await session.commit()
        assert event.status == EventStatus.draft.value
        # Drafts are excluded from the user/admin open lists...
        assert await bet_service.get_open_events(session) == []
        # ...but visible in the Events hub draft list.
        drafts = await bet_service.list_drafts(session)
        assert [d.id for d in drafts] == [event.id]

    async def test_activate_draft_opens_it(self, session, user_factory):
        await user_factory(1, "creator")
        event = await bet_service.create_event(
            session, creator_tg_id=1, title="Match", description="d",
            options=[{"label": "A"}, {"label": "B"}],
            status=EventStatus.draft.value,
        )
        await session.commit()
        activated = await bet_service.activate_event(session, event.id)
        await session.commit()
        assert activated.status == EventStatus.open.value
        assert await bet_service.list_drafts(session) == []
        opened = await bet_service.get_open_events(session)
        assert [e.id for e in opened] == [event.id]

    async def test_activate_is_idempotent_for_open(self, session, user_factory):
        await user_factory(1, "creator")
        event = await bet_service.create_event(
            session, creator_tg_id=1, title="M", description="",
            options=[{"label": "A"}, {"label": "B"}],
            status=EventStatus.draft.value,
        )
        await session.commit()
        await bet_service.activate_event(session, event.id)
        again = await bet_service.activate_event(session, event.id)  # no error
        assert again.status == EventStatus.open.value

    async def test_activate_missing_raises(self, session):
        with pytest.raises(EventNotFoundError):
            await bet_service.activate_event(session, 9999)

    async def test_default_status_is_open(self, session, user_factory):
        await user_factory(1, "creator")
        event = await bet_service.create_event(
            session, creator_tg_id=1, title="M", description="",
            options=[{"label": "A"}, {"label": "B"}],
        )
        await session.commit()
        assert event.status == EventStatus.open.value


class TestPollTemplates:
    async def test_create_list_and_use(self, session):
        poll = await poll_service.create_template(
            session, creator_tg_id=1, question="Best game?",
            options=["A", "B", "C"], group_id=None,
        )
        await session.commit()
        assert poll.status == "ready"
        assert poll_service.options_of(poll) == ["A", "B", "C"]
        assert [p.id for p in await poll_service.list_ready(session)] == [poll.id]

        await poll_service.mark_used(session, poll.id)
        await session.commit()
        used = await poll_service.get(session, poll.id)
        assert used.status == "used" and used.used_at is not None
        assert await poll_service.list_ready(session) == []
