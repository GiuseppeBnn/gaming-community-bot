"""
Shared target-resolution helper for admin/economy commands.

Resolves the target user of a command from, in priority order:
  1. the replied-to message's author (fastest in a busy group)
  2. a text_mention entity (users without a @username)
  3. a @username token
  4. a numeric Telegram id

`user` is the DB row when the user is known (required for currency ops, which
need a wallet); moderation commands only need `tg_id`. `remainder` holds the
leftover args (e.g. amount / reason) after the target token is stripped.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.enums import MessageEntityType
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from utils.text import esc


@dataclass
class ResolvedTarget:
    tg_id: int | None
    user: User | None
    display_name: str
    remainder: str


def _display(user: User | None, tg_id: int | None, fallback: str | None = None) -> str:
    # display_name is always interpolated into HTML replies → escape here once.
    if user is not None:
        return f"@{esc(user.username)}" if user.username else esc(user.full_name)
    if fallback:
        return esc(fallback)
    return f"ID {tg_id}" if tg_id is not None else "utente"


async def _lookup(session: AsyncSession, *, tg_id: int | None = None, username: str | None = None) -> User | None:
    if tg_id is not None:
        stmt = select(User).where(User.tg_id == tg_id)
    elif username is not None:
        stmt = select(User).where(User.username == username)
    else:
        return None
    return (await session.execute(stmt)).scalar_one_or_none()


async def resolve_target(
    message: Message, session: AsyncSession, args: str | None
) -> ResolvedTarget | None:
    """Resolve the command target. Returns None if no target could be determined."""
    args = (args or "").strip()

    # 1. Reply to a message — the author is the target; args are all remainder.
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and not reply.from_user.is_bot:
        author = reply.from_user
        user = await _lookup(session, tg_id=author.id)
        return ResolvedTarget(
            tg_id=author.id,
            user=user,
            display_name=_display(user, author.id, author.full_name),
            remainder=args,
        )

    # 2. text_mention entity (user without username).
    for entity in message.entities or []:
        if entity.type == MessageEntityType.TEXT_MENTION and entity.user is not None:
            mentioned = entity.user
            user = await _lookup(session, tg_id=mentioned.id)
            mention_text = (message.text or "")[entity.offset: entity.offset + entity.length]
            remainder = args.replace(mention_text, "", 1).strip()
            return ResolvedTarget(
                tg_id=mentioned.id,
                user=user,
                display_name=_display(user, mentioned.id, mentioned.full_name),
                remainder=remainder,
            )

    if not args:
        return None

    # 3 & 4. First token is @username or numeric id.
    token, _, remainder = args.partition(" ")
    remainder = remainder.strip()

    if token.startswith("@"):
        username = token[1:]
        user = await _lookup(session, username=username)
        tg_id = user.tg_id if user else None
        return ResolvedTarget(tg_id=tg_id, user=user, display_name=_display(user, tg_id, token), remainder=remainder)

    if token.lstrip("-").isdigit():
        tg_id = int(token)
        user = await _lookup(session, tg_id=tg_id)
        return ResolvedTarget(tg_id=tg_id, user=user, display_name=_display(user, tg_id), remainder=remainder)

    return None
