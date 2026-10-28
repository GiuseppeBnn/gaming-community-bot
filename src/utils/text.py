"""
Presentation-layer text helpers.

Every user-controlled string interpolated into a ParseMode.HTML message MUST go
through esc() — services and the DB keep raw values, escaping happens only at
render time.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

# Count UTF-16 units conservatively, including HTML markup. This also bounds
# emoji-heavy messages, where a Python character may take two Telegram units.
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
    sep_len = len(sep.encode("utf-16-le")) // 2
    for block in blocks:
        block_len = len(block.encode("utf-16-le")) // 2
        add_len = block_len + (sep_len if current else 0)
        if current and current_len + add_len > limit:
            chunks.append(sep.join(current))
            current = [block]
            current_len = block_len
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


def too_long(text: str, cap: int, subject: str) -> str | None:
    """Italian length error for a free-text field, including how much to cut."""
    length = len(text)
    if length <= cap:
        return None
    return (
        f"⚠️ {subject}: <b>{length}/{cap}</b> caratteri.\n"
        f"Accorcia di {length - cap} e reinvia."
    )
