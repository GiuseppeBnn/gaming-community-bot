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


# ---------------------------------------------------------------------------
# Ranks CSV
# ---------------------------------------------------------------------------

class TestRanksCsv:
    def test_sorted_by_min_xp(self, tmp_path):
        (tmp_path / "ranks.csv").write_text(
            "slug,name,emoji,min_xp\nb,B,⭐,500\na,A,🐣,0\n", encoding="utf-8"
        )
        ranks = catalog_loader.load_ranks(str(tmp_path))
        assert [r.min_xp for r in ranks] == [0, 500]

    def test_invalid_min_xp_skipped(self, tmp_path):
        (tmp_path / "ranks.csv").write_text(
            "slug,name,emoji,min_xp\nok,Ok,🐣,0\nbad,Bad,⭐,abc\n", encoding="utf-8"
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
        # restore defaults for other tests
        catalog_loader.init_registries(str(tmp_path))
