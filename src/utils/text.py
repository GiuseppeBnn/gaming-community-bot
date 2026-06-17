"""
Presentation-layer text helpers.

Every user-controlled string interpolated into a ParseMode.HTML message MUST go
through esc() — services and the DB keep raw values, escaping happens only at
render time.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

# Telegram rejects a single message longer than 4096 chars ("Bad Request:
# message is too long"). We chunk well below it: HTML tags in our text are
# stripped before Telegram counts length (so len() over-estimates, which is
# safe), but emoji cost 2 UTF-16 units while Python len() counts 1, so we keep
# a margin to stay safe even on emoji-dense listings.
TELEGRAM_MESSAGE_LIMIT = 4096
_CHUNK_LIMIT = 3900


def esc(value: object, limit: int | None = None) -> str:
    """HTML-escape any user-controlled value, with optional hard truncation."""
    text = "" if value is None else str(value)
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return html.escape(text)


def chunk_blocks(
    blocks: Iterable[str], sep: str = "\n", limit: int = _CHUNK_LIMIT
) -> list[str]:
    """Group pre-rendered text blocks into Telegram-sized messages.

    Each ``block`` is treated as atomic and never split (so an HTML tag is never
    cut mid-way, which would break ``ParseMode.HTML``). Blocks are joined with
    ``sep`` and packed greedily so each returned string stays within ``limit``.
    A single block longer than ``limit`` is emitted on its own (last-resort: it
    is the caller's job to keep individual blocks short).
    """
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    sep_len = len(sep)
    for block in blocks:
        add_len = len(block) + (sep_len if current else 0)
        if current and current_len + add_len > limit:
            chunks.append(sep.join(current))
            current = [block]
            current_len = len(block)
        else:
            current.append(block)
            current_len += add_len
    if current:
        chunks.append(sep.join(current))
    return chunks


def format_duration(seconds: int) -> str:
    """Compact, human-friendly duration in Italian.

    Examples: ``20h`` · ``2h 5min`` · ``45min`` · ``meno di 1 minuto``.
    Replaces the old ``{x:.1f} ore`` decimal-hours rendering, which read as
    "sballato" (e.g. "19.8 ore") to users.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "meno di 1 minuto"
    hours, minutes = divmod(seconds // 60, 60)
    if hours and minutes:
        return f"{hours}h {minutes}min"
    if hours:
        return f"{hours}h"
    return f"{minutes}min"


def format_seconds_short(seconds: int) -> str:
    """Second-precision duration for short spans: ``45s`` · ``1m 30s`` · ``1h 2m``."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
