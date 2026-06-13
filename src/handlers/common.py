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

from database.models import User
from filters.admin_filter import is_admin as is_bot_admin
from handlers.onboarding import show_rules_prompt
from utils.text import esc

router = Router()


def _build_help_text(is_admin: bool = False) -> str:
    user_section = (
        "📖 <b>Comandi disponibili</b>\n\n"
        "👤 <b>Profilo & Economia</b>\n"
        "/start — Menu principale\n"
        "/profilo — Il tuo profilo (XP, saldo, badge)\n"
        "/saldo — Saldo e ultime transazioni\n"
        "/storico — Cronologia completa movimenti\n"
        "/daily — Premio giornaliero (ogni 20h)\n"
        "/trasferisci — Trasferisci Aldueuri a un utente\n"
        "\n"
        "🎲 <b>Scommesse</b>\n"
        "/scommesse — Vedi le scommesse aperte\n"
        "/crea_scommessa — Crea una nuova scommessa\n"
        "\n"
        "🏆 <b>Progressione</b>\n"
        "/traguardi — I tuoi trofei (per rarità) + rango\n"
        "/catalogo_badge — Tutti i trofei disponibili\n"
        "/classifiche — Classifiche: ricchezza · XP · trofei\n"
        "\n"
        "🛒 <b>Negozio</b>\n"
        "/negozio — Compra personalizzazioni (tag) con gli Aldueuri\n"
        "\n"
        "🤖 <b>Intrattenimento AI</b> <i>(nel gruppo, in risposta a un messaggio)</i>\n"
        "/maestro — Trasforma uno sfogo in filosofia aulica\n"
        "/complotto — Genera una teoria del complotto sul messaggio\n"
        "/difendi — Un avvocato senza scrupoli difende il messaggio\n"
        "/accusa — Un inquisitore condanna il messaggio\n"
        "/drama — Riscrive il messaggio come un climax anime drammatico\n"
        "/dialetto — Traduce il messaggio in siciliano grezzo\n"
        "/insulta @utente — Blasta senza pietà l'utente taggato\n"
        "\n"
        "❓ <b>Aiuto</b>\n"
        "/help — Questo messaggio"
    )
    if not is_admin:
        return user_section

    admin_section = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 <b>Comandi Admin</b>\n\n"
        "/admin — Pannello admin (stats · classifica · audit)\n"
        "/gestisci_scommesse — Pannello gestione scommesse\n\n"
        "💰 <b>Valuta</b>\n"
        "/credita · /addebita @u &lt;n&gt; · /setsaldo @u &lt;n&gt;\n"
        "/airdrop &lt;n&gt; · /saldo_di @u\n\n"
        "⚡ <b>XP</b>\n"
        "/dai_xp @u &lt;n&gt; · /set_xp @u &lt;n&gt;\n\n"
        "🛡️ <b>Moderazione</b> (reply o @u/ID)\n"
        "/ban · /sban · /kick · /mute [10m] · /unmute\n"
        "/warn [motivo] · /warns · /unwarn\n\n"
        "📊 <b>Info</b>\n"
        "/info · /cerca &lt;testo&gt; · /classifica · /stats · /audit\n\n"
        "🧠 <b>Quiz & Eventi</b>\n"
        "/crea_quiz · /quiz · /avvia_quiz &lt;id&gt; · /chiudi_quiz &lt;id&gt;\n"
        "/sondaggio · /programma · /programmati"
    )
    return user_section + admin_section


async def _show_help(message: Message, is_admin: bool) -> None:
    await message.answer(_build_help_text(is_admin))


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
    rank = xp_service.rank_for_xp(user.xp)
    rank_line = f"🎖️ <b>Rango:</b> {rank.emoji} {esc(rank.name)}\n" if rank else ""
    tag_line = f"🏷️ <b>Tag:</b> {esc(user.cosmetic_tag)}\n" if user.cosmetic_tag else ""
    title = esc(user.full_name)
    if user.cosmetic_tag:
        title = f"{esc(user.cosmetic_tag)} · {title}"

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


@router.message(Command("help"))
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
