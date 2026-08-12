"""Verified, operator-overridable catalog for Alduino's twenty questions."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from config_data.config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GameDossier:
    key: str
    title: str
    aliases: tuple[str, ...]
    dossier: str


_DEFAULTS = (
    GameDossier("minecraft", "Minecraft", ("minecraft java", "minecraft bedrock"),
        "Sandbox di Mojang pubblicato nel 2011. Mondo 3D a blocchi generato proceduralmente; "
        "si raccolgono risorse, si costruisce e si sopravvive. Prima e terza persona, multiplayer, "
        "nessun protagonista nominato obbligatorio; Creeper e Ender Dragon sono iconici."),
    GameDossier("portal_2", "Portal 2", ("portal two",),
        "Puzzle game in prima persona di Valve del 2011. La protagonista è Chell; usa una portal gun "
        "nei laboratori Aperture Science. GLaDOS e Wheatley sono personaggi centrali. Ha campagna "
        "single player e cooperativa; non è un open world e non usa combattimento tradizionale."),
    GameDossier("dark_souls", "Dark Souls", ("dark souls 1", "dark souls remastered"),
        "Action RPG fantasy di FromSoftware del 2011, ambientato a Lordran. Visuale in terza persona, "
        "combattimento impegnativo, falò, anime come risorsa e multiplayer asincrono. Il protagonista "
        "è un non morto personalizzabile; non è a turni né uno sparatutto."),
    GameDossier("skyrim", "The Elder Scrolls V: Skyrim", ("skyrim", "tes v skyrim"),
        "Action RPG open world fantasy di Bethesda del 2011. Ambientato nella provincia nordica di "
        "Skyrim; protagonista personalizzabile chiamato Sangue di Drago. Draghi, urla Thu'um, gilde, "
        "prima o terza persona. È principalmente single player e non è fantascienza."),
    GameDossier("witcher_3", "The Witcher 3: Wild Hunt", ("the witcher 3", "witcher 3"),
        "Action RPG open world fantasy di CD Projekt Red del 2015. Protagonista Geralt di Rivia, "
        "cacciatore di mostri; cerca Ciri. Terza persona, scelte narrative, spade e Segni magici. "
        "Include il gioco di carte Gwent ed è principalmente single player."),
    GameDossier("hollow_knight", "Hollow Knight", ("hollowknight",),
        "Metroidvania 2D di Team Cherry del 2017, disegnato a mano. Ambientato nel regno sotterraneo "
        "di Hallownest popolato da insetti. Il Cavaliere usa un aculeo; esplorazione interconnessa, "
        "boss e recupero della valuta dopo la morte. È single player e non è 3D."),
    GameDossier("breath_wild", "The Legend of Zelda: Breath of the Wild", ("breath of the wild", "botw"),
        "Action adventure open world Nintendo del 2017 per Wii U e Switch. Protagonista Link, "
        "ambientazione Hyrule dopo una calamità; Zelda è centrale. Terza persona, fisica sistemica, "
        "santuari, arrampicata e armi degradabili. È single player."),
    GameDossier("elden_ring", "Elden Ring", ("eldenring",),
        "Action RPG fantasy open world di FromSoftware del 2022. Ambientato nell'Interregno; "
        "protagonista Senzaluce personalizzabile. Terza persona, combattimento impegnativo, boss, "
        "Siti di Grazia e cavalcatura Torrente. Ha componenti multiplayer ma non è un MMO."),
)

_catalog: tuple[GameDossier, ...] = _DEFAULTS


def _valid(row: dict[str, str]) -> GameDossier | None:
    key, title, dossier = (row.get(name, "").strip() for name in ("key", "title", "dossier"))
    if not key or not title or len(dossier) < 80:
        return None
    aliases = tuple(value.strip() for value in row.get("aliases", "").split("|") if value.strip())
    return GameDossier(key[:64], title[:200], aliases, dossier)


def init_catalog(catalog_dir: str | None = None) -> int:
    global _catalog
    path = Path(catalog_dir or settings.catalog_dir) / "twenty_questions_games.csv"
    if not path.exists():
        _catalog = _DEFAULTS
        return len(_catalog)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = tuple(item for row in csv.DictReader(handle) if (item := _valid(row)))
        if not rows or len({item.key for item in rows}) != len(rows):
            raise ValueError("catalogo vuoto o key duplicate")
        _catalog = rows
    except (OSError, csv.Error, ValueError) as exc:
        log.warning("Catalogo 20 domande illeggibile (%s); uso i default.", exc)
        _catalog = _DEFAULTS
    return len(_catalog)


def all_games() -> tuple[GameDossier, ...]:
    return _catalog
