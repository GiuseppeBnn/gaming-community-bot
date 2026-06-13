"""
General-purpose handlers: /start (with deep-link dispatch), /profilo, /help.

Deep-link payloads routed through /start:
  create_bet            → opens the bet-creation FSM in private chat
  bet_custom_<e>_<o>   → opens the custom-amount FSM for event <e>, option <o>
  bet_<event_id>        → opens event detail in private chat
  help                  → shows the help message
  shop_<group_id>       → opens the shop catalog for the given group
  backup / esporta      → runs the chat archive / state export (admin only)
"""

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.filters.command import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config_data.config import settings
from database.models import User
from filters.admin_filter import is_admin as is_bot_admin
from handlers._privacy import redirect_to_private
from handlers.help_content import render_command, render_legend, suggestions
from handlers.onboarding import show_rules_prompt
from utils import cooldown
from utils.text import esc

router = Router()


async def _show_help(message: Message, is_admin: bool) -> None:
    # The legend lives in help_content (single source of truth shared with
    # /spiega_comando), so the two never drift apart.
    await message.answer(render_legend(is_admin))


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(
        select(User).where(User.tg_id == message.from_user.id)
    )
    user = result.scalar_one_or_none()

    if user is None or not user.onboarding_completed:
        # Group admins/owner are recognized via Telegram, not via the private
        # onboarding flow. Don't trap them behind the rules gate when they
        # switch to private for an admin entry point (e.g. ?start=create_quiz):
        # otherwise the admin recognition further down is never reached.
        if not await is_bot_admin(message.bot, message.from_user.id):
            await show_rules_prompt(message)
            return

    payload = command.args or ""

    # Deep-link: admin (admin tools dashboard)
    if payload == "admin":
        if await is_bot_admin(message.bot, message.from_user.id):
            from handlers.admin_dashboard import show_dashboard_home
            await show_dashboard_home(message, db_session)
        else:
            await message.answer("⛔ Accesso non autorizzato.")
        return

    # Deep-link: eventi (admin events hub)
    if payload == "eventi":
        if await is_bot_admin(message.bot, message.from_user.id):
            from handlers.events import show_hub
            await show_hub(message)
        else:
            await message.answer("⛔ Accesso non autorizzato.")
        return

    # Deep-link: create_quiz (admin quiz creation FSM)
    if payload == "create_quiz":
        if await is_bot_admin(message.bot, message.from_user.id):
            from handlers.quiz import start_quiz_creation
            await start_quiz_creation(message, state)
        else:
            await message.answer("⛔ Accesso non autorizzato.")
        return

    # Deep-link: quiz_<id> (join a running quiz in private)
    if payload.startswith("quiz_") and payload[5:].isdigit():
        from handlers.quiz import start_quiz_session
        await start_quiz_session(message, db_session, int(payload[5:]))
        return

    # Deep-link: programma (admin scheduling FSM)
    if payload == "programma":
        if await is_bot_admin(message.bot, message.from_user.id):
            from handlers.schedule import start_schedule_flow
            await start_schedule_flow(message, state)
        else:
            await message.answer("⛔ Accesso non autorizzato.")
        return

    # Deep-link: manage_bets (admin panel shortcut from group button)
    if payload == "manage_bets":
        if await is_bot_admin(message.bot, message.from_user.id):
            from handlers.admin_betting import _show_event_list
            await _show_event_list(message, db_session, edit=False)
        else:
            await message.answer("⛔ Accesso non autorizzato.")
        return

    # Deep-link: help
    if payload == "help":
        await _show_help(message, await is_bot_admin(message.bot, message.from_user.id))
        return

    # Deep-link: shop_<group_id>
    if payload.startswith("shop_") and payload[5:].lstrip("-").isdigit():
        group_id = int(payload[5:])
        from handlers.shop import start_shop_private
        await start_shop_private(message, state, group_id, db_session)
        return

    # Deep-links for personal data redirected from the group (privacy).
    if payload in ("saldo", "daily"):
        from handlers.economy import show_saldo
        await show_saldo(message, db_session)
        return
    if payload == "storico":
        from handlers.economy import show_storico
        await show_storico(message, db_session)
        return
    if payload == "profilo":
        await show_profilo(message, db_session)
        return
    if payload == "traguardi":
        from handlers.badges import show_traguardi
        await show_traguardi(message, db_session)
        return
    if payload == "classifiche":
        from handlers.leaderboard import show_board_private
        await show_board_private(message, db_session)
        return

    # Deep-links: admin backup / state export (redirected from the group)
    if payload in ("backup", "esporta"):
        if await is_bot_admin(message.bot, message.from_user.id):
            from handlers.backup import run_backup_now, run_export_now
            if payload == "backup":
                await run_backup_now(message, db_session)
            else:
                await run_export_now(message, db_session)
        else:
            await message.answer("⛔ Accesso non autorizzato.")
        return

    # Deep-link: create_bet
    if payload == "create_bet":
        from handlers.betting import start_bet_creation
        await start_bet_creation(message, state)
        return

    # Deep-link: bet_custom_<event_id>_<option_id>
    if payload.startswith("bet_custom_"):
        parts = payload.split("_")
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            from handlers.betting import start_custom_amount
            await start_custom_amount(message, state, int(parts[2]), int(parts[3]))
        else:
            await message.answer("⚠️ Link non valido.")
        return

    # Deep-link: bet_<event_id>
    if payload.startswith("bet_"):
        tail = payload[4:]
        if tail.isdigit():
            from handlers.betting import start_bet_view
            await start_bet_view(message, db_session, int(tail), state)
        else:
            await message.answer("⚠️ Link non valido.")
        return

    # Default: main menu
    name = esc(message.from_user.first_name)
    username_str = f" (@{esc(message.from_user.username)})" if message.from_user.username else ""
    await message.answer(
        f"🎮 <b>Bentornato, {name}{username_str}!</b>\n\n"
        f"/profilo — Il tuo profilo\n"
        f"/saldo — Saldo e movimenti\n"
        f"/daily — Premio giornaliero\n"
        f"/scommesse — Scommesse aperte\n"
        f"/traguardi — I tuoi badge\n"
        f"/help — Tutti i comandi"
    )


