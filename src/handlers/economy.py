"""
Economy handlers: /saldo, /storico, /daily, /trasferisci, /credita (admin).
"""

import logging

from aiogram import Router
from aiogram.filters.command import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import TransactionType, User
from exceptions.economy import (
    DailyAlreadyClaimedError,
    InsufficientFundsError,
    SelfTransferError,
    WalletNotFoundError,
)
from filters.admin_filter import IsAdminFilter
from services import badge_service, economy_service, xp_service
from services.xp_service import XpSource

log = logging.getLogger(__name__)
router = Router()

_TX_LABELS = {
    TransactionType.deposit.value: "📥 Deposito",
    TransactionType.withdrawal.value: "📤 Prelievo",
    TransactionType.transfer_out.value: "➡️ Trasferimento inviato",
    TransactionType.transfer_in.value: "⬅️ Trasferimento ricevuto",
    TransactionType.bet_placed.value: "🎲 Scommessa piazzata",
    TransactionType.bet_won.value: "🏆 Vincita scommessa",
    TransactionType.bet_refund.value: "↩️ Rimborso scommessa",
    TransactionType.admin_credit.value: "🔐 Credito admin",
    TransactionType.admin_debit.value: "🔻 Addebito admin",
    TransactionType.daily_reward.value: "📅 Premio giornaliero",
    TransactionType.shop_purchase.value: "🛒 Acquisto negozio",
    TransactionType.quiz_reward.value: "🧠 Premio quiz",
}


@router.message(Command("saldo"))
async def cmd_saldo(message: Message, db_session: AsyncSession) -> None:
    try:
        balance = await economy_service.get_balance(db_session, message.from_user.id)
    except WalletNotFoundError:
        await message.answer("⚠️ Wallet non trovato. Usa /start per registrarti.")
        return
    await message.answer(f"🪙 Il tuo saldo: <b>{balance:,} Aldueuri</b>")


@router.message(Command("storico"))
async def cmd_storico(message: Message, db_session: AsyncSession) -> None:
    entries = await economy_service.get_history(db_session, message.from_user.id)
    if not entries:
        await message.answer("📋 Nessuna transazione ancora.")
        return

    lines = ["<b>📋 Ultime transazioni:</b>\n"]
    for entry in entries:
        label = _TX_LABELS.get(entry.tx_type, "📌 Transazione")
        sign = "+" if entry.amount > 0 else ""
        ts = entry.created_at
        lines.append(
            f"{label}\n"
            f"   <b>{sign}{entry.amount:,} 🪙</b>"
            f" — <i>{ts.strftime('%d/%m %H:%M')}</i>"
        )
    await message.answer("\n".join(lines))


@router.message(Command("daily"))
async def cmd_daily(message: Message, db_session: AsyncSession) -> None:
    try:
        reward, streak = await economy_service.claim_daily(db_session, message.from_user.id)
        await db_session.commit()
    except DailyAlreadyClaimedError as e:
        await message.answer(
            f"⏰ Hai già riscosso il premio oggi.\n"
            f"Riprova tra <b>{e.hours_remaining:.1f} ore</b>."
        )
        return
    except WalletNotFoundError:
        await message.answer("⚠️ Wallet non trovato. Usa /start per registrarti.")
        return

    # Capped participation XP + milestone check, committed together.
    xp_res = await xp_service.grant_xp(
        db_session, message.from_user.id, settings.xp_per_daily_claim,
        XpSource.daily, capped=True,
    )
    newly_earned = await badge_service.check_and_award_milestones(
        db_session, message.from_user.id
    )
    await db_session.commit()

    text = (
        f"🎉 <b>+{reward:,} Aldueuri</b> riscossi!\n"
        f"📅 Streak: <b>{streak}</b> giorni\n"
    )
    if xp_res.granted:
        text += f"⚡ <b>+{xp_res.granted} XP</b>\n"
    text += "🪙 Usa /saldo per vedere il tuo saldo."
    if xp_res.new_rank is not None:
        text += (
            f"\n\n{xp_res.new_rank.emoji} <b>Nuovo rango:</b> {xp_res.new_rank.name}!"
        )
    if newly_earned:
        badges_text = ", ".join(f"{b.icon_emoji} {b.name}" for b in newly_earned)
        text += f"\n\n🏅 <b>Trofeo sbloccato:</b> {badges_text}!"

    await message.answer(text)


