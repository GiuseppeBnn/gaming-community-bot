"""Telegram adapter for the collaborative «Alduino ha scelto un gioco»."""

from __future__ import annotations

import json
import logging
from asyncio import Lock
from datetime import UTC, datetime
from weakref import WeakValueDictionary

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType, ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers import _mentions
from handlers.callbacks import EventCb
from services import ai_game_service, group_registry
from services.ai_game_types import (
    DEFAULT_DURATION_SECONDS,
    DEFAULT_MAX_COINS_PER_PARTICIPANT,
    GameCreationError,
    GameView,
    TerminalResult,
    TurnOutcome,
    TurnRejectReason,
)
from services.ai_game_service import GameSnapshot
from services.structured_ai import GeminiStructuredProvider, StructuredAIError
from utils.text import esc
from utils.twenty_questions_view import (
    render_live_card,
    render_personal_turn,
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


async def start_creation(message: Message, state: FSMContext, creator_id: int) -> None:
    await state.clear()
    await state.set_state(TwentyQuestionsCreateStates.title)
    await message.answer(
        "🐲 <b>Nuova partita: Alduino ha scelto un gioco</b>\n\n"
        "Scrivi il titolo pubblico della serata (massimo 120 caratteri). "
        "Il gioco segreto verrà estratto dal catalogo verificato.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="❌ Annulla",
                callback_data=EventCb(action="cancelnew", task_type="twentyq").pack(),
            )
        ]]),
    )


@router.callback_query(
    EventCb.filter(F.action == "cancelnew"),
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
    message: Message, state: FSMContext, db_session: AsyncSession,
) -> None:
    title = message.text.strip()
    if not title or len(title) > 120:
        await message.answer(
            f"⚠️ Serve un titolo da 1 a 120 caratteri (ora: {len(title)})."
        )
        return
    if not settings.twentyq_v2_enabled:
        await message.answer(
            "⚠️ Le nuove partite di Alduino sono temporaneamente in manutenzione."
        )
        return
    try:
        game = await ai_game_service.create_twenty_questions(
            db_session,
            creator_tg_id=message.from_user.id,
            title=title,
            duration_seconds=DEFAULT_DURATION_SECONDS,
            expires_at=None,
            max_coins_per_participant=DEFAULT_MAX_COINS_PER_PARTICIPANT,
        )
    except GameCreationError:
        await message.answer("⚠️ Impossibile creare la partita in questo momento.")
        return
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ Partita <b>#{game.session_id} · {esc(game.title)}</b> pronta.\n"
        "Puoi avviarla o programmarla dall'hub /eventi. Il segreto resta nel dossier."
    )


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
