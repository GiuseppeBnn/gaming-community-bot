from __future__ import annotations

import pytest

from handlers import dice as handler
from utils import dice


class _Message:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


def test_generic_die_is_inclusive_and_validates_sides(monkeypatch):
    seen: list[int] = []

    def fixed_randbelow(bound: int) -> int:
        seen.append(bound)
        return bound - 1

    monkeypatch.setattr(dice.secrets, "randbelow", fixed_randbelow)
    assert dice.d20() == 20
    assert seen == [20]
    with pytest.raises(ValueError, match="at least one"):
        dice.roll(0)


async def test_d20_command_answers_with_the_bare_number(monkeypatch):
    monkeypatch.setattr(dice, "d20", lambda: 7)
    message = _Message()
    await handler.cmd_d20(message)
    assert message.answers == ["7"]
