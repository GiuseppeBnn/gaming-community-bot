"""Admin dry-run: the real poll presenter, structurally no score writes."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot, F
from aiogram.types import CallbackQuery, Message, PollAnswer
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from filters.admin_filter import IsAdminCallbackFilter, is_admin
from handlers.callbacks import QuizTryCb
from handlers.quiz import _native
from handlers.quiz._shared import router
from services import quiz_service
from utils.text import esc


@dataclass
class _TryCtx:
    quiz_id: int
    order: list[int]
    index: int = 0
    correct: int = 0
    poll_id: str | None = None
    message_id: int | None = None
    option_order: tuple[int, ...] = ()


_TRY: dict[tuple[int, int], _TryCtx] = {}
_TRY_BANNER = "🧪 <b>MODALITÀ PROVA</b> — nulla conta in classifica."


async def start_quiz_try(
    message: Message,
    db_session: AsyncSession,
    quiz_id: int,
    admin_id: int,
) -> None:
    """admin_id is the tapping human, never message.from_user (the bot)."""
    if message.chat.type != "private":
        return
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    await db_session.commit()
    if quiz is None or not quiz.questions:
        await message.answer("⚠️ Quiz non trovato.")
        return
    if quiz.status != "ready":
        await message.answer(
            "🧪 La prova è disponibile solo per un quiz pronto, non ancora avviato."
        )
        return
    old = _TRY.pop((quiz_id, admin_id), None)
    if old is not None and old.message_id is not None:
        await _native.stop_question(message.bot, admin_id, old.message_id)
    ctx = _TryCtx(
        quiz_id=quiz.id,
        order=[q.id for q in quiz_service.user_question_order(quiz, admin_id)],
    )
    _TRY[(quiz_id, admin_id)] = ctx
    await message.answer(
        f"{_TRY_BANNER}\n\n🧠 <b>{esc(quiz.title)}</b>\n"
        "In prova non c'è timer. Nessun premio o XP viene assegnato."
    )
    await _present(message.bot, quiz, admin_id, ctx)


async def _present(bot: Bot, quiz, admin_id: int, ctx: _TryCtx) -> None:
    question = next((q for q in quiz.questions if q.id == ctx.order[ctx.index]), None)
    if question is None:
        await _finish(bot, admin_id, ctx)
        return
    ordered = quiz_service.user_option_order(quiz, question, admin_id)
    b = InlineKeyboardBuilder()
    b.button(
        text="⏹ Esci dalla prova", callback_data=QuizTryCb(action="stop", quiz_id=quiz.id).pack()
    )
    sent = await _native.send_question(
        bot,
        admin_id,
        question,
        ordered,
        index=ctx.index,
        total=len(ctx.order),
        trial=True,
        reply_markup=b.as_markup(),
    )
    if _TRY.get((quiz.id, admin_id)) is not ctx:
        await _native.stop_question(bot, admin_id, sent.message_id)
        return
    ctx.poll_id, ctx.message_id = sent.poll.id, sent.message_id
    ctx.option_order = tuple(real_id for real_id, _ in ordered)


async def _finish(bot: Bot, admin_id: int, ctx: _TryCtx) -> None:
    if _TRY.get((ctx.quiz_id, admin_id)) is not ctx:
        return
    _TRY.pop((ctx.quiz_id, admin_id), None)
    b = InlineKeyboardBuilder()
    b.button(text="🔁 Riprova", callback_data=QuizTryCb(action="start", quiz_id=ctx.quiz_id).pack())
    await bot.send_message(
        admin_id,
        f"🧪 <b>Prova completata</b>\n\n<b>{ctx.correct}/{len(ctx.order)}</b> corrette.\n"
        "Nessun dato salvato in classifica, nessun premio assegnato.",
        reply_markup=b.as_markup(),
    )


async def on_answer(answer: PollAnswer, bot: Bot, db_session: AsyncSession) -> bool:
    match = next(((key, ctx) for key, ctx in _TRY.items() if ctx.poll_id == answer.poll_id), None)
    if match is None:
        return False
    (quiz_id, admin_id), ctx = match
    user = answer.user
    if user is None or user.id != admin_id or len(answer.option_ids) != 1:
        return True
    option = answer.option_ids[0]
    if not 0 <= option < len(ctx.option_order):
        return True
    # Consume before the first await: concurrent updates cannot advance twice.
    ctx.poll_id = None
    allowed = await is_admin(bot, admin_id)
    if _TRY.get((quiz_id, admin_id)) is not ctx:
        return True
    if not allowed:
        _TRY.pop((quiz_id, admin_id), None)
        return True
    quiz = await quiz_service.get_quiz(db_session, quiz_id)
    if quiz is None or quiz.status != "ready":
        _TRY.pop((quiz_id, admin_id), None)
        await db_session.commit()
        await _native.stop_question(bot, admin_id, ctx.message_id)
        await bot.send_message(admin_id, "🧪 Prova terminata: il quiz non è più disponibile.")
        return True
    question = next((q for q in quiz.questions if q.id == ctx.order[ctx.index]), None)
    if question is None:
        await db_session.commit()
        await _finish(bot, admin_id, ctx)
        return True
    ctx.correct += int(ctx.option_order[option] == question.correct_option_id)
    ctx.index += 1
    await db_session.commit()
    if _TRY.get((quiz_id, admin_id)) is not ctx:
        return True
    await _native.stop_question(bot, admin_id, ctx.message_id)
    if ctx.index == len(ctx.order):
        await _finish(bot, admin_id, ctx)
    else:
        await _present(bot, quiz, admin_id, ctx)
    return True


@router.callback_query(QuizTryCb.filter(F.action == "start"), IsAdminCallbackFilter())
async def cb_try_start(
    callback: CallbackQuery, callback_data: QuizTryCb, db_session: AsyncSession
) -> None:
    await start_quiz_try(callback.message, db_session, callback_data.quiz_id, callback.from_user.id)
    await callback.answer()


@router.callback_query(QuizTryCb.filter(F.action == "stop"), IsAdminCallbackFilter())
async def cb_try_stop(callback: CallbackQuery, callback_data: QuizTryCb) -> None:
    admin_id = callback.from_user.id
    old = _TRY.pop((callback_data.quiz_id, admin_id), None)
    if old is not None and old.message_id is not None:
        await _native.stop_question(callback.bot, admin_id, old.message_id)
    await callback.message.answer("🧪 Prova interrotta. Nessun dato salvato.")
    await callback.answer()


@router.callback_query(QuizTryCb.filter(F.action == "answer"), IsAdminCallbackFilter())
async def cb_try_answer(callback: CallbackQuery, callback_data: QuizTryCb) -> None:
    await callback.answer(
        "Questa prova usa ora i quiz nativi. Riavviala dal pannello.", show_alert=True
    )
