"""Editing the questions of a quiz that is `ready` but not yet launched.

Four fields can be changed one at a time, plus a «rifai» path that walks all of them.
Both share these handlers and are told apart by the `edit_redo` flag in the FSM: the
single-field branch saves immediately, the redo branch collects everything and writes
once at the end.

Only a `ready` quiz is editable, and that is enforced in `quiz_service.update_question`
rather than here — recorded answers reference options by stored index, so editing a
running quiz would silently reassign what players already answered."""

from __future__ import annotations


from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers.event_types import edit_or_send
from services import quiz_service
from utils.text import esc

from handlers.quiz._shared import (
    _MAX_EXPLANATION,
    _MAX_OPTION,
    _MAX_OPTIONS,
    _MAX_QUESTION,
    _MIN_OPTIONS,
    _options_error,
    _too_long,
    router,
)


# ---------------------------------------------------------------------------
# Edit questions of a READY quiz (scroll + per-field edit)
# ---------------------------------------------------------------------------
#
# From a quiz's detail screen (events hub → «✏️ Modifica domande») an admin can
# scroll question by question and edit the text, the answers (options + which is
# correct) or the explanation — or redo the whole question with the same prompts
# used at creation. Editing is allowed ONLY while the quiz is `ready`: once it is
# running/finished the recorded answers reference the stored option order, so
# `quiz_service.update_question` guards on status too (defence beyond the UI).


class QuizEditStates(StatesGroup):
    editing_text = State()
    editing_options = State()
    editing_correct = State()
    editing_explanation = State()


def _edit_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Annulla", callback_data="quiz_edit:cancel")
    ]])


def _edit_back_kb(quiz_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Torna al quiz", callback_data=f"ev:item:quiz:{quiz_id}")
    ]])


def _edit_correct_kb(options: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        b.button(text=f"{i + 1}. {opt[:20]}", callback_data=f"quiz_edit:correct:{i}")
    b.button(text="❌ Annulla", callback_data="quiz_edit:cancel")
    b.adjust(1)
    return b.as_markup()


def _edit_skip_expl_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭️ Salta", callback_data="quiz_edit:redoskipexpl"),
        InlineKeyboardButton(text="❌ Annulla", callback_data="quiz_edit:cancel"),
    ]])


