"""Adding accepted spellings to a round that is already out.

The judge is a model, so it will occasionally turn down something a human would
have accepted. We cannot enumerate those cases in advance — they arrive as «ho
scritto X e me l'ha data sbagliata» — so what matters here is that the fix is
available *while the round is live*, costs one message, and cannot do anything
retroactive: attempts already judged stay judged, and a round that is over is not
editable at all.

The alias path itself (an alias beats the cached verdict) is pinned in
`test_guess_service.py`; this file is about the admin-facing flow around it.
"""

from __future__ import annotations

import types

import pytest

from handlers.guess import editing as ed
from services import guess_service as gs
from services.guess_judge import aliases_of

ADMIN = 1


class _Msg:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.chat = types.SimpleNamespace(id=ADMIN, type="private")
        self.from_user = types.SimpleNamespace(id=ADMIN, full_name="Admin")
        self.answers: list[str] = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.answers.append(text)
        return self

    @property
    def said(self) -> str:
        return "\n".join(self.answers)


class _Cb:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _Msg()
        self.from_user = types.SimpleNamespace(id=ADMIN, full_name="Admin")
        self.alerts: list[str] = []

    async def answer(self, text=None, **kw):
        if text:
            self.alerts.append(text)

    @property
    def said(self) -> str:
        return self.message.said


@pytest.fixture
def state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=999, chat_id=ADMIN, user_id=ADMIN))


@pytest.fixture
async def round_(session):
    r = await gs.create_round(
        session, kind="guess", creator_tg_id=ADMIN, title="Indovina",
        media_file_id="F", media_kind="photo", answer="Doom",
        aliases=[], hints=[], max_attempts=5, time_limit_seconds=0,
    )
    r.status = "running"
    await session.commit()
    return r


async def _open(state, session, round_id: int) -> _Cb:
    cb = _Cb(f"guess_alias:add:{round_id}")
    await ed.cb_add_aliases(cb, state, session)
    return cb


class TestOpening:
    async def test_a_running_round_opens_the_prompt(self, session, state, round_):
        cb = await _open(state, session, round_.id)

        assert await state.get_state() == ed.GuessAliasStates.waiting_aliases.state
        assert (await state.get_data())["alias_round"] == round_.id
        assert "grafie" in cb.said.lower()

    async def test_the_prompt_shows_the_answer_it_is_about(self, session, state, round_):
        """Aliases only make sense next to the canonical answer — an admin comparing
        the two is exactly the decision being made here."""
        cb = await _open(state, session, round_.id)

        assert "Doom" in cb.said

    async def test_a_round_that_has_not_started_is_editable_too(
        self, session, state, round_
    ):
        """Once created, this was the one field with no way back to it short of
        deleting the round and rebuilding it."""
        round_.status = "ready"
        await session.commit()

        await _open(state, session, round_.id)

        assert await state.get_state() == ed.GuessAliasStates.waiting_aliases.state

    async def test_a_finished_round_is_refused(self, session, state, round_):
        """Nobody is guessing any more: it would change nothing and read as if it had."""
        round_.status = "finished"
        await session.commit()

        cb = await _open(state, session, round_.id)

        assert cb.alerts and "non modificabile" in cb.alerts[0].lower()
        assert await state.get_state() is None

    async def test_a_deleted_round_is_refused(self, session, state):
        cb = await _open(state, session, 9999)

        assert cb.alerts and await state.get_state() is None

    async def test_a_non_numeric_id_is_ignored(self, session, state):
        cb = _Cb("guess_alias:add:abc")

        await ed.cb_add_aliases(cb, state, session)

        assert await state.get_state() is None


class TestSubmitting:
    async def test_the_spellings_are_saved_and_committed(self, session, state, round_):
        await _open(state, session, round_.id)
        msg = _Msg("Doom 1993\nDoom I")

        await ed.fsm_aliases(msg, state, session)
        await session.rollback()  # only a committed write survives this

        assert aliases_of(await gs.get_round(session, round_.id)) == ["Doom 1993", "Doom I"]
        assert await state.get_state() is None

    async def test_too_many_at_once_is_refused_and_keeps_the_flow_open(
        self, session, state, round_
    ):
        await _open(state, session, round_.id)
        msg = _Msg("\n".join(f"alias {i}" for i in range(50)))

        await ed.fsm_aliases(msg, state, session)

        assert "⚠️" in msg.said
        assert await state.get_state() == ed.GuessAliasStates.waiting_aliases.state
        assert aliases_of(await gs.get_round(session, round_.id)) == []

    async def test_a_skip_word_closes_the_flow_without_writing(
        self, session, state, round_
    ):
        await _open(state, session, round_.id)
        msg = _Msg("-")

        await ed.fsm_aliases(msg, state, session)

        assert await state.get_state() is None
        assert aliases_of(await gs.get_round(session, round_.id)) == []

    async def test_duplicates_are_reported_not_written_twice(
        self, session, state, round_
    ):
        await _open(state, session, round_.id)
        await ed.fsm_aliases(_Msg("Doom 1993"), state, session)
        await _open(state, session, round_.id)
        msg = _Msg("doom 1993")

        await ed.fsm_aliases(msg, state, session)

        assert "0 grafie" in msg.said
        assert aliases_of(await gs.get_round(session, round_.id)) == ["Doom 1993"]

    async def test_a_round_deleted_mid_flow_says_so_instead_of_raising(
        self, session, state, round_
    ):
        await _open(state, session, round_.id)
        await gs.delete_round(session, round_.id)
        await session.commit()
        msg = _Msg("Doom 1993")

        await ed.fsm_aliases(msg, state, session)

        assert "non trovato" in msg.said.lower()
        assert await state.get_state() is None


class TestCancel:
    async def test_cancelling_clears_the_flow(self, session, state, round_):
        await _open(state, session, round_.id)

        await ed.cb_cancel(_Cb("guess_alias:cancel"), state)

        assert await state.get_state() is None
        assert aliases_of(await gs.get_round(session, round_.id)) == []
