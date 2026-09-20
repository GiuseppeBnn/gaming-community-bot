"""Bounded, provider-neutral memory of ordinary group conversation."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import AlduinoGroupMessage

_MAX_MESSAGE_CHARS = 1500


@dataclass(frozen=True, slots=True)
class ChatLine:
    message_id: int
    display_name: str
    username: str | None
    text: str
    reply_to_message_id: int | None = None


def clip_message(text: str) -> str:
    return (text or "").strip()[:_MAX_MESSAGE_CHARS]


async def record_message(
    session: AsyncSession,
    *,
    group_id: int,
    message_id: int,
    user_tg_id: int,
    display_name: str,
    username: str | None,
    text: str,
    reply_to_message_id: int | None,
) -> bool:
    """Store one human message and prune the rolling group window."""
    value = clip_message(text)
    if not value:
        return False
    exists = (await session.execute(select(AlduinoGroupMessage.id).where(
        AlduinoGroupMessage.group_id == group_id,
        AlduinoGroupMessage.message_id == message_id,
    ))).scalar_one_or_none()
    if exists is not None:
        return False

    session.add(AlduinoGroupMessage(
        group_id=group_id,
        message_id=message_id,
        user_tg_id=user_tg_id,
        display_name=(display_name or "Utente").strip()[:256],
        username=(username or "").strip()[:64] or None,
        reply_to_message_id=reply_to_message_id,
        text=value,
    ))
    await session.flush()
    stale = (
        select(AlduinoGroupMessage.id)
        .where(AlduinoGroupMessage.group_id == group_id)
        .order_by(AlduinoGroupMessage.id.desc())
        .offset(settings.alduino_group_memory_rows)
    )
    await session.execute(
        delete(AlduinoGroupMessage).where(AlduinoGroupMessage.id.in_(stale))
    )
    return True


async def recent_messages(
    session: AsyncSession,
    *,
    group_id: int,
    exclude_message_id: int | None = None,
) -> tuple[ChatLine, ...]:
    """Return a chronological recent slice bounded by rows and rendered chars."""
    query = select(AlduinoGroupMessage).where(AlduinoGroupMessage.group_id == group_id)
    if exclude_message_id is not None:
        query = query.where(AlduinoGroupMessage.message_id != exclude_message_id)
    rows = list((await session.execute(
        query.order_by(AlduinoGroupMessage.id.desc()).limit(
            settings.alduino_group_context_messages
        )
    )).scalars())

    newest_first: list[ChatLine] = []
    used = 0
    for row in rows:
        line = ChatLine(
            message_id=row.message_id,
            display_name=row.display_name,
            username=row.username,
            text=row.text,
            reply_to_message_id=row.reply_to_message_id,
        )
        rendered = render_line(line)
        remaining = settings.alduino_group_context_chars - used
        if remaining <= 0:
            break
        if len(rendered) > remaining:
            if newest_first:
                break
            line = ChatLine(
                line.message_id,
                line.display_name,
                line.username,
                line.text[:remaining],
                line.reply_to_message_id,
            )
            rendered = render_line(line)
        newest_first.append(line)
        used += len(rendered)
    return tuple(reversed(newest_first))


def render_line(line: ChatLine) -> str:
    username = f" (@{line.username})" if line.username else ""
    reply = f" ↪ #{line.reply_to_message_id}" if line.reply_to_message_id is not None else ""
    return f"#{line.message_id}{reply} · {line.display_name}{username}: {line.text}"


def render_context(lines: tuple[ChatLine, ...]) -> str:
    return "\n".join(render_line(line) for line in lines)
