"""`middlewares/group_guard.py` — the gate that keeps non-members out of the bot.

This is a security boundary: everything a user can do in private (wallet, shop,
transfers, bets) sits behind it. Only its two cache helpers were covered; the
middleware itself — the part that decides *pass or reject* — was not.

The events here are **real aiogram objects** bound to a fake bot with `.as_(bot)`,
not mocks. The middleware dispatches on `isinstance(event, Message)`, so a stand-in
class would exercise a different branch than production; the previous version of
this file monkeypatched the module's `Message`/`CallbackQuery` names to work around
that, which tested the patch as much as the code. Constructing the real models and
letting the fake bot receive the outgoing API method avoids both problems.

Two behaviours are deliberately pinned because they are the ones that would be
"fixed" by mistake:

  * **fail open**: if the membership check itself errors (bot removed from the
    group, Telegram down) the user is let through. Locking everyone out of their
    own wallet is worse than admitting an ex-member for at most one cache TTL;
  * **the cache is trusted for its TTL**: a member who leaves keeps access until it
    expires or someone calls `invalidate_cache` — which is exactly what the
    join/leave handlers do.
"""

from __future__ import annotations

import datetime
import time

import pytest
from aiogram.types import CallbackQuery, Chat, InlineQuery, Message, User

import middlewares.group_guard as gg
from services import group_registry

GROUP_ID = -100_777
USER_ID = 42


class _FakeBot:
    """Receives the outgoing API calls aiogram builds from `.answer(...)`."""

    def __init__(self, status: str = "member", *, fail: bool = False) -> None:
        self.status = status
        self.fail = fail
        self.member_checks: list[tuple[int, int]] = []
        self.calls: list[object] = []

    async def __call__(self, method, *args, **kwargs):
        self.calls.append(method)
        return None

    async def get_chat_member(self, chat_id, user_id):
        self.member_checks.append((chat_id, user_id))
        if self.fail:
            raise RuntimeError("Bad Request: chat not found")
        return type("Member", (), {"status": self.status})()

    @property
    def texts(self) -> list[str]:
        return [getattr(c, "text", "") or "" for c in self.calls]


def _message(bot, chat_type: str = "private") -> Message:
    return Message(
        message_id=1,
        date=datetime.datetime.now(),
        chat=Chat(id=USER_ID if chat_type == "private" else GROUP_ID, type=chat_type),
        from_user=User(id=USER_ID, is_bot=False, first_name="Tizio"),
    ).as_(bot)


def _callback(bot, chat_type: str = "private", *, with_message: bool = True) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=USER_ID, is_bot=False, first_name="Tizio"),
        chat_instance="ci",
        data="noop",
        message=_message(bot, chat_type) if with_message else None,
    ).as_(bot)


