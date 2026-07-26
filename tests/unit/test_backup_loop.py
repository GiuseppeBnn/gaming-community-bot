"""Unit tests for services/backup/loop.py — the background backup driver.

This module had **no coverage at all**, and it is the kind of code where that
matters most: a daemon whose whole contract is *never take the bot down with it*.
STEERING §24 states it explicitly — the loop and its commands must never block the
event loop nor crash the process. That is a promise made by three `try/except`
blocks and a pre-flight check, none of which had ever been exercised.

Due-ness is derived from artifacts on disk (snapshot mtime, manifest
``updated_at``) rather than a DB table, so most of this can be tested against a
real temp directory. Only the two actual backup operations are stubbed — they need
a database session and Telethon credentials respectively.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from config_data.config import settings
from services.backup import chat_archive, loop, state_export


def _utc(hours_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


class TestDueness:
    def test_never_run_is_due(self):
        """No artifact on disk yet ⇒ due. The first tick after a fresh deploy has to
        produce a backup, not wait a full cadence."""
        assert loop._due_since(None, 24) is True

    def test_recent_run_is_not_due(self):
        assert loop._due_since(_utc(1), 24) is False

    def test_run_older_than_the_cadence_is_due(self):
        assert loop._due_since(_utc(25), 24) is True

    def test_exactly_at_the_cadence_is_due(self, monkeypatch):
        """`>=`, not `>`: with a 24h cadence a daily tick must not drift later and
        later because it keeps missing the boundary.

        The elapsed hours are stubbed rather than derived from a timestamp: a
        `now - 24h` argument is already 24.000001 hours old by the time the
        comparison runs, so it passes either way and pins nothing. Verified by
        mutation — with a real timestamp, flipping `>=` to `>` kept this green.
        """
        monkeypatch.setattr(loop, "_hours_since", lambda when: 24.0)

        assert loop._due_since(datetime.now(timezone.utc), 24) is True

    def test_a_naive_timestamp_is_read_as_utc(self):
        """`_state_last_run` builds an aware datetime but `_chat_last_run` parses
        whatever the manifest holds, which may be naive. Subtracting a naive from an
        aware datetime raises TypeError — inside the loop that would turn every tick
        into a logged failure, silently, forever.
        """
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=30)
        assert loop._hours_since(naive) == pytest.approx(30, abs=0.1)
        assert loop._due_since(naive, 24) is True


class TestLastRunReaders:
    def test_state_last_run_is_none_without_a_snapshot(self, tmp_path):
        assert loop._state_last_run(str(tmp_path)) is None

    def test_state_last_run_reads_the_snapshot_mtime(self, tmp_path):
        state_export.latest_snapshot_path(tmp_path).write_bytes(b"x")
        last = loop._state_last_run(str(tmp_path))
        assert last is not None and last.tzinfo is not None

    def test_chat_last_run_is_none_without_a_manifest(self, tmp_path):
        assert loop._chat_last_run(str(tmp_path)) is None

    def test_chat_last_run_survives_a_corrupt_timestamp(self, tmp_path):
        """A truncated or hand-edited manifest must degrade to "never run" (⇒ due),
        not raise: the archive is appended to over months, so a partially written
        manifest is a real state, and it must not wedge the loop."""
        chat_archive.manifest_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        chat_archive.manifest_path(tmp_path).write_text('{"updated_at": "not-a-date"}')

        assert loop._chat_last_run(str(tmp_path)) is None


class TestRunDueBackups:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        """Point the backup dir at a temp path and disable the chat archive, so a
        developer's real .env can't make these tests touch Telegram or a real dir."""
        monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
        monkeypatch.setattr(settings, "backup_state_interval_hours", 24)
        monkeypatch.setattr(settings, "backup_chat_interval_hours", 168)
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: False)

    async def test_runs_the_state_export_when_due(self, monkeypatch):
        called: list[str] = []

        async def fake_export(session, dest_dir):
            called.append(dest_dir)
            return f"{dest_dir}/state-latest.jsonl.gz"

        monkeypatch.setattr(state_export, "export_state", fake_export)

        await loop.run_due_backups()

        assert called == [settings.backup_dir]

    async def test_a_failing_state_export_does_not_propagate(self, monkeypatch):
        """The guarantee that keeps the bot alive: one broken backup logs and is
        retried next tick. If this exception escaped, `backup_loop` would still catch
        it — but it would skip the chat archive for that tick too.
        """
        async def boom(session, dest_dir):
            raise OSError("disk full")

        monkeypatch.setattr(state_export, "export_state", boom)

        await loop.run_due_backups()  # must simply return

    async def test_skips_everything_when_the_directory_is_not_writable(
        self, tmp_path, monkeypatch, caplog
    ):
        """A mis-mounted volume must produce one clear warning per tick, not an
        EACCES traceback on every single write (STEERING §25).
        """
        calls: list[str] = []

        async def fake_export(session, dest_dir):
            calls.append(dest_dir)
            return "x"

        monkeypatch.setattr(state_export, "export_state", fake_export)
        # A file where the directory should be: mkdir and the write probe both fail.
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        monkeypatch.setattr(settings, "backup_dir", str(blocked))

        with caplog.at_level("WARNING"):
            await loop.run_due_backups()

        assert calls == [], "wrote a backup into a non-writable directory"
        assert "non scrivibile" in caplog.text

    async def test_does_not_re_export_when_a_recent_snapshot_exists(self, monkeypatch):
        calls: list[str] = []

        async def fake_export(session, dest_dir):
            calls.append(dest_dir)
            return "x"

        monkeypatch.setattr(state_export, "export_state", fake_export)
        # A snapshot written just now ⇒ not due for another 24h.
        from pathlib import Path
        Path(settings.backup_dir).mkdir(parents=True, exist_ok=True)
        state_export.latest_snapshot_path(settings.backup_dir).write_bytes(b"x")

        await loop.run_due_backups()

        assert calls == []

    async def test_skips_the_chat_archive_without_a_group_id(self, monkeypatch, caplog):
        """Enabled credentials but no group configured: warn and skip, never call
        Telethon with a falsy peer."""
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: True)
        monkeypatch.setattr("services.group_registry.get_group_id", lambda: 0)

        async def fake_export(session, dest_dir):
            return "x"

        async def must_not_run(**kwargs):
            raise AssertionError("ran the chat backup without a group id")

        monkeypatch.setattr(state_export, "export_state", fake_export)
        monkeypatch.setattr(chat_archive, "run_chat_backup", must_not_run)

        with caplog.at_level("WARNING"):
            await loop.run_due_backups()

        assert "GROUP_ID" in caplog.text


    async def test_runs_the_chat_archive_when_enabled_and_due(self, monkeypatch):
        """No manifest on disk ⇒ never run ⇒ due. `run_chat_backup` is stubbed: the
        real one needs Telethon credentials and a live Telegram session."""
        import types

        seen: list[int] = []

        async def fake_export(session, dest_dir):
            return "x"

        async def fake_chat(*, group_id):
            seen.append(group_id)
            return types.SimpleNamespace(added=7, file_size=1234)

        monkeypatch.setattr(state_export, "export_state", fake_export)
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: True)
        monkeypatch.setattr(chat_archive, "run_chat_backup", fake_chat)
        monkeypatch.setattr("services.group_registry.get_group_id", lambda: -100123)

        await loop.run_due_backups()

        assert seen == [-100123]

    async def test_a_failing_chat_archive_does_not_propagate(self, monkeypatch):
        """The archive talks to Telegram over MTProto, so it fails for reasons that
        have nothing to do with this bot (flood waits, expired session). One logged
        failure, retried next cadence."""
        async def fake_export(session, dest_dir):
            return "x"

        async def boom(*, group_id):
            raise ConnectionError("telegram unreachable")

        monkeypatch.setattr(state_export, "export_state", fake_export)
        monkeypatch.setattr(chat_archive, "is_enabled", lambda: True)
        monkeypatch.setattr(chat_archive, "run_chat_backup", boom)
        monkeypatch.setattr("services.group_registry.get_group_id", lambda: -100123)

        await loop.run_due_backups()  # must simply return


class TestBackupLoop:
    async def test_the_loop_survives_a_failing_tick(self, monkeypatch):
        """`backup_loop` is `while True` with a total except. Two ticks are driven
        here — the first raising — and the loop is then stopped by making the sleep
        raise, which is the only way out of an endless loop from a test.
        """
        ticks = 0

        async def flaky() -> None:
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                raise RuntimeError("first tick explodes")

        class Stop(Exception):
            pass

        async def fake_sleep(_seconds):
            if ticks >= 2:
                raise Stop
            return None

        monkeypatch.setattr(loop, "run_due_backups", flaky)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        with pytest.raises(Stop):
            await loop.backup_loop()

        assert ticks == 2, "the loop died on the failing tick instead of retrying"

    async def test_the_loop_waits_between_ticks(self, monkeypatch):
        """It must sleep, and for the module's interval — a `while True` that awaits
        nothing meaningful would spin the event loop."""
        slept: list[float] = []

        async def fake_run() -> None:
            return None

        class Stop(Exception):
            pass

        async def fake_sleep(seconds):
            slept.append(seconds)
            raise Stop

        monkeypatch.setattr(loop, "run_due_backups", fake_run)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        with pytest.raises(Stop):
            await loop.backup_loop()

        assert slept == [loop._CHECK_INTERVAL_SECONDS]
