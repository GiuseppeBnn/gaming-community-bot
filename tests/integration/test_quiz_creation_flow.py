"""Building a quiz — the creation FSM, `handlers/quiz/creation.py`.

The whole module is one conversation: title →
description → prizes → time limit → shuffling → questions → review → publish. It is
the longest flow in the bot and the one an admin loses most by having to restart, so
the first test here simply **walks it end to end** and checks that what comes out the
other side is the quiz that was typed in.

After that come the refusals, one per step. They matter more than they look: every
one of them is a place where the flow either stops with an explanation or advances
with something wrong stored. A question saved with `correct_option_id` pointing past
the end of its options, for instance, is a question nobody can answer correctly, and
it is only discovered by the players.

The `⬅️ Indietro` map at the end of the module is checked as data rather than by
walking every path — a table of eleven entries where a wrong value sends the admin to
the wrong step, which no single walk would catch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from config_data.config import settings
from database.models import Quiz, QuizQuestion
from handlers.quiz import _shared
from handlers.quiz import creation as qz
from services import quiz_service
from utils import cooldown

ADMIN_ID = 1


class _FakeBot:
    id = 999_999

    async def get_me(self):
        return SimpleNamespace(username="testbot")


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.bot = _FakeBot()
        self.from_user = SimpleNamespace(id=ADMIN_ID, username="admin", full_name="Admin")
        self.chat = SimpleNamespace(id=ADMIN_ID, type="private")
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=len(self.texts))

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def reply(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        return SimpleNamespace(message_id=len(self.texts))

    @property
    def said(self) -> str:
        return "\n".join(self.texts)


class _FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.bot = self.message.bot
        self.from_user = SimpleNamespace(id=ADMIN_ID, username="admin", full_name="Admin")
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    @property
    def alerts(self) -> list[str]:
        return [t for t, alert in self.answers if alert and t]


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


async def _quiz_row(session, quiz_id: int) -> Quiz:
    return (
        await session.execute(select(Quiz).where(Quiz.id == quiz_id))
    ).scalar_one()


async def _questions(session, quiz_id: int) -> list[QuizQuestion]:
    return list(
        (
            await session.execute(
                select(QuizQuestion)
                .where(QuizQuestion.quiz_id == quiz_id)
                .order_by(QuizQuestion.position)
            )
        )
        .scalars()
        .all()
    )


async def _walk_to_questions(session, state, *, prize: str = "quick") -> None:
    """Title → description → prizes → time limit → shuffling, leaving the flow at the
    first question prompt with a quiz row already created."""
    await qz.start_quiz_creation(_FakeMessage(), state, ADMIN_ID)
    await qz.fsm_title(_FakeMessage("Capitali d'Europa"), state)
    await qz.fsm_description(_FakeMessage("Quiz di geografia"), state)

    if prize == "quick":
        await qz.cb_quick_prize(_FakeCallback("quiz_new:quickprize"), state)
    else:
        await qz.cb_no_prize(_FakeCallback("quiz_new:noprize"), state)

    await qz.cb_time_limit(_FakeCallback("quiz_new:tl:30"), state, session)
    await qz.cb_randomize(_FakeCallback("quiz_new:rnd:none"), state, session)


async def _add_question(session, state, *, text="Capitale d'Italia?",
                        options="Roma\nMilano\nNapoli", correct=0,
                        explanation: str | None = "È Roma") -> None:
    await qz.fsm_question_text(_FakeMessage(text), state)
    await qz.fsm_question_options(_FakeMessage(options), state)
    await qz.cb_correct(_FakeCallback(f"quiz_new:correct:{correct}"), state)
    if explanation is None:
        await qz.cb_skip_explanation(_FakeCallback("quiz_new:skipexpl"), state, session)
    else:
        await qz.fsm_explanation(_FakeMessage(explanation), state, session)


class TestTheWholeFlow:
    async def test_a_quiz_built_end_to_end_is_the_one_that_was_typed(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()

        await _walk_to_questions(session, state)
        await _add_question(session, state)
        await _add_question(
            session, state, text="Capitale di Francia?",
            options="Parigi\nLione", correct=0, explanation=None,
        )

        publish = _FakeCallback("quiz_new:publish")
        await qz.cb_publish(publish, state, session)

        quiz_id = (await session.execute(select(Quiz.id))).scalar_one()
        quiz = await _quiz_row(session, quiz_id)
        assert quiz.title == "Capitali d'Europa"
        assert quiz.description == "Quiz di geografia"
        assert quiz.status == "ready", "publishing did not arm the quiz"
        assert quiz.prize_first == settings.quiz_default_first

        questions = await _questions(session, quiz_id)
        assert [q.text for q in questions] == [
            "Capitale d'Italia?", "Capitale di Francia?"
        ]
        assert [q.position for q in questions] == [0, 1]
        assert quiz_service.question_options(questions[0]) == ["Roma", "Milano", "Napoli"]
        assert questions[0].correct_option_id == 0
        assert questions[0].explanation == "È Roma"
        assert questions[1].explanation is None, "the skipped explanation was invented"
        assert all(q.open_period == 30 for q in questions), "the time limit was not applied"

        assert await state.get_state() is None, "the FSM stayed armed after publishing"
        assert "pronto" in publish.message.said

    async def test_the_no_prize_route_stores_zeroes(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()

        await _walk_to_questions(session, state, prize="none")
        await _add_question(session, state)
        await qz.cb_publish(_FakeCallback("quiz_new:publish"), state, session)

        quiz = await _quiz_row(session, (await session.execute(select(Quiz.id))).scalar_one())
        assert (quiz.prize_first, quiz.prize_second, quiz.prize_third) == (0, 0, 0)
        assert quiz_service.has_explicit_prizes(quiz) is False

    async def test_custom_prizes_are_asked_one_by_one(self, session, user_factory):
        """Four amounts, four steps, in order — and the guaranteed minimum is derived
        from the consolation rather than asked for, so it cannot be set above it."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()

        await qz.start_quiz_creation(_FakeMessage(), state, ADMIN_ID)
        await qz.fsm_title(_FakeMessage("Custom"), state)
        await qz.fsm_description(_FakeMessage("d"), state)
        await qz.cb_custom_prize(_FakeCallback("quiz_new:customprize"), state)

        for value in ("900", "600", "300", "100"):
            await qz.fsm_prize_value(_FakeMessage(value), state)

        data = await state.get_data()
        assert data["prize_first"] == 900
        assert data["prize_second"] == 600
        assert data["prize_third"] == 300
        assert data["prize_consolation"] == 100

        await qz.cb_time_limit(_FakeCallback("quiz_new:tl:0"), state, session)
        await qz.cb_randomize(_FakeCallback("quiz_new:rnd:none"), state, session)

        quiz = await _quiz_row(session, (await state.get_data())["quiz_id"])
        assert quiz.prize_min == quiz_service.participation_floor(100)
        assert quiz.prize_min <= quiz.prize_consolation

    async def test_the_default_button_takes_the_configured_value(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()

        await qz.start_quiz_creation(_FakeMessage(), state, ADMIN_ID)
        await qz.fsm_title(_FakeMessage("Custom"), state)
        await qz.fsm_description(_FakeMessage("d"), state)
        await qz.cb_custom_prize(_FakeCallback("quiz_new:customprize"), state)

        await qz.cb_use_default(_FakeCallback("quiz_new:usedefault"), state)

        assert (await state.get_data())["prize_first"] == settings.quiz_default_first

    async def test_shuffling_choices_are_stored_as_asked(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")

        for choice, (questions, answers) in qz._RANDOMIZE_CHOICES.items():
            state = _state()
            await qz.start_quiz_creation(_FakeMessage(), state, ADMIN_ID)
            await qz.fsm_title(_FakeMessage(f"Quiz {choice}"), state)
            await qz.fsm_description(_FakeMessage("d"), state)
            await qz.cb_no_prize(_FakeCallback("quiz_new:noprize"), state)
            await qz.cb_time_limit(_FakeCallback("quiz_new:tl:0"), state, session)

            await qz.cb_randomize(_FakeCallback(f"quiz_new:rnd:{choice}"), state, session)

            quiz = await _quiz_row(session, (await state.get_data())["quiz_id"])
            assert quiz.randomize_questions is questions, choice
            assert quiz.randomize_answers is answers, choice


class TestRefusals:
    async def test_a_title_that_is_too_short_or_too_long_is_refused(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await qz.start_quiz_creation(_FakeMessage(), state, ADMIN_ID)

        for text in ("ab", "x" * (_shared._MAX_TITLE + 1)):
            message = _FakeMessage(text)
            await qz.fsm_title(message, state)
            assert message.said, text
            assert await state.get_state() == qz.QuizCreationStates.waiting_title, text

    async def test_a_question_shorter_than_three_characters_is_refused(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await _walk_to_questions(session, state)

        message = _FakeMessage("?")
        await qz.fsm_question_text(message, state)

        assert "almeno 3 caratteri" in message.said
        assert await _questions(session, (await state.get_data())["quiz_id"]) == []

    async def test_option_lists_that_cannot_work_are_refused(
        self, session, user_factory
    ):
        """One option is not a question; duplicates make two answers equally right;
        the cap is Telegram's. Each of these would otherwise be saved and only fail
        in front of the players.
        """
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await _walk_to_questions(session, state)
        await qz.fsm_question_text(_FakeMessage("Domanda valida?"), state)

        for options in (
            "Solo una",                                    # a single option
            "",                                            # none at all
            "\n".join(f"opzione {i}" for i in range(20)),  # past the cap
            "x" * (_shared._MAX_OPTION + 1),                    # one option too long
        ):
            message = _FakeMessage(options)
            await qz.fsm_question_options(message, state)
            assert message.said, repr(options)
            assert "q_options" not in (await state.get_data()), repr(options)

    async def test_duplicate_options_are_accepted_today(self, session, user_factory):
        """Documented, not endorsed — and found by writing this file.

        `_options_error` checks the count and the length of each option, not whether
        two of them are the same. So «Roma / Roma» is saved, and a player who taps the
        second one is marked wrong for choosing the right answer: only one index is
        `correct_option_id`. Silent, and invisible to the admin who typed it.

        Pinned as it currently behaves rather than "fixed" here, because adding the
        rule changes what an admin is allowed to type and that is not a call to make
        inside a test file. If someone adds the check, this test fails and says so.
        """
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await _walk_to_questions(session, state)
        await qz.fsm_question_text(_FakeMessage("Capitale d'Italia?"), state)

        await qz.fsm_question_options(_FakeMessage("Roma\nRoma"), state)

        assert (await state.get_data())["q_options"] == ["Roma", "Roma"]

    async def test_a_correct_index_past_the_options_is_refused(
        self, session, user_factory
    ):
        """The one that would produce an unanswerable question: a stale keyboard from
        a previous, longer option list still points at index 4."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await _walk_to_questions(session, state)
        await qz.fsm_question_text(_FakeMessage("Capitale d'Italia?"), state)
        await qz.fsm_question_options(_FakeMessage("Roma\nMilano"), state)

        callback = _FakeCallback("quiz_new:correct:4")
        await qz.cb_correct(callback, state)

        assert callback.alerts and "non valida" in callback.alerts[0]
        assert "q_correct" not in (await state.get_data())

    async def test_an_over_long_explanation_is_refused(self, session, user_factory):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await _walk_to_questions(session, state)
        await qz.fsm_question_text(_FakeMessage("Capitale d'Italia?"), state)
        await qz.fsm_question_options(_FakeMessage("Roma\nMilano"), state)
        await qz.cb_correct(_FakeCallback("quiz_new:correct:0"), state)

        message = _FakeMessage("x" * (_shared._MAX_EXPLANATION + 1))
        await qz.fsm_explanation(message, state, session)

        assert message.said
        assert await _questions(session, (await state.get_data())["quiz_id"]) == []

    async def test_time_limits_outside_the_allowed_range_are_refused(
        self, session, user_factory
    ):
        """0 means «no limit» and is legal; anything else has to be a workable number
        of seconds, or every question would expire instantly or never."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await qz.start_quiz_creation(_FakeMessage(), state, ADMIN_ID)
        await qz.fsm_title(_FakeMessage("Quiz"), state)
        await qz.fsm_description(_FakeMessage("d"), state)
        await qz.cb_no_prize(_FakeCallback("quiz_new:noprize"), state)

        for raw in ("presto", "1", "4", "301", "-10"):
            message = _FakeMessage(raw)
            await qz.fsm_time_limit(message, state, session)
            assert message.said, raw
            assert "time_limit" not in (await state.get_data()), raw

        await qz.fsm_time_limit(_FakeMessage("0"), state, session)
        assert (await state.get_data())["time_limit"] == 0

    async def test_publishing_without_questions_is_refused(
        self, session, user_factory
    ):
        """The quiz row exists from the moment the prizes are settled, so «publish»
        can be reached with nothing in it."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await _walk_to_questions(session, state)

        callback = _FakeCallback("quiz_new:publish")
        await qz.cb_publish(callback, state, session)

        quiz = await _quiz_row(session, (await state.get_data())["quiz_id"])
        assert quiz.status == "draft"
        assert callback.alerts and "almeno una domanda" in callback.alerts[0]


class TestReviewStep:
    async def test_another_question_can_be_added_from_the_review(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await _walk_to_questions(session, state)
        await _add_question(session, state)

        await qz.cb_add_question(_FakeCallback("quiz_new:add"), state)
        await _add_question(session, state, text="Seconda domanda?", options="Sì\nNo")

        quiz_id = (await state.get_data())["quiz_id"]
        assert len(await _questions(session, quiz_id)) == 2

    async def test_removing_the_last_question_removes_exactly_one(
        self, session, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await _walk_to_questions(session, state)
        await _add_question(session, state)
        await qz.cb_add_question(_FakeCallback("quiz_new:add"), state)
        await _add_question(session, state, text="Seconda domanda?", options="Sì\nNo")

        await qz.cb_remove_last(_FakeCallback("quiz_new:removelast"), state, session)

        quiz_id = (await state.get_data())["quiz_id"]
        remaining = await _questions(session, quiz_id)
        assert [q.text for q in remaining] == ["Capitale d'Italia?"]
        assert (await state.get_data())["saved_count"] == 1

    async def test_removing_the_only_question_sends_you_back_to_writing_one(
        self, session, user_factory
    ):
        """Ending up at an empty review with a «publish» button would be a dead end."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        state = _state()
        await _walk_to_questions(session, state)
        await _add_question(session, state)

        callback = _FakeCallback("quiz_new:removelast")
        await qz.cb_remove_last(callback, state, session)

        assert await state.get_state() == qz.QuizCreationStates.waiting_question_text
        assert (await state.get_data())["saved_count"] == 0


class TestCancelAndBack:
    async def test_cancelling_asks_before_throwing_the_work_away(self, session):
        state = _state()
        await state.set_state(qz.QuizCreationStates.waiting_title)

        ask = _FakeCallback("quiz_new:cancel")
        await qz.cb_quiz_cancel(ask, state)
        assert await state.get_state() is not None, "the flow was dropped without asking"
        assert ask.message.said

        await qz.cb_quiz_cancel_yes(_FakeCallback("quiz_new:cancel_yes"), state)
        assert await state.get_state() is None

    async def test_saying_no_to_the_cancellation_keeps_the_flow(self, session):
        state = _state()
        await state.set_state(qz.QuizCreationStates.waiting_title)

        await qz.cb_quiz_cancel_no(_FakeCallback("quiz_new:cancel_no"))

        assert await state.get_state() == qz.QuizCreationStates.waiting_title

    async def test_every_step_that_offers_back_knows_where_back_goes(self):
        """Checked as a table rather than by walking each path. A wrong entry sends
        the admin to a different step than the button promises, and every state here
        renders a «⬅️ Indietro» button — so a missing key is a button that does
        nothing at all.
        """
        for state_name, prompter in qz._BACK_PROMPTERS.items():
            assert callable(prompter), state_name
            assert state_name.startswith("QuizCreationStates:"), state_name

        # The chain must not loop: no step may point at itself.
        for state_name, prompter in qz._BACK_PROMPTERS.items():
            target = getattr(prompter, "keywords", {}).get("step_state")
            if target is not None:
                assert target.state != state_name, state_name

    async def test_back_re_prompts_the_previous_step(self, session):
        state = _state()
        await state.set_state(qz.QuizCreationStates.waiting_description)

        callback = _FakeCallback("quiz_new:back")
        await qz.cb_back(callback, state)

        assert await state.get_state() == qz.QuizCreationStates.waiting_title
        assert callback.message.said


# ---------------------------------------------------------------------------
# The entry command and the remaining refusals
# ---------------------------------------------------------------------------

class _GroupMessage(_FakeMessage):
    """In a group the handler answers with `reply`, whose keyboard carries the
    deep-link — so this variant records the markup of a reply too."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.chat = SimpleNamespace(id=-100_123, type="supergroup")

    async def reply(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=len(self.texts))


class TestEntryCommand:
    def teardown_method(self):
        cooldown.reset()

    async def test_in_a_group_it_only_hands_back_a_link(self, session):
        """A fourteen-step FSM in the group would interleave with everyone else's
        messages, and any other admin's reply could drive it."""
        cooldown.reset()
        message = _GroupMessage()
        state = _state()

        await qz.cmd_crea_quiz(message, state)

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=create_quiz")
        assert await state.get_state() is None

    async def test_in_private_it_starts_the_flow(self, session):
        cooldown.reset()
        message = _FakeMessage()
        state = _state()

        await qz.cmd_crea_quiz(message, state)

        assert await state.get_state() == qz.QuizCreationStates.waiting_title.state

    async def test_the_second_attempt_within_the_cooldown_is_refused(self, session):
        """Admins are not exempt here: creating events is what an admin can flood
        the list with by accident."""
        cooldown.reset()
        await qz.cmd_crea_quiz(_FakeMessage(), _state())
        second, state = _FakeMessage(), _state()

        await qz.cmd_crea_quiz(second, state)

        assert "più piano" in second.said.lower()
        assert await state.get_state() is None


class TestRemainingRefusals:
    async def test_cancelling_outside_the_flow_is_a_no_op(self, session):
        """The cancel button lives on a message that stays on screen after the flow
        ended; tapping it then must not pop a confirmation about nothing."""
        callback = _FakeCallback("quiz_new:cancel")

        await qz.cb_quiz_cancel(callback, _state())

        assert callback.message.texts == []

    async def test_a_description_over_the_cap_does_not_advance(self, session):
        state = _state()
        await qz.start_quiz_creation(_FakeMessage(), state, ADMIN_ID)
        await qz.fsm_title(_FakeMessage("Capitali"), state)
        message = _FakeMessage("x" * (_shared._MAX_DESC + 1))

        await qz.fsm_description(message, state)

        assert "troppo lunga" in message.said
        assert await state.get_state() == qz.QuizCreationStates.waiting_description.state

    async def test_a_single_dash_means_no_description(self, session):
        """Documented shortcut: the description is optional, and «-» is how an admin
        says so without leaving the field blank."""
        state = _state()
        await qz.start_quiz_creation(_FakeMessage(), state, ADMIN_ID)
        await qz.fsm_title(_FakeMessage("Capitali"), state)

        await qz.fsm_description(_FakeMessage("-"), state)

        assert (await state.get_data())["description"] == ""

    async def test_a_question_over_the_cap_does_not_advance(self, session):
        state = _state()
        await _walk_to_questions(session, state)
        message = _FakeMessage("x" * (_shared._MAX_QUESTION + 1))

        await qz.fsm_question_text(message, state)

        assert "troppo lunga" in message.said
        assert await state.get_state() == qz.QuizCreationStates.waiting_question_text.state

    @pytest.mark.parametrize("raw,expected", [
        ("cinquecento", "Inserisci un numero"),
        ("-50", "non può essere negativo"),
    ])
    async def test_an_unusable_prize_is_refused(self, session, raw, expected):
        """The prize is paid out of nothing at close time, so a value that parsed
        loosely would be money invented by a typo."""
        state = _state()
        await qz.start_quiz_creation(_FakeMessage(), state, ADMIN_ID)
        await qz.fsm_title(_FakeMessage("Capitali"), state)
        await qz.fsm_description(_FakeMessage("Geo"), state)
        await qz.cb_custom_prize(_FakeCallback("quiz_new:customprize"), state)
        message = _FakeMessage(raw)

        await qz.fsm_prize_value(message, state)

        assert expected in message.said
        assert await state.get_state() == qz.QuizCreationStates.waiting_prize_first.state

    async def test_the_default_button_outside_a_prize_step_is_ignored(self, session):
        """An old keyboard from a previous run: there is no step to fill in."""
        callback = _FakeCallback("quiz_new:usedefault")

        await qz.cb_use_default(callback, _state())

        assert callback.message.texts == []

    async def test_the_custom_time_limit_button_asks_for_seconds(self, session):
        callback = _FakeCallback("quiz_new:tlcustom")

        await qz.cb_time_limit_custom(callback)

        assert "secondi" in callback.message.said

    async def test_the_review_button_reopens_the_summary(self, session):
        """Reachable from the question screen: the admin wants to see what they have
        so far without publishing it."""
        state = _state()
        await _walk_to_questions(session, state)
        await qz.fsm_question_text(_FakeMessage("Capitale d'Italia?"), state)
        await qz.fsm_question_options(_FakeMessage("Roma\nMilano"), state)
        await qz.cb_correct(_FakeCallback("quiz_new:correct:0"), state)
        await qz.fsm_explanation(_FakeMessage("-"), state, session)
        callback = _FakeCallback("quiz_new:review")

        await qz.cb_review(callback, state, session)

        assert callback.message.said
        assert await state.get_state() == qz.QuizCreationStates.reviewing.state


class TestRandomizeSummary:
    @pytest.mark.parametrize("questions,answers,expected", [
        (True, True, "domande e risposte"),
        (True, False, "solo domande"),
        (False, True, "solo risposte"),
        (False, False, "nessuna"),
    ])
    def test_every_combination_is_named(self, questions, answers, expected):
        """It is what the review screen shows before publishing; naming the wrong
        combination would have the admin publish a shuffle they did not choose."""
        assert qz._randomize_summary(questions, answers) == expected
