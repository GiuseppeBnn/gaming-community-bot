from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import async_session_maker
from database.models import User as DbUser
from database.models import Wallet


class DbSessionMiddleware(BaseMiddleware):
    """
    Injects `db_session: AsyncSession` into every handler's data dict.

    Also upserts the Telegram user into the `users` + `wallets` tables so
    every handler can safely assume the current user exists in the DB.
    Uses a portable select + add pattern (works with both SQLite and PostgreSQL).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with async_session_maker() as session:
            data["db_session"] = session

            tg_user: User | None = data.get("event_from_user")
            if tg_user is not None and not tg_user.is_bot:
                await _upsert_user(session, tg_user)

            return await handler(event, data)


async def _upsert_user(session: AsyncSession, tg_user: User) -> None:
    result = await session.execute(
        select(DbUser).where(DbUser.tg_id == tg_user.id)
    )
    user = result.scalar_one_or_none()
    full_name = (tg_user.full_name or tg_user.first_name or "").strip()

    if user is None:
        user = DbUser(
            tg_id=tg_user.id,
            username=tg_user.username,
            full_name=full_name,
        )
        session.add(user)
        session.add(Wallet(tg_id=tg_user.id, coins=0))
        await session.commit()
        return

    changed = False
    if user.username != tg_user.username:
        user.username = tg_user.username
        changed = True
    if user.full_name != full_name:
        user.full_name = full_name
        changed = True
    if changed:
        await session.commit()

    # Ensure wallet exists (defensive — should always exist after first upsert)
    wallet_result = await session.execute(
        select(Wallet).where(Wallet.tg_id == tg_user.id)
    )
    if wallet_result.scalar_one_or_none() is None:
        session.add(Wallet(tg_id=tg_user.id, coins=0))
        await session.commit()
