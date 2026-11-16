"""Public Telegram command menus stay aligned with the public help catalog."""

from __future__ import annotations

from main import _ADMIN_EXTRA_COMMANDS, _GROUP_COMMANDS, _PRIVATE_COMMANDS


_DESCRIPTION = "🐲 Gioco di Alduino: stato o gestione"


def test_image_command_is_only_in_admin_menu():
    assert not any(c.command=="immagine" for c in _PRIVATE_COMMANDS+_GROUP_COMMANDS)
    assert sum(c.command=="immagine" for c in _ADMIN_EXTRA_COMMANDS)==1


def _count(commands):
    return sum(command.command == "gioco_alduino" for command in commands)


def test_secret_game_command_is_public_with_role_based_behavior():
    assert _count(_PRIVATE_COMMANDS) == 1
    assert _count(_GROUP_COMMANDS) == 1
    assert _count(_ADMIN_EXTRA_COMMANDS) == 0
    for commands in (_PRIVATE_COMMANDS, _GROUP_COMMANDS):
        assert next(c for c in commands if c.command == "gioco_alduino").description == _DESCRIPTION


def test_secret_game_has_only_one_command_without_the_removed_alias():
    assert all(command.command != "gioco" for command in _PRIVATE_COMMANDS + _GROUP_COMMANDS + _ADMIN_EXTRA_COMMANDS)
