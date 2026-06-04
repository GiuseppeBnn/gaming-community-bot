"""
Quiz mode — private, answer-driven quizzes with a podium.

Creation: an admin builds a quiz in private via an FSM (title → description →
prize → loop of questions; no time limit).

Play: when launched, the bot posts an announcement in the group with a "Gioca"
button (deep-link). Each participant plays in their OWN private chat — questions
are sent one at a time as inline option buttons, advancing immediately on answer
and showing the explanation. There is no per-question timer.

Podium: finishers (answered every question) are ranked by correct answers DESC,
then by finish time ASC (arrival order). The admin closes the quiz with
/chiudi_quiz; the top 3 split the prize pool. Answers are private, so nobody can
"answer for everyone".
"""

from __future__ import annotations

import logging
from functools import partial

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters.command import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import User
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from services import quiz_service

log = logging.getLogger(__name__)
router = Router()

_MIN_OPTIONS, _MAX_OPTIONS = 2, 10


class QuizCreationStates(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_prize_mode = State()
    waiting_prize_first = State()
    waiting_prize_second = State()
    waiting_prize_third = State()
    waiting_prize_consolation = State()
    waiting_question_text = State()
    waiting_question_options = State()
    waiting_correct = State()
    waiting_explanation = State()
    reviewing = State()


# Per-rank prize steps: state → (data key, label, settings default attr).
_PRIZE_STEPS: list[tuple[State, str, str, str]] = [
    (QuizCreationStates.waiting_prize_first, "prize_first", "🥇 1° classificato", "quiz_default_first"),
    (QuizCreationStates.waiting_prize_second, "prize_second", "🥈 2° classificato", "quiz_default_second"),
    (QuizCreationStates.waiting_prize_third, "prize_third", "🥉 3° classificato", "quiz_default_third"),
    (QuizCreationStates.waiting_prize_consolation, "prize_consolation",
     "🎖️ 4° classificato (premio di consolazione, poi a scendere)", "quiz_default_consolation"),
]
_PRIZE_BY_STATE = {st.state: (key, label, attr) for st, key, label, attr in _PRIZE_STEPS}


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Annulla", callback_data="quiz_new:cancel")
    ]])


def _back_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Indietro", callback_data="quiz_new:back"),
        InlineKeyboardButton(text="❌ Annulla", callback_data="quiz_new:cancel"),
    ]])


