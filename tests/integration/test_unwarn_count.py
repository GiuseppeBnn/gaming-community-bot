"""Regression test for the /unwarn remaining-count bug.

With the session's autoflush disabled, clearing a warning in memory was not
reflected by the subsequent count query — so removing 1 of 2 warns reported
"restano 2". clear_warnings() now flushes; the count must be exact.
"""

from __future__ import annotations

from services import admin_service


async def _add(session, user_id: int, n: int) -> None:
    for _ in range(n):
        await admin_service.add_warning(session, user_id, group_id=-100, issued_by_tg_id=1, reason=None)
    await session.commit()


async def test_unwarn_one_of_two_leaves_one(session, user_factory):
    await user_factory(50, "target")
    await _add(session, 50, 2)
    assert await admin_service.active_warning_count(session, 50) == 2

    cleared = await admin_service.clear_warnings(session, 50, count=1)
    assert cleared == 1
    # The bug: this used to still read 2 (pre-flush rows).
    assert await admin_service.active_warning_count(session, 50) == 1


async def test_clear_all(session, user_factory):
    await user_factory(51, "target2")
    await _add(session, 51, 3)
    cleared = await admin_service.clear_warnings(session, 51, count=None)
    assert cleared == 3
    assert await admin_service.active_warning_count(session, 51) == 0
