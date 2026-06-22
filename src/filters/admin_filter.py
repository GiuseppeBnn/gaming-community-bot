"""
Admin authorization.

A user is a bot-admin if their id is in `settings.admin_ids` OR they are a
Telegram administrator/creator of the configured group. The Telegram admin set
is cached per group with a short TTL. On API error we **fail closed** (fall
back to `admin_ids` only) — never grant elevated powers on uncertainty.
"""

from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from config_data.config import settings
from services import group_registry

log = logging.getLogger(__name__)

_ADMIN_CACHE_TTL = 300  # seconds
_cache: dict[int, tuple[set[int], float]] = {}


async def _telegram_admin_ids(bot: Bot, group_id: int) -> set[int]:
    now = time.monotonic()
    cached = _cache.get(group_id)
    if cached is not None and now - cached[1] < _ADMIN_CACHE_TTL:
        return cached[0]
    try:
        members = await bot.get_chat_administrators(group_id)
    except Exception:
        # Fail closed for THIS request, but do NOT cache the failure: a transient
        # API error must not lock the group owner/admins out for the whole TTL.
        return set()
    ids = {m.user.id for m in members if m.user is not None}
    # Guard against legacy *basic* groups with "All members are administrators"
    # ON: there Telegram reports EVERY member as an administrator, which would
    # silently elevate every member to bot-admin. If the admin list spans the
    # whole group, it carries no authority → trust only the .env owner. This is a
    # no-op for normal groups, where admins are always a strict subset.
    if ids and await _all_members_are_admins(bot, group_id, len(members)):
        log.warning(
            "Gruppo %s: tutti i membri risultano amministratori ('Tutti i membri "
            "sono amministratori' attivo). Ignoro la lista admin di Telegram: solo "
            "ADMIN_IDS dell'.env sono admin del bot. Converti il gruppo in "
            "supergruppo o disattiva quell'opzione per riconoscere gli admin del gruppo.",
            group_id,
        )
        ids = set()
    _cache[group_id] = (ids, now)
    return ids


async def _all_members_are_admins(bot: Bot, group_id: int, admin_count: int) -> bool:
    """True when the administrator list spans the entire group (the legacy
    "all members are admins" basic-group mode). Conservative: needs ≥3 members and
    a successful member-count read, otherwise returns False (keep normal behavior)."""
    try:
        total = await bot.get_chat_member_count(group_id)
    except Exception:
        return False
    return total >= 3 and admin_count >= total


async def is_admin(bot: Bot, user_id: int) -> bool:
    if user_id in settings.admin_ids:
        return True
    group_id = group_registry.get_group_id()
    if group_id == 0:
        return False
    return user_id in await _telegram_admin_ids(bot, group_id)


def invalidate_admin_cache(group_id: int | None = None) -> None:
    """Drop the cached Telegram admin set (call on admin promotion/demotion)."""
    if group_id is None:
        _cache.clear()
    else:
        _cache.pop(group_id, None)


class IsAdminFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and await is_admin(
            message.bot, message.from_user.id
        )


class IsAdminCallbackFilter(Filter):
    async def __call__(self, callback: CallbackQuery) -> bool:
        return callback.from_user is not None and await is_admin(
            callback.bot, callback.from_user.id
        )
