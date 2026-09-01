"""Telegram adapter for Alduino's collaborative secret game."""

from __future__ import annotations

import json
import logging
from asyncio import Lock
from datetime import UTC, datetime
from weakref import WeakValueDictionary

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType, ParseMode
from aiogram.filters.command import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers import _mentions
from handlers.callbacks import TwentyQuestionsCreateCb
from services import ai_game_service, group_registry, schedule_service
from services.ai_game_types import (
    DEFAULT_DURATION_SECONDS,
    DEFAULT_MAX_COINS_PER_PARTICIPANT,
    DURATION_PRESETS_SECONDS,
    GameCreationError,
    GameView,
    PersonalQuota,
    TerminalResult,
    TurnOutcome,
    TurnRejectReason,
)
from services.ai_game_service import GameSnapshot
from services.structured_ai import GeminiStructuredProvider, StructuredAIError
from services.twenty_questions_rules import v2_policy
from utils.text import esc
from utils.twenty_questions_view import (
    render_live_card,
    render_personal_status,
    render_personal_turn,
    render_policy,
    render_public_help,
    render_question_start,
    render_terminal_card,
)

log = logging.getLogger(__name__)
router = Router(name="twenty_questions")

# Telegram has no compare-and-swap edit. Serialize local publications for one
# session, then re-read the small DTO under that lock so an older live card
# cannot follow a newer terminal card in this process. Idle locks are weakly
# held: an active ``async with`` retains its lock, while historical sessions do
# not accumulate process-lifetime entries. Cross-process recovery still uses
# the persistent anchor CAS below.
_card_publish_locks: WeakValueDictionary[int, Lock] = WeakValueDictionary()


class CardPublicationError(RuntimeError):
    """A strict post-commit publisher could not leave a recoverable card."""


def _card_publish_lock(session_id: int) -> Lock:
    lock = _card_publish_locks.get(session_id)
    if lock is None:
        lock = Lock()
        _card_publish_locks[session_id] = lock
    return lock


class TwentyQuestionsCreateStates(StatesGroup):
    title = State()
    duration_choice = State()
    absolute_expiry = State()
    coins_choice = State()
    custom_coins = State()


def _creation_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="❌ Annulla",
            callback_data=TwentyQuestionsCreateCb(action="cancel").pack(),
        )
    ]])


def _duration_choice_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for seconds in DURATION_PRESETS_SECONDS:
        hours = seconds // 3_600
        label = f"⏱️ {hours} ore"
        if seconds == DEFAULT_DURATION_SECONDS:
            label += " · consigliata"
        buttons.append(InlineKeyboardButton(
            text=label,
            callback_data=TwentyQuestionsCreateCb(action="duration", value=seconds).pack(),
        ))
    buttons.extend((
        InlineKeyboardButton(
            text="🗓️ Data e ora assolute",
            callback_data=TwentyQuestionsCreateCb(action="absolute").pack(),
        ),
        InlineKeyboardButton(
            text="❌ Annulla",
            callback_data=TwentyQuestionsCreateCb(action="cancel").pack(),
        ),
    ))
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:4], buttons[4:]])


def _coins_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🪙 Default ({DEFAULT_MAX_COINS_PER_PARTICIPANT} CoInn max)",
            callback_data=TwentyQuestionsCreateCb(action="coins_default").pack(),
        )],
        [InlineKeyboardButton(
            text="✏️ Importo personalizzato",
            callback_data=TwentyQuestionsCreateCb(action="coins_custom").pack(),
        )],
        [InlineKeyboardButton(
            text="❌ Annulla",
            callback_data=TwentyQuestionsCreateCb(action="cancel").pack(),
        )],
    ])


async def _stop_creation_for_maintenance(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "⚠️ Le nuove partite di Alduino sono temporaneamente in manutenzione."
    )


async def start_creation(message: Message, state: FSMContext, creator_id: int) -> None:
    await state.clear()
    if not settings.twentyq_v2_enabled:
        await message.answer(
            "⚠️ Le nuove partite di Alduino sono temporaneamente in manutenzione."
        )
        return
    await state.set_state(TwentyQuestionsCreateStates.title)
    await message.answer(
        "🐲 <b>Nuova partita: Il gioco segreto di Alduino</b>\n\n"
        "Scrivi il titolo pubblico della serata (massimo 120 caratteri). "
        "Il gioco segreto verrà estratto dal catalogo verificato.",
        reply_markup=_creation_cancel_keyboard(),
    )


