"""Unit tests for services/catalog_loader.py (CSV parsing + fallbacks)."""

from __future__ import annotations

from services import catalog_loader


# ---------------------------------------------------------------------------
# Fallback to built-in defaults when files are absent
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_trophies_default_when_missing(self, tmp_path):
        rows = catalog_loader.load_trophies(str(tmp_path))
        assert rows == catalog_loader.DEFAULT_TROPHIES
        assert any(t["condition_type"] == "xp" for t in rows)

    def test_ranks_default_when_missing(self, tmp_path):
        assert catalog_loader.load_ranks(str(tmp_path)) == catalog_loader.DEFAULT_RANKS

    def test_cosmetics_default_when_missing(self, tmp_path):
        assert catalog_loader.load_cosmetics(str(tmp_path)) == catalog_loader.DEFAULT_COSMETICS


# ---------------------------------------------------------------------------
# Trophies CSV
# ---------------------------------------------------------------------------

class TestTrophiesCsv:
    def _write(self, tmp_path, text):
        (tmp_path / "trophies.csv").write_text(text, encoding="utf-8")

    def test_valid_rows_parsed(self, tmp_path):
        self._write(
            tmp_path,
            "slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value\n"
            "win,Vincitore,desc,🏆,gen,gold,0,xp,500\n",
        )
        rows = catalog_loader.load_trophies(str(tmp_path))
        assert len(rows) == 1
        assert rows[0]["slug"] == "win"
        assert rows[0]["rarity"] == "gold"
        assert rows[0]["condition_type"] == "xp"
        assert rows[0]["condition_value"] == 500

    def test_bad_rarity_falls_back_to_bronze(self, tmp_path):
        self._write(
            tmp_path,
            "slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value\n"
            "x,X,d,🏅,gen,ultraplatinum,0,,\n",
        )
        rows = catalog_loader.load_trophies(str(tmp_path))
        assert rows[0]["rarity"] == "bronze"

    def test_malformed_rows_skipped_then_defaults(self, tmp_path):
        # Header only + a row missing slug → no valid rows → defaults.
        self._write(
            tmp_path,
            "slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value\n"
            ",Senza slug,d,🏅,gen,bronze,0,,\n",
        )
        rows = catalog_loader.load_trophies(str(tmp_path))
        assert rows == catalog_loader.DEFAULT_TROPHIES

    def test_unknown_condition_type_dropped(self, tmp_path):
        self._write(
            tmp_path,
            "slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value\n"
            "x,X,d,🏅,gen,bronze,0,banana,5\n",
        )
        rows = catalog_loader.load_trophies(str(tmp_path))
        assert rows[0]["condition_type"] is None
        assert rows[0]["condition_value"] is None

    def test_condition_param_parsed(self, tmp_path):
        self._write(
            tmp_path,
            "slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value,condition_param\n"
            "buy,Compra,d,🍕,locanda,bronze,0,item_purchases,1,cons_pizza_pacman\n",
        )
        rows = catalog_loader.load_trophies(str(tmp_path))
        assert rows[0]["condition_type"] == "item_purchases"
        assert rows[0]["condition_value"] == 1
        assert rows[0]["condition_param"] == "cons_pizza_pacman"

    def test_collection_keeps_param_drops_value(self, tmp_path):
        self._write(
            tmp_path,
            "slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value,condition_param\n"
            "coll,Coll,d,💠,locanda,gold,0,collection,,a;b;c\n",
        )
        rows = catalog_loader.load_trophies(str(tmp_path))
        assert rows[0]["condition_type"] == "collection"
        assert rows[0]["condition_value"] is None
        assert rows[0]["condition_param"] == "a;b;c"

    def test_param_required_condition_without_param_dropped(self, tmp_path):
        # item_purchases requires a condition_param → without it the condition is dropped.
        self._write(
            tmp_path,
            "slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value,condition_param\n"
            "x,X,d,🍕,locanda,bronze,0,item_purchases,1,\n",
        )
        rows = catalog_loader.load_trophies(str(tmp_path))
        assert rows[0]["condition_type"] is None
        assert rows[0]["condition_param"] is None

    def test_missing_param_column_back_compat(self, tmp_path):
        # A legacy CSV without the condition_param column still parses (param=None).
        self._write(
            tmp_path,
            "slug,name,description,icon_emoji,category,rarity,xp_reward,condition_type,condition_value\n"
            "win,Vincitore,d,🏆,gen,gold,0,xp,500\n",
        )
        rows = catalog_loader.load_trophies(str(tmp_path))
        assert rows[0]["condition_param"] is None
        assert rows[0]["condition_value"] == 500


