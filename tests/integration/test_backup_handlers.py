"""`/backup` and `/esporta` — `handlers/backup.py`, at 34%.

These two commands hand an admin a file containing, between them, the group's
whole message history and the whole database. That makes three things worth
pinning, none of which is about the archive format (that belongs to
`services/backup/`, tested separately):

  * **they never answer in the group**. The artifacts and even the status lines are
    operational data; in the group the file itself would be posted where every
    member could download it;
  * **every run is written to the admin audit log, and only when it succeeded** — an
    audit line for an export that failed is worse than no line;
  * **a file too big to send is not lost**. Telegram caps bot uploads at 50 MB; over
    that the handler must say where the file is on disk instead of failing the send
    and leaving the admin thinking the backup didn't happen.

The archive/export machinery itself is stubbed: running Telethon or dumping the DB
here would test those modules again, and slowly.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import AdminAction
from handlers import backup
from services.backup import chat_archive, state_export
from services.backup.chat_archive import BackupResult, ChatArchiveError
from services import group_registry

ADMIN_ID = 1
GROUP_ID = -100_777


class _FakeBot:
    async def get_me(self):
        return SimpleNamespace(username="testbot")


class _FakeStatus:
    """The «⏳ in corso…» message the handler edits or deletes afterwards."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.deleted = False

    async def edit_text(self, text, **kw):
        self.texts.append(text)

    async def delete(self):
        self.deleted = True


class _FakeMessage:
    def __init__(self, *, chat_type: str = "private") -> None:
        self.bot = _FakeBot()
        self.from_user = SimpleNamespace(id=ADMIN_ID, username="admin", full_name="Admin")
        self.chat = SimpleNamespace(
            id=ADMIN_ID if chat_type == "private" else GROUP_ID, type=chat_type
        )
        self.texts: list[str] = []
        self.markups: list[object] = []
        self.documents: list[tuple[object, str]] = []
        self.statuses: list[_FakeStatus] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        status = _FakeStatus()
        self.statuses.append(status)
        return status

    async def reply(self, text, reply_markup=None, **kw):
        return await self.answer(text, reply_markup, **kw)

    async def answer_document(self, document, caption=None, **kw):
        self.documents.append((document, caption or ""))

    @property
    def said(self) -> str:
        return "\n".join(self.texts + [t for s in self.statuses for t in s.texts])


@pytest.fixture
def in_group():
    group_registry.set_runtime_group_id(GROUP_ID)
    yield GROUP_ID
    group_registry.set_runtime_group_id(None)


@pytest.fixture
def artifact(tmp_path) -> Path:
    path = tmp_path / "archivio.jsonl.gz"
    path.write_bytes(b"x" * 1024)
    return path


def _result(path: Path, *, added=10, first_run=False) -> BackupResult:
    return BackupResult(
        added=added, last_message_id=999, last_message_date=None,
        first_run=first_run, file_size=path.stat().st_size, archive_path=path,
    )


async def _audit(session) -> list[AdminAction]:
    return list((await session.execute(select(AdminAction))).scalars().all())


# ---------------------------------------------------------------------------
# /backup
# ---------------------------------------------------------------------------

class TestBackup:
    async def test_in_the_group_it_only_hands_back_a_link(self, session):
        """Answering here would post the group's own archive into the group."""
        message = _FakeMessage(chat_type="supergroup")

        await backup.cmd_backup(message, session)

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=backup")
        assert message.documents == []

    async def test_in_private_the_command_runs_the_backup(
        self, session, monkeypatch, user_factory, in_group, artifact
    ):
        """The command and the deep-link land on the same body; only the guard in
        front of it differs."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: True)

        async def _run(group_id):
            return _result(artifact)

        monkeypatch.setattr(chat_archive, "run_chat_backup", _run)
        message = _FakeMessage()

        await backup.cmd_backup(message, session)

        assert len(message.documents) == 1

    async def test_without_telethon_credentials_it_explains_what_is_missing(
        self, session, monkeypatch, in_group
    ):
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: False)
        message = _FakeMessage()

        await backup.run_backup_now(message, session)

        assert "TELEGRAM_SESSION" in message.said
        assert await _audit(session) == []

    async def test_without_a_group_there_is_nothing_to_archive(
        self, session, monkeypatch
    ):
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: True)
        group_registry.set_runtime_group_id(0)
        try:
            message = _FakeMessage()
            await backup.run_backup_now(message, session)
        finally:
            group_registry.set_runtime_group_id(None)

        assert "GROUP_ID" in message.said

    async def test_a_successful_run_sends_the_file_and_writes_the_audit_line(
        self, session, monkeypatch, user_factory, in_group, artifact
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: True)

        async def _run(group_id):
            assert group_id == GROUP_ID
            return _result(artifact, added=42)

        monkeypatch.setattr(chat_archive, "run_chat_backup", _run)
        message = _FakeMessage()

        await backup.run_backup_now(message, session)

        assert len(message.documents) == 1
        assert "42" in message.documents[0][1]
        entry = (await _audit(session))[0]
        assert entry.action_type == "backup" and entry.amount == 42
        assert message.statuses[0].deleted, "the «in corso» line must not stay up"

    async def test_the_first_run_is_labelled_as_the_whole_history(
        self, session, monkeypatch, user_factory, in_group, artifact
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: True)

        async def _run(group_id):
            return _result(artifact, first_run=True)

        monkeypatch.setattr(chat_archive, "run_chat_backup", _run)
        message = _FakeMessage()

        await backup.run_backup_now(message, session)

        assert "intera cronologia" in message.documents[0][1]

    async def test_a_known_failure_is_reported_and_not_audited(
        self, session, monkeypatch, user_factory, in_group
    ):
        """The audit log answers "when was the last good backup?"; a line for a run
        that failed makes that question unanswerable."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: True)

        async def _run(group_id):
            raise ChatArchiveError("sessione scaduta")

        monkeypatch.setattr(chat_archive, "run_chat_backup", _run)
        message = _FakeMessage()

        await backup.run_backup_now(message, session)

        assert "sessione scaduta" in message.said
        assert await _audit(session) == []
        assert message.documents == []

    async def test_an_unexpected_failure_is_caught_too(
        self, session, monkeypatch, user_factory, in_group
    ):
        """Telethon can raise anything; the handler must surface it rather than let
        it bubble into the error middleware as an unhandled crash."""
        await user_factory(tg_id=ADMIN_ID, username="admin")
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: True)

        async def _run(group_id):
            raise MemoryError("boom")

        monkeypatch.setattr(chat_archive, "run_chat_backup", _run)
        message = _FakeMessage()

        await backup.run_backup_now(message, session)

        assert "imprevisto" in message.said
        assert await _audit(session) == []


