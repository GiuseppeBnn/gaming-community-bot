"""Building a quiz: the creation FSM.

One conversation from title to publish — title, description, prizes, per-question
time limit, shuffling, then a loop of questions with a review step. Every input is
validated against the caps in `_shared`, and nothing is silently truncated: an
over-long question is rejected with its real length, because a truncated one is only
discovered once the quiz is live.

The quiz row is created as soon as the prizes are settled, so a half-built quiz
exists as a `draft` and only `publish` arms it as `ready`."""

from __future__ import annotations

from functools import partial

from aiogram import F
from aiogram.enums import ChatType
from aiogram.filters.command import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers.callbacks import QuizNewCb
from keyboards.common_kb import confirm_cancel_kb
from services import quiz_service
from utils import cooldown
from utils.text import esc

from handlers.quiz._shared import (
    _MAX_DESC,
    _MAX_EXPLANATION,
    _MAX_OPTION,
    _MAX_OPTIONS,
    _MAX_QUESTION,
    _MAX_TITLE,
    _MIN_OPTIONS,
    _options_error,
    _too_long,
    router,
)


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
        InlineKeyboardButton(text="❌ Annulla", callback_data=QuizNewCb(action="cancel").pack())
    ]])


def _back_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Indietro", callback_data=QuizNewCb(action="back").pack()),
        InlineKeyboardButton(text="❌ Annulla", callback_data=QuizNewCb(action="cancel").pack()),
    ]])


