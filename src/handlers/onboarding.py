"""
First-contact handler for users who have not yet accepted the community rules.

Flow:
  1. show_rules_prompt()  — called by cmd_start the first time a user is seen
  2. cb_accept_rules      — callback on the "accept" button → marks onboarding done

No FSM states. Identity comes from Telegram (@username / first_name).
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from keyboards.onboarding_kb import get_rules_keyboard
from services import badge_service

router = Router()

_COMMUNITY_RULES = (
    "📋 <b>Regole della Community</b>\n\n"
    "1️⃣ Rispetta sempre gli altri membri.\n"
    "2️⃣ Niente spam o contenuti inappropriati.\n"
    "3️⃣ Le scommesse sono solo per divertimento — nessun denaro reale.\n"
    "4️⃣ Gli Aldueuri non hanno valore monetario.\n"
    "5️⃣ Gli admin hanno l'ultima parola sulle dispute.\n\n"
    "<i>Clicca il bottone per confermare di aver letto e accettare le regole.</i>"
)


async def show_rules_prompt(message: Message) -> None:
    """Entry point called from cmd_start when onboarding is not yet completed."""
    name = message.from_user.first_name
    await message.answer(
        f"🎮 <b>Ciao {name}, benvenuto nell'Arena!</b>\n\n"
        f"{_COMMUNITY_RULES}",
        reply_markup=get_rules_keyboard(),
    )


@router.callback_query(F.data == "rules:accept")
async def cb_accept_rules(
    callback: CallbackQuery, db_session: AsyncSession
) -> None:
    result = await db_session.execute(
        select(User).where(User.tg_id == callback.from_user.id)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        user.onboarding_completed = True
        await db_session.commit()

    _, is_new = await badge_service.award_badge(
        db_session, callback.from_user.id, badge_service.BADGE_FIRST_STEPS
    )
    if is_new:
        await db_session.commit()

    badge_text = (
        "\n\n🏅 <b>Badge sbloccato:</b> 🚀 <i>Primi Passi</i> (+50 XP)" if is_new else ""
    )

    name = callback.from_user.first_name
    await callback.message.edit_text(
        f"🎉 <b>Bentornato nella community, {name}!</b>\n\n"
        f"Sei ora un membro a pieno titolo.\n\n"
        f"💰 Usa /daily per il tuo primo premio giornaliero!\n\n"
        f"<b>Comandi disponibili:</b>\n"
        f"/profilo — Il tuo profilo\n"
        f"/saldo — Saldo e movimenti\n"
        f"/scommesse — Scommesse aperte\n"
        f"/traguardi — I tuoi badge\n"
        f"/help — Tutti i comandi{badge_text}"
    )
    await callback.answer("🎮 Benvenuto!")
