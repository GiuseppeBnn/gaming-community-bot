"""The last uncovered lines, gathered in one place.

None of these belong to a flow big enough for a file of its own — they are the
pruning branches of in-memory caches, the fallbacks a malformed catalog CSV lands
on, the "neither argument was given" arm of a lookup. They are collected here
rather than scattered as one-line additions across twelve files, so the reason for
each is written next to it.

Two kinds dominate, and both only ever run in production:

  * **cache hygiene.** Four modules keep a process-global dict keyed on a user or a
    chat. Each has a prune, and each prune is the reason a busy group cannot grow
    the bot's memory without bound. They are never reached in normal use because
    the thresholds are in the thousands;
  * **degrading instead of failing.** A catalog CSV with no usable rows, a poll
    whose options are not JSON, a Groq call that never connects. In every case the
    bot must keep working with a default rather than take the command down with it.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import aiohttp
import pytest

import filters.admin_filter as admin_filter
import middlewares.ban_guard as ban_guard
import middlewares.rate_limit as rate_limit
from database.models import PollTemplate, User
from handlers import _targeting, errors
from handlers._trophy_announce import announce_trophies
from services import (
    ai_service,
    badge_service,
    catalog_loader,
    consumable_service,
    group_registry,
    poll_service,
)
from utils import cooldown, static_reply

GROUP_ID = -100_777
USER_ID = 7


# ---------------------------------------------------------------------------
# Caches that must not grow without bound
# ---------------------------------------------------------------------------

class TestCachePruning:
    def test_the_ban_cache_drops_expired_entries_once_it_is_big(self):
        """One entry per user who ever wrote in the group; without the prune the
        dict is a slow leak for the life of the process."""
        ban_guard.invalidate_all()
        stale = time.monotonic() - ban_guard._BAN_CACHE_TTL - 1
        for uid in range(ban_guard._CACHE_PRUNE_THRESHOLD + 1):
            ban_guard._cache[uid] = (False, stale)

        ban_guard._prune_expired(time.monotonic())

        assert ban_guard._cache == {}
        ban_guard.invalidate_all()

    def test_a_small_ban_cache_is_left_alone(self):
        """Pruning on every lookup would walk the whole dict for nothing."""
        ban_guard.invalidate_all()
        ban_guard._cache[1] = (True, 0.0)

        ban_guard._prune_expired(time.monotonic())

        assert 1 in ban_guard._cache
        ban_guard.invalidate_all()

    async def test_a_fresh_ban_answer_is_reused_without_a_query(self, session):
        """The middleware runs on *every* update; a DB round-trip per update for a
        value that changes on a /ban is the reason this cache exists."""
        ban_guard.invalidate_all()
        ban_guard._cache[USER_ID] = (True, time.monotonic())

        assert await ban_guard._is_banned(session, USER_ID) is True
        ban_guard.invalidate_all()

    def test_the_static_reply_map_drops_its_oldest_half(self):
        """Keyed on (chat, user, command): a busy group fills it steadily, and the
        oldest entries point at messages long gone anyway."""
        static_reply.reset()
        for i in range(static_reply._PRUNE_THRESHOLD + 10):
            static_reply._last[(1, i, "profilo")] = i

        static_reply._prune()

        assert len(static_reply._last) == static_reply._PRUNE_THRESHOLD // 2
        assert (1, 0, "profilo") not in static_reply._last, "the oldest go first"
        static_reply.reset()

    def test_a_small_static_reply_map_is_left_alone(self):
        static_reply.reset()
        static_reply._last[(1, 2, "profilo")] = 3

        static_reply._prune()

        assert static_reply._last == {(1, 2, "profilo"): 3}
        static_reply.reset()

    def test_invalidating_one_group_leaves_the_others_cached(self):
        """A migration or a promotion concerns one chat; dropping every group's
        admin list would send the bot back to Telegram for all of them."""
        admin_filter._cache.clear()
        admin_filter._cache[GROUP_ID] = ({1}, 9_999.0)
        admin_filter._cache[-100_111] = ({2}, 9_999.0)

        admin_filter.invalidate_admin_cache(GROUP_ID)

        assert GROUP_ID not in admin_filter._cache
        assert -100_111 in admin_filter._cache
        admin_filter._cache.clear()


# ---------------------------------------------------------------------------
# Admins bypass the cooldowns
# ---------------------------------------------------------------------------

class TestAdminExemption:
    @staticmethod
    def _message(monkeypatch, *, admin: bool):
        async def _is_admin(bot, uid):
            return admin

        monkeypatch.setattr(cooldown, "is_admin", _is_admin)
        return SimpleNamespace(
            from_user=SimpleNamespace(id=USER_ID),
            bot=SimpleNamespace(id=1),
            reply=_record(),
        )

    async def test_an_admin_passes_the_loud_guard_every_time(self, monkeypatch):
        """Admins run the same commands repeatedly while moderating; throttling them
        would make the panel unusable exactly when it is needed."""
        cooldown.reset()
        message = self._message(monkeypatch, admin=True)

        assert await cooldown.guard(message, "test", 3600) is True
        assert await cooldown.guard(message, "test", 3600) is True
        assert message.reply.calls == []
        cooldown.reset()

    async def test_an_admin_passes_the_silent_one_too(self, monkeypatch):
        cooldown.reset()
        message = self._message(monkeypatch, admin=True)

        assert await cooldown.ready(message, "test", 3600) is True
        assert await cooldown.ready(message, "test", 3600) is True
        cooldown.reset()


def _record():
    class _Recorder:
        def __init__(self):
            self.calls: list[str] = []

        async def __call__(self, text, **kw):
            self.calls.append(text)

    return _Recorder()


# ---------------------------------------------------------------------------
# The rate limiter's two replies
# ---------------------------------------------------------------------------

class TestRateLimitReplies:
    async def _flood(self, monkeypatch, event, kind):
        """Fill the window on ONE middleware instance, then send one more.

        The window lives on the instance (`self._timestamps`), so a fresh
        middleware per call would never reach the limit.
        """
        monkeypatch.setattr(rate_limit, kind, type(event))
        middleware = rate_limit.RateLimitMiddleware()
        handler = _Handler()
        for _ in range(rate_limit.MAX_CALLS + 1):
            await middleware(
                handler, event, {"event_from_user": SimpleNamespace(id=USER_ID)}
            )
        return handler

    async def test_a_flooding_message_is_answered_once_and_dropped(self, monkeypatch):
        """Silence would read as a broken bot; a reply per call would be the flood."""
        event = _Answerable()
        handler = await self._flood(monkeypatch, event, "Message")

        assert handler.calls == rate_limit.MAX_CALLS
        assert event.answers and "troppi comandi" in event.answers[0][0]

    async def test_a_flooding_callback_gets_an_alert(self, monkeypatch):
        """A toast on a button press is easy to miss, and the user keeps tapping."""
        event = _Answerable()
        handler = await self._flood(monkeypatch, event, "CallbackQuery")

        assert handler.calls == rate_limit.MAX_CALLS
        assert event.answers[-1][1].get("show_alert") is True


class _Answerable:
    def __init__(self):
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


class _Handler:
    def __init__(self):
        self.calls = 0

    async def __call__(self, event, data):
        self.calls += 1


# ---------------------------------------------------------------------------
# Degrading instead of failing
# ---------------------------------------------------------------------------

class TestCatalogFallbacks:
    def test_a_ranks_file_with_no_usable_row_falls_back_to_the_defaults(
        self, tmp_path
    ):
        """Ranks drive every level title in the bot; an empty list would leave every
        profile without one, silently, after a bad deploy."""
        (tmp_path / "ranks.csv").write_text(
            "slug,name,min_level,emoji\n,,,\n", encoding="utf-8"
        )

        ranks = catalog_loader.load_ranks(str(tmp_path))

        assert ranks == list(catalog_loader.DEFAULT_RANKS)

    def test_a_categories_file_with_no_usable_row_falls_back_too(self, tmp_path):
        (tmp_path / "consumable_categories.csv").write_text(
            "key,name,emoji\n,,\n", encoding="utf-8"
        )

        cats = catalog_loader.load_consumable_categories(str(tmp_path))

        assert cats == dict(catalog_loader.DEFAULT_CONSUMABLE_CATEGORIES)

    def test_a_duplicate_category_key_is_skipped_not_merged(self, tmp_path):
        """Two rows with the same key would otherwise silently shadow each other."""
        (tmp_path / "consumable_categories.csv").write_text(
            "key,name,emoji\nbevande,Bevande,🥤\nbevande,Doppione,🥤\n", encoding="utf-8"
        )

        cats = catalog_loader.load_consumable_categories(str(tmp_path))

        assert cats["bevande"].name == "Bevande"

    async def test_an_empty_consumable_catalog_yields_no_purchase_counts(
        self, session, monkeypatch
    ):
        """Without the guard the query would build an `IN ()` over no keys."""
        monkeypatch.setattr(catalog_loader, "get_consumables", dict)

        assert await consumable_service.purchase_counts(session, USER_ID) == {}


class TestCorruptData:
    async def test_a_poll_with_unparseable_options_reads_as_no_options(self, session):
        """`options_json` is text in the DB; a bad value must not take down the
        screen that lists the polls."""
        poll = PollTemplate(question="Meglio?", options_json="{non json",
                            creator_tg_id=1, status="ready")
        session.add(poll)
        await session.commit()

        assert poll_service.options_of(poll) == []

    async def test_a_poll_whose_options_are_not_a_list_reads_as_none(self, session):
        poll = PollTemplate(question="Meglio?", options_json=json.dumps({"a": 1}),
                            creator_tg_id=1, status="ready")

        assert poll_service.options_of(poll) == []


class TestAiNetworkFailure:
    async def test_a_connection_error_becomes_the_service_error(self, monkeypatch):
        """Every caller catches `AIServiceError` and shows the fallback line; an
        aiohttp exception escaping raw would surface as a crash instead."""
        class _Boom:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                raise aiohttp.ClientConnectionError("connection refused")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(aiohttp, "ClientSession", _Boom)

        with pytest.raises(ai_service.AIServiceError):
            await ai_service.generate_completion("sys", "user", 50)

    async def test_a_timeout_becomes_the_service_error_too(self, monkeypatch):
        class _Slow:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                raise asyncio.TimeoutError()

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(aiohttp, "ClientSession", _Slow)

        with pytest.raises(ai_service.AIServiceError):
            await ai_service.generate_completion("sys", "user", 50)


# ---------------------------------------------------------------------------
# Small arms nothing else reaches
# ---------------------------------------------------------------------------

class TestTargetLookup:
    async def test_a_lookup_with_neither_id_nor_username_finds_nobody(self, session):
        """Guarding the caller: without it the query would have no WHERE clause and
        return the first user in the table."""
        assert await _targeting._lookup(session) is None


class TestTrophyAnnouncement:
    async def test_several_trophies_at_once_are_announced_as_a_list(
        self, seeded_session, user_factory, monkeypatch
    ):
        """A quiz close can unlock three at a time; three separate messages would
        be three notifications for one event."""
        await user_factory(tg_id=USER_ID, username="tizio")
        badges = (await badge_service.get_all_badges(seeded_session))[:2]
        sent: list[str] = []

        async def _send(bot, db, text, **kw):
            sent.append(text)

        monkeypatch.setattr(group_registry, "get_group_id", lambda: GROUP_ID)
        monkeypatch.setattr(group_registry, "send_group_message", _send)

        await announce_trophies(SimpleNamespace(), seeded_session, USER_ID, badges)

        assert len(sent) == 1
        assert "trofei" in sent[0]

    async def test_a_single_trophy_uses_the_singular(
        self, seeded_session, user_factory, monkeypatch
    ):
        await user_factory(tg_id=USER_ID, username="tizio")
        badges = (await badge_service.get_all_badges(seeded_session))[:1]
        sent: list[str] = []

        async def _send(bot, db, text, **kw):
            sent.append(text)

        monkeypatch.setattr(group_registry, "get_group_id", lambda: GROUP_ID)
        monkeypatch.setattr(group_registry, "send_group_message", _send)

        await announce_trophies(SimpleNamespace(), seeded_session, USER_ID, badges)

        assert "il trofeo" in sent[0]


class TestBadgeConditionEdges:
    def test_a_known_event_metric_is_described_with_its_own_wording(self):
        from services import progress_service

        key = next(iter(progress_service.EVENT_LABELS))

        text = badge_service.describe_condition("event_count", 1, key)

        assert text and "obiettivo" not in text

    def test_an_unknown_counter_condition_is_never_met(self, session):
        """A trophy row can carry a condition this build does not implement (an
        older/newer catalog); awarding it on a fall-through would be worse."""
        user = User(tg_id=USER_ID, username="tizio", full_name="Tizio")

        assert badge_service._counter_met(user, "invenzione", 1) is False


def _benign() -> Exception:
    """The real exception type: `_is_benign` checks `isinstance` first, so a plain
    Exception with the same text would take the *other* branch entirely."""
    from aiogram.exceptions import TelegramBadRequest

    return TelegramBadRequest(method=None, message="Bad Request: message is not modified")


class TestErrorHandler:
    async def test_a_benign_error_still_stops_the_button_spinner(self):
        """«message is not modified» is not worth an error to the user, but leaving
        the callback unanswered spins the client's loader forever."""
        answered = []

        class _Query:
            data = "noop"
            message = None

            async def answer(self):
                answered.append(True)

        update = SimpleNamespace(message=None, callback_query=_Query(),
                                 event_from_user=None)
        event = SimpleNamespace(update=update, exception=_benign())

        assert await errors.on_error(event) is True
        assert answered == [True]

    async def test_an_expired_query_that_cannot_be_answered_is_not_an_error(self):
        """The stop-the-spinner call is itself best-effort: the query may already
        be too old, and that must not turn a benign error into a real one."""
        class _ExpiredQuery:
            data = "noop"
            message = None

            async def answer(self):
                raise RuntimeError("query is too old and response timeout expired")

        update = SimpleNamespace(message=None, callback_query=_ExpiredQuery(),
                                 event_from_user=None)
        event = SimpleNamespace(update=update, exception=_benign())

        assert await errors.on_error(event) is True


