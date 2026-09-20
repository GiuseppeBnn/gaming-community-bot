"""Small, reusable dice primitives for local game mechanics."""

from __future__ import annotations

import secrets


def roll(sides: int) -> int:
    """Return an unbiased integer from 1 through ``sides`` (inclusive)."""
    if sides < 1:
        raise ValueError("a die needs at least one side")
    return secrets.randbelow(sides) + 1


def d20() -> int:
    return roll(20)
