"""Tests for keyboard builders — verifies button text, count, and callback data."""

from __future__ import annotations

from types import SimpleNamespace

from keyboards.betting_kb import (
    PRESET_AMOUNTS,
    get_amount_keyboard,
    get_confirm_bet_keyboard,
    get_events_keyboard,
    get_group_events_keyboard,
    get_options_keyboard,
)
from keyboards.shop_kb import (
    get_consumable_confirm_kb,
    get_locanda_home_kb,
    get_menu_categories_kb,
    get_menu_items_kb,
    get_shop_catalog_kb,
    get_shop_confirm_kb,
)
from services.catalog_loader import ConsumableCategory, ConsumableItem, CosmeticItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flat_buttons(markup):
    """Flatten InlineKeyboardMarkup rows into a single list of buttons."""
    return [btn for row in markup.inline_keyboard for btn in row]


def _make_event(id: int = 1, title: str = "Test Event", options_wagered: list[int] | None = None):
    opts = [
        SimpleNamespace(
            id=i + 1,
            label=f"Option {i + 1}",
            odds_multiplier=2.0,
            total_wagered=w,
        )
        for i, w in enumerate(options_wagered or [100, 200])
    ]
    bets = []
    return SimpleNamespace(id=id, title=title, options=opts, user_bets=bets)


# ---------------------------------------------------------------------------
# get_group_events_keyboard
# ---------------------------------------------------------------------------

class TestGroupEventsKeyboard:
    def test_one_button_per_event(self):
        events = [_make_event(1), _make_event(2)]
        kb = get_group_events_keyboard(events, "mybot")
        buttons = _flat_buttons(kb)
        assert len(buttons) == 2

    def test_button_is_url(self):
        events = [_make_event(id=3)]
        kb = get_group_events_keyboard(events, "gamebot")
        btn = _flat_buttons(kb)[0]
        assert btn.url is not None
        assert "gamebot" in btn.url
        assert "3" in btn.url

    def test_url_uses_start_deep_link(self):
        events = [_make_event(id=5)]
        kb = get_group_events_keyboard(events, "mybot")
        btn = _flat_buttons(kb)[0]
        assert "start=bet_5" in btn.url

    def test_empty_events(self):
        kb = get_group_events_keyboard([], "mybot")
        assert _flat_buttons(kb) == []


# ---------------------------------------------------------------------------
# get_events_keyboard
# ---------------------------------------------------------------------------

class TestEventsKeyboard:
    def test_buttons_include_close(self):
        events = [_make_event(1), _make_event(2)]
        kb = get_events_keyboard(events)
        buttons = _flat_buttons(kb)
        # 2 events + 1 close button
        assert len(buttons) == 3

    def test_close_button_callback(self):
        kb = get_events_keyboard([_make_event()])
        close_btn = _flat_buttons(kb)[-1]
        assert close_btn.callback_data == "bet:close"

    def test_event_button_shows_total_wagered(self):
        event = _make_event(options_wagered=[100, 200])  # total = 300
        kb = get_events_keyboard([event])
        btn = _flat_buttons(kb)[0]
        assert "300" in btn.text

    def test_event_button_callback_data(self):
        event = _make_event(id=7)
        kb = get_events_keyboard([event])
        btn = _flat_buttons(kb)[0]
        assert btn.callback_data == "event:view:7"


# ---------------------------------------------------------------------------
# get_options_keyboard
# ---------------------------------------------------------------------------

class TestOptionsKeyboard:
    def test_button_per_option_plus_back_close(self):
        opts = [
            SimpleNamespace(id=1, label="Team A", odds_multiplier=2.0, total_wagered=500),
            SimpleNamespace(id=2, label="Team B", odds_multiplier=3.5, total_wagered=200),
        ]
        kb = get_options_keyboard(event_id=1, options=opts)
        buttons = _flat_buttons(kb)
        # 2 options + back + close = 4
        assert len(buttons) == 4

    def test_button_shows_label_and_wagered_without_odds(self):
        # The "x2.0" odds multiplier was removed from option buttons (trivial noise).
        opts = [SimpleNamespace(id=1, label="Win", odds_multiplier=2.5, total_wagered=300)]
        kb = get_options_keyboard(event_id=1, options=opts)
        btn = _flat_buttons(kb)[0]
        assert "Win" in btn.text
        assert "300" in btn.text
        assert "x2.5" not in btn.text and "(x" not in btn.text

    def test_callback_format(self):
        opts = [SimpleNamespace(id=3, label="X", odds_multiplier=1.0, total_wagered=0)]
        kb = get_options_keyboard(event_id=9, options=opts)
        btn = _flat_buttons(kb)[0]
        assert btn.callback_data == "bet_option:9:3"