# ---------------------------------------------------------------------------
# The session middleware, and the migration safety net
# ---------------------------------------------------------------------------

class TestDbSessionMiddleware:
    """`DbSessionMiddleware.__call__` opens the session every handler in the bot
    receives, and upserts the caller on the way in. It is the reason a handler can
    assume `db_session` exists and the User row is there."""

    @staticmethod
    def _own_session(monkeypatch, session):
        """Point the middleware at the test's session. Its own
        `async_session_maker` is bound to the module-level engine, a different
        database from the fixture's — the upsert would land somewhere invisible.
        """
        import middlewares.db_middleware as dbm

        class _Ctx:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(dbm, "async_session_maker", lambda: _Ctx())

    async def test_it_hands_the_handler_a_session_and_registers_the_caller(
        self, session, monkeypatch
    ):
        import middlewares.db_middleware as dbm
        from sqlalchemy import select as sa_select

        self._own_session(monkeypatch, session)
        seen: dict = {}

        async def handler(event, data):
            seen.update(data)
            return "handled"

        tg_user = SimpleNamespace(id=USER_ID, username="tizio", full_name="Tizio Test",
                                  first_name="Tizio", is_bot=False)

        result = await dbm.DbSessionMiddleware()(
            handler, SimpleNamespace(), {"event_from_user": tg_user}
        )

        assert result == "handled"
        assert seen["db_session"] is session
        assert await session.scalar(
            sa_select(User.username).where(User.tg_id == USER_ID)
        ) == "tizio"

    async def test_another_bot_is_never_given_a_wallet(self, session, monkeypatch):
        """Bots write in groups too; registering them would put them on the
        leaderboards and hand them a balance."""
        import middlewares.db_middleware as dbm
        from sqlalchemy import select as sa_select

        self._own_session(monkeypatch, session)

        async def handler(event, data):
            return "handled"

        tg_bot = SimpleNamespace(id=999, username="altrobot", full_name="Altro Bot",
                                 first_name="Altro", is_bot=True)

        await dbm.DbSessionMiddleware()(
            handler, SimpleNamespace(), {"event_from_user": tg_bot}
        )

        assert await session.scalar(sa_select(User.tg_id).where(User.tg_id == 999)) is None

    async def test_an_update_with_no_user_still_gets_a_session(self, session, monkeypatch):
        import middlewares.db_middleware as dbm

        self._own_session(monkeypatch, session)
        seen: dict = {}

        async def handler(event, data):
            seen.update(data)

        await dbm.DbSessionMiddleware()(handler, SimpleNamespace(), {})

        assert seen["db_session"] is session


