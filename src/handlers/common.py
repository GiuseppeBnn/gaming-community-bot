"""
General-purpose handlers: /start (with deep-link dispatch), /profilo, /help.

Deep-link payloads routed through /start:
  create_bet            → opens the bet-creation FSM in private chat
  create_poll           → opens the poll-creation FSM in private chat (admin only)
  bet_custom_<e>_<o>   → opens the custom-amount FSM for event <e>, option <o>
  bet_<event_id>        → opens event detail in private chat
  guess_<id> / sound_<id> → plays a Guess The Game / Sound Quest round in private
  help                  → shows the help message
  spiega_<cmd>          → shows the per-command manual (carried from the group button)
  shop_<group_id>       → opens the Locanda (shop) catalog for the given group
  backup / esporta      → runs the chat archive / state export (admin only)

Every admin entry point re-checks ``is_admin`` here: this router is public, so the
deep-link landing must not trust that the caller passed the originating command's
admin filter (a non-admin could craft the ?start=… link directly).
"""

import logging
import re

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.filters.command import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config_data.config import settings
from database.models import User
from filters.admin_filter import is_admin as is_bot_admin
from handlers._privacy import redirect_to_private
from handlers.help_content import normalize, render_command_or_hint, render_legend
from handlers.onboarding import show_rules_prompt
from utils import cooldown
from utils.static_reply import reply_static
from utils.text import esc

router = Router()

log = logging.getLogger(__name__)

#: Risposta per una callback che nessuno ha rivendicato. Corta: esce come toast.
_UNHANDLED_CALLBACK = "Questo bottone non è più valido."


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
            # Rules acceptance is strictly personal and must happen in PRIVATE.
            # In a group the accept button is a single shared message that any
            # other member could tap, completing THEIR onboarding (and grabbing
            # the welcome trophy) in place of the user who ran /start. So in the
            # group we only hand out a deep-link; the rules prompt is shown in
            # private, where the "onboarding" payload re-enters this same gate.
            if message.chat.type != ChatType.PRIVATE:
                await redirect_to_private(
                    message,
                    "onboarding",
                    button_text="🎮 Accetta le regole in privato",
                    notice="🎮 Per entrare nell'Arena leggi e accetta le regole "
                    "qui con me in privato.",
                )
            else:
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

    # Deep-link: guess_<id> / sound_<id> (play a running round in private).
    # Public by design: any group member plays, so unlike the admin landings
    # there is no is_admin re-check here to forget.
    for kind in ("guess", "sound"):
        prefix = f"{kind}_"
        if payload.startswith(prefix) and payload[len(prefix):].isdigit():
            from handlers.guess import start_guess_session
            await start_guess_session(
                message, db_session, state, int(payload[len(prefix):])
            )
            return

    # Deep-link: create_poll (admin poll creation FSM, redirected from the group)
    if payload == "create_poll":
        if await is_bot_admin(message.bot, message.from_user.id):
            from handlers.events import start_poll_creation
            await start_poll_creation(message, state)
        else:
            await message.answer("⛔ Accesso non autorizzato.")
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
            await _show_event_list(message, db_session)
        else:
            await message.answer("⛔ Accesso non autorizzato.")
        return

    # Deep-link: help
    if payload == "help":
        await _show_help(message, await is_bot_admin(message.bot, message.from_user.id))
        return

    # Deep-link: spiega_<cmd> (per-command manual, carried from the group button)
    if payload.startswith("spiega_"):
        cmd = payload[len("spiega_"):]
        admin = await is_bot_admin(message.bot, message.from_user.id)
        await message.answer(render_command_or_hint(cmd, admin))
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
    if payload in ("trofei", "traguardi"):
        from handlers.badges import show_traguardi
        await show_traguardi(message, db_session)
        return
    if payload == "classifiche":
        from handlers.leaderboard import show_board_private
        await show_board_private(message, db_session)
        return
    if payload == "scommesse":
        from handlers.betting import show_events_private
        await show_events_private(message, db_session, state)
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
        f"/saldo — Saldo residuo\n"
        f"/daily — Premio giornaliero\n"
        f"/scommesse — Scommesse aperte\n"
        f"/trofei — I tuoi trofei\n"
        f"/help — Tutti i comandi"
    )


@router.message(Command("profilo"))
async def cmd_profilo(message: Message, db_session: AsyncSession) -> None:
    # Public command: answers in the group too (no longer redirected to private).
    # Light anti-flood cooldown; the reply itself is deduplicated per user+command.
    if not await cooldown.ready(message, "profilo", settings.command_cooldown_seconds):
        return
    await show_profilo(message, db_session)


