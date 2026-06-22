"""
Shared user-mention helper.

Builds an HTML mention for a user, resolved from the DB: ``@username`` when the
user has one, otherwise a clickable ``tg://user?id=…`` link with their full name.
All user-controlled strings pass through ``utils.text.esc`` (STEERING §20).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from utils.text import esc


async def mention(db_session: AsyncSession, tg_id: int) -> str:
    """HTML mention for ``tg_id``: ``@username`` or a tg:// link to the full name."""
    user = (
        await db_session.execute(select(User).where(User.tg_id == tg_id))
    ).scalar_one_or_none()
    if user is None:
        return f'<a href="tg://user?id={tg_id}">giocatore</a>'
    if user.username:
        return f"@{esc(user.username)}"
    return f'<a href="tg://user?id={tg_id}">{esc(user.full_name)}</a>'
