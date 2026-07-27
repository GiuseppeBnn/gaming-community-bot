"""Tests for the Telethon-agnostic core of services.backup.chat_archive."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone

from services.backup import chat_archive
from utils.atomic_io import GzipMemberWriter, read_json


class FakeMsg:
    """Duck-types the few Telethon Message attributes build_record reads."""

    def __init__(self, id, text=None, date=None, sender_id=None, reply_to=None,
                 edit_date=None, **media):
        self.id = id
        self.message = text
        self.date = date
        self.sender_id = sender_id
        self.reply_to_msg_id = reply_to
        self.edit_date = edit_date
        for attr, val in media.items():
            setattr(self, attr, val)


async def _agen(records):
    for r in records:
        yield r


def _ids(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line)["id"] for line in f if line.strip()]


# ---------------------------------------------------------------------------
# build_record / classify_media
# ---------------------------------------------------------------------------

class TestBuildRecord:
    def test_plain_text(self):
        d = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
        r = chat_archive.build_record(FakeMsg(5, text="ciao", date=d, sender_id=99, reply_to=4), 100)
        assert r == {
            "id": 5, "date": d.isoformat(), "sender_id": 99,
            "reply_to": 4, "edit_date": None, "text": "ciao", "media": None,
        }

    def test_text_is_clipped(self):
        r = chat_archive.build_record(FakeMsg(1, text="x" * 5000), 10)
        assert len(r["text"]) == 10

    def test_media_without_text(self):
        r = chat_archive.build_record(FakeMsg(2, text=None, photo=object()), 100)
        assert r["media"] == "photo"
        assert r["text"] is None

    def test_caption_preserved_on_media(self):
        r = chat_archive.build_record(FakeMsg(3, text="guarda qui", video=object()), 100)
        assert r["media"] == "video"
        assert r["text"] == "guarda qui"

    def test_classify_priority_and_none(self):
        assert chat_archive.classify_media(FakeMsg(1, voice=object())) == "voice"
        assert chat_archive.classify_media(FakeMsg(1)) is None


# ---------------------------------------------------------------------------
# _archive_range — append-only, dedup, manifest, no-op
# ---------------------------------------------------------------------------

class TestArchiveRange:
    async def test_first_run_writes_all(self, tmp_path):
        arc = tmp_path / "chat-archive.jsonl.gz"
        man = tmp_path / "m.json"
        prev = {"chat_id": -100, "anchor_ts": "2026-06-13T00:00:00+00:00"}
        res = await chat_archive._archive_range(
            _agen([{"id": 1, "date": "d1"}, {"id": 2, "date": "d2"}]), arc, man, prev
        )
        assert res.added == 2
        assert res.first_run is True
        assert res.last_message_id == 2

        m = read_json(man)
        assert m["last_message_id"] == 2
        assert m["message_count"] == 2
        assert m["committed_offset"] == arc.stat().st_size
        assert m["chat_id"] == -100
        assert _ids(arc) == [1, 2]

    async def test_incremental_dedups_and_appends(self, tmp_path):
        arc = tmp_path / "chat-archive.jsonl.gz"
        man = tmp_path / "m.json"
        await chat_archive._archive_range(
            _agen([{"id": 1}, {"id": 2}]), arc, man, {"chat_id": -1, "anchor_ts": "t"}
        )
        prev = {**read_json(man)}
        # id 2 is already archived; only 3 and 4 are new.
        res = await chat_archive._archive_range(
            _agen([{"id": 2}, {"id": 3}, {"id": 4}]), arc, man, prev
        )
        assert res.added == 2
        assert res.last_message_id == 4
        assert res.first_run is False
        assert _ids(arc) == [1, 2, 3, 4]
        assert read_json(man)["message_count"] == 4

    async def test_no_new_messages_leaves_file_untouched(self, tmp_path):
        arc = tmp_path / "chat-archive.jsonl.gz"
        man = tmp_path / "m.json"
        await chat_archive._archive_range(
            _agen([{"id": 1}]), arc, man, {"chat_id": -1, "anchor_ts": "t"}
        )
        size_before = arc.stat().st_size
        prev = {**read_json(man)}
        res = await chat_archive._archive_range(_agen([{"id": 1}]), arc, man, prev)
        assert res.added == 0
        assert arc.stat().st_size == size_before
        assert _ids(arc) == [1]


# ---------------------------------------------------------------------------
# _recover — heal a partial trailing member
# ---------------------------------------------------------------------------

class TestCooperativeYielding:
    """A first run walks years of history in one coroutine. Both of these exist so
    it cannot starve the event loop — the bot must keep answering while it runs."""

    async def test_a_long_run_yields_periodically(self, tmp_path):
        arc, man = tmp_path / "a.jsonl.gz", tmp_path / "m.json"
        records = [{"id": i, "date": None} for i in range(1, 402)]

        result = await chat_archive._archive_range(_agen(records), arc, man, {})

        assert result.added == 401

    async def test_the_throttle_slows_it_down_on_purpose(self, tmp_path):
        """Used by the periodic loop so a background backup never competes with
        live traffic."""
        arc, man = tmp_path / "a.jsonl.gz", tmp_path / "m.json"
        records = [{"id": i, "date": None} for i in range(1, 4)]

        result = await chat_archive._archive_range(
            _agen(records), arc, man, {}, throttle=0.001
        )

        assert result.added == 3
        assert _ids(arc) == [1, 2, 3]


class TestRecover:
    def test_truncates_partial_tail(self, tmp_path):
        arc = tmp_path / "chat-archive.jsonl.gz"
        w = GzipMemberWriter(arc).open()
        w.write_record({"id": 1})
        committed = w.commit()

        # Simulate a crash that left a half-written member appended.
        with open(arc, "ab") as f:
            f.write(b"\x1f\x8b partial junk that is not a complete member")

        manifest = {"committed_offset": committed, "last_message_id": 1}
        chat_archive._recover(arc, manifest)

        assert arc.stat().st_size == committed
        assert _ids(arc) == [1]

    def test_missing_file_resets_manifest(self, tmp_path):
        manifest = {"committed_offset": 123, "last_message_id": 9}
        chat_archive._recover(tmp_path / "absent.jsonl.gz", manifest)
        assert manifest == {}


# ---------------------------------------------------------------------------
# MTProto orchestration — real logic, fake transport
# ---------------------------------------------------------------------------
#
# `run_chat_backup` imports Telethon *inside* the function, so a fake module in
# `sys.modules` is enough to drive the whole orchestration: recovery, the
# first-run cutoff, dedup against the manifest, the atomic commit and the
# guaranteed disconnect. Everything except the network is the real code.
#
# The credentials are monkeypatched onto `settings` and are obvious fakes. The
# real TELEGRAM_SESSION is a full-account credential and lives only in the .env —
# nothing here reads it, and no test may ever print one.

import sys
import types

import pytest

from services.backup.chat_archive import BackupDisabledError, ChatArchiveError

GROUP_ID = -100_777


class _FakeClient:
    """Duck-types the handful of Telethon calls the orchestration makes."""

    def __init__(self, messages=(), *, authorized=True, entity_fails=False,
                 dialogs=()):
        self.messages = list(messages)
        self.authorized = authorized
        self.entity_fails = entity_fails
        self.dialogs = list(dialogs)
        self.connected = False
        self.disconnected = False
        self.iter_kwargs: dict = {}

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return self.authorized

    async def get_entity(self, group_id):
        if self.entity_fails:
            raise RuntimeError("Cannot find any entity corresponding to")
        return f"entity:{group_id}"

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d

    async def iter_messages(self, entity, **kwargs):
        self.iter_kwargs = kwargs
        for m in self.messages:
            yield m


@pytest.fixture
def telethon(monkeypatch):
    """Install a fake `telethon` package and hand back a slot for the client."""
    holder: dict = {}

    def _factory(session, api_id, api_hash, **kwargs):
        holder["session"] = session
        holder["api_id"] = api_id
        return holder["client"]

    fake = types.ModuleType("telethon")
    fake.TelegramClient = _factory
    sessions = types.ModuleType("telethon.sessions")
    sessions.StringSession = lambda s: f"session:{s}"
    monkeypatch.setitem(sys.modules, "telethon", fake)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)
    return holder


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """Credentials present (obvious fakes) and a throwaway backup dir."""
    monkeypatch.setattr(chat_archive.settings, "telegram_api_id", 12345)
    monkeypatch.setattr(chat_archive.settings, "telegram_api_hash", "fake-hash")
    monkeypatch.setattr(chat_archive.settings, "telegram_session", "fake-session")
    monkeypatch.setattr(chat_archive.settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(chat_archive.settings, "backup_max_message_chars", 100)
    return tmp_path


def _msg(id, text="ciao", *, date=None):
    return FakeMsg(id, text=text, date=date or datetime(2020, 1, 1, tzinfo=timezone.utc))


class TestEnabled:
    def test_it_needs_all_three_credentials(self, monkeypatch):
        monkeypatch.setattr(chat_archive.settings, "telegram_api_id", 1)
        monkeypatch.setattr(chat_archive.settings, "telegram_api_hash", "h")
        monkeypatch.setattr(chat_archive.settings, "telegram_session", "")

        assert chat_archive.is_enabled() is False

    def test_all_three_present_enables_it(self, configured):
        assert chat_archive.is_enabled() is True


class TestRunChatBackup:
    async def test_it_refuses_to_run_unconfigured(self, monkeypatch, tmp_path):
        monkeypatch.setattr(chat_archive.settings, "telegram_session", "")

        with pytest.raises(BackupDisabledError):
            await chat_archive.run_chat_backup(group_id=GROUP_ID)

    async def test_it_refuses_without_a_group(self, configured, telethon):
        with pytest.raises(ChatArchiveError):
            await chat_archive.run_chat_backup(group_id=0)

    async def test_the_first_run_archives_everything_and_writes_the_manifest(
        self, configured, telethon
    ):
        telethon["client"] = _FakeClient([_msg(1), _msg(2), _msg(3)])

        result = await chat_archive.run_chat_backup(group_id=GROUP_ID)

        assert (result.added, result.first_run, result.last_message_id) == (3, True, 3)
        archive, manifest_file = chat_archive.archive_paths(configured)
        assert _ids(archive) == [1, 2, 3]
        manifest = read_json(manifest_file)
        assert manifest["last_message_id"] == 3 and manifest["chat_id"] == GROUP_ID
        assert manifest["sha256"]

    async def test_the_second_run_only_takes_what_is_new(self, configured, telethon):
        """The offset is what keeps a daily backup from re-downloading years of
        history — and the dedup is what keeps it from duplicating it if it does."""
        telethon["client"] = _FakeClient([_msg(1), _msg(2)])
        await chat_archive.run_chat_backup(group_id=GROUP_ID)

        telethon["client"] = _FakeClient([_msg(2), _msg(3)])
        result = await chat_archive.run_chat_backup(group_id=GROUP_ID)

        assert (result.added, result.first_run) == (1, False)
        assert telethon["client"].iter_kwargs["offset_id"] == 2
        assert _ids(chat_archive.archive_paths(configured)[0]) == [1, 2, 3]

    async def test_a_run_with_nothing_new_leaves_the_archive_byte_identical(
        self, configured, telethon
    ):
        telethon["client"] = _FakeClient([_msg(1)])
        await chat_archive.run_chat_backup(group_id=GROUP_ID)
        archive = chat_archive.archive_paths(configured)[0]
        before = archive.read_bytes()

        telethon["client"] = _FakeClient([])
        result = await chat_archive.run_chat_backup(group_id=GROUP_ID)

        assert result.added == 0
        assert archive.read_bytes() == before

    async def test_the_first_run_stops_at_the_anchor(self, configured, telethon):
        """The first run anchors on «now»: messages arriving *during* it belong to
        the next range, or the manifest's last_message_id would jump past records
        that were never written."""
        future = datetime(2999, 1, 1, tzinfo=timezone.utc)
        telethon["client"] = _FakeClient([_msg(1), _msg(2, date=future), _msg(3)])

        result = await chat_archive.run_chat_backup(group_id=GROUP_ID)

        assert result.added == 1
        assert _ids(chat_archive.archive_paths(configured)[0]) == [1]

    async def test_an_unauthorised_session_says_how_to_fix_it(self, configured, telethon):
        """A Telethon session expires; the message has to name the script that
        regenerates it, or the archive silently stops working."""
        telethon["client"] = _FakeClient([], authorized=False)

        with pytest.raises(ChatArchiveError, match="login_telethon"):
            await chat_archive.run_chat_backup(group_id=GROUP_ID)

        assert telethon["client"].disconnected, "the client must be disconnected anyway"

    async def test_the_client_is_disconnected_even_when_the_run_explodes(
        self, configured, telethon
    ):
        """A leaked MTProto connection keeps the account's session slot busy."""
        class _Boom(_FakeClient):
            async def iter_messages(self, entity, **kwargs):
                raise RuntimeError("connection reset")
                yield  # pragma: no cover - makes this an async generator

        telethon["client"] = _Boom([])

        with pytest.raises(RuntimeError):
            await chat_archive.run_chat_backup(group_id=GROUP_ID)

        assert telethon["client"].disconnected

    async def test_a_failed_run_leaves_the_previous_archive_intact(
        self, configured, telethon
    ):
        """The half-written member is rolled back and the manifest is untouched, so
        the next run resumes from the last good state instead of a corrupt file."""
        telethon["client"] = _FakeClient([_msg(1)])
        await chat_archive.run_chat_backup(group_id=GROUP_ID)
        archive, manifest_file = chat_archive.archive_paths(configured)
        before, manifest_before = archive.read_bytes(), read_json(manifest_file)

        class _HalfWay(_FakeClient):
            async def iter_messages(self, entity, **kwargs):
                yield _msg(2)
                raise RuntimeError("flood wait / disconnected")

        telethon["client"] = _HalfWay([])
        with pytest.raises(RuntimeError):
            await chat_archive.run_chat_backup(group_id=GROUP_ID)

        assert archive.read_bytes() == before
        assert read_json(manifest_file) == manifest_before

    async def test_the_session_string_reaches_the_client(self, configured, telethon):
        telethon["client"] = _FakeClient([])

        await chat_archive.run_chat_backup(group_id=GROUP_ID)

        assert telethon["session"] == "session:fake-session"
        assert telethon["api_id"] == 12345


