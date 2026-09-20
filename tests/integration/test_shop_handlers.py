"""The Locanda's handlers — `handlers/shop.py`, where a user spends their own coins.

`test_shop_service.py` and `test_consumable_service.py` cover the service layer;
`test_shop_home_balance.py` covers one regression on the landing screen. What had no
tests is the layer in between: the confirm-then-execute pairs that actually debit a
wallet, and the tag switcher that decides what a profile shows.

The two purchase flows are deliberately different and the difference is the reason
this file exists. A **cosmetic** is bought once and owning it twice is meaningless, so
`cb_shop_execute` checks ownership, debits, then checks ownership *again* under the
wallet lock and rolls back if it lost the race. A **consumable** is repeatable by
design, so it has no such check and buying it ten times must cost ten times. Getting
those two the wrong way round is a silent way to either charge twice or hand out
something free, and neither leaves an error in the logs.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import func, select

from database.models import ShopPurchase, User, Wallet
from handlers.callbacks import ShopCb
from handlers import shop
from services import catalog_loader, consumable_service, shop_service
from utils import cooldown

async def _get_me():
    return SimpleNamespace(username="testbot")


USER_ID = 1
COSMETIC = "tag_memelord"     # 2000 🪙, the cheapest cosmetic
CONSUMABLE = "cons_occhi_drago"  # 450 🪙


class _FakeBot:
    id = 999_999

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _FakeMessage:
    """The panel the bot posted: `from_user` is the BOT, as in production."""

    def __init__(self, bot=None) -> None:
        self.bot = bot or _FakeBot()
        self.from_user = SimpleNamespace(id=_FakeBot.id)
        self.chat = SimpleNamespace(id=USER_ID, type="private")
        self.texts: list[str] = []
        self.markups: list[object] = []
        self.deleted = False

    async def answer(self, text, reply_markup=None):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def edit_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def delete(self):
        self.deleted = True


class _FakeCallback:
    def __init__(self, callback_data: ShopCb, message=None, user_id: int = USER_ID) -> None:
        self.callback_data = callback_data
        self.data = callback_data.pack()
        self.message = message or _FakeMessage()
        self.bot = self.message.bot
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    @property
    def alerts(self) -> list[str]:
        return [t for t, alert in self.answers if alert and t]

    @property
    def toasts(self) -> list[str]:
        return [t for t, alert in self.answers if not alert and t]


def _shop_callback(action: str, key: str | None = None, message=None, user_id: int = USER_ID):
    return _FakeCallback(ShopCb(action=action, key=key), message, user_id)


async def _call_keyed(handler, callback: _FakeCallback, session) -> None:
    await handler(callback, callback.callback_data, session)


async def test_keyed_callbacks_without_a_key_are_ignored_before_any_purchase_logic():
    """Removing a `key is None` guard would turn this into an unavailable-item
    alert or reach a service with `None`; either behavior admits a malformed
    callback beyond its typed boundary."""
    for handler, action in (
        (shop.cb_shop_buy, "buy"),
        (shop.cb_shop_execute, "exec"),
        (shop.cb_shop_category, "cat"),
        (shop.cb_consumable_buy, "cbuy"),
        (shop.cb_consumable_execute, "cexec"),
        (shop.cb_shop_toggle_tag, "tag"),
    ):
        callback = _shop_callback(action)

        await _call_keyed(handler, callback, None)

        assert callback.answers == [(None, False)], action


async def _coins(session, tg_id: int = USER_ID) -> int:
    return (
        await session.execute(select(Wallet.coins).where(Wallet.tg_id == tg_id))
    ).scalar_one()


async def _purchases(session, item_key: str, tg_id: int = USER_ID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(ShopPurchase).where(
                ShopPurchase.user_tg_id == tg_id, ShopPurchase.item_key == item_key
            )
        )
    ).scalar_one()


class TestCosmeticPurchase:
    async def test_buying_charges_once_and_applies_the_tag(self, session, user_factory):
        await user_factory(tg_id=USER_ID, coins=5000)
        item = shop_service.get_item(COSMETIC)
        cb = _shop_callback("exec", COSMETIC)

        await _call_keyed(shop.cb_shop_execute, cb, session)

        assert await _coins(session) == 5000 - item.price
        assert await _purchases(session, COSMETIC) == 1
        assert await shop_service.has_cosmetic(session, USER_ID, COSMETIC) is True

    async def test_buying_the_same_cosmetic_twice_is_refused_for_free(
        self, session, user_factory
    ):
        """Owning a tag twice means nothing, so the second attempt must cost nothing.
        The assertion is on the balance rather than on the alert: an alert that says
        «già posseduto» while the coins left anyway is the failure being guarded
        against."""
        await user_factory(tg_id=USER_ID, coins=5000)
        item = shop_service.get_item(COSMETIC)

        await _call_keyed(shop.cb_shop_execute, _shop_callback("exec", COSMETIC), session)
        after_first = await _coins(session)

        second = _shop_callback("exec", COSMETIC)
        await _call_keyed(shop.cb_shop_execute, second, session)

        assert await _coins(session) == after_first == 5000 - item.price
        assert await _purchases(session, COSMETIC) == 1
        assert second.alerts and "già" in second.alerts[0]

    async def test_not_enough_coins_leaves_the_wallet_alone(self, session, user_factory):
        await user_factory(tg_id=USER_ID, coins=10)
        cb = _shop_callback("exec", COSMETIC)

        await _call_keyed(shop.cb_shop_execute, cb, session)

        assert await _coins(session) == 10
        assert await _purchases(session, COSMETIC) == 0
        assert cb.alerts and "insufficiente" in cb.alerts[0]

    async def test_a_user_without_a_wallet_is_told_to_register(self, session):
        cb = _shop_callback("exec", COSMETIC)

        await _call_keyed(shop.cb_shop_execute, cb, session)

        assert cb.alerts and "/start" in cb.alerts[0]

    async def test_an_unknown_item_is_refused(self, session, user_factory):
        await user_factory(tg_id=USER_ID, coins=5000)
        cb = _shop_callback("exec", "tag_inesistente")

        await _call_keyed(shop.cb_shop_execute, cb, session)

        assert await _coins(session) == 5000
        assert cb.alerts and "non disponibile" in cb.alerts[0]

    async def test_the_confirm_screen_shows_price_and_balance(self, session, user_factory):
        """The user approves the spend from this screen, so both numbers on it have to
        be theirs — the panel's author is the bot, which owns no wallet."""
        await user_factory(tg_id=USER_ID, coins=4321)
        cb = _shop_callback("buy", COSMETIC)

        await _call_keyed(shop.cb_shop_buy, cb, session)

        text = cb.message.texts[-1]
        assert "2,000" in text or "2.000" in text or "2000" in text
        assert "4,321" in text or "4.321" in text or "4321" in text

    async def test_the_confirm_screen_is_skipped_when_it_cannot_be_afforded(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, coins=1)
        cb = _shop_callback("buy", COSMETIC)

        await _call_keyed(shop.cb_shop_buy, cb, session)

        assert cb.message.texts == []
        assert cb.alerts and "insufficiente" in cb.alerts[0]

    async def test_the_confirm_screen_is_skipped_when_already_owned(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, coins=5000)
        await _call_keyed(shop.cb_shop_execute, _shop_callback("exec", COSMETIC), session)

        cb = _shop_callback("buy", COSMETIC)
        await _call_keyed(shop.cb_shop_buy, cb, session)

        assert cb.message.texts == []
        assert cb.alerts and "già" in cb.alerts[0]

    async def test_an_unknown_item_on_the_confirm_screen_is_refused(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, coins=5000)
        cb = _shop_callback("buy", "tag_inesistente")

        await _call_keyed(shop.cb_shop_buy, cb, session)

        assert cb.alerts and "non disponibile" in cb.alerts[0]


