"""
Incremental, crash-safe archive of the group's text messages via MTProto.

The Telegram **Bot API cannot read history**, so the archive is built with a
Telethon *user* session (a sensitive credential — see scripts/login_telethon.py).

Design (all streaming, low-RAM):

  * the archive is **one** file, ``chat-archive.jsonl.gz``, a concatenation of
    gzip members — one per backup range (``gunzip`` reads them transparently);
  * a sidecar ``chat-archive.manifest.json`` records the last *committed* byte
    offset, ``last_message_id`` (the resume cursor), ``anchor_ts`` (the first
    run's cutoff) and the file's sha256;
  * **first run**: archive everything up to ``anchor_ts`` (= the moment of the
    first backup); **next runs**: only messages after ``last_message_id`` up to
    the new cutoff — exactly the "growing single file by date range" model;
  * **recovery**: if a crash left a partial trailing member, the next run
    truncates the file back to ``committed_offset`` before appending → never broken.

The pure core (``build_record``, ``classify_media``, ``_archive_range``) is
Telethon-agnostic and unit-tested with fakes; ``run_chat_backup`` adds the
connect-on-demand MTProto client (disconnected right after — no persistent 2nd
Telegram connection).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from config_data.config import settings
from utils.atomic_io import (
    GzipMemberWriter,
    read_json,
    sha256_file,
    truncate_file,
    write_json_atomic,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_ARCHIVE_NAME = "chat-archive.jsonl.gz"
_MANIFEST_NAME = "chat-archive.manifest.json"

# One backup may run at a time (periodic loop vs /backup command) — they share
# the same file, so serialize them on this in-process lock.
_LOCK = asyncio.Lock()

# Media types are NOT downloaded (the requirement excludes photos/audio). We keep
# a lightweight label so reply chains stay intact and captions are preserved.
_MEDIA_ATTRS: tuple[tuple[str, str], ...] = (
    ("photo", "photo"),
    ("voice", "voice"),
    ("audio", "audio"),
    ("video_note", "video"),
    ("video", "video"),
    ("gif", "gif"),
    ("sticker", "sticker"),
    ("document", "document"),
    ("poll", "poll"),
    ("contact", "contact"),
    ("geo", "geo"),
    ("dice", "dice"),
)


class ChatArchiveError(Exception):
    """A chat-archive run could not complete (config/auth/entity/IO)."""


class BackupDisabledError(ChatArchiveError):
    """Telethon credentials are not configured — the archive is disabled."""


@dataclass
class BackupResult:
    added: int
    last_message_id: int
    last_message_date: str | None
    first_run: bool
    file_size: int
    archive_path: Path


# ---------------------------------------------------------------------------
# Paths / status
# ---------------------------------------------------------------------------

def archive_paths(backup_dir: str | Path) -> tuple[Path, Path]:
    d = Path(backup_dir)
    return d / _ARCHIVE_NAME, d / _MANIFEST_NAME


def manifest_path(backup_dir: str | Path) -> Path:
    return archive_paths(backup_dir)[1]


def last_run_iso(backup_dir: str | Path) -> str | None:
    manifest = read_json(manifest_path(backup_dir))
    return manifest.get("updated_at") if manifest else None


def is_enabled() -> bool:
    return bool(
        settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_session
    )


# ---------------------------------------------------------------------------
# Pure record building (Telethon-agnostic)
# ---------------------------------------------------------------------------

def classify_media(message: Any) -> str | None:
    for attr, kind in _MEDIA_ATTRS:
        if getattr(message, attr, None):
            return kind
    return None


def build_record(message: Any, max_chars: int) -> dict[str, Any]:
    """Map a Telethon-like message into a compact, JSON-safe archive record.

    Binary media is never downloaded — only a `media` label is kept; the text /
    caption (``message``) is preserved (clipped to `max_chars`).
    """
    text = getattr(message, "message", None)
    if text is None:
        text = getattr(message, "text", None)
    if text is not None:
        text = text[:max_chars]
    date_val = getattr(message, "date", None)
    edit_val = getattr(message, "edit_date", None)
    return {
        "id": int(message.id),
        "date": date_val.isoformat() if date_val is not None else None,
        "sender_id": getattr(message, "sender_id", None),
        "reply_to": getattr(message, "reply_to_msg_id", None),
        "edit_date": edit_val.isoformat() if edit_val is not None else None,
        "text": text or None,
        "media": classify_media(message),
    }


# ---------------------------------------------------------------------------
# Recovery + range archiving (Telethon-agnostic core)
# ---------------------------------------------------------------------------

def _recover(archive_path: Path, manifest: dict[str, Any]) -> None:
    """Heal a partial trailing member left by a crash, in-place on disk."""
    committed = int(manifest.get("committed_offset", 0))
    if archive_path.exists():
        size = archive_path.stat().st_size
        if size > committed:
            log.warning(
                "Archive %s longer than committed offset (%d > %d) — truncating partial member.",
                archive_path.name, size, committed,
            )
            truncate_file(archive_path, committed)
        elif size < committed:
            log.warning(
                "Archive %s shorter than committed offset (%d < %d) — adjusting manifest.",
                archive_path.name, size, committed,
            )
            manifest["committed_offset"] = size
    elif committed or "last_message_id" in manifest:
        log.warning("Archive file missing but manifest present — rebuilding from scratch.")
        manifest.clear()


async def _archive_range(
    records: AsyncIterator[dict[str, Any]],
    archive_path: Path,
    manifest_path_: Path,
    prev: dict[str, Any],
    *,
    throttle: float = 0.0,
) -> BackupResult:
    """Append one gzip member of new records and commit the manifest atomically.

    `prev` is the (recovered) manifest plus `chat_id`/`anchor_ts`. Records are
    deduped against `last_message_id`. On any failure the half-written member is
    rolled back and the manifest is left untouched (the previous good state).
    """
    first_run = "last_message_id" not in prev
    last_id = int(prev.get("last_message_id", 0))
    last_date = prev.get("last_message_date")
    msg_count = int(prev.get("message_count", 0))

    writer = GzipMemberWriter(archive_path).open()
    added = 0
    try:
        async for rec in records:
            rid = int(rec["id"])
            if rid <= last_id:
                continue  # already archived (dedup / resume safety)
            writer.write_record(rec)
            added += 1
            last_id = rid
            last_date = rec.get("date")
            if throttle:
                await asyncio.sleep(throttle)
            elif added % 200 == 0:
                await asyncio.sleep(0)
    except BaseException:
        writer.rollback()
        raise

    now_iso = datetime.now(timezone.utc).isoformat()

    if added == 0:
        # Nothing new: drop the empty member, just refresh the heartbeat so the
        # periodic loop's cadence advances.
        writer.rollback()
        manifest = {**prev, "schema_version": SCHEMA_VERSION,
                    "committed_offset": writer.start_offset, "updated_at": now_iso}
        write_json_atomic(manifest_path_, manifest)
        return BackupResult(0, last_id, last_date, first_run, writer.start_offset, archive_path)

    end = writer.commit()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "chat_id": prev.get("chat_id"),
        "anchor_ts": prev.get("anchor_ts"),
        "last_message_id": last_id,
        "last_message_date": last_date,
        "committed_offset": end,
        "message_count": msg_count + added,
        "sha256": sha256_file(archive_path),
        "updated_at": now_iso,
    }
    write_json_atomic(manifest_path_, manifest)
    return BackupResult(added, last_id, last_date, first_run, end, archive_path)


# ---------------------------------------------------------------------------
# MTProto orchestration (Telethon)
# ---------------------------------------------------------------------------

async def _telethon_records(
    client: Any, entity: Any, *, offset_id: int, cutoff: datetime, max_chars: int
) -> AsyncIterator[dict[str, Any]]:
    # reverse=True → oldest→newest after offset_id; stop once past the cutoff.
    async for message in client.iter_messages(entity, reverse=True, offset_id=offset_id):
        mdate = getattr(message, "date", None)
        if mdate is not None and mdate > cutoff:
            break
        yield build_record(message, max_chars)


async def _resolve_entity(client: Any, group_id: int) -> Any:
    try:
        return await client.get_entity(group_id)
    except Exception:  # noqa: BLE001 — fall back to a dialog scan
        async for dialog in client.iter_dialogs():
            if dialog.id == group_id:
                return dialog.entity
        raise ChatArchiveError(
            "Gruppo non trovato dall'account Telethon: l'account deve essere membro del gruppo."
        ) from None


async def run_chat_backup(*, group_id: int, max_chars: int | None = None) -> BackupResult:
    """Connect to MTProto, extend the archive with the new range, disconnect."""
    if not is_enabled():
        raise BackupDisabledError(
            "Archivio chat disattivato: imposta TELEGRAM_API_ID/HASH/SESSION nella .env."
        )
    if not group_id:
        raise ChatArchiveError("GROUP_ID non configurato.")

    max_chars = settings.backup_max_message_chars if max_chars is None else max_chars
    archive_path, manifest_path_ = archive_paths(settings.backup_dir)
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    async with _LOCK:
        manifest = read_json(manifest_path_) or {}
        _recover(archive_path, manifest)

        first_run = "last_message_id" not in manifest
        now = datetime.now(timezone.utc)
        anchor_ts = manifest.get("anchor_ts") or now.isoformat()
        cutoff = datetime.fromisoformat(anchor_ts) if first_run else now
        offset_id = int(manifest.get("last_message_id", 0))
        prev = {**manifest, "chat_id": group_id, "anchor_ts": anchor_ts}

        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise ChatArchiveError("Telethon non installato (pip install telethon).") from exc

        client = TelegramClient(
            StringSession(settings.telegram_session),
            settings.telegram_api_id,
            settings.telegram_api_hash,
            flood_sleep_threshold=60,
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise ChatArchiveError(
                    "Sessione Telethon non autorizzata. Rigenerala con scripts/login_telethon.py."
                )
            entity = await _resolve_entity(client, group_id)
            records = _telethon_records(
                client, entity, offset_id=offset_id, cutoff=cutoff, max_chars=max_chars
            )
            result = await _archive_range(records, archive_path, manifest_path_, prev)
        finally:
            await client.disconnect()

    log.info(
        "Chat backup done: +%d messages (last_id=%d, file=%d bytes, first_run=%s).",
        result.added, result.last_message_id, result.file_size, result.first_run,
    )
    return result