@router.message(Command("trasferisci"))
async def cmd_trasferisci(message: Message, db_session: AsyncSession) -> None:
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "ℹ️ Uso: /trasferisci @username importo\n"
            "Esempio: /trasferisci @mario 100"
        )
        return

    target_ref = parts[1].lstrip("@")
    try:
        amount = int(parts[2])
    except ValueError:
        await message.answer("⚠️ L'importo deve essere un numero intero.")
        return

    if amount <= 0:
        await message.answer("⚠️ L'importo deve essere positivo.")
        return

    # Resolve target by username or tg_id
    if target_ref.isdigit():
        result = await db_session.execute(
            select(User).where(User.tg_id == int(target_ref))
        )
    else:
        result = await db_session.execute(
            select(User).where(User.username == target_ref)
        )
    target = result.scalar_one_or_none()
    if target is None:
        await message.answer(
            "⚠️ Utente non trovato. Deve aver interagito con il bot almeno una volta."
        )
        return

    try:
        await economy_service.transfer(
            db_session, message.from_user.id, target.tg_id, amount
        )
        await db_session.commit()
    except SelfTransferError:
        await message.answer("⚠️ Non puoi trasferire Aldueuri a te stesso.")
        return
    except InsufficientFundsError as e:
        await message.answer(
            f"⚠️ Saldo insufficiente: hai <b>{e.balance:,} 🪙</b>, servono <b>{e.required:,} 🪙</b>."
        )
        return
    except ValueError as e:
        await message.answer(f"⚠️ {e}")
        return
    except WalletNotFoundError:
        await message.answer("⚠️ Wallet non trovato.")
        return

    target_name = f"@{target.username}" if target.username else target.full_name
    newly_earned = await badge_service.check_and_award_milestones(
        db_session, message.from_user.id
    )
    if newly_earned:
        await db_session.commit()

    text = f"✅ Trasferiti <b>{amount:,} 🪙</b> a {target_name}!"
    if newly_earned:
        badges_text = ", ".join(f"{b.icon_emoji} {b.name}" for b in newly_earned)
        text += f"\n\n🏅 <b>Badge sbloccato:</b> {badges_text}!"
    await message.answer(text)


@router.message(Command("credita"), IsAdminFilter())
async def cmd_credita(message: Message, db_session: AsyncSession) -> None:
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("ℹ️ Uso: /credita @username &lt;importo&gt;")
        return

    target_ref = parts[1].lstrip("@")
    try:
        amount = int(parts[2])
    except ValueError:
        await message.answer("⚠️ L'importo deve essere un numero intero.")
        return

    if amount <= 0 or amount > 10_000_000:
        await message.answer("⚠️ Importo non valido (1 – 10.000.000).")
        return

    if target_ref.isdigit():
        result = await db_session.execute(select(User).where(User.tg_id == int(target_ref)))
    else:
        result = await db_session.execute(select(User).where(User.username == target_ref))
    target = result.scalar_one_or_none()
    if target is None:
        await message.answer(
            "⚠️ Utente non trovato. Deve aver interagito con il bot almeno una volta."
        )
        return

    try:
        await economy_service.credit(
            db_session,
            target.tg_id,
            amount,
            TransactionType.admin_credit,
            f"Credito admin da #{message.from_user.id}",
        )
        await db_session.commit()
    except Exception as e:
        log.error("Admin credit failed: %s", e)
        await message.answer("⚠️ Operazione fallita.")
        return

    target_name = f"@{target.username}" if target.username else target.full_name
    await message.answer(
        f"✅ Accreditati <b>{amount:,} 🪙</b> a {target_name} (ID: <code>{target.tg_id}</code>)."
    )
