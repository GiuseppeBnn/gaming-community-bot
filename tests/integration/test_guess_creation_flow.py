"""Building a round: three questions, then a card you can edit.

The old flow asked eleven questions in a row with no way back. Getting the answer
wrong on question three left two options — walk the remaining eight steps, or
cancel and retype everything. On an eleven-question form that is *the* defect,
and it is the one this shape removes: only title, medium and answer are asked,
because only those three have no sensible default. Everything else starts filled
in and is one tap away from being changed.

The refusals are still the point. A round is admin-authored data that later
decides who gets paid: a title silently truncated, a hint threshold above the
attempt limit, or a medium that cannot be resent are all defects that surface
only once players are already looking at it. Every one of them is refused here,
with the real number, at the moment it can still be fixed.

The medium echo is pinned as behaviour, not decoration: sending it straight back
is how a dead `file_id` gets caught while the admin can still pick another.
"""

from __future__ import annotations

import types

import pytest
from sqlalchemy import select

from database.models import GuessRound
from handlers.guess import creation as cr
from services import guess_service as gs


class _StubBot:
    id = 999

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_photo(self, chat_id, file_id, **kw):
        self.sent.append(("photo", file_id))

    async def send_audio(self, chat_id, file_id, **kw):
        self.sent.append(("audio", file_id))

    async def send_voice(self, chat_id, file_id, **kw):
        self.sent.append(("voice", file_id))

    async def get_chat_administrators(self, chat_id):
        return []


class _Photo:
    def __init__(self, file_id: str) -> None:
        self.file_id = file_id


class _Msg:
    """The narrow slice of Message the creation FSM actually touches."""

    def __init__(self, text: str | None = None, *, photo=None, audio=None,
                 voice=None, user_id: int = 1) -> None:
        self.text = text
        self.photo = photo
        self.audio = audio
        self.voice = voice
        self.bot = _StubBot()
        self.chat = types.SimpleNamespace(id=user_id, type="private")
        self.from_user = types.SimpleNamespace(id=user_id, full_name="Admin")
        self.answers: list[str] = []

    async def answer(self, text, **kw):
        self.answers.append(text)

    async def reply(self, text, **kw):
        self.answers.append(text)

    async def edit_text(self, text, **kw):
        self.answers.append(text)

    @property
    def said(self) -> str:
        return "\n".join(self.answers)


class _Cb:
    """A callback query carrying `data`, over a message we can read back."""

    def __init__(self, data: str, user_id: int = 1) -> None:
        self.data = data
        self.message = _Msg(user_id=user_id)
        self.from_user = types.SimpleNamespace(id=user_id, full_name="Admin")
        self.alerts: list[str] = []

    async def answer(self, text=None, **kw):
        if text:
            self.alerts.append(text)

    @property
    def said(self) -> str:
        return self.message.said


@pytest.fixture
def state():
    """A real FSMContext on aiogram's in-memory storage — the state machine is
    what is under test, so a stub would be testing the stub."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=999, chat_id=1, user_id=1))


async def _to_media(state, kind="guess"):
    await cr.start_guess_creation(_Msg(), state, kind=kind, creator_id=42)
    await cr.fsm_title(_Msg("Il titolo"), state)


async def _to_answer(state, kind="guess"):
    await _to_media(state, kind)
    media = (_Msg(photo=[_Photo("F")]) if kind == "guess"
             else _Msg(audio=types.SimpleNamespace(file_id="F")))
    await cr.fsm_media(media, state)


async def _to_card(state, kind="guess", answer="Doom"):
    """The whole mandatory path: title → medium → answer → the card."""
    await _to_answer(state, kind)
    msg = _Msg(answer)
    await cr.fsm_answer(msg, state)
    return msg


async def _edit(state, field: str, value: str) -> _Msg:
    """Tap a field's edit button, then send a new value."""
    await cr.cb_edit(_Cb(f"guess_new:edit:{field}"), state)
    msg = _Msg(value)
    await cr.fsm_edit_value(msg, state)
    return msg


