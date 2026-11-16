"""Public Telegram command menus stay aligned with the public help catalog."""

from __future__ import annotations

from main import _ADMIN_EXTRA_COMMANDS, _GROUP_COMMANDS, _PRIVATE_COMMANDS


_DESCRIPTION = "🐲 Gioca con Alduino e consulta lo stato"


def test_image_command_is_only_in_admin_menu():
    assert not any(c.command=="immagine" for c in _PRIVATE_COMMANDS+_GROUP_COMMANDS)
    assert sum(c.command=="immagine" for c in _ADMIN_EXTRA_COMMANDS)==1


def _count(commands):
    return sum(command.command == "gioco_alduino" for command in commands)


def test_secret_game_play_command_is_public_for_every_role():
    assert _count(_PRIVATE_COMMANDS) == 1
    assert _count(_GROUP_COMMANDS) == 1
    assert _count(_ADMIN_EXTRA_COMMANDS) == 0
    for commands in (_PRIVATE_COMMANDS, _GROUP_COMMANDS):
        assert next(c for c in commands if c.command == "gioco_alduino").description == _DESCRIPTION


def test_secret_game_commands_keep_the_removed_short_alias_disabled():
    assert all(command.command != "gioco" for command in _PRIVATE_COMMANDS + _GROUP_COMMANDS + _ADMIN_EXTRA_COMMANDS)


def test_secret_game_management_has_its_own_admin_only_command():
    name = "gestisci_gioco_alduino"
    assert not any(c.command == name for c in _PRIVATE_COMMANDS + _GROUP_COMMANDS)
    assert sum(c.command == name for c in _ADMIN_EXTRA_COMMANDS) == 1
