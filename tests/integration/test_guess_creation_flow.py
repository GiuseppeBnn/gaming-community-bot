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
    """Records every outgoing call, because *how many* we make is under test.

    Each card render used to resend the medium and post two fresh messages. Six
    field edits meant six media uploads into one chat, which Telegram rate-limits
    — the bot appeared to hang, and the chat filled with stale cards.
    """

    id = 999

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.sent: list[tuple[str, str]] = []      # media sends
        self.posted: list[str] = []                # NEW text messages
        self.edits: list[tuple[int, str]] = []     # (message_id, text)
        self.deleted: list[int] = []
        self.screens: list[str] = []               # every text put on screen, in order
        self.markups: list = []                    # keyboard attached to each screen
        self._next_id = 100

    def _mk_id(self) -> int:
        self._next_id += 1
        return self._next_id

    @property
    def last_kb(self):
        """The keyboard on the panel right now — buttons are the interface here."""
        return self.markups[-1] if self.markups else None

    @property
    def screen(self) -> str:
        """What the admin is looking at right now.

        With the card edited in place, most text no longer arrives as a new
        message — asserting on "what this call sent" would miss it. This is the
        panel's current contents, which is what the admin actually sees.
        """
        return self.screens[-1] if self.screens else ""

    async def send_photo(self, chat_id, file_id, **kw):
        self.sent.append(("photo", file_id))
        return types.SimpleNamespace(message_id=self._mk_id())

    async def send_audio(self, chat_id, file_id, **kw):
        self.sent.append(("audio", file_id))
        return types.SimpleNamespace(message_id=self._mk_id())

    async def send_voice(self, chat_id, file_id, **kw):
        self.sent.append(("voice", file_id))
        return types.SimpleNamespace(message_id=self._mk_id())

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
        self.edits.append((message_id, text))
        self.screens.append(text)
        self.markups.append(kw.get("reply_markup"))
        return types.SimpleNamespace(message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def get_chat_administrators(self, chat_id):
        return []


class _Photo:
    def __init__(self, file_id: str) -> None:
        self.file_id = file_id


#: One bot per flow, so "how many messages did this whole edit produce" is a
#: question the tests can actually ask. A bot per message could not see it.
_BOT = _StubBot()


class _Msg:
    """The narrow slice of Message the creation FSM actually touches."""

    def __init__(self, text: str | None = None, *, photo=None, audio=None,
                 voice=None, user_id: int = 1, message_id: int | None = None) -> None:
        self.text = text
        self.photo = photo
        self.audio = audio
        self.voice = voice
        self.bot = _BOT
        self.message_id = message_id if message_id is not None else _BOT._mk_id()
        self.chat = types.SimpleNamespace(id=user_id, type="private")
        self.from_user = types.SimpleNamespace(id=user_id, full_name="Admin")
        self.answers: list[str] = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        _BOT.posted.append(text)
        _BOT.screens.append(text)
        _BOT.markups.append(kw.get("reply_markup"))
        return _Msg(user_id=self.chat.id)

    async def reply(self, text, **kw):
        return await self.answer(text, **kw)

    async def edit_text(self, text, **kw):
        self.answers.append(text)
        _BOT.edits.append((self.message_id, text))
        _BOT.screens.append(text)
        _BOT.markups.append(kw.get("reply_markup"))
        return self

    @property
    def said(self) -> str:
        return "\n".join(self.answers)


class _Cb:
    """A callback query carrying `data`, over a message we can read back."""

    def __init__(self, data: str, user_id: int = 1, message_id: int | None = None) -> None:
        self.data = data
        self.message = _Msg(user_id=user_id, message_id=message_id)
        self.from_user = types.SimpleNamespace(id=user_id, full_name="Admin")
        self.alerts: list[str] = []

    async def answer(self, text=None, **kw):
        if text:
            self.alerts.append(text)

    @property
    def said(self) -> str:
        return self.message.said


@pytest.fixture(autouse=True)
def bot():
    """A brand-new recorder per test.

    Rebinding rather than resetting: one test replaces `send_photo` to simulate a
    dead `file_id`, and on a reused instance that patch would leak into every test
    that ran after it.
    """
    global _BOT
    _BOT = _StubBot()
    return _BOT


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


async def _edit(state, field: str, value: str) -> str:
    """Tap a field's edit button, send a new value, return what is on screen.

    The panel is edited in place, so "what the bot said" is not a property of any
    single message any more — it is the current contents of the card.
    """
    await cr.cb_edit(_Cb(f"guess_new:edit:{field}"), state)
    await cr.fsm_edit_value(_Msg(value), state)
    return _BOT.screen


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


class TestTheCardIsOneMessage:
    """The card is a panel, not a feed.

    Re-sending it (and the medium with it) on every change buried the chat in
    stale copies and fired a media upload per edit — which Telegram rate-limits,
    so the bot looked like it had frozen. Editing one message in place fixes the
    mess and the stall with the same change.
    """

    async def test_editing_fields_never_posts_a_second_card(self, state, bot):
        await _to_card(state)
        posted_after_card = len(bot.posted)

        await _edit(state, "max_attempts", "8")
        await _edit(state, "prizes", "500 250 100 50")
        await _edit(state, "aliases", "GTA SA")

        assert len(bot.posted) == posted_after_card, (
            f"three edits posted {len(bot.posted) - posted_after_card} new messages"
        )
        assert bot.edits, "the card must be updated in place"

    async def test_the_medium_is_sent_once_not_once_per_edit(self, state, bot):
        """The upload burst that made the bot hang."""
        await _to_card(state)

        for _ in range(5):
            await _edit(state, "max_attempts", "7")

        assert bot.sent == [("photo", "F")], f"medium sent {len(bot.sent)}x"

    async def test_opening_a_field_reuses_the_card_message(self, state, bot):
        """The prompt replaces the card instead of stacking under it."""
        await _to_card(state)
        posted_before = len(bot.posted)

        await cr.cb_edit(_Cb("guess_new:edit:max_attempts"), state)

        assert len(bot.posted) == posted_before

    async def test_replacing_the_medium_removes_the_old_one(self, state, bot):
        """Two media in the chat and the admin cannot tell which one is live."""
        await _to_card(state)
        first_media_id = (await state.get_data())["media_message_id"]

        await cr.cb_edit(_Cb("guess_new:edit:media"), state)
        await cr.fsm_media(_Msg(photo=[_Photo("NEW")]), state)

        assert first_media_id in bot.deleted

    async def test_publishing_turns_the_card_into_the_confirmation(
        self, state, session, bot
    ):
        """No orphan card left behind: its «✏️ campo» buttons would still be
        tappable and would edit a round that no longer exists in the flow."""
        await _to_card(state)
        card_id = (await state.get_data())["card_message_id"]

        await cr.cb_publish(_Cb("guess_new:publish"), state, session)

        edited_ids = [mid for mid, _ in bot.edits]
        assert card_id in edited_ids, "the card must become the confirmation"
        assert await state.get_state() is None


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
        ("title", "ab"),
        ("answer", "x"),
    ])
    async def test_a_bad_value_is_refused_and_the_field_stays_open(
        self, state, field, bad
    ):
        await _to_card(state)

        screen = await _edit(state, field, bad)

        assert await state.get_state() == cr.GuessCreationStates.editing.state
        assert "⚠️" in screen

    async def test_a_refused_value_does_not_overwrite_the_old_one(self, state):
        await _to_card(state)

        await _edit(state, "max_attempts", "0")

        assert (await state.get_data())["max_attempts"] == (
            cr.settings.guess_default_attempts
        )

    async def test_too_many_aliases_are_refused(self, state):
        await _to_card(state)

        screen = await _edit(state, "aliases", "\n".join(f"a{i}" for i in
                                                      range(cr._MAX_ALIASES + 1)))

        assert "⚠️" in screen

    async def test_an_over_long_alias_is_refused(self, state):
        await _to_card(state)

        screen = await _edit(state, "aliases", "x" * (cr._MAX_ALIAS + 1))

        assert "⚠️" in screen


