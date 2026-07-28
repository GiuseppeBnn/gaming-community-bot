"""First test coverage for `_MIGRATIONS` (src/database/connection.py).

`run_migrations()` returns early unless the dialect is postgresql, and the test
suite runs on SQLite — so this DDL list has never been executed by a test, while
it runs in **production on every deploy**. These tests close that gap.

`run_migrations()` reads the module-global `engine`, so each test points that
global at the throwaway Postgres engine. That exercises the real statement list
*and* the real dialect guard, rather than a reimplementation of either.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

import database.connection as conn_mod

pytestmark = [pytest.mark.pg]


async def _columns(engine, table: str) -> dict[str, dict]:
    """{column_name: {'default': ..., 'nullable': ...}} from information_schema."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, column_default, is_nullable, data_type "
                    "FROM information_schema.columns WHERE table_name = :t"
                ),
                {"t": table},
            )
        ).all()
    return {
        r[0]: {"default": r[1], "nullable": r[2] == "YES", "type": r[3]} for r in rows
    }


async def _run_migrations(engine, monkeypatch) -> None:
    monkeypatch.setattr(conn_mod, "engine", engine)
    await conn_mod.run_migrations()


# Raw SQL on purpose: the ORM would fill in its Python-side defaults and hide the
# very thing under test (whether the migration restored the *server-side* DEFAULT).
# So every NOT-NULL-without-server-default column is supplied here EXCEPT the one
# a given test is checking.
#
# Worth knowing: on a `create_all` schema these columns have no server default at
# all (the models declare `default=0`, which SQLAlchemy applies in Python), while
# `_MIGRATIONS` re-adds them as `NOT NULL DEFAULT 0`. A fresh DB and a migrated DB
# therefore differ in their DDL — harmless for the ORM, but it means raw INSERTs
# behave differently on the two, which is exactly what test 3 pins down.
_USER_COLS = "tg_id, username, full_name, xp, onboarding_completed, is_banned, daily_streak, bets_won, transfers_made"
_USER_VALS = ":id, 'u', 'U', 0, false, false, 0, 0, 0"

# Valid on any schema: supplies every NOT-NULL column that has no server default.
INSERT_USER = f"INSERT INTO users ({_USER_COLS}, xp_today) VALUES ({_USER_VALS}, 0)"
# Valid only AFTER the migration re-added xp_today with `DEFAULT 0`. Omitting the
# column is the assertion.
INSERT_USER_WITHOUT_XP_TODAY = f"INSERT INTO users ({_USER_COLS}) VALUES ({_USER_VALS})"

# Same idea for badges.rarity (`DEFAULT 'bronze'`).
INSERT_BADGE_WITHOUT_RARITY = (
    "INSERT INTO badges (slug, name, description, icon_emoji, category, xp_reward, hidden) "
    "VALUES (:slug, 'n', 'd', 'x', 'c', 0, false)"
)


class TestMigrationsApply:
    async def test_apply_on_a_fresh_schema(self, pg_engine, monkeypatch):
        """Every statement in _MIGRATIONS parses and executes on a real Postgres.

        This is the headline gain: a typo or a Postgres-version incompatibility in
        that list currently reaches production before anyone finds out.
        """
        await _run_migrations(pg_engine, monkeypatch)

    async def test_are_idempotent(self, pg_engine, monkeypatch):
        """Running twice must not raise — the property every `IF NOT EXISTS` and
        every same-type `ALTER ... TYPE` in the list claims in its comment."""
        await _run_migrations(pg_engine, monkeypatch)
        await _run_migrations(pg_engine, monkeypatch)

    async def test_skipped_on_sqlite(self, engine, monkeypatch):
        """The dialect guard: this DDL is Postgres-only and must no-op elsewhere,
        or a SQLite dev DB would blow up at startup."""
        monkeypatch.setattr(conn_mod, "engine", engine)
        await conn_mod.run_migrations()  # must not raise


