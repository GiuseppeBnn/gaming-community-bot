"""
Runtime "effective group id".

Telegram converts a basic group into a supergroup (with a NEW chat id) when it
is made public — and `settings.group_id` from the .env becomes stale, silently
breaking admin checks and the membership guard. This module is the single
runtime source of truth for the group id:

  - boots as `settings.group_id`
  - updated (and persisted in the `bot_state` table) when the bot observes a
    chat migration (handlers/group_events.py)
  - the persisted override survives restarts, but is discarded if the operator
    has since changed GROUP_ID in the .env

Rule (§22): never read `settings.group_id` at runtime — call get_group_id().
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramMigrateToChat
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from database.models import BotState

log = logging.getLogger(__name__)

_KEY_EFFECTIVE = "effective_group_id"
_KEY_CONFIGURED = "configured_group_id"

# In-memory override; None → fall back to settings.group_id.
_effective: int | None = None


def get_group_id() -> int:
    """The group id to use for every runtime Telegram call."""
    return _effective if _effective is not None else settings.group_id


def set_runtime_group_id(new_id: int | None) -> None:
    """Set (or clear, with None) the in-memory override. Used by handlers/tests."""
    global _effective
    _effective = new_id


async def _get_state(session: AsyncSession, key: str) -> BotState | None:
    return (
        await session.execute(select(BotState).where(BotState.key == key))
    ).scalar_one_or_none()


async def _upsert_state(session: AsyncSession, key: str, value: str) -> None:
    row = await _get_state(session, key)
    if row is None:
        session.add(BotState(key=key, value=value))
    else:
        row.value = value


async def record_migration(session: AsyncSession, old_id: int, new_id: int) -> None:
    """Persist a chat migration and apply it in memory. Does NOT commit (§5)."""
    await _upsert_state(session, _KEY_EFFECTIVE, str(new_id))
    await _upsert_state(session, _KEY_CONFIGURED, str(settings.group_id))
    set_runtime_group_id(new_id)
    log.warning(
        "Group migrated %s → %s. Update GROUP_ID in the .env to make it permanent.",
        old_id, new_id,
    )


async def load(session: AsyncSession) -> int:
    """Startup: apply the persisted migration override, if still relevant.

    If the operator changed GROUP_ID in the .env after the migration was
    recorded, the stored override is stale → delete it and trust the .env.
    Commits its own cleanup (startup context, like sync_trophies).
    """
    set_runtime_group_id(None)
    effective_row = await _get_state(session, _KEY_EFFECTIVE)
    configured_row = await _get_state(session, _KEY_CONFIGURED)

    if effective_row is None:
        return get_group_id()

    if configured_row is None or int(configured_row.value) != settings.group_id:
        await session.delete(effective_row)
        if configured_row is not None:
            await session.delete(configured_row)
        await session.commit()
        log.info("GROUP_ID changed in .env — discarded stale migration override.")
        return get_group_id()

    set_runtime_group_id(int(effective_row.value))
    log.info(
        "Applied persisted group migration: effective group id %s (configured %s).",
        effective_row.value, settings.group_id,
    )
    return get_group_id()


async def send_group_message(
    bot: Bot, session: AsyncSession, text: str, **kwargs: Any
) -> Message:
    """Send a message to the effective group, transparently following a chat
    migration if Telegram reports one (TelegramMigrateToChat).

    The bot may have missed the migration service message (e.g. it was offline),
    so the Bot API error is the safety net: record the new id, commit it, and
    retry once against the migrated chat. Commits its own migration bookkeeping
    (exceptional path, independent of the caller's transaction).
    """
    group_id = get_group_id()
    try:
        return await bot.send_message(group_id, text, **kwargs)
    except TelegramMigrateToChat as exc:
        new_id = exc.migrate_to_chat_id
        await record_migration(session, group_id, new_id)
        await session.commit()
        return await bot.send_message(new_id, text, **kwargs)
