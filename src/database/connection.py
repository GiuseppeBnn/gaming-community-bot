from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config_data.config import settings
from database.models import Base

engine = create_async_engine(
    settings.db_url,
    echo=False,
    pool_pre_ping=True,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def create_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


_MIGRATIONS: list[str] = [
    # badges: columns added after initial deploy
    "ALTER TABLE badges ADD COLUMN IF NOT EXISTS condition_type VARCHAR(64)",
    "ALTER TABLE badges ADD COLUMN IF NOT EXISTS condition_value INTEGER",
    # users: counters added after initial deploy
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_streak INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS bets_won INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS transfers_made INTEGER NOT NULL DEFAULT 0",
    # users: bot-level ban flag (silent-drop middleware) added after initial deploy
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT false",
    # quizzes: per-rank custom prizes + decreasing consolation (added after initial deploy)
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS prize_first INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS prize_second INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS prize_third INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS prize_consolation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS prize_min INTEGER NOT NULL DEFAULT 0",
    # users: progression / cosmetics + daily-XP cap bookkeeping (added after initial deploy)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS cosmetic_tag VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS active_tags_json VARCHAR(512)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS rank_slug VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS xp_today INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS xp_today_date VARCHAR(10)",
    # badges → trophies: PlayStation-style rarity tier
    "ALTER TABLE badges ADD COLUMN IF NOT EXISTS rarity VARCHAR(16) NOT NULL DEFAULT 'bronze'",
    # wallets/ledger: balances can exceed int32 (airdrops/payouts accumulate).
    # ALTER TYPE to the same type is a no-op → idempotent like the entries above.
    "ALTER TABLE wallets ALTER COLUMN coins TYPE BIGINT",
    "ALTER TABLE ledger ALTER COLUMN amount TYPE BIGINT",
]


async def run_migrations() -> None:
    """Apply idempotent DDL migrations for columns added after the initial deploy.

    Only runs on PostgreSQL — SQLite test DBs are always created fresh by create_tables().
    """
    if engine.dialect.name != "postgresql":
        return
    async with engine.begin() as conn:
        for stmt in _MIGRATIONS:
            await conn.execute(text(stmt))
