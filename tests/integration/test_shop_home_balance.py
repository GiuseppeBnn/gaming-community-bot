"""Regression: the Locanda landing must show the *clicking user's* balance.

Bug (reported): buy an item, then tap "🔙 Locanda" → the balance showed 0 even
though the CoInn were untouched. ``shop:home`` routed through ``cb_shop_home``,
which used to read the balance from ``callback.message.from_user.id``. On a
callback that message is the panel the *bot* sent, so ``from_user`` is the bot —
which has no wallet → ``_balance`` returned 0. The fix passes the clicking user's
id explicitly (``callback.from_user.id``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from handlers.shop import cb_shop_home
from handlers.callbacks import ShopCb

USER_ID = 1
BOT_ID = 999_999  # the panel's author on a callback; intentionally has no wallet


class _FakeMessage:
    def __init__(self, author_id: int) -> None:
        self.from_user = SimpleNamespace(id=author_id)
        self.rendered: str | None = None

    async def edit_text(self, text: str, reply_markup=None) -> None:
        self.rendered = text

    async def answer(self, text: str, reply_markup=None) -> None:  # edit fallback
        self.rendered = text


class _FakeCallback:
    def __init__(self, user_id: int, panel_author_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.message = _FakeMessage(panel_author_id)
        self.data = ShopCb(action="home").pack()

    async def answer(self, *args, **kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_shop_home_shows_clicking_user_balance(session, user_factory):
    await user_factory(tg_id=USER_ID, coins=4200)
    cb = _FakeCallback(user_id=USER_ID, panel_author_id=BOT_ID)

    await cb_shop_home(cb, session)

    assert cb.message.rendered is not None
    assert "<b>4,200 CoInn</b>" in cb.message.rendered   # real balance, formatted
    assert "<b>0 CoInn</b>" not in cb.message.rendered    # not the bot's empty wallet