@router.message(Command("profilo"))
async def cmd_profilo(message: Message, db_session: AsyncSession) -> None:
    from handlers._privacy import redirect_to_private
    if await redirect_to_private(message, "profilo", "🎮 Vedi il tuo profilo"):
        return
    await show_profilo(message, db_session)


async def show_profilo(message: Message, db_session: AsyncSession) -> None:
    """Render the caller's profile (private chat only)."""
    result = await db_session.execute(
        select(User)
        .where(User.tg_id == message.from_user.id)
        .options(
            selectinload(User.wallet),
            selectinload(User.badges),
        )
    )
    user = result.scalar_one_or_none()

    if user is None or user.wallet is None:
        await message.answer("⚠️ Profilo non trovato. Usa /start per registrarti.")
        return

    username_display = f"@{esc(user.username)}" if user.username else "N/D"
    badge_count = len(user.badges)
    member_since = user.created_at.strftime("%d/%m/%Y")

    from services import xp_service
    from services.shop_service import render_active_tags
    rank = xp_service.rank_for_xp(user.xp)
    rank_line = f"🎖️ <b>Rango:</b> {rank.emoji} {esc(rank.name)}\n" if rank else ""
    tags = render_active_tags(user)
    tag_line = f"🏷️ <b>Tag:</b> {esc(tags)}\n" if tags else ""
    title = esc(user.full_name)
    if tags:
        title = f"{esc(tags)} · {title}"

    await message.answer(
        f"🎮 <b>{title}</b>\n\n"
        f"🔖 <b>Username:</b> {username_display}\n"
        f"🆔 <b>Telegram ID:</b> <code>{user.tg_id}</code>\n"
        f"📅 <b>Membro dal:</b> {member_since}\n\n"
        f"{tag_line}"
        f"{rank_line}"
        f"💰 <b>Aldueuri:</b> <b>{user.wallet.coins:,} 🪙</b>\n"
        f"⚡ <b>XP:</b> {user.xp:,}\n"
        f"🏆 <b>Trofei:</b> {badge_count}"
    )


# /comandi is the canonical name; /help is kept as a hidden back-compat alias.
@router.message(Command("comandi", "help"))
async def cmd_help(message: Message) -> None:
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        bot_info = await message.bot.get_me()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="📖 Apri la guida in privato",
                    url=f"https://t.me/{bot_info.username}?start=help",
                )
            ]]
        )
        await message.reply(
            "ℹ️ Clicca per vedere tutti i comandi disponibili.",
            reply_markup=kb,
        )
        return

    await _show_help(message, await is_bot_admin(message.bot, message.from_user.id))


@router.message(Command("spiega_comando"))
async def cmd_spiega_comando(message: Message, command: CommandObject) -> None:
    # Detailed per-command manual — private only (keeps the group uncluttered and
    # never reveals the admin command set there).
    if await redirect_to_private(
        message, "comandi", "📖 Apri la guida",
        notice="📖 La guida dettagliata si apre in chat privata.",
    ):
        return
    if not await cooldown.guard(message, "spiega", settings.command_cooldown_seconds):
        return

    is_admin = await is_bot_admin(message.bot, message.from_user.id)
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(
            "📘 <b>Spiega comando</b>\n\n"
            "Uso: <code>/spiega_comando &lt;comando&gt;</code>\n"
            "Esempio: <code>/spiega_comando daily</code>.\n\n"
            "Per l'elenco completo usa /comandi."
        )
        return

    page = render_command(arg, is_admin)
    if page is None:
        hint = ""
        near = [s for s in suggestions(arg) if render_command(s, is_admin)]
        if near:
            hint = "\n\nForse cercavi: " + " · ".join(f"<code>/{s}</code>" for s in near)
        await message.answer(
            f"❓ Comando «{esc(arg, 32)}» non trovato. Usa /comandi per l'elenco.{hint}"
        )
        return
    await message.answer(page)