class TestTheMandatoryThree:
    """Only title, medium and answer are asked. They are the three with no
    sensible default; everything else the card can guess and the admin can
    correct."""

    async def test_the_flow_starts_by_asking_for_a_title(self, state):
        msg = _Msg()

        await cr.start_guess_creation(msg, state, kind="guess", creator_id=42)

        assert await state.get_state() == cr.GuessCreationStates.waiting_title.state
        assert "titolo" in msg.said.lower()

    async def test_the_chosen_kind_is_remembered(self, state):
        await cr.start_guess_creation(_Msg(), state, kind="sound", creator_id=42)

        assert (await state.get_data())["kind"] == "sound"

    async def test_the_creator_is_taken_from_the_argument_not_the_message(self, state):
        """The events hub calls this with `message.from_user` set to the bot."""
        await cr.start_guess_creation(_Msg(user_id=999), state,
                                      kind="guess", creator_id=42)

        assert (await state.get_data())["creator_id"] == 42

    async def test_a_title_that_is_too_short_is_rejected(self, state):
        await cr.start_guess_creation(_Msg(), state, kind="guess", creator_id=42)
        msg = _Msg("ab")

        await cr.fsm_title(msg, state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_title.state
        assert "⚠️" in msg.said

    async def test_a_title_over_the_cap_is_rejected_with_its_real_length(self, state):
        """Never silently truncated: a cut title is only discovered once players
        are already reading it."""
        await cr.start_guess_creation(_Msg(), state, kind="guess", creator_id=42)
        msg = _Msg("x" * (cr._MAX_TITLE + 5))

        await cr.fsm_title(msg, state)

        assert str(cr._MAX_TITLE + 5) in msg.said

    async def test_a_photo_is_accepted_and_echoed_back(self, state):
        """The echo IS the validation that the file_id can be resent."""
        await _to_media(state)
        msg = _Msg(photo=[_Photo("FILE")])

        await cr.fsm_media(msg, state)

        assert msg.bot.sent == [("photo", "FILE")]
        assert (await state.get_data())["media_file_id"] == "FILE"

    async def test_the_largest_photo_size_is_the_one_kept(self, state):
        await _to_media(state)

        await cr.fsm_media(_Msg(photo=[_Photo("small"), _Photo("big")]), state)

        assert (await state.get_data())["media_file_id"] == "big"

    async def test_a_voice_note_is_accepted_for_a_sound_round(self, state):
        await _to_media(state, kind="sound")

        await cr.fsm_media(_Msg(voice=types.SimpleNamespace(file_id="V")), state)

        assert (await state.get_data())["media_kind"] == "voice"

    @pytest.mark.parametrize("kind,msg_kwargs", [
        ("sound", {"photo": [_Photo("F")]}),
        ("guess", {"audio": types.SimpleNamespace(file_id="F")}),
        ("guess", {}),
    ])
    async def test_the_wrong_medium_is_refused(self, state, kind, msg_kwargs):
        await _to_media(state, kind=kind)
        msg = _Msg(**msg_kwargs)

        await cr.fsm_media(msg, state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_media.state
        assert "⚠️" in msg.said

    async def test_a_medium_that_cannot_be_resent_is_refused_now(self, state):
        """While the admin can still pick another file — not in front of players."""
        await _to_media(state)
        msg = _Msg(photo=[_Photo("DEAD")])

        async def _boom(chat_id, file_id, **kw):
            raise RuntimeError("file_id is dead")

        msg.bot.send_photo = _boom

        await cr.fsm_media(msg, state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_media.state
        assert "⚠️" in msg.said

    async def test_an_answer_that_is_too_short_is_refused(self, state):
        await _to_answer(state)
        msg = _Msg("x")

        await cr.fsm_answer(msg, state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_answer.state

    async def test_an_answer_over_the_cap_is_refused(self, state):
        await _to_answer(state)
        msg = _Msg("x" * (cr._MAX_ANSWER + 1))

        await cr.fsm_answer(msg, state)

        assert str(cr._MAX_ANSWER + 1) in msg.said


class TestTheCard:
    """After the third question the admin sees the whole round at once, already
    filled in. No more walking eight steps to reach the end."""

    async def test_the_answer_leads_straight_to_the_card(self, state):
        await _to_card(state)

        assert await state.get_state() == cr.GuessCreationStates.card.state

    async def test_the_card_shows_every_field(self, state):
        msg = await _to_card(state)

        for field in cr.FIELDS.values():
            assert field.label in msg.said, f"«{field.label}» missing from the card"

    async def test_the_card_shows_the_answer(self, state):
        msg = await _to_card(state, answer="Grand Theft Auto")

        assert "Grand Theft Auto" in msg.said

    async def test_the_card_shows_the_medium_again(self, state):
        """Seeing it next to the answer is how you notice you attached the wrong
        file — and the resend is a second proof the file_id is alive."""
        msg = await _to_card(state)

        assert msg.bot.sent == [("photo", "F")]

    async def test_everything_optional_starts_at_its_default(self, state):
        await _to_card(state)

        data = await state.get_data()
        assert data["max_attempts"] == cr.settings.guess_default_attempts
        assert data["time_limit_seconds"] == cr.settings.guess_default_time_limit_seconds
        assert data["round_duration_seconds"] == (
            cr.settings.guess_default_round_duration_seconds
        )
        assert data["prize_first"] == cr.settings.guess_default_first
        assert data["aliases"] == [] and data["hints"] == []


class TestEditingAField:
    """The whole point of the card: any field, any time, no walking."""

    @pytest.mark.parametrize("field,value,key,expected", [
        ("title", "Nuovo titolo", "title", "Nuovo titolo"),
        ("answer", "Quake", "answer", "Quake"),
        ("max_attempts", "8", "max_attempts", 8),
        ("time_limit_seconds", "120", "time_limit_seconds", 120),
        ("time_limit_seconds", "0", "time_limit_seconds", 0),
        ("round_duration_seconds", "900", "round_duration_seconds", 900),
        ("round_duration_seconds", "0", "round_duration_seconds", 0),
        ("aliases", "GTA SA\nSan Andreas", "aliases", ["GTA SA", "San Andreas"]),
        ("aliases", "-", "aliases", []),
        ("hints", "2 | sparatutto\n3 | anni 90", "hints",
         [[2, "sparatutto"], [3, "anni 90"]]),
        ("hints", "-", "hints", []),
        ("prizes", "500 250 100 50", "prize_first", 500),
    ])
    async def test_a_field_can_be_changed_from_the_card(
        self, state, field, value, key, expected
    ):
        await _to_card(state)

        await _edit(state, field, value)

        assert (await state.get_data())[key] == expected

    async def test_editing_returns_to_the_card(self, state):
        await _to_card(state)

        await _edit(state, "max_attempts", "8")

        assert await state.get_state() == cr.GuessCreationStates.card.state

    async def test_the_prizes_are_one_field_not_four_steps(self, state):
        await _to_card(state)

        await _edit(state, "prizes", "500 250 100 50")

        data = await state.get_data()
        assert (data["prize_first"], data["prize_second"],
                data["prize_third"], data["prize_consolation"]) == (500, 250, 100, 50)

    async def test_editing_the_medium_comes_back_to_the_card(self, state):
        """Not back into the old title→medium→answer march."""
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:media"), state)

        await cr.fsm_media(_Msg(photo=[_Photo("NEW")]), state)

        assert await state.get_state() == cr.GuessCreationStates.card.state
        assert (await state.get_data())["media_file_id"] == "NEW"

    @pytest.mark.parametrize("field,bad", [
        ("max_attempts", "0"),
        ("max_attempts", "999"),
        ("max_attempts", "tanti"),
        ("time_limit_seconds", "5"),
        ("time_limit_seconds", "99999"),
        ("round_duration_seconds", "5"),
        ("prizes", "500 250"),
        ("prizes", "-100 0 0 0"),
        ("prizes", "a b c d"),
        ("hints", "sparatutto"),
        ("hints", "99 | oltre il limite"),
        ("hints", "2 | "),
        ("title", "ab"),
        ("answer", "x"),
    ])
    async def test_a_bad_value_is_refused_and_the_field_stays_open(
        self, state, field, bad
    ):
        await _to_card(state)

        msg = await _edit(state, field, bad)

        assert await state.get_state() == cr.GuessCreationStates.editing.state
        assert "⚠️" in msg.said

    async def test_a_refused_value_does_not_overwrite_the_old_one(self, state):
        await _to_card(state)

        await _edit(state, "max_attempts", "0")

        assert (await state.get_data())["max_attempts"] == (
            cr.settings.guess_default_attempts
        )

    async def test_a_hint_threshold_above_the_attempt_limit_is_refused(self, state):
        """A hint that unlocks past the budget is a hint nobody ever sees."""
        await _to_card(state)
        await _edit(state, "max_attempts", "3")

        msg = await _edit(state, "hints", "5 | mai vista")

        assert "⚠️" in msg.said
        assert (await state.get_data())["hints"] == []

    async def test_two_hints_on_the_same_threshold_are_refused(self, state):
        await _to_card(state)

        msg = await _edit(state, "hints", "2 | uno\n2 | due")

        assert "⚠️" in msg.said

    async def test_too_many_aliases_are_refused(self, state):
        await _to_card(state)

        msg = await _edit(state, "aliases", "\n".join(f"a{i}" for i in
                                                      range(cr._MAX_ALIASES + 1)))

        assert "⚠️" in msg.said

    async def test_an_over_long_alias_is_refused(self, state):
        await _to_card(state)

        msg = await _edit(state, "aliases", "x" * (cr._MAX_ALIAS + 1))

        assert "⚠️" in msg.said

    async def test_an_over_long_hint_is_refused(self, state):
        await _to_card(state)

        msg = await _edit(state, "hints", "2 | " + "x" * (cr._MAX_HINT + 1))

        assert "⚠️" in msg.said


class TestPublish:
    async def test_a_published_round_is_ready_and_complete(self, state, session):
        await _to_card(state, answer="Doom")
        await _edit(state, "aliases", "DOOM 1993")
        await _edit(state, "hints", "2 | sparatutto")
        await _edit(state, "prizes", "500 250 100 50")

        await cr.cb_publish(_Cb("guess_new:publish"), state, session)

        r = (await session.execute(select(GuessRound))).scalar_one()
        assert r.status == "ready"
        assert (r.title, r.answer, r.kind) == ("Il titolo", "Doom", "guess")
        assert (r.media_file_id, r.media_kind) == ("F", "photo")
        assert r.creator_tg_id == 42
        assert gs.hints_of(r) == [(2, "sparatutto")]
        assert r.prize_first == 500

    async def test_publishing_untouched_uses_every_default(self, state, session):
        """The three-question path has to produce a playable round on its own,
        or the defaults are decoration."""
        await _to_card(state)

        await cr.cb_publish(_Cb("guess_new:publish"), state, session)

        r = (await session.execute(select(GuessRound))).scalar_one()
        assert r.max_attempts == cr.settings.guess_default_attempts
        assert r.round_duration_seconds == (
            cr.settings.guess_default_round_duration_seconds
        )
        assert r.prize_first == cr.settings.guess_default_first

    async def test_publishing_clears_the_state(self, state, session):
        await _to_card(state)

        await cr.cb_publish(_Cb("guess_new:publish"), state, session)

        assert await state.get_state() is None

    async def test_a_sound_round_publishes_with_its_own_kind(self, state, session):
        await _to_card(state, kind="sound")

        await cr.cb_publish(_Cb("guess_new:publish"), state, session)

        r = (await session.execute(select(GuessRound))).scalar_one()
        assert (r.kind, r.media_kind) == ("sound", "audio")

    async def test_the_confirmation_offers_start_and_schedule(self, state, session):
        cb = _Cb("guess_new:publish")

        await _to_card(state)
        await cr.cb_publish(cb, state, session)

        assert "creato" in cb.said.lower()


class TestCancel:
    async def test_cancelling_asks_first(self, state):
        await _to_card(state)
        cb = _Cb("guess_new:cancel")

        await cr.cb_cancel(cb, state)

        assert "sicuro" in cb.said.lower()
        assert await state.get_state() is not None, "not cancelled until confirmed"

    async def test_cancelling_outside_a_flow_is_a_no_op(self, state):
        cb = _Cb("guess_new:cancel")

        await cr.cb_cancel(cb, state)

        assert cb.said == ""

    async def test_confirming_clears_the_flow(self, state):
        await _to_card(state)

        await cr.cb_cancel_yes(_Cb("guess_new:cancel_yes"), state)

        assert await state.get_state() is None
