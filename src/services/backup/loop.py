"""
Background backup driver — exports DB state and extends the chat archive on a
cadence, without ever blocking handlers or crashing the bot.

Mirrors ``handlers.schedule.scheduler_loop``: an endless loop wrapped in a total
``try/except`` (a backup failure logs and is retried next tick — it never kills
the process). Due-ness is derived from the artifacts on disk (snapshot mtime /
manifest ``updated_at``), so no extra DB table is needed and the schedule
survives restarts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from config_data.config import settings
from database.connection import async_session_maker
from services import group_registry
from services.backup import chat_archive, state_export
from utils import atomic_io

log = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 1800  # how often to re-evaluate due-ness (cadence is in hours)


def _hours_since(when: datetime | None) -> float | None:
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600.0


def _due_since(last: datetime | None, interval_hours: int) -> bool:
    elapsed = _hours_since(last)
    return elapsed is None or elapsed >= interval_hours


def _state_last_run(backup_dir: str) -> datetime | None:
    latest = state_export.latest_snapshot_path(backup_dir)
    if not latest.exists():
        return None
    return datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)


def _chat_last_run(backup_dir: str) -> datetime | None:
    iso = chat_archive.last_run_iso(backup_dir)
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


async def run_due_backups() -> None:
    """Run whichever backups are due. Each is independently guarded."""
    backup_dir = settings.backup_dir

    # Pre-flight: a mis-mounted/non-writable backup volume would otherwise blow up
    # on every write with an EACCES stack trace. Degrade to one clear warning and
    # skip this tick (the loop retries on the next cadence).
    reason = atomic_io.probe_writable(backup_dir)
    if reason is not None:
        log.warning(
            "Backup saltato: cartella «%s» non scrivibile — %s. "
            "Controlla i permessi del volume montato su /app/%s (vedi STEERING §25).",
            backup_dir, reason, backup_dir,
        )
        return

    if _due_since(_state_last_run(backup_dir), settings.backup_state_interval_hours):
        try:
            async with async_session_maker() as session:
                path = await state_export.export_state(session, backup_dir)
            log.info("State export scritto: %s", Path(path).name)
        except Exception:  # noqa: BLE001 — never let one backup sink the loop
            log.exception("State export fallito")

    if chat_archive.is_enabled() and _due_since(
        _chat_last_run(backup_dir), settings.backup_chat_interval_hours
    ):
        group_id = group_registry.get_group_id()
        if not group_id:
            log.warning("Chat backup saltato: GROUP_ID non configurato.")
            return
        try:
            result = await chat_archive.run_chat_backup(group_id=group_id)
            log.info("Chat backup: +%d messaggi (totale file %d byte).", result.added, result.file_size)
        except Exception:  # noqa: BLE001
            log.exception("Chat backup fallito")


async def backup_loop() -> None:
    """Endless background loop started from main(). Never raises out."""
    log.info(
        "Backup loop avviato (stato ogni %sh, chat ogni %sh, archivio chat %s).",
        settings.backup_state_interval_hours,
        settings.backup_chat_interval_hours,
        "attivo" if chat_archive.is_enabled() else "disattivo",
    )
    while True:
        try:
            await run_due_backups()
        except Exception:  # noqa: BLE001 — loop must never die
            log.exception("Backup loop error")
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
