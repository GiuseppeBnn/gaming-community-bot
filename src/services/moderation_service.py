"""
Moderation service — Telegram-side wrappers (no DB).

Each action returns (success: bool, reason: str) with human-readable, mapped
error messages. The bot must be an admin of the target chat with the relevant
permissions.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import ChatPermissions

_DEFAULT_MUTE_SECONDS = 3600
_MAX_DURATION_SECONDS = 366 * 24 * 3600  # Telegram treats >366d / <30s as "forever"

_MUTED_PERMS = ChatPermissions(
    can_send_messages=False,
    can_send_media_messages=False,
    can_send_polls=False,
    can_send_other_messages=False,
)
_UNMUTED_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
)

_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def looks_like_duration(text: str | None) -> bool:
    """True if `text` is a duration token like '10m'/'1h'/'2d'/'45s'."""
    return bool(text) and _DURATION_RE.match(text.strip()) is not None


def parse_duration(text: str | None, default: int = _DEFAULT_MUTE_SECONDS) -> int:
    """Parse '10m'/'1h'/'2d'/'45s' into seconds. Falls back to `default`."""
    if not text:
        return default
    match = _DURATION_RE.match(text.strip())
    if not match:
        return default
    value, unit = int(match.group(1)), match.group(2).lower()
    return min(value * _UNIT_SECONDS[unit], _MAX_DURATION_SECONDS)


def _map_error(err: str) -> str:
    low = err.lower()
    if "not enough rights" in low or "chat_admin_required" in low:
        return "Il bot non ha i permessi necessari (deve essere admin del gruppo)."
    if "user is an administrator" in low or "can't restrict" in low or "can't remove" in low:
        return "Non posso agire su un admin con rank pari o superiore al mio."
    if "user not found" in low or "user_not_participant" in low or "participant_id_invalid" in low:
        return "L'utente non è nel gruppo."
    return f"Errore Telegram: {err[:100]}"


async def ban(bot: Bot, chat_id: int, tg_id: int) -> tuple[bool, str]:
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=tg_id)
        return True, ""
    except Exception as e:  # noqa: BLE001 — map all Telegram errors to a reason
        return False, _map_error(str(e))


async def unban(bot: Bot, chat_id: int, tg_id: int) -> tuple[bool, str]:
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=tg_id, only_if_banned=True)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, _map_error(str(e))


async def kick(bot: Bot, chat_id: int, tg_id: int) -> tuple[bool, str]:
    """Ban then immediately unban so the user can rejoin later."""
    ok, reason = await ban(bot, chat_id, tg_id)
    if not ok:
        return ok, reason
    await unban(bot, chat_id, tg_id)  # best-effort lift of the ban
    return True, ""


async def mute(bot: Bot, chat_id: int, tg_id: int, duration_seconds: int) -> tuple[bool, str]:
    until_ts = int(
        (datetime.now(tz=timezone.utc) + timedelta(seconds=duration_seconds)).timestamp()
    )
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=tg_id, permissions=_MUTED_PERMS, until_date=until_ts
        )
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, _map_error(str(e))


async def unmute(bot: Bot, chat_id: int, tg_id: int) -> tuple[bool, str]:
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=tg_id, permissions=_UNMUTED_PERMS
        )
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, _map_error(str(e))