class TestResolveEntity:
    async def test_the_direct_lookup_is_used_when_it_works(self):
        client = _FakeClient([])

        assert await chat_archive._resolve_entity(client, GROUP_ID) == f"entity:{GROUP_ID}"

    async def test_it_falls_back_to_scanning_the_dialogs(self):
        """`get_entity` fails for a group the account has never opened in this
        session; the dialog list still has it."""
        dialog = types.SimpleNamespace(id=GROUP_ID, entity="dal-dialogo")
        client = _FakeClient([], entity_fails=True, dialogs=[dialog])

        assert await chat_archive._resolve_entity(client, GROUP_ID) == "dal-dialogo"

    async def test_a_group_the_account_is_not_in_is_reported_clearly(self):
        """The commonest setup mistake: the Telethon account was never added to the
        group. The message has to say that, not «entity not found»."""
        client = _FakeClient([], entity_fails=True, dialogs=[])

        with pytest.raises(ChatArchiveError, match="membro del gruppo"):
            await chat_archive._resolve_entity(client, GROUP_ID)


class TestRecoverAdjustsShortFiles:
    def test_a_file_shorter_than_the_manifest_rewinds_the_offset(self, tmp_path):
        """The opposite of a partial tail: the file was restored from an older copy.
        Trusting the manifest would append past the end and corrupt the archive."""
        arc = tmp_path / "chat-archive.jsonl.gz"
        w = GzipMemberWriter(arc).open()
        w.write_record({"id": 1})
        committed = w.commit()
        manifest = {"committed_offset": committed + 10_000, "last_message_id": 1}

        chat_archive._recover(arc, manifest)

        assert manifest["committed_offset"] == committed
        assert _ids(arc) == [1]

    def test_a_missing_file_with_an_empty_manifest_changes_nothing(self, tmp_path):
        manifest: dict = {}

        chat_archive._recover(tmp_path / "absent.jsonl.gz", manifest)

        assert manifest == {}
