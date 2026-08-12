"""Telegram adapter for the collaborative «Alduino ha scelto un gioco»."""

from __future__ import annotations

import json
import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers.callbacks import EventCb
from services import ai_game_service, group_registry
from services.ai_game_service import GameSnapshot
from services.structured_ai import GeminiStructuredProvider, StructuredAIError
from utils.text import esc

log = logging.getLogger(__name__)
router = Router(name="twenty_questions")


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
    game = await ai_game_service.create_twenty_questions(
        db_session, creator_tg_id=message.from_user.id, title=title,
    )
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ Partita <b>#{game.id} · {esc(game.title)}</b> pronta.\n"
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
                icon = {"si": "✅", "no": "❌", "irrilevante": "➖"}.get(
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


async def refresh_group_card(bot, db_session: AsyncSession, snapshot: GameSnapshot) -> None:
    try:
        await bot.edit_message_text(
            chat_id=snapshot.session.group_id,
            message_id=snapshot.session.anchor_message_id,
            text=render_card(snapshot),
        )
    except Exception as exc:  # noqa: BLE001 — non-editable/deleted anchor: recover with a new one
        log.warning("Card 20 domande #%s non editabile: %s", snapshot.session.id, exc)
        sent = await group_registry.send_group_message(
            bot, db_session, render_card(snapshot),
        )
        await ai_game_service.move_anchor(db_session, snapshot.session.id, sent.message_id)


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
    text = message.text.strip()
    if not text or len(text) > 500:
        await message.reply("🐲 Tienila tra 1 e 500 caratteri, avventuriero.")
        return

    guess = _guess(text)
    if guess == "":
        await message.reply("🐲 Scrivi un titolo dopo <code>RISPOSTA:</code>.")
        return

    token = await ai_game_service.claim_turn(db_session, snapshot.session.id)
    if token is None:
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
        label = {"si": "SÌ", "no": "NO", "irrilevante": "IRRILEVANTE"}[verdict.verdict]
        await message.reply(f"🐲 <b>{label}</b> — {esc(verdict.reply)}")

    fresh = await ai_game_service.get_snapshot(db_session, snapshot.session.id)
    if fresh is not None:
        await refresh_group_card(message.bot, db_session, fresh)
        await db_session.commit()