# ---------------------------------------------------------------------------
# /esporta
# ---------------------------------------------------------------------------

class TestExport:
    async def test_in_the_group_it_only_hands_back_a_link(self, session):
        message = _FakeMessage(chat_type="supergroup")

        await backup.cmd_esporta(message, session)

        assert message.markups[0].inline_keyboard[0][0].url.endswith("?start=esporta")

    async def test_in_private_the_command_runs_the_export(
        self, session, monkeypatch, user_factory, artifact
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")

        async def _export(db_session, dest_dir, **kw):
            return artifact

        monkeypatch.setattr(state_export, "export_state", _export)
        message = _FakeMessage()

        await backup.cmd_esporta(message, session)

        assert len(message.documents) == 1

    async def test_a_successful_export_sends_the_snapshot_and_audits_it(
        self, session, monkeypatch, user_factory, artifact
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")

        async def _export(db_session, dest_dir, **kw):
            return artifact

        monkeypatch.setattr(state_export, "export_state", _export)
        message = _FakeMessage()

        await backup.run_export_now(message, session)

        assert message.documents and artifact.name in message.documents[0][1]
        entry = (await _audit(session))[0]
        assert entry.action_type == "esporta" and entry.detail == artifact.name

    async def test_a_failed_export_is_reported_and_not_audited(
        self, session, monkeypatch, user_factory
    ):
        await user_factory(tg_id=ADMIN_ID, username="admin")

        async def _export(db_session, dest_dir, **kw):
            raise OSError("disco pieno")

        monkeypatch.setattr(state_export, "export_state", _export)
        message = _FakeMessage()

        await backup.run_export_now(message, session)

        assert "disco pieno" in message.said
        assert await _audit(session) == []
        assert message.documents == []


# ---------------------------------------------------------------------------
# The 50 MB ceiling
# ---------------------------------------------------------------------------

class TestUploadLimit:
    async def test_a_file_over_the_limit_is_left_on_disk_with_its_path(
        self, session, tmp_path, monkeypatch
    ):
        """Failing the send would leave the admin believing the backup didn't run,
        when in fact it did and the file is sitting right there."""
        path = tmp_path / "enorme.jsonl.gz"
        path.write_bytes(b"")
        monkeypatch.setattr(
            Path, "stat", lambda self: SimpleNamespace(st_size=backup._TG_UPLOAD_LIMIT + 1)
        )
        message = _FakeMessage()

        await backup._send_file_or_note(message, path, "didascalia")

        assert message.documents == []
        assert "enorme.jsonl.gz" in message.said and "50 MB" in message.said

    async def test_a_file_at_the_limit_is_still_sent(self, session, tmp_path, monkeypatch):
        path = tmp_path / "giusto.jsonl.gz"
        path.write_bytes(b"")
        monkeypatch.setattr(
            Path, "stat", lambda self: SimpleNamespace(st_size=backup._TG_UPLOAD_LIMIT)
        )
        message = _FakeMessage()

        await backup._send_file_or_note(message, path, "didascalia")

        assert len(message.documents) == 1

    @pytest.mark.parametrize("num,expected", [
        (512, "512.0 B"),
        (2048, "2.0 KB"),
        (5 * 1024 * 1024, "5.0 MB"),
        (3 * 1024 ** 3, "3.0 GB"),
        (4096 * 1024 ** 3, "4096.0 GB"),
    ])
    def test_sizes_are_rendered_for_humans(self, num, expected):
        """Including past GB: the loop stops there rather than inventing units.

        The `return` after the loop in `_human_size` stays uncovered on purpose —
        it is unreachable (the last iteration always returns, since `unit == "GB"`)
        and exists only so the function has a return on every path for mypy.
        """
        assert backup._human_size(num) == expected
