"""Prize schedule shared by every community game with a podium.

These two functions used to live in `quiz_service`. They are pure — no session,
no SQL, no mutation — and the guess games need exactly the same schedule, so
leaving them there would have meant either duplicating the arithmetic that
decides how many coins someone gets, or importing one game from another.
`quiz_service` re-exports both names, so every existing caller is untouched.
"""

from __future__ import annotations

from config_data.config import settings


def participation_floor(consolation: int) -> int:
    """Derive the guaranteed minimum (last-place consolation) from the 4th-place prize.

    floor = max(floor_min, round(consolation * floor_ratio)), but never above the
    consolation itself and never below 0.
    """
    if consolation <= 0:
        return 0
    floor = max(settings.quiz_participation_floor_min,
                round(consolation * settings.quiz_participation_floor_ratio))
    return max(0, min(floor, consolation))


def consolation_amounts(n: int, top: int, floor: int) -> list[int]:
    """Linear, non-increasing consolation schedule for the `n` non-podium finishers.

    Position 0 (4th place) gets `top`; the last gets `floor`; the rest interpolate
    linearly. Everyone gets at least `floor` (and at least 0). Pure function.
    """
    if n <= 0:
        return []
    if top <= 0:
        return [0] * n
    floor = max(0, min(floor, top))
    if n == 1:
        return [top]
    return [
        max(floor, round(top - (top - floor) * i / (n - 1)))
        for i in range(n)
    ]
