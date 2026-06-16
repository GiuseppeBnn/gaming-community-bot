"""Unit tests for pure (sync) Locanda helpers: consumable catalog accessors and
badge_service.describe_condition wording for the parametrized conditions."""

from __future__ import annotations

import services.badge_service as badge_svc
from services import consumable_service


class TestConsumableCatalogHelpers:
    def test_categories_in_order(self):
        cats = consumable_service.get_categories()
        orders = [c.order for c in cats]
        assert orders == sorted(orders)

    def test_items_in_category(self):
        items = consumable_service.items_in_category("specialita")
        assert items
        assert all(i.category == "specialita" for i in items)

    def test_unknown_category_empty(self):
        assert consumable_service.items_in_category("nope") == []


class TestDescribeCondition:
    def test_counter_conditions_still_work(self):
        assert badge_svc.describe_condition("xp", 500) == "Accumula 500 XP"
        assert badge_svc.describe_condition("balance", 1000) == "Raggiungi 1,000 CoInn"

    def test_no_condition_returns_none(self):
        assert badge_svc.describe_condition(None, None) is None

    def test_item_resolves_name(self):
        txt = badge_svc.describe_condition("item_purchases", 1, "cons_pizza_pacman")
        assert "Pizza Pac-Man" in txt

    def test_category_resolves_name(self):
        txt = badge_svc.describe_condition("category_purchases", 10, "bevande")
        assert "10" in txt and "Bevande" in txt

    def test_shop_purchases_wording(self):
        txt = badge_svc.describe_condition("shop_purchases", 50)
        assert "50" in txt and "Locanda" in txt

    def test_podium_resolves_game(self):
        txt = badge_svc.describe_condition("podium_count", 5, "trivia")
        assert "Trivia Nerd" in txt and "5" in txt

    def test_first_place_wording(self):
        txt = badge_svc.describe_condition("first_place_count", 1, "trivia")
        assert "1°" in txt and "Trivia Nerd" in txt

    def test_collection_counts_prereqs(self):
        txt = badge_svc.describe_condition("collection", None, "a;b;c")
        assert "3" in txt

    def test_unknown_param_does_not_crash(self):
        assert badge_svc.describe_condition("item_purchases", 1, "missing") is not None
