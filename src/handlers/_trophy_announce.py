"""
Trophy announcements — broadcast newly unlocked trophies to the group.

Every trophy/objective a user unlocks is announced **in the group**, tagging the
user ("@utente ha ottenuto il trofeo …"), instead of a private DM. Best-effort and
non-blocking: a send failure (bot not in group, no group configured) is swallowed.

Call this AFTER the handler's commit — the awarding session must be persisted first
(the Badge objects stay readable because ``expire_on_commit=False``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Badge
from handlers._mentions import mention
from services import group_registry
from utils.text import esc

log = logging.getLogger(__name__)


async def announce_trophies(
    bot: Bot, db_session: AsyncSession, tg_id: int, badges: Sequence[Badge]
) -> None:
    """Announce ``badges`` newly unlocked by ``tg_id`` in the group. Best-effort."""
    if not badges or group_registry.get_group_id() == 0:
        return
    who = await mention(db_session, tg_id)
    names = ", ".join(f"{b.icon_emoji} <b>{esc(b.name)}</b>" for b in badges)
    if len(badges) == 1:
        text = f"🏅 {who} ha ottenuto il trofeo {names}! 🎉"
    else:
        text = f"🏅 {who} ha ottenuto i trofei: {names}! 🎉"
    try:
        await group_registry.send_group_message(bot, db_session, text)
    except Exception:  # noqa: BLE001 — never let a notification break the flow
        log.warning("Annuncio trofeo nel gruppo fallito per %s", tg_id)
