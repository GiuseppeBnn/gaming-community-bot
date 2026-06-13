"""
Total-state export / import — engine-agnostic logical dump of the whole DB.

A ``pg_dump`` would not survive a SQLite↔Postgres migration; this dumps every
table's rows as gzip-compressed JSONL instead, so coins/XP/streaks/trophies/bets
restore byte-for-byte onto any engine. Both directions **stream** (server-side
cursor + per-row write) so memory stays flat on a 300 MB host.

File layout (one snapshot)::

    state-YYYYMMDD-HHMMSS.jsonl.gz       # header line + one {"t","r"} line per row
    state-YYYYMMDD-HHMMSS.jsonl.gz.sha256
    state-latest.jsonl.gz                # hardlink to the newest snapshot (atomic swap)
    state-latest.jsonl.gz.sha256

Snapshots are published atomically (tmp → fsync → os.replace) and rotated, so a
failed export can never clobber the last good one. STEERING §5: the service does
not commit — the caller (CLI/handler) owns the transaction.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Date, DateTime

from config_data.config import settings
from database.models import Base
from utils.atomic_io import atomic_write_bytes, sha256_file

SCHEMA_VERSION = 1
_YIELD = 500          # server-side cursor batch
_INSERT_BATCH = 500   # rows per executemany on import
_LATEST_NAME = "state-latest.jsonl.gz"


class StateExportError(Exception):
    """Raised when an import is unsafe (checksum/schema/non-empty DB)."""


@dataclass
class ImportReport:
    tables: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.tables.values())


# ---------------------------------------------------------------------------
# Value (de)serialization — only datetime/date need special handling; every
# other column type in this schema is already JSON-native.
# ---------------------------------------------------------------------------

def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _temporal_cols(table) -> dict[str, type]:
    """{column_name: datetime|date} for columns needing parse-back on import."""
    out: dict[str, type] = {}
    for col in table.columns:
        if isinstance(col.type, DateTime):
            out[col.name] = datetime
        elif isinstance(col.type, Date):
            out[col.name] = date
    return out


def _parse_temporal(kind: type, raw: str) -> Any:
    return datetime.fromisoformat(raw) if kind is datetime else date.fromisoformat(raw)


def _dialect_name(session: AsyncSession) -> str:
    try:
        bind = session.bind
        dialect = getattr(bind, "dialect", None) or getattr(
            getattr(bind, "sync_engine", None), "dialect", None
        )
        return getattr(dialect, "name", "unknown")
    except Exception:  # noqa: BLE001 — purely informational field
        return "unknown"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _fsync_path(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def export_state(
    session: AsyncSession,
    dest_dir: str | os.PathLike[str],
    *,
    keep: int | None = None,
) -> Path:
    """Stream the whole DB into a fresh, atomically-published snapshot.

    Returns the path to the timestamped snapshot. Also refreshes
    ``state-latest.jsonl.gz`` and prunes old snapshots beyond `keep`.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    keep = settings.backup_state_keep if keep is None else keep

    tables = list(Base.metadata.sorted_tables)  # FK-safe (parents first)
    counts = {
        t.name: (await session.execute(select(func.count()).select_from(t))).scalar_one()
        for t in tables
    }
    ts = datetime.now(timezone.utc)
    header = {
        "_": "header",
        "schema_version": SCHEMA_VERSION,
        "created_at": ts.isoformat(),
        "dialect": _dialect_name(session),
        "tables": [t.name for t in tables],
        "counts": counts,
    }

    tmp = dest / f".state-{ts:%Y%m%d-%H%M%S}.{os.getpid()}.tmp"
    written = 0
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as gz:
            gz.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n")
            for table in tables:
                colnames = list(table.c.keys())
                stmt = select(*[table.c[c] for c in colnames]).execution_options(yield_per=_YIELD)
                result = await session.stream(stmt)
                async for row in result:
                    rec = {colnames[i]: _to_jsonable(row[i]) for i in range(len(colnames))}
                    gz.write(
                        json.dumps({"t": table.name, "r": rec}, ensure_ascii=False,
                                   separators=(",", ":")) + "\n"
                    )
                    written += 1
                    if written % 1000 == 0:
                        await asyncio.sleep(0)  # cooperative: don't starve the loop
        _fsync_path(tmp)
        digest = sha256_file(tmp)

        snapshot = dest / f"state-{ts:%Y%m%d-%H%M%S}.jsonl.gz"
        os.replace(tmp, snapshot)
        atomic_write_bytes(
            snapshot.with_name(snapshot.name + ".sha256"),
            f"{digest}  {snapshot.name}\n".encode("utf-8"),
        )
        _publish_latest(dest, snapshot, digest)
        _prune(dest, keep)
        return snapshot
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _publish_latest(dest: Path, snapshot: Path, digest: str) -> None:
    """Point `state-latest.jsonl.gz` at `snapshot` via an atomic name swap.

    Uses a hardlink (same inode, zero extra bytes) when the filesystem allows it,
    falling back to a copy. The previous timestamped snapshots keep their own
    inodes, so swapping `latest` never disturbs the rotation history.
    """
    latest = dest / _LATEST_NAME
    link_tmp = dest / f".{_LATEST_NAME}.{os.getpid()}.tmp"
    if link_tmp.exists():
        link_tmp.unlink()
    try:
        os.link(snapshot, link_tmp)
    except OSError:
        shutil.copyfile(snapshot, link_tmp)
    os.replace(link_tmp, latest)
    atomic_write_bytes(
        latest.with_name(latest.name + ".sha256"),
        f"{digest}  {latest.name}\n".encode("utf-8"),
    )


