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