async def show_profilo(message: Message, db_session: AsyncSession) -> None:
    """Render the caller's own profile. Public: works in the group (one live reply
    per user via static_reply) and in private. Never shows the Telegram ID — that
    stays exclusive to the admin /info dossier."""
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

    from services import consumable_service, xp_service
    from services.shop_service import render_active_tags
    prog = xp_service.level_for_xp(user.xp)
    rank = xp_service.rank_for_level(prog.level)
    rank_txt = f" · {rank.emoji} {esc(rank.name)}" if rank else ""
    level_line = (
        f"⚡ <b>Livello {prog.level}</b>{rank_txt}\n"
        f"   {xp_service.progress_bar(prog)} "
        f"{prog.xp_into_level:,}/{prog.xp_for_next:,} XP\n"
    )
    tags = render_active_tags(user)
    tag_line = f"🏷️ <b>Tag:</b> {esc(tags)}\n" if tags else ""
    title = esc(user.full_name)
    if tags:
        title = f"{esc(tags)} · {title}"

    # Pantry preview: the first few consumables, compact (e.g. "🍕 ×3 · 🐉 ×1").
    pantry = await consumable_service.inventory(db_session, user.tg_id)
    pantry_line = ""
    if pantry:
        shown = " · ".join(f"{it.emoji} ×{qty}" for it, qty in pantry[:6])
        more = " …" if len(pantry) > 6 else ""
        pantry_line = f"🎒 <b>Dispensa:</b> {shown}{more}\n"

    await reply_static(
        message,
        f"🎮 <b>{title}</b>\n\n"
        f"🔖 <b>Username:</b> {username_display}\n\n"
        f"{tag_line}"
        f"{level_line}"
        f"💰 <b>CoInn:</b> <b>{user.wallet.coins:,} 🪙</b>\n"
        f"🏆 <b>Trofei:</b> {badge_count}\n"
        f"{pantry_line}".rstrip("\n"),
        "profilo",
    )


# /comandi is the canonical name; /help is kept as a hidden back-compat alias.
@router.message(Command("comandi", "help"))
async def cmd_help(message: Message) -> None:
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if not await cooldown.ready(message, "comandi", settings.command_cooldown_seconds):
            return
        bot_info = await message.bot.get_me()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="📖 Apri la guida in privato",
                    url=f"https://t.me/{bot_info.username}?start=help",
                )
            ]]
        )
        # Anti-flood: keep a single live "open the guide" button per user.
        await reply_static(
            message,
            "ℹ️ Clicca per vedere tutti i comandi disponibili.",
            "comandi",
            reply_markup=kb,
        )
        return

    await _show_help(message, await is_bot_admin(message.bot, message.from_user.id))


@router.message(Command("spiega_comando"))
async def cmd_spiega_comando(message: Message, command: CommandObject) -> None:
    # Detailed per-command manual — private only (keeps the group uncluttered and
    # never reveals the admin command set there).
    arg = (command.args or "").strip()
    # From the group, carry the command name through the deep-link so the landing
    # shows its explanation (not the generic /start menu). The payload charset is
    # [a-z0-9_] ⊂ Telegram's allowed [A-Za-z0-9_-]; unknown/empty → general guide.
    norm = normalize(arg)
    payload = f"spiega_{norm}" if arg and re.fullmatch(r"[a-z0-9_]+", norm) else "help"
    if await redirect_to_private(
        message, payload, "📖 Apri la guida",
        notice="📖 La guida dettagliata si apre in chat privata.",
    ):
        return
    if not await cooldown.guard(message, "spiega", settings.command_cooldown_seconds):
        return

    if not arg:
        await message.answer(
            "📘 <b>Spiega comando</b>\n\n"
            "Uso: <code>/spiega_comando &lt;comando&gt;</code>\n"
            "Esempio: <code>/spiega_comando daily</code>.\n\n"
            "Per l'elenco completo usa /comandi."
        )
        return

    is_admin = await is_bot_admin(message.bot, message.from_user.id)
    await message.answer(render_command_or_hint(arg, is_admin))


@router.callback_query()
async def cb_unhandled(callback: CallbackQuery) -> None:
    """Answer any callback no router claimed, instead of leaving the spinner up.

    `common.router` is last (`handlers/__init__.py`), so nothing that another
    router wanted can reach here. Two cases do: a button from a keyboard older
    than the current deploy — normal, and the user deserves a reply — and a
    handler that stopped matching by mistake, which would otherwise be silent
    forever. Hence the WARNING: it reaches the admins through utils.alerts
    (STEERING §26), so a button that quietly stops working reports itself.

    The `%s` is deliberate, not style: utils.alerts deduplicates on the message
    *template*, so an f-string would turn every stale click into its own alert
    and drown the channel meant to protect us.
    """
    log.warning("Callback non gestita: %s", callback.data)
    await callback.answer(_UNHANDLED_CALLBACK)
