"""
Shared user-mention helper.

Builds an HTML mention for a user, resolved from the DB: ``@username`` when the
user has one, otherwise a clickable ``tg://user?id=…`` link with their full name.
All user-controlled strings pass through ``utils.text.esc`` (STEERING §20).
"""

from __future__ import annotations

import unicodedata

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
        return _render(None, tg_id)
    return _render(user, tg_id)


async def mention_many(
    db_session: AsyncSession, tg_ids: list[int], *, prefer_name: bool = False,
) -> dict[int, str]:
    """Resolve a complete ranking in one query instead of one per player."""
    if not tg_ids:
        return {}
    users = (await db_session.execute(select(User).where(User.tg_id.in_(tg_ids)))).scalars().all()
    by_id = {user.tg_id: user for user in users}
    return {tg_id: _render(by_id.get(tg_id), tg_id, prefer_name=prefer_name) for tg_id in tg_ids}


def _render(user: User | None, tg_id: int, *, prefer_name: bool = False) -> str:
    if user is None:
        return f'<a href="tg://user?id={tg_id}">giocatore</a>'
    if prefer_name:
        name = " ".join((user.full_name or "").split())
        if not any(
            ch.isprintable() and not ch.isspace()
            and unicodedata.category(ch)[0] != "C" and ch not in "\u2800\u3164\uffa0\u115f\u1160"
            for ch in name
        ):
            name = f"@{user.username}" if user.username else "giocatore"
        return f'<a href="tg://user?id={tg_id}">{esc(name, 80)}</a>'
    if user.username:
        return f"@{esc(user.username)}"
    return f'<a href="tg://user?id={tg_id}">{esc(user.full_name)}</a>'