def _buttons(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row
            if b.callback_data]


async def _add_hint(state, text: str, after: int) -> None:
    """Add a hint the way an admin does: tap ➕, type the text, tap a number."""
    await cr.cb_hint_add(_Cb("guess_new:hint:add"), state)
    await cr.fsm_hint_text(_Msg(text), state)
    await cr.cb_hint_at(_Cb(f"guess_new:hint:at:{after}"), state)


class TestAddingHints:
    """No syntax. At all.

    The old field wanted `3 | È uno sparatutto` typed by hand: a separator
    character, an argument order and a magic number, i.e. three things an admin
    who does not write code has no reason to guess. The threshold now comes from
    a button, so the only free text is the hint itself — and a value that never
    passes through a parser is a value that cannot be malformed.
    """

    async def test_the_prompt_shows_no_syntax(self, state, bot):
        await _to_card(state)

        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)

        assert "|" not in bot.screen, "the pipe syntax must be gone"

    async def test_adding_asks_only_for_the_text(self, state, bot):
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)

        await cr.cb_hint_add(_Cb("guess_new:hint:add"), state)

        assert await state.get_state() == cr.GuessCreationStates.waiting_hint_text.state
        assert "|" not in bot.screen

    async def test_the_thresholds_are_offered_as_buttons(self, state, bot):
        await _to_card(state)
        await _edit(state, "max_attempts", "4")
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await cr.cb_hint_add(_Cb("guess_new:hint:add"), state)

        cb = _Cb("x")
        await cr.fsm_hint_text(_Msg("È uno sparatutto"), state)

        assert cb is not None
        assert bot.last_kb is not None
        picks = [b for b in _buttons(bot.last_kb) if b.startswith("guess_new:hint:at:")]
        assert picks == [f"guess_new:hint:at:{n}" for n in (1, 2, 3, 4)]

    async def test_picking_a_number_stores_the_hint(self, state):
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)

        await _add_hint(state, "È uno sparatutto", 2)

        assert (await state.get_data())["hints"] == [[2, "È uno sparatutto"]]

    async def test_a_taken_threshold_is_not_offered_again(self, state, bot):
        """Two hints on one number is not refused with a message — it is simply
        not offered, which is the version that cannot be got wrong."""
        await _to_card(state)
        await _edit(state, "max_attempts", "3")
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await _add_hint(state, "primo", 2)

        await cr.cb_hint_add(_Cb("guess_new:hint:add"), state)
        await cr.fsm_hint_text(_Msg("secondo"), state)

        picks = [b for b in _buttons(bot.last_kb) if b.startswith("guess_new:hint:at:")]
        assert "guess_new:hint:at:2" not in picks
        assert picks == ["guess_new:hint:at:1", "guess_new:hint:at:3"]

    async def test_the_hints_can_be_removed_one_at_a_time(self, state):
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await _add_hint(state, "primo", 1)
        await _add_hint(state, "secondo", 2)

        await cr.cb_hint_undo(_Cb("guess_new:hint:undo"), state)

        assert (await state.get_data())["hints"] == [[1, "primo"]]

    async def test_they_can_all_be_cleared(self, state):
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await _add_hint(state, "primo", 1)

        await cr.cb_hint_clear(_Cb("guess_new:hint:clear"), state)

        assert (await state.get_data())["hints"] == []

    async def test_done_goes_back_to_the_card(self, state):
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)

        await cr.cb_hint_done(_Cb("guess_new:hint:done"), state)

        assert await state.get_state() == cr.GuessCreationStates.card.state

    async def test_an_over_long_hint_is_refused(self, state, bot):
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await cr.cb_hint_add(_Cb("guess_new:hint:add"), state)

        await cr.fsm_hint_text(_Msg("x" * (cr._MAX_HINT + 1)), state)

        assert "⚠️" in bot.screen
        assert await state.get_state() == cr.GuessCreationStates.waiting_hint_text.state

    async def test_an_empty_hint_is_refused(self, state, bot):
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await cr.cb_hint_add(_Cb("guess_new:hint:add"), state)

        await cr.fsm_hint_text(_Msg("   "), state)

        assert "⚠️" in bot.screen


