"""The bet-creation FSM gains a betting-window step: after the options, the admin
picks a duration (preset/custom/illimitata) and only THEN is the event created,
so the window is baked in from the start."""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import services.bet_service as bet_svc
from handlers.betting import (
    BetCreationStates,
    _finalize_bet_creation,
    fsm_bet_options,
)


def _fresh_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


class _FakeMsg:
    def __init__(self, text: str):
        self.text = text
        self.from_user = SimpleNamespace(id=1)
        self.replies: list[tuple] = []

    async def answer(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


async def test_options_step_advances_to_window_and_stashes_options(session):
    state = _fresh_state()
    await state.set_state(BetCreationStates.waiting_for_options)
    await state.update_data(title="T", description="D", bet_as_draft=True)

    await fsm_bet_options(_FakeMsg("A\nB\nC"), state)

    assert await state.get_state() == BetCreationStates.waiting_for_window.state
    data = await state.get_data()
    assert data["options"] == ["A", "B", "C"]  # stashed, event NOT yet created
    assert await bet_svc.list_drafts(session) == []


async def test_finalize_draft_stores_window_without_arming(session, user_factory):
    await user_factory(tg_id=1)
    state = _fresh_state()
    await state.update_data(title="T", description="D", options=["A", "B"], bet_as_draft=True)

    await _finalize_bet_creation(_FakeMsg(""), state, session, 1800, creator_id=1)

    drafts = await bet_svc.list_drafts(session)
    assert len(drafts) == 1
    assert drafts[0].betting_window_seconds == 1800
    assert drafts[0].closes_at is None          # a draft's window starts at activation
    assert await state.get_state() is None


async def test_finalize_open_unlimited_has_no_deadline(session, user_factory):
    await user_factory(tg_id=1)
    state = _fresh_state()
    await state.update_data(title="T", description="D", options=["A", "B"], bet_as_draft=False)

    await _finalize_bet_creation(_FakeMsg(""), state, session, None, creator_id=1)

    opened = await bet_svc.get_open_events(session)
    assert len(opened) == 1
    assert opened[0].betting_window_seconds is None
    assert opened[0].closes_at is None          # illimitata → only manual lock