class _Handler:
    """The downstream handler: records whether the middleware let the update through."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event, data):
        self.calls += 1
        return "handled"


@pytest.fixture(autouse=True)
def _isolated_guard():
    """The cache and the group id are module-level state shared by the whole suite."""
    gg.invalidate_all()
    group_registry.set_runtime_group_id(GROUP_ID)
    yield
    gg.invalidate_all()
    group_registry.set_runtime_group_id(None)


def _data(bot, user: User | None = None) -> dict:
    return {"bot": bot, "event_from_user": user or User(id=USER_ID, is_bot=False,
                                                        first_name="Tizio")}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class TestTheGate:
    async def test_a_member_gets_through(self):
        bot = _FakeBot("member")
        handler = _Handler()

        result = await gg.GroupMemberMiddleware()(handler, _message(bot), _data(bot))

        assert handler.calls == 1 and result == "handled"

    @pytest.mark.parametrize("status", ["left", "kicked", "banned"])
    async def test_a_non_member_is_stopped_before_the_handler(self, status):
        """The reply matters less than the fact that the handler never runs: that
        is what keeps a stranger out of someone else's wallet."""
        bot = _FakeBot(status)
        handler = _Handler()

        await gg.GroupMemberMiddleware()(handler, _message(bot), _data(bot))

        assert handler.calls == 0
        assert "Accesso negato" in bot.texts[0]

    async def test_a_non_member_pressing_a_button_gets_an_alert(self):
        bot = _FakeBot("left")
        handler = _Handler()

        await gg.GroupMemberMiddleware()(handler, _callback(bot), _data(bot))

        assert handler.calls == 0
        assert getattr(bot.calls[0], "show_alert", False) is True

    async def test_group_updates_are_never_checked(self):
        """The group is the authoritative context: a message posted in it is proof
        of membership, and checking would burn an API call per group message."""
        bot = _FakeBot("left")
        handler = _Handler()

        await gg.GroupMemberMiddleware()(handler, _message(bot, "supergroup"), _data(bot))

        assert handler.calls == 1
        assert bot.member_checks == []

    async def test_a_callback_from_the_group_is_not_checked_either(self):
        bot = _FakeBot("left")
        handler = _Handler()

        await gg.GroupMemberMiddleware()(handler, _callback(bot, "supergroup"), _data(bot))

        assert handler.calls == 1 and bot.member_checks == []

    async def test_with_no_group_configured_the_guard_is_off(self):
        """A bot with GROUP_ID=0 is a bot with no group to be a member of; gating
        would make it unusable rather than safe."""
        group_registry.set_runtime_group_id(0)
        bot = _FakeBot("kicked")
        handler = _Handler()

        await gg.GroupMemberMiddleware()(handler, _message(bot), _data(bot))

        assert handler.calls == 1 and bot.member_checks == []

    async def test_an_update_with_no_user_passes_through(self):
        """Channel posts and service updates have no `from_user`; there is nobody to
        check, and dropping them would break the bot's own bookkeeping."""
        bot = _FakeBot("member")
        handler = _Handler()
        data = {"bot": bot, "event_from_user": None}

        await gg.GroupMemberMiddleware()(handler, _message(bot), data)

        assert handler.calls == 1 and bot.member_checks == []

    async def test_an_event_that_is_neither_message_nor_callback_passes_through(self):
        """`_chat_type` returns None for anything else, which is not "private", so
        the guard must not try to gate it."""
        bot = _FakeBot("kicked")
        handler = _Handler()

        await gg.GroupMemberMiddleware()(handler, object(), _data(bot))

        assert handler.calls == 1


# ---------------------------------------------------------------------------
# The membership check itself
# ---------------------------------------------------------------------------

class TestMembershipCheck:
    async def test_the_answer_is_cached_for_the_whole_ttl(self):
        bot = _FakeBot("member")

        for _ in range(5):
            assert await gg._is_group_member(bot, USER_ID) is True

        assert len(bot.member_checks) == 1

    async def test_an_expired_entry_is_asked_again(self):
        bot = _FakeBot("member")
        await gg._is_group_member(bot, USER_ID)
        # Age the entry past the TTL instead of sleeping through it.
        is_member, ts = gg._cache[USER_ID]
        gg._cache[USER_ID] = (is_member, ts - gg._GROUP_MEMBER_CACHE_TTL - 1)

        await gg._is_group_member(bot, USER_ID)

        assert len(bot.member_checks) == 2

    async def test_a_failing_check_fails_open(self):
        """Bot removed from the group, Telegram down: everyone would be locked out
        of their own balance. Admitting an ex-member for one TTL is the lesser bug."""
        bot = _FakeBot(fail=True)

        assert await gg._is_group_member(bot, USER_ID) is True

    async def test_a_user_who_left_is_still_admitted_until_invalidated(self):
        """Pinning the known consequence of caching: `invalidate_cache` is not an
        optimisation, it is what makes a leave take effect immediately."""
        bot = _FakeBot("member")
        await gg._is_group_member(bot, USER_ID)
        bot.status = "left"

        assert await gg._is_group_member(bot, USER_ID) is True
        gg.invalidate_cache(USER_ID)
        assert await gg._is_group_member(bot, USER_ID) is False

    async def test_the_cache_does_not_grow_without_bound(self):
        """A busy group would otherwise leak one entry per user forever."""
        bot = _FakeBot("member")
        stale = time.monotonic() - gg._GROUP_MEMBER_CACHE_TTL - 1
        for uid in range(gg._CACHE_PRUNE_THRESHOLD + 1):
            gg._cache[uid] = (True, stale)

        await gg._is_group_member(bot, 10_000_000)

        assert len(gg._cache) == 1, "only the fresh entry survives the prune"

    async def test_a_fresh_entry_is_never_pruned(self):
        bot = _FakeBot("member")
        gg._cache[7] = (True, time.monotonic())
        for uid in range(gg._CACHE_PRUNE_THRESHOLD + 1):
            gg._cache[uid + 100] = (True, time.monotonic() - gg._GROUP_MEMBER_CACHE_TTL - 1)

        await gg._is_group_member(bot, 10_000_000)

        assert 7 in gg._cache

    def test_invalidate_all_drops_everything(self):
        gg._cache[1] = (True, 0.0)
        gg._cache[2] = (False, 0.0)

        gg.invalidate_all()

        assert gg._cache == {}


