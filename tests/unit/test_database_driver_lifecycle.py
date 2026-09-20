from __future__ import annotations

import subprocess
import sys


_UNSCOPED_ENGINE_SCRIPT = """
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


asyncio.run(main())
print("clean-exit", flush=True)
"""


def test_aiosqlite_worker_cannot_keep_the_process_alive() -> None:
    """Guard the SQLAlchemy/aiosqlite compatibility contract.

    The application closes its engine explicitly, but an import/startup failure can
    happen before normal shutdown.  The driver pair must still let Python exit; an
    incompatible pair used to finish this script and then retain a non-daemon worker
    forever, which left GitHub Actions running until its six-hour default timeout.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", _UNSCOPED_ENGINE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "SQLAlchemy/aiosqlite left a worker alive after the event loop stopped"
        ) from exc

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean-exit"