class TestConsumablePurchase:
    async def test_a_consumable_can_be_bought_again_and_costs_again(
        self, session, user_factory
    ):
        """The mirror image of the cosmetic rule, and the reason the two flows cannot
        share an implementation: repeatability is the feature here."""
        item = consumable_service.get_item(CONSUMABLE)
        await user_factory(tg_id=USER_ID, coins=item.price * 3)

        for _ in range(3):
            await _call_keyed(
                shop.cb_consumable_execute, _shop_callback("cexec", CONSUMABLE), session
            )

        assert await _coins(session) == 0
        assert await _purchases(session, CONSUMABLE) == 3

    async def test_it_lands_in_the_pantry(self, session, user_factory):
        item = consumable_service.get_item(CONSUMABLE)
        await user_factory(tg_id=USER_ID, coins=item.price)

        await _call_keyed(
            shop.cb_consumable_execute, _shop_callback("cexec", CONSUMABLE), session
        )

        cb = _shop_callback("pantry")
        await shop.cb_shop_pantry(cb, session)
        assert item.name in cb.message.texts[-1]

    async def test_an_empty_pantry_says_so(self, session, user_factory):
        await user_factory(tg_id=USER_ID, coins=0)
        cb = _shop_callback("pantry")

        await shop.cb_shop_pantry(cb, session)

        assert "vuota" in cb.message.texts[-1]

    async def test_not_enough_coins_leaves_the_wallet_alone(self, session, user_factory):
        await user_factory(tg_id=USER_ID, coins=1)
        cb = _shop_callback("cexec", CONSUMABLE)

        await _call_keyed(shop.cb_consumable_execute, cb, session)

        assert await _coins(session) == 1
        assert await _purchases(session, CONSUMABLE) == 0
        assert cb.alerts and "insufficiente" in cb.alerts[0]

    async def test_a_user_without_a_wallet_is_told_to_register(self, session):
        cb = _shop_callback("cexec", CONSUMABLE)

        await _call_keyed(shop.cb_consumable_execute, cb, session)

        assert cb.alerts and "/start" in cb.alerts[0]

    async def test_unknown_consumables_are_refused_on_both_screens(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, coins=5000)
        for handler, callback_data in (
            (shop.cb_consumable_buy, ShopCb(action="cbuy", key="cons_inesistente")),
            (shop.cb_consumable_execute, ShopCb(action="cexec", key="cons_inesistente")),
        ):
            cb = _FakeCallback(callback_data)
            await _call_keyed(handler, cb, session)
            assert cb.alerts and "non disponibile" in cb.alerts[0], callback_data
        assert await _coins(session) == 5000

    async def test_the_confirm_screen_shows_the_price(self, session, user_factory):
        item = consumable_service.get_item(CONSUMABLE)
        await user_factory(tg_id=USER_ID, coins=5000)
        cb = _shop_callback("cbuy", CONSUMABLE)

        await _call_keyed(shop.cb_consumable_buy, cb, session)

        assert str(item.price) in cb.message.texts[-1]

    async def test_the_confirm_screen_is_skipped_when_it_cannot_be_afforded(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, coins=1)
        cb = _shop_callback("cbuy", CONSUMABLE)

        await _call_keyed(shop.cb_consumable_buy, cb, session)

        assert cb.message.texts == []
        assert cb.alerts and "insufficiente" in cb.alerts[0]

    async def test_a_trophy_unlocked_by_eating_is_announced(
        self, session, user_factory, monkeypatch
    ):
        """Consumable trophies are announced in the **group**, tagging the user, not in
        the private chat where the purchase happened — that is the whole point of the
        reward. The check runs after a flush precisely so it can see the purchase that
        just triggered it."""
        announced: list[tuple[int, list]] = []

        async def fake_announce(bot, db, tg_id, newly):
            announced.append((tg_id, newly))

        async def fake_check(db, tg_id):
            return ["ghiottone"]

        monkeypatch.setattr(shop, "announce_trophies", fake_announce)
        monkeypatch.setattr(shop.badge_service, "check_and_award_milestones", fake_check)

        item = consumable_service.get_item(CONSUMABLE)
        await user_factory(tg_id=USER_ID, coins=item.price)

        await _call_keyed(
            shop.cb_consumable_execute, _shop_callback("cexec", CONSUMABLE), session
        )

        assert announced == [(USER_ID, ["ghiottone"])]