class TestInvalidateCache:
    def test_removes_user_from_cache(self):
        gg._cache[42] = (True, 9999.0)

        gg.invalidate_cache(42)

        assert 42 not in gg._cache

    def test_safe_when_user_not_in_cache(self):
        gg.invalidate_cache(99999)

    def test_only_removes_target_user(self):
        gg._cache[1] = (True, 0.0)
        gg._cache[2] = (False, 0.0)

        gg.invalidate_cache(1)

        assert 1 not in gg._cache and 2 in gg._cache


def _inline(bot, chat_type: str = "private") -> InlineQuery:
    return InlineQuery(
        id="iq1",
        from_user=User(id=USER_ID, is_bot=False, first_name="Tizio"),
        query="giu",
        offset="",
        chat_type=chat_type,
    ).as_(bot)


class TestInlineGate:
    async def test_a_member_querying_inline_gets_through(self):
        bot = _FakeBot("member")
        handler = _Handler()

        result = await gg.GroupMemberMiddleware()(handler, _inline(bot), _data(bot))

        assert handler.calls == 1 and result == "handled"
        assert bot.member_checks == [(GROUP_ID, USER_ID)]

    async def test_a_non_member_inline_query_is_stopped(self):
        """Il punto è che l'handler non gira mai: la card profilo non parte per un estraneo."""
        bot = _FakeBot("left")
        handler = _Handler()

        await gg.GroupMemberMiddleware()(handler, _inline(bot), _data(bot))

        assert handler.calls == 0
        # `query.answer(...)` su un InlineQuery `.as_(bot)` produce un request method
        # con `.results[]`; l'articolo di rifiuto ha l'InputMessageContent con il testo.
        assert any(
            "Accesso negato" in getattr(r.input_message_content, "message_text", "")
            for c in bot.calls
            for r in getattr(c, "results", [])
        )

    async def test_inline_is_gated_even_when_typed_inside_a_group_chat(self):
        """chat_type non è fonte affidabile: chiunque digita @bot da una chat straniera.
        Il gate membership decide, non il chat_type della query."""
        bot = _FakeBot("left")
        handler = _Handler()

        await gg.GroupMemberMiddleware()(handler, _inline(bot, "supergroup"), _data(bot))

        assert handler.calls == 0


class TestChatType:
    def test_an_inline_query_is_treated_as_private(self):
        """'private' → il gate membership scatta sempre, indipendentemente dal chat_type."""
        assert gg._chat_type(_inline(_FakeBot(), "supergroup")) == "private"

    def test_a_private_message_is_private(self):
        assert gg._chat_type(_message(_FakeBot())) == "private"

    def test_a_callback_reports_the_chat_of_its_message(self):
        assert gg._chat_type(_callback(_FakeBot(), "supergroup")) == "supergroup"

    def test_a_callback_without_a_message_has_no_chat(self):
        """Inline-mode callbacks carry no message; unknown must not read as private,
        or the guard would gate something it cannot identify."""
        assert gg._chat_type(_callback(_FakeBot(), with_message=False)) is None

    def test_anything_else_has_no_chat(self):
        assert gg._chat_type(object()) is None


class TestNonMemberStatuses:
    @pytest.mark.parametrize("status", ["left", "kicked", "banned"])
    def test_these_statuses_mean_not_a_member(self, status):
        assert status in gg._NON_MEMBER_STATUSES

    @pytest.mark.parametrize("status", ["member", "administrator", "creator", "restricted"])
    def test_these_do_not(self, status):
        """`restricted` included on purpose: a muted member is still a member, and
        must keep access to the private-chat features."""
        assert status not in gg._NON_MEMBER_STATUSES