def _back_to_review_kb() -> InlineKeyboardMarkup:
    """Shown on the question-text step when adding a further question: lets the
    admin abandon the new question and return to the review screen."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Riepilogo", callback_data=QuizNewCb(action="review").pack()),
        InlineKeyboardButton(text="❌ Annulla", callback_data=QuizNewCb(action="cancel").pack()),
    ]])


def _prize_mode_kb() -> InlineKeyboardMarkup:
    d1, d2, d3, dc = (
        settings.quiz_default_first, settings.quiz_default_second,
        settings.quiz_default_third, settings.quiz_default_consolation,
    )
    b = InlineKeyboardBuilder()
    b.button(text=f"⚡ Premi consigliati (🥇{d1} 🥈{d2} 🥉{d3} 🎖️{dc})", callback_data=QuizNewCb(action="quickprize").pack())
    b.button(text="✏️ Personalizza i premi", callback_data=QuizNewCb(action="customprize").pack())
    b.button(text="🚫 Nessun premio", callback_data=QuizNewCb(action="noprize").pack())
    b.button(text="⬅️ Indietro", callback_data=QuizNewCb(action="back").pack())
    b.button(text="❌ Annulla", callback_data=QuizNewCb(action="cancel").pack())
    b.adjust(1)
    return b.as_markup()


def _prize_step_kb(default_value: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Usa {default_value} 🪙", callback_data=QuizNewCb(action="usedefault").pack())
    b.button(text="⬅️ Indietro", callback_data=QuizNewCb(action="back").pack())
    b.button(text="❌ Annulla", callback_data=QuizNewCb(action="cancel").pack())
    b.adjust(1, 2)
    return b.as_markup()


_TIME_LIMIT_PRESETS = (15, 30, 45, 60)


def _time_limit_kb() -> InlineKeyboardMarkup:
    default = settings.quiz_default_time_limit_seconds
    b = InlineKeyboardBuilder()
    for sec in _TIME_LIMIT_PRESETS:
        mark = "✅ " if sec == default else "⏱️ "
        b.button(text=f"{mark}{sec}s", callback_data=QuizNewCb(action="time_limit", value=sec).pack())
    b.button(text="🚫 Nessun limite", callback_data=QuizNewCb(action="time_limit", value=0).pack())
    b.button(text="✏️ Personalizza", callback_data=QuizNewCb(action="time_limit_custom").pack())
    b.button(text="⬅️ Indietro", callback_data=QuizNewCb(action="back").pack())
    b.button(text="❌ Annulla", callback_data=QuizNewCb(action="cancel").pack())
    b.adjust(4, 1, 1, 2)
    return b.as_markup()


def _randomize_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔀 Domande", callback_data=QuizNewCb(action="randomize", key="q").pack())
    b.button(text="🔀 Risposte", callback_data=QuizNewCb(action="randomize", key="a").pack())
    b.button(text="🔀 Entrambe", callback_data=QuizNewCb(action="randomize", key="both").pack())
    b.button(text="🚫 Nessuna", callback_data=QuizNewCb(action="randomize", key="none").pack())
    b.button(text="⬅️ Indietro", callback_data=QuizNewCb(action="back").pack())
    b.button(text="❌ Annulla", callback_data=QuizNewCb(action="cancel").pack())
    b.adjust(2, 2, 2)
    return b.as_markup()


def _correct_kb(options: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        b.button(text=f"{i + 1}. {opt[:20]}", callback_data=QuizNewCb(action="correct", value=i).pack())
    b.button(text="⬅️ Indietro", callback_data=QuizNewCb(action="back").pack())
    b.button(text="❌ Annulla", callback_data=QuizNewCb(action="cancel").pack())
    b.adjust(1)
    return b.as_markup()


def _explanation_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭️ Salta", callback_data=QuizNewCb(action="skip_explanation").pack()),
        InlineKeyboardButton(text="⬅️ Indietro", callback_data=QuizNewCb(action="back").pack()),
        InlineKeyboardButton(text="❌ Annulla", callback_data=QuizNewCb(action="cancel").pack()),
    ]])


def _review_kb(question_count: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Aggiungi domanda", callback_data=QuizNewCb(action="add").pack())
    if question_count > 0:
        b.button(text="🗑 Rimuovi ultima", callback_data=QuizNewCb(action="remove_last").pack())
    b.button(text="✅ Pubblica", callback_data=QuizNewCb(action="publish").pack())
    b.button(text="❌ Annulla", callback_data=QuizNewCb(action="cancel").pack())
    b.adjust(1, 2)
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


@router.callback_query(QuizNewCb.filter(F.action == "cancel"), IsAdminCallbackFilter())
async def cb_quiz_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    # Confirm before discarding — the in-progress prompt above stays intact, so
    # "No" simply lets the admin keep going.
    if await state.get_state() is None:
        await callback.answer()
        return
    await callback.message.answer(
        "⚠️ Sicuro di voler annullare la creazione del quiz? I dati inseriti andranno persi.",
        reply_markup=confirm_cancel_kb(
            QuizNewCb(action="cancel_yes").pack(), QuizNewCb(action="cancel_no").pack()
        ),
    )
    await callback.answer()


@router.callback_query(QuizNewCb.filter(F.action == "cancel_yes"), IsAdminCallbackFilter())
async def cb_quiz_cancel_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Creazione quiz annullata.")
    await callback.answer()


@router.callback_query(QuizNewCb.filter(F.action == "cancel_no"), IsAdminCallbackFilter())
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

@router.callback_query(QuizNewCb.filter(F.action == "back"), IsAdminCallbackFilter())
async def cb_back(callback: CallbackQuery, state: FSMContext) -> None:
    prompter = _BACK_PROMPTERS.get(await state.get_state())
    if prompter is not None:
        await prompter(callback.message, state)
    await callback.answer()


@router.callback_query(QuizNewCb.filter(F.action == "review"), IsAdminCallbackFilter())
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

@router.callback_query(
    QuizCreationStates.waiting_prize_mode, QuizNewCb.filter(F.action == "quickprize"),
    IsAdminCallbackFilter(),
)
async def cb_quick_prize(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(
        prize_first=settings.quiz_default_first,
        prize_second=settings.quiz_default_second,
        prize_third=settings.quiz_default_third,
        prize_consolation=settings.quiz_default_consolation,
    )
    await _prompt_time_limit(callback.message, state)
    await callback.answer()


@router.callback_query(
    QuizCreationStates.waiting_prize_mode, QuizNewCb.filter(F.action == "noprize"),
    IsAdminCallbackFilter(),
)
async def cb_no_prize(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(prize_first=0, prize_second=0, prize_third=0, prize_consolation=0)
    await _prompt_time_limit(callback.message, state)
    await callback.answer()


@router.callback_query(
    QuizCreationStates.waiting_prize_mode, QuizNewCb.filter(F.action == "customprize"),
    IsAdminCallbackFilter(),
)
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


@router.callback_query(QuizNewCb.filter(F.action == "usedefault"), IsAdminCallbackFilter())
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
    QuizCreationStates.waiting_time_limit, QuizNewCb.filter(F.action == "time_limit"),
    IsAdminCallbackFilter(),
)
async def cb_time_limit(
    callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, callback_data: QuizNewCb
) -> None:
    value = callback_data.value
    if value is None:
        await callback.answer()
        return
    await state.update_data(time_limit=value)
    await _prompt_randomize(callback.message, state)
    await callback.answer()


@router.callback_query(
    QuizCreationStates.waiting_time_limit, QuizNewCb.filter(F.action == "time_limit_custom"),
    IsAdminCallbackFilter(),
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
    QuizCreationStates.waiting_randomize, QuizNewCb.filter(F.action == "randomize"),
    IsAdminCallbackFilter(),
)
async def cb_randomize(
    callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, callback_data: QuizNewCb
) -> None:
    choice = callback_data.key
    if choice is None:
        await callback.answer()
        return
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


@router.callback_query(
    QuizCreationStates.waiting_correct, QuizNewCb.filter(F.action == "correct"), IsAdminCallbackFilter()
)
async def cb_correct(
    callback: CallbackQuery, state: FSMContext, callback_data: QuizNewCb
) -> None:
    idx = callback_data.value
    if idx is None:
        await callback.answer()
        return
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


@router.callback_query(
    QuizCreationStates.waiting_explanation, QuizNewCb.filter(F.action == "skip_explanation"),
    IsAdminCallbackFilter(),
)
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

@router.callback_query(
    QuizCreationStates.reviewing, QuizNewCb.filter(F.action == "add"), IsAdminCallbackFilter()
)
async def cb_add_question(callback: CallbackQuery, state: FSMContext) -> None:
    await _prompt_question_text(callback.message, state)
    await callback.answer()


@router.callback_query(
    QuizCreationStates.reviewing, QuizNewCb.filter(F.action == "remove_last"), IsAdminCallbackFilter()
)
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


@router.callback_query(
    QuizCreationStates.reviewing, QuizNewCb.filter(F.action == "publish"), IsAdminCallbackFilter()
)
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
