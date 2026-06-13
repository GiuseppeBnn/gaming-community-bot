"""
Crash-safe file primitives shared by the backup subsystem.

Two guarantees, both designed for a low-RAM host:

  * **Atomic publish** — a file is written to a sibling ``*.tmp``, ``fsync``-ed,
    then ``os.replace``-d onto its final name. A crash mid-write never corrupts
    the previous good file (rename is atomic on POSIX).
  * **Append-only gzip log** — the chat archive is a concatenation of independent
    gzip *members* (one per backup range; ``gunzip`` reads them transparently).
    A ``GzipMemberWriter`` appends one member and, on failure, truncates back to
    where it started. A sidecar manifest records the last *committed* byte offset
    so a hard crash is healed on the next run by truncating the partial tail.

Nothing here buffers a whole dataset in memory: writers stream record by record.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_TMP_SUFFIX = ".tmp"
_SHA_CHUNK = 1 << 20  # 1 MiB streaming read — constant memory regardless of size


def probe_writable(directory: str | os.PathLike[str]) -> str | None:
    """Check that `directory` exists (creating it) and is writable.

    Returns ``None`` when writable, else a short human-readable reason suitable
    for logging. Never raises — used as a cheap pre-flight so a mis-mounted volume
    degrades to a clear warning instead of a stack trace on every backup tick.
    """
    d = Path(directory)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"impossibile creare {d}: {e}"
    probe = d / f".write-probe.{os.getpid()}"
    try:
        with open(probe, "wb") as f:
            f.write(b"ok")
        probe.unlink(missing_ok=True)
    except OSError as e:
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        return f"scrittura non consentita in {d} (uid={uid}): {e}"
    return None


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory so a rename/replace is durable.

    Not supported on every platform (e.g. Windows) — failures are ignored.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _tmp_path(path: Path) -> Path:
    # Same directory as the target so os.replace stays on one filesystem.
    return path.with_name(f".{path.name}.{os.getpid()}{_TMP_SUFFIX}")


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Write `data` to `path` atomically (tmp + fsync + replace)."""
    path = Path(path)
    tmp = _tmp_path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except OSError as e:
        log.error("Scrittura atomica fallita su %s: %s", path, e)
        raise
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def write_json_atomic(path: str | os.PathLike[str], obj: Any) -> None:
    """Serialize `obj` to JSON and write it atomically."""
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))


def read_json(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Load a JSON object, or None if the file is missing or unreadable/corrupt."""
    try:
        with open(path, "rb") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Streaming SHA-256 of a file (constant memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_SHA_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def truncate_file(path: str | os.PathLike[str], size: int) -> None:
    """Truncate `path` to exactly `size` bytes and fsync it."""
    with open(path, "r+b") as f:
        f.truncate(size)
        f.flush()
        os.fsync(f.fileno())
    _fsync_dir(Path(path).parent)


class GzipMemberWriter:
    """Append a single gzip member to a file, record by record.

    Usage::

        w = GzipMemberWriter(path).open()
        try:
            for obj in items:
                w.write_record(obj)
            end_offset = w.commit()
        except BaseException:
            w.rollback()   # truncates the half-written member, file stays valid
            raise

    The member is a self-contained gzip stream; appending more members later is
    valid (gzip concatenation). `start_offset` is captured at ``open()`` so a
    rollback (or an external recovery) can restore the file to its prior length.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.start_offset = 0
        self.records = 0
        self._fileobj = None
        self._gz: gzip.GzipFile | None = None

    def open(self) -> "GzipMemberWriter":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.start_offset = self.path.stat().st_size if self.path.exists() else 0
            self._fileobj = open(self.path, "ab")
        except OSError as e:
            log.error("Apertura archivio backup fallita su %s: %s", self.path, e)
            raise
        # mtime=0 → deterministic header (no wall-clock in the gzip stream).
        self._gz = gzip.GzipFile(fileobj=self._fileobj, mode="wb", mtime=0)
        return self

    def write_record(self, obj: Any) -> None:
        assert self._gz is not None, "writer not opened"
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._gz.write(line.encode("utf-8"))
        self.records += 1

    def commit(self) -> int:
        """Close the member, fsync, and return the new end-of-file offset."""
        assert self._gz is not None and self._fileobj is not None
        self._gz.close()
        self._fileobj.flush()
        os.fsync(self._fileobj.fileno())
        end = self._fileobj.tell()
        self._fileobj.close()
        self._gz = None
        self._fileobj = None
        _fsync_dir(self.path.parent)
        return end

    def rollback(self) -> None:
        """Discard the (possibly partial) member, restoring the prior length."""
        for closer in (self._gz, self._fileobj):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        self._gz = None
        self._fileobj = None
        if self.path.exists() and self.path.stat().st_size > self.start_offset:
            truncate_file(self.path, self.start_offset)
