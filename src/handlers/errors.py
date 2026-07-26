"""
Global fallback for exceptions that escape a handler.

Without this, an unhandled exception dies in the log and the user just sees a
silent bot: no reply, and for a callback the button keeps its loading spinner
until Telegram times it out. Registered on the **Dispatcher** (``dp.errors``)
rather than a router, so it covers every one of the registered handlers.

Two deliberate non-goals:

* **No session rollback here.** ``DbSessionMiddleware`` opens the session with
  ``async with async_session_maker()``, and leaving that block closes the session,
  which discards any uncommitted transaction. A handler that raised before its
  ``commit()`` has already left nothing behind — an explicit rollback here would
  be dead code operating on a session this function cannot reach anyway.
* **No re-raise.** Returning True marks the update handled so aiogram's polling
  loop keeps going.

The bot is Italian-facing, so the user-visible string is Italian; everything
aimed at the maintainer (log lines) stays English like the rest of the code.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ErrorEvent

log = logging.getLogger(__name__)

router = Router(name="errors")

_USER_MESSAGE = (
    "⚠️ Qualcosa è andato storto. L'errore è stato registrato — riprova tra poco."
)

# Telegram rejects these for reasons that are normal in a callback-heavy bot and
# say nothing about our code: re-rendering a keyboard into identical content, or
# a user tapping a button on a message old enough that the query expired. They
# are noise at WARNING and must never produce a scary message to the user.
_BENIGN_FRAGMENTS = (
    "message is not modified",
    "query is too old",
    "message to edit not found",
    "message can't be deleted",
)


def _is_benign(exc: Exception) -> bool:
    if not isinstance(exc, TelegramBadRequest):
        return False
    text = str(exc).lower()
    return any(fragment in text for fragment in _BENIGN_FRAGMENTS)


async def on_error(event: ErrorEvent) -> bool:
    """Log an escaped exception with enough context to act on, then tell the user.

    Returns True (update handled) so polling continues.
    """
    update = event.update
    message = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    user = update.event_from_user
    callback_data = update.callback_query.data if update.callback_query else None

    if _is_benign(event.exception):
        log.debug("Benign Telegram rejection on callback %s: %s", callback_data, event.exception)
        # Still stop the button's spinner, or the UI looks stuck.
        if update.callback_query is not None:
            try:
                await update.callback_query.answer()
            except Exception:  # noqa: BLE001 — best effort; the query may be expired
                pass
        return True

    log.exception(
        "Unhandled exception in handler | user_id=%s username=%s chat_id=%s "
        "callback_data=%s text=%r",
        user.id if user else None,
        user.username if user else None,
        message.chat.id if message else None,
        callback_data,
        (update.message.text[:120] if update.message and update.message.text else None),
        exc_info=event.exception,
    )

    # Telling the user is best-effort: the failure may itself be the send path
    # (bot blocked, chat gone), and a raise here would replace a logged bug with
    # an unlogged one.
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(_USER_MESSAGE, show_alert=True)
        elif message is not None:
            await message.answer(_USER_MESSAGE)
    except Exception:  # noqa: BLE001 — nothing useful left to do
        log.warning("Could not deliver the error notice to the user.")

    return True