def _edit_view_kb(quiz_id: int, idx: int, total: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if idx > 0:
        b.button(text="⬅️", callback_data=f"quiz_edit:nav:{quiz_id}:{idx - 1}")
    b.button(text=f"{idx + 1}/{total}", callback_data="quiz_edit:noop")
    if idx < total - 1:
        b.button(text="➡️", callback_data=f"quiz_edit:nav:{quiz_id}:{idx + 1}")
    b.button(text="✏️ Testo", callback_data=f"quiz_edit:text:{quiz_id}:{idx}")
    b.button(text="✏️ Risposte", callback_data=f"quiz_edit:opts:{quiz_id}:{idx}")
    b.button(text="✏️ Spiegazione", callback_data=f"quiz_edit:expl:{quiz_id}:{idx}")
    b.button(text="🔄 Rifai domanda", callback_data=f"quiz_edit:redo:{quiz_id}:{idx}")
    b.button(text="⬅️ Torna al quiz", callback_data=f"ev:item:quiz:{quiz_id}")
    nav_count = 1 + (1 if idx > 0 else 0) + (1 if idx < total - 1 else 0)
    b.adjust(nav_count, 3, 1, 1)  # nav row · three edit buttons · redo · back
    return b.as_markup()


async def _render_question_edit(
    message: Message, db_session: AsyncSession, quiz_id: int, idx: int
) -> None:
    """Show the edit view for question #idx of a ready quiz, clamping idx into range.
    Falls back to a notice + back button when the quiz isn't editable."""
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None:
        await edit_or_send(message, "⚠️ Quiz non trovato (eliminato?).", _edit_back_kb(quiz_id))
        return
    if quiz.status != "ready":
        await edit_or_send(
            message,
            "⚠️ Le domande si possono modificare solo su un quiz <b>pronto</b> "
            "(non ancora avviato).",
            _edit_back_kb(quiz_id),
        )
        return
    if not quiz.questions:
        await edit_or_send(message, "⚠️ Questo quiz non ha domande.", _edit_back_kb(quiz_id))
        return

    idx = max(0, min(idx, len(quiz.questions) - 1))
    q = quiz.questions[idx]
    options = quiz_service.question_options(q)
    lines = [
        f"✏️ <b>Modifica domande — {esc(quiz.title)}</b>",
        f"\n<b>Domanda {idx + 1}/{len(quiz.questions)}</b>",
        f"\n{esc(q.text)}\n",
    ]
    for i, opt in enumerate(options):
        lines.append(f"{'✅' if i == q.correct_option_id else '▫️'} {esc(opt)}")
    lines.append(
        f"\n💡 <i>{esc(q.explanation)}</i>" if q.explanation else "\n💡 <i>Nessuna spiegazione</i>"
    )
    await edit_or_send(message, "\n".join(lines), _edit_view_kb(quiz_id, idx, len(quiz.questions)))


async def _edit_load(
    state: FSMContext, db_session: AsyncSession, quiz_id: int, idx: int
) -> "object | None":
    """Resolve question #idx of a ready quiz and stash the edit context in the FSM.
    Returns the question, or None if the quiz is no longer editable."""
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or quiz.status != "ready" or idx >= len(quiz.questions):
        return None
    q = quiz.questions[idx]
    await state.update_data(
        edit_quiz_id=quiz_id, edit_idx=idx, edit_question_id=q.id, edit_redo=False
    )
    return q


async def _finish_field_edit(
    message: Message, state: FSMContext, db_session: AsyncSession, ok: bool
) -> None:
    """Common tail after saving an edit: clear the FSM and re-show the question."""
    data = await state.get_data()
    quiz_id = data.get("edit_quiz_id")
    idx = data.get("edit_idx", 0)
    await state.clear()
    if not ok:
        await message.answer("⚠️ Modifica non applicata (quiz non più modificabile).")
    if quiz_id is not None:
        await _render_question_edit(message, db_session, quiz_id, idx)


@router.callback_query(F.data == "quiz_edit:noop", IsAdminCallbackFilter())
async def cb_edit_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "quiz_edit:cancel", IsAdminCallbackFilter())
async def cb_edit_cancel(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    data = await state.get_data()
    quiz_id = data.get("edit_quiz_id")
    idx = data.get("edit_idx", 0)
    await state.clear()
    if quiz_id is None:
        await callback.answer()
        return
    await _render_question_edit(callback.message, db_session, quiz_id, idx)
    await callback.answer()


@router.callback_query(F.data.startswith("quiz_edit:nav:"), IsAdminCallbackFilter())
async def cb_edit_nav(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await state.clear()  # entering/scrolling the editor abandons any half-done field edit
    _, _, raw_quiz, raw_idx = callback.data.split(":")
    if not (raw_quiz.isdigit() and raw_idx.isdigit()):
        await callback.answer()
        return
    await _render_question_edit(callback.message, db_session, int(raw_quiz), int(raw_idx))
    await callback.answer()


# --- Single-field edits ----------------------------------------------------

@router.callback_query(F.data.startswith("quiz_edit:text:"), IsAdminCallbackFilter())
async def cb_edit_text(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    _, _, raw_quiz, raw_idx = callback.data.split(":")
    q = await _edit_load(state, db_session, int(raw_quiz), int(raw_idx))
    if q is None:
        await callback.answer("Quiz non più modificabile.", show_alert=True)
        return
    await state.set_state(QuizEditStates.editing_text)
    await callback.message.answer(
        f"✏️ Invia il <b>nuovo testo</b> della domanda (max {_MAX_QUESTION} caratteri):\n\n"
        f"<i>Attuale: {esc(q.text)}</i>",
        reply_markup=_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(QuizEditStates.editing_text, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_edit_text(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("⚠️ La domanda deve avere almeno 3 caratteri.", reply_markup=_edit_cancel_kb())
        return
    if err := _too_long(text, _MAX_QUESTION, "La domanda è troppo lunga"):
        await message.answer(err, reply_markup=_edit_cancel_kb())
        return
    data = await state.get_data()
    if data.get("edit_redo"):  # first step of a full-question redo → collect and continue
        await state.update_data(edit_text=text)
        await _prompt_edit_options(message, state)
        return
    ok = await quiz_service.update_question(db_session, data["edit_question_id"], text=text)
    await db_session.commit()
    await _finish_field_edit(message, state, db_session, ok)


@router.callback_query(F.data.startswith("quiz_edit:expl:"), IsAdminCallbackFilter())
async def cb_edit_expl(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    _, _, raw_quiz, raw_idx = callback.data.split(":")
    q = await _edit_load(state, db_session, int(raw_quiz), int(raw_idx))
    if q is None:
        await callback.answer("Quiz non più modificabile.", show_alert=True)
        return
    await state.set_state(QuizEditStates.editing_explanation)
    current = f"\n\n<i>Attuale: {esc(q.explanation)}</i>" if q.explanation else ""
    await callback.message.answer(
        f"💡 Invia la <b>nuova spiegazione</b> (max {_MAX_EXPLANATION} caratteri), "
        "«-» per rimuoverla:" + current,
        reply_markup=_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(QuizEditStates.editing_explanation, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_edit_explanation(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    raw = (message.text or "").strip()
    if err := _too_long(raw, _MAX_EXPLANATION, "La spiegazione è troppo lunga"):
        await message.answer(err, reply_markup=_edit_cancel_kb())
        return
    explanation = None if raw == "-" else raw
    data = await state.get_data()
    if data.get("edit_redo"):  # final step of a redo → save everything at once
        await _save_redo(message, state, db_session, explanation)
        return
    ok = await quiz_service.update_question(
        db_session, data["edit_question_id"], explanation=explanation
    )
    await db_session.commit()
    await _finish_field_edit(message, state, db_session, ok)


# --- Answers edit (options + which is correct) ------------------------------

async def _prompt_edit_options(message: Message, state: FSMContext, current: list[str] | None = None) -> None:
    await state.set_state(QuizEditStates.editing_options)
    hint = f"\n\n<i>Attuali: {esc(', '.join(current))}</i>" if current else ""
    await message.answer(
        f"✏️ Invia le <b>opzioni</b>, una per riga (min {_MIN_OPTIONS}, max {_MAX_OPTIONS}, "
        f"max {_MAX_OPTION} caratteri ciascuna):" + hint,
        reply_markup=_edit_cancel_kb(),
    )


@router.callback_query(F.data.startswith("quiz_edit:opts:"), IsAdminCallbackFilter())
async def cb_edit_opts(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    _, _, raw_quiz, raw_idx = callback.data.split(":")
    q = await _edit_load(state, db_session, int(raw_quiz), int(raw_idx))
    if q is None:
        await callback.answer("Quiz non più modificabile.", show_alert=True)
        return
    await _prompt_edit_options(callback.message, state, quiz_service.question_options(q))
    await callback.answer()


@router.message(QuizEditStates.editing_options, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_edit_options(message: Message, state: FSMContext) -> None:
    options = [o.strip() for o in (message.text or "").splitlines() if o.strip()]
    if err := _options_error(options):
        await message.answer(err, reply_markup=_edit_cancel_kb())
        return
    await state.update_data(edit_options=options)
    await state.set_state(QuizEditStates.editing_correct)
    await message.answer("Quale opzione è quella <b>corretta</b>?", reply_markup=_edit_correct_kb(options))


@router.callback_query(
    QuizEditStates.editing_correct, F.data.startswith("quiz_edit:correct:"), IsAdminCallbackFilter()
)
async def cb_edit_correct(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    idx = int(callback.data.split(":")[2])
    data = await state.get_data()
    options = data.get("edit_options", [])
    if idx >= len(options):
        await callback.answer("Opzione non valida.", show_alert=True)
        return
    if data.get("edit_redo"):  # redo → collect the correct answer, then ask the explanation
        await state.update_data(edit_correct=idx)
        await state.set_state(QuizEditStates.editing_explanation)
        await callback.message.answer(
            "💡 Invia una <b>spiegazione</b> (max 200 caratteri) oppure salta:",
            reply_markup=_edit_skip_expl_kb(),
        )
        await callback.answer()
        return
    ok = await quiz_service.update_question(
        db_session, data["edit_question_id"], options=options, correct_option_id=idx
    )
    await db_session.commit()
    await _finish_field_edit(callback.message, state, db_session, ok)
    await callback.answer()


# --- Redo the whole question ------------------------------------------------

@router.callback_query(F.data.startswith("quiz_edit:redo:"), IsAdminCallbackFilter())
async def cb_edit_redo(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    _, _, raw_quiz, raw_idx = callback.data.split(":")
    q = await _edit_load(state, db_session, int(raw_quiz), int(raw_idx))
    if q is None:
        await callback.answer("Quiz non più modificabile.", show_alert=True)
        return
    await state.update_data(edit_redo=True)
    await state.set_state(QuizEditStates.editing_text)
    await callback.message.answer(
        "🔄 <b>Rifai la domanda</b>\n\nInvia il <b>testo</b> della domanda (max 300 caratteri):",
        reply_markup=_edit_cancel_kb(),
    )
    await callback.answer()


@router.callback_query(
    QuizEditStates.editing_explanation, F.data == "quiz_edit:redoskipexpl", IsAdminCallbackFilter()
)
async def cb_redo_skip_expl(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await _save_redo(callback.message, state, db_session, None)
    await callback.answer()


async def _save_redo(
    message: Message, state: FSMContext, db_session: AsyncSession, explanation: str | None
) -> None:
    data = await state.get_data()
    ok = await quiz_service.update_question(
        db_session,
        data["edit_question_id"],
        text=data.get("edit_text"),
        options=data.get("edit_options"),
        correct_option_id=data.get("edit_correct"),
        explanation=explanation,
    )
    await db_session.commit()
    await _finish_field_edit(message, state, db_session, ok)

