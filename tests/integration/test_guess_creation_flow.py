"""Building a round, step by step — and every way the flow refuses one.

The refusals are the point. A round is admin-authored data that later decides who
gets paid: a title that was silently truncated, a hint threshold above the attempt
limit, or a medium that cannot be resent are all defects that only surface once
players are already looking at it.

The preview step is pinned as behaviour, not decoration: sending the medium back
to the admin is how a dead `file_id` gets caught while it can still be fixed.
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

    @property
    def said(self) -> str:
        return "\n".join(self.answers)


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


async def _to_attempts(state):
    await _to_answer(state)
    await cr.fsm_answer(_Msg("Doom"), state)
    await cr.fsm_aliases(_Msg("-"), state)


async def _to_hints(state):
    await _to_attempts(state)
    await cr.fsm_attempts(_Msg("5"), state)
    await cr.fsm_time_limit(_Msg("0"), state)


async def _to_review(state):
    await _to_hints(state)
    await cr.fsm_hint(_Msg("3 | sparatutto"), state)
    await cr.fsm_hint(_Msg("fine"), state)
    for value in ("100", "50", "25", "10"):
        await cr.fsm_prize_value(_Msg(value), state)


class TestTitle:
    async def test_the_flow_starts_by_asking_for_a_title(self, state):
        m = _Msg()

        await cr.start_guess_creation(m, state, kind="guess", creator_id=1)

        assert "titolo" in m.said.lower()
        assert await state.get_state() == cr.GuessCreationStates.waiting_title.state

    async def test_the_chosen_kind_is_remembered(self, state):
        await cr.start_guess_creation(_Msg(), state, kind="sound", creator_id=1)

        assert (await state.get_data())["kind"] == "sound"

    async def test_the_creator_is_taken_from_the_argument_not_the_message(self, state):
        """The hub calls this with `message.from_user` = the bot."""
        await cr.start_guess_creation(_Msg(user_id=999), state, kind="guess",
                                      creator_id=42)

        assert (await state.get_data())["creator_id"] == 42

    async def test_a_title_over_the_cap_is_rejected_with_its_real_length(self, state):
        await cr.start_guess_creation(_Msg(), state, kind="guess", creator_id=1)
        m = _Msg("x" * 300)

        await cr.fsm_title(m, state)

        assert "300/256" in m.said
        assert await state.get_state() == cr.GuessCreationStates.waiting_title.state

    async def test_a_title_that_is_too_short_is_rejected(self, state):
        await cr.start_guess_creation(_Msg(), state, kind="guess", creator_id=1)

        await cr.fsm_title(_Msg("ab"), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_title.state


class TestMedia:
    async def test_a_photo_is_accepted_and_echoed_back(self, state):
        """The echo IS the validation: a file_id that cannot be resent must fail
        here, where the admin can still send another file."""
        await _to_media(state)
        m = _Msg(photo=[_Photo("small"), _Photo("BIG")])

        await cr.fsm_media(m, state)

        assert m.bot.sent == [("photo", "BIG")]
        assert (await state.get_data())["media_file_id"] == "BIG"

    async def test_the_largest_photo_size_is_the_one_kept(self, state):
        """Players squint at this; the thumbnail would be unfair."""
        await _to_media(state)

        await cr.fsm_media(_Msg(photo=[_Photo("thumb"), _Photo("full")]), state)

        assert (await state.get_data())["media_file_id"] == "full"

    async def test_a_voice_note_is_accepted_for_a_sound_round(self, state):
        await _to_media(state, kind="sound")
        m = _Msg(voice=types.SimpleNamespace(file_id="V"))

        await cr.fsm_media(m, state)

        assert (await state.get_data())["media_kind"] == "voice"

    async def test_a_photo_is_refused_for_a_sound_round(self, state):
        """Otherwise Sound Quest ships with an image nobody can listen to."""
        await _to_media(state, kind="sound")
        m = _Msg(photo=[_Photo("BIG")])

        await cr.fsm_media(m, state)

        assert "audio" in m.said.lower()
        assert await state.get_state() == cr.GuessCreationStates.waiting_media.state

    async def test_an_audio_is_refused_for_an_image_round(self, state):
        await _to_media(state, kind="guess")
        m = _Msg(audio=types.SimpleNamespace(file_id="A"))

        await cr.fsm_media(m, state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_media.state

    async def test_text_instead_of_media_is_refused(self, state):
        await _to_media(state)

        await cr.fsm_media(_Msg("una foto bellissima"), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_media.state

    async def test_a_medium_that_cannot_be_resent_is_refused_now(self, state):
        """The whole reason the preview exists."""
        await _to_media(state)
        m = _Msg(photo=[_Photo("DEAD")])

        async def _boom(*a, **kw):
            raise RuntimeError("wrong file identifier")
        m.bot.send_photo = _boom

        await cr.fsm_media(m, state)

        assert "non riesco" in m.said.lower()
        assert await state.get_state() == cr.GuessCreationStates.waiting_media.state
        assert "media_file_id" not in await state.get_data()


class TestAnswerAndAliases:
    async def test_the_answer_is_stored(self, state):
        await _to_answer(state)

        await cr.fsm_answer(_Msg("GTA San Andreas"), state)

        assert (await state.get_data())["answer"] == "GTA San Andreas"

    async def test_an_empty_answer_is_refused(self, state):
        await _to_answer(state)

        await cr.fsm_answer(_Msg("   "), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_answer.state

    async def test_an_answer_over_the_cap_is_refused(self, state):
        await _to_answer(state)
        m = _Msg("x" * 250)

        await cr.fsm_answer(m, state)

        assert "250/200" in m.said

    async def test_aliases_are_one_per_line(self, state):
        await _to_answer(state)
        await cr.fsm_answer(_Msg("GTA San Andreas"), state)

        await cr.fsm_aliases(_Msg("GTA SA\nSan Andreas"), state)

        assert (await state.get_data())["aliases"] == ["GTA SA", "San Andreas"]

    async def test_aliases_can_be_skipped(self, state):
        await _to_answer(state)
        await cr.fsm_answer(_Msg("GTA San Andreas"), state)

        await cr.fsm_aliases(_Msg("-"), state)

        assert (await state.get_data())["aliases"] == []

    async def test_too_many_aliases_are_refused(self, state):
        await _to_answer(state)
        await cr.fsm_answer(_Msg("Doom"), state)

        await cr.fsm_aliases(_Msg("\n".join(f"a{i}" for i in range(25))), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_aliases.state

    async def test_an_over_long_alias_is_refused(self, state):
        await _to_answer(state)
        await cr.fsm_answer(_Msg("Doom"), state)

        await cr.fsm_aliases(_Msg("x" * 150), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_aliases.state


class TestAttemptsAndTime:
    async def test_attempts_are_stored(self, state):
        await _to_attempts(state)

        await cr.fsm_attempts(_Msg("5"), state)

        assert (await state.get_data())["max_attempts"] == 5

    @pytest.mark.parametrize("bad", ["0", "-3", "abc", "999"])
    async def test_an_impossible_attempt_count_is_refused(self, state, bad):
        """Zero attempts is a round nobody may answer; 999 is not a game."""
        await _to_attempts(state)

        await cr.fsm_attempts(_Msg(bad), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_attempts.state

    async def test_no_time_limit_is_a_valid_choice(self, state):
        await _to_attempts(state)
        await cr.fsm_attempts(_Msg("5"), state)

        await cr.fsm_time_limit(_Msg("0"), state)

        assert (await state.get_data())["time_limit_seconds"] == 0

    @pytest.mark.parametrize("bad", ["5", "99999", "presto"])
    async def test_an_impossible_time_limit_is_refused(self, state, bad):
        await _to_attempts(state)
        await cr.fsm_attempts(_Msg("5"), state)

        await cr.fsm_time_limit(_Msg(bad), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_time_limit.state


class TestHints:
    async def test_a_hint_is_parsed_as_threshold_and_text(self, state):
        await _to_hints(state)

        await cr.fsm_hint(_Msg("3 | È uno sparatutto"), state)

        assert (await state.get_data())["hints"] == [(3, "È uno sparatutto")]

    async def test_hints_can_be_skipped_entirely(self, state):
        await _to_hints(state)

        await cr.fsm_hint(_Msg("fine"), state)

        assert (await state.get_data())["hints"] == []
        assert await state.get_state() == cr.GuessCreationStates.waiting_prize_first.state

    async def test_a_threshold_above_the_attempt_limit_is_refused(self, state):
        """A hint after 9 attempts on a 5-attempt round is a hint nobody sees."""
        await _to_hints(state)
        m = _Msg("9 | mai visibile")

        await cr.fsm_hint(m, state)

        assert "5" in m.said
        assert (await state.get_data())["hints"] == []

    @pytest.mark.parametrize("bad", ["senza separatore", "tre | testo", "3 | "])
    async def test_a_malformed_hint_is_refused(self, state, bad):
        await _to_hints(state)

        await cr.fsm_hint(_Msg(bad), state)

        assert (await state.get_data())["hints"] == []
        assert await state.get_state() == cr.GuessCreationStates.waiting_hints.state

    async def test_two_hints_on_the_same_threshold_are_refused(self, state):
        """Only one would ever be delivered; the other is silently lost work."""
        await _to_hints(state)
        await cr.fsm_hint(_Msg("2 | primo"), state)

        await cr.fsm_hint(_Msg("2 | secondo"), state)

        assert (await state.get_data())["hints"] == [(2, "primo")]

    async def test_an_over_long_hint_is_refused(self, state):
        await _to_hints(state)

        await cr.fsm_hint(_Msg("2 | " + "x" * 250), state)

        assert (await state.get_data())["hints"] == []

    async def test_hints_accumulate_in_order(self, state):
        await _to_hints(state)
        await cr.fsm_hint(_Msg("4 | secondo"), state)
        await cr.fsm_hint(_Msg("2 | primo"), state)

        assert (await state.get_data())["hints"] == [(4, "secondo"), (2, "primo")]


class TestPrizes:
    async def test_the_four_steps_run_in_order(self, state):
        await _to_hints(state)
        await cr.fsm_hint(_Msg("fine"), state)

        for value, expected in [("100", "prize_first"), ("50", "prize_second"),
                                ("25", "prize_third"), ("10", "prize_consolation")]:
            await cr.fsm_prize_value(_Msg(value), state)
            assert expected in await state.get_data()

    async def test_a_negative_prize_is_refused(self, state):
        await _to_hints(state)
        await cr.fsm_hint(_Msg("fine"), state)

        await cr.fsm_prize_value(_Msg("-10"), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_prize_first.state

    async def test_a_non_number_is_refused(self, state):
        await _to_hints(state)
        await cr.fsm_hint(_Msg("fine"), state)

        await cr.fsm_prize_value(_Msg("tanti"), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_prize_first.state

    async def test_the_last_prize_step_leads_to_the_review(self, state):
        await _to_review(state)

        assert await state.get_state() == cr.GuessCreationStates.reviewing.state


class TestPublish:
    async def test_a_published_round_is_ready_and_complete(self, state, session):
        await _to_review(state)

        await cr.fsm_publish(_Msg(), state, session)
        await session.commit()

        r = (await session.execute(select(GuessRound))).scalar_one()
        assert r.status == "ready"
        assert (r.title, r.answer, r.max_attempts, r.time_limit_seconds) == \
               ("Il titolo", "Doom", 5, 0)
        assert r.creator_tg_id == 42
        assert r.prize_first == 100 and r.prize_min > 0
        assert gs.hints_of(r) == [(3, "sparatutto")]

    async def test_publishing_clears_the_state(self, state, session):
        await _to_review(state)

        await cr.fsm_publish(_Msg(), state, session)

        assert await state.get_state() is None

    async def test_the_confirmation_offers_start_and_schedule(self, state, session):
        await _to_review(state)
        m = _Msg()

        await cr.fsm_publish(m, state, session)

        assert "creato" in m.said.lower()

    async def test_a_sound_round_publishes_with_its_own_kind(self, state, session):
        await _to_answer(state, kind="sound")
        await cr.fsm_answer(_Msg("Doom"), state)
        await cr.fsm_aliases(_Msg("-"), state)
        await cr.fsm_attempts(_Msg("3"), state)
        await cr.fsm_time_limit(_Msg("0"), state)
        await cr.fsm_hint(_Msg("fine"), state)
        for value in ("10", "5", "2", "1"):
            await cr.fsm_prize_value(_Msg(value), state)

        await cr.fsm_publish(_Msg(), state, session)
        await session.commit()

        r = (await session.execute(select(GuessRound))).scalar_one()
        assert (r.kind, r.media_kind) == ("sound", "audio")


class _Cb:
    """The narrow slice of CallbackQuery the button handlers touch."""

    def __init__(self, message: _Msg | None = None) -> None:
        self.message = message or _Msg()
        self.from_user = types.SimpleNamespace(id=1, full_name="Admin")
        self.answered = 0
        self.edits: list[str] = []
        self.message.edit_text = self._edit  # type: ignore[method-assign]

    async def _edit(self, text, **kw):
        self.edits.append(text)

    async def answer(self, text=None, **kw):
        self.answered += 1


class TestDefaultAndSkipButtons:
    """The buttons exist so an admin can take the suggested value without
    retyping it. If one silently did nothing, the flow would look frozen."""

    async def test_the_default_button_fills_in_the_attempts(self, state):
        await _to_attempts(state)

        await cr.cb_use_default(_Cb(), state)

        data = await state.get_data()
        assert data["max_attempts"] == cr.settings.guess_default_attempts
        assert await state.get_state() == cr.GuessCreationStates.waiting_time_limit.state

    async def test_the_default_button_fills_in_the_time_limit(self, state):
        await _to_attempts(state)
        await cr.fsm_attempts(_Msg("5"), state)

        await cr.cb_use_default(_Cb(), state)

        data = await state.get_data()
        assert data["time_limit_seconds"] == cr.settings.guess_default_time_limit_seconds
        assert await state.get_state() == cr.GuessCreationStates.waiting_hints.state

    async def test_the_default_button_fills_in_a_prize(self, state):
        await _to_hints(state)
        await cr.fsm_hint(_Msg("fine"), state)

        await cr.cb_use_default(_Cb(), state)

        assert (await state.get_data())["prize_first"] == cr.settings.guess_default_first

    async def test_the_default_button_walks_the_whole_prize_ladder(self, state):
        await _to_hints(state)
        await cr.fsm_hint(_Msg("fine"), state)

        for _ in range(4):
            await cr.cb_use_default(_Cb(), state)

        assert await state.get_state() == cr.GuessCreationStates.reviewing.state

    async def test_the_default_button_outside_a_default_step_does_nothing(self, state):
        """A stale button from an earlier message must not corrupt the flow."""
        await _to_media(state)

        cb = _Cb()
        await cr.cb_use_default(cb, state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_media.state
        assert cb.answered == 1

    async def test_the_skip_button_skips_the_aliases(self, state):
        await _to_answer(state)
        await cr.fsm_answer(_Msg("Doom"), state)

        await cr.cb_skip(_Cb(), state)

        assert (await state.get_data())["aliases"] == []
        assert await state.get_state() == cr.GuessCreationStates.waiting_attempts.state

    async def test_the_skip_button_skips_the_hints(self, state):
        await _to_hints(state)

        await cr.cb_skip(_Cb(), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_prize_first.state

    async def test_the_skip_button_outside_a_skippable_step_does_nothing(self, state):
        await _to_media(state)

        await cr.cb_skip(_Cb(), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_media.state


class TestPublishButton:
    async def test_it_publishes(self, state, session):
        await _to_review(state)

        await cr.cb_publish(_Cb(), state, session)
        await session.commit()

        assert (await session.execute(select(GuessRound.status))).scalar_one() == "ready"


class TestCancel:
    async def test_cancelling_asks_first(self, state):
        """A half-built round is real work; one tap must not throw it away."""
        await _to_attempts(state)
        cb = _Cb()

        await cr.cb_cancel(cb, state)

        assert "sicuro" in cb.message.said.lower()
        assert await state.get_state() is not None, "nothing is lost until confirmed"

    async def test_cancelling_outside_a_flow_is_a_no_op(self, state):
        cb = _Cb()

        await cr.cb_cancel(cb, state)

        assert cb.message.said == ""

    async def test_confirming_clears_the_flow(self, state):
        await _to_attempts(state)
        cb = _Cb()

        await cr.cb_cancel_yes(cb, state)

        assert await state.get_state() is None
        assert "annullata" in cb.edits[0].lower()

    async def test_declining_leaves_the_flow_intact(self, state):
        await _to_attempts(state)
        before = await state.get_state()
        cb = _Cb()

        await cr.cb_cancel_no(cb)

        assert await state.get_state() == before
        assert cb.edits


class TestHintCaps:
    async def test_too_many_hints_are_refused(self, state):
        await _to_attempts(state)
        await cr.fsm_attempts(_Msg("20"), state)
        await cr.fsm_time_limit(_Msg("0"), state)
        for i in range(1, 11):
            await cr.fsm_hint(_Msg(f"{i} | suggerimento {i}"), state)
        m = _Msg("11 | uno di troppo")

        await cr.fsm_hint(m, state)

        assert "10" in m.said
        assert len((await state.get_data())["hints"]) == 10
