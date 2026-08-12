"""Inline mode: public, read-only discovery of live and upcoming events."""

from __future__ import annotations

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from sqlalchemy.ext.asyncio import AsyncSession

from handlers import event_types
from services import event_discovery, schedule_service
from services.public_event import PublicEvent
from utils.text import esc

router = Router(name="inline_mode")
_RESULT_LIMIT = 30


def _mode(raw: str) -> str:
    value = raw.strip().lower()
    if value in {"aperti", "aperto", "live", "ora"}:
        return "open"
    if value in {"prossimi", "prossimo", "coming soon", "soon", "programmati"}:
        return "soon"
    return "all"


def _article(event: PublicEvent, bot_username: str) -> InlineQueryResultArticle:
    if event.is_open:
        badge = "🟢 APERTO"
        timing = "Puoi partecipare adesso."
    else:
        assert event.starts_at is not None
        when = schedule_service.to_local(event.starts_at).strftime("%d/%m/%Y alle %H:%M")
        badge = "🗓️ COMING SOON"
        timing = f"In programma il {when}."

    markup = None
    if event.deep_link_payload:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="▶️ Apri l'evento" if event.is_open else "ℹ️ Dettagli",
                url=f"https://t.me/{bot_username}?start={event.deep_link_payload}",
            )
        ]])
    message_text = (
        f"{event.emoji} <b>{esc(event.title)}</b>\n"
        f"{badge}\n\n{esc(event.summary)}\n{timing}"
    )
    return InlineQueryResultArticle(
        id=event.result_id,
        title=f"{badge} · {event.title}",
        description=f"{event.summary} · {timing}",
        input_message_content=InputTextMessageContent(
            message_text=message_text, parse_mode=ParseMode.HTML,
        ),
        reply_markup=markup,
    )


@router.inline_query()
async def public_events(query: InlineQuery, db_session: AsyncSession) -> None:
    events = await event_discovery.list_public_events(
        db_session, event_types=event_types.all_types(),
        mode=_mode(query.query), limit=_RESULT_LIMIT,
    )
    bot_info = await query.bot.get_me()
    results = [_article(event, bot_info.username) for event in events]
    await query.answer(results=results, is_personal=True, cache_time=5)
