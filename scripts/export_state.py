#!/usr/bin/env python3
"""
Export the bot's total state to a portable snapshot (migration-safe).

    python scripts/export_state.py [--dir backups]

Writes ``state-YYYYMMDD-HHMMSS.jsonl.gz`` (+ ``state-latest.jsonl.gz``) under the
backup dir. Restore later with ``scripts/import_state.py`` — values (coins, XP,
streaks, trophies, bets …) survive a SQLite↔Postgres migration unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make `src/` importable when run as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config_data.config import settings  # noqa: E402
from database.connection import async_session_maker  # noqa: E402
from services.backup import state_export  # noqa: E402


async def _run(dest: str) -> None:
    async with async_session_maker() as session:
        path = await state_export.export_state(session, dest)
    print(f"✅ Stato esportato: {path}  ({path.stat().st_size:,} byte)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Esporta lo stato totale del bot.")
    parser.add_argument("--dir", default=settings.backup_dir, help="dir di destinazione")
    args = parser.parse_args()
    asyncio.run(_run(args.dir))


if __name__ == "__main__":
    main()
