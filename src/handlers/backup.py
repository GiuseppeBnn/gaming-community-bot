"""
Admin backup commands (§25).

  /backup  — extend the MTProto chat archive now and DM the file (if ≤ 50 MB)
  /esporta — produce a fresh total-state snapshot now and DM it

Both reveal operational data → private chat only (redirect from the group with
the existing deep-link pattern). Every run is recorded in the admin audit log.
The reusable bodies (``run_backup_now`` / ``run_export_now``) are also invoked by
the ``backup`` / ``esporta`` deep-link payloads in ``common.cmd_start``.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters.command import Command
from aiogram.types import FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers._privacy import redirect_to_private
from services import admin_service, group_registry
from services.backup import chat_archive, state_export
from services.backup.chat_archive import ChatArchiveError
from utils.text import esc

log = logging.getLogger(__name__)
router = Router()
# 100%-admin router: gate every message/callback at the root (STEERING §8).
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminCallbackFilter())

# Telegram caps bot file uploads at 50 MB; stay safely under it.
_TG_UPLOAD_LIMIT = 49 * 1024 * 1024


def _human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


async def _send_file_or_note(message: Message, path, caption: str) -> None:
    """DM the artifact if it fits Telegram's upload limit, else point to disk."""
    size = path.stat().st_size
    if size <= _TG_UPLOAD_LIMIT:
        await message.answer_document(FSInputFile(path), caption=caption)
    else:
        await message.answer(
            f"{caption}\n\n⚠️ File troppo grande per l'invio "
            f"({_human_size(size)} &gt; 50 MB): resta su disco in "
            f"<code>{esc(str(path))}</code>."
        )


# ---------------------------------------------------------------------------
# /backup — chat archive
# ---------------------------------------------------------------------------

@router.message(Command("backup"), IsAdminFilter())
async def cmd_backup(message: Message, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "backup", "💾 Esegui il backup in privato"):
        return
    await run_backup_now(message, db_session)


async def run_backup_now(message: Message, db_session: AsyncSession) -> None:
    if not chat_archive.is_enabled():
        await message.answer(
            "⚠️ <b>Archivio chat non configurato.</b>\n\n"
            "Imposta <code>TELEGRAM_API_ID</code>, <code>TELEGRAM_API_HASH</code> e "
            "<code>TELEGRAM_SESSION</code> nella <code>.env</code> "
            "(genera la sessione con <code>scripts/login_telethon.py</code>)."
        )
        return

    group_id = group_registry.get_group_id()
    if not group_id:
        await message.answer("⚠️ GROUP_ID non configurato: impossibile archiviare la chat.")
        return

    status = await message.answer("⏳ Backup della chat in corso… (può richiedere un po')")
    try:
        result = await chat_archive.run_chat_backup(group_id=group_id)
    except ChatArchiveError as exc:
        await status.edit_text(f"❌ Backup non riuscito: {esc(str(exc))}")
        return
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the handler
        log.exception("Chat backup error")
        await status.edit_text(f"❌ Errore imprevisto durante il backup: {esc(str(exc))}")
        return

    await admin_service.log_action(
        db_session, message.from_user.id, "backup",
        group_id=group_id, amount=result.added,
        detail=f"first_run={result.first_run} last_id={result.last_message_id}",
    )
    await db_session.commit()

    scope = "intera cronologia" if result.first_run else "nuovi messaggi"
    caption = (
        f"💾 <b>Backup chat completato</b>\n"
        f"➕ {result.added:,} messaggi ({scope})\n"
        f"🆔 Ultimo id: <code>{result.last_message_id}</code>\n"
        f"📦 Archivio: {_human_size(result.file_size)}"
    )
    await status.delete()
    await _send_file_or_note(message, result.archive_path, caption)


# ---------------------------------------------------------------------------
# /esporta — total state export
# ---------------------------------------------------------------------------

@router.message(Command("esporta"), IsAdminFilter())
async def cmd_esporta(message: Message, db_session: AsyncSession) -> None:
    if await redirect_to_private(message, "esporta", "📤 Esporta lo stato in privato"):
        return
    await run_export_now(message, db_session)


async def run_export_now(message: Message, db_session: AsyncSession) -> None:
    status = await message.answer("⏳ Esportazione dello stato in corso…")
    try:
        path = await state_export.export_state(db_session, settings.backup_dir)
    except Exception as exc:  # noqa: BLE001
        log.exception("State export error")
        await status.edit_text(f"❌ Esportazione non riuscita: {esc(str(exc))}")
        return

    await admin_service.log_action(
        db_session, message.from_user.id, "esporta", detail=path.name
    )
    await db_session.commit()

    caption = (
        "📤 <b>Stato esportato</b>\n"
        f"📦 {esc(path.name)} · {_human_size(path.stat().st_size)}\n"
        "Reimporta con <code>scripts/import_state.py</code> dopo una migrazione."
    )
    await status.delete()
    await _send_file_or_note(message, path, caption)
