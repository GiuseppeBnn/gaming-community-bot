"""
General-purpose handlers: /start (with deep-link dispatch), /profilo, /help.

Deep-link payloads routed through /start:
  create_bet            → opens the bet-creation FSM in private chat
  create_poll           → opens the poll-creation FSM in private chat (admin only)
  manage_quiz/guess/sound/twentyq → admin management list of that event type
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

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from services import consumable_service
from utils import cooldown
from utils.profile_view import profile_text
from utils.static_reply import reply_static
from utils.text import esc

router = Router()

log = logging.getLogger(__name__)

#: Response for a callback that no router claimed. Short enough for a toast.
_UNHANDLED_CALLBACK = "Questo bottone non è più valido."


async def _show_help(message: Message, is_admin: bool) -> None:
    # The legend lives in help_content (single source of truth shared with
    # /spiega_comando), so the two never drift apart.
    await message.answer(render_legend(is_admin))


async def _ensure_welcome_trophy(message: Message, db_session: AsyncSession) -> None:
    """Grant the «first_steps» welcome trophy if the caller is using the bot but
    somehow never got it (see cmd_start for why an admin can miss it). Idempotent:
    ``award_badge`` returns ``is_new=False`` for anyone who already owns it, so this
    only ever affects the few users who slipped through. Announced like every trophy."""
    from database.models import Badge
    from handlers._trophy_announce import announce_trophies
    from services import badge_service

    _ub, is_new = await badge_service.award_badge(
        db_session, message.from_user.id, badge_service.BADGE_FIRST_STEPS
    )
    if not is_new:
        return
    await db_session.commit()
    badge = (
        await db_session.execute(
            select(Badge).where(Badge.slug == badge_service.BADGE_FIRST_STEPS)
        )
    ).scalar_one_or_none()
    if badge is not None:
        await announce_trophies(message.bot, db_session, message.from_user.id, [badge])


async def _dispatch_admin_link(
    payload: str, message: Message, state: FSMContext, db_session: AsyncSession,
) -> bool:
    """Whitelist admin landings and recheck permissions before every dispatch.

    Functions are resolved at call time, like the original lazy imports. Group
    routes by their actual signature, without reflection or adapter wrappers.
    """
    from handlers import (
        admin_betting,
        admin_dashboard,
        backup,
        events,
        quiz,
        schedule,
        twenty_questions,
    )
    from handlers.event_types.guess_type import GuessType
    from handlers.event_types.quiz_type import QuizType

    quiz_list = QuizType().render_list
    db_routes: dict[str, Callable[[Message, AsyncSession], Awaitable[None]]] = {
        "admin": admin_dashboard.show_dashboard_home,
        "manage_twentyq": twenty_questions.cmd_gestisci_gioco_alduino,
        "manage_trivianerd": quiz_list,
        "manage_quiz": quiz_list,
        "manage_guess": GuessType("guess").render_list,
        "manage_sound": GuessType("sound").render_list,
        "programmati": schedule.cmd_programmati,
        "manage_bets": admin_betting._show_event_list,
        "backup": backup.run_backup_now,
        "esporta": backup.run_export_now,
    }
    state_routes: dict[str, Callable[[Message, FSMContext], Awaitable[None]]] = {
        "create_trivianerd": quiz.start_quiz_creation,
        "create_quiz": quiz.start_quiz_creation,
        "create_poll": events.start_poll_creation,
        "programma": schedule.start_schedule_flow,
    }
    db_action, state_action = db_routes.get(payload), state_routes.get(payload)
    if db_action is None and state_action is None and payload != "eventi":
        return False
    if not await is_bot_admin(message.bot, message.from_user.id):
        await message.answer("⛔ Accesso non autorizzato.")
        return True
    if db_action is not None:
        await db_action(message, db_session)
    elif state_action is not None:
        await state_action(message, state)
    else:
        await events.show_hub(message)
    return True


async def _dispatch_game_link(
    payload: str, message: Message, state: FSMContext, db_session: AsyncSession,
) -> bool:
    """Public game/gift landings retain membership and ownership guards."""
    if payload.startswith("offri_"):
        from handlers.tavern import start_offer_private
        await start_offer_private(message, db_session, payload[6:])
        return True
    if payload.startswith("telefono_") and payload[9:].isdigit():
        from handlers.telephone import join
        await join(message, state, db_session, int(payload[9:]))
        return True
    # New invitations and already-published links use the same session engine.
    for prefix in ("trivianerd_", "quiz_"):
        value = payload[len(prefix):]
        if payload.startswith(prefix) and value.isascii() and value.isdecimal():
            from handlers.quiz import start_quiz_session
            await start_quiz_session(message, db_session, int(value))
            return True
    for kind in ("guess", "sound"):
        prefix = f"{kind}_"
        if payload.startswith(prefix) and payload[len(prefix):].isdigit():
            from handlers.guess import start_guess_session
            await start_guess_session(message, db_session, state, int(payload[len(prefix):]))
            return True
    return False


async def _dispatch_personal_link(
    payload: str, message: Message, state: FSMContext, db_session: AsyncSession,
) -> bool:
    """Public navigation; each destination retains its existing privacy policy."""
    from handlers import badges, betting, economy, leaderboard, shop

    db_routes: dict[str, Callable[[Message, AsyncSession], Awaitable[None]]] = {
        "saldo": economy.show_saldo,
        "daily": economy.show_saldo,
        "storico": economy.show_storico,
        "profilo": show_profilo,
        "trofei": badges.show_traguardi,
        "traguardi": badges.show_traguardi,
        "classifiche": leaderboard.show_board_private,
    }
    action = db_routes.get(payload)
    if action is not None:
        await action(message, db_session)
    elif payload == "scommesse":
        await betting.show_events_private(message, db_session, state)
    elif payload == "create_bet":
        await betting.start_bet_creation(message, state)
    elif payload == "help":
        await _show_help(message, await is_bot_admin(message.bot, message.from_user.id))
    elif payload.startswith("spiega_"):
        admin = await is_bot_admin(message.bot, message.from_user.id)
        await message.answer(render_command_or_hint(payload[len("spiega_"):], admin))
    elif payload.startswith("shop_") and payload[5:].lstrip("-").isdigit():
        await shop.start_shop_private(message, state, int(payload[5:]), db_session)
    else:
        return False
    return True


async def _dispatch_bet_link(
    payload: str, message: Message, state: FSMContext, db_session: AsyncSession,
) -> bool:
    """Keep custom amounts ahead of event links; preserve invalid-link feedback."""
    if payload.startswith("bet_custom_"):
        parts = payload.split("_")
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            from handlers.betting import start_custom_amount
            await start_custom_amount(message, state, int(parts[2]), int(parts[3]))
        else:
            await message.answer("⚠️ Link non valido.")
        return True
    if payload.startswith("bet_"):
        tail = payload[4:]
        if tail.isdigit():
            from handlers.betting import start_bet_view
            await start_bet_view(message, db_session, int(tail), state)
        else:
            await message.answer("⚠️ Link non valido.")
        return True
    return False


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
        # switch to private for an admin entry point (e.g. ?start=create_trivianerd):
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

    # Welcome-trophy backfill (idempotent): reaching here means the caller is using
    # the bot and has cleared the onboarding gate — either they completed onboarding
    # (and normally already have the trophy) or they are a Telegram-recognized admin
    # who bypasses the rules-acceptance flow, which is the only place «first_steps» is
    # granted. Without this, such an admin never gets it. No-op for anyone who has it.
    await _ensure_welcome_trophy(message, db_session)

    payload = command.args or ""

    # A private game is not a modal dialog: following a new /start link releases
    # the old answer mode, without resetting the DB-backed clock or attempts.
    # Other FSMs (e.g. admin drafts) retain their existing interruption policy.
    if message.chat.type == ChatType.PRIVATE:
        from handlers.guess.play import leave_answering
        await leave_answering(message, state)

    for dispatch in (
        _dispatch_game_link,
        _dispatch_admin_link,
        _dispatch_personal_link,
        _dispatch_bet_link,
    ):
        if await dispatch(payload, message, state, db_session):
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

    # Pantry preview: the first few consumables, compact (e.g. "🍕 ×3 · 🐉 ×1").
    pantry = await consumable_service.inventory(db_session, user.tg_id)
    await reply_static(
        message,
        profile_text(user, pantry),
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
    log.warning("Unhandled callback: %s", callback.data)
    await callback.answer(_UNHANDLED_CALLBACK)
