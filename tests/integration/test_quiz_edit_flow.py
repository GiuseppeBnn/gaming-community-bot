"""Editing the questions of a ready quiz — `handlers/quiz/editing.py`.

An admin builds a quiz, spots a typo, and fixes it before launching. Four fields can
be changed one at a time (text, options, which option is correct, explanation) plus a
«rifai» path that walks all of them in one go — and both paths run through the *same*
handlers, told apart by an `edit_redo` flag in the FSM. That sharing is the reason
this flow deserves tests: a single-field edit that accidentally takes the redo branch
saves a half-built question, and a redo that takes the single-field branch saves only
the first field the admin typed.

The other thing pinned here is the window in which editing is legal at all. Recorded
answers reference options by **stored index**, so changing them under a running quiz
would silently reassign what every player already answered. `update_question` refuses
anything that is not `ready`; these tests check the handlers respect that refusal
instead of reporting a success that did not happen.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers.callbacks import QuizEditCb
from handlers.quiz import _shared
from handlers.quiz import editing as qz
from services import quiz_service

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

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        return SimpleNamespace(message_id=len(self.texts))

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)

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


async def _ready_quiz(session, user_factory, *, n_questions: int = 2):
    await user_factory(tg_id=ADMIN_ID, username="admin")
    quiz = await quiz_service.create_quiz(session, ADMIN_ID, "Capitali", "Geo")
    for i in range(n_questions):
        await quiz_service.add_question(
            session,
            quiz.id,
            f"Domanda {i}?",
            ["Roma", "Milano", "Napoli"],
            correct_option_id=0,
            explanation=f"Spiegazione {i}",
        )
    await quiz_service.set_status(session, quiz.id, "ready")
    await session.commit()
    return quiz


async def _question(session, quiz_id: int, idx: int = 0):
    quiz = await quiz_service.get_quiz(session, quiz_id)
    return quiz.questions[idx]


class TestViewing:
    async def test_the_editor_shows_the_question_and_marks_the_right_answer(
        self, session, user_factory
    ):
        quiz = await _ready_quiz(session, user_factory)
        cb = _FakeCallback(QuizEditCb(action="nav", quiz_id=quiz.id, index=0).pack())

        await qz.cb_edit_nav(
            cb, _state(), session, QuizEditCb(action="nav", quiz_id=quiz.id, index=0)
        )

        assert "Domanda 1/2" in cb.message.said
        assert "✅ Roma" in cb.message.said, "the correct option is not marked"
        assert "Spiegazione 0" in cb.message.said

    async def test_scrolling_past_the_end_clamps_instead_of_failing(self, session, user_factory):
        """The navigation buttons are built from a count that can be stale after a
        question is removed elsewhere; an out-of-range index must land on the last
        question, not raise."""
        quiz = await _ready_quiz(session, user_factory)
        cb = _FakeCallback(QuizEditCb(action="nav", quiz_id=quiz.id, index=99).pack())

        await qz.cb_edit_nav(
            cb, _state(), session, QuizEditCb(action="nav", quiz_id=quiz.id, index=99)
        )

        assert "Domanda 2/2" in cb.message.said

    async def test_navigation_without_a_question_locator_is_ignored(self, session, user_factory):
        """Removing either guard would let an incomplete typed payload reach the renderer."""
        cb = _FakeCallback(QuizEditCb(action="nav").pack())

        await qz.cb_edit_nav(cb, _state(), session, QuizEditCb(action="nav"))

        assert cb.message.texts == []
        assert cb.answers == [(None, False)]

    async def test_a_running_quiz_cannot_be_edited(self, session, user_factory):
        """Recorded answers reference options by stored index, so editing a running
        quiz would reassign what players already answered."""
        quiz = await _ready_quiz(session, user_factory)
        await quiz_service.set_status(session, quiz.id, "running")
        await session.commit()

        cb = _FakeCallback(QuizEditCb(action="nav", quiz_id=quiz.id, index=0).pack())
        await qz.cb_edit_nav(
            cb, _state(), session, QuizEditCb(action="nav", quiz_id=quiz.id, index=0)
        )

        assert "solo su un quiz" in cb.message.said

    async def test_a_missing_or_empty_quiz_says_so(self, session, user_factory):
        missing = _FakeCallback(QuizEditCb(action="nav", quiz_id=999999, index=0).pack())
        await qz.cb_edit_nav(
            missing, _state(), session, QuizEditCb(action="nav", quiz_id=999999, index=0)
        )
        assert "non trovato" in missing.message.said

        await user_factory(tg_id=ADMIN_ID, username="admin")
        empty = await quiz_service.create_quiz(session, ADMIN_ID, "Vuoto", "d")
        await quiz_service.set_status(session, empty.id, "ready")
        await session.commit()

        cb = _FakeCallback(QuizEditCb(action="nav", quiz_id=empty.id, index=0).pack())
        await qz.cb_edit_nav(
            cb, _state(), session, QuizEditCb(action="nav", quiz_id=empty.id, index=0)
        )
        assert "non ha domande" in cb.message.said

    async def test_the_noop_button_does_nothing(self):
        cb = _FakeCallback(QuizEditCb(action="noop").pack())
        await qz.cb_edit_noop(cb, QuizEditCb(action="noop"))
        assert cb.message.texts == []


class TestTypedCallbackGuards:
    @pytest.mark.parametrize(
        ("handler", "payload"),
        [
            (qz.cb_edit_nav, QuizEditCb(action="nav", quiz_id=7)),
            (qz.cb_edit_nav, QuizEditCb(action="nav", index=0)),
            (qz.cb_edit_text, QuizEditCb(action="text", quiz_id=7)),
            (qz.cb_edit_text, QuizEditCb(action="text", index=0)),
            (qz.cb_edit_expl, QuizEditCb(action="explanation", quiz_id=7)),
            (qz.cb_edit_expl, QuizEditCb(action="explanation", index=0)),
            (qz.cb_edit_opts, QuizEditCb(action="options", quiz_id=7)),
            (qz.cb_edit_opts, QuizEditCb(action="options", index=0)),
            (qz.cb_edit_redo, QuizEditCb(action="redo", quiz_id=7)),
            (qz.cb_edit_redo, QuizEditCb(action="redo", index=0)),
        ],
    )
    async def test_question_actions_reject_each_missing_locator(self, handler, payload, session):
        """Changing an ``or`` guard to ``and`` would call the service with a partial locator."""
        cb = _FakeCallback(payload.pack())

        await handler(cb, _state(), session, payload)

        assert cb.message.texts == []
        assert cb.answers == [(None, False)]

    async def test_correct_requires_only_its_option_index(self, session):
        """The correct-answer action is scoped by FSM state, never by a quiz id."""
        cb = _FakeCallback(QuizEditCb(action="correct").pack())

        await qz.cb_edit_correct(cb, _state(), session, QuizEditCb(action="correct"))

        assert cb.message.texts == []
        assert cb.answers == [(None, False)]


class TestSingleFieldEdits:
    async def test_the_text_can_be_replaced_without_touching_anything_else(
        self, session, user_factory
    ):
        quiz = await _ready_quiz(session, user_factory)
        state = _state()

        await qz.cb_edit_text(
            _FakeCallback(QuizEditCb(action="text", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="text", quiz_id=quiz.id, index=0),
        )
        await qz.fsm_edit_text(_FakeMessage("Capitale d'Italia?"), state, session)

        q = await _question(session, quiz.id)
        assert q.text == "Capitale d'Italia?"
        assert quiz_service.question_options(q) == ["Roma", "Milano", "Napoli"]
        assert q.correct_option_id == 0
        assert q.explanation == "Spiegazione 0"
        assert await state.get_state() is None

    async def test_a_too_short_text_is_refused_and_the_step_stays_open(self, session, user_factory):
        quiz = await _ready_quiz(session, user_factory)
        state = _state()
        await qz.cb_edit_text(
            _FakeCallback(QuizEditCb(action="text", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="text", quiz_id=quiz.id, index=0),
        )

        message = _FakeMessage("ab")
        await qz.fsm_edit_text(message, state, session)

        assert "almeno 3 caratteri" in message.said
        assert (await _question(session, quiz.id)).text == "Domanda 0?"
        assert await state.get_state() == qz.QuizEditStates.editing_text

    async def test_an_over_long_text_is_refused(self, session, user_factory):
        quiz = await _ready_quiz(session, user_factory)
        state = _state()
        await qz.cb_edit_text(
            _FakeCallback(QuizEditCb(action="text", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="text", quiz_id=quiz.id, index=0),
        )

        message = _FakeMessage("x" * (_shared._MAX_QUESTION + 1))
        await qz.fsm_edit_text(message, state, session)

        assert message.said
        assert (await _question(session, quiz.id)).text == "Domanda 0?"

    async def test_the_explanation_can_be_replaced_and_removed(self, session, user_factory):
        """«-» is the only way to clear it — an empty message would be indistinguishable
        from a mistap, so the sentinel is what makes removal expressible."""
        quiz = await _ready_quiz(session, user_factory)

        state = _state()
        await qz.cb_edit_expl(
            _FakeCallback(QuizEditCb(action="explanation", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="explanation", quiz_id=quiz.id, index=0),
        )
        await qz.fsm_edit_explanation(_FakeMessage("Perché è Roma"), state, session)
        assert (await _question(session, quiz.id)).explanation == "Perché è Roma"

        state = _state()
        await qz.cb_edit_expl(
            _FakeCallback(QuizEditCb(action="explanation", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="explanation", quiz_id=quiz.id, index=0),
        )
        await qz.fsm_edit_explanation(_FakeMessage("-"), state, session)
        assert (await _question(session, quiz.id)).explanation is None

    async def test_an_over_long_explanation_is_refused(self, session, user_factory):
        quiz = await _ready_quiz(session, user_factory)
        state = _state()
        await qz.cb_edit_expl(
            _FakeCallback(QuizEditCb(action="explanation", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="explanation", quiz_id=quiz.id, index=0),
        )

        message = _FakeMessage("x" * (_shared._MAX_EXPLANATION + 1))
        await qz.fsm_edit_explanation(message, state, session)

        assert message.said
        assert (await _question(session, quiz.id)).explanation == "Spiegazione 0"

    async def test_the_options_and_the_correct_one_are_replaced_together(
        self, session, user_factory
    ):
        """They have to move as one: the correct answer is an index into the list, so
        saving new options while keeping the old index would point at the wrong text —
        or past the end."""
        quiz = await _ready_quiz(session, user_factory)
        state = _state()

        await qz.cb_edit_opts(
            _FakeCallback(QuizEditCb(action="options", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="options", quiz_id=quiz.id, index=0),
        )
        await qz.fsm_edit_options(_FakeMessage("Parigi\nLione\nNizza"), state)
        await qz.cb_edit_correct(
            _FakeCallback(QuizEditCb(action="correct", index=1).pack()),
            state,
            session,
            QuizEditCb(action="correct", index=1),
        )

        q = await _question(session, quiz.id)
        assert quiz_service.question_options(q) == ["Parigi", "Lione", "Nizza"]
        assert q.correct_option_id == 1
        assert q.text == "Domanda 0?", "only the answers were meant to change"

    async def test_an_invalid_option_list_is_refused(self, session, user_factory):
        quiz = await _ready_quiz(session, user_factory)
        state = _state()
        await qz.cb_edit_opts(
            _FakeCallback(QuizEditCb(action="options", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="options", quiz_id=quiz.id, index=0),
        )

        message = _FakeMessage("Una sola")
        await qz.fsm_edit_options(message, state)

        assert message.said
        assert quiz_service.question_options(await _question(session, quiz.id)) == [
            "Roma",
            "Milano",
            "Napoli",
        ]

    async def test_a_correct_index_past_the_new_options_is_refused(self, session, user_factory):
        quiz = await _ready_quiz(session, user_factory)
        state = _state()
        await qz.cb_edit_opts(
            _FakeCallback(QuizEditCb(action="options", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="options", quiz_id=quiz.id, index=0),
        )
        await qz.fsm_edit_options(_FakeMessage("Parigi\nLione"), state)

        cb = _FakeCallback(QuizEditCb(action="correct", index=5).pack())
        await qz.cb_edit_correct(cb, state, session, QuizEditCb(action="correct", index=5))

        assert cb.alerts and "non valida" in cb.alerts[0]
        assert quiz_service.question_options(await _question(session, quiz.id)) == [
            "Roma",
            "Milano",
            "Napoli",
        ]

    async def test_starting_an_edit_on_a_quiz_that_is_no_longer_ready_is_refused(
        self, session, user_factory
    ):
        """The editor screen can sit in the chat while somebody launches the quiz from
        the hub, so each entry point re-checks instead of trusting the screen."""
        quiz = await _ready_quiz(session, user_factory)
        await quiz_service.set_status(session, quiz.id, "running")
        await session.commit()

        for handler, prefix in (
            (qz.cb_edit_text, "text"),
            (qz.cb_edit_expl, "explanation"),
            (qz.cb_edit_opts, "options"),
            (qz.cb_edit_redo, "redo"),
        ):
            payload = QuizEditCb(action=prefix, quiz_id=quiz.id, index=0)
            cb = _FakeCallback(payload.pack())
            await handler(cb, _state(), session, payload)
            assert cb.alerts and "non più modificabile" in cb.alerts[0], prefix

    async def test_an_edit_that_lands_after_the_launch_reports_the_failure(
        self, session, user_factory
    ):
        """Started while `ready`, submitted after somebody launched the quiz.
        `update_question` refuses it, and the admin has to be told — otherwise the
        editor re-renders as if the change had been saved."""
        quiz = await _ready_quiz(session, user_factory)
        state = _state()
        await qz.cb_edit_text(
            _FakeCallback(QuizEditCb(action="text", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="text", quiz_id=quiz.id, index=0),
        )

        await quiz_service.set_status(session, quiz.id, "running")
        await session.commit()

        message = _FakeMessage("Testo nuovo")
        await qz.fsm_edit_text(message, state, session)

        assert "non applicata" in message.said
        assert (await _question(session, quiz.id)).text == "Domanda 0?"


class TestRedo:
    async def test_the_redo_path_replaces_every_field_at_once(self, session, user_factory):
        """Same handlers as the single-field edits, told apart by `edit_redo`. Nothing
        may be written until the last step, so an admin who abandons halfway leaves
        the question untouched."""
        quiz = await _ready_quiz(session, user_factory)
        state = _state()

        await qz.cb_edit_redo(
            _FakeCallback(QuizEditCb(action="redo", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="redo", quiz_id=quiz.id, index=0),
        )
        await qz.fsm_edit_text(_FakeMessage("Capitale di Francia?"), state, session)

        # Nothing saved yet: the redo is still collecting.
        assert (await _question(session, quiz.id)).text == "Domanda 0?"

        await qz.fsm_edit_options(_FakeMessage("Parigi\nLione"), state)
        await qz.cb_edit_correct(
            _FakeCallback(QuizEditCb(action="correct", index=0).pack()),
            state,
            session,
            QuizEditCb(action="correct", index=0),
        )
        await qz.fsm_edit_explanation(_FakeMessage("È Parigi"), state, session)

        q = await _question(session, quiz.id)
        assert q.text == "Capitale di Francia?"
        assert quiz_service.question_options(q) == ["Parigi", "Lione"]
        assert q.correct_option_id == 0
        assert q.explanation == "È Parigi"
        assert await state.get_state() is None

    async def test_a_redo_can_skip_the_explanation(self, session, user_factory):
        quiz = await _ready_quiz(session, user_factory)
        state = _state()

        await qz.cb_edit_redo(
            _FakeCallback(QuizEditCb(action="redo", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="redo", quiz_id=quiz.id, index=0),
        )
        await qz.fsm_edit_text(_FakeMessage("Capitale di Francia?"), state, session)
        await qz.fsm_edit_options(_FakeMessage("Parigi\nLione"), state)
        await qz.cb_edit_correct(
            _FakeCallback(QuizEditCb(action="correct", index=0).pack()),
            state,
            session,
            QuizEditCb(action="correct", index=0),
        )
        await qz.cb_redo_skip_expl(
            _FakeCallback(QuizEditCb(action="redo_skip_explanation").pack()),
            state,
            session,
            QuizEditCb(action="redo_skip_explanation"),
        )

        q = await _question(session, quiz.id)
        assert q.text == "Capitale di Francia?"
        assert q.explanation is None

    async def test_abandoning_a_redo_leaves_the_question_alone(self, session, user_factory):
        quiz = await _ready_quiz(session, user_factory)
        state = _state()

        await qz.cb_edit_redo(
            _FakeCallback(QuizEditCb(action="redo", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="redo", quiz_id=quiz.id, index=0),
        )
        await qz.fsm_edit_text(_FakeMessage("Mai salvata"), state, session)
        await qz.cb_edit_cancel(
            _FakeCallback(QuizEditCb(action="cancel").pack()),
            state,
            session,
            QuizEditCb(action="cancel"),
        )

        q = await _question(session, quiz.id)
        assert q.text == "Domanda 0?"
        assert await state.get_state() is None


class TestCancel:
    async def test_cancelling_returns_to_the_question_being_edited(self, session, user_factory):
        quiz = await _ready_quiz(session, user_factory, n_questions=3)
        state = _state()
        await qz.cb_edit_text(
            _FakeCallback(QuizEditCb(action="text", quiz_id=quiz.id, index=2).pack()),
            state,
            session,
            QuizEditCb(action="text", quiz_id=quiz.id, index=2),
        )

        cb = _FakeCallback(QuizEditCb(action="cancel").pack())
        await qz.cb_edit_cancel(cb, state, session, QuizEditCb(action="cancel"))

        assert "Domanda 3/3" in cb.message.said, "cancelling jumped to another question"
        assert await state.get_state() is None

    async def test_cancelling_with_nothing_in_progress_is_silent(self, session):
        cb = _FakeCallback(QuizEditCb(action="cancel").pack())

        await qz.cb_edit_cancel(cb, _state(), session, QuizEditCb(action="cancel"))

        assert cb.message.texts == []

    async def test_navigating_away_abandons_a_half_done_edit(self, session, user_factory):
        """Otherwise the next message typed anywhere would be swallowed by the
        still-armed edit step and applied to a question the admin has left behind."""
        quiz = await _ready_quiz(session, user_factory)
        state = _state()
        await qz.cb_edit_text(
            _FakeCallback(QuizEditCb(action="text", quiz_id=quiz.id, index=0).pack()),
            state,
            session,
            QuizEditCb(action="text", quiz_id=quiz.id, index=0),
        )
        assert await state.get_state() == qz.QuizEditStates.editing_text

        await qz.cb_edit_nav(
            _FakeCallback(QuizEditCb(action="nav", quiz_id=quiz.id, index=1).pack()),
            state,
            session,
            QuizEditCb(action="nav", quiz_id=quiz.id, index=1),
        )

        assert await state.get_state() is None
