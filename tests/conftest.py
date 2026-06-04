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
from sqlalchemy.pool import StaticPool

from database.models import Base, User, Wallet

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

    return _create


@pytest.fixture
async def seeded_session(session):
    """Session with badge catalog pre-populated (needed by badge service tests)."""
    from services.badge_service import seed_badges
    await seed_badges(session)
    return session
