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

import asyncio
import logging
import time
from dataclasses import dataclass
from functools import partial

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters.command import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.connection import async_session_maker
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter, is_admin
from handlers._mentions import mention
from handlers._privacy import redirect_to_private
from handlers._trophy_announce import announce_trophies
from handlers.event_types import edit_or_send
from keyboards.common_kb import confirm_cancel_kb
from services import badge_service, group_registry, progress_service, quiz_service
from utils import cooldown
from utils.static_reply import reply_static
from utils.text import esc, format_seconds_short

log = logging.getLogger(__name__)
router = Router()

_MIN_OPTIONS, _MAX_OPTIONS = 2, 10

# Length caps for every free-text field of a quiz. Single source of truth: the
# prompts, the validation and the edit flow all read these, so the number an admin
# is told can never drift from the number actually enforced. Inputs over the cap
# are REJECTED with the real count, never silently truncated — a truncated
# question only shows up once the quiz is already live.
_MAX_TITLE = 256
_MAX_DESC = 1024
_MAX_QUESTION = 300
_MAX_OPTION = 100
_MAX_EXPLANATION = 200

# Quiz management (list/start/close) is admin-only AND private-only: in a group it
# redirects to the private dashboard. Creation already redirects via /crea_quiz.
_QUIZ_PRIVATE_NOTICE = "🧠 Gestisci i quiz in chat privata col bot."


def _too_long(text: str, cap: int, subject: str) -> str | None:
    """Error message if `text` exceeds `cap`, else None. `subject` is the already
    gender-agreed Italian phrase (e.g. "Il titolo è troppo lungo"). Reports the
    actual length and how much to cut, so the admin doesn't have to count."""
    if len(text) <= cap:
        return None
    return (
        f"⚠️ {subject}: <b>{len(text)}/{cap}</b> caratteri.\n"
        f"Accorcia di {len(text) - cap} e reinvia."
    )


def _options_error(options: list[str]) -> str | None:
    """Validate a parsed option list (count + per-option length). Shared by the
    creation and the edit flow so both enforce exactly the same rules."""
    if not (_MIN_OPTIONS <= len(options) <= _MAX_OPTIONS):
        return f"⚠️ Servono da {_MIN_OPTIONS} a {_MAX_OPTIONS} opzioni (una per riga)."
    long_ones = [(i, o) for i, o in enumerate(options, start=1) if len(o) > _MAX_OPTION]
    if long_ones:
        detail = "\n".join(f"• opzione {i}: {len(o)}/{_MAX_OPTION}" for i, o in long_ones)
        return (
            f"⚠️ Ogni opzione può essere al massimo <b>{_MAX_OPTION}</b> caratteri.\n{detail}\n"
            "Accorcia e reinvia tutte le opzioni."
        )
    return None