class TestMigrationsRepairAnOldDeploy:
    """The tests with teeth: assert `set(model_cols) == set(db_cols)` on a schema
    that `create_all` just built would be tautological. These simulate a database
    that predates the columns, which is the situation `_MIGRATIONS` exists for.
    """

    async def test_readds_the_round_duration_to_an_older_guess_table(
        self, pg_engine, monkeypatch
    ):
        """`guess_rounds.round_duration_seconds` drives the auto-close task.

        `create_all` skips tables that already exist, so a deploy that already has
        `guess_rounds` would never get this column and every round would fail to
        open. DEFAULT 0 means "close it by hand", so existing rounds keep behaving
        exactly as they did.
        """
        async with pg_engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE guess_rounds DROP COLUMN round_duration_seconds")
            )

        await _run_migrations(pg_engine, monkeypatch)

        col = (await _columns(pg_engine, "guess_rounds"))["round_duration_seconds"]
        assert col["nullable"] is False
        assert "0" in col["default"]

    async def test_readds_columns_missing_from_an_older_schema(self, pg_engine, monkeypatch):
        async with pg_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE users DROP COLUMN xp_today"))
            await conn.execute(text("ALTER TABLE users DROP COLUMN xp_today_date"))
            await conn.execute(text("ALTER TABLE badges DROP COLUMN rarity"))

        assert "xp_today" not in await _columns(pg_engine, "users")

        await _run_migrations(pg_engine, monkeypatch)

        users = await _columns(pg_engine, "users")
        badges = await _columns(pg_engine, "badges")
        assert "xp_today" in users and "xp_today_date" in users
        assert "rarity" in badges
        # The declared NOT NULL DEFAULTs must come back too, not just the columns:
        # an existing row on a live DB has to get a usable value.
        assert users["xp_today"]["nullable"] is False
        assert badges["rarity"]["nullable"] is False

        # xp_today / rarity are deliberately NOT supplied: the server default the
        # migration declared is what has to fill them in.
        async with pg_engine.begin() as conn:
            await conn.execute(text(INSERT_USER_WITHOUT_XP_TODAY), {"id": 99})
            await conn.execute(text(INSERT_BADGE_WITHOUT_RARITY), {"slug": "s"})
            xp_today = (
                await conn.execute(text("SELECT xp_today FROM users WHERE tg_id = 99"))
            ).scalar_one()
            rarity = (
                await conn.execute(text("SELECT rarity FROM badges WHERE slug = 's'"))
            ).scalar_one()
        assert xp_today == 0
        assert rarity == "bronze"

    async def test_widens_int32_balances_to_bigint(self, pg_engine, monkeypatch):
        """`ALTER COLUMN coins TYPE BIGINT` is the only migration with no
        `IF NOT EXISTS` guard. Simulate the pre-widening schema and prove a balance
        past 2^31 survives — the exact value the state export/import round-trip
        already cares about.
        """
        async with pg_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE wallets ALTER COLUMN coins TYPE INTEGER"))
            await conn.execute(text("ALTER TABLE ledger ALTER COLUMN amount TYPE INTEGER"))
        assert (await _columns(pg_engine, "wallets"))["coins"]["type"] == "integer"

        await _run_migrations(pg_engine, monkeypatch)

        assert (await _columns(pg_engine, "wallets"))["coins"]["type"] == "bigint"
        assert (await _columns(pg_engine, "ledger"))["amount"]["type"] == "bigint"

        big = 12_345_678_900  # > 2^31: would fail as INTEGER
        async with pg_engine.begin() as conn:
            await conn.execute(text(INSERT_USER), {"id": 7})
            await conn.execute(
                text("INSERT INTO wallets (tg_id, coins) VALUES (7, :c)"), {"c": big}
            )
            stored = (
                await conn.execute(text("SELECT coins FROM wallets WHERE tg_id = 7"))
            ).scalar_one()
        assert stored == big


    async def test_widens_the_other_int32_counters(self, pg_engine, monkeypatch):
        """`users.xp` and `betting_options.total_wagered` accumulate without any
        upper bound of their own — XP from airdrops, wagers from a pot paid in
        coins, which are already BIGINT. Overflowing either raises mid-transaction
        on a write path, so they get widened for the same reason balances did.
        """
        async with pg_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE users ALTER COLUMN xp TYPE INTEGER"))
            await conn.execute(
                text("ALTER TABLE betting_options ALTER COLUMN total_wagered TYPE INTEGER")
            )

        await _run_migrations(pg_engine, monkeypatch)

        assert (await _columns(pg_engine, "users"))["xp"]["type"] == "bigint"
        assert (
            await _columns(pg_engine, "betting_options")
        )["total_wagered"]["type"] == "bigint"

        big = 12_345_678_900  # > 2^31
        async with pg_engine.begin() as conn:
            await conn.execute(text(INSERT_USER), {"id": 8})
            await conn.execute(text("UPDATE users SET xp = :x WHERE tg_id = 8"), {"x": big})
            stored = (
                await conn.execute(text("SELECT xp FROM users WHERE tg_id = 8"))
            ).scalar_one()
        assert stored == big


class TestLedgerIndexes:
    async def test_history_indexes_exist(self, pg_engine, monkeypatch):
        """/storico filters `from_tg_id OR to_tg_id` ordered by created_at. Both
        composite indexes must exist — via create_all on a new DB, and via
        _MIGRATIONS on a deployed one (create_all skips existing tables)."""
        await _run_migrations(pg_engine, monkeypatch)
        async with pg_engine.connect() as conn:
            names = {
                r[0]
                for r in (
                    await conn.execute(
                        text("SELECT indexname FROM pg_indexes WHERE tablename = 'ledger'")
                    )
                ).all()
            }
        assert "ix_ledger_from_created" in names
        assert "ix_ledger_to_created" in names

    async def test_migrations_create_them_on_a_preexisting_table(self, pg_engine, monkeypatch):
        """The deployed-DB path: the table already exists without the indexes, so
        only _MIGRATIONS can add them."""
        async with pg_engine.begin() as conn:
            await conn.execute(text("DROP INDEX IF EXISTS ix_ledger_from_created"))
            await conn.execute(text("DROP INDEX IF EXISTS ix_ledger_to_created"))

        await _run_migrations(pg_engine, monkeypatch)

        async with pg_engine.connect() as conn:
            names = {
                r[0]
                for r in (
                    await conn.execute(
                        text("SELECT indexname FROM pg_indexes WHERE tablename = 'ledger'")
                    )
                ).all()
            }
        assert {"ix_ledger_from_created", "ix_ledger_to_created"} <= names
