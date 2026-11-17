from __future__ import annotations

import pytest

from services.title_matcher import matches_title


@pytest.mark.parametrize("candidate,canonical", [
    ("GTA 5", "Grand Theft Auto V"),
    ("gta5", "Grand Theft Auto V"),
    ("Grand Theft Auto 5", "Grand Theft Auto V"),
    (" FF7 ", "Final Fantasy VII"),
    ("Portal2", "Portal 2"),
    ("Pokémon Écarlate", "Pokemon Ecarlate"),
    ("DARK SOULS 3", "Dark Souls III"),
])
def test_equivalent_spelling_is_local(candidate, canonical):
    assert matches_title(candidate, canonical)


@pytest.mark.parametrize("candidate,canonical", [
    ("GTA", "Grand Theft Auto V"),
    ("GTA 4", "Grand Theft Auto V"),
    ("Grand Theft Auto Online", "Grand Theft Auto V"),
    ("Mega Man 10", "Mega Man X"),
    ("Portal", "Portal 2"),
    ("Resident Evil 2 Remake", "Resident Evil 2"),
    ("Doom Eternal", "Doom"),
    ("1 am alive", "I Am Alive"),
    ("GTA 5 o GTA 4", "Grand Theft Auto V"),
    ("", ""),
])
def test_ambiguous_or_different_game_never_wins_locally(candidate, canonical):
    assert not matches_title(candidate, canonical)


def test_explicit_aliases_remain_authoritative():
    assert matches_title("Portal Two", "Portal 2", ("portal two",))