# ---------------------------------------------------------------------------
# Consumables + categories CSV
# ---------------------------------------------------------------------------

class TestConsumablesCsv:
    def test_default_when_missing(self, tmp_path):
        assert catalog_loader.load_consumables(str(tmp_path)) == catalog_loader.DEFAULT_CONSUMABLES
        cats = catalog_loader.load_consumable_categories(str(tmp_path))
        assert cats == catalog_loader.DEFAULT_CONSUMABLE_CATEGORIES

    def test_valid_consumable(self, tmp_path):
        (tmp_path / "consumables.csv").write_text(
            "key,name,emoji,category,price,description\n"
            "cons_x,Snack X,🍿,snack,99,Buono\n",
            encoding="utf-8",
        )
        items = catalog_loader.load_consumables(str(tmp_path))
        assert "cons_x" in items
        assert items["cons_x"].price == 99
        assert items["cons_x"].category == "snack"

    def test_missing_category_or_price_skipped_then_defaults(self, tmp_path):
        (tmp_path / "consumables.csv").write_text(
            "key,name,emoji,category,price,description\n"
            "cons_a,A,🍿,,10,x\n"        # missing category
            "cons_b,B,🍿,snack,-5,x\n",  # negative price
            encoding="utf-8",
        )
        assert catalog_loader.load_consumables(str(tmp_path)) == catalog_loader.DEFAULT_CONSUMABLES

    def test_categories_valid(self, tmp_path):
        (tmp_path / "consumable_categories.csv").write_text(
            "key,name,emoji,order\nbev,Bevande,🧪,2\nfood,Cibo,🍖,1\n",
            encoding="utf-8",
        )
        cats = catalog_loader.load_consumable_categories(str(tmp_path))
        assert set(cats) == {"bev", "food"}
        assert cats["bev"].order == 2


# ---------------------------------------------------------------------------
# Ranks CSV
# ---------------------------------------------------------------------------

class TestRanksCsv:
    def test_sorted_by_min_level(self, tmp_path):
        (tmp_path / "ranks.csv").write_text(
            "slug,name,emoji,min_level\nb,B,⭐,6\na,A,🐣,1\n", encoding="utf-8"
        )
        ranks = catalog_loader.load_ranks(str(tmp_path))
        assert [r.min_level for r in ranks] == [1, 6]

    def test_invalid_min_level_skipped(self, tmp_path):
        # Non-numeric and < 1 levels are rejected; a stale min_xp file is ignored.
        (tmp_path / "ranks.csv").write_text(
            "slug,name,emoji,min_level\nok,Ok,🐣,1\nbad,Bad,⭐,abc\nzero,Zero,💤,0\n",
            encoding="utf-8",
        )
        ranks = catalog_loader.load_ranks(str(tmp_path))
        assert [r.slug for r in ranks] == ["ok"]


# ---------------------------------------------------------------------------
# Cosmetics CSV
# ---------------------------------------------------------------------------

class TestCosmeticsCsv:
    def test_valid_cosmetic(self, tmp_path):
        (tmp_path / "shop_cosmetics.csv").write_text(
            "key,name,tag_text,emoji,price\nvip,Tag VIP,VIP,💎,5000\n", encoding="utf-8"
        )
        cos = catalog_loader.load_cosmetics(str(tmp_path))
        assert "vip" in cos
        assert cos["vip"].price == 5000
        assert cos["vip"].tag_text == "VIP"

    def test_negative_price_skipped_then_defaults(self, tmp_path):
        (tmp_path / "shop_cosmetics.csv").write_text(
            "key,name,tag_text,emoji,price\nbad,Bad,B,💎,-1\n", encoding="utf-8"
        )
        assert catalog_loader.load_cosmetics(str(tmp_path)) == catalog_loader.DEFAULT_COSMETICS


# ---------------------------------------------------------------------------
# Registry init
# ---------------------------------------------------------------------------

class TestRegistries:
    def test_init_returns_counts(self, tmp_path):
        counts = catalog_loader.init_registries(str(tmp_path))
        assert counts["ranks"] == len(catalog_loader.DEFAULT_RANKS)
        assert counts["cosmetics"] == len(catalog_loader.DEFAULT_COSMETICS)
        assert counts["consumables"] == len(catalog_loader.DEFAULT_CONSUMABLES)
        assert counts["consumable_categories"] == len(catalog_loader.DEFAULT_CONSUMABLE_CATEGORIES)
        assert catalog_loader.get_consumables() == catalog_loader.DEFAULT_CONSUMABLES
        # restore defaults for other tests
        catalog_loader.init_registries(str(tmp_path))