class TestTagSwitcher:
    async def _own(self, session, user_factory, *keys: str) -> None:
        total = sum(shop_service.get_item(k).price for k in keys)
        await user_factory(tg_id=USER_ID, coins=total)
        for key in keys:
            await _call_keyed(shop.cb_shop_execute, _shop_callback("exec", key), session)

    async def test_a_tag_can_be_switched_off_and_back_on(self, session, user_factory):
        await self._own(session, user_factory, COSMETIC)

        off = _shop_callback("tag", COSMETIC)
        await _call_keyed(shop.cb_shop_toggle_tag, off, session)
        assert COSMETIC not in await shop_service.get_active_keys(session, USER_ID)
        assert off.toasts and "disattivato" in off.toasts[0]

        on = _shop_callback("tag", COSMETIC)
        await _call_keyed(shop.cb_shop_toggle_tag, on, session)
        assert COSMETIC in await shop_service.get_active_keys(session, USER_ID)
        assert on.toasts and "attivato" in on.toasts[0]

    async def test_a_tag_you_do_not_own_cannot_be_activated(self, session, user_factory):
        """Otherwise the switcher becomes a way to wear anything in the catalogue for
        free — the buy flow is not the only entry point to the active list."""
        await user_factory(tg_id=USER_ID, coins=0)
        cb = _shop_callback("tag", "tag_leggenda")

        await _call_keyed(shop.cb_shop_toggle_tag, cb, session)

        assert cb.alerts and "Non possiedi" in cb.alerts[0]
        assert await shop_service.get_active_keys(session, USER_ID) == []

    async def test_the_cap_on_simultaneous_tags_is_enforced(
        self, session, user_factory, monkeypatch
    ):
        monkeypatch.setattr(shop.settings, "max_active_tags", 1)
        await self._own(session, user_factory, COSMETIC, "tag_pro")

        # Buying applies each tag, so with a cap of 1 the second one is already the
        # one being worn; activating the first again must be refused.
        active = set(await shop_service.get_active_keys(session, USER_ID))
        spare = ({COSMETIC, "tag_pro"} - active).pop()
        cb = _shop_callback("tag", spare)

        await _call_keyed(shop.cb_shop_toggle_tag, cb, session)

        assert cb.alerts and "Massimo" in cb.alerts[0]
        assert set(await shop_service.get_active_keys(session, USER_ID)) == active

    async def test_the_switcher_tells_you_when_you_own_nothing(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, coins=0)
        cb = _shop_callback("tags")

        await shop.cb_shop_tags(cb, session)

        assert "Non possiedi ancora nessun tag" in cb.message.texts[-1]

    async def test_the_switcher_falls_back_when_the_edit_is_refused(
        self, session, user_factory
    ):
        """Toggling re-renders the same screen, so Telegram often answers «message is
        not modified». Without the fallback the switcher would look frozen."""
        await self._own(session, user_factory, COSMETIC)

        message = _FakeMessage()

        async def refuse_edit(text, reply_markup=None, **kw):
            raise RuntimeError("Bad Request: message is not modified")

        message.edit_text = refuse_edit
        cb = _shop_callback("tag", COSMETIC, message)

        await _call_keyed(shop.cb_shop_toggle_tag, cb, session)

        assert message.texts, "no fallback message was sent"