def _prune(dest: Path, keep: int) -> None:
    snapshots = sorted(
        (p for p in dest.glob("state-*.jsonl.gz") if p.name != _LATEST_NAME),
        reverse=True,
    )
    for stale in snapshots[max(keep, 0):]:
        stale.unlink(missing_ok=True)
        stale.with_name(stale.name + ".sha256").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Import / restore
# ---------------------------------------------------------------------------

async def import_state(
    session: AsyncSession,
    src_path: str | os.PathLike[str],
    *,
    mode: str = "empty",
) -> ImportReport:
    """Restore a snapshot into `session`'s DB. Does NOT commit (caller owns it).

    `mode="empty"` (default) refuses to run unless every table is empty — the
    safe path for a migration into a fresh DB. `mode="replace"` wipes all tables
    first (destructive; reserved for the CLI with explicit intent).
    """
    src = Path(src_path)
    _verify_checksum(src)

    tables_by_name = {t.name: t for t in Base.metadata.sorted_tables}

    with gzip.open(src, "rt", encoding="utf-8") as gz:
        header = json.loads(gz.readline() or "{}")
        if header.get("_") != "header":
            raise StateExportError("Header dello snapshot mancante o non valido.")
        if header.get("schema_version") != SCHEMA_VERSION:
            raise StateExportError(
                f"schema_version {header.get('schema_version')} != {SCHEMA_VERSION} atteso."
            )

        await _prepare_target(session, mode)

        report = ImportReport()
        cur_table: str | None = None
        temporal: dict[str, type] = {}
        buf: list[dict[str, Any]] = []

        async def flush() -> None:
            if buf and cur_table is not None:
                await session.execute(insert(tables_by_name[cur_table]), buf)
                buf.clear()

        for line in gz:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            tname, row = rec["t"], rec["r"]
            if tname not in tables_by_name:
                continue  # unknown table (older/newer schema) — skip defensively
            if tname != cur_table:
                await flush()
                cur_table = tname
                temporal = _temporal_cols(tables_by_name[tname])
            for col, kind in temporal.items():
                val = row.get(col)
                if isinstance(val, str):
                    row[col] = _parse_temporal(kind, val)
            buf.append(row)
            report.tables[tname] = report.tables.get(tname, 0) + 1
            if len(buf) >= _INSERT_BATCH:
                await flush()
        await flush()

    return report


def _verify_checksum(src: Path) -> None:
    sidecar = src.with_name(src.name + ".sha256")
    if not sidecar.exists():
        return
    expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
    actual = sha256_file(src)
    if expected != actual:
        raise StateExportError(
            f"Checksum non corrispondente per {src.name}: file corrotto o manomesso."
        )


async def _prepare_target(session: AsyncSession, mode: str) -> None:
    if mode == "empty":
        for table in Base.metadata.sorted_tables:
            n = (await session.execute(select(func.count()).select_from(table))).scalar_one()
            if n:
                raise StateExportError(
                    f"La tabella '{table.name}' non è vuota ({n} righe). "
                    "Usa un DB vuoto, oppure mode='replace' per sovrascrivere."
                )
    elif mode == "replace":
        for table in reversed(Base.metadata.sorted_tables):  # children first (FK-safe)
            await session.execute(table.delete())
    else:
        raise ValueError(f"mode sconosciuto: {mode!r} (usa 'empty' o 'replace').")


def latest_snapshot_path(dest_dir: str | os.PathLike[str]) -> Path:
    return Path(dest_dir) / _LATEST_NAME