class QuizCreationStates(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_prize_mode = State()
    waiting_prize_first = State()
    waiting_prize_second = State()
    waiting_prize_third = State()
    waiting_prize_consolation = State()
    waiting_time_limit = State()
    waiting_randomize = State()
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


_TIME_LIMIT_PRESETS = (15, 30, 45, 60)


def _time_limit_kb() -> InlineKeyboardMarkup:
    default = settings.quiz_default_time_limit_seconds
    b = InlineKeyboardBuilder()
    for sec in _TIME_LIMIT_PRESETS:
        mark = "✅ " if sec == default else "⏱️ "
        b.button(text=f"{mark}{sec}s", callback_data=f"quiz_new:tl:{sec}")
    b.button(text="🚫 Nessun limite", callback_data="quiz_new:tl:0")
    b.button(text="✏️ Personalizza", callback_data="quiz_new:tlcustom")
    b.button(text="⬅️ Indietro", callback_data="quiz_new:back")
    b.button(text="❌ Annulla", callback_data="quiz_new:cancel")
    b.adjust(4, 1, 1, 2)
    return b.as_markup()


def _randomize_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔀 Domande", callback_data="quiz_new:rnd:q")
    b.button(text="🔀 Risposte", callback_data="quiz_new:rnd:a")
    b.button(text="🔀 Entrambe", callback_data="quiz_new:rnd:both")
    b.button(text="🚫 Nessuna", callback_data="quiz_new:rnd:none")
    b.button(text="⬅️ Indietro", callback_data="quiz_new:back")
    b.button(text="❌ Annulla", callback_data="quiz_new:cancel")
    b.adjust(2, 2, 2)
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


def _question_kb(
    quiz_id: int, question_id: int, ordered_options: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    """``ordered_options`` are ``(real_index, text)`` pairs, already in display order —
    the callback always carries ``real_index`` so answer recording is unaffected by
    display-order randomization (§19)."""
    b = InlineKeyboardBuilder()
    for real_idx, opt in ordered_options:
        b.button(text=opt[:40], callback_data=f"quiz_ans:{quiz_id}:{question_id}:{real_idx}")
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
    if not await cooldown.guard(
        message, "event_create", settings.event_create_cooldown_seconds, exempt_admin=False
    ):
        return
    await start_quiz_creation(message, state)


@router.callback_query(F.data == "quiz_new:cancel", IsAdminCallbackFilter())
async def cb_quiz_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    # Confirm before discarding — the in-progress prompt above stays intact, so
    # "No" simply lets the admin keep going.
    if await state.get_state() is None:
        await callback.answer()
        return
    await callback.message.answer(
        "⚠️ Sicuro di voler annullare la creazione del quiz? I dati inseriti andranno persi.",
        reply_markup=confirm_cancel_kb("quiz_new:cancel_yes", "quiz_new:cancel_no"),
    )
    await callback.answer()


@router.callback_query(F.data == "quiz_new:cancel_yes", IsAdminCallbackFilter())
async def cb_quiz_cancel_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Creazione quiz annullata.")
    await callback.answer()


@router.callback_query(F.data == "quiz_new:cancel_no", IsAdminCallbackFilter())
async def cb_quiz_cancel_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("▶️ Ok, continua pure da dove eri rimasto.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Step prompts (shared by the forward flow and the «⬅️ Indietro» button)
# ---------------------------------------------------------------------------

async def _prompt_title(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_title)
    current = (await state.get_data()).get("title")
    hint = f"\n<i>Attuale: {esc(current)}</i>" if current else ""
    await message.answer(
        "🧠 <b>Crea un nuovo quiz</b>\n\n"
        f"<b>Step 1/3</b> — Invia il <b>titolo</b> del quiz (max {_MAX_TITLE} caratteri):" + hint,
        reply_markup=_cancel_kb(),
    )


async def _prompt_description(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_description)
    current = (await state.get_data()).get("description")
    hint = f"\n<i>Attuale: {esc(current)}</i>" if current else ""
    await message.answer(
        f"<b>Step 2/3</b> — Invia una breve <b>descrizione</b>, max {_MAX_DESC} caratteri "
        "(o «-» per saltare):" + hint,
        reply_markup=_back_cancel_kb(),
    )


async def _prompt_prize_mode(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_prize_mode)
    await message.answer(
        "<b>Step 3/3 — Premi</b> 💰\n\n"
        "Come vuoi assegnare i premi in CoInn?\n"
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


async def _prompt_time_limit(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_time_limit)
    current = (await state.get_data()).get("time_limit")
    current_hint = (
        f"\n<i>Attuale: {current}s</i>" if current
        else ("\n<i>Attuale: nessun limite</i>" if current == 0 else "")
    )
    await message.answer(
        "⏱️ <b>Limite di tempo per domanda</b>\n\n"
        "Quanti secondi ha ogni giocatore per rispondere a <b>ciascuna</b> domanda?\n"
        "Allo scadere la domanda è data come <b>sbagliata</b> e si passa avanti.\n\n"
        "Scegli un valore, «Nessun limite», o «Personalizza» (5–300 secondi)." + current_hint,
        reply_markup=_time_limit_kb(),
    )


async def _prompt_randomize(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_randomize)
    await message.answer(
        "🔀 <b>Domande e risposte casuali</b>\n\n"
        "Vuoi che ogni giocatore veda le <b>domande</b> in un ordine diverso, le "
        "<b>risposte</b> di ogni domanda in un ordine diverso, entrambe, o nessuna delle due "
        "(ordine invariato per tutti)?",
        reply_markup=_randomize_kb(),
    )


async def _prompt_question_text(message: Message, state: FSMContext, first: bool = False) -> None:
    await state.set_state(QuizCreationStates.waiting_question_text)
    data = await state.get_data()
    n = data.get("saved_count", 0) + 1
    intro = "🧠 <b>Quiz creato!</b>\n\n" if first else ""
    kb = _back_to_review_kb() if data.get("saved_count", 0) > 0 else _cancel_kb()
    await message.answer(
        f"{intro}<b>Domanda {n}</b> — Invia il <b>testo della domanda</b> "
        f"(max {_MAX_QUESTION} caratteri):",
        reply_markup=kb,
    )


async def _prompt_question_options(message: Message, state: FSMContext) -> None:
    await state.set_state(QuizCreationStates.waiting_question_options)
    current = (await state.get_data()).get("q_options")
    hint = f"\n<i>Attuali: {esc(', '.join(current))}</i>" if current else ""
    await message.answer(
        f"Invia le <b>opzioni</b>, una per riga (min {_MIN_OPTIONS}, max {_MAX_OPTIONS}, "
        f"max {_MAX_OPTION} caratteri ciascuna):\n\n"
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
        f"💡 Invia una <b>spiegazione</b> (mostrata dopo la risposta, max {_MAX_EXPLANATION} "
        "caratteri) oppure salta:",
        reply_markup=_explanation_kb(),
    )


def _randomize_summary(randomize_questions: bool, randomize_answers: bool) -> str:
    if randomize_questions and randomize_answers:
        return "domande e risposte"
    if randomize_questions:
        return "solo domande"
    if randomize_answers:
        return "solo risposte"
    return "nessuna"


async def _prompt_review(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    await state.set_state(QuizCreationStates.reviewing)
    data = await state.get_data()
    quiz = await quiz_service.get_quiz(db_session, data["quiz_id"])
    lines = ["🧠 <b>Riepilogo quiz</b>\n", f"📌 <b>{esc(quiz.title)}</b>"]
    if quiz.description:
        lines.append(f"<i>{esc(quiz.description)}</i>")
    lines.append(f"💰 Premi: {quiz_service.format_prize_summary(quiz)}")
    tl = data.get("time_limit", 0)
    lines.append(f"⏱️ Tempo: {f'{tl}s per domanda' if tl else 'nessun limite'}")
    lines.append(f"🔀 Casualità: {_randomize_summary(quiz.randomize_questions, quiz.randomize_answers)}")
    lines.append(f"\n❓ <b>Domande ({len(quiz.questions)}):</b>")
    for i, q in enumerate(quiz.questions, 1):
        lines.append(f"{i}. {esc(q.text[:50])}")
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
    title = (message.text or "").strip()
    if len(title) < 3:
        await message.answer("⚠️ Il titolo deve avere almeno 3 caratteri.", reply_markup=_cancel_kb())
        return
    if err := _too_long(title, _MAX_TITLE, "Il titolo è troppo lungo"):
        await message.answer(err, reply_markup=_cancel_kb())
        return
    await state.update_data(title=title)
    await _prompt_description(message, state)


@router.message(QuizCreationStates.waiting_description, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_description(message: Message, state: FSMContext) -> None:
    desc = (message.text or "").strip()
    if err := _too_long(desc, _MAX_DESC, "La descrizione è troppo lunga"):
        await message.answer(err, reply_markup=_back_cancel_kb())
        return
    if desc == "-":
        desc = ""
    await state.update_data(description=desc)
    await _prompt_prize_mode(message, state)


# ---------------------------------------------------------------------------
# Prizes
# ---------------------------------------------------------------------------

@router.callback_query(QuizCreationStates.waiting_prize_mode, F.data == "quiz_new:quickprize", IsAdminCallbackFilter())
async def cb_quick_prize(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(
        prize_first=settings.quiz_default_first,
        prize_second=settings.quiz_default_second,
        prize_third=settings.quiz_default_third,
        prize_consolation=settings.quiz_default_consolation,
    )
    await _prompt_time_limit(callback.message, state)
    await callback.answer()


@router.callback_query(QuizCreationStates.waiting_prize_mode, F.data == "quiz_new:noprize", IsAdminCallbackFilter())
async def cb_no_prize(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(prize_first=0, prize_second=0, prize_third=0, prize_consolation=0)
    await _prompt_time_limit(callback.message, state)
    await callback.answer()


@router.callback_query(QuizCreationStates.waiting_prize_mode, F.data == "quiz_new:customprize", IsAdminCallbackFilter())
async def cb_custom_prize(callback: CallbackQuery, state: FSMContext) -> None:
    await _prompt_prize_step(callback.message, state, QuizCreationStates.waiting_prize_first)
    await callback.answer()


@router.message(QuizCreationStates.waiting_prize_first, IsAdminFilter(), ~F.text.startswith("/"))
@router.message(QuizCreationStates.waiting_prize_second, IsAdminFilter(), ~F.text.startswith("/"))
@router.message(QuizCreationStates.waiting_prize_third, IsAdminFilter(), ~F.text.startswith("/"))
@router.message(QuizCreationStates.waiting_prize_consolation, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_prize_value(message: Message, state: FSMContext) -> None:
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
    await _advance_prize(message, state, key, value)


@router.callback_query(F.data == "quiz_new:usedefault", IsAdminCallbackFilter())
async def cb_use_default(callback: CallbackQuery, state: FSMContext) -> None:
    meta = _PRIZE_BY_STATE.get(await state.get_state())
    if meta is None:
        await callback.answer()
        return
    key, _label, attr = meta
    await _advance_prize(callback.message, state, key, getattr(settings, attr))
    await callback.answer()


async def _advance_prize(message: Message, state: FSMContext, key: str, value: int) -> None:
    await state.update_data(**{key: value})
    states = [s.state for s, *_ in _PRIZE_STEPS]
    idx = states.index(await state.get_state())
    if idx + 1 < len(states):
        await _prompt_prize_step(message, state, _PRIZE_STEPS[idx + 1][0])
    else:
        # Prizes done → pick the per-question time limit, then create the quiz.
        await _prompt_time_limit(message, state)


# ---------------------------------------------------------------------------
# Time limit per question
# ---------------------------------------------------------------------------

@router.callback_query(
    QuizCreationStates.waiting_time_limit, F.data.startswith("quiz_new:tl:"), IsAdminCallbackFilter()
)
async def cb_time_limit(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    value = int(callback.data.split(":")[2])
    await state.update_data(time_limit=value)
    await _prompt_randomize(callback.message, state)
    await callback.answer()


@router.callback_query(
    QuizCreationStates.waiting_time_limit, F.data == "quiz_new:tlcustom", IsAdminCallbackFilter()
)
async def cb_time_limit_custom(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "⏱️ Invia il limite in <b>secondi</b> (da 5 a 300), oppure 0 per nessun limite:",
        reply_markup=_back_cancel_kb(),
    )
    await callback.answer()


@router.message(QuizCreationStates.waiting_time_limit, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_time_limit(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer(
            "⚠️ Inserisci un numero di secondi (es. 30), oppure 0 per nessun limite.",
            reply_markup=_time_limit_kb(),
        )
        return
    if value != 0 and not (5 <= value <= 300):
        await message.answer(
            "⚠️ Il limite deve essere 0 (nessuno) oppure tra 5 e 300 secondi.",
            reply_markup=_time_limit_kb(),
        )
        return
    await state.update_data(time_limit=value)
    await _prompt_randomize(message, state)


# ---------------------------------------------------------------------------
# Randomize questions/answers
# ---------------------------------------------------------------------------

_RANDOMIZE_CHOICES = {
    "q": (True, False),
    "a": (False, True),
    "both": (True, True),
    "none": (False, False),
}


@router.callback_query(
    QuizCreationStates.waiting_randomize, F.data.startswith("quiz_new:rnd:"), IsAdminCallbackFilter()
)
async def cb_randomize(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    choice = callback.data.split(":")[2]
    randomize_questions, randomize_answers = _RANDOMIZE_CHOICES[choice]
    await state.update_data(
        randomize_questions=randomize_questions, randomize_answers=randomize_answers
    )
    await _finalize_prizes_and_create(callback.message, state, db_session)
    await callback.answer()


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
        randomize_questions=data.get("randomize_questions", False),
        randomize_answers=data.get("randomize_answers", False),
    )
    await db_session.commit()
    await state.update_data(quiz_id=quiz.id, saved_count=0)
    await _prompt_question_text(message, state, first=True)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

@router.message(QuizCreationStates.waiting_question_text, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_question_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("⚠️ La domanda deve avere almeno 3 caratteri.", reply_markup=_cancel_kb())
        return
    if err := _too_long(text, _MAX_QUESTION, "La domanda è troppo lunga"):
        await message.answer(err, reply_markup=_cancel_kb())
        return
    await state.update_data(q_text=text)
    await _prompt_question_options(message, state)


@router.message(QuizCreationStates.waiting_question_options, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_question_options(message: Message, state: FSMContext) -> None:
    options = [o.strip() for o in (message.text or "").splitlines() if o.strip()]
    if err := _options_error(options):
        await message.answer(err, reply_markup=_back_cancel_kb())
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
    explanation = (message.text or "").strip()
    if err := _too_long(explanation, _MAX_EXPLANATION, "La spiegazione è troppo lunga"):
        await message.answer(err, reply_markup=_explanation_kb())
        return
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
        time_limit_seconds=data.get("time_limit", 0),
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
    b = InlineKeyboardBuilder()
    b.button(text="🧪 Prova il quiz", callback_data=f"quiz_try:start:{quiz.id}")
    b.adjust(1)
    await callback.message.answer(
        f"🎉 <b>Quiz pronto!</b>\n\n"
        f"🧠 <b>#{quiz.id} {esc(quiz.title)}</b>\n"
        f"❓ Domande: <b>{len(quiz.questions)}</b>\n"
        f"💰 Premi: {quiz_service.format_prize_summary(quiz)}\n\n"
        f"Avvialo nel gruppo con <code>/avvia_quiz {quiz.id}</code> o <code>/quiz</code>, "
        f"oppure dalla dashboard <code>/admin</code>.\n\n"
        f"<i>Prima di avviarlo puoi provarlo tu: la prova non viene salvata "
        f"e non conta in classifica.</i>",
        reply_markup=b.as_markup(),
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
    QuizCreationStates.waiting_time_limit.state: _prompt_prize_mode,
    QuizCreationStates.waiting_randomize.state: _prompt_time_limit,
    QuizCreationStates.waiting_question_options.state: _prompt_question_text,
    QuizCreationStates.waiting_correct.state: _prompt_question_options,
    QuizCreationStates.waiting_explanation.state: _prompt_correct,
}


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


# ---------------------------------------------------------------------------
# Launch (announce in group) + listing + close
# ---------------------------------------------------------------------------

async def open_quiz(bot, db_session: AsyncSession, quiz_id: int) -> tuple[bool, str]:
    """Set a quiz running and announce it in the group. Caller commits."""
    group_id = group_registry.get_group_id()
    if group_id == 0:
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
    limit = quiz_service.time_limit_seconds(quiz)
    tl_text = f"⏱️ {limit}s/domanda" if limit else "⏱️ senza limite"
    try:
        bot_info = await bot.get_me()
        await group_registry.send_group_message(
            bot, db_session,
            f"🧠 <b>QUIZ: {esc(quiz.title)}</b>\n"
            f"❓ {len(quiz.questions)} domande · {tl_text} · 🏆 {quiz_service.format_prize_summary(quiz)}\n\n"
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


async def _show_play_view(message: Message, db_session: AsyncSession) -> None:
    """Non-admin view of /quiz: a button to play the running quiz, or a clear
    'no quiz active' message — never silence (the old admin filter just dropped
    a non-admin's /quiz). One live reply per user in groups (anti-flood)."""
    if not await cooldown.ready(message, "quiz", settings.command_cooldown_seconds):
        return
    running = [q for q in await quiz_service.list_ready(db_session) if q.status == "running"]
    if not running:
        await reply_static(
            message,
            "🧠 <b>Nessun quiz attivo al momento.</b>\n"
            "Quando un admin ne avvia uno te lo segnaliamo qui nel gruppo! 🏁",
            "quiz",
        )
        return
    bot_info = await message.bot.get_me()
    b = InlineKeyboardBuilder()
    for q in running:
        # Button text is not HTML-parsed → raw title is fine here.
        b.button(
            text=f"▶️ Gioca: {q.title[:30]}",
            url=f"https://t.me/{bot_info.username}?start=quiz_{q.id}",
        )
    b.adjust(1)
    await reply_static(
        message,
        "🧠 <b>Quiz in corso!</b> Tocca per giocare in chat privata:",
        "quiz",
        reply_markup=b.as_markup(),
    )


@router.message(Command("quiz"))
async def cmd_quiz_list(message: Message, db_session: AsyncSession) -> None:
    # Public entry point. Non-admins get the "play" view (with a clear message
    # when no quiz is active); admins get the events-hub management list. The admin
    # branch is still gated by this in-handler is_admin check.
    if not await is_admin(message.bot, message.from_user.id):
        await _show_play_view(message, db_session)
        return
    if await redirect_to_private(message, "admin", "🛠️ Apri il pannello", notice=_QUIZ_PRIVATE_NOTICE):
        return
    # Management goes through the events hub: tapping a quiz opens its detail
    # screen (info + avvia/programma/chiudi/riproponi/elimina, each with a
    # confirmation) — never a one-tap launch (STEERING §18.2).
    from handlers.event_types.quiz_type import QuizType

    await QuizType().render_list(message, db_session)


@router.message(Command("avvia_quiz"), IsAdminFilter())
async def cmd_avvia_quiz(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "admin", "🛠️ Apri il pannello", notice=_QUIZ_PRIVATE_NOTICE):
        return
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
    # Lock the quiz row: the status check below and the prize payout must be atomic,
    # or two concurrent closes could both pass the guard and pay out twice.
    quiz = await quiz_service.get_quiz(db_session, quiz_id, for_update=True)
    if quiz is None:
        return False, "Quiz non trovato."
    if quiz.status == "finished":
        return False, "Questo quiz è già stato chiuso."
    if quiz.status != "running":
        return False, "Questo quiz non è in corso (avvialo prima)."

    ranked = await quiz_service.podium(db_session, quiz_id)
    awards = await quiz_service.award_prizes(db_session, quiz_id)
    await quiz_service.set_status(db_session, quiz_id, "finished")

    # Record Trivia progress for the trophy engine, then re-check trophies for
    # everyone it could affect (not just the podium):
    #   • podium, top 3          → game_podiums (podium_count / first_place_count)
    #   • last place, ≥2 players → "trivia_last_place" event (hidden trophies)
    #   • completed under 30 s   → "trivia_sub30" event (Velocista / Need For Speed …)
    # plus any XP/coin milestone hit by the quiz rewards. record_event is idempotent
    # per (user, metric, quiz) so a re-close cannot double-count.
    affected: set[int] = set()
    for rank, row in enumerate(ranked[:3], start=1):
        await progress_service.record_podium(db_session, row.user_tg_id, "trivia", rank, quiz_id)
        affected.add(row.user_tg_id)
    if len(ranked) >= 2:
        last = ranked[-1]
        await progress_service.record_event(
            db_session, last.user_tg_id, progress_service.TRIVIA_LAST_PLACE, quiz_id
        )
        affected.add(last.user_tg_id)
    for row in ranked:
        if row.completion_ms < 30_000:  # total play time under 30 seconds
            await progress_service.record_event(
                db_session, row.user_tg_id, progress_service.TRIVIA_SUB30, quiz_id
            )
            affected.add(row.user_tg_id)
    await db_session.flush()  # make the new rows visible to the count queries
    trophy_notes: dict[int, list] = {}
    for uid in affected:
        earned = await badge_service.check_and_award_milestones(db_session, uid)
        if earned:
            trophy_notes[uid] = earned
    await db_session.commit()

    # Announce newly unlocked trophies in the group, tagging each finisher.
    for uid, earned in trophy_notes.items():
        await announce_trophies(bot, db_session, uid, earned)

    text = await _podium_text(db_session, quiz.title, ranked, awards)
    if group_registry.get_group_id() != 0:
        try:
            await group_registry.send_group_message(bot, db_session, text)
        except Exception:  # noqa: BLE001
            log.warning("Impossibile annunciare il podio nel gruppo.")
        return True, "🏁 Quiz chiuso. Podio pubblicato nel gruppo."
    return True, text


@router.message(Command("chiudi_quiz"), IsAdminFilter())
async def cmd_chiudi_quiz(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "admin", "🛠️ Apri il pannello", notice=_QUIZ_PRIVATE_NOTICE):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.reply("ℹ️ Uso: <code>/chiudi_quiz &lt;id&gt;</code>")
        return
    ok, msg = await close_quiz(message.bot, db_session, int(raw))
    await message.reply(msg if ok else f"⚠️ {msg}")


async def _podium_text(db_session: AsyncSession, title: str, ranked, awards) -> str:
    if not ranked:
        return f"🏁 <b>{esc(title)}</b> — chiuso!\n\n<i>Nessun partecipante ha completato il quiz.</i>"
    award_by_user = {a.user_tg_id: a for a in awards}
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏁 <b>{esc(title)} — PODIO</b>\n"]
    for i, row in enumerate(ranked[:10]):
        rank = medals[i] if i < 3 else f"{i + 1}."
        name = await _display_name(db_session, row.user_tg_id)
        award = award_by_user.get(row.user_tg_id)
        prize_txt = ""
        if award and award.coins:
            icon = "🎖️" if award.kind == "consolation" else "🏆"
            prize_txt = f" — {icon} <b>+{award.coins} 🪙 CoInn</b>"
        time_txt = ""
        if row.completion_seconds is not None:
            time_txt = f" · ⏱️ {format_seconds_short(row.completion_seconds)}"
        lines.append(f"{rank} {name} — {row.correct} ✅{time_txt}{prize_txt}")
    if settings.quiz_xp_participation > 0 or settings.quiz_xp_per_correct > 0:
        lines.append(
            f"\n⚡ <i>XP a tutti i partecipanti: {settings.quiz_xp_participation} di base"
            f" + {settings.quiz_xp_per_correct}/risposta giusta + bonus podio.</i>"
        )
    return "\n".join(lines)


async def _display_name(db_session: AsyncSession, tg_id: int) -> str:
    return await mention(db_session, tg_id)


# ---------------------------------------------------------------------------
# Private play (one question at a time, with an optional per-question timer)
# ---------------------------------------------------------------------------

@dataclass
class _PlayCtx:
    """In-memory state of the question a user is currently looking at — used to
    measure response time and to run the countdown. Keyed by (quiz_id, user_tg_id)."""
    question_id: int
    shown_at: float          # time.monotonic() when the question was sent
    message_id: int
    chat_id: int
    timer: "asyncio.Task | None" = None


_PLAY: dict[tuple[int, int], _PlayCtx] = {}


def _play_key(quiz_id: int, user_tg_id: int) -> tuple[int, int]:
    return (quiz_id, user_tg_id)


def _cancel_task(task: "asyncio.Task | None") -> None:
    """Cancel a question's countdown task — but NEVER the one currently running.
    A timer that cancels itself would raise CancelledError (a BaseException, so it
    slips past ``except Exception``) at the next await and abort the very coroutine
    that is finishing the quiz — the user would never see "Quiz completato!"."""
    if task is not None and task is not asyncio.current_task():
        task.cancel()


def _cancel_timer(quiz_id: int, user_tg_id: int) -> None:
    ctx = _PLAY.get(_play_key(quiz_id, user_tg_id))
    if ctx is not None:
        _cancel_task(ctx.timer)


def _forget_play(quiz_id: int, user_tg_id: int) -> None:
    ctx = _PLAY.pop(_play_key(quiz_id, user_tg_id), None)
    if ctx is not None:
        _cancel_task(ctx.timer)


def _response_ms(quiz_id: int, user_tg_id: int, question_id: int, limit: int) -> int:
    """Milliseconds the user took on this question (from the in-memory shown_at).
    Falls back to 0 if the context is missing (e.g. after a restart)."""
    ctx = _PLAY.get(_play_key(quiz_id, user_tg_id))
    if ctx is None or ctx.question_id != question_id:
        return 0
    ms = round((time.monotonic() - ctx.shown_at) * 1000)
    if limit > 0:
        ms = min(ms, limit * 1000)
    return max(0, ms)


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
            f"✅ Hai già completato <b>{esc(quiz.title)}</b>: <b>{correct}/{total}</b> corrette.\n"
            "Aspetta la chiusura per vedere il podio! 🏁"
        )
        return

    limit = quiz_service.time_limit_seconds(quiz)
    rules = (
        f"Hai <b>{limit} secondi</b> per ogni domanda: allo scadere è data come sbagliata. "
        "A parità di risposte, chi finisce prima sale sul podio!"
        if limit > 0 else
        "Nessun limite di tempo, ma chi finisce prima sale sul podio a parità di risposte!"
    )
    await message.answer(f"🧠 <b>{esc(quiz.title)}</b>\n<i>{rules}</i>")
    await _present_question(message.bot, message.chat.id, message.from_user.id, quiz, done)


async def _present_question(bot: Bot, chat_id: int, user_tg_id: int, quiz, index: int) -> None:
    """Send question #index (in this user's order) and arm its countdown (if a limit is set)."""
    question = quiz_service.user_question_order(quiz, user_tg_id)[index]
    ordered_options = quiz_service.user_option_order(quiz, question, user_tg_id)
    limit = question.open_period
    hint = f"\n\n⏱️ <i>Hai {limit} secondi</i>" if limit > 0 else ""
    sent = await bot.send_message(
        chat_id,
        f"❓ <b>Domanda {index + 1}/{len(quiz.questions)}</b>\n\n{esc(question.text)}{hint}",
        reply_markup=_question_kb(quiz.id, question.id, ordered_options),
    )

    key = _play_key(quiz.id, user_tg_id)
    old = _PLAY.pop(key, None)
    if old is not None:
        _cancel_task(old.timer)
    ctx = _PlayCtx(
        question_id=question.id, shown_at=time.monotonic(),
        message_id=sent.message_id, chat_id=chat_id,
    )
    _PLAY[key] = ctx
    if limit > 0:
        ctx.timer = asyncio.create_task(
            _expire_question(bot, chat_id, quiz.id, user_tg_id, question.id, sent.message_id, limit)
        )


async def _advance_or_finish(
    bot: Bot, chat_id: int, user_tg_id: int, quiz, db_session: AsyncSession
) -> None:
    """Send the next question or, once all are answered, the final wrap-up."""
    total = len(quiz.questions)
    done = await quiz_service.answered_count(db_session, quiz.id, user_tg_id)
    if done < total:
        await _present_question(bot, chat_id, user_tg_id, quiz, done)
        return
    _forget_play(quiz.id, user_tg_id)
    correct = await quiz_service.correct_count(db_session, quiz.id, user_tg_id)
    secs = await quiz_service.user_completion_seconds(db_session, quiz.id, user_tg_id)
    time_line = (
        f"⏱️ Tempo impiegato: <b>{format_seconds_short(secs)}</b>\n" if secs is not None else ""
    )
    await bot.send_message(
        chat_id,
        f"🏁 <b>Quiz completato!</b>\n\n"
        f"Hai totalizzato <b>{correct}/{total}</b> risposte corrette.\n"
        f"{time_line}"
        "Aspetta la chiusura per scoprire il podio! 🏆",
    )


async def _expire_question(
    bot: Bot, chat_id: int, quiz_id: int, user_tg_id: int,
    question_id: int, message_id: int, limit: int,
) -> None:
    """Countdown for one question: after `limit` seconds, if still unanswered, mark
    it wrong and advance. Uses its own DB session (the request's is long gone)."""
    try:
        await asyncio.sleep(limit)
    except asyncio.CancelledError:
        return
    try:
        async with async_session_maker() as session:
            quiz = await quiz_service.get_quiz(session, quiz_id)
            if quiz is None or quiz.status != "running":
                return
            question = next((q for q in quiz.questions if q.id == question_id), None)
            if question is None:
                return
            # -1 = no option chosen → never matches the correct id → wrong.
            outcome = await quiz_service.record_answer(
                session, quiz_id, question_id, user_tg_id, -1, response_ms=limit * 1000
            )
            if outcome is None or not outcome.recorded:
                return  # the user answered just in time — nothing to do
            await session.commit()

            options = quiz_service.question_options(question)
            correct_label = esc(options[outcome.correct_option_id])
            feedback = (
                "⏱️ <b>Tempo scaduto!</b> Nessuna risposta.\n"
                f"✅ Giusta: <b>{correct_label}</b>"
            )
            if question.explanation:
                feedback += f"\n\n💡 <i>{esc(question.explanation)}</i>"
            try:
                await bot.edit_message_text(
                    f"❓ {esc(question.text)}\n\n{feedback}",
                    chat_id=chat_id, message_id=message_id,
                )
            except Exception:  # noqa: BLE001 — message may be too old to edit
                pass
            await _advance_or_finish(bot, chat_id, user_tg_id, quiz, session)
    except Exception:  # noqa: BLE001 — a timer must never crash the event loop
        log.exception("Timer domanda quiz %s/%s fallito", quiz_id, question_id)


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

    user_tg_id = callback.from_user.id
    # Enforce sequential play: only the user's current question (in THEIR order,
    # which may be randomized — §19) is answerable.
    order = quiz_service.user_question_order(quiz, user_tg_id)
    idx_in_order = next((i for i, q in enumerate(order) if q.id == question.id), -1)
    done_before = await quiz_service.answered_count(db_session, quiz_id, user_tg_id)
    if idx_in_order != done_before:
        await callback.answer(
            "Hai già risposto a questa domanda."
            if idx_in_order < done_before
            else "Rispondi prima alla domanda corrente."
        )
        return

    # Measure the response time, then stop this question's countdown before recording.
    resp_ms = _response_ms(quiz_id, user_tg_id, question_id, question.open_period)
    _cancel_timer(quiz_id, user_tg_id)
    outcome = await quiz_service.record_answer(
        db_session, quiz_id, question_id, user_tg_id, opt_idx, response_ms=resp_ms
    )
    if outcome is None:
        await callback.answer("Domanda non valida.", show_alert=True)
        return
    if not outcome.recorded:
        await callback.answer("Hai già risposto a questa domanda.")
        return
    await db_session.commit()

    # Feedback on the answered message (buttons removed).
    chosen = esc(options[opt_idx])
    if outcome.is_correct:
        feedback = f"✅ <b>Esatto!</b> — {chosen}"
    else:
        correct_label = esc(options[outcome.correct_option_id])
        feedback = f"❌ <b>Sbagliato.</b> Hai scelto: {chosen}\n✅ Giusta: <b>{correct_label}</b>"
    if question.explanation:
        feedback += f"\n\n💡 <i>{esc(question.explanation)}</i>"
    try:
        await callback.message.edit_text(f"❓ {esc(question.text)}\n\n{feedback}")
    except Exception:  # noqa: BLE001 — editing may fail if message is too old
        await callback.message.answer(feedback)

    await _advance_or_finish(callback.bot, callback.message.chat.id, user_tg_id, quiz, db_session)
    await callback.answer()


# ---------------------------------------------------------------------------
# Test run — admin dry-run of a `ready` quiz (§19.b)
# ---------------------------------------------------------------------------
#
# Lets an admin play a quiz after publishing it but before starting it, to check
# wording, options and explanations for real. The whole flow is IN-MEMORY: it
# never writes a `quiz_answers` row, so a test run can't reach the podium, the
# prizes, the XP or `game_podiums` — the isolation is structural, not a filter
# someone has to remember to apply on every query.
#
# Separate callback namespace (`quiz_try:*` vs `quiz_ans:*`) so a test answer can
# never be routed into the real recorder. Admin-gated per handler: quiz.router is
# mixed (public `quiz_ans:*`), so it cannot carry a router-level filter (§8).


@dataclass
class _TryCtx:
    """One admin's test run. Lives only in memory and dies with the process — a
    lost run costs nothing, so there is no persistence to justify."""
    quiz_id: int
    order: list[int]                  # question ids, in this run's display order
    index: int = 0                    # how many questions are already answered
    correct: int = 0


_TRY: dict[tuple[int, int], _TryCtx] = {}

_TRY_BANNER = "🧪 <b>MODALITÀ PROVA</b> — nulla viene salvato né conta in classifica."


def _try_key(quiz_id: int, admin_tg_id: int) -> tuple[int, int]:
    return (quiz_id, admin_tg_id)


def _try_question_kb(
    quiz_id: int, question_id: int, ordered_options: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for real_idx, opt in ordered_options:
        b.button(text=opt[:40], callback_data=f"quiz_try:ans:{quiz_id}:{question_id}:{real_idx}")
    b.button(text="⏹ Esci dalla prova", callback_data=f"quiz_try:stop:{quiz_id}")
    b.adjust(1)
    return b.as_markup()


async def start_quiz_try(message: Message, db_session: AsyncSession, quiz_id: int) -> None:
    """Begin (or restart) an admin test run of a `ready` quiz."""
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or not quiz.questions:
        await message.answer("⚠️ Quiz non trovato.")
        return
    if quiz.status != "ready":
        await message.answer(
            "🧪 La prova è disponibile solo per un quiz <b>pronto</b> e non ancora avviato."
        )
        return

    admin_id = message.from_user.id
    # `started_at` is still NULL here, so the randomization seed is stable for the
    # whole test run — the admin sees one coherent order, not a reshuffle per question.
    order = [q.id for q in quiz_service.user_question_order(quiz, admin_id)]
    _TRY[_try_key(quiz_id, admin_id)] = _TryCtx(quiz_id=quiz_id, order=order)

    limit = quiz_service.time_limit_seconds(quiz)
    timing = (
        f"Nel quiz vero ogni domanda avrà <b>{limit} secondi</b>; in prova non c'è timer."
        if limit > 0
        else "Nessun limite di tempo, come nel quiz vero."
    )
    await message.answer(
        f"{_TRY_BANNER}\n\n🧠 <b>{esc(quiz.title)}</b>\n<i>{timing}</i>"
    )
    await _present_try_question(message, db_session, quiz, admin_id)


async def _present_try_question(
    message: Message, db_session: AsyncSession, quiz, admin_id: int
) -> None:
    ctx = _TRY.get(_try_key(quiz.id, admin_id))
    if ctx is None:
        return
    question = next((q for q in quiz.questions if q.id == ctx.order[ctx.index]), None)
    if question is None:  # question deleted mid-run
        await _finish_try(message, quiz, admin_id)
        return
    ordered_options = quiz_service.user_option_order(quiz, question, admin_id)
    await message.answer(
        f"🧪 <b>Domanda {ctx.index + 1}/{len(ctx.order)}</b>\n\n{esc(question.text)}",
        reply_markup=_try_question_kb(quiz.id, question.id, ordered_options),
    )


async def _finish_try(message: Message, quiz, admin_id: int) -> None:
    ctx = _TRY.pop(_try_key(quiz.id, admin_id), None)
    total = len(ctx.order) if ctx else 0
    correct = ctx.correct if ctx else 0
    b = InlineKeyboardBuilder()
    b.button(text="🔁 Riprova", callback_data=f"quiz_try:start:{quiz.id}")
    b.adjust(1)
    await message.answer(
        f"🧪 <b>Prova completata</b>\n\n"
        f"Risultato: <b>{correct}/{total}</b> corrette.\n\n"
        f"<i>Nessun dato è stato salvato: il quiz è ancora pronto da avviare "
        f"e la classifica è intatta.</i>",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data.startswith("quiz_try:start:"), IsAdminCallbackFilter())
async def cb_try_start(callback: CallbackQuery, db_session: AsyncSession) -> None:
    quiz_id = int(callback.data.split(":")[2])
    await start_quiz_try(callback.message, db_session, quiz_id)
    await callback.answer()


@router.callback_query(F.data.startswith("quiz_try:stop:"), IsAdminCallbackFilter())
async def cb_try_stop(callback: CallbackQuery) -> None:
    quiz_id = int(callback.data.split(":")[2])
    _TRY.pop(_try_key(quiz_id, callback.from_user.id), None)
    try:
        await callback.message.edit_text("🧪 Prova interrotta. Nessun dato salvato.")
    except Exception:  # noqa: BLE001 — message may be too old to edit
        await callback.message.answer("🧪 Prova interrotta. Nessun dato salvato.")
    await callback.answer()


@router.callback_query(F.data.startswith("quiz_try:ans:"), IsAdminCallbackFilter())
async def cb_try_answer(callback: CallbackQuery, db_session: AsyncSession) -> None:
    try:
        _, _, raw_quiz, raw_q, raw_opt = callback.data.split(":")
        quiz_id, question_id, opt_idx = int(raw_quiz), int(raw_q), int(raw_opt)
    except (ValueError, IndexError):
        await callback.answer("Dati non validi.", show_alert=True)
        return

    admin_id = callback.from_user.id
    ctx = _TRY.get(_try_key(quiz_id, admin_id))
    if ctx is None:
        await callback.answer("🧪 Prova scaduta. Riavviala dal quiz.", show_alert=True)
        return
    if ctx.order[ctx.index] != question_id:
        await callback.answer("Hai già risposto a questa domanda.")
        return

    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or quiz.status != "ready":
        _TRY.pop(_try_key(quiz_id, admin_id), None)
        await callback.answer("⚠️ Il quiz non è più in prova.", show_alert=True)
        return
    question = next((q for q in quiz.questions if q.id == question_id), None)
    if question is None:
        await callback.answer("Domanda non valida.", show_alert=True)
        return
    options = quiz_service.question_options(question)
    if not (0 <= opt_idx < len(options)):
        await callback.answer("Opzione non valida.", show_alert=True)
        return

    is_correct = opt_idx == question.correct_option_id
    ctx.index += 1
    ctx.correct += int(is_correct)

    chosen = esc(options[opt_idx])
    if is_correct:
        feedback = f"✅ <b>Esatto!</b> — {chosen}"
    else:
        feedback = (
            f"❌ <b>Sbagliato.</b> Hai scelto: {chosen}\n"
            f"✅ Giusta: <b>{esc(options[question.correct_option_id])}</b>"
        )
    if question.explanation:
        feedback += f"\n\n💡 <i>{esc(question.explanation)}</i>"
    else:
        feedback += "\n\n<i>(nessuna spiegazione impostata)</i>"
    try:
        await callback.message.edit_text(f"🧪 ❓ {esc(question.text)}\n\n{feedback}")
    except Exception:  # noqa: BLE001 — editing may fail if message is too old
        await callback.message.answer(feedback)

    if ctx.index >= len(ctx.order):
        await _finish_try(callback.message, quiz, admin_id)
    else:
        await _present_try_question(callback.message, db_session, quiz, admin_id)
    await callback.answer()