class TestNavigation:
    async def test_the_catalogue_lists_items_and_survives_an_empty_one(
        self, session, user_factory, monkeypatch
    ):
        await user_factory(tg_id=USER_ID, coins=5000)

        cb = _shop_callback("list")
        await shop.cb_shop_list(cb, session)
        assert cb.message.texts

        monkeypatch.setattr(catalog_loader, "get_cosmetics", dict)
        monkeypatch.setattr(shop_service, "get_cosmetics", dict)
        empty = _shop_callback("list")
        await shop.cb_shop_list(empty, session)
        assert empty.message.texts

    async def test_the_menu_and_its_categories_render(self, session, user_factory):
        await user_factory(tg_id=USER_ID, coins=5000)

        menu = _shop_callback("menu")
        await shop.cb_shop_menu(menu, session)
        assert menu.message.texts

        category = consumable_service.get_categories()[0]
        cat_cb = _shop_callback("cat", category.key)
        await _call_keyed(shop.cb_shop_category, cat_cb, session)
        assert cat_cb.message.texts

    async def test_an_unknown_category_is_refused(self, session, user_factory):
        await user_factory(tg_id=USER_ID, coins=100)
        cb = _shop_callback("cat", "inesistente")

        await _call_keyed(shop.cb_shop_category, cb, session)

        assert cb.message.texts == []

    async def test_the_locanda_opens_from_the_command_in_private(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, coins=1234)
        message = _FakeMessage()
        message.from_user = SimpleNamespace(id=USER_ID)

        await shop.cmd_locanda(message, session)

        assert message.texts

    async def test_close_deletes_the_panel_and_survives_a_failed_delete(self):
        cb = _shop_callback("close")
        await shop.cb_shop_close(cb)
        assert cb.message.deleted

        class _Undeletable(_FakeMessage):
            async def delete(self):
                raise RuntimeError("message to delete not found")

        await shop.cb_shop_close(_shop_callback("close", message=_Undeletable()))

    async def test_owned_screen_renders(self, session, user_factory):
        await user_factory(tg_id=USER_ID, coins=0)
        cb = _shop_callback("owned")
        await shop.cb_shop_owned(cb)
        assert cb.message.texts or cb.answers