class TestMigrationSafetyNet:
    async def test_a_send_that_reports_a_migration_is_retried_on_the_new_chat(
        self, session
    ):
        """The bot can miss the migration service message (it was offline). The Bot
        API error is the second chance: record the new id, commit it, resend. Without
        this the announcement is simply lost, and so is every one after it."""
        from aiogram.exceptions import TelegramMigrateToChat

        group_registry.set_runtime_group_id(GROUP_ID)
        new_id = -1_001_234_567
        sent: list[int] = []

        class _MigratingBot:
            async def send_message(self, chat_id, text, **kw):
                sent.append(chat_id)
                if chat_id == GROUP_ID:
                    raise TelegramMigrateToChat(
                        method=None, message="group upgraded", migrate_to_chat_id=new_id
                    )
                return SimpleNamespace(message_id=1)

        try:
            await group_registry.send_group_message(_MigratingBot(), session, "ciao")

            assert sent == [GROUP_ID, new_id]
            assert group_registry.get_group_id() == new_id
        finally:
            group_registry.set_runtime_group_id(None)

    async def test_recording_a_second_migration_overwrites_the_first(self, session):
        """`bot_state` holds one row per key: a group that migrates twice must end
        up with the latest id, not two rows or a stale one."""
        from sqlalchemy import select as sa_select

        from database.models import BotState

        group_registry.set_runtime_group_id(GROUP_ID)
        try:
            # Committed in between, as production does: each migration is its own
            # transaction, and `_upsert_state` reads before it writes.
            await group_registry.record_migration(session, GROUP_ID, -1_001)
            await session.commit()
            await group_registry.record_migration(session, -1_001, -1_002)
            await session.commit()

            rows = (await session.execute(
                sa_select(BotState.value).where(BotState.key == "effective_group_id")
            )).scalars().all()
            assert rows == ["-1002"]
        finally:
            group_registry.set_runtime_group_id(None)


