"""
Shared fixtures for the test suite.

IMPORTANT: The os.environ.setdefault calls at the top of this file MUST stay
before any application module imports. pytest loads conftest.py before test
files, so these env vars are set before Settings() is ever instantiated.
"""

from __future__ import annotations

import os

# Provide minimal env vars so pydantic-settings can build Settings() in CI
# (where there is no .env file). In local dev the .env file takes precedence
# for any vars it defines.
os.environ.setdefault("BOT_TOKEN", "1234567890:AAFtesttoken_ci")
os.environ.setdefault("DAILY_REWARD_COINS", "100")
os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from database.models import Base, User, Wallet

# Set to a throwaway PostgreSQL to enable the `pg`-marked tests (concurrency +
# migrations). Unset → those tests skip, and the suite runs exactly as before.
#   export TEST_PG_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/gamingbot_test"
PG_URL = os.environ.get("TEST_PG_URL")

# ---------------------------------------------------------------------------
# In-memory SQLite engine — one fresh DB per test function (complete isolation)
# ---------------------------------------------------------------------------

@pytest.fixture
async def engine():
    """Creates a throwaway in-memory SQLite database with all tables."""
    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield _engine
    await _engine.dispose()


@pytest.fixture
async def session(engine):
    """Provides an AsyncSession bound to the per-test in-memory engine."""
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as s:
        yield s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_user(
    session: AsyncSession,
    tg_id: int,
    username: str = "testuser",
    coins: int = 0,
    full_name: str | None = None,
    **user_kwargs,
) -> tuple[User, Wallet]:
    """Insert a User + Wallet and commit. Shared by the SQLite and Postgres factories
    so the two backends seed identical rows."""
    user = User(
        tg_id=tg_id,
        username=username,
        full_name=full_name or f"Test {username}",
        **user_kwargs,
    )
    wallet = Wallet(tg_id=tg_id, coins=coins)
    session.add(user)
    session.add(wallet)
    await session.commit()
    return user, wallet


@pytest.fixture
def user_factory(session):
    """
    Returns an async factory: create_user(tg_id, username, coins, **user_kwargs) → (User, Wallet).

    Extra keyword args (e.g. bets_won=1, daily_streak=7) are forwarded to the User model.
    Commits immediately so subsequent queries in the same session see the row.
    """
    async def _create(
        tg_id: int,
        username: str = "testuser",
        coins: int = 0,
        full_name: str | None = None,
        **user_kwargs,
    ) -> tuple[User, Wallet]:
        return await _make_user(session, tg_id, username, coins, full_name, **user_kwargs)

    return _create


@pytest.fixture
async def seeded_session(session):
    """Session with badge catalog pre-populated (needed by badge service tests)."""
    from services.badge_service import seed_badges
    await seed_badges(session)
    return session


# ---------------------------------------------------------------------------
# Real PostgreSQL — for what SQLite structurally cannot express
# ---------------------------------------------------------------------------
#
# Two things are invisible to the SQLite suite, and both live on the money path:
#
#   1. `SELECT ... FOR UPDATE` is a no-op on SQLite, so every lock assertion in
#      the existing tests asserts behaviour, not the lock.
#   2. The in-memory engine uses StaticPool, which hands every session the SAME
#      DBAPI connection — hence the same transaction. A two-session race is not
#      expressible there at all, no matter how the test is written.
#
# Plus `run_migrations()` returns early unless the dialect is postgresql
# (src/database/connection.py), so the whole _MIGRATIONS DDL list — which runs in
# production on every deploy — has never been executed by a test.

@pytest.fixture
async def pg_engine():
    """A real PostgreSQL engine with a freshly built schema, dropped afterwards.

    Skips (never fails) when TEST_PG_URL is unset, so the default local run stays
    Docker-free and exactly as fast as before.
    """
    if not PG_URL:
        pytest.skip("TEST_PG_URL not set — see PG_URL at the top of conftest.py")

    # This fixture calls drop_all. Refuse anything that isn't obviously a throwaway
    # database: docker-compose's live DB is named `gamingbot`, one character away
    # from `gamingbot_test`, and pointing this at it would delete the bot's data.
    db_name = PG_URL.rsplit("/", 1)[-1].split("?")[0]
    if not db_name.endswith("_test"):
        pytest.fail(
            f"Refusing to run: TEST_PG_URL database is {db_name!r}, which does not end "
            "in '_test'. This fixture DROPS every table — point it at a throwaway DB."
        )

    engine = create_async_engine(
        PG_URL,
        poolclass=NullPool,  # one connection per session; none pooled across tests
        connect_args={
            # A test that deadlocks itself must fail fast instead of hanging CI.
            # These are the substitute for a pytest-timeout dependency.
            "server_settings": {"lock_timeout": "5000", "statement_timeout": "15000"},
        },
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
def pg_sessions(pg_engine):
    """The session *factory*, not a session.

    Concurrency tests need two independent sessions (two connections, two
    transactions) on the same database, so they must build their own. The flags
    mirror src/database/connection.py exactly — `expire_on_commit=False` is where
    the staleness bug being measured actually lives, so changing it here would
    make these tests measure a bot that doesn't exist.
    """
    return async_sessionmaker(
        pg_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest.fixture
async def pg_session(pg_sessions):
    """A single Postgres session, for tests that don't need a second one."""
    async with pg_sessions() as s:
        yield s


@pytest.fixture
def pg_user_factory(pg_session):
    """`user_factory`, bound to the Postgres session."""
    async def _create(
        tg_id: int,
        username: str = "testuser",
        coins: int = 0,
        full_name: str | None = None,
        **user_kwargs,
    ) -> tuple[User, Wallet]:
        return await _make_user(pg_session, tg_id, username, coins, full_name, **user_kwargs)

    return _create
