"""Public Telegram command menus stay aligned with the public help catalog."""

from __future__ import annotations

from main import _ADMIN_EXTRA_COMMANDS, _GROUP_COMMANDS, _PRIVATE_COMMANDS


_DESCRIPTION = "🐲 Regole e stato del gioco segreto"


def _count(commands):
    return sum(command.command == "gioco_alduino" for command in commands)


def test_secret_game_command_is_once_in_each_public_menu():
    private = [command for command in _PRIVATE_COMMANDS if command.command == "gioco_alduino"]
    group = [command for command in _GROUP_COMMANDS if command.command == "gioco_alduino"]

    assert len(private) == len(group) == 1
    assert private[0].description == group[0].description == _DESCRIPTION


def test_secret_game_command_is_not_an_admin_extra():
    assert _count(_ADMIN_EXTRA_COMMANDS) == 0
