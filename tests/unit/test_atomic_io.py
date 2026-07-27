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


# ---------------------------------------------------------------------------
# The failure paths
# ---------------------------------------------------------------------------
#
# This module exists to make a crash survivable, so its error handling *is* the
# feature. All of it is unreachable in a normal run — a full disk, a read-only
# mount, a directory that fsync doesn't support — hence the monkeypatching: the
# alternative is that the code that runs only on a bad day is the code nobody ever
# executed.

import os

import pytest

from utils import atomic_io


class TestProbeWritable:
    def test_a_writable_directory_reports_no_problem(self, tmp_path):
        assert atomic_io.probe_writable(tmp_path) is None

    def test_a_directory_that_cannot_be_created_is_reported(self, tmp_path, monkeypatch):
        """Startup calls this to fail loudly instead of discovering at 4am that no
        backup was ever written."""
        def _no_mkdir(self, *a, **kw):
            raise OSError("read-only file system")

        monkeypatch.setattr(atomic_io.Path, "mkdir", _no_mkdir)

        problem = atomic_io.probe_writable(tmp_path / "nuova")

        assert problem and "impossibile creare" in problem

    def test_a_directory_that_cannot_be_written_is_reported(self, tmp_path, monkeypatch):
        """It exists and can be listed, but the process cannot write in it — the
        message carries the uid, because that is the thing to fix."""
        real_open = open

        def _no_write(path, mode="r", *a, **kw):
            if "w" in mode and ".write-probe" in str(path):
                raise OSError("permission denied")
            return real_open(path, mode, *a, **kw)

        monkeypatch.setattr("builtins.open", _no_write)

        problem = atomic_io.probe_writable(tmp_path)

        assert problem and "non consentita" in problem

    def test_the_probe_file_never_survives(self, tmp_path):
        atomic_io.probe_writable(tmp_path)

        assert list(tmp_path.glob(".write-probe*")) == []


class TestFsyncDir:
    def test_a_platform_without_directory_fsync_is_not_an_error(self, tmp_path, monkeypatch):
        """Windows can't fsync a directory. Best-effort means best-effort: the write
        already happened and must not be undone by a durability nicety."""
        def _no_open(*a, **kw):
            raise OSError("not supported")

        monkeypatch.setattr(os, "open", _no_open)

        atomic_io._fsync_dir(tmp_path)  # must not raise


class TestAtomicWriteFailures:
    def test_a_failed_replace_raises_and_leaves_no_temp_file(self, tmp_path, monkeypatch):
        """The caller must learn the write failed — and the half-written temp file
        must not be left behind to be mistaken for a snapshot."""
        def _no_replace(src, dst):
            raise OSError("no space left on device")

        monkeypatch.setattr(os, "replace", _no_replace)
        target = tmp_path / "stato.json"

        with pytest.raises(OSError):
            atomic_io.atomic_write_bytes(target, b"dati")

        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_the_original_file_survives_a_failed_overwrite(self, tmp_path, monkeypatch):
        """This is the whole point of writing to a temp file first."""
        target = tmp_path / "stato.json"
        atomic_io.atomic_write_bytes(target, b"buono")

        def _no_replace(src, dst):
            raise OSError("no space left on device")

        monkeypatch.setattr(os, "replace", _no_replace)
        with pytest.raises(OSError):
            atomic_io.atomic_write_bytes(target, b"rotto")

        assert target.read_bytes() == b"buono"


class TestGzipWriterFailures:
    def test_an_archive_that_cannot_be_opened_raises(self, tmp_path, monkeypatch):
        def _no_mkdir(self, *a, **kw):
            raise OSError("read-only file system")

        monkeypatch.setattr(atomic_io.Path, "mkdir", _no_mkdir)

        with pytest.raises(OSError):
            atomic_io.GzipMemberWriter(tmp_path / "sub" / "a.gz").open()

    def test_rollback_survives_a_close_that_fails(self, tmp_path):
        """Rollback runs while something has already gone wrong; raising from the
        cleanup would replace the real error with this one."""
        class _BadCloser:
            def close(self):
                raise RuntimeError("already closed")

        writer = atomic_io.GzipMemberWriter(tmp_path / "a.gz").open()
        writer.write_record({"id": 1})
        writer._gz = _BadCloser()
        writer._fileobj = _BadCloser()

        writer.rollback()  # must not raise

        assert writer._gz is None and writer._fileobj is None
