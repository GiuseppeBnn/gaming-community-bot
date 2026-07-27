"""Pieces every part of the guess flow needs.

The `router` lives here so creation, lifecycle and play can register on the same
one — the same arrangement as `handlers/quiz/_shared.py` — and the package
re-exports it, so `handlers/__init__.py` still sees a single router.

`KINDS` is the only place the two games differ: labels, prompts and which media
a round of that kind accepts. Everything downstream reads from here, so adding a
third medium is a dict entry and not a new branch anywhere.

The caps are the single source of truth for every free-text field. The prompts,
the validation and the review step all read them, so the number an admin is told
can never drift from the number actually enforced. Input over a cap is REJECTED
with its real length, never silently truncated — a truncated answer is only
discovered once players are already guessing against it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Router
from aiogram.types import Message

log = logging.getLogger(__name__)
router = Router()

_MAX_TITLE = 256
_MAX_ANSWER = 200
_MAX_ALIAS = 100
_MAX_ALIASES = 20
_MAX_HINT = 200
_MAX_HINTS = 10
_MAX_ATTEMPTS_ALLOWED = 20
_MAX_TIME_LIMIT = 3600

#: Guess rounds are managed by admins in private, like quizzes.
_GUESS_PRIVATE_NOTICE = "🎮 Gestisci questi eventi in chat privata col bot."


@dataclass(frozen=True)
class KindSpec:
    """Everything that differs between the two games. Nothing else does."""

    key: str              # event-type key, ScheduledTask.task_type AND trophy game_key
    label: str
    emoji: str
    create_label: str
    media_prompt: str
    accepted_media: tuple[str, ...]   # the media_kind values a round of this kind takes


KINDS: dict[str, KindSpec] = {
    "guess": KindSpec(
        key="guess",
        label="Guess The Game",
        emoji="🖼️",
        create_label="➕ Crea Guess The Game",
        media_prompt="Manda la <b>foto</b> da indovinare.",
        accepted_media=("photo",),
    ),
    "sound": KindSpec(
        key="sound",
        label="Sound Quest",
        emoji="🔊",
        create_label="➕ Crea Sound Quest",
        media_prompt="Manda l'<b>audio</b> da indovinare (file audio o vocale).",
        accepted_media=("audio", "voice"),
    ),
}


def kind_of(key: str) -> KindSpec:
    """The spec for a kind. Raises ``KeyError`` on an unknown one: a typo in a
    callback must fail loudly here, not quietly render the wrong game."""
    return KINDS[key]


def too_long(text: str, cap: int, subject: str) -> str | None:
    """Error message if `text` exceeds `cap`, else None. `subject` is the already
    gender-agreed Italian phrase. Reports the real length and how much to cut, so
    the admin doesn't have to count."""
    if len(text) <= cap:
        return None
    return (
        f"⚠️ {subject}: <b>{len(text)}/{cap}</b> caratteri.\n"
        f"Accorcia di {len(text) - cap} e reinvia."
    )


def extract_media(message: Message) -> tuple[str, str] | None:
    """``(file_id, media_kind)`` from an incoming message, or None if it carries
    none of the supported media.

    Photos arrive as a size ladder and the last entry is the largest — that is
    the one worth storing, because it is what players will be squinting at.
    """
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.audio:
        return message.audio.file_id, "audio"
    if message.voice:
        return message.voice.file_id, "voice"
    return None


async def send_media(bot, chat_id: int, file_id: str, media_kind: str, **kwargs) -> None:
    """Resend a stored medium.

    The one place that maps ``media_kind`` → Bot API call, shared by the creation
    preview, the play screen and the podium reveal. Three call sites doing their
    own mapping is three chances to add a fourth medium in two of them.
    """
    sender = {
        "photo": bot.send_photo,
        "audio": bot.send_audio,
        "voice": bot.send_voice,
    }[media_kind]
    await sender(chat_id, file_id, **kwargs)
