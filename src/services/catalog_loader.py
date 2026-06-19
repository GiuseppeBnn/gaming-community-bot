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
  trophies.csv      slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value,condition_param
  ranks.csv         slug,name,emoji,min_level
  shop_cosmetics.csv key,name,tag_text,emoji,price
  consumables.csv    key,name,emoji,category,price,description
  consumable_categories.csv key,name,emoji,order

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
# The first 6 read counters on User/Wallet; the rest are scoped by condition_param
# (item key / category key / game key / ``;``-separated trophy slugs).
TROPHY_CONDITIONS = (
    "onboarding", "balance", "daily_streak", "bets_won", "transfers_made", "xp",
    "item_purchases", "category_purchases", "shop_purchases",
    "podium_count", "first_place_count", "collection",
)
# Condition types whose ``condition_param`` is mandatory (a scope is required).
CONDITIONS_REQUIRING_PARAM = ("item_purchases", "category_purchases", "collection")


@dataclass(frozen=True)
class Rank:
    slug: str
    name: str
    emoji: str
    min_level: int   # the level (not XP) at which this named tier starts


@dataclass(frozen=True)
class CosmeticItem:
    key: str
    name: str
    tag_text: str
    emoji: str
    price: int


@dataclass(frozen=True)
class ConsumableItem:
    """A repeatable-purchase shop item (food/drink). Buying it spends CoInn, logs a
    ShopPurchase row and adds to the buyer's pantry — no Telegram permission, no
    gameplay effect. Counts feed the "Locanda menu" trophies."""

    key: str
    name: str
    emoji: str
    category: str
    price: int
    description: str = ""


@dataclass(frozen=True)
class ConsumableCategory:
    key: str
    name: str
    emoji: str
    order: int = 0


# ---------------------------------------------------------------------------
# Built-in defaults (used when the CSV is absent/invalid)
# ---------------------------------------------------------------------------

# Slugs of the 12 per-item "menu" bronze trophies — referenced by the Critico
# Gastronomico collection (kept as a constant so the two never drift).
_MENU_BRONZE_SLUGS = (
    "t_alette", "t_coda", "t_occhi", "t_caloriemate", "t_pizza", "t_fungo",
    "t_latte", "t_elisir", "t_nuka", "t_estus", "t_torta", "t_gelato",
)
_MENU_SILVER_SLUGS = ("menu_specialita", "menu_snack", "menu_bevande", "menu_dessert")

