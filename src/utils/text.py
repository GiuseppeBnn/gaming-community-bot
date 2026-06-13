"""
Presentation-layer text helpers.

Every user-controlled string interpolated into a ParseMode.HTML message MUST go
through esc() — services and the DB keep raw values, escaping happens only at
render time.
"""

from __future__ import annotations

import html


def esc(value: object, limit: int | None = None) -> str:
    """HTML-escape any user-controlled value, with optional hard truncation."""
    text = "" if value is None else str(value)
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return html.escape(text)


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