class TestHintsCannotBeCorrupted:
    """The threshold arrives as callback data, and callback data is user input.

    A button that only offers valid numbers is the intuitive half; refusing an
    invalid one anyway is the safe half. A crafted `guess_new:hint:at:99` must
    not create a hint no player could ever reach.
    """

    @pytest.mark.parametrize("forged", ["99", "0", "-3", "abc", ""])
    async def test_a_forged_threshold_is_refused(self, state, forged):
        await _to_card(state)
        await _edit(state, "max_attempts", "3")
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await cr.cb_hint_add(_Cb("guess_new:hint:add"), state)
        await cr.fsm_hint_text(_Msg("payload"), state)

        await cr.cb_hint_at(_Cb(f"guess_new:hint:at:{forged}"), state)

        assert (await state.get_data())["hints"] == []

    async def test_a_duplicate_threshold_is_refused_even_if_forged(self, state):
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await _add_hint(state, "primo", 2)
        await cr.cb_hint_add(_Cb("guess_new:hint:add"), state)
        await cr.fsm_hint_text(_Msg("secondo"), state)

        await cr.cb_hint_at(_Cb("guess_new:hint:at:2"), state)

        assert (await state.get_data())["hints"] == [[2, "primo"]]

    async def test_picking_a_number_without_a_pending_text_does_nothing(self, state):
        """A stale button from an older screen must not invent a hint."""
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)

        await cr.cb_hint_at(_Cb("guess_new:hint:at:2"), state)

        assert (await state.get_data())["hints"] == []

    async def test_the_hint_cap_is_enforced(self, state, bot):
        await _to_card(state)
        await _edit(state, "max_attempts", str(cr._MAX_HINTS + 5))
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        for n in range(1, cr._MAX_HINTS + 1):
            await _add_hint(state, f"hint {n}", n)

        await cr.cb_hint_add(_Cb("guess_new:hint:add"), state)

        assert len((await state.get_data())["hints"]) == cr._MAX_HINTS
        assert "massimo" in bot.screen.lower()

    async def test_lowering_the_attempts_drops_unreachable_hints(self, state, bot):
        """The hole the old flow had.

        The threshold was checked only as it was typed. Setting 10 attempts,
        adding a hint at 8 and then dropping to 3 left a hint no player could
        ever reach — exactly what that check exists to prevent.
        """
        await _to_card(state)
        await _edit(state, "max_attempts", "10")
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await _add_hint(state, "presto", 2)
        await _add_hint(state, "tardi", 8)
        await cr.cb_hint_done(_Cb("guess_new:hint:done"), state)

        await _edit(state, "max_attempts", "3")

        assert (await state.get_data())["hints"] == [[2, "presto"]]
        assert "1</b> suggerimento:" in bot.screen, "singular, and it says which"
        assert "non lo avrebbe visto nessuno" in bot.screen

    async def test_publishing_can_never_store_an_unreachable_hint(
        self, state, session
    ):
        """The last gate before the data becomes a round that pays coins.

        Every path into `hints` is guarded, so this cannot be reached today — it
        is here so that a *future* path which sets `max_attempts` without pruning
        cannot quietly ship a hint nobody will see. The state is corrupted
        directly, because no sequence of taps can produce it.
        """
        await _to_card(state)
        await state.update_data(max_attempts=3, hints=[[2, "ok"], [9, "mai vista"]])

        await cr.cb_publish(_Cb("guess_new:publish"), state, session)

        r = (await session.execute(select(GuessRound))).scalar_one()
        assert gs.hints_of(r) == [(2, "ok")]

    async def test_leaving_the_hints_screen_drops_a_half_written_hint(self, state):
        """Text typed but never given a number must not attach itself later, when
        a stale threshold button from that screen gets tapped."""
        await _to_card(state)
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await cr.cb_hint_add(_Cb("guess_new:hint:add"), state)
        await cr.fsm_hint_text(_Msg("mai confermato"), state)

        await cr.cb_edit(_Cb("guess_new:edit:max_attempts"), state)
        await cr.cb_hint_at(_Cb("guess_new:hint:at:2"), state)

        assert (await state.get_data())["hints"] == []

    async def test_raising_the_attempts_keeps_every_hint(self, state):
        await _to_card(state)
        await _edit(state, "max_attempts", "3")
        await cr.cb_edit(_Cb("guess_new:edit:hints"), state)
        await _add_hint(state, "uno", 2)
        await cr.cb_hint_done(_Cb("guess_new:hint:done"), state)

        await _edit(state, "max_attempts", "9")

        assert (await state.get_data())["hints"] == [[2, "uno"]]


class TestPublish:
    async def test_a_published_round_is_ready_and_complete(self, state, session):
        await _to_card(state, answer="Doom")
        await _edit(state, "aliases", "DOOM 1993")
        await _add_hint(state, "sparatutto", 2)
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

    async def test_the_confirmation_offers_start_and_schedule(self, state, session, bot):
        cb = _Cb("guess_new:publish")

        await _to_card(state)
        await cr.cb_publish(cb, state, session)

        assert "creato" in bot.screen.lower()


class TestCancel:
    async def test_cancelling_asks_first(self, state, bot):
        await _to_card(state)
        cb = _Cb("guess_new:cancel")

        await cr.cb_cancel(cb, state)

        assert "sicuro" in bot.screen.lower()
        assert await state.get_state() is not None, "not cancelled until confirmed"

    async def test_cancelling_outside_a_flow_is_a_no_op(self, state):
        cb = _Cb("guess_new:cancel")

        await cr.cb_cancel(cb, state)

        assert cb.said == ""

    async def test_confirming_clears_the_flow(self, state):
        await _to_card(state)

        await cr.cb_cancel_yes(_Cb("guess_new:cancel_yes"), state)

        assert await state.get_state() is None
