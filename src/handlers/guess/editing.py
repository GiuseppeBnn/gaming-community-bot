"""Fixing a round that is already out, without rebuilding it.

The judge is a model, and a model will occasionally turn down a spelling that any
human would have accepted. That is not something we can enumerate in advance —
it shows up when a player says «ho scritto X e me l'ha data sbagliata». The round
detail screen already lists the last rejected answers for exactly that reason;
what was missing was the one move that follows from reading them.

So: an admin adds accepted spellings to a live round, and from that moment on the
answer wins locally, before the model and before the cached verdict
(``guess_judge.judge`` consults the aliases first). **Forward-only, on purpose** —
attempts already judged stay judged. Re-scoring them would move a podium that has
been announced, and the player who was turned down can simply try again.

Available while the round is ``ready`` too: once created, that was the only field
with no way back to it short of deleting the round and building it again.
"""

from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers.callbacks import GuessAliasCb
from services import guess_service
from utils.text import esc

from handlers.guess._shared import _MAX_ALIASES, kind_of, router
from handlers.guess.creation import _parse_aliases

#: Adding is allowed before the round starts and while it runs. On a finished one
#: it would change nothing: nobody is guessing any more.
_EDITABLE = ("ready", "running")


class GuessAliasStates(StatesGroup):
    waiting_aliases = State()


def _cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Annulla", callback_data=GuessAliasCb(action="cancel").pack())
    return b.as_markup()


@router.callback_query(GuessAliasCb.filter(F.action == "add"), IsAdminCallbackFilter())
async def cb_add_aliases(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    callback_data: GuessAliasCb,
) -> None:
    round_id = callback_data.round_id
    if round_id is None:
        await callback.answer()
        return
    round_ = await guess_service.get_round(db_session, round_id)
    if round_ is None or round_.status not in _EDITABLE:
        await callback.answer("Round non modificabile (concluso o eliminato).", show_alert=True)
        return
    spec = kind_of(round_.kind)
    await state.clear()
    await state.update_data(alias_round=round_.id)
    await state.set_state(GuessAliasStates.waiting_aliases)
    await callback.message.answer(
        f"🔤 <b>{spec.emoji} {esc(round_.title)}</b>\n"
        f"✅ Risposta: <b>{esc(round_.answer)}</b>\n\n"
        f"Manda le <b>grafie da accettare</b>, una per riga (max {_MAX_ALIASES}).\n"
        "<i>Valgono da subito per chi risponde d'ora in poi, senza passare dall'AI. "
        "I tentativi già giudicati restano come sono — chi era stato scartato può "
        "riprovare.</i>",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.message(GuessAliasStates.waiting_aliases, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_aliases(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    values, err = _parse_aliases((message.text or "").strip(), {})
    if err:
        await message.answer(err, reply_markup=_cancel_kb())
        return
    if not values:  # «-», «no», … — the skip words mean "lascia stare"
        await state.clear()
        await message.answer("👍 Nessuna grafia aggiunta.")
        return
    round_id = (await state.get_data())["alias_round"]
    result = await guess_service.add_aliases(db_session, round_id, values)
    if result is None:
        await state.clear()
        await message.answer("⚠️ Round non trovato (eliminato nel frattempo?).")
        return
    added, skipped = result
    await db_session.commit()
    await state.clear()
    tail = f" ({skipped} già presenti o fuori spazio)" if skipped else ""
    await message.answer(
        f"✅ <b>{added} grafie aggiunte</b>{tail}.\nDa adesso chi le scrive viene accettato subito."
    )


@router.callback_query(GuessAliasCb.filter(F.action == "cancel"), IsAdminCallbackFilter())
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Nessuna grafia aggiunta.")
    await callback.answer()
