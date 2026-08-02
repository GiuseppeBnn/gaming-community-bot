"""Fetta verticale dello spike su aiogram-dialog — codice temporaneo.

Non è una schermata del prodotto: è la prova che la libreria convive con questo
stack. Vive finché il gate della Fase 1 (spec §4.3) non decide se la libreria
resta o va via, e in entrambi i casi questo file sparisce — o perché si
abbandona, o perché lo sostituiscono le finestre vere di `guess/creation.py`.

Quello che dimostra, e che nessuna lettura della documentazione può dimostrare:
il `getter` riceve `db_session` dal middleware del progetto, quindi la DI per
chiave di STEERING §4 vale anche dentro un dialogo.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.widgets.kbd import Cancel
from aiogram_dialog.widgets.text import Const, Format
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter

router = Router()
# Admin-only at the root: a handler driven by dialog state alone would not
# re-check anything, and dialog state has no TTL (STEERING §8). This router is
# 100% admin (nothing behind it is public), so STEERING §8 makes both root
# filters mandatory, not just the message one: `spike_dialog`'s Cancel button
# is a callback_query handler registered on the *nested* Dialog router, and
# only the parent's own root filter check gates entry to it before aiogram
# ever recurses into that sub-router.
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminCallbackFilter())


class SpikeStates(StatesGroup):
    main = State()


async def user_count_getter(db_session: AsyncSession, **kwargs) -> dict:
    """Reads through the session the project's middleware injected.

    The count is deliberately a real query: a constant here would prove nothing
    about dependency injection, which is the whole point of this slice.
    """
    total = await db_session.scalar(select(func.count()).select_from(User))
    return {"total": total or 0}


spike_dialog = Dialog(
    Window(
        Const("Spike aiogram-dialog."),
        Format("Utenti a DB: {total}"),
        Cancel(Const("Chiudi")),
        state=SpikeStates.main,
        getter=user_count_getter,
    ),
)

router.include_router(spike_dialog)


@router.message(Command("spike"))
async def cmd_spike(message: Message, dialog_manager: DialogManager) -> None:
    await dialog_manager.start(SpikeStates.main, mode=StartMode.RESET_STACK)
