"""Best-effort capture of ordinary group text for Alduino's local memory."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import TelegramObject
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from config_data.config import settings
from database.connection import async_session_maker
from services import group_context, group_registry

log = logging.getLogger(__name__)

_GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


class GroupContextMiddleware(BaseMiddleware):
    """Persist a bounded transcript before downstream handlers inspect it.

    Its short independent transaction never spans an LLM call and failures are
    intentionally non-fatal: conversational memory may degrade, the bot may not.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message = getattr(event, "message", None) or getattr(event, "edited_message", None)
        if settings.alduino_capture_group_context and message is not None:
            await self._capture(message)
        return await handler(event, data)

    async def _capture(self, message: Any) -> None:
        chat = getattr(message, "chat", None)
        author = getattr(message, "from_user", None)
        if (
            chat is None
            or getattr(chat, "type", None) not in _GROUP_TYPES
            or author is None
            or getattr(author, "is_bot", False)
        ):
            return
        configured_group = group_registry.get_group_id()
        if configured_group and chat.id != configured_group:
            return
        text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
        # Commands are control-plane noise and the current /alduino text is sent
        # separately; retaining them would duplicate it and waste context.
        if not text or text.startswith("/"):
            return
        target = getattr(message, "reply_to_message", None)
        try:
            async with async_session_maker() as session:
                await group_context.record_message(
                    session,
                    group_id=chat.id,
                    message_id=message.message_id,
                    user_tg_id=author.id,
                    display_name=author.full_name or author.first_name or "Utente",
                    username=author.username,
                    text=text,
                    reply_to_message_id=getattr(target, "message_id", None),
                )
                await session.commit()
        except IntegrityError:
            # Duplicate Telegram delivery/edit: the original row is enough.
            return
        except SQLAlchemyError:
            log.exception("Memoria chat di Alduino non aggiornata; continuo senza bloccare l'update.")