DEFAULT_TROPHIES: list[dict] = [
    # --- Core progression (existing slugs — kept stable) ---
    {"slug": "first_steps", "name": "Primi Passi", "description": "Entra a far parte della community.",
     "icon_emoji": "🚀", "category": "onboarding", "rarity": "bronze", "xp_reward": 50,
     "condition_type": "onboarding", "condition_value": 1},
    {"slug": "generous", "name": "Generoso", "description": "Trasferisci CoInn a un altro membro.",
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

    # --- Extra milestones on existing counters (from note-implementative) ---
    {"slug": "xp_100", "name": "Senzaluce", "description": "Raggiungi 100 XP di esperienza.",
     "icon_emoji": "🕯️", "category": "esperienza", "rarity": "bronze", "xp_reward": 0,
     "condition_type": "xp", "condition_value": 100},
    {"slug": "xp_1000", "name": "Dovahkiin", "description": "Raggiungi 1.000 XP di esperienza.",
     "icon_emoji": "🐲", "category": "esperienza", "rarity": "silver", "xp_reward": 0,
     "condition_type": "xp", "condition_value": 1000},
    {"slug": "streak_60", "name": "Determination", "description": "Riscuoti il daily reward per 60 giorni di fila.",
     "icon_emoji": "❤️", "category": "daily", "rarity": "silver", "xp_reward": 200,
     "condition_type": "daily_streak", "condition_value": 60},
    {"slug": "streak_365", "name": "Hardcore Gamer", "description": "Riscuoti il daily reward per 365 giorni di fila.",
     "icon_emoji": "🗓️", "category": "daily", "rarity": "gold", "xp_reward": 500,
     "condition_type": "daily_streak", "condition_value": 365},
    {"slug": "rich_100k", "name": "Oro colato, baby!", "description": "Accumula 100.000 🪙 CoInn nel saldo.",
     "icon_emoji": "🏦", "category": "economia", "rarity": "gold", "xp_reward": 500,
     "condition_type": "balance", "condition_value": 100000},
    {"slug": "bets_10", "name": "Fortuna Sfacciata", "description": "Vinci 10 scommesse totali.",
     "icon_emoji": "🍀", "category": "scommesse", "rarity": "bronze", "xp_reward": 75,
     "condition_type": "bets_won", "condition_value": 10},
    {"slug": "bets_50", "name": "Scommettitore nato", "description": "Vinci 50 scommesse totali.",
     "icon_emoji": "🎲", "category": "scommesse", "rarity": "silver", "xp_reward": 150,
     "condition_type": "bets_won", "condition_value": 50},
    {"slug": "bets_100", "name": "Gran Puntatore", "description": "Vinci 100 scommesse totali.",
     "icon_emoji": "💸", "category": "scommesse", "rarity": "gold", "xp_reward": 300,
     "condition_type": "bets_won", "condition_value": 100},

    # --- Trivia (quiz) podium trophies ---
    {"slug": "podium_trivia_1", "name": "So di non sapere", "description": "Raggiungi il podio per la prima volta nel Trivia Nerd.",
     "icon_emoji": "🧠", "category": "trivia", "rarity": "bronze", "xp_reward": 50,
     "condition_type": "podium_count", "condition_value": 1, "condition_param": "trivia"},
    {"slug": "podium_trivia_10", "name": "Un travestimento sapiente", "description": "Raggiungi il podio 10 volte nel Trivia Nerd.",
     "icon_emoji": "🎭", "category": "trivia", "rarity": "bronze", "xp_reward": 100,
     "condition_type": "podium_count", "condition_value": 10, "condition_param": "trivia"},
    {"slug": "podium_trivia_50", "name": "Sapere è Potere", "description": "Raggiungi il podio 50 volte nel Trivia Nerd.",
     "icon_emoji": "📚", "category": "trivia", "rarity": "silver", "xp_reward": 200,
     "condition_type": "podium_count", "condition_value": 50, "condition_param": "trivia"},
    {"slug": "podium_trivia_100", "name": "So-tutto-io", "description": "Raggiungi il podio 100 volte nel Trivia Nerd.",
     "icon_emoji": "🦉", "category": "trivia", "rarity": "gold", "xp_reward": 400,
     "condition_type": "podium_count", "condition_value": 100, "condition_param": "trivia"},
    {"slug": "first_trivia_10", "name": "Campione del Trivia", "description": "Arriva 1° per 10 volte nel Trivia Nerd.",
     "icon_emoji": "🥇", "category": "trivia", "rarity": "silver", "xp_reward": 250,
     "condition_type": "first_place_count", "condition_value": 10, "condition_param": "trivia"},

    # --- 🍖 Menu della Locanda: per-item bronze (buy at least once) ---
    {"slug": "t_alette", "name": "Addestratore di Draghi", "description": "Acquista le Alette di Drago croccanti.",
     "icon_emoji": "🐉", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_alette_drago"},
    {"slug": "t_coda", "name": "Taglio Pregiato", "description": "Acquista la Coda di Drago alla brace.",
     "icon_emoji": "🔥", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_coda_drago"},
    {"slug": "t_occhi", "name": "Vedo la gente viva", "description": "Acquista gli Occhi di Drago al sugo.",
     "icon_emoji": "👁️", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_occhi_drago"},
    {"slug": "t_caloriemate", "name": "Mmm, squisito!", "description": "Acquista il CalorieMate.",
     "icon_emoji": "🍫", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_caloriemate"},
    {"slug": "t_pizza", "name": "Febbre da Pixel", "description": "Acquista la Pizza Pac-Man.",
     "icon_emoji": "🍕", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_pizza_pacman"},
    {"slug": "t_fungo", "name": "Crescita Miracolosa", "description": "Acquista il Super Fungo.",
     "icon_emoji": "🍄", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_super_fungo"},
    {"slug": "t_latte", "name": "Fulminato", "description": "Acquista il Latte di Mandorla.",
     "icon_emoji": "🥛", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_latte_mandorla"},
    {"slug": "t_elisir", "name": "Nuova Vita", "description": "Acquista l'Elisir di Salute.",
     "icon_emoji": "🧉", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_elisir_salute"},
    {"slug": "t_nuka", "name": "Tappi in tasca", "description": "Acquista la Nuka-Cola.",
     "icon_emoji": "🥤", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_nuka_cola"},
    {"slug": "t_estus", "name": "Non morto", "description": "Acquista la Fiaschetta Estus.",
     "icon_emoji": "🍶", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_fiaschetta_estus"},
    {"slug": "t_torta", "name": "The Cake is a Lie?", "description": "Acquista la Torta di Minecraft.",
     "icon_emoji": "🎂", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_torta_minecraft"},
    {"slug": "t_gelato", "name": "Custode del Keyblade", "description": "Acquista il Gelato al Sale Marino.",
     "icon_emoji": "🍦", "category": "locanda", "rarity": "bronze", "xp_reward": 25,
     "condition_type": "item_purchases", "condition_value": 1, "condition_param": "cons_gelato_sale_marino"},

    # --- 🍖 Menu della Locanda: per-category silver aggregates ---
    {"slug": "menu_specialita", "name": "Gran Banchetto", "description": "Acquista 10 piatti delle Specialità della Locanda.",
     "icon_emoji": "🍖", "category": "locanda", "rarity": "silver", "xp_reward": 150,
     "condition_type": "category_purchases", "condition_value": 10, "condition_param": "specialita"},
    {"slug": "menu_snack", "name": "Scorta di Sopravvivenza", "description": "Acquista 15 Consumabili Leggendari.",
     "icon_emoji": "📦", "category": "locanda", "rarity": "silver", "xp_reward": 150,
     "condition_type": "category_purchases", "condition_value": 15, "condition_param": "snack"},
    {"slug": "menu_bevande", "name": "Astemio", "description": "Acquista 25 Bevande Leggendarie.",
     "icon_emoji": "🧪", "category": "locanda", "rarity": "silver", "xp_reward": 150,
     "condition_type": "category_purchases", "condition_value": 25, "condition_param": "bevande"},
    {"slug": "menu_dessert", "name": "Ghiottone", "description": "Acquista 10 Dessert.",
     "icon_emoji": "🍰", "category": "locanda", "rarity": "silver", "xp_reward": 150,
     "condition_type": "category_purchases", "condition_value": 10, "condition_param": "dessert"},

    # --- 🍖 Menu della Locanda: collection trophies ---
    {"slug": "menu_critico", "name": "Critico Gastronomico", "description": "Sblocca tutti i trofei di bronzo del menù.",
     "icon_emoji": "🍽️", "category": "locanda", "rarity": "gold", "xp_reward": 400,
     "condition_type": "collection", "condition_param": ";".join(_MENU_BRONZE_SLUGS)},
    {"slug": "menu_leggenda_inn", "name": "Leggenda di Dragons'Inn", "description": "Sblocca tutti i trofei della Taverna del Drago.",
     "icon_emoji": "💠", "category": "locanda", "rarity": "platinum", "xp_reward": 0,
     "condition_type": "collection",
     "condition_param": ";".join(("menu_critico", *_MENU_SILVER_SLUGS))},
]

# Named tiers spread evenly across levels (every ~5 levels). Editable via
# data/ranks.csv (column ``min_level``); the level→XP curve is the geometric
# progression in xp_service (settings ``xp_level_base`` / ``xp_level_growth``).
DEFAULT_RANKS: list[Rank] = [
    Rank("novizio", "Novizio", "🐣", 1),
    Rank("iniziato", "Iniziato", "🔰", 6),
    Rank("esperto", "Esperto", "⭐", 11),
    Rank("veterano", "Veterano", "🎖️", 16),
    Rank("maestro", "Maestro", "🧠", 21),
    Rank("leggenda", "Leggenda", "👑", 26),
]

DEFAULT_COSMETICS: dict[str, CosmeticItem] = {
    "tag_pro": CosmeticItem("tag_pro", "Tag «PRO»", "PRO", "🎮", 3000),
    "tag_vip": CosmeticItem("tag_vip", "Tag «VIP»", "VIP", "💎", 5000),
    "tag_memelord": CosmeticItem("tag_memelord", "Tag «Memelord»", "MEMELORD", "😎", 2000),
    "tag_leggenda": CosmeticItem("tag_leggenda", "Tag «Leggenda»", "LEGGENDA", "👑", 10000),
}

# Consumable shop catalog — keys are ``cons_*`` so they never collide with the
# ``tag_*`` cosmetic keys in the shared ``shop_purchases.item_key`` namespace.
DEFAULT_CONSUMABLE_CATEGORIES: dict[str, ConsumableCategory] = {
    "specialita": ConsumableCategory("specialita", "Specialità della Locanda", "🍖", 0),
    "snack": ConsumableCategory("snack", "Consumabili Leggendari", "📦", 1),
    "bevande": ConsumableCategory("bevande", "Bevande Leggendarie", "🧪", 2),
    "dessert": ConsumableCategory("dessert", "Dessert", "🍰", 3),
}

DEFAULT_CONSUMABLES: dict[str, ConsumableItem] = {
    # 🍖 Specialità della Locanda
    "cons_alette_drago": ConsumableItem(
        "cons_alette_drago", "Alette di Drago croccanti", "🐉", "specialita", 500,
        "Croccanti fuori, fumanti dentro."),
    "cons_coda_drago": ConsumableItem(
        "cons_coda_drago", "Coda di Drago alla brace", "🔥", "specialita", 600,
        "Un taglio pregiato per palati coraggiosi."),
    "cons_occhi_drago": ConsumableItem(
        "cons_occhi_drago", "Occhi di Drago al sugo", "👁️", "specialita", 450,
        "Ti guardano mentre li mangi."),
    # 📦 Consumabili Leggendari (snack)
    "cons_caloriemate": ConsumableItem(
        "cons_caloriemate", "CalorieMate", "🍫", "snack", 200,
        "Razione energetica da vero agente."),
    "cons_pizza_pacman": ConsumableItem(
        "cons_pizza_pacman", "Pizza Pac-Man", "🍕", "snack", 300,
        "Waka waka waka."),
    "cons_super_fungo": ConsumableItem(
        "cons_super_fungo", "Super Fungo", "🍄", "snack", 250,
        "Crescita miracolosa garantita."),
    # 🧪 Bevande Leggendarie
    "cons_latte_mandorla": ConsumableItem(
        "cons_latte_mandorla", "Latte di Mandorla", "🥛", "bevande", 150,
        "Fulmineo al palato."),
    "cons_elisir_salute": ConsumableItem(
        "cons_elisir_salute", "Elisir di Salute", "🧉", "bevande", 350,
        "Una nuova vita in una fiala."),
    "cons_nuka_cola": ConsumableItem(
        "cons_nuka_cola", "Nuka-Cola", "🥤", "bevande", 250,
        "Tappi in tasca, post-apocalisse nel cuore."),
    "cons_fiaschetta_estus": ConsumableItem(
        "cons_fiaschetta_estus", "Fiaschetta Estus", "🍶", "bevande", 400,
        "Sorseggia accanto al falò."),
    # 🍰 Dessert
    "cons_torta_minecraft": ConsumableItem(
        "cons_torta_minecraft", "Torta di Minecraft", "🎂", "dessert", 300,
        "The cake is a lie? Forse."),
    "cons_gelato_sale_marino": ConsumableItem(
        "cons_gelato_sale_marino", "Gelato al Sale Marino", "🍦", "dessert", 280,
        "Dolce e salato, da custodi del Keyblade."),
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
        cparam = (_clean(row, "condition_param") or None)
        # Scoped conditions (item/category/collection) are meaningless without a
        # param → drop the condition so the trophy doesn't silently never unlock.
        if ctype in CONDITIONS_REQUIRING_PARAM and not cparam:
            log.warning("trophies.csv riga %d: condition_type '%s' richiede condition_param, ignorato.", i, ctype)
            ctype = None
        # ``collection`` unlocks on owning other trophies, not on a numeric threshold.
        needs_value = ctype is not None and ctype != "collection"
        out.append({
            "slug": slug, "name": name,
            "description": _clean(row, "description"),
            "icon_emoji": _clean(row, "icon_emoji") or "🏅",
            "category": _clean(row, "category") or "generale",
            "rarity": rarity,
            "xp_reward": max(0, xp_reward),
            "condition_type": ctype,
            "condition_value": cval if (needs_value and cval is not None and cval >= 0) else None,
            "condition_param": cparam if ctype is not None else None,
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
        min_level = _as_int(_clean(row, "min_level"))
        if not slug or not name or slug in seen or min_level is None or min_level < 1:
            log.warning("ranks.csv riga %d ignorata (campi mancanti o min_level non valido).", i)
            continue
        out.append(Rank(slug=slug, name=name, emoji=_clean(row, "emoji") or "🎖️", min_level=min_level))
        seen.add(slug)

    if not out:
        log.warning("ranks.csv senza righe valide; uso i default.")
        return list(DEFAULT_RANKS)
    out.sort(key=lambda r: r.min_level)
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


def load_consumable_categories(catalog_dir: str | None = None) -> dict[str, ConsumableCategory]:
    rows = _read_rows(catalog_dir, "consumable_categories.csv")
    if rows is None:
        return dict(DEFAULT_CONSUMABLE_CATEGORIES)

    out: dict[str, ConsumableCategory] = {}
    for i, row in enumerate(rows, start=2):
        key, name = _clean(row, "key"), _clean(row, "name")
        if not key or not name or key in out:
            log.warning("consumable_categories.csv riga %d ignorata (key/name mancante o duplicato).", i)
            continue
        out[key] = ConsumableCategory(
            key=key, name=name, emoji=_clean(row, "emoji") or "🍽️",
            order=_as_int(_clean(row, "order")) or 0,
        )

    if not out:
        log.warning("consumable_categories.csv senza righe valide; uso i default.")
        return dict(DEFAULT_CONSUMABLE_CATEGORIES)
    return out


def load_consumables(catalog_dir: str | None = None) -> dict[str, ConsumableItem]:
    rows = _read_rows(catalog_dir, "consumables.csv")
    if rows is None:
        return dict(DEFAULT_CONSUMABLES)

    out: dict[str, ConsumableItem] = {}
    for i, row in enumerate(rows, start=2):
        key, name, category = _clean(row, "key"), _clean(row, "name"), _clean(row, "category")
        price = _as_int(_clean(row, "price"))
        if not key or not name or not category or key in out or price is None or price < 0:
            log.warning("consumables.csv riga %d ignorata (campi mancanti o prezzo non valido).", i)
            continue
        out[key] = ConsumableItem(
            key=key, name=name[:64], emoji=_clean(row, "emoji") or "🍽️",
            category=category, price=price, description=_clean(row, "description")[:128],
        )

    if not out:
        log.warning("consumables.csv senza righe valide; uso i default.")
        return dict(DEFAULT_CONSUMABLES)
    return out


# ---------------------------------------------------------------------------
# Runtime registries (loaded once at startup; default to built-ins on import)
# ---------------------------------------------------------------------------

_ranks: list[Rank] = list(DEFAULT_RANKS)
_cosmetics: dict[str, CosmeticItem] = dict(DEFAULT_COSMETICS)
_consumables: dict[str, ConsumableItem] = dict(DEFAULT_CONSUMABLES)
_consumable_categories: dict[str, ConsumableCategory] = dict(DEFAULT_CONSUMABLE_CATEGORIES)


def init_registries(catalog_dir: str | None = None) -> dict[str, int]:
    """Load ranks, cosmetics & consumables into the in-memory registries.

    Returns counts (for logging). The trophy catalog is synced separately to the DB
    by ``badge_service.sync_trophies`` (the only catalog that is persisted)."""
    global _ranks, _cosmetics, _consumables, _consumable_categories
    _ranks = load_ranks(catalog_dir)
    _cosmetics = load_cosmetics(catalog_dir)
    _consumable_categories = load_consumable_categories(catalog_dir)
    _consumables = load_consumables(catalog_dir)
    return {
        "ranks": len(_ranks),
        "cosmetics": len(_cosmetics),
        "consumables": len(_consumables),
        "consumable_categories": len(_consumable_categories),
    }


def get_ranks() -> list[Rank]:
    return _ranks


def get_cosmetics() -> dict[str, CosmeticItem]:
    return _cosmetics


def get_consumables() -> dict[str, ConsumableItem]:
    return _consumables


def get_consumable_categories() -> dict[str, ConsumableCategory]:
    return _consumable_categories
