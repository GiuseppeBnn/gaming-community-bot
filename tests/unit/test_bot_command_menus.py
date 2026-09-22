"""Public Telegram command menus stay aligned with the public help catalog."""

from __future__ import annotations

from main import _ADMIN_EXTRA_COMMANDS, _GROUP_COMMANDS, _PRIVATE_COMMANDS


_DESCRIPTION = "🐲 Regole e stato del gioco segreto"


def _count(commands):
    return sum(command.command == "gioco_alduino" for command in commands)


def test_secret_game_command_is_admin_only_while_in_development():
    """Temporarily restricted: it must not leak into the public menus, only
    into the admin one."""
    assert _count(_PRIVATE_COMMANDS) == 0
    assert _count(_GROUP_COMMANDS) == 0

    admin_extra = [
        command for command in _ADMIN_EXTRA_COMMANDS if command.command == "gioco_alduino"
    ]
    assert len(admin_extra) == 1
    assert admin_extra[0].description == _DESCRIPTION
