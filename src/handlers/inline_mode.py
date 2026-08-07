"""Inline mode: the user picker. `@bot <name>` → type-ahead search over users
(partial match on username/full_name) → tap → the chosen player's full profile
card lands in the chat (balance included, by explicit user decision).

One query per keystroke: a small TTL cache absorbs the repeats Telegram fires for
the same substring. Results are personal to the caller (is_personal=True)."""

from __future__ import annotations

import time
from collections import OrderedDict

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from sqlalchemy.ext.asyncio import AsyncSession

from services import admin_service, xp_service
from utils.profile_view import profile_text
from utils.text import esc

router = Router(name="inline_mode")

_HINT_FOCUS = (
    "🔎 Cerca un giocatore scrivendo il suo nome o @username."
)

# (query_lower) -> (timestamp, results). Ordered so the oldest entry is evicted first.
_RESULT_CACHE: OrderedDict[str, tuple[float, list[InlineQueryResultArticle]]] = OrderedDict()
_CACHE_TTL = 3.0
_CACHE_MAX = 256
_RESULT_LIMIT = 20

# Module alias, patchable in tests: the only place the handler talks to the DB.
_search_users = admin_service.search_users


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _cache_get(key: str) -> list[InlineQueryResultArticle] | None:
    hit = _RESULT_CACHE.get(key)
    if hit is None:
        return None
    ts, results = hit
    if time.monotonic() - ts > _CACHE_TTL:
        _RESULT_CACHE.pop(key, None)
        return None
    _RESULT_CACHE.move_to_end(key)
    return results


def _cache_set(key: str, results: list[InlineQueryResultArticle]) -> None:
    _RESULT_CACHE[key] = (time.monotonic(), results)
    _RESULT_CACHE.move_to_end(key)
    while len(_RESULT_CACHE) > _CACHE_MAX:
        _RESULT_CACHE.popitem(last=False)


def _cache_size() -> int:
    return len(_RESULT_CACHE)


def clear_cache() -> None:
    _RESULT_CACHE.clear()


# ---------------------------------------------------------------------------
# articles
# ---------------------------------------------------------------------------

def _hint_article(
    key: str | None, text: str, message_text: str | None = None
) -> InlineQueryResultArticle:
    """Hint article. `title`/`description` are not HTML-parsed by Telegram, so the
    title keeps the raw text; `message_text` is HTML-parsed and must be escaped when
    it carries user input (rule 20) — pass it explicitly in that case."""
    return InlineQueryResultArticle(
        id=key or "hint",
        title=text,
        description=_HINT_FOCUS,
        input_message_content=InputTextMessageContent(
            message_text=text if message_text is None else message_text,
            parse_mode=ParseMode.HTML,
        ),
    )


def _user_article(user) -> InlineQueryResultArticle:
    handle = f"@{user.username}" if user.username else "nessun @"
    prog = xp_service.level_for_xp(user.xp)
    rank = xp_service.rank_for_level(prog.level)
    rank_emoji = rank.emoji if rank else ""
    return InlineQueryResultArticle(
        id=str(user.tg_id),
        title=f"{rank_emoji} {user.full_name}".strip(),
        description=f"{handle} · 🏆 {len(user.badges)} trofei",
        input_message_content=InputTextMessageContent(
            message_text=profile_text(user),
            parse_mode=ParseMode.HTML,
        ),
    )


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------

@router.inline_query()
async def user_picker(query: InlineQuery, db_session: AsyncSession) -> None:
    raw = query.query.strip().lstrip("@").strip()
    key = raw.lower()

    cached = _cache_get(key)
    if cached is not None:
        await query.answer(results=cached, is_personal=True, cache_time=2)
        return

    if len(raw) < 2:
        await query.answer(
            results=[_hint_article(None, "Scrivi altre lettere per trovare un giocatore…")],
            is_personal=True,
            cache_time=2,
        )
        return

    users = await _search_users(db_session, raw, limit=_RESULT_LIMIT)

    if not users:
        await query.answer(
            results=[_hint_article(
                key,
                f"Nessun giocatore trovato per «{raw}».",
                message_text=f"Nessun giocatore trovato per «{esc(raw)}».",
            )],
            is_personal=True,
            cache_time=2,
        )
        return

    articles = [_user_article(u) for u in users]
    _cache_set(key, articles)
    await query.answer(results=articles, is_personal=True, cache_time=2)
