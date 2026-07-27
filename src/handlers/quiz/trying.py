"""Admin dry-run: play a quiz without recording anything.

Same questions and the same feedback as the real thing, but nothing is written and
nothing counts towards the podium — the point is to catch a typo before an audience
sees it. Entirely in memory, keyed per (quiz, admin)."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from filters.admin_filter import IsAdminCallbackFilter
from services import quiz_service
from utils.text import esc

from handlers.quiz._shared import (
    router,
)


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


async def start_quiz_try(
    message: Message, db_session: AsyncSession, quiz_id: int, admin_id: int
) -> None:
    """Begin (or restart) an admin test run of a `ready` quiz.

    ``admin_id`` is passed in explicitly and has NO default on purpose: every entry
    point reaches this through a button on a message the BOT sent, so
    ``message.from_user`` is the bot, not the admin who tapped (same trap as
    ``ev:new:quiz`` passing ``creator_id``, §19). Deriving the identity from
    `message` here stored the run under the bot's id while the answer handler
    looked it up under the admin's — every answer was refused as "Prova scaduta".
    Keep it a required parameter so a future entry point must state who is acting.
    """
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or not quiz.questions:
        await message.answer("⚠️ Quiz non trovato.")
        return
    if quiz.status != "ready":
        await message.answer(
            "🧪 La prova è disponibile solo per un quiz <b>pronto</b> e non ancora avviato."
        )
        return

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
    # `callback.from_user` is the admin who tapped; `callback.message.from_user`
    # would be the bot that posted the button (see start_quiz_try's docstring).
    await start_quiz_try(callback.message, db_session, quiz_id, callback.from_user.id)
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
