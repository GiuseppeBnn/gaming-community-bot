"""
External CSV catalogs for Trophies, XP Ranks and shop Cosmetics.

The community owner can customise names / thresholds / rarities / prices without
touching code, by dropping CSV files into ``settings.catalog_dir`` (the mounted
``data/`` volume). Files are read **once at startup** (per the product decision);
edit + restart to apply.

Robustness: every loader validates rows, **skips and logs** malformed ones, and
falls back to the built-in Python defaults when a file is missing or yields no
valid row — so the bot always starts, even cold or in tests.

Expected files (headers shown):
  trophies.csv      slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value
  ranks.csv         slug,name,emoji,min_xp
  shop_cosmetics.csv key,name,tag_text,emoji,price

See ``catalogs/*.example.csv`` for copy-paste templates.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from config_data.config import settings

log = logging.getLogger(__name__)

RARITIES = ("bronze", "silver", "gold", "platinum")
# Trophy condition types understood by badge_service.check_and_award_milestones.
TROPHY_CONDITIONS = ("onboarding", "balance", "daily_streak", "bets_won", "transfers_made", "xp")


@dataclass(frozen=True)
class Rank:
    slug: str
    name: str
    emoji: str
    min_xp: int


@dataclass(frozen=True)
class CosmeticItem:
    key: str
    name: str
    tag_text: str
    emoji: str
    price: int


# ---------------------------------------------------------------------------
# Built-in defaults (used when the CSV is absent/invalid)
# ---------------------------------------------------------------------------

DEFAULT_TROPHIES: list[dict] = [
    {"slug": "first_steps", "name": "Primi Passi", "description": "Entra a far parte della community.",
     "icon_emoji": "🚀", "category": "onboarding", "rarity": "bronze", "xp_reward": 50,
     "condition_type": "onboarding", "condition_value": 1},
    {"slug": "generous", "name": "Generoso", "description": "Trasferisci Aldueuri a un altro membro.",
     "icon_emoji": "🎁", "category": "economia", "rarity": "bronze", "xp_reward": 50,
     "condition_type": "transfers_made", "condition_value": 1},
    {"slug": "rich_1k", "name": "Benestante", "description": "Raggiungi 1.000 🪙 nel saldo.",
     "icon_emoji": "💰", "category": "economia", "rarity": "silver", "xp_reward": 100,
     "condition_type": "balance", "condition_value": 1000},
    {"slug": "streak_7", "name": "Costanza", "description": "Riscuoti il daily reward per 7 giorni di fila.",
     "icon_emoji": "📅", "category": "daily", "rarity": "silver", "xp_reward": 75,
     "condition_type": "daily_streak", "condition_value": 7},
    {"slug": "bet_winner", "name": "Scommettitore", "description": "Vinci la tua prima scommessa.",
     "icon_emoji": "🎰", "category": "scommesse", "rarity": "silver", "xp_reward": 100,
     "condition_type": "bets_won", "condition_value": 1},
    {"slug": "xp_500", "name": "Esperto", "description": "Raggiungi 500 XP di esperienza.",
     "icon_emoji": "⭐", "category": "esperienza", "rarity": "gold", "xp_reward": 0,
     "condition_type": "xp", "condition_value": 500},
    {"slug": "rich_10k", "name": "Milionario", "description": "Raggiungi 10.000 🪙 nel saldo.",
     "icon_emoji": "🤑", "category": "economia", "rarity": "gold", "xp_reward": 250,
     "condition_type": "balance", "condition_value": 10000},
    {"slug": "streak_30", "name": "Dedicato", "description": "Riscuoti il daily reward per 30 giorni di fila.",
     "icon_emoji": "🔥", "category": "daily", "rarity": "gold", "xp_reward": 300,
     "condition_type": "daily_streak", "condition_value": 30},
    {"slug": "xp_2000", "name": "Leggenda", "description": "Raggiungi 2.000 XP: sei nell'olimpo.",
     "icon_emoji": "🏆", "category": "esperienza", "rarity": "platinum", "xp_reward": 0,
     "condition_type": "xp", "condition_value": 2000},
]

DEFAULT_RANKS: list[Rank] = [
    Rank("novizio", "Novizio", "🐣", 0),
    Rank("iniziato", "Iniziato", "🔰", 100),
    Rank("esperto", "Esperto", "⭐", 500),
    Rank("veterano", "Veterano", "🎖️", 1500),
    Rank("maestro", "Maestro", "🧠", 3000),
    Rank("leggenda", "Leggenda", "👑", 6000),
]

DEFAULT_COSMETICS: dict[str, CosmeticItem] = {
    "tag_pro": CosmeticItem("tag_pro", "Tag «PRO»", "PRO", "🎮", 3000),
    "tag_vip": CosmeticItem("tag_vip", "Tag «VIP»", "VIP", "💎", 5000),
    "tag_memelord": CosmeticItem("tag_memelord", "Tag «Memelord»", "MEMELORD", "😎", 2000),
    "tag_leggenda": CosmeticItem("tag_leggenda", "Tag «Leggenda»", "LEGGENDA", "👑", 10000),
}


# ---------------------------------------------------------------------------
# CSV parsing helpers
# ---------------------------------------------------------------------------

def _read_rows(catalog_dir: str | None, filename: str) -> list[dict] | None:
    """Return parsed CSV rows, or None when the file is absent/unreadable."""
    base = catalog_dir if catalog_dir is not None else settings.catalog_dir
    path = Path(base) / filename
    if not path.is_file():
        return None
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except Exception as exc:  # pragma: no cover - unexpected IO error
        log.warning("Catalogo %s illeggibile (%s); uso i default.", path, exc)
        return None


def _clean(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public loaders (pure; safe to call without side effects)
# ---------------------------------------------------------------------------

def load_trophies(catalog_dir: str | None = None) -> list[dict]:
    rows = _read_rows(catalog_dir, "trophies.csv")
    if rows is None:
        return [dict(t) for t in DEFAULT_TROPHIES]

    out: list[dict] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):
        slug, name = _clean(row, "slug"), _clean(row, "name")
        if not slug or not name or slug in seen:
            log.warning("trophies.csv riga %d ignorata (slug/name mancante o duplicato).", i)
            continue
        rarity = _clean(row, "rarity").lower() or "bronze"
        if rarity not in RARITIES:
            rarity = "bronze"
        xp_reward = _as_int(_clean(row, "xp_reward")) or 0
        ctype = _clean(row, "condition_type") or None
        if ctype is not None and ctype not in TROPHY_CONDITIONS:
            log.warning("trophies.csv riga %d: condition_type '%s' sconosciuto, ignorato.", i, ctype)
            ctype = None
        cval = _as_int(_clean(row, "condition_value"))
        out.append({
            "slug": slug, "name": name,
            "description": _clean(row, "description"),
            "icon_emoji": _clean(row, "icon_emoji") or "🏅",
            "category": _clean(row, "category") or "generale",
            "rarity": rarity,
            "xp_reward": max(0, xp_reward),
            "condition_type": ctype,
            "condition_value": cval if (ctype is not None and cval is not None and cval >= 0) else None,
        })
        seen.add(slug)

    if not out:
        log.warning("trophies.csv senza righe valide; uso i default.")
        return [dict(t) for t in DEFAULT_TROPHIES]
    return out


def load_ranks(catalog_dir: str | None = None) -> list[Rank]:
    rows = _read_rows(catalog_dir, "ranks.csv")
    if rows is None:
        return list(DEFAULT_RANKS)

    out: list[Rank] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):
        slug, name = _clean(row, "slug"), _clean(row, "name")
        min_xp = _as_int(_clean(row, "min_xp"))
        if not slug or not name or slug in seen or min_xp is None or min_xp < 0:
            log.warning("ranks.csv riga %d ignorata (campi mancanti o min_xp non valido).", i)
            continue
        out.append(Rank(slug=slug, name=name, emoji=_clean(row, "emoji") or "🎖️", min_xp=min_xp))
        seen.add(slug)

    if not out:
        log.warning("ranks.csv senza righe valide; uso i default.")
        return list(DEFAULT_RANKS)
    out.sort(key=lambda r: r.min_xp)
    return out


def load_cosmetics(catalog_dir: str | None = None) -> dict[str, CosmeticItem]:
    rows = _read_rows(catalog_dir, "shop_cosmetics.csv")
    if rows is None:
        return dict(DEFAULT_COSMETICS)

    out: dict[str, CosmeticItem] = {}
    for i, row in enumerate(rows, start=2):
        key, name, tag_text = _clean(row, "key"), _clean(row, "name"), _clean(row, "tag_text")
        price = _as_int(_clean(row, "price"))
        if not key or not name or not tag_text or key in out or price is None or price < 0:
            log.warning("shop_cosmetics.csv riga %d ignorata (campi mancanti o prezzo non valido).", i)
            continue
        out[key] = CosmeticItem(
            key=key, name=name, tag_text=tag_text[:64],
            emoji=_clean(row, "emoji") or "🏷️", price=price,
        )

    if not out:
        log.warning("shop_cosmetics.csv senza righe valide; uso i default.")
        return dict(DEFAULT_COSMETICS)
    return out


# ---------------------------------------------------------------------------
# Runtime registries (loaded once at startup; default to built-ins on import)
# ---------------------------------------------------------------------------

_ranks: list[Rank] = list(DEFAULT_RANKS)
_cosmetics: dict[str, CosmeticItem] = dict(DEFAULT_COSMETICS)


def init_registries(catalog_dir: str | None = None) -> dict[str, int]:
    """Load ranks & cosmetics into the in-memory registries. Returns counts (for logging)."""
    global _ranks, _cosmetics
    _ranks = load_ranks(catalog_dir)
    _cosmetics = load_cosmetics(catalog_dir)
    return {"ranks": len(_ranks), "cosmetics": len(_cosmetics)}


def get_ranks() -> list[Rank]:
    return _ranks


def get_cosmetics() -> dict[str, CosmeticItem]:
    return _cosmetics
