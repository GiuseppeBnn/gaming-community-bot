#!/usr/bin/env python3
"""
Restore a state snapshot into the configured database (post-migration).

    python scripts/import_state.py backups/state-latest.jsonl.gz [--mode empty|replace]

`empty` (default) refuses to run unless every table is empty — the safe path for
a brand-new DB. `replace` wipes all tables first (DESTRUCTIVE). The snapshot's
sha256 sidecar is verified before anything is written.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make `src/` importable when run as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from database.connection import async_session_maker  # noqa: E402
from services.backup import state_export  # noqa: E402


async def _run(src: str, mode: str) -> None:
    async with async_session_maker() as session:
        report = await state_export.import_state(session, src, mode=mode)
        await session.commit()
    print(f"✅ Importate {report.total:,} righe in {len(report.tables)} tabelle:")
    for name, n in sorted(report.tables.items()):
        print(f"   • {name}: {n:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reimporta uno snapshot di stato.")
    parser.add_argument("src", help="percorso dello snapshot .jsonl.gz")
    parser.add_argument(
        "--mode", choices=("empty", "replace"), default="empty",
        help="'empty' (richiede DB vuoto) o 'replace' (sovrascrive, distruttivo)",
    )
    args = parser.parse_args()
    if args.mode == "replace":
        confirm = input("⚠️  mode=replace CANCELLA tutti i dati esistenti. Scrivi 'SI' per procedere: ")
        if confirm.strip() != "SI":
            sys.exit("Annullato.")
    asyncio.run(_run(args.src, args.mode))


if __name__ == "__main__":
    main()
