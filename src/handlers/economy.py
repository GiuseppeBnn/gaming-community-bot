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
from aiogram.enums import ChatType

from filters.admin_filter import IsAdminFilter
from handlers._privacy import redirect_to_private
from services import badge_service, economy_service, xp_service
from services.xp_service import XpSource
from utils import cooldown
from utils.static_reply import reply_static
from utils.text import esc, format_duration

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


async def show_saldo(message: Message, db_session: AsyncSession) -> None:
    """Render the caller's balance. Public: works in the group (one live reply per
    user via static_reply) and in private; also reused by the ``?start=saldo`` /
    ``?start=daily`` deep-links."""
    try:
        balance = await economy_service.get_balance(db_session, message.from_user.id)
    except WalletNotFoundError:
        await message.answer("⚠️ Wallet non trovato. Usa /start per registrarti.")
        return
    await reply_static(message, f"🪙 Il tuo saldo: <b>{balance:,} Aldueuri</b>", "saldo")


@router.message(Command("saldo"))
async def cmd_saldo(message: Message, db_session: AsyncSession) -> None:
    # Public command: answers in the group too (no longer redirected to private).
    if not await cooldown.ready(message, "saldo", settings.command_cooldown_seconds):
        return
    await show_saldo(message, db_session)


async def show_storico(message: Message, db_session: AsyncSession) -> None:
    """Render the caller's transaction history (private chat only)."""
    entries = await economy_service.get_history(db_session, message.from_user.id)
    if not entries:
        await message.answer("📋 Nessuna transazione ancora.")
        return

    _TRANSFER_TYPES = (
        TransactionType.transfer_out.value,
        TransactionType.transfer_in.value,
    )
    lines = ["<b>📋 Ultime transazioni:</b>\n"]
    for entry in entries:
        label = _TX_LABELS.get(entry.tx_type, "📌 Transazione")
        sign = "+" if entry.amount > 0 else ""
        ts = entry.created_at
        # For transfers the counterparty lives in the description (e.g.
        # "Trasferimento a @mario") — surface the name so the history is readable.
        detail = ""
        if entry.tx_type in _TRANSFER_TYPES and entry.description:
            detail = f"\n   <i>{esc(entry.description)}</i>"
        lines.append(
            f"{label}\n"
            f"   <b>{sign}{entry.amount:,} 🪙</b>"
            f" — <i>{ts.strftime('%d/%m %H:%M')}</i>"
            f"{detail}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("storico"))
async def cmd_storico(message: Message, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "storico", "📋 Vedi lo storico"):
        return
    await show_storico(message, db_session)


@router.message(Command("daily"))
async def cmd_daily(message: Message, db_session: AsyncSession) -> None:
    try:
        reward, streak = await economy_service.claim_daily(db_session, message.from_user.id)
        await db_session.commit()
    except DailyAlreadyClaimedError as e:
        await message.reply(
            f"⏰ Hai già riscosso il premio oggi.\n"
            f"Riprova tra <b>{format_duration(e.seconds_remaining)}</b>."
        )
        return
    except WalletNotFoundError:
        await message.reply("⚠️ Wallet non trovato. Usa /start per registrarti.")
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
            f"\n\n{xp_res.new_rank.emoji} <b>Nuovo rango:</b> {esc(xp_res.new_rank.name)}!"
        )
    if newly_earned:
        badges_text = ", ".join(f"{b.icon_emoji} {esc(b.name)}" for b in newly_earned)
        text += f"\n\n🏅 <b>Trofeo sbloccato:</b> {badges_text}!"

    # In private: show everything. In group: claim succeeded, but the streak/XP/
    # rank details are personal → minimal public ack + best-effort DM of the rest.
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(text)
        return

    dm_ok = True
    try:
        await message.bot.send_message(message.from_user.id, text)
    except Exception:  # noqa: BLE001 — user may have never opened the bot
        dm_ok = False
    if dm_ok:
        await message.reply("✅ Premio giornaliero riscosso! Ti ho mandato i dettagli in privato. 📩")
    else:
        bot_info = await message.bot.get_me()
        await message.reply(
            "✅ Premio giornaliero riscosso! Apri la chat privata col bot per i dettagli: "
            f"https://t.me/{bot_info.username}?start=daily"
        )


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

    sender = message.from_user
    sender_name = f"@{sender.username}" if sender.username else sender.full_name
    recipient_name = f"@{target.username}" if target.username else target.full_name
    try:
        await economy_service.transfer(
            db_session, message.from_user.id, target.tg_id, amount,
            from_name=sender_name, to_name=recipient_name,
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

    target_name = f"@{esc(target.username)}" if target.username else esc(target.full_name)
    newly_earned = await badge_service.check_and_award_milestones(
        db_session, message.from_user.id
    )
    if newly_earned:
        await db_session.commit()

    text = f"✅ Trasferiti <b>{amount:,} 🪙</b> a {target_name}!"
    if newly_earned:
        badges_text = ", ".join(f"{b.icon_emoji} {esc(b.name)}" for b in newly_earned)
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

    target_name = f"@{esc(target.username)}" if target.username else esc(target.full_name)
    await message.answer(
        f"✅ Accreditati <b>{amount:,} 🪙</b> a {target_name} (ID: <code>{target.tg_id}</code>)."
    )