class TestBalanceBelongsToTheClicker:
    async def test_every_screen_reads_the_clicking_user_not_the_panel_author(
        self, session, user_factory
    ):
        """Regression guard, widened. On a callback the panel's `from_user` is the bot,
        which has no wallet, so any screen reading the balance off the message would
        show 0. `test_shop_home_balance.py` pins the landing; these are the others.
        """
        await user_factory(tg_id=USER_ID, coins=7777)

        for handler, action in (
            (shop.cb_shop_list, "list"),
            (shop.cb_shop_menu, "menu"),
        ):
            cb = _shop_callback(action)
            await handler(cb, session)
            text = cb.message.texts[-1]
            assert "7" in text and "0 CoInn" not in text, action


class TestUserWithoutAWallet:
    async def test_the_screens_do_not_crash(self, session):
        """A user who reached the shop through a deep link before `/start` finished.
        Nothing has to work for them, but nothing may raise either."""
        for handler, action in (
            (shop.cb_shop_list, "list"),
            (shop.cb_shop_menu, "menu"),
            (shop.cb_shop_pantry, "pantry"),
            (shop.cb_shop_tags, "tags"),
        ):
            cb = _shop_callback(action, user_id=424242)
            await handler(cb, session)
            assert cb.message.texts, action

        assert (await session.execute(select(User).where(User.tg_id == 424242))).first() is None


# ---------------------------------------------------------------------------
# The way in, and the screens that have to survive an old message
# ---------------------------------------------------------------------------

class _UserMessage(_FakeMessage):
    """A message the *user* sent (the command), as opposed to a bot-sent panel."""

    def __init__(self, chat_type: str = "private", bot=None) -> None:
        super().__init__(bot=bot)
        self.from_user = SimpleNamespace(id=USER_ID, username="tizio", full_name="Tizio")
        self.chat = SimpleNamespace(
            id=USER_ID if chat_type == "private" else -100_123, type=chat_type
        )

    async def reply(self, text, reply_markup=None, **kw):
        self.texts.append(text)
        self.markups.append(reply_markup)
        return SimpleNamespace(message_id=len(self.texts))

    async def get_me(self):  # pragma: no cover - not used
        raise AssertionError

    @property
    def said(self) -> str:
        return "\n".join(self.texts)


class _StaleMessage(_FakeMessage):
    """A panel Telegram will not let the bot edit any more (too old, or unchanged)."""

    async def edit_text(self, text, reply_markup=None, **kw):
        raise RuntimeError("message is not modified")


class TestEntry:
    def teardown_method(self):
        cooldown.reset()

    async def test_in_a_group_it_only_hands_back_a_link(self, session, user_factory):
        """The catalog prints the opener's balance and its buttons act on whoever
        taps them — both wrong in a room full of people."""
        cooldown.reset()
        await user_factory(tg_id=USER_ID, coins=5000)
        message = _UserMessage(chat_type="supergroup")
        message.bot = SimpleNamespace(get_me=_get_me)

        await shop.cmd_locanda(message, session)

        url = message.markups[0].inline_keyboard[0][0].url
        assert url.endswith(f"?start=shop_{message.chat.id}")
        assert "saldo" not in message.said.lower()

    async def test_in_private_it_opens_the_home_screen(self, session, user_factory):
        cooldown.reset()
        await user_factory(tg_id=USER_ID, coins=5000)
        message = _UserMessage()

        await shop.cmd_locanda(message, session)

        assert "5,000" in message.said

    async def test_the_second_call_within_the_window_is_refused(
        self, session, user_factory
    ):
        cooldown.reset()
        await user_factory(tg_id=USER_ID, coins=5000)
        first, second = _UserMessage(), _UserMessage()

        await shop.cmd_locanda(first, session)
        await shop.cmd_locanda(second, session)

        assert first.said and "più piano" in second.said.lower()

    async def test_the_legacy_deep_link_still_opens_the_shop(self, session, user_factory):
        """Old group messages carry `?start=shop_<group_id>`; they must keep working
        or those buttons become dead links."""
        await user_factory(tg_id=USER_ID, coins=5000)
        message = _UserMessage()

        await shop.start_shop_private(message, None, -100_123, session)

        assert "5,000" in message.said


