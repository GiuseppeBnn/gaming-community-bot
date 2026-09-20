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
    GameDossier("super_mario_bros", "Super Mario Bros.", ("super mario bros", "mario bros"),
        "Platform 2D Nintendo del 1985 per NES. Mario attraversa il Regno dei Funghi per salvare "
        "la Principessa Peach da Bowser; corre, salta sui nemici, rompe blocchi e usa funghi e fiori. "
        "Ha livelli lineari a scorrimento laterale e una modalità alternata per due giocatori."),
    GameDossier("gta_5", "Grand Theft Auto V", ("gta 5", "gta v"),
        "Action adventure open world di Rockstar del 2013 ambientato a Los Santos e Blaine County. "
        "I protagonisti giocabili sono Michael, Franklin e Trevor. Terza o prima persona, veicoli, "
        "rapine e sparatorie; include una campagna single player e GTA Online."),
    GameDossier("red_dead_2", "Red Dead Redemption 2", ("rdr2", "red dead 2"),
        "Action adventure open world western di Rockstar del 2018. Il protagonista è Arthur Morgan, "
        "membro della banda Van der Linde, nell'America del 1899. Cavalli, armi da fuoco, caccia, "
        "onore e accampamento; ha campagna single player e modalità online."),
    GameDossier("god_of_war_2018", "God of War", ("god of war 2018", "gow 2018"),
        "Action adventure di Santa Monica Studio del 2018 ispirato alla mitologia norrena. Kratos "
        "viaggia con il figlio Atreus e usa soprattutto l'Ascia Leviatano. Terza persona con camera "
        "ravvicinata, combattimento, enigmi ed esplorazione; è single player e non open world puro."),
    GameDossier("last_of_us", "The Last of Us", ("the last of us part 1", "tlou"),
        "Action adventure survival di Naughty Dog ambientato negli Stati Uniti dopo un'epidemia da "
        "Cordyceps. Joel accompagna Ellie; terza persona, furtività, armi, crafting e infetti. La "
        "storia è lineare e principalmente single player, non un open world."),
    GameDossier("cyberpunk_2077", "Cyberpunk 2077", ("cyberpunk", "cp2077"),
        "Action RPG open world di CD Projekt Red del 2020 ambientato nella futuristica Night City. "
        "Il protagonista V è personalizzabile; Johnny Silverhand è interpretato da Keanu Reeves. "
        "Prima persona, armi, hacking, impianti cibernetici, veicoli e scelte narrative."),
    GameDossier("doom_1993", "Doom", ("doom 1993", "classic doom"),
        "Sparatutto in prima persona di id Software del 1993. Un marine combatte demoni in basi su "
        "Marte e poi all'Inferno usando armi come shotgun e BFG. Livelli labirintici, chiavi colorate, "
        "azione veloce e grafica 2.5D; include single player e multiplayer."),
    GameDossier("half_life_2", "Half-Life 2", ("half life 2", "hl2"),
        "Sparatutto in prima persona di Valve del 2004. Gordon Freeman combatte il Combine a City 17 "
        "insieme ad Alyx Vance. La gravity gun e la fisica sono centrali; struttura narrativa lineare, "
        "veicoli e puzzle ambientali. È principalmente single player."),
    GameDossier("mass_effect_2", "Mass Effect 2", ("mass effect two", "me2"),
        "Action RPG fantascientifico di BioWare del 2010. Il comandante Shepard recluta una squadra "
        "per una missione suicida contro i Collettori. Terza persona, combattimento con coperture, "
        "dialoghi e scelte importate; ambientazione spaziale e campagna single player."),
    GameDossier("metal_gear_3", "Metal Gear Solid 3: Snake Eater", ("mgs3", "snake eater"),
        "Stealth action di Konami del 2004 ambientato durante la Guerra Fredda. Il protagonista è "
        "Naked Snake, futuro Big Boss; giungla sovietica, mimetizzazione, sopravvivenza e boss. "
        "Visuale in terza persona, forte componente narrativa e campagna single player."),
    GameDossier("resident_evil_4", "Resident Evil 4", ("re4", "resident evil iv"),
        "Survival horror action di Capcom del 2005. Leon S. Kennedy cerca Ashley Graham in un villaggio "
        "europeo controllato dai Ganados. Terza persona sopra la spalla, armi, inventario a valigetta "
        "e mercante. È una campagna lineare single player, non un open world."),
    GameDossier("final_fantasy_7", "Final Fantasy VII", ("ff7", "final fantasy 7"),
        "JRPG di Square del 1997. Cloud Strife e il gruppo Avalanche combattono la corporazione Shinra "
        "e Sephiroth in un mondo fantasy-fantascientifico. Combattimenti a turni con barra ATB, Materia, "
        "party di personaggi e grafica 3D prerenderizzata; è single player."),
    GameDossier("pokemon_red_blue", "Pokémon Rosso e Blu", ("pokemon red and blue", "pokemon rosso", "pokemon blu"),
        "JRPG Game Boy di Game Freak pubblicato in Occidente nel 1998. Il giovane allenatore esplora "
        "Kanto, cattura e allena Pokémon, sfida otto palestre e il Team Rocket. Combattimenti a turni, "
        "visuale dall'alto e scambi o lotte tramite cavo tra due giocatori."),
    GameDossier("stardew_valley", "Stardew Valley", ("stardew",),
        "Simulazione agricola e RPG di ConcernedApe del 2016. Il personaggio eredita una fattoria a "
        "Pelican Town: coltiva, pesca, alleva animali, esplora miniere e stringe relazioni. Grafica "
        "pixel art con visuale dall'alto; giocabile in single player e cooperativa."),
    GameDossier("hades", "Hades", ("hades game",),
        "Action roguelike isometrico di Supergiant Games del 2020. Zagreus tenta ripetutamente di "
        "fuggire dagli Inferi e riceve doni dagli dèi dell'Olimpo. Combattimento rapido, stanze "
        "procedurali, morte e nuovi tentativi con progressione narrativa; è single player."),
    GameDossier("outer_wilds", "Outer Wilds", ("the outer wilds",),
        "Avventura esplorativa in prima persona di Mobius Digital del 2019. Un astronauta Hearthian "
        "esplora un piccolo sistema solare intrappolato in un ciclo temporale di 22 minuti. Astronave, "
        "fisica orbitale e archeologia Nomai; niente combattimento tradizionale ed è single player."),
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
