import pytest

from services import twenty_questions_catalog as catalog


@pytest.fixture(autouse=True)
def _reset_catalog(tmp_path):
    yield
    catalog.init_catalog(str(tmp_path / "missing"))


def test_missing_file_uses_builtins(tmp_path):
    assert catalog.init_catalog(str(tmp_path)) >= 8
    assert any(game.title == "Minecraft" for game in catalog.all_games())


def test_valid_csv_replaces_catalog_and_parses_aliases(tmp_path):
    path = tmp_path / "twenty_questions_games.csv"
    path.write_text(
        "key,title,aliases,dossier\n"
        "doom,Doom,doom 1993|ultimate doom,"
        "Sparatutto in prima persona di id Software del 1993 ambientato su Marte e all'inferno; "
        "il protagonista combatte demoni con armi da fuoco in livelli labirintici.\n",
        encoding="utf-8",
    )
    assert catalog.init_catalog(str(tmp_path)) == 1
    assert catalog.all_games()[0].aliases == ("doom 1993", "ultimate doom")


def test_invalid_or_duplicate_catalog_degrades_to_builtins(tmp_path):
    path = tmp_path / "twenty_questions_games.csv"
    path.write_text(
        "key,title,aliases,dossier\n"
        "same,Uno,," + "f" * 90 + "\n"
        "same,Due,," + "g" * 90 + "\n",
        encoding="utf-8",
    )
    assert catalog.init_catalog(str(tmp_path)) >= 8

    path.write_text("key,title,aliases,dossier\nbad,,,corto\n", encoding="utf-8")
    assert catalog.init_catalog(str(tmp_path)) >= 8