class TestStaleScreens:
    async def test_the_home_falls_back_to_a_new_message(self, session, user_factory):
        """A tap on a panel too old to edit must still show the screen."""
        await user_factory(tg_id=USER_ID, coins=5000)
        message = _StaleMessage()

        await shop._show_home(message, session, USER_ID, edit=True)

        assert message.texts and "5,000" in message.texts[-1]

    async def test_the_tag_switcher_falls_back_too(self, session, user_factory):
        await user_factory(tg_id=USER_ID, coins=5000)
        await shop_service.record_purchase(
            session, USER_ID, COSMETIC, shop_service.get_item(COSMETIC).price
        )
        await session.commit()
        message = _StaleMessage()

        await shop._show_tag_switcher(message, session, USER_ID, edit=True)

        assert message.texts and "I tuoi tag" in message.texts[-1]

    async def test_the_tag_switcher_sends_a_fresh_screen_when_asked_to(
        self, session, user_factory
    ):
        await user_factory(tg_id=USER_ID, coins=5000)
        message = _FakeMessage()

        await shop._show_tag_switcher(message, session, USER_ID, edit=False)

        assert "I tuoi tag" in message.texts[-1]


class TestEmptyMenu:
    async def test_an_empty_menu_says_so_instead_of_opening_nothing(
        self, session, user_factory, monkeypatch
    ):
        """The catalog is loaded from CSV at startup; a bad deploy can leave it
        empty, and an empty screen with buttons is worse than a clear message."""
        await user_factory(tg_id=USER_ID, coins=5000)
        monkeypatch.setattr(consumable_service, "get_categories", lambda: [])
        cb = _shop_callback("menu")

        await shop.cb_shop_menu(cb, session)

        assert cb.alerts and "vuoto" in cb.alerts[0]
        assert cb.message.texts == []


class TestOwnedKeys:
    async def test_it_reports_only_what_the_user_owns(self, session, user_factory):
        await user_factory(tg_id=USER_ID, coins=5000)
        await shop_service.record_purchase(
            session, USER_ID, COSMETIC, shop_service.get_item(COSMETIC).price
        )
        await session.commit()
        keys = list(catalog_loader.get_cosmetics().keys())

        owned = await shop._owned_keys(session, USER_ID, keys)

        assert owned == {COSMETIC}


class TestConcurrentCosmeticPurchase:
    async def test_losing_the_race_under_the_lock_refunds_the_debit(
        self, session, user_factory, monkeypatch
    ):
        """Two taps on the same «compra» button, close enough that both pass the
        fast-path check. The debit takes the wallet lock, so they serialize there;
        the loser re-checks ownership *under the lock* and rolls its own debit back.

        The race is simulated at `has_cosmetic` — the second answer flips to True,
        which is exactly what the winner's commit would have made it. On SQLite two
        sessions share one transaction, so a real two-session race is not
        expressible here (see conftest's pg fixtures); what is under test is the
        recovery, not the locking.
        """
        await user_factory(tg_id=USER_ID, coins=5000)
        answers = iter([False, True])
        real_has = shop_service.has_cosmetic

        async def _racing(db, tg_id, key):
            try:
                return next(answers)
            except StopIteration:  # pragma: no cover - later calls, if any
                return await real_has(db, tg_id, key)

        monkeypatch.setattr(shop_service, "has_cosmetic", _racing)
        cb = _shop_callback("exec", COSMETIC)

        await _call_keyed(shop.cb_shop_execute, cb, session)

        assert cb.alerts and "già questa" in cb.alerts[0]
        assert await _coins(session) == 5000, "the loser's debit must be rolled back"
        assert await _purchases(session, COSMETIC) == 0