# ---------------------------------------------------------------------------
# Empty-catalog and no-op guards
# ---------------------------------------------------------------------------

class TestEmptyCatalogGuards:
    async def test_no_cosmetics_in_the_catalog_means_nobody_owns_any(
        self, session, monkeypatch
    ):
        """Without the guard the query builds an `IN ()` over an empty key list."""
        from services import shop_service

        monkeypatch.setattr(catalog_loader, "get_cosmetics", dict)

        assert await shop_service.owned_cosmetic_keys(session, USER_ID) == set()

    async def test_a_grant_clipped_to_nothing_reports_zero_and_the_cap(
        self, session, user_factory
    ):
        """The daily XP cap can leave no room at all. Reporting `granted=0` with
        `capped=True` is what lets the caller say «cap raggiunto» instead of
        silently pretending the XP was awarded."""
        from config_data.config import settings
        from services import xp_service

        from utils import daytime

        await user_factory(tg_id=USER_ID, username="tizio",
                           xp_today=settings.xp_daily_participation_cap,
                           xp_today_date=daytime.local_today().isoformat())

        result = await xp_service.grant_xp(
            session, USER_ID, 50, xp_service.XpSource.daily, capped=True
        )

        assert result.granted == 0 and result.capped is True

    async def test_a_collection_trophy_with_no_prerequisites_is_never_awarded(
        self, seeded_session, user_factory
    ):
        """`condition_param` holds the required slugs; an empty one would otherwise
        be a subset of everything and unlock for free."""
        from database.models import Badge

        await user_factory(tg_id=USER_ID, username="tizio")
        seeded_session.add(Badge(
            slug="collezione-vuota", name="Collezione vuota", description="d",
            icon_emoji="🏆", category="test", rarity="bronze", xp_reward=0,
            condition_type="collection", condition_value=None, condition_param="",
        ))
        await seeded_session.commit()

        earned = await badge_service.check_and_award_milestones(seeded_session, USER_ID)

        assert all(b.slug != "collezione-vuota" for b in earned)