@router.callback_query(
    TwentyQuestionsCreateCb.filter(F.action == "cancel"),
    IsAdminCallbackFilter(),
)
async def cancel_creation(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Creazione della partita annullata.")
    await callback.answer()


@router.message(
    TwentyQuestionsCreateStates.title,
    IsAdminFilter(),
    F.text,
    ~F.text.startswith("/"),
)
async def create_from_title(
    message: Message, state: FSMContext,
) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 120:
        await message.answer(
            f"⚠️ Serve un titolo da 1 a 120 caratteri (ora: {len(title)})."
        )
        return
    if not settings.twentyq_v2_enabled:
        await _stop_creation_for_maintenance(message, state)
        return
    await state.update_data(title=title)
    await state.set_state(TwentyQuestionsCreateStates.duration_choice)
    await message.answer(
        f"✅ Titolo: <b>{esc(title)}</b>\n\n"
        "<b>Step 2/3</b> — Scegli la durata della partita:",
        reply_markup=_duration_choice_keyboard(),
    )


@router.callback_query(
    TwentyQuestionsCreateStates.duration_choice,
    TwentyQuestionsCreateCb.filter(F.action == "duration"),
    IsAdminCallbackFilter(),
)
async def choose_creation_duration(
    callback: CallbackQuery,
    callback_data: TwentyQuestionsCreateCb,
    state: FSMContext,
) -> None:
    seconds = callback_data.value
    if seconds not in DURATION_PRESETS_SECONDS:
        await callback.answer("Durata non valida.", show_alert=True)
        return
    await state.update_data(duration_seconds=seconds, expires_at=None)
    await state.set_state(TwentyQuestionsCreateStates.coins_choice)
    await callback.message.edit_text(
        "<b>Step 3/3</b> — Scegli il premio massimo per partecipante:",
        reply_markup=_coins_choice_keyboard(),
    )
    await callback.answer()


@router.callback_query(
    TwentyQuestionsCreateStates.duration_choice,
    TwentyQuestionsCreateCb.filter(F.action == "absolute"),
    IsAdminCallbackFilter(),
)
async def choose_creation_absolute_expiry(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await state.set_state(TwentyQuestionsCreateStates.absolute_expiry)
    await callback.message.edit_text(
        "🗓️ Invia data e ora future in formato <code>AAAA-MM-GG HH:MM</code> "
        f"({esc(settings.scheduler_timezone)}).",
        reply_markup=_creation_cancel_keyboard(),
    )
    await callback.answer()


@router.message(
    TwentyQuestionsCreateStates.absolute_expiry,
    IsAdminFilter(),
    F.text,
    ~F.text.startswith("/"),
)
async def receive_absolute_expiry(message: Message, state: FSMContext) -> None:
    try:
        expires_at = schedule_service.parse_absolute_run_at(message.text or "")
    except ValueError as exc:
        await message.answer(
            f"⚠️ {esc(str(exc))}. Riprova con <code>AAAA-MM-GG HH:MM</code>.",
            reply_markup=_creation_cancel_keyboard(),
        )
        return
    await state.update_data(duration_seconds=None, expires_at=expires_at.isoformat())
    await state.set_state(TwentyQuestionsCreateStates.coins_choice)
    await message.answer(
        "<b>Step 3/3</b> — Scegli il premio massimo per partecipante:",
        reply_markup=_coins_choice_keyboard(),
    )


@router.callback_query(
    TwentyQuestionsCreateStates.coins_choice,
    TwentyQuestionsCreateCb.filter(F.action.in_({"coins_default", "coins_custom"})),
    IsAdminCallbackFilter(),
)
async def choose_creation_coins(
    callback: CallbackQuery,
    callback_data: TwentyQuestionsCreateCb,
    state: FSMContext,
    db_session: AsyncSession,
) -> None:
    if callback_data.action == "coins_custom":
        await state.set_state(TwentyQuestionsCreateStates.custom_coins)
        await callback.message.edit_text(
            "✏️ Invia il premio massimo per partecipante, da "
            f"<b>1</b> a <b>{settings.twentyq_max_coins_per_participant}</b> CoInn.",
            reply_markup=_creation_cancel_keyboard(),
        )
    else:
        await _finish_creation(
            callback.message,
            state,
            db_session,
            creator_id=callback.from_user.id,
            max_coins_per_participant=DEFAULT_MAX_COINS_PER_PARTICIPANT,
        )
    await callback.answer()


@router.message(
    TwentyQuestionsCreateStates.custom_coins,
    IsAdminFilter(),
    F.text,
    ~F.text.startswith("/"),
)
async def receive_custom_coins(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
) -> None:
    try:
        max_coins = int((message.text or "").strip())
    except ValueError:
        max_coins = 0
    if not 1 <= max_coins <= settings.twentyq_max_coins_per_participant:
        await message.answer(
            "⚠️ Inserisci un numero intero da <b>1</b> a "
            f"<b>{settings.twentyq_max_coins_per_participant}</b> CoInn.",
            reply_markup=_creation_cancel_keyboard(),
        )
        return
    await _finish_creation(
        message,
        state,
        db_session,
        creator_id=message.from_user.id,
        max_coins_per_participant=max_coins,
    )


async def _finish_creation(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    *,
    creator_id: int,
    max_coins_per_participant: int,
) -> None:
    if not settings.twentyq_v2_enabled:
        await _stop_creation_for_maintenance(message, state)
        return
    data = await state.get_data()
    title = data.get("title")
    duration_seconds = data.get("duration_seconds")
    expires_text = data.get("expires_at")
    expires_at = datetime.fromisoformat(expires_text) if isinstance(expires_text, str) else None
    duration = duration_seconds if isinstance(duration_seconds, int) else None
    if (
        not isinstance(title, str)
        or not title
        or (duration is None) == (expires_at is None)
    ):
        await state.clear()
        await message.answer("⚠️ I dati della creazione non sono più disponibili: ricomincia.")
        return
    try:
        game = await ai_game_service.create_twenty_questions(
            db_session,
            creator_tg_id=creator_id,
            title=title,
            duration_seconds=duration,
            expires_at=expires_at,
            max_coins_per_participant=max_coins_per_participant,
        )
        await db_session.commit()
    except GameCreationError:
        await db_session.rollback()
        await message.answer(
            "⚠️ Impossibile creare la partita in questo momento. Puoi riprovare."
        )
        return
    except Exception:  # noqa: BLE001 — retain the FSM draft for a retry after persistence trouble
        await db_session.rollback()
        log.exception("Secret-game creation failed")
        await message.answer(
            "⚠️ Impossibile salvare la partita in questo momento. Puoi riprovare."
        )
        return
    await state.clear()
    if duration is not None:
        deadline = f"⏱️ Durata: <b>{duration // 3_600} ore</b>"
    else:
        assert expires_at is not None
        deadline = f"🗓️ Scade: <b>{expires_at.strftime('%d/%m/%Y %H:%M UTC')}</b>"
    await message.answer(
        f"✅ Partita <b>#{game.session_id} · {esc(game.title)}</b> pronta.\n{deadline}\n\n"
        f"{render_policy(v2_policy(max_coins_per_participant))}\n\n"
        "Puoi avviarla o programmarla dall'hub /eventi. Il segreto resta nel dossier."
    )


@router.message(Command("gioco_alduino"))
async def cmd_gioco_alduino(message: Message, db_session: AsyncSession) -> None:
    """Explain the public rules privately, or show safe live status in a group."""
    policy = v2_policy(DEFAULT_MAX_COINS_PER_PARTICIPANT)
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(render_public_help(policy))
        return

    view, quota, alternatives = await _group_game_status(message, db_session)
    if view is None or quota is None:
        await message.answer(render_public_help(policy))
        return
    if view.anchor_message_id is None:
        await refresh_group_card(message.bot, db_session, view)
    alternatives_text = ""
    if alternatives:
        alternatives_text = (
            f"\n\nℹ️ Ci sono anche <b>{alternatives}</b> altre partite in corso: "
            "rispondi alla card desiderata con <code>/gioco_alduino</code> "
            "per selezionarla."
        )
    await message.answer(
        f"{render_personal_status(view, quota, now=_presentation_now())}{alternatives_text}"
    )


async def _group_game_status(
    message: Message,
    db_session: AsyncSession,
) -> tuple[GameView | None, PersonalQuota | None, int]:
    """Read only safe DTOs and close the transaction before command Telegram I/O."""
    try:
        rows = await ai_game_service.list_manageable(db_session)
        running = [
            row for row in rows
            if row.status == "running" and row.group_id == message.chat.id
        ]
        running.sort(
            key=lambda row: (row.started_at or datetime.min, row.id),
            reverse=True,
        )
        reply_id = (
            message.reply_to_message.message_id
            if message.reply_to_message is not None else None
        )
        selected = next(
            (row for row in running if row.anchor_message_id == reply_id),
            running[0] if running else None,
        )
        if selected is None:
            await db_session.rollback()
            return None, None, 0
        view = await ai_game_service.get_game_view(db_session, selected.id)
        quota = await ai_game_service.get_personal_quota(
            db_session,
            selected.id,
            message.from_user.id,
        )
        alternatives = max(0, len(running) - 1)
        await db_session.rollback()
    except Exception:  # noqa: BLE001 — a public rules page is safer than a failed command
        await db_session.rollback()
        log.exception("Secret-game public status lookup failed")
        return None, None, 0
    if view is None or view.status != "running":
        return None, None, 0
    return view, quota, alternatives


def render_card(
    snapshot: GameSnapshot, *, reveal: bool = False, open_preview: bool = False,
) -> str:
    lines = [
        f"🐲 <b>{esc(snapshot.session.title)}</b>",
        "<i>Alduino ha scelto un videogioco.</i>",
        "",
        f"❓ Domande rimaste: <b>{snapshot.questions_left}/{snapshot.game.question_limit}</b>",
        f"🎯 Tentativi rimasti: <b>{snapshot.guesses_left}/{snapshot.game.guess_limit}</b>",
    ]
    recent = snapshot.turns[-6:]
    if recent:
        lines.extend(["", "<b>Ultimi turni</b>"])
        for turn in recent:
            output = json.loads(turn.output_json)
            if turn.kind == "question":
                # ``irrilevante`` is kept for cards from games started before
                # the terse sì/no/forse contract was introduced.
                icon = {"si": "✅", "no": "❌", "forse": "🤔", "irrilevante": "➖"}.get(
                    output.get("verdetto"), "➖"
                )
                lines.append(f"{icon} {esc(turn.input_text)}")
            else:
                lines.append(
                    f"{'🏆' if output.get('correct') else '💥'} Tentativo: {esc(turn.input_text)}"
                )
    if snapshot.session.status == "running" or open_preview:
        lines.extend([
            "", "Rispondi <b>a questo messaggio</b> con una domanda.",
            "Per tentare: <code>RISPOSTA: titolo del gioco</code>",
        ])
    if reveal or snapshot.session.status == "finished":
        winner = (
            f"\n🏆 Vincitore: <code>{snapshot.game.winner_tg_id}</code>"
            if snapshot.game.winner_tg_id else "\n⌛ Risorse esaurite."
        )
        lines.extend(["", f"🔓 Era <b>{esc(snapshot.game.answer)}</b>!{winner}"])
    return "\n".join(lines)


async def _refresh_legacy_group_card(
    bot: Bot, db_session: AsyncSession, snapshot: GameSnapshot,
) -> None:
    """Keep the historical v1 publisher isolated from the v2 CAS publisher."""
    try:
        await bot.edit_message_text(
            chat_id=snapshot.session.group_id,
            message_id=snapshot.session.anchor_message_id,
            text=render_card(snapshot),
        )
    except Exception as exc:  # noqa: BLE001 — non-editable/deleted anchor: recover with a new one
        log.warning(
            "Card 20 domande #%s non editabile error=%s",
            snapshot.session.id,
            type(exc).__name__,
        )
        sent = await group_registry.send_group_message(
            bot, db_session, render_card(snapshot),
        )
        await ai_game_service.move_anchor(db_session, snapshot.session.id, sent.message_id)


def _presentation_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _delete_orphan(sent, *, session_id: int) -> bool:
    try:
        await sent.delete()
        return True
    except Exception as exc:  # noqa: BLE001 — cleanup must not affect the winner's anchor
        log.warning(
            "Card 20 domande orphan cleanup failed session=%s error=%s",
            session_id,
            type(exc).__name__,
        )
        return False


async def _has_winning_anchor(db_session: AsyncSession, *, session_id: int) -> bool:
    """Confirm a CAS loser raced with another durable publisher, not with deletion."""
    try:
        current = await ai_game_service.get_game_view(db_session, session_id)
        await db_session.commit()
    except Exception as exc:
        await db_session.rollback()
        raise CardPublicationError("impossibile verificare la card concorrente") from exc
    return current is not None and current.anchor_message_id is not None


async def _move_sent_anchor(
    db_session: AsyncSession,
    *,
    session_id: int,
    expected_message_id: int | None,
    sent,
    strict: bool = False,
) -> None:
    """CAS an already-sent card, then delete only our message if we lost."""
    try:
        moved = await ai_game_service.move_anchor_if_current(
            db_session,
            session_id,
            expected_message_id=expected_message_id,
            new_message_id=sent.message_id,
        )
        await db_session.commit()
    except Exception as exc:  # noqa: BLE001 — sent card is orphaned on DB failure too
        await db_session.rollback()
        log.warning(
            "Card 20 domande anchor CAS failed session=%s error=%s",
            session_id,
            type(exc).__name__,
        )
        await _delete_orphan(sent, session_id=session_id)
        if strict:
            raise CardPublicationError("anchor della card non salvato") from exc
        return
    if not moved:
        cleaned = await _delete_orphan(sent, session_id=session_id)
        if strict:
            winner_exists = await _has_winning_anchor(
                db_session,
                session_id=session_id,
            )
            if not winner_exists:
                raise CardPublicationError("nessuna card concorrente recuperabile")
            if not cleaned:
                raise CardPublicationError("card concorrente presente ma orphan non rimosso")


async def _publish_rendered_card(
    bot: Bot,
    db_session: AsyncSession,
    *,
    session_id: int,
    group_id: int | None,
    anchor_message_id: int | None,
    text: str,
    strict: bool = False,
) -> None:
    """Edit a known card or publish-and-CAS a recovery card after a caller commit."""
    if group_id is None:
        log.warning("Card 20 domande skipped without group session=%s", session_id)
        if strict:
            await db_session.rollback()
            raise CardPublicationError("gruppo della card non disponibile")
        return
    if anchor_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=group_id,
                message_id=anchor_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as exc:  # noqa: BLE001 — Telegram can reject a stale/deleted card
            log.warning(
                "Card 20 domande not editable session=%s error=%s",
                session_id,
                type(exc).__name__,
            )
    try:
        sent = await group_registry.send_group_message(
            bot,
            db_session,
            text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:  # noqa: BLE001 — card publication is post-commit best effort
        log.warning(
            "Card 20 domande send failed session=%s error=%s",
            session_id,
            type(exc).__name__,
        )
        if strict:
            await db_session.rollback()
            raise CardPublicationError("invio della card fallito") from exc
        return
    await _move_sent_anchor(
        db_session,
        session_id=session_id,
        expected_message_id=anchor_message_id,
        sent=sent,
        strict=strict,
    )


async def refresh_group_card(
    bot: Bot,
    db_session: AsyncSession,
    view: GameView | GameSnapshot,
    *,
    strict: bool = False,
) -> None:
    """Refresh a v2 live card while retaining the isolated v1 event adapter."""
    if isinstance(view, GameSnapshot):
        await _refresh_legacy_group_card(bot, db_session, view)
        return
    async with _card_publish_lock(view.session_id):
        try:
            current = await ai_game_service.get_game_view(db_session, view.session_id)
            # A read starts a transaction too; close it before Telegram I/O.
            await db_session.commit()
        except Exception as exc:  # noqa: BLE001 — refresh remains post-commit best effort
            await db_session.rollback()
            log.warning(
                "Card 20 domande reread failed session=%s error=%s",
                view.session_id,
                type(exc).__name__,
            )
            if strict:
                raise CardPublicationError("rilettura della card fallita") from exc
            return
        if current is None:
            log.warning("Card 20 domande skipped after missing view session=%s", view.session_id)
            if strict:
                await db_session.rollback()
                raise CardPublicationError("stato della card non disponibile")
            return
        if current.status != "running":
            log.info("Card 20 domande skipped for terminal session=%s", view.session_id)
            if strict:
                await db_session.rollback()
                raise CardPublicationError("stato della card non pubblicabile")
            return
        await _publish_rendered_card(
            bot,
            db_session,
            session_id=current.session_id,
            group_id=current.group_id,
            anchor_message_id=current.anchor_message_id,
            text=render_live_card(current, now=_presentation_now()),
            strict=strict,
        )


async def publish_terminal(
    bot: Bot,
    db_session: AsyncSession,
    result: TerminalResult,
    *,
    strict: bool = False,
) -> None:
    """Publish a committed terminal result without ever re-terminalizing it."""
    winner_html: str | None = None
    try:
        if result.winner_tg_id is not None:
            winner_html = await _mentions.mention(db_session, result.winner_tg_id)
        # mention() is a read; close that transaction before any Telegram call.
        await db_session.commit()
    except Exception as exc:  # noqa: BLE001 — terminal settlement remains committed and retryable
        await db_session.rollback()
        log.warning(
            "Terminal card mention lookup failed session=%s error=%s",
            result.session_id,
            type(exc).__name__,
        )
        # Settlement was committed by the caller already; publish the generic
        # terminal card after rollback instead of losing the only announcement.
    async with _card_publish_lock(result.session_id):
        group_id = result.group_id
        anchor_message_id = result.anchor_message_id
        try:
            current = await ai_game_service.get_game_view(db_session, result.session_id)
            # A queued live fallback may have recovered a newer anchor while
            # this terminal result waited for the publication lock.
            await db_session.commit()
        except Exception as exc:  # noqa: BLE001 — result still allows recovery by its saved anchor
            await db_session.rollback()
            log.warning(
                "Terminal card reread failed session=%s error=%s",
                result.session_id,
                type(exc).__name__,
            )
        else:
            if current is not None:
                group_id = current.group_id
                anchor_message_id = current.anchor_message_id
        await _publish_rendered_card(
            bot,
            db_session,
            session_id=result.session_id,
            group_id=group_id,
            anchor_message_id=anchor_message_id,
            text=render_terminal_card(result, winner_html=winner_html),
            strict=strict,
        )


def _guess(text: str) -> str | None:
    head, separator, tail = text.partition(":")
    if separator and head.strip().casefold() == "risposta":
        value = tail.strip()
        return value
    return None


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.reply_to_message,
    F.text,
    ~F.text.startswith("/"),
)
async def play_turn(message: Message, db_session: AsyncSession) -> None:
    snapshot = await ai_game_service.find_by_anchor(
        db_session, message.chat.id, message.reply_to_message.message_id,
    )
    if snapshot is None:
        raise SkipHandler()
    if snapshot.game.rules_version == 1:
        await _play_turn_v1(message, db_session, snapshot)
        return
    await _play_turn_v2(message, db_session, session_id=snapshot.session.id)


async def _commit_or_rollback(db_session: AsyncSession) -> None:
    try:
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise


async def _play_turn_v2(
    message: Message, db_session: AsyncSession, *, session_id: int,
) -> None:
    """Drive typed v2 turns with DB/AI/DB boundaries and post-commit Telegram I/O."""
    raw_text = message.text
    guess = _guess(raw_text)
    if guess is not None:
        await _play_v2_guess(message, db_session, session_id=session_id, answer=guess)
        return

    started = await ai_game_service.begin_question(
        db_session,
        session_id=session_id,
        user_tg_id=message.from_user.id,
        question=raw_text,
    )
    if started.outcome is not TurnOutcome.claimed:
        await _commit_or_rollback(db_session)
        if started.terminal is not None:
            await publish_terminal(message.bot, db_session, started.terminal)
        await message.reply(render_question_start(started))
        return
    if started.claim is None:
        raise RuntimeError(f"v2 question claim missing session={session_id}")

    # The short lease is durable before the configured free-first router gets
    # network access. ai_game_service resolves that router; no handler provider
    # object or ORM snapshot crosses this boundary.
    await _commit_or_rollback(db_session)
    try:
        routed = await ai_game_service.classify_question(started.claim)
    except StructuredAIError:
        failed = await ai_game_service.abandon_claim(
            db_session,
            claim=started.claim,
            reason=TurnRejectReason.providers_unavailable,
        )
        await _commit_or_rollback(db_session)
        await message.reply(render_personal_turn(failed))
        return

    completed = await ai_game_service.complete_question(
        db_session,
        claim=started.claim,
        verdict=routed.value,
    )
    await _commit_or_rollback(db_session)
    await message.reply(render_personal_turn(completed))
    if completed.terminal is not None:
        await publish_terminal(message.bot, db_session, completed.terminal)
    elif completed.outcome is TurnOutcome.recorded:
        latest = await ai_game_service.get_game_view(db_session, completed.session_id)
        # Reading the presenter DTO opens an implicit transaction too. Close it
        # before the Telegram refresh; a missing row remains a best-effort no-op.
        await _commit_or_rollback(db_session)
        if latest is None:
            log.warning("Card 20 domande skipped after missing view session=%s", completed.session_id)
        else:
            await refresh_group_card(message.bot, db_session, latest)


async def _play_v2_guess(
    message: Message,
    db_session: AsyncSession,
    *,
    session_id: int,
    answer: str,
) -> None:
    result = await ai_game_service.submit_guess(
        db_session,
        session_id=session_id,
        user_tg_id=message.from_user.id,
        answer=answer,
    )
    await _commit_or_rollback(db_session)
    await message.reply(render_personal_turn(result))
    if result.terminal is not None:
        await publish_terminal(message.bot, db_session, result.terminal)
    elif result.outcome is TurnOutcome.recorded:
        latest = await ai_game_service.get_game_view(db_session, result.session_id)
        await _commit_or_rollback(db_session)
        if latest is None:
            log.warning("Card 20 domande skipped after missing view session=%s", result.session_id)
        else:
            await refresh_group_card(message.bot, db_session, latest)


async def _play_turn_v1(
    message: Message, db_session: AsyncSession, snapshot: GameSnapshot,
) -> None:
    """Historical 20/3 implementation, intentionally separate from v2 DTO APIs."""
    text = message.text.strip()
    if not text or len(text) > 500:
        # find_by_anchor() read under the handler session; no mutation occurred.
        await db_session.rollback()
        await message.reply("🐲 Tienila tra 1 e 500 caratteri, avventuriero.")
        return

    guess = _guess(text)
    if guess == "":
        # The anchor lookup is still open, so close its read transaction first.
        await db_session.rollback()
        await message.reply("🐲 Scrivi un titolo dopo <code>RISPOSTA:</code>.")
        return

    token = await ai_game_service.claim_turn(db_session, snapshot.session.id)
    if token is None:
        # A failed conditional lease changed nothing; do not retain its transaction for Telegram.
        await db_session.rollback()
        await message.reply("🐲 Sto già rispondendo a un'altra domanda. Un respiro e riprova.")
        return
    # This short commit is deliberate: never hold a DB transaction while Gemini thinks.
    await db_session.commit()

    if guess is not None:
        correct = ai_game_service.guess_is_correct(snapshot.game, guess)
        recorded = await ai_game_service.record_guess(
            db_session, session_id=snapshot.session.id, token=token,
            user_tg_id=message.from_user.id, answer=guess, correct=correct,
        )
        if not recorded:
            await db_session.rollback()
            await message.reply("🐲 Quel tentativo non è più disponibile.")
            return
        await db_session.commit()
        await message.reply("🏆 Preso!" if correct else "💥 No, non è lui.")
    else:
        try:
            verdict = await ai_game_service.classify_question(
                snapshot, text, GeminiStructuredProvider(),
            )
        except StructuredAIError:
            await ai_game_service.release_turn(db_session, snapshot.session.id, token)
            await db_session.commit()
            await message.reply(
                "🔥 Alduino ha il cervello in fumo. La domanda non è stata consumata: riprova."
            )
            return
        recorded = await ai_game_service.record_question(
            db_session, session_id=snapshot.session.id, token=token,
            user_tg_id=message.from_user.id, question=text, verdict=verdict,
        )
        if not recorded:
            await db_session.rollback()
            await message.reply("🐲 Le domande disponibili sono finite.")
            return
        await db_session.commit()
        label = {
            "si": "SÌ", "no": "NO", "forse": "FORSE", "usa_risposta": "PROVA A INDOVINARE",
        }[verdict.value]
        await message.reply(f"🐲 <b>{label}</b>")

    fresh = await ai_game_service.get_snapshot(db_session, snapshot.session.id)
    # The legacy refresh still renders its historical snapshot, but the read
    # transaction must be closed before Telegram edit/send I/O.
    await _commit_or_rollback(db_session)
    if fresh is not None:
        await _refresh_legacy_group_card(message.bot, db_session, fresh)
        # A legacy fallback can have moved the anchor after its Telegram send.
        await _commit_or_rollback(db_session)
