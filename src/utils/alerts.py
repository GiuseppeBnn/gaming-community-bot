"""
Telegram alert channel for the maintainer: every problem the bot already logs,
delivered to the admins' private chats.

Wired as a **logging handler** rather than as a `notify_admins()` helper called
by hand. Every `log.warning`/`log.error`/`log.exception` already written — the
global error handler, the backup loop, the scheduler, the Redis degradation of
`main._build_storage` — becomes an alert without any of those files knowing this
module exists. A helper would have covered only the call sites someone
remembered to add, and «remembered to add» is how a channel like this rots.

Three constraints shape everything here:

* **`emit()` never does I/O.** Logging is synchronous and is called from inside
  handlers; a send in `emit` would block the event loop on every log line. It
  appends to a bounded buffer and returns.
* **The sender never logs.** A delivery failure that reached the logger would
  come straight back into this buffer and the bot would feed itself alerts
  forever. Delivery failures are counted in memory instead, and reported with
  the next successful drain.
* **Repeats are deduplicated by template.** A group announcement failing in a
  loop is one warning every few seconds — exactly the flood that makes an alert
  channel worth ignoring. Repeats are counted and reported, not dropped.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from config_data.config import settings

# An alert storm must cost the memory we agreed to spend, not all of it.
_MAX_BUFFERED = 200
# Two identical alerts inside this window are one alert plus a count.
_DEDUP_WINDOW_SECONDS = 300.0
# Telegram refuses over 4096 characters; leave room for the header.
_MAX_TEXT = 3500

_buffer: deque[logging.LogRecord] = deque()
_dropped = 0
# fingerprint → (monotonic time of last delivery, repeats suppressed since then)
_seen: dict[tuple[str, str], tuple[float, int]] = {}
# Delivery failures are counted, never logged — see the module docstring.
_undelivered = 0

# Formatting a traceback is all we borrow from the logging machinery.
_formatter = logging.Formatter()


def _min_level() -> int:
    """Threshold from config, falling back to WARNING on anything unreadable.

    A typo in `.env` must not silence the channel: a level nobody can parse is
    the one case where guessing beats refusing.
    """
    level = logging.getLevelName(settings.alert_min_level.upper())
    return level if isinstance(level, int) else logging.WARNING


class TelegramAlertHandler(logging.Handler):
    """Buffers records for `alert_loop`. Does no I/O, so it cannot block."""

    def emit(self, record: logging.LogRecord) -> None:
        global _dropped
        if record.name.startswith(__name__):
            # Our own failures must never become alerts about themselves.
            return
        if len(_buffer) >= _MAX_BUFFERED:
            _dropped += 1
            return
        _buffer.append(record)


def _fingerprint(record: logging.LogRecord) -> tuple[str, str]:
    """Group by **template**, not by formatted message: «Annuncio round %s
    fallito» is one problem whether it fires for round 7 or round 8."""
    return (record.name, str(record.msg))


def _should_send(fingerprint: tuple[str, str], now: float) -> tuple[bool, int]:
    """(send?, repeats suppressed since the last delivery of this fingerprint)."""
    last, suppressed = _seen.get(fingerprint, (0.0, 0))
    if last and now - last < _DEDUP_WINDOW_SECONDS:
        _seen[fingerprint] = (last, suppressed + 1)
        return False, 0
    _seen[fingerprint] = (now, 0)
    return True, suppressed


def format_alert(record: logging.LogRecord, suppressed: int = 0) -> str:
    """Plain text, never HTML.

    A traceback is full of characters that HTML mode would reject, and one
    missed escape would turn an alert about a bug into a bug of its own. Sent
    with `parse_mode=None`, like the AI commands' output (STEERING §17).
    """
    parts = [f"[{record.levelname}] {record.name}", record.getMessage()]
    if record.exc_info:
        parts.append(_formatter.formatException(record.exc_info))
    if suppressed:
        parts.append(
            f"(+{suppressed} ripetizioni soppresse negli ultimi "
            f"{int(_DEDUP_WINDOW_SECONDS)}s)"
        )
    text = "\n".join(parts)
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "\n…(troncato)"
    return text


def install() -> TelegramAlertHandler:
    """Attach the handler to the root logger. Called once, from `main()`."""
    handler = TelegramAlertHandler()
    handler.setLevel(_min_level())
    logging.getLogger().addHandler(handler)
    return handler


def reset() -> None:
    """Clear buffer and dedup state — test helper, mirrors `utils.cooldown.reset`."""
    global _dropped, _undelivered
    _buffer.clear()
    _seen.clear()
    _dropped = 0
    _undelivered = 0


# How often the loop looks at the buffer. Alerts are not interactive: a couple of
# seconds of latency costs nothing and keeps the loop cheap.
_POLL_INTERVAL_SECONDS = 2.0


async def _deliver(bot: Any, text: str) -> None:
    """Best effort, one message per admin. Never raises, **never logs**.

    `bot` is typed loosely on purpose: this module must stay importable and
    testable without dragging aiogram's Bot into a unit test.
    """
    global _undelivered
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
        except Exception:  # noqa: BLE001 — logging here would feed the buffer
            _undelivered += 1


async def _flush_housekeeping(bot: Any) -> int:
    """Report what this module itself lost: dropped alerts, failed deliveries.

    Two orderings matter here, and both are load-bearing:

    * **Rate-limited before anything else.** A failed delivery increments
      `_undelivered`, which would make the next tick try again — every two
      seconds, forever, while the channel is down. Running it through the same
      dedup window as any other repeat caps that at one attempt per window, and
      leaves the counters accumulating in the meantime rather than lost.
    * **Counters reset before the send.** Otherwise a housekeeping message that
      fails to deliver counts its own failure and re-reports itself.
    """
    global _dropped, _undelivered
    if not _dropped and not _undelivered:
        return 0
    send, _ = _should_send((__name__, "housekeeping"), time.monotonic())
    if not send:
        return 0
    dropped, undelivered = _dropped, _undelivered
    _dropped = _undelivered = 0
    lines = [f"[WARNING] {__name__}"]
    if dropped:
        lines.append(f"{dropped} alert scartati: buffer pieno ({_MAX_BUFFERED}).")
    if undelivered:
        lines.append(f"{undelivered} consegne fallite.")
    await _deliver(bot, "\n".join(lines))
    return 1


async def drain(bot: Any) -> int:
    """Send whatever is buffered, deduplicated. Returns how many alerts went out."""
    sent = 0
    now = time.monotonic()
    while _buffer:
        record = _buffer.popleft()
        send, suppressed = _should_send(_fingerprint(record), now)
        if not send:
            continue
        await _deliver(bot, format_alert(record, suppressed))
        sent += 1
    return sent + await _flush_housekeeping(bot)


async def alert_loop(bot: Any) -> None:
    """Endless background loop started from `main()`. Never raises out.

    Mirrors `services.backup.loop.backup_loop`, minus its logging: an exception
    logged from inside the alert path is the one thing that could turn this into
    a feedback loop.
    """
    while True:
        try:
            await drain(bot)
        except Exception:  # noqa: BLE001 — the loop must never die, nor log
            pass
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