# ---------------------------------------------------------------------------
# get_amount_keyboard
# ---------------------------------------------------------------------------

class TestAmountKeyboard:
    def test_no_presets_when_balance_zero(self):
        kb = get_amount_keyboard(event_id=1, option_id=1, user_balance=0)
        buttons = _flat_buttons(kb)
        # Only: custom + back + close = 3
        cbs = [b.callback_data for b in buttons if b.callback_data]
        assert not any(cb.startswith("bet_amount:") for cb in cbs)

    def test_only_affordable_presets_shown(self):
        kb = get_amount_keyboard(event_id=1, option_id=1, user_balance=150)
        buttons = _flat_buttons(kb)
        amount_buttons = [b for b in buttons if b.callback_data and "bet_amount:" in b.callback_data]
        amounts = [int(b.callback_data.split(":")[-1]) for b in amount_buttons]
        # PRESET_AMOUNTS = [50, 100, 250, 500] — only 50 and 100 fit
        assert 50 in amounts
        assert 100 in amounts
        assert 250 not in amounts
        assert 500 not in amounts

    def test_all_presets_when_balance_sufficient(self):
        kb = get_amount_keyboard(event_id=1, option_id=1, user_balance=1000)
        amount_buttons = [
            b for b in _flat_buttons(kb)
            if b.callback_data and "bet_amount:" in b.callback_data
        ]
        assert len(amount_buttons) == len(PRESET_AMOUNTS)

    def test_custom_button_always_present(self):
        for balance in [0, 50, 1000]:
            kb = get_amount_keyboard(event_id=1, option_id=1, user_balance=balance)
            cbs = [b.callback_data for b in _flat_buttons(kb) if b.callback_data]
            assert any("bet_custom:" in cb for cb in cbs)


# ---------------------------------------------------------------------------
# get_confirm_bet_keyboard
# ---------------------------------------------------------------------------

class TestConfirmBetKeyboard:
    def test_confirm_button_text_contains_amount(self):
        kb = get_confirm_bet_keyboard(event_id=1, option_id=2, amount=250)
        confirm_btn = _flat_buttons(kb)[0]
        assert "250" in confirm_btn.text

    def test_confirm_callback_data(self):
        kb = get_confirm_bet_keyboard(event_id=3, option_id=5, amount=100)
        confirm_btn = _flat_buttons(kb)[0]
        assert confirm_btn.callback_data == "bet_confirm:3:5:100"

    def test_three_buttons_total(self):
        kb = get_confirm_bet_keyboard(event_id=1, option_id=1, amount=50)
        assert len(_flat_buttons(kb)) == 3


# ---------------------------------------------------------------------------
# Shop keyboards
# ---------------------------------------------------------------------------

def _cosmetic(key="tag_pro", price=100):
    return CosmeticItem(key=key, name="Tag", tag_text="PRO", emoji="🎮", price=price)


class TestShopCatalogKb:
    def test_affordable_item_has_checkmark(self):
        kb = get_shop_catalog_kb([_cosmetic(price=100)], balance=500, owned=set())
        btn = _flat_buttons(kb)[0]
        assert "✅" in btn.text

    def test_unaffordable_item_has_cross(self):
        kb = get_shop_catalog_kb([_cosmetic(price=1000)], balance=50, owned=set())
        btn = _flat_buttons(kb)[0]
        assert "❌" in btn.text

    def test_owned_item_is_marked_and_not_buyable(self):
        kb = get_shop_catalog_kb([_cosmetic()], balance=9999, owned={"tag_pro"})
        btn = _flat_buttons(kb)[0]
        assert "🎁" in btn.text
        assert btn.callback_data == "shop:owned"

    def test_close_button_always_last(self):
        kb = get_shop_catalog_kb([_cosmetic()], balance=9999, owned=set())
        last_btn = _flat_buttons(kb)[-1]
        assert last_btn.callback_data == "shop:close"

    def test_button_callback_contains_item_key(self):
        kb = get_shop_catalog_kb([_cosmetic(key="tag_vip")], balance=500, owned=set())
        btn = _flat_buttons(kb)[0]
        assert btn.callback_data == "shop:buy:tag_vip"


