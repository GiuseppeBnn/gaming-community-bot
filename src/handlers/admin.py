"""
Admin toolset — currency management, Telegram moderation, user dossiers/stats,
a warn/strike system, and the shared dashboard renderers.

Text commands (fast, target via reply/@mention/id) handle all actions and are
gated by IsAdminFilter. The button-driven /admin dashboard lives in
handlers/admin_dashboard.py and reuses the renderers exposed here
(render_stats / render_leaderboard / render_audit / render_panel_help).

All mutating actions are recorded in the admin audit log (admin_service.log_action).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters.command import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import TransactionType
from exceptions.economy import InsufficientFundsError, WalletNotFoundError
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers._privacy import redirect_to_private
from handlers._targeting import ResolvedTarget, resolve_target
from middlewares import ban_guard

# Admin commands that must never answer in the group (data exposure / noise):
# in a group they redirect the admin to the private dashboard.
_ADMIN_PRIVATE_NOTICE = "🔒 Comando riservato agli admin: usalo in chat privata col bot."
from services import admin_service, economy_service, group_registry, moderation_service, xp_service
from services.xp_service import XpSource
from utils.text import esc

log = logging.getLogger(__name__)
router = Router()
# 100%-admin router: gate every message/callback at the root, so no handler can
# ever be reached by a non-admin even if a per-handler filter is forgotten. The
# only authority is is_admin = env owner (admin_ids) OR current Telegram group
# admin/creator; everyone else is denied (STEERING §8).
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminCallbackFilter())

_GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)

_AUDIT_LABELS = {
    "credita": "💰 Credito",
    "addebita": "🔻 Addebito",
    "setsaldo": "⚖️ Set saldo",
    "airdrop": "🎁 Airdrop",
    "ban": "⛔ Ban",
    "sban": "✅ Sban",
    "kick": "👢 Kick",
    "mute": "🔇 Mute",
    "unmute": "🔊 Unmute",
    "warn": "⚠️ Warn",
    "unwarn": "♻️ Unwarn",
    "xp_grant": "⚡ XP +",
    "xp_set": "⚡ Set XP",
    "xp_airdrop": "⚡ Airdrop XP",
}

_COIN_AUDIT = ("credita", "addebita", "airdrop", "setsaldo")
_XP_AUDIT = ("xp_grant", "xp_set", "xp_airdrop")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mod_chat_id(message: Message) -> int | None:
    """The chat to moderate: the current group, or the effective GROUP_ID."""
    if message.chat.type in _GROUP_TYPES:
        return message.chat.id
    return group_registry.get_group_id() or None


async def _notify(message: Message, tg_id: int, text: str) -> None:
    """Best-effort private notification to the target user."""
    try:
        await message.bot.send_message(tg_id, text)
    except Exception:  # noqa: BLE001
        pass


def _split_amount(remainder: str) -> tuple[int | None, str]:
    parts = remainder.split(maxsplit=1)
    if not parts:
        return None, ""
    try:
        amount = int(parts[0])
    except ValueError:
        return None, remainder
    return amount, (parts[1].strip() if len(parts) > 1 else "")


async def _resolve_or_warn(
    message: Message, db_session: AsyncSession, command: CommandObject
) -> ResolvedTarget | None:
    target = await resolve_target(message, db_session, command.args)
    if target is None or target.tg_id is None:
        await message.reply(
            "🎯 Specifica un utente: rispondi a un suo messaggio, oppure usa "
            "<code>@username</code> o l'ID numerico."
        )
        return None
    return target


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

@router.message(Command("addebita"), IsAdminFilter())
async def cmd_addebita(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    if target.user is None:
        await message.reply("⚠️ L'utente non è registrato nel bot (nessun wallet).")
        return

    amount, reason = _split_amount(target.remainder)
    if amount is None or amount <= 0:
        await message.reply("ℹ️ Uso: <code>/addebita @user &lt;importo&gt; [motivo]</code>")
        return

    try:
        await economy_service.debit(
            db_session, target.tg_id, amount,
            TransactionType.admin_debit,
            reason or f"Addebito admin #{message.from_user.id}",
        )
    except InsufficientFundsError as e:
        await message.reply(
            f"⚠️ {target.display_name} ha solo <b>{e.balance:,} 🪙</b> (richiesti {e.required:,})."
        )
        return

    await admin_service.log_action(
        db_session, message.from_user.id, "addebita",
        target_tg_id=target.tg_id, amount=-amount, detail=reason or None,
    )
    await db_session.commit()
    await message.reply(f"🔻 Addebitati <b>{amount:,} 🪙</b> a {target.display_name}.")


@router.message(Command("setsaldo"), IsAdminFilter())
async def cmd_setsaldo(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    if target.user is None:
        await message.reply("⚠️ L'utente non è registrato nel bot (nessun wallet).")
        return

    amount, _ = _split_amount(target.remainder)
    if amount is None or amount < 0:
        await message.reply("ℹ️ Uso: <code>/setsaldo @user &lt;importo&gt;</code>")
        return

    try:
        old, new = await admin_service.set_balance(db_session, target.tg_id, amount)
    except (WalletNotFoundError, ValueError) as e:
        await message.reply(f"⚠️ {e}")
        return

    await admin_service.log_action(
        db_session, message.from_user.id, "setsaldo",
        target_tg_id=target.tg_id, amount=new, detail=f"{old} → {new}",
    )
    await db_session.commit()
    await message.reply(
        f"⚖️ Saldo di {target.display_name}: <b>{old:,}</b> → <b>{new:,} 🪙</b>."
    )


@router.message(Command("dai_xp"), IsAdminFilter())
async def cmd_dai_xp(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    if target.user is None:
        await message.reply("⚠️ L'utente non è registrato nel bot.")
        return
    amount, _ = _split_amount(target.remainder)
    if amount is None or amount <= 0:
        await message.reply("ℹ️ Uso: <code>/dai_xp @user &lt;xp&gt;</code>")
        return

    res = await xp_service.grant_xp(
        db_session, target.tg_id, amount, XpSource.admin_grant, capped=False
    )
    await admin_service.log_action(
        db_session, message.from_user.id, "xp_grant", target_tg_id=target.tg_id, amount=amount
    )
    await db_session.commit()
    extra = f" (nuovo rango: {esc(res.new_rank.name)})" if res.new_rank else ""
    await message.reply(f"⚡ Assegnati <b>+{res.granted:,} XP</b> a {target.display_name}{extra}.")


@router.message(Command("set_xp"), IsAdminFilter())
async def cmd_set_xp(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    if target.user is None:
        await message.reply("⚠️ L'utente non è registrato nel bot.")
        return
    amount, _ = _split_amount(target.remainder)
    if amount is None or amount < 0:
        await message.reply("ℹ️ Uso: <code>/set_xp @user &lt;xp&gt;</code>")
        return

    new_xp = await xp_service.set_xp(db_session, target.tg_id, amount)
    await admin_service.log_action(
        db_session, message.from_user.id, "xp_set", target_tg_id=target.tg_id, amount=new_xp
    )
    await db_session.commit()
    await message.reply(f"⚡ XP di {target.display_name} impostati a <b>{new_xp:,}</b>.")


@router.message(Command("airdrop"), IsAdminFilter())
async def cmd_airdrop(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    amount, reason = _split_amount((command.args or "").strip())
    if amount is None or amount <= 0:
        await message.reply("ℹ️ Uso: <code>/airdrop &lt;importo&gt; [motivo]</code>")
        return

    description = reason or "Airdrop admin"
    count = await admin_service.mass_credit(db_session, amount, description)
    await admin_service.log_action(
        db_session, message.from_user.id, "airdrop", amount=amount,
        detail=f"{count} utenti · {description}"[:512],
    )
    await db_session.commit()
    await message.reply(
        f"🎁 <b>Airdrop!</b> Accreditati <b>{amount:,} 🪙</b> a <b>{count}</b> utenti."
    )


@router.message(Command("saldo_di"), IsAdminFilter())
async def cmd_saldo_di(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    if target.user is None:
        await message.reply("⚠️ L'utente non è registrato nel bot.")
        return
    dossier = await admin_service.get_dossier(db_session, target.tg_id)
    await message.reply(
        f"🪙 Saldo di {target.display_name}: <b>{dossier.coins:,} 🪙</b>"
    )


# ---------------------------------------------------------------------------
# Moderation
# ---------------------------------------------------------------------------

async def _guard_mod_target(message: Message, target: ResolvedTarget) -> int | None:
    """Validate the moderation target/chat. Returns chat_id or None (already replied)."""
    if target.tg_id == message.from_user.id:
        await message.reply("⚠️ Non puoi usare questo comando su te stesso.")
        return None
    if target.tg_id == message.bot.id:
        await message.reply("⚠️ Non posso moderare me stesso. 🤖")
        return None
    chat_id = _mod_chat_id(message)
    if chat_id is None:
        await message.reply("⚠️ GROUP_ID non configurato: usa il comando nel gruppo.")
        return None
    return chat_id


@router.message(Command("ban"), IsAdminFilter())
async def cmd_ban(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    chat_id = await _guard_mod_target(message, target)
    if chat_id is None:
        return

    reason = target.remainder.strip()
    ok, err = await moderation_service.ban(message.bot, chat_id, target.tg_id)
    if not ok:
        await message.reply(f"❌ {err}")
        return

    # Bot-level ban too: the user becomes mute-to-the-bot everywhere (silent drop).
    await admin_service.set_user_banned(db_session, target.tg_id, True)
    await admin_service.log_action(
        db_session, message.from_user.id, "ban",
        target_tg_id=target.tg_id, group_id=chat_id, detail=reason or None,
    )
    await db_session.commit()
    ban_guard.invalidate(target.tg_id)
    await _notify(message, target.tg_id, "⛔ Sei stato bannato dal gruppo.")
    suffix = f"\n📝 Motivo: <i>{esc(reason)}</i>" if reason else ""
    await message.reply(f"⛔ {target.display_name} bannato.{suffix}")


@router.message(Command("sban"), IsAdminFilter())
async def cmd_sban(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    chat_id = _mod_chat_id(message)
    if chat_id is None:
        await message.reply("⚠️ GROUP_ID non configurato: usa il comando nel gruppo.")
        return

    ok, err = await moderation_service.unban(message.bot, chat_id, target.tg_id)
    if not ok:
        await message.reply(f"❌ {err}")
        return
    # Lift the bot-level ban too: the user can talk to the bot again.
    await admin_service.set_user_banned(db_session, target.tg_id, False)
    await admin_service.log_action(
        db_session, message.from_user.id, "sban", target_tg_id=target.tg_id, group_id=chat_id
    )
    await db_session.commit()
    ban_guard.invalidate(target.tg_id)
    await message.reply(f"✅ {target.display_name} può rientrare nel gruppo.")


@router.message(Command("kick"), IsAdminFilter())
async def cmd_kick(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    chat_id = await _guard_mod_target(message, target)
    if chat_id is None:
        return

    reason = target.remainder.strip()
    ok, err = await moderation_service.kick(message.bot, chat_id, target.tg_id)
    if not ok:
        await message.reply(f"❌ {err}")
        return
    await admin_service.log_action(
        db_session, message.from_user.id, "kick",
        target_tg_id=target.tg_id, group_id=chat_id, detail=reason or None,
    )
    await db_session.commit()
    await _notify(message, target.tg_id, "👢 Sei stato espulso dal gruppo (puoi rientrare).")
    await message.reply(f"👢 {target.display_name} espulso.")


@router.message(Command("mute"), IsAdminFilter())
async def cmd_mute(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    chat_id = await _guard_mod_target(message, target)
    if chat_id is None:
        return

    # remainder = "[durata] [motivo]" — first token may be a duration like 10m.
    parts = target.remainder.split(maxsplit=1)
    duration_token = parts[0] if parts else None
    if moderation_service.looks_like_duration(duration_token):
        duration = moderation_service.parse_duration(duration_token)
        reason = parts[1].strip() if len(parts) > 1 else ""
    else:
        duration = moderation_service.parse_duration(None)  # default
        reason = target.remainder.strip()

    ok, err = await moderation_service.mute(message.bot, chat_id, target.tg_id, duration)
    if not ok:
        await message.reply(f"❌ {err}")
        return

    mins = duration // 60
    await admin_service.log_action(
        db_session, message.from_user.id, "mute",
        target_tg_id=target.tg_id, group_id=chat_id, amount=duration, detail=reason or None,
    )
    await db_session.commit()
    await _notify(message, target.tg_id, f"🔇 Sei stato silenziato per {mins} minuti.")
    await message.reply(f"🔇 {target.display_name} silenziato per <b>{mins} min</b>.")


@router.message(Command("unmute"), IsAdminFilter())
async def cmd_unmute(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    chat_id = _mod_chat_id(message)
    if chat_id is None:
        await message.reply("⚠️ GROUP_ID non configurato: usa il comando nel gruppo.")
        return

    ok, err = await moderation_service.unmute(message.bot, chat_id, target.tg_id)
    if not ok:
        await message.reply(f"❌ {err}")
        return
    await admin_service.log_action(
        db_session, message.from_user.id, "unmute", target_tg_id=target.tg_id, group_id=chat_id
    )
    await db_session.commit()
    await _notify(message, target.tg_id, "🔊 Puoi di nuovo scrivere nel gruppo.")
    await message.reply(f"🔊 {target.display_name} può di nuovo scrivere.")


# ---------------------------------------------------------------------------
# Warn / strike
# ---------------------------------------------------------------------------

async def apply_warning(
    bot, db_session: AsyncSession, admin_id: int, target_id: int, chat_id: int, reason: str | None
) -> tuple[int, str]:
    """Issue a warn, log it, and apply threshold escalation (auto mute/ban).

    Returns (active_warn_count, escalation_html). Does NOT commit — the caller owns
    the transaction. Shared by the /warn command and the admin dashboard so both
    paths behave identically (including the audit trail).
    """
    count = await admin_service.add_warning(db_session, target_id, chat_id, admin_id, reason or None)
    await admin_service.log_action(
        db_session, admin_id, "warn",
        target_tg_id=target_id, group_id=chat_id, amount=count, detail=reason or None,
    )

    escalation = ""
    if count >= settings.warn_ban_threshold:
        ok, _ = await moderation_service.ban(bot, chat_id, target_id)
        if ok:
            # Bot-level ban too (caller invalidates the cache after commit).
            await admin_service.set_user_banned(db_session, target_id, True)
            await admin_service.log_action(
                db_session, admin_id, "ban",
                target_tg_id=target_id, group_id=chat_id, detail="auto: soglia warn",
            )
            escalation = "\n⛔ Soglia raggiunta → <b>BAN automatico</b>."
    elif count >= settings.warn_mute_threshold:
        ok, _ = await moderation_service.mute(
            bot, chat_id, target_id, settings.warn_mute_duration_seconds
        )
        if ok:
            mins = settings.warn_mute_duration_seconds // 60
            await admin_service.log_action(
                db_session, admin_id, "mute",
                target_tg_id=target_id, group_id=chat_id,
                amount=settings.warn_mute_duration_seconds, detail="auto: soglia warn",
            )
            escalation = f"\n🔇 Soglia raggiunta → <b>mute automatico {mins} min</b>."
    return count, escalation


@router.message(Command("warn"), IsAdminFilter())
async def cmd_warn(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    chat_id = await _guard_mod_target(message, target)
    if chat_id is None:
        return

    reason = target.remainder.strip()
    count, escalation = await apply_warning(
        message.bot, db_session, message.from_user.id, target.tg_id, chat_id, reason or None
    )

    await db_session.commit()
    # A warn may have auto-banned the user → refresh the ban-guard cache.
    ban_guard.invalidate(target.tg_id)
    suffix = f"\n📝 Motivo: <i>{esc(reason)}</i>" if reason else ""
    await _notify(
        message, target.tg_id,
        f"⚠️ Hai ricevuto un warn ({count}/{settings.warn_ban_threshold}).{suffix}",
    )
    await message.reply(
        f"⚠️ Warn a {target.display_name}: <b>{count}/{settings.warn_ban_threshold}</b>.{suffix}{escalation}"
    )


@router.message(Command("warns"), IsAdminFilter())
async def cmd_warns(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    warns = await admin_service.active_warnings(db_session, target.tg_id)
    if not warns:
        await message.reply(f"✅ {target.display_name} non ha warn attivi.")
        return
    lines = [f"⚠️ <b>Warn attivi di {target.display_name}: {len(warns)}</b>\n"]
    for i, w in enumerate(warns, 1):
        when = w.created_at.strftime("%d/%m %H:%M")
        reason = f" — <i>{esc(w.reason)}</i>" if w.reason else ""
        lines.append(f"{i}. {when}{reason}")
    await message.reply("\n".join(lines))


@router.message(Command("unwarn"), IsAdminFilter())
async def cmd_unwarn(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    cleared = await admin_service.clear_warnings(db_session, target.tg_id, count=1)
    if cleared == 0:
        await message.reply(f"ℹ️ {target.display_name} non ha warn attivi.")
        return
    remaining = await admin_service.active_warning_count(db_session, target.tg_id)
    await admin_service.log_action(
        db_session, message.from_user.id, "unwarn", target_tg_id=target.tg_id, amount=remaining
    )
    await db_session.commit()
    await message.reply(f"♻️ Rimosso 1 warn a {target.display_name} (restano {remaining}).")


# ---------------------------------------------------------------------------
# Dossier / search / stats
# ---------------------------------------------------------------------------

@router.message(Command("info"), IsAdminFilter())
async def cmd_info(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    target = await _resolve_or_warn(message, db_session, command)
    if target is None:
        return
    dossier = await admin_service.get_dossier(db_session, target.tg_id)
    if dossier is None:
        await message.reply("⚠️ Utente non registrato nel bot.")
        return

    u = dossier.user
    username = f"@{esc(u.username)}" if u.username else "N/D"
    member_since = u.created_at.strftime("%d/%m/%Y")

    # Live group status (best-effort)
    status_line = ""
    chat_id = _mod_chat_id(message)
    if chat_id is not None:
        try:
            member = await message.bot.get_chat_member(chat_id, target.tg_id)
            status_map = {
                "creator": "👑 Creatore", "administrator": "🛡️ Admin",
                "member": "✅ Membro", "restricted": "🔇 Limitato",
                "left": "🚪 Uscito", "kicked": "⛔ Bannato",
            }
            status_line = f"\n👥 <b>Stato gruppo:</b> {status_map.get(member.status, member.status)}"
        except Exception:  # noqa: BLE001
            pass

    await message.reply(
        f"🪪 <b>{esc(u.full_name)}</b>\n\n"
        f"🔖 Username: {username}\n"
        f"🆔 ID: <code>{u.tg_id}</code>\n"
        f"📅 Membro dal: {member_since}\n"
        f"💰 Saldo: <b>{dossier.coins:,} 🪙</b>\n"
        f"⚡ XP: {u.xp}\n"
        f"🎖️ Badge: {dossier.badge_count}\n"
        f"🎲 Scommesse: {dossier.bet_count} (vinte: {u.bets_won})\n"
        f"🔥 Streak: {u.daily_streak}\n"
        f"⚠️ Warn attivi: <b>{dossier.active_warnings}</b>"
        f"{status_line}"
    )


@router.message(Command("cerca"), IsAdminFilter())
async def cmd_cerca(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "admin", "🛠️ Apri il pannello", notice=_ADMIN_PRIVATE_NOTICE):
        return
    query = (command.args or "").strip()
    if len(query) < 2:
        await message.reply("ℹ️ Uso: <code>/cerca &lt;testo&gt;</code> (almeno 2 caratteri).")
        return
    users = await admin_service.search_users(db_session, query)
    if not users:
        await message.reply(f"🔍 Nessun utente trovato per «{esc(query)}».")
        return
    lines = [f"🔍 <b>Risultati per «{esc(query)}»: {len(users)}</b>\n"]
    for u in users:
        username = f"@{esc(u.username)}" if u.username else "—"
        lines.append(f"• {esc(u.full_name)} ({username}) · <code>{u.tg_id}</code>")
    await message.reply("\n".join(lines))


@router.message(Command("classifica"), IsAdminFilter())
async def cmd_classifica(message: Message, db_session: AsyncSession) -> None:
    await message.reply(await render_leaderboard(db_session))


@router.message(Command("stats"), IsAdminFilter())
async def cmd_stats(message: Message, db_session: AsyncSession) -> None:
    await message.reply(await render_stats(db_session))


@router.message(Command("audit"), IsAdminFilter())
async def cmd_audit(message: Message, command: CommandObject, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "admin", "🛠️ Apri il pannello", notice=_ADMIN_PRIVATE_NOTICE):
        return
    target = await resolve_target(message, db_session, command.args)
    target_tg_id = target.tg_id if target else None
    await message.reply(await render_audit(db_session, target_tg_id))


# ---------------------------------------------------------------------------
# Renderers (shared by text commands and the /admin panel)
# ---------------------------------------------------------------------------

async def render_stats(db_session: AsyncSession) -> str:
    s = await admin_service.economy_stats(db_session)
    return (
        "📊 <b>Statistiche gruppo</b>\n\n"
        f"👥 Utenti registrati: <b>{s.total_users:,}</b>\n"
        f"💰 Aldueuri in circolazione: <b>{s.total_coins:,} 🪙</b>\n"
        f"📈 Media per utente: <b>{s.avg_coins:,} 🪙</b>\n"
        f"🎲 Scommesse attive: <b>{s.active_events}</b>\n"
        f"⚠️ Warn attivi: <b>{s.active_warnings}</b>"
    )


async def render_leaderboard(db_session: AsyncSession) -> str:
    rows = await admin_service.leaderboard(db_session)
    if not rows:
        return "🏆 <b>Classifica</b>\n\n<i>Nessun utente.</i>"
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Classifica ricchezza</b>\n"]
    for i, (user, coins) in enumerate(rows):
        rank = medals[i] if i < 3 else f"{i + 1}."
        name = f"@{esc(user.username)}" if user.username else esc(user.full_name)
        lines.append(f"{rank} {name} — <b>{coins:,} 🪙</b>")
    return "\n".join(lines)


async def render_audit(db_session: AsyncSession, target_tg_id: int | None = None) -> str:
    actions = await admin_service.recent_actions(db_session, target_tg_id)
    if not actions:
        return "🧾 <b>Audit log</b>\n\n<i>Nessuna azione registrata.</i>"
    header = "🧾 <b>Ultime azioni admin</b>"
    if target_tg_id is not None:
        header += f" (target <code>{target_tg_id}</code>)"
    lines = [header + "\n"]
    for a in actions:
        label = _AUDIT_LABELS.get(a.action_type, a.action_type)
        when = a.created_at.strftime("%d/%m %H:%M")
        if a.amount is not None and a.action_type in _COIN_AUDIT:
            amt = f" · {a.amount:+,}🪙"
        elif a.amount is not None and a.action_type in _XP_AUDIT:
            amt = f" · {a.amount:+,} XP"
        else:
            amt = ""
        tgt = f" → <code>{a.target_tg_id}</code>" if a.target_tg_id else ""
        detail = f" <i>{esc(a.detail)}</i>" if a.detail else ""
        lines.append(f"{label}{tgt}{amt} · {when}{detail}")
    return "\n".join(lines)


def render_panel_help() -> str:
    return (
        "❓ <b>Comandi admin</b>\n\n"
        "💰 <b>Valuta</b>\n"
        "/credita /addebita @u &lt;n&gt; · /setsaldo @u &lt;n&gt;\n"
        "/airdrop &lt;n&gt; · /saldo_di @u\n\n"
        "⚡ <b>XP</b>\n"
        "/dai_xp @u &lt;n&gt; · /set_xp @u &lt;n&gt;\n\n"
        "🛡️ <b>Moderazione</b> (reply o @u/ID)\n"
        "/ban /sban /kick · /mute [10m] /unmute\n\n"
        "⚠️ <b>Warn</b>\n"
        "/warn [motivo] · /warns · /unwarn\n\n"
        "📊 <b>Info</b>\n"
        "/info /saldo_di · /cerca &lt;testo&gt; · /classifica · /stats · /audit"
    )


# The button-driven /admin dashboard lives in handlers/admin_dashboard.py and
# reuses the renderers above (render_stats / render_leaderboard / render_audit /
# render_panel_help). These text commands remain the fast path for power users.
