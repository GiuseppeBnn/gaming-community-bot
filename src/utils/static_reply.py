"""
Anti-flood for *static* group commands (e.g. /profilo, /saldo, /comandi).

Strategy (hybrid, see STEERING §14): keep **one live bot reply per
(chat, user, command)** — before sending a fresh copy, delete this user's
previous reply for that command, so spamming the command can't pile up a wall of
duplicates. Pair it with a light per-command cooldown (``utils.cooldown.ready``,
silent) so rapid repeats are ignored without adding their own noise.

Only deduplicates in groups: private chats are 1:1, nothing to dedup. Only the
bot's own *replies* are deleted — never the user's command messages (we assume no
delete-messages permission on members).

State is in-memory (resets on restart — fine for spam control) and self-prunes so
a busy group can't grow it without bound.
"""

from __future__ import annotations

from aiogram.enums import ChatType
from aiogram.types import Message

# (chat_id, user_id, bucket) -> message_id of this user's last live reply.
_last: dict[tuple[int, int, str], int] = {}
_PRUNE_THRESHOLD = 2048

_GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


def _prune() -> None:
    """Cheap bound: once the map grows past the threshold, drop the oldest-inserted
    half (dict preserves insertion order)."""
    if len(_last) <= _PRUNE_THRESHOLD:
        return
    excess = len(_last) - _PRUNE_THRESHOLD // 2
    for key in list(_last)[:excess]:
        _last.pop(key, None)


async def reply_static(message: Message, text: str, bucket: str, **send_kwargs) -> Message:
    """Reply to a static command. In a group, first delete this user's previous
    reply for ``bucket`` (one live copy per user+command); in private, just send.
    Extra kwargs (e.g. ``reply_markup``) pass through to ``message.answer``.
    Returns the sent Message."""
    chat = message.chat
    if chat.type not in _GROUP_TYPES or message.from_user is None:
        return await message.answer(text, **send_kwargs)

    key = (chat.id, message.from_user.id, bucket)
    prev = _last.pop(key, None)
    if prev is not None and message.bot is not None:
        try:
            await message.bot.delete_message(chat.id, prev)
        except Exception:  # noqa: BLE001 — message may be gone / too old to delete
            pass
    sent = await message.answer(text, **send_kwargs)
    _last[key] = sent.message_id
    _prune()
    return sent


def reset() -> None:
    """Clear all tracked replies — test helper."""
    _last.clear()