def _back_to_review_kb() -> InlineKeyboardMarkup:
    """Shown on the question-text step when adding a further question: lets the
    admin abandon the new question and return to the review screen."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Riepilogo", callback_data="quiz_new:review"),
        InlineKeyboardButton(text="❌ Annulla", callback_data="quiz_new:cancel"),
    ]])


def _prize_mode_kb() -> InlineKeyboardMarkup:
    d1, d2, d3, dc = (
        settings.quiz_default_first, settings.quiz_default_second,
        settings.quiz_default_third, settings.quiz_default_consolation,
    )
    b = InlineKeyboardBuilder()
    b.button(text=f"⚡ Premi consigliati (🥇{d1} 🥈{d2} 🥉{d3} 🎖️{dc})", callback_data="quiz_new:quickprize")
    b.button(text="✏️ Personalizza i premi", callback_data="quiz_new:customprize")
    b.button(text="🚫 Nessun premio", callback_data="quiz_new:noprize")
    b.button(text="⬅️ Indietro", callback_data="quiz_new:back")
    b.button(text="❌ Annulla", callback_data="quiz_new:cancel")
    b.adjust(1)
    return b.as_markup()


def _prize_step_kb(default_value: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Usa {default_value} 🪙", callback_data="quiz_new:usedefault")
    b.button(text="⬅️ Indietro", callback_data="quiz_new:back")
    b.button(text="❌ Annulla", callback_data="quiz_new:cancel")
    b.adjust(1, 2)
    return b.as_markup()


def _correct_kb(options: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        b.button(text=f"{i + 1}. {opt[:20]}", callback_data=f"quiz_new:correct:{i}")
    b.button(text="⬅️ Indietro", callback_data="quiz_new:back")
    b.button(text="❌ Annulla", callback_data="quiz_new:cancel")
    b.adjust(1)
    return b.as_markup()


def _explanation_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭️ Salta", callback_data="quiz_new:skipexpl"),
        InlineKeyboardButton(text="⬅️ Indietro", callback_data="quiz_new:back"),
        InlineKeyboardButton(text="❌ Annulla", callback_data="quiz_new:cancel"),
    ]])


def _review_kb(question_count: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Aggiungi domanda", callback_data="quiz_new:add")
    if question_count > 0:
        b.button(text="🗑 Rimuovi ultima", callback_data="quiz_new:removelast")
    b.button(text="✅ Pubblica", callback_data="quiz_new:publish")
    b.button(text="❌ Annulla", callback_data="quiz_new:cancel")
    b.adjust(1, 2)
    return b.as_markup()


def _question_kb(quiz_id: int, question_id: int, options: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        b.button(text=opt[:40], callback_data=f"quiz_ans:{quiz_id}:{question_id}:{i}")
    b.adjust(1)
    return b.as_markup()


# ---------------------------------------------------------------------------
# Creation FSM
# ---------------------------------------------------------------------------

async def start_quiz_creation(
    message: Message, state: FSMContext, creator_id: int | None = None
) -> None:
    """Deep-link create_quiz, /crea_quiz, and dashboard «Crea quiz» entry.

    `creator_id` lets callers that start the flow from a callback (where
    message.from_user is the bot) pass the real admin id.
    """
    await state.clear()
    # Remember who is creating: later steps may run from callbacks (where
    # message.from_user would be the bot), so we can't rely on from_user there.
    await state.update_data(creator_id=creator_id or message.from_user.id)
    await _prompt_title(message, state)


@router.message(Command("crea_quiz"), IsAdminFilter())
async def cmd_crea_quiz(message: Message, state: FSMContext) -> None:
    if message.chat.type != ChatType.PRIVATE:
        bot_info = await message.bot.get_me()
        await message.reply(
            "🧠 Crea il quiz in chat privata:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="➡️ Crea quiz", url=f"https://t.me/{bot_info.username}?start=create_quiz"
                )
            ]]),
        )
        return
    await start_quiz_creation(message, state)


@router.callback_query(F.data == "quiz_new:cancel", IsAdminCallbackFilter())
async def cb_quiz_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("❌ Creazione quiz annullata.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Step prompts (shared by the forward flow and the «⬅️ Indietro» button)
# ---------------------------------------------------------------------------

async def _prompt_title(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_title)
    current = (await state.get_data()).get("title")
    hint = f"\n<i>Attuale: {current}</i>" if current else ""
    await message.answer(
        "🧠 <b>Crea un nuovo quiz</b>\n\n"
        "<b>Step 1/3</b> — Invia il <b>titolo</b> del quiz (max 256 caratteri):" + hint,
        reply_markup=_cancel_kb(),
    )


async def _prompt_description(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_description)
    current = (await state.get_data()).get("description")
    hint = f"\n<i>Attuale: {current}</i>" if current else ""
    await message.answer(
        "<b>Step 2/3</b> — Invia una breve <b>descrizione</b> (o «-» per saltare):" + hint,
        reply_markup=_back_cancel_kb(),
    )


async def _prompt_prize_mode(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_prize_mode)
    await message.answer(
        "<b>Step 3/3 — Premi</b> 💰\n\n"
        "Come vuoi assegnare i premi in Aldueuri?\n"
        "• <b>Consigliati</b> — usa i valori di default\n"
        "• <b>Personalizza</b> — scegli 1°/2°/3° e la consolazione (a scendere, con minimo "
        "garantito per <i>tutti</i> i finisher)\n"
        "• <b>Nessun premio</b>",
        reply_markup=_prize_mode_kb(),
    )


async def _prompt_prize_step(message: Message, state: FSMContext, step_state: State) -> None:
    key, label, attr = _PRIZE_BY_STATE[step_state.state]
    default_value = getattr(settings, attr)
    await state.set_state(step_state)
    current = (await state.get_data()).get(key)
    current_hint = f"\n<i>Valore attuale: {current} 🪙</i>" if current is not None else ""
    extra = ""
    if step_state == QuizCreationStates.waiting_prize_consolation:
        extra = (
            "\n\n<i>Da qui in giù la consolazione scende in modo uniforme fino a un minimo "
            "garantito per l'ultimo: tutti i finisher prendono qualcosa. 0 = nessuna consolazione.</i>"
        )
    await message.answer(
        f"💰 Premio per <b>{label}</b>\n"
        f"Invia un numero (≥ 0) oppure usa il default.{extra}{current_hint}",
        reply_markup=_prize_step_kb(default_value),
    )


async def _prompt_question_text(message: Message, state: FSMContext, first: bool = False) -> None:
    await state.set_state(QuizCreationStates.waiting_question_text)
    data = await state.get_data()
    n = data.get("saved_count", 0) + 1
    intro = "🧠 <b>Quiz creato!</b>\n\n" if first else ""
    kb = _back_to_review_kb() if data.get("saved_count", 0) > 0 else _cancel_kb()
    await message.answer(
        f"{intro}<b>Domanda {n}</b> — Invia il <b>testo della domanda</b> (max 300 caratteri):",
        reply_markup=kb,
    )


async def _prompt_question_options(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_question_options)
    current = (await state.get_data()).get("q_options")
    hint = f"\n<i>Attuali: {', '.join(current)}</i>" if current else ""
    await message.answer(
        f"Invia le <b>opzioni</b>, una per riga (min {_MIN_OPTIONS}, max {_MAX_OPTIONS}):\n\n"
        "<i>Esempio:\n<code>Roma\nMilano\nNapoli</code></i>" + hint,
        reply_markup=_back_cancel_kb(),
    )


async def _prompt_correct(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_correct)
    options = (await state.get_data()).get("q_options", [])
    await message.answer("Quale opzione è quella <b>corretta</b>?", reply_markup=_correct_kb(options))


async def _prompt_explanation(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_explanation)
    await message.answer(
        "💡 Invia una <b>spiegazione</b> (mostrata dopo la risposta, max 200 caratteri) oppure salta:",
        reply_markup=_explanation_kb(),
    )


async def _prompt_review(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    await state.set_state(QuizCreationStates.reviewing)
    data = await state.get_data()
    quiz = await quiz_service.get_quiz(db_session, data["quiz_id"])
    lines = ["🧠 <b>Riepilogo quiz</b>\n", f"📌 <b>{quiz.title}</b>"]
    if quiz.description:
        lines.append(f"<i>{quiz.description}</i>")
    lines.append(f"💰 Premi: {quiz_service.format_prize_summary(quiz)}")
    lines.append(f"\n❓ <b>Domande ({len(quiz.questions)}):</b>")
    for i, q in enumerate(quiz.questions, 1):
        lines.append(f"{i}. {q.text[:50]}")
    lines.append("\nAggiungi altre domande, rimuovi l'ultima, oppure pubblica. ✅")
    await message.answer("\n".join(lines), reply_markup=_review_kb(len(quiz.questions)))


# ---------------------------------------------------------------------------
# Back navigation
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "quiz_new:back", IsAdminCallbackFilter())
async def cb_back(callback: CallbackQuery, state: FSMContext) -> None:
    prompter = _BACK_PROMPTERS.get(await state.get_state())
    if prompter is not None:
        await prompter(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "quiz_new:review", IsAdminCallbackFilter())
async def cb_review(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await _prompt_review(callback.message, state, db_session)
    await callback.answer()


# ---------------------------------------------------------------------------
# Title / description
# ---------------------------------------------------------------------------

@router.message(QuizCreationStates.waiting_title, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:256]
    if len(title) < 3:
        await message.answer("⚠️ Il titolo deve avere almeno 3 caratteri.", reply_markup=_cancel_kb())
        return
    await state.update_data(title=title)
    await _prompt_description(message, state)


@router.message(QuizCreationStates.waiting_description, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_description(message: Message, state: FSMContext) -> None:
    desc = (message.text or "").strip()[:1024]
    if desc == "-":
        desc = ""
    await state.update_data(description=desc)
    await _prompt_prize_mode(message, state)


# ---------------------------------------------------------------------------
# Prizes
# ---------------------------------------------------------------------------

@router.callback_query(QuizCreationStates.waiting_prize_mode, F.data == "quiz_new:quickprize", IsAdminCallbackFilter())
async def cb_quick_prize(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await state.update_data(
        prize_first=settings.quiz_default_first,
        prize_second=settings.quiz_default_second,
        prize_third=settings.quiz_default_third,
        prize_consolation=settings.quiz_default_consolation,
    )
    await _finalize_prizes_and_create(callback.message, state, db_session)
    await callback.answer()


@router.callback_query(QuizCreationStates.waiting_prize_mode, F.data == "quiz_new:noprize", IsAdminCallbackFilter())
async def cb_no_prize(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await state.update_data(prize_first=0, prize_second=0, prize_third=0, prize_consolation=0)
    await _finalize_prizes_and_create(callback.message, state, db_session)
    await callback.answer()


@router.callback_query(QuizCreationStates.waiting_prize_mode, F.data == "quiz_new:customprize", IsAdminCallbackFilter())
async def cb_custom_prize(callback: CallbackQuery, state: FSMContext) -> None:
    await _prompt_prize_step(callback.message, state, QuizCreationStates.waiting_prize_first)
    await callback.answer()


@router.message(QuizCreationStates.waiting_prize_first, IsAdminFilter(), ~F.text.startswith("/"))
@router.message(QuizCreationStates.waiting_prize_second, IsAdminFilter(), ~F.text.startswith("/"))
@router.message(QuizCreationStates.waiting_prize_third, IsAdminFilter(), ~F.text.startswith("/"))
@router.message(QuizCreationStates.waiting_prize_consolation, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_prize_value(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    cur = await state.get_state()
    key, _label, attr = _PRIZE_BY_STATE[cur]
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("⚠️ Inserisci un numero (es. 500, oppure 0).",
                             reply_markup=_prize_step_kb(getattr(settings, attr)))
        return
    if value < 0:
        await message.answer("⚠️ Il premio non può essere negativo.",
                             reply_markup=_prize_step_kb(getattr(settings, attr)))
        return
    await _advance_prize(message, state, db_session, key, value)


@router.callback_query(F.data == "quiz_new:usedefault", IsAdminCallbackFilter())
async def cb_use_default(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    meta = _PRIZE_BY_STATE.get(await state.get_state())
    if meta is None:
        await callback.answer()
        return
    key, _label, attr = meta
    await _advance_prize(callback.message, state, db_session, key, getattr(settings, attr))
    await callback.answer()


async def _advance_prize(
    message: Message, state: FSMContext, db_session: AsyncSession, key: str, value: int
) -> None:
    await state.update_data(**{key: value})
    states = [s.state for s, *_ in _PRIZE_STEPS]
    idx = states.index(await state.get_state())
    if idx + 1 < len(states):
        await _prompt_prize_step(message, state, _PRIZE_STEPS[idx + 1][0])
    else:
        await _finalize_prizes_and_create(message, state, db_session)


async def _finalize_prizes_and_create(
    message: Message, state: FSMContext, db_session: AsyncSession
) -> None:
    data = await state.get_data()
    consolation = data.get("prize_consolation", 0)
    quiz = await quiz_service.create_quiz(
        db_session,
        data["creator_id"],
        data["title"],
        data.get("description", ""),
        prize_first=data.get("prize_first", 0),
        prize_second=data.get("prize_second", 0),
        prize_third=data.get("prize_third", 0),
        prize_consolation=consolation,
        prize_min=quiz_service.participation_floor(consolation),
    )
    await db_session.commit()
    await state.update_data(quiz_id=quiz.id, saved_count=0)
    await _prompt_question_text(message, state, first=True)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

@router.message(QuizCreationStates.waiting_question_text, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_question_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()[:300]
    if len(text) < 3:
        await message.answer("⚠️ La domanda deve avere almeno 3 caratteri.", reply_markup=_cancel_kb())
        return
    await state.update_data(q_text=text)
    await _prompt_question_options(message, state)


@router.message(QuizCreationStates.waiting_question_options, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_question_options(message: Message, state: FSMContext) -> None:
    options = [o.strip() for o in (message.text or "").splitlines() if o.strip()]
    if not (_MIN_OPTIONS <= len(options) <= _MAX_OPTIONS):
        await message.answer(
            f"⚠️ Servono da {_MIN_OPTIONS} a {_MAX_OPTIONS} opzioni (una per riga).",
            reply_markup=_back_cancel_kb(),
        )
        return
    if any(len(o) > 100 for o in options):
        await message.answer("⚠️ Ogni opzione deve essere max 100 caratteri.", reply_markup=_back_cancel_kb())
        return
    await state.update_data(q_options=options)
    await _prompt_correct(message, state)


@router.callback_query(QuizCreationStates.waiting_correct, F.data.startswith("quiz_new:correct:"), IsAdminCallbackFilter())
async def cb_correct(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":")[2])
    data = await state.get_data()
    if idx >= len(data.get("q_options", [])):
        await callback.answer("Opzione non valida.", show_alert=True)
        return
    await state.update_data(q_correct=idx)
    await _prompt_explanation(callback.message, state)
    await callback.answer()


@router.message(QuizCreationStates.waiting_explanation, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_explanation(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    explanation = (message.text or "").strip()[:200]
    await _save_question(message, state, db_session, explanation)


@router.callback_query(QuizCreationStates.waiting_explanation, F.data == "quiz_new:skipexpl", IsAdminCallbackFilter())
async def cb_skip_explanation(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await _save_question(callback.message, state, db_session, None)
    await callback.answer()


async def _save_question(
    message: Message, state: FSMContext, db_session: AsyncSession, explanation: str | None
) -> None:
    data = await state.get_data()
    await quiz_service.add_question(
        db_session,
        quiz_id=data["quiz_id"],
        text=data["q_text"],
        options=data["q_options"],
        correct_option_id=data["q_correct"],
        explanation=explanation,
    )
    await db_session.commit()
    await state.update_data(
        saved_count=data.get("saved_count", 0) + 1, q_text=None, q_options=None, q_correct=None
    )
    await _prompt_review(message, state, db_session)


# ---------------------------------------------------------------------------
# Review / publish
# ---------------------------------------------------------------------------

@router.callback_query(QuizCreationStates.reviewing, F.data == "quiz_new:add", IsAdminCallbackFilter())
async def cb_add_question(callback: CallbackQuery, state: FSMContext) -> None:
    await _prompt_question_text(callback.message, state)
    await callback.answer()


@router.callback_query(QuizCreationStates.reviewing, F.data == "quiz_new:removelast", IsAdminCallbackFilter())
async def cb_remove_last(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    data = await state.get_data()
    remaining = await quiz_service.delete_last_question(db_session, data["quiz_id"])
    await db_session.commit()
    await state.update_data(saved_count=remaining)
    if remaining == 0:
        await callback.answer("Nessuna domanda rimasta: aggiungine almeno una.")
        await _prompt_question_text(callback.message, state)
    else:
        await callback.answer("🗑 Ultima domanda rimossa.")
        await _prompt_review(callback.message, state, db_session)


@router.callback_query(QuizCreationStates.reviewing, F.data == "quiz_new:publish", IsAdminCallbackFilter())
async def cb_publish(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    data = await state.get_data()
    quiz_id = data.get("quiz_id")
    quiz = await quiz_service.get_quiz(db_session, quiz_id) if quiz_id else None
    if quiz is None or not quiz.questions:
        await callback.answer("⚠️ Aggiungi almeno una domanda prima di pubblicare.", show_alert=True)
        return
    await quiz_service.set_status(db_session, quiz_id, "ready")
    await db_session.commit()
    await state.clear()
    await callback.message.answer(
        f"🎉 <b>Quiz pronto!</b>\n\n"
        f"🧠 <b>#{quiz.id} {quiz.title}</b>\n"
        f"❓ Domande: <b>{len(quiz.questions)}</b>\n"
        f"💰 Premi: {quiz_service.format_prize_summary(quiz)}\n\n"
        f"Avvialo nel gruppo con <code>/avvia_quiz {quiz.id}</code> o <code>/quiz</code>, "
        f"oppure dalla dashboard <code>/admin</code>."
    )
    await callback.answer("✅ Quiz pronto!")


# Maps each step's state → the prompt to re-show when the user taps «⬅️ Indietro».
_BACK_PROMPTERS = {
    QuizCreationStates.waiting_description.state: _prompt_title,
    QuizCreationStates.waiting_prize_mode.state: _prompt_description,
    QuizCreationStates.waiting_prize_first.state: _prompt_prize_mode,
    QuizCreationStates.waiting_prize_second.state:
        partial(_prompt_prize_step, step_state=QuizCreationStates.waiting_prize_first),
    QuizCreationStates.waiting_prize_third.state:
        partial(_prompt_prize_step, step_state=QuizCreationStates.waiting_prize_second),
    QuizCreationStates.waiting_prize_consolation.state:
        partial(_prompt_prize_step, step_state=QuizCreationStates.waiting_prize_third),
    QuizCreationStates.waiting_question_options.state: _prompt_question_text,
    QuizCreationStates.waiting_correct.state: _prompt_question_options,
    QuizCreationStates.waiting_explanation.state: _prompt_correct,
}


# ---------------------------------------------------------------------------
# Launch (announce in group) + listing + close
# ---------------------------------------------------------------------------

async def open_quiz(bot, db_session: AsyncSession, quiz_id: int) -> tuple[bool, str]:
    """Set a quiz running and announce it in the group. Caller commits."""
    if settings.group_id == 0:
        return False, "GROUP_ID non configurato."
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or not quiz.questions:
        return False, "Quiz non trovato o senza domande."
    if quiz.status == "running":
        return False, "Questo quiz è già in corso."
    if quiz.status == "finished":
        return False, "Questo quiz è già stato giocato."

    # Announce FIRST: if the group send fails (bot not admin / not in group) we
    # leave the quiz as `ready` rather than marking it running with no announcement.
    try:
        bot_info = await bot.get_me()
        await bot.send_message(
            settings.group_id,
            f"🧠 <b>QUIZ: {quiz.title}</b>\n"
            f"❓ {len(quiz.questions)} domande · 🏆 {quiz_service.format_prize_summary(quiz)}\n\n"
            "Gioca in <b>chat privata</b> col bot! Vince chi ne azzecca di più — "
            "a parità conta l'ordine di arrivo. Premio garantito a tutti i finisher! 🏁",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="▶️ Gioca", url=f"https://t.me/{bot_info.username}?start=quiz_{quiz.id}"
                )
            ]]),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Annuncio quiz %s nel gruppo fallito: %s", quiz_id, e)
        return False, "Impossibile annunciare nel gruppo (il bot è nel gruppo?)."

    await quiz_service.set_status(db_session, quiz_id, "running")
    return True, "Quiz avviato nel gruppo!"


@router.message(Command("quiz"), IsAdminFilter())
async def cmd_quiz_list(message: Message, db_session: AsyncSession) -> None:
    quizzes = [q for q in await quiz_service.list_ready(db_session) if q.status == "ready"]
    if not quizzes:
        await message.reply("🧠 Nessun quiz pronto. Creane uno con /crea_quiz.")
        return
    b = InlineKeyboardBuilder()
    for q in quizzes:
        b.button(
            text=f"▶️ #{q.id} {q.title[:30]} ({len(q.questions)} dom.)",
            callback_data=f"quiz:open:{q.id}",
        )
    b.adjust(1)
    await message.reply("🧠 <b>Quiz pronti</b> — tocca per avviare:", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("quiz:open:"), IsAdminCallbackFilter())
async def cb_quiz_open(callback: CallbackQuery, db_session: AsyncSession) -> None:
    quiz_id = int(callback.data.split(":")[2])
    ok, msg = await open_quiz(callback.bot, db_session, quiz_id)
    if ok:
        await db_session.commit()
        await callback.message.edit_text(f"🧠 Quiz #{quiz_id} avviato! Annuncio inviato nel gruppo. 🎬")
    await callback.answer(msg, show_alert=not ok)


@router.message(Command("avvia_quiz"), IsAdminFilter())
async def cmd_avvia_quiz(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.reply("ℹ️ Uso: <code>/avvia_quiz &lt;id&gt;</code>")
        return
    ok, msg = await open_quiz(message.bot, db_session, int(raw))
    if ok:
        await db_session.commit()
    await message.reply(("🎬 " if ok else "⚠️ ") + msg)


async def close_quiz(bot, db_session: AsyncSession, quiz_id: int) -> tuple[bool, str]:
    """Close a running quiz: rank finishers, pay prizes, announce the podium.

    Returns (ok, message). The message is the podium text when there is no group
    to announce to, otherwise a short confirmation. Caller need not commit.
    """
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None:
        return False, "Quiz non trovato."
    if quiz.status == "finished":
        return False, "Questo quiz è già stato chiuso."
    if quiz.status != "running":
        return False, "Questo quiz non è in corso (avvialo prima)."

    ranked = await quiz_service.podium(db_session, quiz_id)
    awards = await quiz_service.award_prizes(db_session, quiz_id)
    await quiz_service.set_status(db_session, quiz_id, "finished")
    await db_session.commit()

    text = await _podium_text(db_session, quiz.title, ranked, awards)
    if settings.group_id != 0:
        try:
            await bot.send_message(settings.group_id, text)
        except Exception:  # noqa: BLE001
            log.warning("Impossibile annunciare il podio nel gruppo.")
        return True, "🏁 Quiz chiuso. Podio pubblicato nel gruppo."
    return True, text


@router.message(Command("chiudi_quiz"), IsAdminFilter())
async def cmd_chiudi_quiz(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.reply("ℹ️ Uso: <code>/chiudi_quiz &lt;id&gt;</code>")
        return
    ok, msg = await close_quiz(message.bot, db_session, int(raw))
    await message.reply(msg if ok else f"⚠️ {msg}")


async def _podium_text(db_session: AsyncSession, title: str, ranked, awards) -> str:
    if not ranked:
        return f"🏁 <b>{title}</b> — chiuso!\n\n<i>Nessun partecipante ha completato il quiz.</i>"
    award_by_user = {a.user_tg_id: a for a in awards}
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏁 <b>{title} — PODIO</b>\n"]
    for i, row in enumerate(ranked[:10]):
        rank = medals[i] if i < 3 else f"{i + 1}."
        name = await _display_name(db_session, row.user_tg_id)
        award = award_by_user.get(row.user_tg_id)
        prize_txt = ""
        if award and award.coins:
            icon = "🎖️" if award.kind == "consolation" else "🏆"
            prize_txt = f" — {icon} <b>+{award.coins} 🪙 Aldueuri</b>"
        lines.append(f"{rank} {name} — {row.correct} ✅{prize_txt}")
    return "\n".join(lines)


async def _display_name(db_session: AsyncSession, tg_id: int) -> str:
    user = (await db_session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if user is None:
        return f"<a href=\"tg://user?id={tg_id}\">giocatore</a>"
    if user.username:
        return f"@{user.username}"
    return f"<a href=\"tg://user?id={tg_id}\">{user.full_name}</a>"


# ---------------------------------------------------------------------------
# Private play
# ---------------------------------------------------------------------------

async def start_quiz_session(message: Message, db_session: AsyncSession, quiz_id: int) -> None:
    """Deep-link quiz_<id>: start (or resume) playing the quiz in private."""
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or not quiz.questions:
        await message.answer("⚠️ Quiz non trovato.")
        return
    if quiz.status == "finished":
        await message.answer("🏁 Questo quiz è già terminato.")
        return
    if quiz.status != "running":
        await message.answer("⏳ Questo quiz non è ancora iniziato. Aspetta che un admin lo avvii.")
        return

    total = len(quiz.questions)
    done = await quiz_service.answered_count(db_session, quiz_id, message.from_user.id)
    if done >= total:
        correct = await quiz_service.correct_count(db_session, quiz_id, message.from_user.id)
        await message.answer(
            f"✅ Hai già completato <b>{quiz.title}</b>: <b>{correct}/{total}</b> corrette.\n"
            "Aspetta la chiusura per vedere il podio! 🏁"
        )
        return

    await message.answer(
        f"🧠 <b>{quiz.title}</b>\n<i>Rispondi alle domande. Nessun limite di tempo, "
        "ma chi finisce prima sale sul podio a parità di risposte!</i>"
    )
    await _send_question(message, quiz, done)


async def _send_question(message: Message, quiz, index: int) -> None:
    question = quiz.questions[index]
    options = quiz_service.question_options(question)
    await message.answer(
        f"❓ <b>Domanda {index + 1}/{len(quiz.questions)}</b>\n\n{question.text}",
        reply_markup=_question_kb(quiz.id, question.id, options),
    )


@router.callback_query(F.data.startswith("quiz_ans:"))
async def cb_quiz_answer(callback: CallbackQuery, db_session: AsyncSession) -> None:
    try:
        _, raw_quiz, raw_q, raw_opt = callback.data.split(":")
        quiz_id, question_id, opt_idx = int(raw_quiz), int(raw_q), int(raw_opt)
    except (ValueError, IndexError):
        await callback.answer("Dati non validi.", show_alert=True)
        return

    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or quiz.status != "running":
        await callback.answer("⚠️ Questo quiz non è più disponibile.", show_alert=True)
        return

    question = next((q for q in quiz.questions if q.id == question_id), None)
    if question is None:
        await callback.answer("Domanda non valida.", show_alert=True)
        return
    options = quiz_service.question_options(question)
    if not (0 <= opt_idx < len(options)):
        await callback.answer("Opzione non valida.", show_alert=True)
        return

    # Enforce sequential play: only the user's current question is answerable.
    done_before = await quiz_service.answered_count(db_session, quiz_id, callback.from_user.id)
    if question.position != done_before:
        await callback.answer(
            "Hai già risposto a questa domanda."
            if question.position < done_before
            else "Rispondi prima alla domanda corrente."
        )
        return

    outcome = await quiz_service.record_answer(
        db_session, quiz_id, question_id, callback.from_user.id, opt_idx
    )
    if outcome is None:
        await callback.answer("Domanda non valida.", show_alert=True)
        return
    if not outcome.recorded:
        await callback.answer("Hai già risposto a questa domanda.")
        return
    await db_session.commit()

    # Feedback on the answered message (buttons removed).
    chosen = options[opt_idx]
    if outcome.is_correct:
        feedback = f"✅ <b>Esatto!</b> — {chosen}"
    else:
        correct_label = options[outcome.correct_option_id]
        feedback = f"❌ <b>Sbagliato.</b> Hai scelto: {chosen}\n✅ Giusta: <b>{correct_label}</b>"
    if question.explanation:
        feedback += f"\n\n💡 <i>{question.explanation}</i>"
    try:
        await callback.message.edit_text(
            f"❓ {question.text}\n\n{feedback}"
        )
    except Exception:  # noqa: BLE001 — editing may fail if message is too old
        await callback.message.answer(feedback)

    # Advance or finish.
    total = len(quiz.questions)
    done = await quiz_service.answered_count(db_session, quiz_id, callback.from_user.id)
    if done < total:
        await _send_question(callback.message, quiz, done)
    else:
        correct = await quiz_service.correct_count(db_session, quiz_id, callback.from_user.id)
        await callback.message.answer(
            f"🏁 <b>Quiz completato!</b>\n\n"
            f"Hai totalizzato <b>{correct}/{total}</b> risposte corrette.\n"
            "Aspetta la chiusura per scoprire il podio! 🏆"
        )
    await callback.answer()