def _consumable(key="cons_pizza_pacman", price=300, category="snack"):
    return ConsumableItem(
        key=key, name="Pizza", emoji="🍕", category=category, price=price, description="d",
    )


class TestLocandaKeyboards:
    def test_home_has_both_sections(self):
        kb = get_locanda_home_kb(has_cosmetics=True, has_menu=True)
        cbs = [b.callback_data for b in _flat_buttons(kb)]
        assert "shop:list" in cbs and "shop:menu" in cbs
        assert "shop:pantry" in cbs and "shop:close" in cbs

    def test_home_hides_empty_sections(self):
        kb = get_locanda_home_kb(has_cosmetics=False, has_menu=False)
        cbs = [b.callback_data for b in _flat_buttons(kb)]
        assert "shop:list" not in cbs and "shop:menu" not in cbs

    def test_categories_callbacks(self):
        cats = [ConsumableCategory("snack", "Snack", "📦", 0)]
        kb = get_menu_categories_kb(cats)
        cbs = [b.callback_data for b in _flat_buttons(kb)]
        assert "shop:cat:snack" in cbs
        assert all(len(c.encode()) <= 64 for c in cbs)

    def test_menu_item_affordable_and_owned_count(self):
        kb = get_menu_items_kb([_consumable()], balance=500, counts={"cons_pizza_pacman": 2})
        btn = _flat_buttons(kb)[0]
        assert btn.callback_data == "shop:cbuy:cons_pizza_pacman"
        assert "✅" in btn.text and "·2" in btn.text

    def test_menu_item_unaffordable(self):
        kb = get_menu_items_kb([_consumable(price=9999)], balance=10, counts={})
        assert "❌" in _flat_buttons(kb)[0].text

    def test_consumable_confirm_callbacks(self):
        kb = get_consumable_confirm_kb("cons_pizza_pacman", "snack")
        btns = _flat_buttons(kb)
        assert btns[0].callback_data == "shop:cexec:cons_pizza_pacman"
        assert btns[1].callback_data == "shop:cat:snack"


class TestShopConfirmKb:
    def test_execute_button_callback(self):
        kb = get_shop_confirm_kb("tag_vip")
        execute_btn = _flat_buttons(kb)[0]
        assert execute_btn.callback_data == "shop:exec:tag_vip"

    def test_back_button_goes_to_list(self):
        kb = get_shop_confirm_kb("tag_vip")
        back_btn = _flat_buttons(kb)[1]
        assert back_btn.callback_data == "shop:list"

    def test_three_buttons(self):
        kb = get_shop_confirm_kb("tag_pro")
        assert len(_flat_buttons(kb)) == 3


# ---------------------------------------------------------------------------
# Admin betting keyboards
# ---------------------------------------------------------------------------

class TestAdminBettingKeyboards:
    def test_admin_events_keyboard_icons(self):
        from keyboards.admin_betting_kb import get_admin_events_keyboard
        from database.models import EventStatus

        event_open = SimpleNamespace(
            id=1, title="Open Event", status=EventStatus.open.value,
            options=[SimpleNamespace(total_wagered=100)],
            user_bets=[SimpleNamespace(), SimpleNamespace()],
        )
        event_locked = SimpleNamespace(
            id=2, title="Locked Event", status=EventStatus.locked.value,
            options=[SimpleNamespace(total_wagered=50)],
            user_bets=[],
        )
        kb = get_admin_events_keyboard([event_open, event_locked])
        buttons = _flat_buttons(kb)
        assert "🟢" in buttons[0].text
        assert "🔒" in buttons[1].text

    def test_admin_confirm_resolve_callback(self):
        from keyboards.admin_betting_kb import get_admin_confirm_resolve_keyboard
        kb = get_admin_confirm_resolve_keyboard(event_id=5, option_id=3)
        confirm_btn = _flat_buttons(kb)[0]
        assert confirm_btn.callback_data == "admin_bet:confirm_resolve:5:3"

    def test_admin_confirm_cancel_shows_count(self):
        from keyboards.admin_betting_kb import get_admin_confirm_cancel_keyboard
        kb = get_admin_confirm_cancel_keyboard(event_id=1, pending_count=7)
        confirm_btn = _flat_buttons(kb)[0]
        assert "7" in confirm_btn.text
