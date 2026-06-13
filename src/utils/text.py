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
