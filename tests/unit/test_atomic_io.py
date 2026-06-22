"""Tests for utils.atomic_io — crash-safe file primitives."""

from __future__ import annotations

import gzip
import hashlib
import json

from utils.atomic_io import (
    GzipMemberWriter,
    atomic_write_bytes,
    read_json,
    sha256_file,
    truncate_file,
    write_json_atomic,
)


def _gz_lines(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Atomic JSON / bytes
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_write_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "a" / "b" / "x.json"
        write_json_atomic(p, {"k": 1})
        assert read_json(p) == {"k": 1}

    def test_overwrite_replaces_content(self, tmp_path):
        p = tmp_path / "f.bin"
        atomic_write_bytes(p, b"one")
        atomic_write_bytes(p, b"two")
        assert p.read_bytes() == b"two"

    def test_no_tmp_files_left_behind(self, tmp_path):
        atomic_write_bytes(tmp_path / "f.bin", b"data")
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    def test_read_json_missing_is_none(self, tmp_path):
        assert read_json(tmp_path / "nope.json") is None

    def test_read_json_corrupt_is_none(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json", encoding="utf-8")
        assert read_json(p) is None


# ---------------------------------------------------------------------------
# Checksums / truncation
# ---------------------------------------------------------------------------

class TestChecksumTruncate:
    def test_sha256_matches_hashlib(self, tmp_path):
        p = tmp_path / "f.bin"
        data = b"hello world" * 5000
        p.write_bytes(data)
        assert sha256_file(p) == hashlib.sha256(data).hexdigest()

    def test_truncate_shortens_file(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"0123456789")
        truncate_file(p, 4)
        assert p.read_bytes() == b"0123"


# ---------------------------------------------------------------------------
# Append-only gzip member log
# ---------------------------------------------------------------------------

class TestGzipMemberWriter:
    def test_single_member_roundtrip(self, tmp_path):
        p = tmp_path / "a.jsonl.gz"
        w = GzipMemberWriter(p).open()
        w.write_record({"id": 1})
        w.write_record({"id": 2})
        end = w.commit()
        assert end == p.stat().st_size
        assert _gz_lines(p) == [{"id": 1}, {"id": 2}]

    def test_appended_members_are_concatenated(self, tmp_path):
        p = tmp_path / "a.jsonl.gz"
        w1 = GzipMemberWriter(p).open()
        w1.write_record({"id": 1})
        w1.commit()
        w2 = GzipMemberWriter(p).open()
        w2.write_record({"id": 2})
        w2.commit()
        # gzip transparently decompresses the two concatenated members.
        assert _gz_lines(p) == [{"id": 1}, {"id": 2}]

    def test_rollback_restores_prior_length(self, tmp_path):
        p = tmp_path / "a.jsonl.gz"
        w1 = GzipMemberWriter(p).open()
        w1.write_record({"id": 1})
        w1.commit()
        good_size = p.stat().st_size

        w2 = GzipMemberWriter(p).open()
        w2.write_record({"id": 2})
        w2.rollback()  # simulate a crash mid-member

        assert p.stat().st_size == good_size
        assert _gz_lines(p) == [{"id": 1}]
