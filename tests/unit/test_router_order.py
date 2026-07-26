"""The router registration list is a correctness constraint, not a style choice.

Two failure modes it protects against, both silent:

* **A module nobody registers.** A new `handlers/foo.py` with its own `router` is
  simply dead until someone adds an `include_router` call — no error, no log, the
  commands just never fire. `test_every_module_with_a_router_is_registered` walks
  the package instead of trusting a hand-written list.
* **Wrong order.** aiogram tries routers in registration order and stops at the
  first match, so a catch-all in an earlier router swallows later handlers.
  `admin_betting` ends with a deny-everything for `admin_bet:*`, so it has to come
  before `betting`; `common` is the global fallback and has to come last. That used
  to be written in a comment above the calls and enforced by nothing.
"""

from __future__ import annotations

import importlib
import pkgutil

from aiogram import Dispatcher, Router

import handlers


def _modules_defining_a_router() -> dict[str, Router]:
    """Every `handlers.*` module that owns a `Router`, discovered by walking the
    package.

    There is deliberately **no exclusion list**: an exclusion list is one more thing
    to keep in sync, and it can hide a genuinely forgotten module. Instead the rule
    is exact — a module that defines `router` must be registered — and the modules
    that must not be (`_privacy`, `_trophy_announce`, `errors`, which registers on
    `dp.errors`) simply don't define one. Writing this test is what surfaced the
    unused `Router` that `errors.py` used to carry.
    """
    found: dict[str, Router] = {}
    for info in pkgutil.iter_modules(handlers.__path__):
        module = importlib.import_module(f"handlers.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, Router):
            found[info.name] = router
    return found


class TestRouterRegistry:
    def test_every_module_with_a_router_is_registered(self):
        registered = set(handlers.ROUTERS)
        missing = {
            name for name, router in _modules_defining_a_router().items()
            if router not in registered
        }
        assert not missing, (
            f"these handler modules define a router that nobody registers, so their "
            f"commands never fire: {sorted(missing)} — add them to handlers.ROUTERS"
        )

    def test_no_router_is_registered_twice(self):
        assert len(set(map(id, handlers.ROUTERS))) == len(handlers.ROUTERS)

    def test_admin_betting_precedes_betting(self):
        """`admin_betting.router` ends with a catch-all that denies unmatched
        `admin_bet:*` callbacks. Registered after `betting`, that deny would never
        be reached — and worse, `betting`'s own patterns would answer admin
        callbacks first.

        Compared by identity, not by `Router.name`: only `Router(name=...)` sets a
        readable name, and these are all built bare, so `.name` is a hex id.
        """
        from handlers import admin_betting, betting

        order = list(handlers.ROUTERS)
        assert order.index(admin_betting.router) < order.index(betting.router)

    def test_common_is_registered_last(self):
        """`common` holds the fallbacks (unknown command, generic text). Anywhere but
        last, it answers before the specific handlers get a chance."""
        from handlers import common

        assert handlers.ROUTERS[-1] is common.router


class TestRegister:
    def test_register_attaches_in_declared_order(self, monkeypatch):
        """`register(dp)` must be exactly "include ROUTERS in order" — no filtering,
        no reordering.

        Tested with throwaway routers on purpose. `ROUTERS` holds module-level
        singletons and aiogram refuses to attach a router to a second parent, so
        registering the real ones here would permanently bind them to a discarded
        Dispatcher and break the app's own `register` call in any later test.
        """
        a, b, c = Router(name="a"), Router(name="b"), Router(name="c")
        monkeypatch.setattr(handlers, "ROUTERS", (a, b, c))

        dp = Dispatcher()
        handlers.register(dp)

        assert dp.sub_routers == [a, b, c]