class TestUnknownTrophyCondition:
    async def test_a_condition_this_build_does_not_implement_is_never_met(
        self, seeded_session, user_factory
    ):
        """Trophy rows come from a CSV that can be newer than the code. An
        unrecognised condition must fall through to «not earned» — the opposite
        would hand out a trophy for a rule nobody implemented."""
        from database.models import Badge

        await user_factory(tg_id=USER_ID, username="tizio")
        seeded_session.add(Badge(
            slug="dal-futuro", name="Dal futuro", description="d",
            icon_emoji="🛸", category="test", rarity="bronze", xp_reward=0,
            condition_type="regola_inventata", condition_value=1, condition_param=None,
        ))
        await seeded_session.commit()

        earned = await badge_service.check_and_award_milestones(seeded_session, USER_ID)

        assert all(b.slug != "dal-futuro" for b in earned)


class TestSchemaBootstrap:
    async def test_create_tables_runs_against_the_configured_engine(self):
        """Called once at startup. It is idempotent (`create_all` skips what exists),
        which is why running it here against the test DB_URL is harmless."""
        from database import connection

        try:
            await connection.create_tables()
        finally:
            # This test is the only one that opens the application-level engine.
            # Do not leave its aiosqlite worker alive until interpreter shutdown.
            await connection.engine.dispose()

    async def test_migrations_are_a_no_op_outside_postgresql(self):
        """The DDL list is Postgres-specific; on SQLite it must return immediately
        instead of failing on the first `ALTER TABLE ... IF NOT EXISTS`.

        The Postgres side of this guard — the statements actually executing — is
        covered by `tests/integration/test_migrations_pg.py`, which is why
        `connection.py` keeps three uncovered lines in a run without a database.
        """
        from database import connection

        assert connection.engine.dialect.name != "postgresql"
        await connection.run_migrations()
